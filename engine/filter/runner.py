"""Batched liveness probes across candidate nodes (docs/scraping.md#liveness-filter)."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from engine.models import Node, ProbeResult

from .probe import probe


def batch_probe(
    nodes: list[Node],
    *,
    batch_size: int = 50,
    timeout_s: float = 4.0,
    max_workers: int | None = None,
    on_batch: Callable[[dict[str, ProbeResult]], None] | None = None,
) -> dict[str, ProbeResult]:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    workers = min(batch_size, max_workers if max_workers is not None else batch_size)
    results: dict[str, ProbeResult] = {}
    for start in range(0, len(nodes), batch_size):
        chunk = nodes[start : start + batch_size]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_probe_one, node, timeout_s): node.node_id for node in chunk
            }
            for future, node_id in futures.items():
                results[node_id] = future.result()
        if on_batch is not None:
            # Surface every batch's verdict as soon as it is known, so the
            # store sees alive nodes incrementally instead of after the whole
            # (potentially very large) pass completes.
            partial = {
                node_id: results[node_id]
                for node in chunk
                if (node_id := node.node_id) in results
            }
            on_batch(partial)
    return results


def _probe_one(node: Node, timeout_s: float) -> ProbeResult:
    try:
        return probe(node, timeout_s)
    except Exception as exc:  # noqa: BLE001 - a probe failure must never take a batch down
        return ProbeResult(
            node_id=node.node_id, alive=False, error=f"probe raised: {exc}"
        )
