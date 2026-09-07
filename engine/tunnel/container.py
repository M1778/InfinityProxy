"""Per-tunnel sing-box container control (ADR-0001).

Each tunnel is one sing-box container on the host network. The config is copied
into the container as an in-memory tar, never bind-mounted: the Docker daemon
resolves bind-mount sources on its own host, so a mount breaks whenever the
engine itself runs inside a container.
"""

from __future__ import annotations

import io
import json as _json
import tarfile
from typing import Any

from engine.config import Settings
from engine.models import Tunnel

try:
    from docker.errors import NotFound as _ContainerNotFound
except ImportError:
    _ContainerNotFound = KeyError

_CONFIG_PATH = "/etc/sing-box/config.json"


class TunnelRuntimeUnavailable(RuntimeError):
    """The Docker runtime is not reachable; tunnel containers cannot run."""


class ContainerController:
    """Drives the Docker engine to spawn, update, and reap tunnel containers."""

    def __init__(self, settings: Settings, docker_client: Any | None = None) -> None:
        self._settings = settings
        self._docker_client = docker_client

    @property
    def docker(self) -> Any:
        """The docker client, lazily built from the environment on first use."""
        if self._docker_client is not None:
            return self._docker_client
        try:
            import docker
        except ImportError as e:
            raise TunnelRuntimeUnavailable(
                "the engine needs the Docker SDK to run tunnel containers; "
                "install it or run on a host with the Docker socket"
            ) from e
        try:
            client = docker.from_env()
            client.ping()
        except (docker.errors.DockerException, OSError) as e:
            raise TunnelRuntimeUnavailable(
                "cannot reach the Docker daemon; the engine needs the Docker "
                "socket to run tunnel containers"
            ) from e
        self._docker_client = client
        return client

    def _container_name(self, tunnel_id: str) -> str:
        return f"{self._settings.engine_name_prefix}-{tunnel_id}"

    def start(self, tunnel: Tunnel, config: dict) -> None:
        """Spawn the container for a fresh tunnel."""
        self._run(self._container_name(tunnel.tunnel_id), config)

    def update(self, tunnel_id: str, config: dict) -> None:
        """Re-roll a tunnel's config: replace the container with a new one."""
        name = self._container_name(tunnel_id)
        self._remove(name)
        self._run(name, config)

    def restart(self, tunnel_id: str) -> None:
        """Restart the container in place (same shipped config)."""
        container = self.docker.containers.get(self._container_name(tunnel_id))
        container.restart()

    def stop(self, tunnel_id: str) -> None:
        """Stop and remove the container for a tunnel."""
        self._remove(self._container_name(tunnel_id))

    def reconcile(self, expected_ids: set[str]) -> None:
        """Remove containers that are the engine's but not in expected_ids."""
        prefix = self._settings.engine_name_prefix
        for container in self.docker.containers.list(
            all=True, filters={"name": prefix}
        ):
            name = container.name or ""
            if not name.startswith(f"{prefix}-"):
                continue
            suffix = name.removeprefix(f"{prefix}-")
            if suffix not in expected_ids:
                container.remove(force=True)

    def _run(self, name: str, config: dict) -> None:
        client = self.docker
        container = client.containers.create(
            self._settings.singbox_image,
            # The official image entrypoint is bare `sing-box`; `run` must be
            # given explicitly or the container just prints help and exits.
            command=["run", "-c", _CONFIG_PATH],
            name=name,
            network_mode="host",
            restart_policy={"Name": "always"},
        )
        try:
            container.put_archive("/etc/", _config_tar(config))
            container.start()
        except BaseException:
            # The container may have been created but never got a usable
            # config; do not leave a half-rolled shell behind.
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001 - cleanup is best-effort
                pass
            raise

    def _remove(self, name: str) -> None:
        try:
            container = self.docker.containers.get(name)
        except (_ContainerNotFound, KeyError):
            container = None
        if container is not None:
            # stop() is best-effort: docker returns a 304 for an already-stopped
            # container and may hiccup on a dying one. remove(force=True) must
            # still clear the container either way, otherwise a still-running
            # container would block cleanup with a 409.
            try:
                container.stop()
            except Exception:  # noqa: BLE001 - cleanup must proceed regardless
                pass
            container.remove(force=True)


def _config_tar(config: dict) -> bytes:
    # The official sing-box image has no /etc/sing-box, and put_archive cannot
    # create its target directory, so the archive carries a nested member and
    # is extracted into /etc to create the path on the fly.
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as tar:
        data = _json.dumps(config, indent=2).encode("utf-8")
        directory = tarfile.TarInfo("sing-box/")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        tar.addfile(directory)
        info = tarfile.TarInfo("sing-box/config.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return payload.getvalue()
