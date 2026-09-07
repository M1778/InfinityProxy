"""ContainerController doctored-fake-driver tests. No real docker needed."""  # noqa: E501

from __future__ import annotations

from typing import Any

import docker
import pytest

from engine.config import Settings
from engine.models import Tunnel
from engine.tunnel.container import ContainerController, TunnelRuntimeUnavailable


class _FakeContainer:
    def __init__(self) -> None:
        self._removed = False

    def put_archive(self, path: str, data: bytes) -> None:  # noqa: ARG002
        pass

    def start(self) -> None:
        raise docker.errors.APIError(
            '500 Server Error: Internal Server Error ("device or resource busy")'
        )

    def remove(self, force: bool = False) -> None:  # noqa: ARG002
        self._removed = True

    def stop(self) -> None:
        pass


class _FakeDocker:
    def __init__(self) -> None:
        self.created: _FakeContainer | None = None

    def ping(self) -> None:
        pass

    class containers:  # noqa: N801 - mirrors the docker SDK surface
        @classmethod
        def create(cls, *args: Any, **kwargs: Any) -> _FakeContainer:
            client = _FakeDocker._instance
            container = _FakeContainer()
            client.created = container
            return container

        @classmethod
        def list(cls, *args: Any, **kwargs: Any) -> list[_FakeContainer]:
            return []


_FakeDocker._instance = None  # type: ignore[attr-defined]


def _controller() -> tuple[ContainerController, _FakeDocker]:
    settings = Settings(
        host="127.0.0.1",
        port=8000,
        db_path="/tmp/irrelevant.db",
        engine_name_prefix="inf-test",
    )
    fake = _FakeDocker()
    _FakeDocker._instance = fake  # type: ignore[attr-defined]
    controller = ContainerController(settings)
    controller._docker_client = fake  # type: ignore[attr-defined]
    return controller, fake


def _tunnel() -> Tunnel:
    return Tunnel(tunnel_id="tu_test0001", state="starting", port=10000)


def test_start_translates_docker_api_error():
    controller, fake = _controller()
    with pytest.raises(TunnelRuntimeUnavailable, match="device or resource busy"):
        controller.start(_tunnel(), {"outbounds": []})
    assert fake.created is not None
    assert fake.created._removed
