"""Panel backend tests: fake engine client + injected dial; no poll threads, no network."""  # noqa: E501

from __future__ import annotations

import json
import threading
from typing import Any

import pytest
import requests

from panel.app import create_app
from panel.client import EngineClient, EngineError
from panel.config import PanelSettings
from panel.history import SnapshotHistory

FAKE_STATUS: dict[str, Any] = {
    "engine": "ok",
    "uptime_s": 42,
    "pool": {
        "total": 30,
        "alive": 20,
        "dead": 5,
        "untested": 5,
        "in_use": 8,
        "assignable": 12,
        "by_protocol": [
            {"protocol": "ss", "total": 30, "alive": 20, "dead": 5, "untested": 5}
        ],
        "stale_sources": 0,
    },
    "tunnels": {"active": 2, "degraded": 0},
    "sources": [{"name": "ebrasha", "last_fetch_s": 90, "next_fetch_s": 810}],
    "last_fetch_per_source": {"ebrasha": "2026-09-07T02:00:00Z"},
}

FAKE_TUNNEL: dict[str, Any] = {
    "id": "tu_1",
    "host": "127.0.0.1",
    "port": 12001,
    "username": "u_1",
    "password": "p_1",
    "state": "running",
    "degraded": False,
    "nodes": [],
}


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        content: bytes = b"{}",
        reason: str = "OK",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.reason = reason

    def json(self) -> Any:
        if self._payload is not None:
            return self._payload
        return json.loads(self.content)


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def status(self) -> dict[str, Any]:
        self.calls.append(("status",))
        return dict(FAKE_STATUS)

    def tunnels(self) -> dict[str, Any]:
        self.calls.append(("tunnels",))
        return {"tunnels": [dict(FAKE_TUNNEL)]}

    def create_tunnel(
        self, node_count: int, auto_renew: bool = False
    ) -> dict[str, Any]:
        self.calls.append(("create", node_count, auto_renew))
        return {**dict(FAKE_TUNNEL), "id": "tu_new", "port": 12002}

    def renew_tunnel(self, tunnel_id: str) -> dict[str, Any]:
        self.calls.append(("renew", tunnel_id))
        return {"id": tunnel_id}

    def delete_tunnel(self, tunnel_id: str) -> None:
        self.calls.append(("delete", tunnel_id))

    def nodes(self, params: dict[str, str] | None = None) -> dict[str, Any]:
        self.calls.append(("nodes", dict(params or {})))
        return {"nodes": [], "count": 0, "limit": 200}

    def node(self, node_id: str) -> dict[str, Any]:
        if node_id == "nope":
            raise EngineError(404, "not_found", "no node nope")
        self.calls.append(("node", node_id))
        return {"id": node_id, "uri": "vless://a@1.2.3.4:443"}

    def refresh_source(self, source_name: str) -> dict[str, Any]:
        self.calls.append(("refresh", source_name))
        return {"name": source_name, "last_fetch_s": 3, "next_fetch_s": 897}

    def host(self) -> dict[str, Any]:
        self.calls.append(("host",))
        return {
            "hostagent": {"available": True, "uptime_s": 12},
            "proxy": {"enabled": False, "endpoint": None, "mode": "none"},
            "tun": {"enabled": False, "running": False},
        }

    def host_pick(self, tunnel: str = "auto") -> dict[str, Any]:
        self.calls.append(("host_pick", tunnel))
        return {"tunnel": "tu_1", "cached": False, "stale_in_s": 60, "measurements": []}

    def host_set_proxy(
        self, enabled: bool, tunnel: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("host_set_proxy", enabled, tunnel))
        return {"proxy": {"enabled": enabled, "tunnel": "tu_1" if enabled else None}}

    def host_set_tun(self, enabled: bool, tunnel: str | None = None) -> dict[str, Any]:
        self.calls.append(("host_set_tun", enabled, tunnel))
        return {"tun": {"enabled": enabled, "tunnel": "tu_1" if enabled else None}}


class RaisingEngine(FakeEngine):
    def renew_tunnel(self, tunnel_id: str) -> dict[str, Any]:
        if tunnel_id == "ghost":
            raise EngineError(404, "not_found", f"no tunnel {tunnel_id}")
        return {"id": tunnel_id}


class EngineDown(FakeEngine):
    def status(self) -> dict[str, Any]:
        raise EngineError(0, "engine_unreachable", "connection refused")


@pytest.fixture()
def panel():
    fake = FakeEngine()
    app = create_app(settings=PanelSettings(), engine=fake, poll=False)
    app.config["TESTING"] = True
    app._test_engine = fake  # type: ignore[attr-defined]
    return app.test_client(), fake


def test_snapshot_placeholder(panel):
    test, _ = panel
    res = test.get("/api/snapshot")
    assert res.status_code == 200
    body = res.get_json()
    assert body["reachable"] is False
    assert body["t"] == 0.0


def test_crawl_updates_snapshot_and_history(panel):
    test, _ = panel
    test.application.crawl_once()
    res = test.get("/api/snapshot")
    assert res.status_code == 200
    body = res.get_json()
    assert body["reachable"] is True
    assert body["status"]["pool"]["alive"] == 20
    assert len(body["tunnels"]) == 1
    history = test.get("/api/history").get_json()
    assert history["t"] == [pytest.approx(body["t"])]
    assert history["pool_alive"] == [20]
    assert history["tunnel_active"] == [2]


def test_events_stream_yields_snapshot(panel):
    test, _ = panel
    test.application.crawl_once()
    resp = test.get("/api/events", buffered=False)
    assert resp.mimetype == "text/event-stream"
    chunk = next(iter(resp.response))
    text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
    assert text.startswith("event: snapshot")
    data = text.split("data: ", 1)[1]
    payload = json.loads(data)
    assert payload["reachable"] is True
    resp.close()


def test_events_stream_stays_alive_without_crawl():
    fake = FakeEngine()
    app = create_app(settings=PanelSettings(sse_heartbeat_s=1), engine=fake, poll=False)
    app.config["TESTING"] = True
    test = app.test_client()
    resp = test.get("/api/events", buffered=False)
    chunks = []
    for chunk in resp.response:
        if isinstance(chunk, bytes):
            chunks.append(chunk.decode("utf-8"))
        else:
            chunks.append(chunk)
        if sum(c.count("event: heartbeat") for c in chunks) >= 1:
            break
    assert any("event: heartbeat" in c for c in chunks)
    resp.close()


def test_create_tunnel_action(panel):
    test, fake = panel
    res = test.post("/api/tunnels", json={"node_count": 4, "auto_renew": True})
    assert res.status_code == 200
    assert res.get_json()["id"] == "tu_new"
    assert fake.calls[-1] == ("create", 4, True)


def test_create_tunnel_invalid_body(panel):
    test, _ = panel
    assert test.post("/api/tunnels", json={"node_count": "x"}).status_code == 400


def test_renew_and_delete_actions(panel):
    test, fake = panel
    assert test.post("/api/tunnels/tu_1/renew").status_code == 200
    assert fake.calls[-1] == ("renew", "tu_1")
    assert test.delete("/api/tunnels/tu_1").status_code == 200
    assert fake.calls[-1] == ("delete", "tu_1")


def test_nodes_actions_pass_params(panel):
    test, fake = panel
    res = test.get("/api/nodes?state=alive&limit=10")
    assert res.status_code == 200
    assert fake.calls[-1] == ("nodes", {"state": "alive", "limit": "10"})
    assert test.get("/api/nodes/nope").status_code == 404
    assert test.get("/api/nodes/tu_1").get_json()["uri"].startswith("vless://")


def test_refresh_source_action(panel):
    test, fake = panel
    res = test.post("/api/sources/ebrasha/refresh")
    assert res.status_code == 200
    assert fake.calls[-1] == ("refresh", "ebrasha")


def test_test_tunnel_dials_through():
    fake = FakeEngine()

    def fake_dial(tunnel, test_url):
        return {"ok": True, "exit_ip": "9.9.9.9", "latency_ms": 12.3}

    app = create_app(settings=PanelSettings(), engine=fake, dial=fake_dial, poll=False)
    app.config["TESTING"] = True
    test = app.test_client()
    res = test.post("/api/tunnels/tu_1/test")
    assert res.status_code == 200
    assert res.get_json() == {"ok": True, "exit_ip": "9.9.9.9", "latency_ms": 12.3}
    assert test.post("/api/tunnels/missing/test").status_code == 404


def test_default_dial_returns_error_json():
    from panel.app import _default_dial

    result = _default_dial(dict(FAKE_TUNNEL, port=1), "http://127.0.0.1:1/")
    assert result["ok"] is False
    assert "error" in result


def test_index_served(panel):
    test, _ = panel
    res = test.get("/")
    assert res.status_code == 200
    assert "InfinityProxy" in res.get_data(as_text=True)
    assert res.get_data(as_text=True).count("chart.umd.min.js") == 1


def test_call_always_passes_timeout_with_override_winning(monkeypatch):
    seen: list[tuple[Any, ...]] = []

    def fake_request(method: str, url: str, timeout: float | None = None, **kw: Any):
        seen.append((method, url, timeout, kw))
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr("panel.client.requests.request", fake_request)
    client = EngineClient("http://127.0.0.1:8787/", timeout=7.5)
    assert client.status() == {"ok": True}
    client._call("POST", "/x", timeout=1.25, json={"a": 1})
    assert seen[0][0:3] == ("GET", "http://127.0.0.1:8787/status", 7.5)
    assert seen[1][0:3] == ("POST", "http://127.0.0.1:8787/x", 1.25)
    assert seen[1][3] == {"json": {"a": 1}}


def test_refresh_source_uses_scaled_timeout(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_request(method: str, url: str, timeout: float | None = None, **kw: Any):
        seen["timeout"] = timeout
        return _FakeResponse(200, {"name": "ebrasha", "last_fetch_s": 3})

    monkeypatch.setattr("panel.client.requests.request", fake_request)
    client = EngineClient("http://127.0.0.1:8787", timeout=5.0)
    assert client.refresh_source("ebrasha")["last_fetch_s"] == 3
    assert seen["timeout"] == 60.0


def test_call_maps_request_exception_to_engine_unreachable(monkeypatch):
    def boom(method: str, url: str, timeout: float | None = None, **kw: Any):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr("panel.client.requests.request", boom)
    with pytest.raises(EngineError) as excinfo:
        EngineClient("http://127.0.0.1:8787").status()
    assert excinfo.value.status == 0
    assert excinfo.value.code == "engine_unreachable"


def test_call_maps_error_json_onto_engine_error(monkeypatch):
    def fake_request(method: str, url: str, timeout: float | None = None, **kw: Any):
        return _FakeResponse(404, {"error": {"code": "not_found", "message": "nope"}})

    monkeypatch.setattr("panel.client.requests.request", fake_request)
    with pytest.raises(EngineError) as excinfo:
        EngineClient("http://127.0.0.1:8787").node("ghost")
    assert excinfo.value.status == 404
    assert excinfo.value.code == "not_found"
    assert excinfo.value.message == "nope"


def test_call_returns_none_on_204_and_json_on_2xx(monkeypatch):
    responses = iter(
        [_FakeResponse(204, content=b""), _FakeResponse(200, {"count": 3})]
    )

    def fake_request(method: str, url: str, timeout: float | None = None, **kw: Any):
        return next(responses)

    monkeypatch.setattr("panel.client.requests.request", fake_request)
    client = EngineClient("http://127.0.0.1:8787")
    assert client.delete_tunnel("tu_1") is None
    assert client.status() == {"count": 3}


def test_snapshot_includes_config(panel):
    test, _ = panel
    body = test.get("/api/snapshot").get_json()
    assert body["config"] == {
        "engine_url": "http://127.0.0.1:8787",
        "panel_host": "127.0.0.1",
        "panel_port": 8000,
        "test_url": "https://api.ipify.org",
    }


def test_index_served_as_html_with_modal(panel):
    test, _ = panel
    res = test.get("/")
    assert res.mimetype == "text/html"
    assert "modal" in res.get_data(as_text=True)


def test_app_css_keeps_modal_hidden_rule(panel):
    test, _ = panel
    res = test.get("/app.css")
    assert res.status_code == 200
    assert ".modal[hidden]" in res.get_data(as_text=True)


def test_tunnel_test_dial_failure_maps_to_502():
    fake = FakeEngine()

    def failing_dial(tunnel: dict[str, Any], test_url: str) -> dict[str, Any]:
        return {"ok": False, "error": "egress probe failed", "latency_ms": None}

    app = create_app(
        settings=PanelSettings(), engine=fake, dial=failing_dial, poll=False
    )
    app.config["TESTING"] = True
    res = app.test_client().post("/api/tunnels/tu_1/test")
    assert res.status_code == 502
    assert res.get_json()["error"]["code"] == "dial_failed"


def test_renew_engine_error_maps_to_same_status():
    app = create_app(settings=PanelSettings(), engine=RaisingEngine(), poll=False)
    app.config["TESTING"] = True
    res = app.test_client().post("/api/tunnels/ghost/renew")
    assert res.status_code == 404
    assert res.get_json() == {
        "error": {"code": "not_found", "message": "no tunnel ghost"}
    }


def test_action_engine_down_maps_to_502():
    class DownRenew(FakeEngine):
        def renew_tunnel(self, tunnel_id: str) -> dict[str, Any]:
            raise EngineError(0, "engine_unreachable", "connection refused")

    app = create_app(settings=PanelSettings(), engine=DownRenew(), poll=False)
    app.config["TESTING"] = True
    res = app.test_client().post("/api/tunnels/tu_1/renew")
    assert res.status_code == 502
    assert res.get_json()["error"]["code"] == "engine_unreachable"


def test_crawl_engine_error_marks_snapshot_unreachable():
    app = create_app(settings=PanelSettings(), engine=EngineDown(), poll=False)
    app.config["TESTING"] = True
    test = app.test_client()
    app.crawl_once()
    body = test.get("/api/snapshot").get_json()
    assert body["reachable"] is False
    assert body["error"] == {
        "code": "engine_unreachable",
        "message": "connection refused",
    }


def test_poll_guard_skips_overlapping_crawl():
    """A slow crawl must not overlap itself; panel/app.py has no guard today."""  # noqa: E501
    fake = FakeEngine()
    app = create_app(settings=PanelSettings(), engine=fake, poll=False)
    app.config["TESTING"] = True
    app.test_client()

    entered = threading.Event()
    release = threading.Event()
    status_calls = {"n": 0}

    def slow_status() -> dict[str, Any]:
        status_calls["n"] += 1
        entered.set()
        release.wait(5)
        return dict(FAKE_STATUS)

    fake.status = slow_status  # type: ignore[method-assign]
    worker = threading.Thread(target=app.crawl_once)
    worker.start()
    assert entered.wait(5)
    second = threading.Thread(target=app.crawl_once)
    second.start()
    release.set()
    worker.join(5)
    second.join(5)

    assert status_calls["n"] == 1


def test_history_trims_oldest_beyond_window():
    hist = SnapshotHistory(window_s=10, max_samples=100)
    for i in range(15):
        hist.add(float(i), {"pool_alive": float(i)})
    assert hist.as_columns()["t"] == [
        4.0,
        5.0,
        6.0,
        7.0,
        8.0,
        9.0,
        10.0,
        11.0,
        12.0,
        13.0,
        14.0,
    ]


def test_history_respects_max_samples():
    hist = SnapshotHistory(window_s=7200, max_samples=3)
    for i in range(10):
        hist.add(float(i), {"pool_alive": float(i)})
    assert hist.as_columns()["t"] == [7.0, 8.0, 9.0]


def test_history_columns_preserve_insertion_order():
    hist = SnapshotHistory()
    hist.add(1.0, {"pool_alive": 1.0, "pool_total": 5.0})
    hist.add(2.0, {"pool_alive": 2.0, "tunnel_active": 3.0})
    cols = hist.as_columns()
    assert list(cols) == ["pool_alive", "pool_total", "tunnel_active", "t"]
    assert cols["pool_alive"] == [1.0, 2.0]
    assert cols["pool_total"] == [5.0]
    assert cols["t"] == [1.0, 2.0]


def test_history_flattens_unknown_metric_key():
    hist = SnapshotHistory()
    hist.add(1.0, {"pool_alive": 1.0, "mystery_metric": 9.0})
    cols = hist.as_columns()
    assert cols["mystery_metric"] == [9.0]
    assert cols["pool_alive"] == [1.0]
    assert cols["t"] == [1.0]


# ---------- /api/host proxy routes ----------


def test_host_routes_proxy_to_engine(panel):
    test, fake = panel
    assert test.get("/api/host").status_code == 200
    assert fake.calls[-1] == ("host",)

    res = test.post("/api/host/pick", json={"tunnel": "auto"})
    assert res.status_code == 200
    assert res.get_json()["tunnel"] == "tu_1"
    assert fake.calls[-1] == ("host_pick", "auto")

    res = test.post("/api/host/proxy", json={"enabled": True, "tunnel": "tu_1"})
    assert res.status_code == 200
    assert res.get_json()["proxy"]["enabled"] is True
    assert fake.calls[-1] == ("host_set_proxy", True, "tu_1")

    res = test.post("/api/host/tun", json={"enabled": False})
    assert res.status_code == 200
    assert res.get_json()["tun"]["enabled"] is False
    assert fake.calls[-1] == ("host_set_tun", False, None)


def test_host_routes_pass_engine_errors(panel):
    test, fake = panel

    def raise_engine(*_a, **_k):
        raise EngineError(502, "hostagent_unreachable", "down")

    fake.host_set_proxy = raise_engine  # type: ignore[method-assign]
    res = test.post("/api/host/proxy", json={"enabled": True, "tunnel": "tu_1"})
    assert res.status_code == 502
    assert res.get_json()["error"]["code"] == "hostagent_unreachable"


# ---------- /docs page ----------


@pytest.fixture()
def docs_panel(tmp_path, monkeypatch):
    (tmp_path / "architecture.md").write_text(
        "# Architecture\n\nSome **markdown** body with a `code` span.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("panel.docs.repo_docs_dir", lambda: tmp_path)
    fake = FakeEngine()
    app = create_app(
        settings=PanelSettings(), engine=fake, poll=False, docs_dir=str(tmp_path)
    )
    app.config["TESTING"] = True
    return app.test_client(), fake


def test_docs_redirects_to_architecture(docs_panel):
    test, _ = docs_panel
    for path in ("/docs", "/docs/"):
        res = test.get(path)
        assert res.status_code == 302
        assert res.headers["Location"] == "/docs/architecture"


def test_docs_page_renders_markdown(docs_panel):
    test, _ = docs_panel
    res = test.get("/docs/architecture")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert "Architecture" in body
    assert "<strong>markdown</strong>" in body
    assert "<code>code</code>" in body
    assert 'class="doc-nav"' in body


def test_docs_supports_md_and_html_suffix(docs_panel):
    test, _ = docs_panel
    assert test.get("/docs/architecture.html").status_code == 200
    assert test.get("/docs/architecture.md").status_code == 200


def test_docs_unknown_page_404(docs_panel):
    test, _ = docs_panel
    assert test.get("/docs/doesnotexist").status_code == 404
