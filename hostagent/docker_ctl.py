"""TUN container lifecycle over the host's Docker socket (ADR-0010).

Same docker-sdk pattern as the engine's ContainerController: containers
get their config as an in-memory tar, never a bind mount, because /etc is a
container-local path even though the agent and the container share the host
network namespace.
"""

from __future__ import annotations

from typing import Any

from engine.tunnel.container import config_tar
from hostagent.config import HostAgentSettings

try:
    from docker.errors import NotFound as _ContainerNotFound

    def _docker_api_error() -> tuple[type[Exception], ...]:
        from docker.errors import APIError, DockerException

        return (APIError, DockerException)

except ImportError:
    _ContainerNotFound = KeyError

    def _docker_api_error() -> tuple[type[Exception], ...]:
        return (OSError,)


class TunRuntimeError(RuntimeError):
    """The TUN container could not be created, shipped its config, or started."""


class TunController:
    _CONFIG_PATH = "/etc/sing-box/config.json"

    def __init__(
        self, settings: HostAgentSettings, docker_client: Any | None = None
    ) -> None:
        self._settings = settings
        self._docker_client = docker_client

    @property
    def docker(self) -> Any:
        if self._docker_client is not None:
            return self._docker_client
        try:
            import docker
        except ImportError as e:
            raise TunRuntimeError(
                "the hostagent needs the Docker SDK to run the TUN container"
            ) from e
        try:
            client = docker.from_env()
            client.ping()
        except (docker.errors.DockerException, OSError) as e:
            raise TunRuntimeError(
                "cannot reach the Docker daemon; the hostagent needs the Docker "
                "socket to run the TUN container"
            ) from e
        self._docker_client = client
        return client

    def container_name(self) -> str:
        return f"{self._settings.engine_name_prefix}-host-tun"

    def is_running(self) -> bool:
        try:
            container = self.docker.containers.get(self.container_name())
        except (_ContainerNotFound, KeyError):
            return False
        except _docker_api_error():
            return False
        return bool(container.status == "running")

    def start(self, config: dict) -> None:
        client = self.docker
        try:
            container = client.containers.create(
                self._settings.singbox_image,
                command=["run", "-c", self._CONFIG_PATH],
                name=self.container_name(),
                network_mode="host",
                privileged=True,
                restart_policy={"Name": "always"},
                devices=[f"{self._settings.tun_device}:{self._settings.tun_device}"],
            )
        except _docker_api_error() as exc:
            raise TunRuntimeError(str(exc)) from exc
        try:
            container.put_archive("/etc/", config_tar(config))
            container.start()
        except _docker_api_error() as exc:
            self._remove()
            raise TunRuntimeError(str(exc)) from exc

    def stop(self) -> None:
        self._remove()

    def _remove(self) -> None:
        try:
            container = self.docker.containers.get(self.container_name())
        except (_ContainerNotFound, KeyError):
            container = None
        if container is not None:
            try:
                container.stop()
            except Exception:  # noqa: BLE001 - cleanup must proceed regardless
                pass
            container.remove(force=True)
