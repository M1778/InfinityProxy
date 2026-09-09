"""Offline tests for engine.scheduler boot reconcile. No docker, no network."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.assigner import Assigner
from engine.config import Settings
from engine.models import Tunnel
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
