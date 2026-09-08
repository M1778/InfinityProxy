"""Panel backend tests: fake engine client + injected dial; no poll threads, no network."""  # noqa: E501

from __future__ import annotations

import json
from typing import Any

import pytest

from panel.app import create_app
from panel.client import EngineError
from panel.config import PanelSettings

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
