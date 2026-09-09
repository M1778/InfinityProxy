"""Hostagent package tests: settings, sing-box rendering, platform setters,
TunController lifecycle, and the /host control routes (all fakes, no docker)."""

from __future__ import annotations

from typing import Any

import pytest

from hostagent import config as cfg
from hostagent.app import create_app
from hostagent.docker_ctl import TunController, TunRuntimeError
from hostagent.platform import (
    PROXY_CAPABILITY_NONE,
    _gsettings_set,
    _kwriteconfig_set,
    _split_endpoint,
    _system_proxy_set,
)
from hostagent.singbox import render_tun_config


def test_settings_defaults_and_from_env(monkeypatch):
    default = cfg.HostAgentSettings()
    assert default.host == "127.0.0.1"
    assert default.port == 8788
    assert default.uid == 1000
    assert default.bus_path == ""
    assert default.tun_interface == "tun0"
    assert default.singbox_image.startswith("ghcr.io/sagernet/sing-box")

    monkeypatch.setenv("INFINITY_HOST_UID", "1002")
    monkeypatch.setenv("INFINITY_HOSTAGENT_PORT", "9900")
    monkeypatch.setenv("INFINITY_TUN_MTU", "1400")
    settings = cfg.HostAgentSettings.from_env()
    assert settings.uid == 1002
    assert settings.port == 9900
    assert settings.tun_mtu == 1400
    assert settings.bus_path == "/run/user/1002/bus"


def test_split_endpoint():
    assert _split_endpoint("127.0.0.1:20001") == ("127.0.0.1", 20001)
    with pytest.raises(ValueError):
        _split_endpoint("nonsense")
    with pytest.raises(ValueError):
        _split_endpoint("host:abc")


def test_render_tun_config_structure():
    config = render_tun_config("127.0.0.1:20001", "u1", "p1")
    tun = config["inbounds"][0]
    assert tun["type"] == "tun"
    assert tun["interface_name"] == "tun0"
    assert tun["auto_route"] is True and tun["strict_route"] is True
    assert tun["stack"] == "system"
    assert config["route"]["final"] == "host-tunnel"
    socks = config["outbounds"][0]
    assert socks["type"] == "socks"
    assert socks["server"] == "127.0.0.1" and socks["server_port"] == 20001
    assert socks["username"] == "u1" and socks["password"] == "p1"
    remote = config["dns"]["servers"][0]
    assert remote["address_resolver"] == "local"
    assert remote["detour"] == "host-tunnel"
    assert config["dns"]["final"] == "remote"


def test_render_tun_config_drops_empty_creds():
    config = render_tun_config("127.0.0.1:20001", "", "")
    socks = config["outbounds"][0]
    assert "username" not in socks and "password" not in socks


def test_render_tun_config_rejects_bad_endpoint():
    with pytest.raises(ValueError):
        render_tun_config("oops", "u", "p")


def test_gsettings_commands():
    calls: list[list[str]] = []

    def run(cmd, *, check=True):  # noqa: ANN001
        calls.append(cmd)

    result = _gsettings_set(True, "127.0.0.1:20001", run)
    assert result["mode"] == "manual"
    assert calls[-1] == ["gsettings", "set", "org.gnome.system.proxy", "mode", "manual"]
    assert len(calls) == 7  # 3 schemas x (host+port) + mode

    calls.clear()
    result = _gsettings_set(False, None, run)
    assert result["mode"] == "none"
    assert calls == [["gsettings", "set", "org.gnome.system.proxy", "mode", "none"]]


def test_kwriteconfig_commands():
    calls: list[list[str]] = []

    def run(cmd, *, check=True):  # noqa: ANN001
        calls.append(cmd)

    result = _kwriteconfig_set(True, "127.0.0.1:20001", run)
    assert result["mode"] == "manual"
    assert any("httpProxy" in c and "http://127.0.0.1:20001" in c for c in calls)
    assert calls[-1][-3:] == ["--key", "ProxyType", "1"]

    calls.clear()
    result = _kwriteconfig_set(False, None, run)
    assert result["mode"] == "none"
    assert calls[-1][-3:] == ["--key", "ProxyType", "0"]


def test_system_proxy_set_none_capability():
    with pytest.raises(ValueError):
        _system_proxy_set(PROXY_CAPABILITY_NONE, True, "127.0.0.1:20001")


class FakeTunController:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.stopped: int = 0
        self.running: bool = False
        self.fail_start: TunRuntimeError | None = None
        self.fail_stop: TunRuntimeError | None = None

    def is_running(self) -> bool:
        return self.running

    def start(self, config: dict) -> None:
        if self.fail_start:
            raise self.fail_start
        self.started.append(config)
        self.running = True

    def stop(self) -> None:
        if self.fail_stop:
            raise self.fail_stop
        self.stopped += 1
        self.running = False


def _proxy_setter():
    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool, str | None]] = []

        def __call__(self, capability, enabled, endpoint):  # noqa: ANN001
            self.calls.append((capability, enabled, endpoint))
            return {"mode": "manual" if enabled else "none"}

    return Recorder()


@pytest.fixture()
def hostagent_client(tmp_path, monkeypatch):
    fake_ctl = FakeTunController()
    clock = {"now": 100.0}

    def tick() -> float:
        return clock["now"]

    setter = _proxy_setter()

    app = create_app(
        cfg.HostAgentSettings(engine_name_prefix="inf-test"),
        clock=tick,
        tun_controller=fake_ctl,
        proxy_setter=setter,
        platform_system=lambda: "linux",
        platform_machine=lambda: "x86_64",
        docker_socket_present=lambda: True,
        tun_present=lambda _dev: True,
        proxy_capability=lambda: "gsettings",
    )
    app.config["TESTING"] = True
    app._test_tun_controller = fake_ctl  # type: ignore[attr-defined]
    return app.test_client(), fake_ctl, clock, setter


def test_get_host_snapshot(hostagent_client):
    test, fake_ctl, clock, _ = hostagent_client
    res = test.get("/host")
    assert res.status_code == 200
    body = res.get_json()
    assert body["hostagent"]["available"] is True
    assert body["hostagent"]["uptime_s"] == 0  # test clock never advances
    assert body["platform"]["system"] == "linux"
    assert body["capabilities"] == {"proxy": "gsettings", "tun": True, "docker": True}
    assert body["proxy"] == {"enabled": False, "endpoint": None, "mode": "none"}
    assert body["tun"]["running"] is False

    clock["now"] = 130.0
    assert test.get("/host").get_json()["hostagent"]["uptime_s"] == 30


def test_proxy_enable_and_disable(hostagent_client):
    test, _, _, setter = hostagent_client
    res = test.post(
        "/host/proxy", json={"enabled": True, "endpoint": "127.0.0.1:20001"}
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["proxy"]["enabled"] is True
    assert body["proxy"]["endpoint"] == "127.0.0.1:20001"
    assert body["proxy"]["mode"] == "manual"
    assert setter.calls[-1] == ("gsettings", True, "127.0.0.1:20001")

    res = test.post("/host/proxy", json={"enabled": False})
    assert res.status_code == 200
    assert res.get_json()["proxy"]["mode"] == "none"


def test_proxy_requires_endpoint_when_enabling(hostagent_client):
    test, _, _, _ = hostagent_client
    res = test.post("/host/proxy", json={"enabled": True})
    assert res.status_code == 400
    assert res.get_json()["error"]["code"] == "invalid_request"


def test_proxy_unsupported_capability(tmp_path):
    app = create_app(
        cfg.HostAgentSettings(engine_name_prefix="inf-test"),
        tun_controller=FakeTunController(),
        proxy_setter=lambda _c, _e, _p: {},
        proxy_capability=lambda: PROXY_CAPABILITY_NONE,
    )
    app.config["TESTING"] = True
    test = app.test_client()
    res = test.post(
        "/host/proxy", json={"enabled": True, "endpoint": "127.0.0.1:20001"}
    )
    assert res.status_code == 409
    assert res.get_json()["error"]["code"] == "host_unsupported"


def test_proxy_setter_errors_map(hostagent_client, tmp_path):
    test, _, _, _ = hostagent_client

    def raise_value(_c, _e, _p):
        raise ValueError("no session bus")

    def raise_runtime(_c, _e, _p):
        raise RuntimeError("boom")

    app2 = create_app(
        cfg.HostAgentSettings(engine_name_prefix="inf-test"),
        tun_controller=FakeTunController(),
        proxy_setter=raise_value,
        proxy_capability=lambda: "gsettings",
    )
    app2.config["TESTING"] = True
    t2 = app2.test_client()
    res = t2.post("/host/proxy", json={"enabled": True, "endpoint": "127.0.0.1:1"})
    body = res.get_json()
    assert res.status_code == 409 and body["error"]["code"] == "host_unsupported"

    app3 = create_app(
        cfg.HostAgentSettings(engine_name_prefix="inf-test"),
        tun_controller=FakeTunController(),
        proxy_setter=raise_runtime,
        proxy_capability=lambda: "gsettings",
    )
    app3.config["TESTING"] = True
    t3 = app3.test_client()
    res = t3.post("/host/proxy", json={"enabled": True, "endpoint": "127.0.0.1:1"})
    assert res.status_code == 502 and res.get_json()["error"]["code"] == "host_runtime"


def test_tun_enable_builds_config(hostagent_client):
    test, fake_ctl, _, _ = hostagent_client
    res = test.post(
        "/host/tun",
        json={
            "enabled": True,
            "endpoint": "127.0.0.1:20001",
            "username": "u",
            "password": "p",
        },
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["tun"]["enabled"] is True
    assert body["tun"]["running"] is True
    assert body["tun"]["endpoint"] == "127.0.0.1:20001"
    assert len(fake_ctl.started) == 1
    assert fake_ctl.started[0]["route"]["final"] == "host-tunnel"

    res = test.post("/host/tun", json={"enabled": False})
    assert res.status_code == 200
    assert res.get_json()["tun"]["running"] is False
    assert fake_ctl.stopped == 1


def test_tun_requires_endpoint(hostagent_client):
    test, _, _, _ = hostagent_client
    res = test.post("/host/tun", json={"enabled": True})
    assert res.status_code == 400


def test_tun_needs_capabilities(tmp_path):
    app = create_app(
        cfg.HostAgentSettings(engine_name_prefix="inf-test"),
        tun_controller=FakeTunController(),
        tun_present=lambda _dev: False,
        docker_socket_present=lambda: False,
        proxy_capability=lambda: "gsettings",
    )
    app.config["TESTING"] = True
    res = app.test_client().post(
        "/host/tun", json={"enabled": True, "endpoint": "127.0.0.1:20001"}
    )
    assert res.status_code == 502
    assert res.get_json()["error"]["code"] == "host_privilege"


def test_tun_runtime_failure_maps(tmp_path):
    fail = TunRuntimeError("cannot reach the Docker daemon")

    def failing_start(_config):
        raise fail

    ctl = FakeTunController()
    ctl.fail_start = fail
    app = create_app(
        cfg.HostAgentSettings(engine_name_prefix="inf-test"),
        tun_controller=ctl,
        tun_present=lambda _dev: True,
        docker_socket_present=lambda: True,
        proxy_capability=lambda: "gsettings",
    )
    app.config["TESTING"] = True
    res = app.test_client().post(
        "/host/tun", json={"enabled": True, "endpoint": "127.0.0.1:20001"}
    )
    assert res.status_code == 502
    assert res.get_json()["error"]["code"] == "host_privilege"


def test_tun_controller_with_fake_docker():
    created_kw: dict[str, Any] = {}
    calls: dict[str, list] = {"put_archive": [], "start": [], "remove": [], "stop": []}

    class FakeContainer:
        def __init__(self) -> None:
            self.status = "running"

        def put_archive(self, path, tar):  # noqa: ANN001
            calls["put_archive"].append((path, bool(tar)))

        def start(self):
            calls["start"].append(True)

        def remove(self, force=False):  # noqa: ANN001
            calls["remove"].append(force)
            self.status = "exited"

        def stop(self):
            calls["stop"].append(True)

    class FakeContainers:
        def __init__(self) -> None:
            self._by_name: dict[str, FakeContainer] = {}

        def get(self, name):
            if name not in self._by_name:
                raise KeyError(name)
            return self._by_name[name]

        def create(self, image, **kw):  # noqa: ANN003
            if not image:
                raise ValueError("missing image")
            created_kw.update(kw)
            container = FakeContainer()
            self._by_name[kw["name"]] = container
            return container

    class FakeDockerClient:
        def __init__(self) -> None:
            self.containers = FakeContainers()

    settings = cfg.HostAgentSettings(engine_name_prefix="inf-test")
    controller = TunController(settings, docker_client=FakeDockerClient())

    assert controller.container_name() == "inf-test-host-tun"
    assert controller.is_running() is False  # no container yet

    controller.start({"log": {"level": "warn"}})
    assert created_kw["name"] == "inf-test-host-tun"
    assert created_kw["network_mode"] == "host"
    assert created_kw["privileged"] is True
    assert created_kw["devices"] == ["/dev/net/tun:/dev/net/tun"]
    assert calls["put_archive"] and calls["start"]
    assert controller.is_running() is True

    controller.stop()
    assert calls["stop"] and calls["remove"]
    assert controller.is_running() is False
