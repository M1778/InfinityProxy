"""Unit tests for engine.stability scoring helpers (ADR-0009)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from engine.models import Node
from engine.stability import (
    availability,
    composite_score,
    score_nodes,
    speed_percentile_map,
    tier,
    wilson_lower,
)


def make_node(
    node_id: str,
    protocol: str = "vless",
    throughput_kb_s: int | None = None,
    probe_ok: int = 0,
    probe_total: int = 0,
    latency_ms: int | None = None,
) -> Node:
    return Node(
        node_id=node_id,
        uri=f"{protocol}://x@{node_id}:443",
        protocol=protocol,
        server=node_id,
        port=443,
        user="x",
        source="test",
        first_seen_s=1.0,
        last_latency_ms=latency_ms,
        state="alive",
        throughput_kb_s=throughput_kb_s,
        probe_ok=probe_ok,
        probe_total=probe_total,
    )


def test_wilson_lower_blends_small_samples() -> None:
    assert wilson_lower(0, 0) == 0.0
    assert wilson_lower(0, 100) == 0.0
    # 2/2 is far less confident than 95/100 (the "2 ratings" problem).
    assert wilson_lower(2, 2) < wilson_lower(95, 100)
    assert wilson_lower(95, 100) == pytest.approx(0.8882, abs=1e-3)
    assert 0.0 <= wilson_lower(100, 100) < 1.0


def test_availability_of_never_probed_node_is_zero() -> None:
    assert availability(make_node("n1")) == 0.0


def test_tier_rules() -> None:
    assert tier(make_node("never", probe_total=0), min_probes=6, min_avail=0.4) is None
    assert (
        tier(make_node("cold", probe_ok=2, probe_total=2), min_probes=6, min_avail=0.4)
        is None
    )
    good = make_node("good", probe_ok=90, probe_total=100)
    bad = make_node("bad", probe_ok=20, probe_total=100)
    assert tier(good, min_probes=6, min_avail=0.4) == "A"
    assert tier(bad, min_probes=6, min_avail=0.4) == "B"


def test_speed_percentile_handles_unknown_and_lone_member() -> None:
    nodes = [make_node("a", throughput_kb_s=500), make_node("b")]
    pct = speed_percentile_map(nodes)
    assert pct["a"] == 1.0  # sole measured member ranks best
    assert "b" not in pct
    assert speed_percentile_map([make_node("x")]) == {}


def test_speed_percentile_min_and_max() -> None:
    nodes = [
        make_node("slow", throughput_kb_s=200),
        make_node("mid", throughput_kb_s=900),
        make_node("fast", throughput_kb_s=1500),
    ]
    pct = speed_percentile_map(nodes)
    assert pct["slow"] == 0.0
    assert pct["mid"] == 0.5
    assert pct["fast"] == 1.0


def test_speed_percentile_is_within_protocol() -> None:
    nodes = [
        make_node("vless_a", protocol="vless", throughput_kb_s=100),
        make_node("vless_b", protocol="vless", throughput_kb_s=200),
        make_node("ss_a", protocol="ss", throughput_kb_s=100000),
    ]
    pct = speed_percentile_map(nodes)
    assert pct["ss_a"] == 1.0  # huge raw value never outbids vless internally
    assert pct["vless_a"] == 0.0
    assert pct["vless_b"] == 1.0


def test_composite_cold_node_has_no_availability_term() -> None:
    cold = make_node("cold", throughput_kb_s=9000, probe_ok=2, probe_total=2)
    pct = {"cold": 1.0}
    assert composite_score(
        cold, pct, min_probes=6, weight_avail=0.6, weight_speed=0.4
    ) == pytest.approx(0.4)


def test_score_nodes_orders_tier_a_then_b_then_cold() -> None:
    nodes = [
        make_node("cold", throughput_kb_s=9000, probe_ok=2, probe_total=2),
        make_node("tier_b", throughput_kb_s=7000, probe_ok=20, probe_total=100),
        make_node("tier_a", throughput_kb_s=1, probe_ok=90, probe_total=100),
    ]
    ordered = score_nodes(
        nodes,
        min_probes=6,
        min_avail=0.4,
        weight_avail=0.6,
        weight_speed=0.4,
    )
    assert [n.node_id for n in ordered] == ["tier_a", "tier_b", "cold"]


def test_score_nodes_orders_within_tier_a_by_composite() -> None:
    # Identical throughput => same speed percentile, so the availability term
    # decides; more history (95/100) outranks a shakier 90/100 within Tier A.
    better = make_node("better", throughput_kb_s=300, probe_ok=95, probe_total=100)
    faster = make_node("faster", throughput_kb_s=300, probe_ok=90, probe_total=100)
    ordered = score_nodes(
        [faster, better],
        min_probes=6,
        min_avail=0.4,
        weight_avail=0.6,
        weight_speed=0.4,
    )
    assert [n.node_id for n in ordered] == ["better", "faster"]
