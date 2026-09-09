"""Offline tests for engine.assigner against a fake store. No docker, no network."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from engine.assigner import Assigner, PortExhausted
from engine.config import Settings
from engine.models import Node, Tunnel


def make_node(
    node_id: str,
    state: str = "alive",
    latency_ms: int | None = None,
    throughput_kb_s: int | None = None,
    probe_ok: int = 0,
    probe_total: int = 0,
) -> Node:
    return Node(
        node_id=node_id,
        uri=f"vless://x@{node_id}:443",
        protocol="vless",
        server=node_id,
        port=443,
        user="x",
        source="test",
        first_seen_s=1.0,
        last_latency_ms=latency_ms,
        state=state,
        throughput_kb_s=throughput_kb_s,
        probe_ok=probe_ok,
        probe_total=probe_total,
    )


def make_tunnel(
    tunnel_id: str, port: int = 0, granted: int = 0, requested: int = 0
) -> Tunnel:
    return Tunnel(
        tunnel_id=tunnel_id,
        port=port,
        node_count_requested=requested,
        node_count_granted=granted,
        auto_renew=True,
    )


class FakeStore:
    """Duck-typed Store backed by dicts over the tunnels/nodes/assignments tables."""

    def __init__(self) -> None:
        self._tunnels: dict[str, Tunnel] = {}
        self._nodes: dict[str, Node] = {}
        self._assignment: dict[str, str] = {}

    def add_node(self, node: Node) -> None:
        self._nodes[node.node_id] = node

    def assigned_nodes(self, tunnel_id: str) -> list[Node]:
        return [
            self._nodes[nid]
            for nid, owner in self._assignment.items()
            if owner == tunnel_id
        ]

    def get_tunnel(self, tunnel_id: str) -> Tunnel | None:
        return self._tunnels.get(tunnel_id)

    def save_tunnel(self, tunnel: Tunnel) -> None:
        self._tunnels[tunnel.tunnel_id] = tunnel

    def load_nodes(
        self, state: str | None = None, protocols: set[str] | None = None
    ) -> list[Node]:
        return [
            Node(**vars(n))  # copies so the assigner can't mutate store rows
            for n in self._nodes.values()
            if (state is None or n.state == state)
            and (protocols is None or n.protocol in protocols)
        ]

    def node_ids_in_use(self) -> list[str]:
        return list(self._assignment)

    def assign_nodes(self, node_ids: list[str], tunnel_id: str) -> None:
        for nid in node_ids:
            if nid in self._assignment:
                raise AssertionError(
                    f"node {nid} already assigned, exclusivity violated"
                )
            self._assignment[nid] = tunnel_id

    def release_tunnel(self, tunnel_id: str) -> int:
        freed = 0
        for nid in list(self._assignment):
            if self._assignment[nid] == tunnel_id:
                del self._assignment[nid]
                freed += 1
        return freed

    def port_in_use(self, port: int) -> bool:
        return any(t.port == port for t in self._tunnels.values())


def tiny_settings() -> Settings:
    return Settings(tunnel_port_start=20000, tunnel_port_end=20003)


def stability_settings() -> Settings:
    return Settings(
        tunnel_port_start=20000,
        tunnel_port_end=20003,
        stability_enabled=True,
        stability_min_probes=6,
        stability_min_avail=0.4,
        stability_weight_avail=0.6,
        stability_weight_speed=0.4,
    )


def test_allocate_port_returns_lowest_free():
    assigner = Assigner(tiny_settings())
    store = FakeStore()

    assert assigner.allocate_port(store) == 20000

    store.save_tunnel(make_tunnel("t1", port=20000))
    assert assigner.allocate_port(store) == 20001

    store.save_tunnel(make_tunnel("t2", port=20001))
    store.save_tunnel(make_tunnel("t3", port=20002))
    assert assigner.allocate_port(store) == 20003


def test_allocate_port_exhausts_full_range():
    assigner = Assigner(tiny_settings())
    store = FakeStore()
    for i, port in enumerate(range(20000, 20004)):
        store.save_tunnel(make_tunnel(f"t{i}", port=port))

    with pytest.raises(PortExhausted):
        assigner.allocate_port(store)


def test_generate_credentials_are_stable_and_distinct():
    assigner = Assigner(tiny_settings())

    pairs = [assigner.generate_credentials() for _ in range(50)]
    assert len(set(pairs)) == 50  # every pair distinct
    for username, password in pairs:
        assert username.startswith("u_")
        assert password.startswith("p_")
        assert len(username) > 2 and len(password) > 2
        assert username != password


def test_assign_new_respects_requested_cap_and_gives_distinct_nodes():
    assigner = Assigner(tiny_settings())
    store = FakeStore()
    for i in range(5):
        store.add_node(make_node(f"n{i}"))

    granted = assigner.assign_new(store, "t1", requested=2)

    assert len(granted) == 2
    assert len({n.node_id for n in granted}) == 2
    assert store.node_ids_in_use() == [n.node_id for n in granted]


def test_assign_new_degrades_when_pool_is_small():
    assigner = Assigner(tiny_settings())
    store = FakeStore()
    store.add_node(make_node("n1"))
    store.add_node(make_node("n2"))
    store.add_node(make_node("nd", state="dead"))

    granted = assigner.assign_new(store, "t1", requested=5)

    assert len(granted) == 2  # dead node never granted
    assert {n.node_id for n in granted} == {"n1", "n2"}


def test_assign_new_nodes_are_exclusive_across_tunnels():
    assigner = Assigner(tiny_settings())
    store = FakeStore()
    for i in range(3):
        store.add_node(make_node(f"n{i}"))

    first = assigner.assign_new(store, "t1", requested=3)
    second = assigner.assign_new(store, "t2", requested=3)

    assert len(first) == 3
    assert second == []  # every node already taken
    assert not ({n.node_id for n in first} & {n.node_id for n in second})


def test_assign_new_prefers_tested_nodes_over_untested():
    assigner = Assigner(tiny_settings())
    store = FakeStore()
    store.add_node(make_node("untested1", latency_ms=None))
    store.add_node(make_node("untested2", latency_ms=None))
    store.add_node(make_node("tested1", latency_ms=120))
    store.add_node(make_node("tested2", latency_ms=300))

    granted = assigner.assign_new(store, "t1", requested=2)

    assert [n.node_id for n in granted] == ["tested1", "tested2"]


def test_assign_new_prefers_fastest_throughput_first():
    assigner = Assigner(tiny_settings())
    store = FakeStore()
    store.add_node(make_node("fast", latency_ms=300, throughput_kb_s=1500))
    store.add_node(make_node("slow", latency_ms=10, throughput_kb_s=220))
    store.add_node(make_node("unknown", latency_ms=120, throughput_kb_s=None))
    store.add_node(make_node("untested", latency_ms=None, throughput_kb_s=None))

    granted = assigner.assign_new(store, "t1", requested=3)

    assert [n.node_id for n in granted] == ["fast", "slow", "unknown"]


def test_assign_new_stability_prefers_proven_slow_over_flappy_fast():
    assigner = Assigner(stability_settings())
    store = FakeStore()
    store.add_node(
        make_node("proven", throughput_kb_s=300, probe_ok=90, probe_total=100)
    )
    store.add_node(
        make_node("flappy", throughput_kb_s=5000, probe_ok=1, probe_total=10)
    )

    granted = assigner.assign_new(store, "t1", requested=2)

    assert [n.node_id for n in granted] == ["proven", "flappy"]


def test_assign_new_stability_feature_off_keeps_legacy_order():
    # Same nodes, feature flag off (Settings() default): the single throughput
    # measurement still dominates, exactly as before ADR-0009.
    assigner = Assigner(tiny_settings())
    store = FakeStore()
    store.add_node(
        make_node("proven", throughput_kb_s=300, probe_ok=90, probe_total=100)
    )
    store.add_node(
        make_node("flappy", throughput_kb_s=5000, probe_ok=1, probe_total=10)
    )

    granted = assigner.assign_new(store, "t1", requested=2)

    assert [n.node_id for n in granted] == ["flappy", "proven"]


def test_assign_new_stability_cold_node_never_outranks_evaluated():
    assigner = Assigner(stability_settings())
    store = FakeStore()
    # Two flawless fresh probes (cold, probe_total 2 < min_probes) but a huge
    # download: must still never outrank a node with a probe history.
    store.add_node(make_node("cold", throughput_kb_s=9000, probe_ok=2, probe_total=2))
    store.add_node(
        make_node("evaluated", throughput_kb_s=1, probe_ok=20, probe_total=100)
    )

    granted = assigner.assign_new(store, "t1", requested=2)

    assert [n.node_id for n in granted] == ["evaluated", "cold"]


def test_top_up_adds_only_new_nodes_and_reaches_target():
    assigner = Assigner(tiny_settings())
    store = FakeStore()
    for i in range(5):
        store.add_node(make_node(f"n{i}"))

    first = assigner.assign_new(store, "t1", requested=2)
    tunnel = make_tunnel("t1", port=20000, granted=len(first), requested=5)
    store.save_tunnel(tunnel)

    added = assigner.top_up(store, "t1", target=5)

    assert len(added) == 3
    added_ids = {n.node_id for n in added}
    first_ids = {n.node_id for n in first}
    assert not added_ids & first_ids
    assert {n.node_id for n in store.assigned_nodes("t1")} == {
        n.node_id for n in first + added
    }
    assert len(store.assigned_nodes("t1")) == 5


def test_top_up_returns_empty_at_or_above_target():
    assigner = Assigner(tiny_settings())
    store = FakeStore()
    store.add_node(make_node("n1"))
    assigner.assign_new(store, "t1", requested=1)
    store.save_tunnel(make_tunnel("t1", port=20000, granted=1, requested=1))

    assert assigner.top_up(store, "t1", target=1) == []
    assert assigner.top_up(store, "t1", target=0) == []


def test_top_up_degrades_when_pool_is_starved():
    assigner = Assigner(tiny_settings())
    store = FakeStore()
    store.add_node(make_node("n1"))
    assigner.assign_new(store, "t1", requested=1)
    store.save_tunnel(make_tunnel("t1", port=20000, granted=1, requested=4))

    assert assigner.top_up(store, "t1", target=4) == []  # no free alive nodes left


def test_release_tunnel_frees_nodes_for_future_assignment():
    assigner = Assigner(tiny_settings())
    store = FakeStore()
    for i in range(3):
        store.add_node(make_node(f"n{i}"))
    assigner.assign_new(store, "t1", requested=3)
    store.save_tunnel(make_tunnel("t1", port=20000, granted=3, requested=3))

    freed = assigner.release_tunnel(store, "t1")

    assert freed == 3
    assert store.node_ids_in_use() == []
    re_granted = assigner.assign_new(store, "t2", requested=3)
    assert len(re_granted) == 3


def test_release_tunnel_missing_row_still_frees_nodes():
    assigner = Assigner(tiny_settings())
    store = FakeStore()
    store.add_node(make_node("n1"))
    store.add_node(make_node("n2"))
    store.assign_nodes(["n1", "n2"], "ghost")  # no tunnel row was ever saved

    freed = assigner.release_tunnel(store, "ghost")

    assert freed == 2
    assert store.node_ids_in_use() == []


def test_create_tunnel_config_saves_tunnel_with_granted_count():
    assigner = Assigner(tiny_settings())
    store = FakeStore()
    for i in range(2):
        store.add_node(make_node(f"n{i}"))

    tunnel, nodes = assigner.create_tunnel_config(store, requested=5, auto_renew=False)

    assert len(nodes) == 2  # degraded: pool starved
    assert tunnel.node_count_requested == 5
    assert tunnel.node_count_granted == 2
    assert tunnel.port == 20000
    assert tunnel.username.startswith("u_")
    assert tunnel.password.startswith("p_")
    assert store.get_tunnel(tunnel.tunnel_id) is tunnel
    assert [n.node_id for n in store.assigned_nodes(tunnel.tunnel_id)] == [
        n.node_id for n in nodes
    ]
