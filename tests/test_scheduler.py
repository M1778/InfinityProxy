"""Offline tests for engine.scheduler boot reconcile. No docker, no network."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.assigner import Assigner
from engine.config import Settings
from engine.models import Node, Tunnel
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


def test_engine_status_resolves_assignments_with_one_store_query():
    store = StatusStore(
        [_alive_node("n1"), _alive_node("n2"), _alive_node("n3")], {"n1"}
    )
    engine = make_engine(store)

    status = engine.engine_status()

    assert status["pool"]["assignable"] == 2
    assert store.in_use_calls == 1
