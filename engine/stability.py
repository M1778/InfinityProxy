"""Stability scoring: the engine's slow-moving membership vote (ADR-0009).

Availability is the Wilson lower bound over the windowed probe counters — a
small sample is blended toward the mean so a 2/2 node never outranks a 95/100
node. Speed is the node's throughput percentile within its own protocol,
computed at assignment time over the candidate set (raw KiB/s scales differ
wildly between vless feeds and ss). Latency is deliberately excluded: sing-box
`urltest` owns request-time delay (ADR-0005), the engine owns membership.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from typing import Sequence

from .models import Node

WILSON_Z = 1.96


def wilson_lower(ok: int, total: int, *, z: float = WILSON_Z) -> float:
    """Lower bound of the Wilson score interval for p = ok/total.

    Returns 0.0 for an empty sample. Shrinks toward the mean as the sample
    shrinks, so few observations are never treated as confident.
    """
    if total <= 0:
        return 0.0
    p_hat = ok / total
    z2 = z * z
    denom = 1 + z2 / total
    center = (p_hat + z2 / (2 * total)) / denom
    half = z * math.sqrt(p_hat * (1 - p_hat) / total + z2 / (4 * total * total)) / denom
    return max(0.0, center - half)


def availability(node: Node) -> float:
    """Wilson lower bound over the node's probe outcomes; 0.0 when never probed."""
    return wilson_lower(node.probe_ok, node.probe_total)


def speed_percentile_map(nodes: Sequence[Node]) -> dict[str, float]:
    """Percentile rank (0..1) of each measured node within its protocol.

    Protocols are ranked separately because their feed scales differ; a lone
    measured member ranks 1.0, the slowest member ranks 0.0, and a node with no
    measured throughput gets no entry (callers default it to 0.0).
    """
    by_protocol: dict[str, list[Node]] = defaultdict(list)
    for node in nodes:
        by_protocol[node.protocol].append(node)
    out: dict[str, float] = {}
    for protocol_nodes in by_protocol.values():
        measured = sorted(
            n.throughput_kb_s for n in protocol_nodes if n.throughput_kb_s is not None
        )
        if not measured:
            continue
        for node in protocol_nodes:
            if node.throughput_kb_s is None:
                continue
            if len(measured) == 1:
                out[node.node_id] = 1.0
                continue
            rank = bisect_right(measured, node.throughput_kb_s)
            out[node.node_id] = (rank - 1) / (len(measured) - 1)
    return out


def tier(node: Node, *, min_probes: int, min_avail: float) -> str | None:
    """A = evaluated and at/above the availability floor; B = evaluated below it.

    None means the node has too few probes to judge (cold start) and must never
    outrank an evaluated node, but stays assignable under pool starvation.
    """
    if node.probe_total < min_probes:
        return None
    return "A" if availability(node) >= min_avail else "B"


def composite_score(
    node: Node,
    percentile_map: dict[str, float],
    *,
    min_probes: int,
    weight_avail: float,
    weight_speed: float,
) -> float:
    """0..1 weighted availability + speed composite for display and sorting.

    A cold node (probe_total < min_probes) contributes no availability term, so
    a single admission measurement can never dominate a node with history.
    """
    speed = weight_speed * percentile_map.get(node.node_id, 0.0)
    if node.probe_total < min_probes:
        return speed
    return weight_avail * availability(node) + speed


def score_nodes(
    nodes: Sequence[Node],
    *,
    min_probes: int,
    min_avail: float,
    weight_avail: float,
    weight_speed: float,
) -> list[Node]:
    """Order candidates for admission: Tier A, then Tier B, then cold, each
    by composite score descending (tested latency as the final tiebreak)."""
    percentiles = speed_percentile_map(nodes)

    def _key(node: Node) -> tuple[int, float, int]:
        node_tier = tier(node, min_probes=min_probes, min_avail=min_avail)
        rank = {"A": 0, "B": 1}.get(node_tier, 2)
        score = composite_score(
            node,
            percentiles,
            min_probes=min_probes,
            weight_avail=weight_avail,
            weight_speed=weight_speed,
        )
        return (rank, -score, node.last_latency_ms is None)

    return sorted(nodes, key=_key)
