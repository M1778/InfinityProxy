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


class _FakeImages:
    def __init__(self) -> None:
        self.missing = False
        self.pulled: list[str] = []
        self.pull_error: Exception | None = None

    def get(self, name: str) -> object:
        if self.missing:
            raise docker.errors.ImageNotFound(name)
        return object()

    def pull(self, name: str) -> object:
        if self.pull_error is not None:
            raise self.pull_error
        self.pulled.append(name)
        self.missing = False
        return object()


class _FakeDocker:
    def __init__(self) -> None:
        self.created: _FakeContainer | None = None
        self.images = _FakeImages()

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
    assert fake.images.pulled == []


def test_first_use_pulls_missing_singbox_image():
    controller, fake = _controller()
    fake.images.missing = True
    with pytest.raises(TunnelRuntimeUnavailable, match="device or resource busy"):
        controller.start(_tunnel(), {"outbounds": []})
    assert fake.images.pulled == ["ghcr.io/sagernet/sing-box:v1.11.6"]
    assert fake.created is not None  # creation was attempted after the pull


def test_image_pull_failure_is_translated():
    controller, fake = _controller()
    fake.images.missing = True
    fake.images.pull_error = docker.errors.APIError(
        '500 Server Error: Internal Server Error ("pull access denied")'
    )
    with pytest.raises(TunnelRuntimeUnavailable, match="cannot pull sing-box image"):
        controller.start(_tunnel(), {"outbounds": []})
    assert fake.created is None  # aborted before create
    assert fake.images.pulled == []
