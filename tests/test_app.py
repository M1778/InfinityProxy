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
    assert set(body["pool"]) == {"total", "alive", "dead", "in_use", "assignable"}
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


def test_dashboard_renders(client):
    test, _, _ = client
    res = test.get("/")
    assert res.status_code == 200
    assert "InfinityProxy" in res.get_data(as_text=True)
    assert "Control API" in res.get_data(as_text=True)
