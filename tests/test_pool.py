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


def test_admit_batch_gate_missing_settings_keeps_handshake_verdicts() -> None:
    from engine.config import Settings

    good = make_node("nd_g", GOOD_SS)
    store = FakeProbeBatchStore()
    store.nodes = {good.node_id: good}
    batch = {good.node_id: ProbeResult(good.node_id, alive=True, latency_ms=9)}
    _admit_batch(store, batch)
    assert store.saved[good.node_id].alive is True

    # A settings-armed gate demotes a node that never delivered a body.
    gated = FakeProbeBatchStore()
    gated.nodes = {good.node_id: good}
    _admit_batch(
        gated,
        {good.node_id: ProbeResult(good.node_id, alive=True, throughput_kb_s=None)},
        settings=Settings(),
    )
    assert gated.saved[good.node_id].alive is False


def test_admit_batch_demotes_missing_and_slow_throughput() -> None:
    from engine.config import Settings

    good = make_node("nd_tt", GOOD_SS)
    slow = make_node("nd_slow", GOOD_SS)
    settings = Settings(throughput_enabled=True, throughput_min_kb_s=200)
    store = FakeProbeBatchStore()
    store.nodes = {good.node_id: good, slow.node_id: slow}
    batch = {
        good.node_id: ProbeResult(good.node_id, alive=True, throughput_kb_s=900),
        slow.node_id: ProbeResult(slow.node_id, alive=True, throughput_kb_s=120),
    }

    _admit_batch(store, batch, settings)

    assert store.saved[good.node_id].alive is True
    assert store.saved[slow.node_id].alive is False
    assert "below floor 200" in store.saved[slow.node_id].error

    node2 = make_node("nd_none", GOOD_SS)
    store2 = FakeProbeBatchStore()
    store2.nodes = {node2.node_id: node2}
    _admit_batch(
        store2,
        {node2.node_id: ProbeResult(node2.node_id, alive=True, throughput_kb_s=None)},
        settings,
    )
    assert store2.saved[node2.node_id].alive is False
    assert "failed throughput certification" in store2.saved[node2.node_id].error


def test_admit_batch_records_stability_counters_when_enabled() -> None:
    from engine.config import Settings

    class RecordingStore(FakeProbeBatchStore):
        def __init__(self) -> None:
            super().__init__()
            self.stability_flags: list[bool] = []

        def apply_probe_results(
            self, results: dict[str, ProbeResult], *, stability: bool = False
        ) -> None:
            self.stability_flags.append(stability)
            self.saved.update(results)

    good = make_node("nd_stab", GOOD_SS)
    on = RecordingStore()
    on.nodes = {good.node_id: good}
    _admit_batch(
        on,
        {good.node_id: ProbeResult(good.node_id, alive=True, throughput_kb_s=500)},
        Settings(stability_enabled=True),
    )
    assert on.stability_flags == [True]
    assert on.saved[good.node_id].alive is True

    off = RecordingStore()
    off.nodes = {good.node_id: good}
    _admit_batch(
        off,
        {good.node_id: ProbeResult(good.node_id, alive=True, throughput_kb_s=500)},
        Settings(stability_enabled=False),
    )
    assert off.stability_flags == [False]


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


def test_demote_sweep_rejects_ws_transport_alive() -> None:
    from engine.models import NodeCandidate

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(str(Path(tmp) / "t.db"))
        store.create_schema()
        store.upsert_candidates(
            [
                NodeCandidate(
                    uri=(
                        "trojan://pw@127.0.0.1:443?security=tls&sni=example.com&type=ws"
                    ),
                    protocol="trojan",
                    server="127.0.0.1",
                    port=443,
                    user="pw",
                    source="test",
                )
            ]
        )
        alive = {
            n.node_id: ProbeResult(n.node_id, alive=True) for n in store.load_nodes()
        }
        store.apply_probe_results(alive)
        assert store.pool_counts()["alive"] == 1

        demoted = demote_unrenderable(store)
        assert demoted == 1
        assert store.pool_counts()["alive"] == 0
        assert store.pool_counts()["dead"] == 1


def test_probe_new_respects_untested_budget() -> None:
    from engine.config import Settings
    from engine.models import NodeCandidate, SourceManifest
    from engine.pool import _probe_new

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(str(Path(tmp) / "t.db"))
        store.create_schema()
        store.upsert_candidates(
            [
                NodeCandidate(
                    uri=f"vless://abc@1.0.0.{i}:443?type=tcp",
                    protocol="vless",
                    server=f"1.0.0.{i}",
                    port=443,
                    user="abc",
                    source="test",
                )
                for i in range(1, 7)
            ]
        )
        source = SourceManifest(
            name="test",
            urls=(),
            cadence_s=60,
            encoding="plain",
            line_separated=True,
            license="",
            protocols=(),
        )
        settings = Settings(batch_size=50, probe_budget_per_refresh=2)

        import engine.pool as pool_mod

        probed: list[int] = []

        def fake_batch_probe(nodes, **kwargs):
            probed.append(len(nodes))

            def on_batch(batch, **bkw):
                kwargs["on_batch"](batch)

            for node in nodes:
                kwargs["on_batch"](
                    {node.node_id: ProbeResult(node.node_id, alive=False)}
                )
            return {}

        original = pool_mod.batch_probe
        pool_mod.batch_probe = fake_batch_probe
        try:
            _probe_new(store, settings, source)
            _probe_new(store, settings, source)
        finally:
            pool_mod.batch_probe = original

        # Two refreshes, budget 2 each: the first drains 2 untested that become
        # dead; the second takes the next 2 untested (FIFO) plus up to batch_size
        # of this source's dead nodes for retest. No single refresh drains the
        # whole queue: dead + untested stay bounded.
        assert probed == [2, 4]
