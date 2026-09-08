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
        self.running: set[str] | None = None

    def is_running(self, tunnel_id: str) -> bool:
        if self.running is None:
            return True
        return tunnel_id in self.running

    def start(self, tunnel, config: dict) -> None:  # noqa: ANN001
        self.started.append((tunnel.tunnel_id, config))

    def update(self, tunnel_id: str, config: dict) -> None:
        self.updated.append((tunnel_id, config))

    def stop(self, tunnel_id: str) -> None:
        self.stopped.append(tunnel_id)

    def reconcile(self, expected_ids: set[str]) -> None:
        self.reconciled.append(expected_ids)


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8000,
        db_path=str(tmp_path / "test.db"),
        engine_name_prefix="inf-test",
        panel_base_url="http://127.0.0.1:8000",
        **overrides,
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


@pytest.fixture()
def stability_client(tmp_path):
    settings = _settings(
        tmp_path,
        stability_enabled=True,
        stability_min_probes=6,
        stability_min_avail=0.4,
        stability_weight_avail=0.6,
        stability_weight_speed=0.4,
    )
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
        "tier_a",
        "tier_b",
        "avg_score",
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


def test_renew_keeps_credentials_stable(client, monkeypatch):
    test, _, _ = client
    engine = app_engine(test)
    monkeypatch.setattr(engine, "health_check", lambda tunnel: (0, 0))
    before = test.post("/tunnels", json={"node_count": 2}).get_json()
    res = test.post(f"/tunnels/{before['id']}/renew")
    assert res.status_code == 200
    body = res.get_json()
    assert body["swapped"] == 0
    assert body["added"] == 0
    assert body["node_count_granted"] == before["node_count_granted"]
    assert body["next_check_s"] == 30
    after = test.get("/tunnels").get_json()["tunnels"][0]
    for key in ("host", "port", "username", "password"):
        assert after[key] == before[key]


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


def test_delete_unknown_404(client):
    test, _, _ = client
    assert test.delete("/tunnels/doesnotexist").status_code == 404


def test_dashboard_redirects_to_panel(client):
    test, _, _ = client
    res = test.get("/")
    assert res.status_code == 302
    assert res.headers["Location"] == "http://127.0.0.1:8000"


def test_status_pool_by_protocol_after_seeding(client):
    from engine.models import NodeCandidate, ProbeResult

    test, store, _ = client
    store.upsert_candidates(
        [
            NodeCandidate(
                uri="vless://a@1.2.3.4:443?type=tcp",
                protocol="vless",
                server="1.2.3.4",
                port=443,
                user="a",
                source="s1",
            ),
            NodeCandidate(
                uri="ss://x@5.6.7.8:8388",
                protocol="ss",
                server="5.6.7.8",
                port=8388,
                user="x",
                source="s2",
            ),
            NodeCandidate(
                uri="trojan://t@9.9.9.9:443",
                protocol="trojan",
                server="9.9.9.9",
                port=443,
                user="t",
                source="s3",
            ),
        ]
    )
    nodes = store.load_nodes()
    store.apply_probe_results(
        {
            n.node_id: ProbeResult(n.node_id, alive=True, latency_ms=10)
            for n in nodes
            if n.server != "9.9.9.9"
        }
    )

    res = test.get("/status")
    assert res.status_code == 200
    pool = res.get_json()["pool"]
    assert pool["untested"] == 1
    assert pool["alive"] == 2
    assert pool["dead"] == 0
    assert len(pool["by_protocol"]) == 3
    for entry in pool["by_protocol"]:
        assert {"protocol", "total", "alive", "dead", "untested"} <= set(entry)
    assert {entry["protocol"] for entry in pool["by_protocol"]} == {
        "vless",
        "ss",
        "trojan",
    }


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


def test_nodes_limit_cap_default_and_validation(client):
    from engine.models import NodeCandidate

    test, store, _ = client
    store.upsert_candidates(
        [
            NodeCandidate(
                uri=f"vless://u@10.0.{(i // 256) % 256}.{i % 256}:443?type=tcp#n{i}",
                protocol="vless",
                server=f"10.0.{(i // 256) % 256}.{i % 256}",
                port=443,
                user="u",
                source="bulk",
            )
            for i in range(1005)
        ]
    )
    assert store.pool_counts()["untested"] == 1005

    default = test.get("/nodes").get_json()
    assert default["count"] == 200
    assert default["limit"] == 200

    explicit = test.get("/nodes?limit=10").get_json()
    assert explicit["count"] == 10 and explicit["limit"] == 10

    capped = test.get("/nodes?limit=9999").get_json()
    assert capped["count"] == 1000 and capped["limit"] == 1000

    assert test.get("/nodes?limit=0").status_code == 400
    assert test.get("/nodes?limit=-1").status_code == 400
    assert test.get("/nodes?limit=abc").status_code == 400


def test_nodes_in_use_false_and_query_on_node_id(client):
    test, store, _ = client
    nodes = _seed_nodes(store)
    vless = next(n for n in nodes if n.protocol == "vless")
    store.assign_nodes([vless.node_id], "tu_x")

    unused = test.get("/nodes?in_use=0").get_json()
    assert unused["count"] == 1
    assert all(not n["in_use"] for n in unused["nodes"])

    by_id = test.get(f"/nodes?q={vless.node_id[:8]}").get_json()
    assert by_id["count"] == 1 and by_id["nodes"][0]["id"] == vless.node_id
    assert test.get("/nodes?q=no-such-node").get_json()["count"] == 0


def _seed_scored(store: Store) -> None:
    from engine.models import NodeCandidate
    from engine.stability import wilson_lower

    store.upsert_candidates(
        [
            NodeCandidate(
                uri="vless://a@1.2.3.4:443?type=tcp",
                protocol="vless",
                server="1.2.3.4",
                port=443,
                user="a",
                source="src_a",
            ),
            NodeCandidate(
                uri="vless://b@5.6.7.8:443?type=tcp",
                protocol="vless",
                server="5.6.7.8",
                port=443,
                user="b",
                source="src_b",
            ),
            NodeCandidate(
                uri="vless://c@9.9.9.9:443?type=tcp",
                protocol="vless",
                server="9.9.9.9",
                port=443,
                user="c",
                source="src_c",
            ),
        ]
    )
    nodes = {n.server: n for n in store.load_nodes()}
    # a: tier A (98/100), b: tier B (20/100), c: cold (2/2 but enormous download).
    for server, ok, total, tp in (
        ("1.2.3.4", 98, 100, 400),
        ("5.6.7.8", 20, 100, 9000),
        ("9.9.9.9", 2, 2, 6500),
    ):
        store._conn.execute(
            "UPDATE nodes SET state = 'alive', probe_ok = ?, probe_total = ?, "
            "throughput_kb_s = ?, score_f = ? WHERE node_id = ?",
            (ok, total, tp, wilson_lower(ok, total), nodes[server].node_id),
        )
        store._conn.commit()


def test_nodes_stability_fields_present_when_disabled(client):
    test, store, _ = client
    _seed_nodes(store)
    node = test.get("/nodes").get_json()["nodes"][0]
    assert {"probe_total", "availability", "tier", "score"} <= set(node)
    assert node["tier"] is None
    assert node["score"] is None


def test_nodes_sort_score_requires_feature(client):
    test, _, _ = client
    assert test.get("/nodes?sort=score").status_code == 400
    assert test.get("/nodes?min_tier=A").status_code == 400


def test_nodes_sort_score_orders_and_min_tier(stability_client):
    test, store, _ = stability_client
    _seed_scored(store)
    by_server = {n.server: n for n in store.load_nodes()}

    res = test.get("/nodes?state=alive&sort=score")
    assert res.status_code == 200
    ids = [n["id"] for n in res.get_json()["nodes"]]
    assert ids[0] == by_server["1.2.3.4"].node_id  # tier A first

    only_a = test.get("/nodes?state=alive&min_tier=A").get_json()
    assert len(only_a["nodes"]) == 1
    assert only_a["nodes"][0]["tier"] == "A"

    a_and_b = test.get("/nodes?state=alive&min_tier=B").get_json()
    assert {n["tier"] for n in a_and_b["nodes"]} == {"A", "B"}


def test_nodes_status_pool_tiers_when_enabled(stability_client):
    test, store, _ = stability_client
    _seed_scored(store)

    pool = test.get("/status").get_json()["pool"]
    assert pool["tier_a"] == 1
    assert pool["tier_b"] == 1
    assert pool["avg_score"] is not None


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


def test_self_heal_redeploys_down_container(client):
    from engine.models import NodeCandidate, ProbeResult, Tunnel

    test, store, controller = client
    controller.running = set()  # nothing is running: every tunnel needs a redeploy
    store.upsert_candidates(
        [
            NodeCandidate(
                uri="vless://00000000-0000-0000-0000-000000000001@1.2.3.4:443?type=tcp",
                protocol="vless",
                server="1.2.3.4",
                port=443,
                user="00000000-0000-0000-0000-000000000001",
                source="test",
            )
        ]
    )
    node = store.load_nodes(state="untested")[0]
    store.apply_probe_results(
        {
            node.node_id: ProbeResult(
                node.node_id, alive=True, latency_ms=50, throughput_kb_s=800
            )
        }
    )
    store.assign_nodes([node.node_id], "tu_heal")
    store.save_tunnel(
        Tunnel(
            tunnel_id="tu_heal",
            state="running",
            port=20010,
            username="u_x",
            password="p_y",
            node_count_requested=1,
            node_count_granted=1,
            auto_renew=True,
            created_at_s=1000.0,
            updated_at_s=1000.0,
        )
    )

    engine = app_engine(test)
    engine._self_heal("tu_heal")

    assert [tid for tid, _ in controller.updated] == ["tu_heal"]
    assert engine.tunnel_health(store.get_tunnel("tu_heal"))["restarts_24h"] == 1


def test_self_heal_noop_when_container_running(client):
    from engine.models import Tunnel

    test, store, controller = client
    controller.running = {"tu_alive"}
    store.save_tunnel(
        Tunnel(
            tunnel_id="tu_alive",
            state="running",
            port=20011,
            username="u_x",
            password="p_y",
            node_count_requested=1,
            node_count_granted=0,
            auto_renew=True,
            created_at_s=1000.0,
            updated_at_s=1000.0,
        )
    )

    engine = app_engine(test)
    engine._self_heal("tu_alive")

    assert controller.updated == []
    assert engine.tunnel_health(store.get_tunnel("tu_alive"))["restarts_24h"] == 0
