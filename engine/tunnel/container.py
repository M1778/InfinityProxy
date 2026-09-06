"""Per-tunnel sing-box container control (ADR-0001).

Each tunnel is one sing-box container on the host network with a read-only
mount of its rendered config, restarted as a whole when the config changes.
"""

from __future__ import annotations

import json as _json
import os
import shutil
import tempfile
from typing import Any

from engine.config import Settings
from engine.models import Tunnel

try:
    from docker.errors import NotFound as _ContainerNotFound
except ImportError:
    _ContainerNotFound = KeyError

_CONFIG_MOUNT_TARGET = "/etc/sing-box/config.json"


class TunnelRuntimeUnavailable(RuntimeError):
    """The Docker runtime is not reachable; tunnel containers cannot run."""


class ContainerController:
    """Drives the Docker engine to spawn, update, and reap tunnel containers."""

    def __init__(self, settings: Settings, docker_client: Any | None = None) -> None:
        self._settings = settings
        self._docker_client = docker_client
        self._config_paths: dict[str, str] = {}

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
        """Restart the container in place (same config mount)."""
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
        config_dir = tempfile.mkdtemp(prefix=f"{name}-")
        config_path = os.path.join(config_dir, "config.json")
        with open(config_path, "w", encoding="utf-8") as fh:
            _json.dump(config, fh)
        client.containers.run(
            self._settings.singbox_image,
            # The official image entrypoint is bare `sing-box`; `run` must be
            # given explicitly or the container just prints help and exits.
            command=["run", "-c", _CONFIG_MOUNT_TARGET],
            name=name,
            network_mode="host",
            volumes={config_path: {"bind": _CONFIG_MOUNT_TARGET, "mode": "ro"}},
            detach=True,
            restart_policy={"Name": "always"},
        )
        self._config_paths[name] = config_path

    def _remove(self, name: str) -> None:
        try:
            container = self.docker.containers.get(name)
        except (_ContainerNotFound, KeyError):
            container = None
        if container is not None:
            # stop() is a no-op on an already-stopped container (304), which
            # still lets remove() clear it up.
            container.stop()
            container.remove()
        config_path = self._config_paths.pop(name, None)
        if config_path:
            shutil.rmtree(os.path.dirname(config_path), ignore_errors=True)
