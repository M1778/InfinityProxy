"""Control API tests against a real Store with a fake container driver. No docker/network."""  # noqa: E501

from __future__ import annotations

from typing import Any

import pytest

from engine.app import create_app
from engine.config import Settings
from engine.db import Store


class FakeContainerController:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.started: list[tuple[str, dict[str, Any]]] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.stopped: list[str] = []
        self.reconciled: list[set[str]] = []

    def start(self, tunnel, config: dict) -> None:  # noqa: ANN001
        self.started.append((tunnel.tunnel_id, config))

    def update(self, tunnel_id: str, config: dict) -> None:
        self.updated.append((tunnel_id, config))

    def stop(self, tunnel_id: str) -> None:
        self.stopped.append(tunnel_id)

    def reconcile(self, expected_ids: set[str]) -> None:
        self.reconciled.append(expected_ids)


def _settings(tmp_path) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8000,
        db_path=str(tmp_path / "test.db"),
        engine_name_prefix="inf-test",
        panel_base_url="http://127.0.0.1:8000",
    )


@pytest.fixture()
def client(tmp_path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.create_schema()
    controller = FakeContainerController(settings)
    app = create_app(
        settings=settings,
        store=store,
        controller=controller,
        background=False,
    )
    app.config["TESTING"] = True
    app._test_store = store  # type: ignore[attr-defined]
    app._test_controller = controller  # type: ignore[attr-defined]
    return app.test_client(), store, controller


def test_status_ok(client):
    test, store, _ = client
    res = test.get("/status")
    assert res.status_code == 200
    body = res.get_json()
    assert body["engine"] == "ok"
    assert set(body["pool"]) == {
        "total",
        "alive",
        "dead",
        "untested",
        "in_use",
        "assignable",
        "by_protocol",
    }
    assert body["pool"]["by_protocol"] == []
    assert body["tunnels"]["active"] == 0


def test_create_tunnel_degraded_on_empty_pool(client):
    test, store, controller = client
    res = test.post("/tunnels", json={"node_count": 10, "auto_renew": True})
    assert res.status_code == 201
    body = res.get_json()
    assert body["port"] >= 10000
    assert body["node_count_requested"] == 10
    assert body["node_count_granted"] == 0
    assert body["degraded"] is True
    assert body["protocols"] == ["http", "socks5"]
    assert body["username"].startswith("u_") and body["password"].startswith("p_")
    assert len(controller.started) == 1
    tunnel_id, config = controller.started[0]
    assert tunnel_id == body["id"]
    assert config["route"]["final"] == "rotator"


def test_create_tunnel_validation(client):
    test, _, _ = client
    assert test.post("/tunnels", json={"node_count": 0}).status_code == 400
    assert test.post("/tunnels", json={"node_count": "x"}).status_code == 400


def test_create_tunnel_unrenderable_nodes_cleans_up(client):
    from engine.models import NodeCandidate, ProbeResult

    test, store, controller = client
    store.upsert_candidates(
        [
            NodeCandidate(
                uri="ss://07042893-9818-4e42-a3e9-2f9f1dc4f6a1@1.2.3.4:9000#z",
                protocol="ss",
                server="1.2.3.4",
                port=9000,
                user=None,
                source="test",
            )
        ]
    )
    node = store.load_nodes(state="untested")[0]
    store.apply_probe_results({node.node_id: ProbeResult(node.node_id, alive=True)})
    assert store.pool_counts()["alive"] == 1

    res = test.post("/tunnels", json={"node_count": 2})
    assert res.status_code == 502
    assert res.get_json()["error"]["code"] == "invalid_nodes"
    assert store.pool_counts()["alive"] == 1
    assert store.load_tunnels() == []
    assert controller.started == []


def test_list_tunnels_round_trip(client):
    test, store, _ = client
    created = test.post("/tunnels", json={"node_count": 3}).get_json()
    res = test.get("/tunnels")
    assert res.status_code == 200
    tunnels = res.get_json()["tunnels"]
    assert len(tunnels) == 1
    assert tunnels[0]["id"] == created["id"]
    assert tunnels[0]["health"]["dead_swapped_24h"] == 0
    assert tunnels[0]["nodes"] == []


def test_renew_unknown_404(client):
    test, _, _ = client
    assert test.post("/tunnels/nope/renew").status_code == 404


def test_delete_tunnel(client):
    test, store, controller = client
    created = test.post("/tunnels", json={"node_count": 1}).get_json()
    res = test.delete(f"/tunnels/{created['id']}")
    assert res.status_code == 204
    assert controller.stopped == [created["id"]]
    assert store.get_tunnel(created["id"]) is None


def test_dashboard_redirects_to_panel(client):
    test, _, _ = client
    res = test.get("/")
    assert res.status_code == 302
    assert res.headers["Location"] == "http://127.0.0.1:8000"


def _seed_nodes(store: Store) -> None:
    from engine.models import NodeCandidate, ProbeResult

    store.upsert_candidates(
        [
            NodeCandidate(
                uri="vless://a@1.2.3.4:443?type=tcp",
                protocol="vless",
                server="1.2.3.4",
                port=443,
                user=None,
                source="src_a",
            ),
            NodeCandidate(
                uri="ss://method:pass@5.6.7.8:8388",
                protocol="ss",
                server="5.6.7.8",
                port=8388,
                user=None,
                source="src_b",
            ),
        ]
    )
    nodes = store.load_nodes(state="untested")
    store.apply_probe_results(
        {n.node_id: ProbeResult(n.node_id, alive=n.server == "1.2.3.4") for n in nodes}
    )
    return nodes


def test_nodes_list_filters(client):
    test, store, _ = client
    nodes = _seed_nodes(store)
    vless = next(n for n in nodes if n.protocol == "vless")
    ss = next(n for n in nodes if n.protocol == "ss")

    res = test.get("/nodes")
    assert res.status_code == 200
    body = res.get_json()
    assert body["count"] == 2
    assert {"protocol", "state", "last_latency_ms", "in_use", "source"} <= set(
        body["nodes"][0]
    )

    assert test.get("/nodes?state=alive").get_json()["count"] == 1
    assert (
        test.get(f"/nodes?state=alive&protocol={vless.protocol}").get_json()["count"]
        == 1
    )
    assert test.get("/nodes?source=src_b").get_json()["count"] == 1
    assert test.get("/nodes?limit=1").get_json()["count"] == 1
    found_by_q = test.get(f"/nodes?q={ss.server}").get_json()
    assert found_by_q["count"] == 1 and found_by_q["nodes"][0]["protocol"] == "ss"
    assert test.get("/nodes?in_use=1").get_json()["count"] == 0

    store.assign_nodes([vless.node_id], "tu_x")
    in_use = test.get("/nodes?in_use=1").get_json()
    assert in_use["count"] == 1 and in_use["nodes"][0]["id"] == vless.node_id


def test_nodes_detail_and_not_found(client):
    test, store, _ = client
    node = _seed_nodes(store)[0]
    res = test.get(f"/nodes/{node.node_id}")
    assert res.status_code == 200
    assert res.get_json()["uri"].startswith("vless://")
    assert test.get("/nodes/doesnotexist").status_code == 404


def test_source_refresh(client, monkeypatch):
    test, _, _ = client
    engine = app_engine(test)
    monkeypatch.setattr(engine, "request_source_refresh", lambda n: n == "ok_src")
    assert test.post("/sources/unknown/refresh").status_code == 404
    res = test.post("/sources/ok_src/refresh")
    assert res.status_code == 202
    assert res.get_json()["name"] == "ok_src"
    assert res.get_json()["refreshing"] is True

    def boom(_n):
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "request_source_refresh", boom)
    res = test.post("/sources/ok_src/refresh")
    assert res.status_code == 502
    assert res.get_json()["error"]["code"] == "source_refresh_failed"


def app_engine(test):
    return test.application.engine
