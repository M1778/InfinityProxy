"""Host controller + engine /host route tests. No docker, no hostagent binary."""

from __future__ import annotations

from typing import Any

import pytest

from engine.config import Settings
from engine.host import HostAgentError, HostController
from engine.models import Tunnel


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8000,
        db_path=str(tmp_path / "test.db"),
        engine_name_prefix="inf-test",
        panel_base_url="http://127.0.0.1:8000",
        hostagent_url="http://127.0.0.1:8788",
        **overrides,
    )


def _tunnel(tid, port, state="running") -> Tunnel:
    return Tunnel(
        tunnel_id=tid,
        state=state,
        port=port,
        username=f"u_{tid}",
        password=f"p_{tid}",
        node_count_requested=1,
        node_count_granted=1,
        auto_renew=True,
        created_at_s=1000.0,
        updated_at_s=1000.0,
    )


class FakeClock:
    def __init__(self, times: list[float]) -> None:
        self._times = times
        self.i = 0

    def __call__(self) -> float:
        value = self._times[min(self.i, len(self._times) - 1)]
        self.i += 1
        return value


class FakeResponse:
    def __init__(self, *, status=200, payload=None, raises=None, chunks=()):
        self.status_code = status
        self._payload = payload
        self.raises = raises
        self.chunks = chunks

    def __enter__(self):
        if self.raises:
            raise self.raises
        return self

    def __exit__(self, *exc):
        return False

    def json(self):
        return self._payload

    def iter_content(self, chunk_size):  # noqa: ANN001
        yield from self.chunks


class FakeAgentSession:
    """Serves agent /host/* routes and proxied egress measurements in one stub."""

    def __init__(self) -> None:
        self.gets: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []
        self.agent_payload: dict | None = None
        self.agent_status = 200
        self.agent_get_raises: Exception | None = None
        self.proxy_chunks: dict[int, bytes] = {}
        self.proxy_status: dict[int, int] = {}

    def get(self, url, **kwargs):
        self.gets.append({"url": url, **kwargs})
        if url.rstrip("/").endswith("/host"):
            if self.agent_get_raises:
                return FakeResponse(raises=self.agent_get_raises)
            return FakeResponse(status=self.agent_status, payload=self.agent_payload)
        port = int(kwargs["proxies"]["https"].rsplit(":", 1)[-1])
        return FakeResponse(
            status=self.proxy_status.get(port, 200),
            chunks=(self.proxy_chunks.get(port, b""),),
        )

    def post(self, url, json=None, timeout=None):  # noqa: ANN001
        self.posts.append({"url": url, "json": json})
        return FakeResponse(status=self.agent_status, payload=self.agent_payload)


def test_measure_reports_latency_and_throughput(tmp_path):
    session = FakeAgentSession()
    session.proxy_chunks = {20001: b"x" * 196608, 20002: b"x" * 65536}
    controller = HostController(
        _settings(tmp_path), session=session, clock=FakeClock([0.0, 0.1, 0.5])
    )
    result = controller.measure(_tunnel("tu_a", 20001))
    assert result is not None
    assert result["latency_ms"] == 100.0  # (0.1 - 0.0) * 1000
    expected = (196608 / 1024) / 0.4
    assert result["throughput_kb_s"] == pytest.approx(expected, abs=0.1)


def test_measure_rejects_bad_status_and_no_port(tmp_path):
    session = FakeAgentSession()
    session.proxy_status = {20001: 503}
    controller = HostController(
        _settings(tmp_path), session=session, clock=FakeClock([0.0])
    )
    assert controller.measure(_tunnel("tu_a", 20001)) is None
    assert controller.measure(_tunnel("tu_a", 0)) is None


def test_pick_best_ranks_and_caches(tmp_path):
    session = FakeAgentSession()
    session.proxy_chunks = {20001: b"x" * 262144, 20002: b"x" * 524288}
    controller = HostController(
        _settings(tmp_path), session=session, clock=FakeClock([0.0, 0.1, 0.5])
    )
    tunnels = [_tunnel("tu_a", 20001), _tunnel("tu_b", 20002)]
    pick = controller.pick_best(tunnels)
    assert pick["cached"] is False
    assert pick["tunnel"] == "tu_b"  # equal latency, higher throughput wins
    assert len(pick["measurements"]) == 2
    assert pick["stale_in_s"] == 60

    gets_after_first = len(session.gets)
    pick2 = controller.pick_best(tunnels)
    assert pick2["cached"] is True
    assert pick2["tunnel"] == "tu_b"
    assert len(session.gets) == gets_after_first  # no re-measure within TTL

    re_measured = len(session.gets)
    controller.pick_best(tunnels, force=True)
    assert len(session.gets) > re_measured


def test_pick_best_expiry_re_measures(tmp_path):
    session = FakeAgentSession()
    session.proxy_chunks = {20001: b"x", 20002: b"x"}
    controller = HostController(
        _settings(tmp_path),
        session=session,
        clock=FakeClock([0.0, 0.1] + [0.5] * 8 + [60.5]),
    )
    tunnels = [_tunnel("tu_a", 20001), _tunnel("tu_b", 20002)]
    controller.pick_best(tunnels)
    gets = len(session.gets)
    stale = controller.pick_best(tunnels)
    assert stale["cached"] is False  # 60.5 - 0.5 >= TTL 60
    assert len(session.gets) > gets


def test_pick_best_stopped_tunnels_skipped(tmp_path):
    session = FakeAgentSession()
    session.proxy_chunks = {20001: b"x"}
    controller = HostController(
        _settings(tmp_path), session=session, clock=FakeClock([0.0])
    )
    tunnels = [_tunnel("tu_a", 20001, state="stopped")]
    with pytest.raises(HostAgentError) as exc:
        controller.pick_best(tunnels)
    assert exc.value.code == "host_target_unavailable"
    assert session.gets == []


def test_pick_best_no_running_tunnels_raises(tmp_path):
    controller = HostController(_settings(tmp_path), session=FakeAgentSession())
    with pytest.raises(HostAgentError) as exc:
        controller.pick_best([])
    assert exc.value.code == "host_target_unavailable"


def test_host_state_degraded_when_agent_down(tmp_path):
    session = FakeAgentSession()
    session.agent_get_raises = ConnectionError("refused")
    controller = HostController(_settings(tmp_path), session=session)
    assert controller.host_state() is None
    status = controller.status([_tunnel("tu_a", 20001)])
    assert status["hostagent"]["available"] is False
    assert "unreachable" in status["hostagent"]["error"]
    assert status["proxy"] is None and status["tun"] is None


def test_status_merges_target_tunnels_from_endpoint(tmp_path):
    session = FakeAgentSession()
    session.agent_payload = {
        "hostagent": {"available": True, "uptime_s": 12},
        "proxy": {"enabled": True, "endpoint": "127.0.0.1:20001", "mode": "manual"},
        "tun": {"enabled": False, "endpoint": None, "iface": "tun0", "running": False},
    }
    controller = HostController(_settings(tmp_path), session=session)
    status = controller.status([_tunnel("tu_a", 20001), _tunnel("tu_b", 20002)])
    assert status["hostagent"]["available"] is True
    assert status["proxy"]["tunnel"] == "tu_a"
    assert status["tun"]["tunnel"] is None
    assert {c["tunnel"] for c in status["tunnels"]} == {"tu_a", "tu_b"}


def test_set_proxy_and_tun_happy_path(tmp_path):
    session = FakeAgentSession()
    session.agent_payload = {"proxy": {"enabled": True, "endpoint": "127.0.0.1:20001"}}
    controller = HostController(_settings(tmp_path), session=session)
    tunnel = _tunnel("tu_a", 20001)
    result = controller.set_proxy(True, tunnel)
    assert result["tunnel"] == "tu_a"
    sent = session.posts[-1]["json"]
    assert sent == {"enabled": True, "endpoint": "127.0.0.1:20001"}

    session.agent_payload = {"tun": {"enabled": True, "running": True}}
    result = controller.set_tun(True, tunnel)
    assert result["tunnel"] == "tu_a"
    sent = session.posts[-1]["json"]
    assert sent == {
        "enabled": True,
        "endpoint": "127.0.0.1:20001",
        "username": "u_tu_a",
        "password": "p_tu_a",
    }

    off = controller.set_proxy(False, None)
    assert off["tunnel"] is None
    assert session.posts[-1]["json"] == {"enabled": False}


def test_set_proxy_requires_target(tmp_path):
    controller = HostController(_settings(tmp_path), session=FakeAgentSession())
    with pytest.raises(HostAgentError) as exc:
        controller.set_proxy(True, None)
    assert exc.value.code == "host_target_unavailable"
    with pytest.raises(HostAgentError) as exc:
        controller.set_tun(True, _tunnel("tu_a", 0))
    assert exc.value.code == "host_target_unavailable"


def test_agent_error_propagates_code_and_status(tmp_path):
    session = FakeAgentSession()
    session.agent_status = 502
    session.agent_payload = {"error": {"code": "host_privilege", "message": "no perms"}}
    controller = HostController(_settings(tmp_path), session=session)
    with pytest.raises(HostAgentError) as exc:
        controller.set_proxy(True, _tunnel("tu_a", 20001))
    assert exc.value.code == "host_privilege"
    assert exc.value.status == 502


# ---------- engine routes ----------


class FakeHost:
    def __init__(self) -> None:
        self.pick: dict[str, Any] = {
            "tunnel": "tu_seed",
            "cached": False,
            "stale_in_s": 60,
            "measurements": [],
        }
        self.pick_raises: HostAgentError | None = None
        self.pick_calls: list[bool] = []
        self.proxy_calls: list[tuple[bool, Tunnel | None]] = []
        self.tun_calls: list[tuple[bool, Tunnel | None]] = []
        self.status_payload: dict[str, Any] = {"ok": True}

    def pick_best(self, tunnels, force=False):  # noqa: ANN001
        self.pick_calls.append(force)
        if self.pick_raises:
            raise self.pick_raises
        return self.pick

    def status(self, tunnels):  # noqa: ANN001
        return self.status_payload

    def set_proxy(self, enabled, tunnel):  # noqa: ANN001
        self.proxy_calls.append((enabled, tunnel))
        return {
            "proxy": {
                "enabled": enabled,
                "tunnel": tunnel.tunnel_id if tunnel else None,
            }
        }

    def set_tun(self, enabled, tunnel):  # noqa: ANN001
        self.tun_calls.append((enabled, tunnel))
        return {
            "tun": {
                "enabled": enabled,
                "tunnel": tunnel.tunnel_id if tunnel else None,
            }
        }


@pytest.fixture()
def host_client(tmp_path):
    from engine.app import create_app

    settings = _settings(tmp_path, host_enabled=True)
    store = _new_store(settings)
    fake_host = FakeHost()
    app = create_app(
        settings=settings,
        store=store,
        controller=FakeContainerDriver(),
        background=False,
        host=fake_host,
    )
    app.config["TESTING"] = True
    app._test_store = store  # type: ignore[attr-defined]
    app._test_host = fake_host  # type: ignore[attr-defined]
    return app.test_client(), store, fake_host


def _new_store(settings):
    from engine.db import Store

    store = Store(settings.db_path)
    store.create_schema()
    store.save_tunnel(_tunnel("tu_seed", 20001))
    return store


class FakeContainerDriver:
    def is_running(self, tunnel_id):  # noqa: ANN001
        return True

    def start(self, tunnel, config):  # noqa: ANN001
        pass

    def update(self, tunnel_id, config):  # noqa: ANN001
        pass

    def stop(self, tunnel_id):  # noqa: ANN001
        pass

    def reconcile(self, expected_ids):  # noqa: ANN001
        pass


def test_host_disabled_409(tmp_path):
    from engine.app import create_app

    settings = _settings(tmp_path, host_enabled=False)
    store = _new_store(settings)
    app = create_app(
        settings=settings,
        store=store,
        controller=FakeContainerDriver(),
        background=False,
    )
    app.config["TESTING"] = True
    test = app.test_client()
    for method, path in (
        ("get", "/host"),
        ("post", "/host/pick"),
        ("post", "/host/proxy"),
        ("post", "/host/tun"),
    ):
        res = getattr(test, method)(path, json={})
        assert res.status_code == 409
        assert res.get_json()["error"]["code"] == "host_features_disabled"


def test_host_get_ok(host_client):
    test, _, fake_host = host_client
    res = test.get("/host")
    assert res.status_code == 200
    assert res.get_json() == {"ok": True}


def test_host_pick_auto(host_client):
    test, _, fake_host = host_client
    res = test.post("/host/pick", json={"tunnel": "auto"})
    assert res.status_code == 200
    assert res.get_json()["tunnel"] == "tu_seed"
    assert fake_host.pick_calls == [True]  # forced, never served stale


def test_host_pick_no_target_409(host_client):
    test, _, fake_host = host_client
    fake_host.pick_raises = HostAgentError(
        409, "host_target_unavailable", "no running tunnels"
    )
    res = test.post("/host/pick", json={"tunnel": "auto"})
    assert res.status_code == 409
    assert res.get_json()["error"]["code"] == "host_target_unavailable"


def test_host_proxy_enable_explicit_tunnel(host_client):
    test, _, fake_host = host_client
    res = test.post("/host/proxy", json={"enabled": True, "tunnel": "tu_seed"})
    assert res.status_code == 200
    enabled, tunnel = fake_host.proxy_calls[-1]
    assert enabled is True and tunnel.tunnel_id == "tu_seed"


def test_host_proxy_auto_resolves_pick(host_client):
    test, store, fake_host = host_client
    res = test.post("/host/proxy", json={"enabled": True, "tunnel": "auto"})
    assert res.status_code == 200
    enabled, tunnel = fake_host.proxy_calls[-1]
    assert enabled is True and tunnel.tunnel_id == "tu_seed"


def test_host_proxy_disable_needs_no_target(host_client):
    test, _, fake_host = host_client
    res = test.post("/host/proxy", json={"enabled": False})
    assert res.status_code == 200
    enabled, tunnel = fake_host.proxy_calls[-1]
    assert enabled is False and tunnel is None


def test_host_tun_enable_passes_credentials(host_client):
    test, _, fake_host = host_client
    res = test.post("/host/tun", json={"enabled": True, "tunnel": "tu_seed"})
    assert res.status_code == 200
    enabled, tunnel = fake_host.tun_calls[-1]
    assert enabled is True and tunnel.username == "u_tu_seed"


def test_host_target_missing_409(host_client):
    test, _, fake_host = host_client
    res = test.post("/host/proxy", json={"enabled": True, "tunnel": "no_such"})
    assert res.status_code == 409
    assert res.get_json()["error"]["code"] == "host_target_unavailable"


def test_host_agent_unreachable_502(host_client):
    test, _, fake_host = host_client
    fake_host.pick_raises = HostAgentError(502, "hostagent_unreachable", "down")
    res = test.post("/host/proxy", json={"enabled": True, "tunnel": "auto"})
    assert res.status_code == 502
    assert res.get_json()["error"]["code"] == "hostagent_unreachable"
