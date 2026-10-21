"""Offline tests for engine.scheduler boot reconcile. No docker, no network."""

import sys
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.assigner import Assigner
from engine.config import Settings
from engine.models import Node, ProbeResult, Tunnel
from engine.scheduler import Engine


class FakeController:
    def reconcile(self, expected) -> None:  # noqa: ANN001
        self.expected = expected


class FakeStore:
    def __init__(self) -> None:
        self._tunnels: dict[str, Tunnel] = {}
        self._assignment: dict[str, str] = {}
        self.released: list[str] = []

    def save_tunnel(self, tunnel: Tunnel) -> None:
        self._tunnels[tunnel.tunnel_id] = tunnel

    def load_tunnels(self) -> list[Tunnel]:
        return list(self._tunnels.values())

    def set_tunnel_state(self, tunnel_id: str, state: str) -> None:
        if tunnel_id in self._tunnels:
            self._tunnels[tunnel_id].state = state

    def assign(self, node_id: str, tunnel_id: str) -> None:
        self._assignment[node_id] = tunnel_id

    def assigned_tunnel_ids(self) -> set[str]:
        return {owner for owner in self._assignment.values()}

    def release_tunnel(self, tunnel_id: str) -> int:
        freed = sum(1 for o in self._assignment.values() if o == tunnel_id)
        if freed:
            self.released.append(tunnel_id)
        self._assignment = {
            nid: owner for nid, owner in self._assignment.items() if owner != tunnel_id
        }
        return freed


def make_tunnel(tunnel_id: str, state: str = "starting") -> Tunnel:
    return Tunnel(
        tunnel_id=tunnel_id,
        state=state,
        node_count_requested=5,
        auto_renew=True,
    )


def make_engine(store: FakeStore) -> Engine:
    controller = FakeController()
    engine = Engine(Settings(), store, Assigner(Settings()), controller)
    return engine


def test_reconcile_frees_orphan_assignments_and_keeps_live_ones():
    store = FakeStore()
    store.save_tunnel(make_tunnel("tu_live"))
    store.assign("n1", "tu_live")
    store.assign("n2", "tu_ghost")
    store.assign("n3", "tu_ghost")
    controller = FakeController()
    engine = Engine(Settings(), store, Assigner(Settings()), controller)

    engine.reconcile()

    assert controller.expected == {"tu_live"}
    assert store.released == ["tu_ghost"]
    assert store.assigned_tunnel_ids() == {"tu_live"}
    assert store.load_tunnels()[0].state == "running"


def _alive_node(node_id: str) -> Node:
    return Node(
        node_id=node_id,
        uri=f"vless://u@{node_id}.example.com:443?encryption=none#n",
        protocol="vless",
        server=f"{node_id}.example.com",
        port=443,
        user="u",
        source="bench",
        first_seen_s=0.0,
        state="alive",
    )


class StatusStore(FakeStore):
    def __init__(self, nodes: list[Node], in_use: set[str]) -> None:
        super().__init__()
        self._nodes = nodes
        self._in_use = in_use
        self.in_use_calls = 0
        self.assignable_calls = 0

    def sources_summary(self) -> list[dict]:  # type: ignore[no-untyped-def]
        return []

    def pool_counts(self, **kwargs) -> dict:  # type: ignore[no-untyped-def]
        return {
            "total": len(self._nodes),
            "alive": len(self._nodes),
            "dead": 0,
            "untested": 0,
            "in_use": len(self._in_use),
            "tier_a": 0,
            "tier_b": 0,
            "working_set": 0,
            "avg_score": None,
        }

    def pool_by_protocol(self) -> list:
        return []

    def load_nodes(self, state=None, protocols=None) -> list[Node]:  # type: ignore[no-untyped-def]  # noqa: ANN001
        return list(self._nodes)

    def node_ids_in_use(self) -> set[str]:
        self.in_use_calls += 1
        return set(self._in_use)

    def assignable_count(self, protocols: set[str]) -> int:
        self.assignable_calls += 1
        return sum(1 for n in self._nodes if n.node_id not in self._in_use)


def test_engine_status_resolves_assignments_with_one_store_query():
    store = StatusStore(
        [_alive_node("n1"), _alive_node("n2"), _alive_node("n3")], {"n1"}
    )
    engine = make_engine(store)

    status = engine.engine_status()

    assert status["pool"]["assignable"] == 2
    assert store.assignable_calls == 1
    assert store.in_use_calls == 0


def test_tunnel_health_counts_roll_out_of_24h_window():
    store = FakeStore()
    engine = make_engine(store)

    now = time.time()
    stale_window = engine._swapped_24h.setdefault("tu_1", deque())
    stale_window.append(now - 86401)
    fresh_window = deque([now - 3600, now])
    engine._restarted_24h["tu_1"] = fresh_window

    health = engine.tunnel_health(make_tunnel("tu_1", state="running"))

    assert health["dead_swapped_24h"] == 0
    assert health["restarts_24h"] == 2
    # The pruned deque stays pruned, so the stale entry never comes back.
    assert engine._swapped_24h["tu_1"] == deque()


def test_record_event_prunes_on_write():
    store = FakeStore()
    engine = make_engine(store)

    old = time.time() - 90000
    engine._swapped_24h["tu_2"] = deque([old])
    engine._record_event(engine._swapped_24h, "tu_2")
    engine._record_event(engine._swapped_24h, "tu_2")

    assert len(engine._swapped_24h["tu_2"]) == 2
    assert (
        engine.tunnel_health(make_tunnel("tu_2", state="running"))["dead_swapped_24h"]
        == 2
    )


class HealStore(FakeStore):
    def __init__(self, tunnel: Tunnel, node: Node) -> None:
        super().__init__()
        self.save_tunnel(tunnel)
        self._nodes = {node.node_id: node}
        self._assignment = {node.node_id: tunnel.tunnel_id}

    def get_tunnel(self, tunnel_id: str) -> Tunnel | None:
        return self._tunnels.get(tunnel_id)

    def node_ids_assigned_to(self, tunnel_id: str) -> list[str]:
        return [nid for nid, owner in self._assignment.items() if owner == tunnel_id]

    def get_node(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)


class HealController:
    def __init__(self, running: bool) -> None:
        self.running = running
        self.is_running_calls = 0
        self.updated: list[str] = []

    def is_running(self, tunnel_id: str) -> bool:  # noqa: ARG002
        self.is_running_calls += 1
        return self.running

    def update(self, tunnel_id: str, config: dict) -> None:  # noqa: ARG002
        self.updated.append(tunnel_id)


def test_health_check_skips_tunnel_under_creation():
    tunnel = make_tunnel("tu_starting", state="starting")
    store = HealStore(tunnel, _alive_node("n1"))
    controller = HealController(running=False)
    engine = Engine(Settings(), store, Assigner(Settings()), controller)

    assert engine.health_check(tunnel) == (0, 0)
    assert controller.is_running_calls == 0
    assert controller.updated == []


def test_self_heal_redeploys_running_tunnel_without_container():
    tunnel = make_tunnel("tu_live", state="running")
    store = HealStore(tunnel, _alive_node("n1"))
    controller = HealController(running=False)
    engine = Engine(Settings(), store, Assigner(Settings()), controller)

    engine._self_heal("tu_live")

    assert controller.updated == ["tu_live"]


class TopUpStore(HealStore):
    """HealStore plus the queries a top-up needs to grant fresh nodes."""

    def __init__(self, tunnel: Tunnel, nodes: list[Node]) -> None:
        super().__init__(tunnel, nodes[0])
        self._nodes = {n.node_id: n for n in nodes}

    def assignable_count(self, protocols: set[str]) -> int:
        return sum(
            1
            for n in self._nodes.values()
            if n.protocol in protocols and n.node_id not in self._assignment
        )

    def load_nodes(self, state=None, protocols=None) -> list[Node]:  # type: ignore[no-untyped-def]  # noqa: ANN001
        return [
            n
            for n in self._nodes.values()
            if (state is None or n.state == state)
            and (protocols is None or n.protocol in protocols)
        ]

    def node_ids_in_use(self) -> list[str]:
        return list(self._assignment)

    def assign_nodes(self, node_ids: list[str], tunnel_id: str) -> None:
        for node_id in node_ids:
            self._assignment[node_id] = tunnel_id

    def apply_probe_results(self, results, **kw) -> None:  # noqa: ANN001
        for node_id, result in results.items():
            self._nodes[node_id].state = "alive" if result.alive else "dead"

    def unassign_node(self, node_id: str) -> None:
        self._assignment.pop(node_id, None)

    def node_ids_assigned_to(self, tunnel_id: str) -> list[str]:
        return [nid for nid, owner in self._assignment.items() if owner == tunnel_id]


def test_health_check_tops_up_degraded_tunnel(monkeypatch) -> None:
    tunnel = make_tunnel("tu_degraded", state="running")
    tunnel.node_count_granted = 1
    tunnel.node_count_requested = 2
    store = TopUpStore(
        tunnel, [_alive_node("n1"), _alive_node("n2"), _alive_node("n3")]
    )
    controller = HealController(running=True)
    settings = replace(Settings(), stability_enabled=True)
    engine = Engine(settings, store, Assigner(settings), controller)
    monkeypatch.setattr(
        "engine.scheduler.batch_probe",
        lambda nodes, **kw: {n.node_id: _probe_alive(n.node_id) for n in nodes},
    )

    swapped, added = engine.health_check(tunnel)

    assert swapped == 0
    assert added == 1
    assert set(store.node_ids_assigned_to("tu_degraded")) == {"n1", "n2"}
    assert tunnel.node_count_granted == 2
    assert controller.updated == ["tu_degraded"]
    assert engine._rendered["tu_degraded"][0] == ("n1", "n2")


def _probe_alive(node_id: str) -> ProbeResult:
    return ProbeResult(node_id=node_id, alive=True)


def test_health_pass_healthy_tunnel_is_noop(monkeypatch):
    tunnel = make_tunnel("tu_healthy", state="running")
    tunnel.node_count_requested = tunnel.node_count_granted = 1
    store = TopUpStore(tunnel, [_alive_node("n1")])
    controller = HealController(running=True)
    engine = Engine(Settings(), store, Assigner(Settings()), controller)
    monkeypatch.setattr(
        "engine.scheduler.batch_probe",
        lambda nodes, **kw: {
            n.node_id: ProbeResult(n.node_id, alive=True) for n in nodes
        },
    )
    assert engine.health_check(tunnel) == (0, 0)
    assert controller.updated == []
    assert engine._health_busy == set()


def test_health_pass_redeploys_after_dead_removal_without_replacement(monkeypatch):
    tunnel = make_tunnel("tu_partial", state="running")
    tunnel.node_count_requested = tunnel.node_count_granted = 2
    store = TopUpStore(tunnel, [_alive_node("n1"), _alive_node("n2")])
    store.assign_nodes(["n2"], tunnel.tunnel_id)
    controller = HealController(running=True)
    engine = Engine(Settings(), store, Assigner(Settings()), controller)
    monkeypatch.setattr(
        "engine.scheduler.batch_probe",
        lambda nodes, **kw: {
            n.node_id: ProbeResult(n.node_id, alive=n.node_id == "n1") for n in nodes
        },
    )
    assert engine.health_check(tunnel) == (0, 0)
    assert engine.health_check(tunnel) == (1, 0)
    assert controller.updated == ["tu_partial"]
    assert engine._rendered[tunnel.tunnel_id][0] == ("n1",)
    assert tunnel.node_count_granted == 1


def test_health_pass_replaces_dead_node_and_preserves_credentials(monkeypatch):
    tunnel = make_tunnel("tu_replace", state="running")
    tunnel.node_count_requested = tunnel.node_count_granted = 1
    tunnel.port, tunnel.username, tunnel.password = 12345, "u", "p"
    store = TopUpStore(tunnel, [_alive_node("n1"), _alive_node("n2")])
    controller = HealController(running=True)
    engine = Engine(Settings(), store, Assigner(Settings()), controller)
    monkeypatch.setattr(
        "engine.scheduler.batch_probe",
        lambda nodes, **kw: {
            n.node_id: ProbeResult(n.node_id, alive=False) for n in nodes
        },
    )
    assert engine.health_check(tunnel) == (0, 0)
    assert engine.health_check(tunnel) == (1, 1)
    assert store.node_ids_assigned_to(tunnel.tunnel_id) == ["n2"]
    assert controller.updated == ["tu_replace"]
    assert (tunnel.port, tunnel.username, tunnel.password) == (12345, "u", "p")


def test_health_pass_does_not_revive_deleted_tunnel(monkeypatch):
    tunnel = make_tunnel("tu_deleted", state="running")
    tunnel.node_count_requested = tunnel.node_count_granted = 1
    store = TopUpStore(tunnel, [_alive_node("n1")])
    controller = HealController(running=True)
    engine = Engine(Settings(), store, Assigner(Settings()), controller)

    def probe(nodes, **kwargs):
        store._tunnels.pop(tunnel.tunnel_id)
        return {n.node_id: ProbeResult(n.node_id, alive=False) for n in nodes}

    monkeypatch.setattr("engine.scheduler.batch_probe", probe)
    assert engine.health_check(tunnel) == (0, 0)
    assert controller.updated == []
    assert engine._health_busy == set()
