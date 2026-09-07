"""Offline tests for the pool's node-admission gate and source refresh plumbing."""

from __future__ import annotations

import base64
import tempfile
import time
from pathlib import Path

from engine.db import Store
from engine.models import Node, ProbeResult
from engine.pool import _admit_batch, demote_unrenderable

GARBAGE_SS = "ss://07042893-9818-4e42-a3e9-2f9f1dc4f6a1@127.0.0.1:9000#garbage"
GOOD_SS = (
    "ss://"
    + base64.b64encode(b"aes-256-gcm:changeit-now").decode()
    + "@127.0.0.1:8388#good"
)


def make_node(node_id: str, uri: str) -> Node:
    return Node(
        node_id=node_id,
        uri=uri,
        protocol="ss",
        server="127.0.0.1",
        port=8388,
        user=None,
        source="test",
        first_seen_s=time.time(),
    )


class FakeProbeBatchStore:
    """Records whether a node's verdict survives the admission gate."""

    def __init__(self) -> None:
        self.saved: dict[str, ProbeResult] = {}
        self.nodes: dict[str, Node] = {}

    def get_node(self, node_id: str) -> Node | None:
        return self.nodes.get(node_id)

    def apply_probe_results(self, results: dict[str, ProbeResult]) -> None:
        self.saved.update(results)


def test_admit_batch_flips_unrenderable_alive_nodes_to_dead() -> None:
    good = make_node("nd_good", GOOD_SS)
    bad = make_node("nd_bad", GARBAGE_SS)
    store = FakeProbeBatchStore()
    store.nodes = {good.node_id: good, bad.node_id: bad}
    batch = {
        good.node_id: ProbeResult(good.node_id, alive=True, latency_ms=12),
        bad.node_id: ProbeResult(bad.node_id, alive=True, latency_ms=9),
    }

    _admit_batch(store, batch)

    assert store.saved[good.node_id].alive is True
    assert store.saved[good.node_id].error is None
    assert store.saved[bad.node_id].alive is False
    assert store.saved[bad.node_id].error == "unrenderable"


def test_admit_batch_leaves_dead_verdicts_alone() -> None:
    good = make_node("nd_d", GOOD_SS)
    store = FakeProbeBatchStore()
    store.nodes = {good.node_id: good}
    batch = {good.node_id: ProbeResult(good.node_id, alive=False, error="timeout")}

    _admit_batch(store, batch)

    assert store.saved[good.node_id].alive is False
    assert store.saved[good.node_id].error == "timeout"


def test_admit_batch_skips_unknown_nodes() -> None:
    store = FakeProbeBatchStore()
    batch = {"nd_ghost": ProbeResult("nd_ghost", alive=True)}

    _admit_batch(store, batch)

    assert store.saved["nd_ghost"].alive is True


def test_pool_store_roundtrip_garbage_node_is_dead() -> None:
    from engine.models import NodeCandidate

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(str(Path(tmp) / "t.db"))
        store.create_schema()
        cand = NodeCandidate(
            uri=GARBAGE_SS,
            protocol="ss",
            server="127.0.0.1",
            port=8388,
            user=None,
            source="test",
        )
        store.upsert_candidates([cand])
        node = store.load_nodes(state="untested")[0]
        _admit_batch(store, {node.node_id: ProbeResult(node.node_id, alive=True)})
        assert store.pool_counts()["alive"] == 0
        assert store.get_node(node.node_id).state == "dead"


def test_demote_unrenderable_sweeps_alive_garbage() -> None:
    from engine.models import NodeCandidate

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(str(Path(tmp) / "t.db"))
        store.create_schema()
        store.upsert_candidates(
            [
                NodeCandidate(
                    uri=GARBAGE_SS,
                    protocol="ss",
                    server="127.0.0.1",
                    port=9000,
                    user=None,
                    source="test",
                ),
                NodeCandidate(
                    uri=GOOD_SS,
                    protocol="ss",
                    server="127.0.0.1",
                    port=8388,
                    user=None,
                    source="test",
                ),
            ]
        )
        # Leak state: a URI that changed after admission left garbage flagged
        # alive. The sweep must demote exactly the renderer-rejected node.
        alive = {
            n.node_id: ProbeResult(n.node_id, alive=True) for n in store.load_nodes()
        }
        store.apply_probe_results(alive)
        assert store.pool_counts()["alive"] == 2

        demoted = demote_unrenderable(store)
        assert demoted == 1
        assert store.pool_counts()["alive"] == 1
        assert store.pool_counts()["dead"] == 1
