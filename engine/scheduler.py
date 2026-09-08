"""Always-on Engine runtime: source refresh loop + per-tunnel health loop.

Vocabulary per CONTEXT.md: tunnels are continuously health-checked; a node is
swapped after `max_misses` consecutive failed probes, and degraded tunnels are
topped up on every pass.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from engine.assigner import Assigner
from engine.config import Settings
from engine.db import Store
from engine.filter.runner import batch_probe
from engine.models import Tunnel
from engine.pool import refresh_source, seed_sources, source_by_name
from engine.scraper import SOURCES
from engine.tunnel.config import _PROTOCOL_OUTBOUND_TYPE, render_config
from engine.tunnel.container import ContainerController, TunnelRuntimeUnavailable

logger = logging.getLogger("infinity.engine")

ASSIGNABLE_PROTOCOLS = frozenset(_PROTOCOL_OUTBOUND_TYPE)


class Engine:
    """Owns the store, rate-limit-free loops, and container control. Daemon threads."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        assigner: Assigner,
        controller: ContainerController,
    ) -> None:
        self.settings = settings
        self.store = store
        self.assigner = assigner
        self.controller = controller
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._strikes: dict[tuple[str, str], int] = {}
        self._rendered: dict[str, tuple[tuple[str, ...], dict[str, Any]]] = {}
        self._swapped_24h: dict[str, int] = {}
        self._last_check: dict[str, float] = {}
        self._threads: list[threading.Thread] = []
        self._started_s = time.time()
        self._refreshing: set[str] = set()
        self._refresh_lock = threading.Lock()

    def start(self) -> None:
        seed_sources(self.store)
        self.reconcile()
        self._threads = [
            threading.Thread(
                target=self._source_loop, name="infinity-sources", daemon=True
            ),
            threading.Thread(
                target=self._health_loop, name="infinity-health", daemon=True
            ),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)

    def reconcile(self) -> None:
        """Boot reconciliation: adopt live tunnels, reap orphans, mark running."""
        expected = {
            t.tunnel_id for t in self.store.load_tunnels() if t.state != "stopped"
        }
        try:
            self.controller.reconcile(expected)
        except TunnelRuntimeUnavailable:
            logger.warning("docker unreachable; skipping container reconciliation")
        for tunnel_id in expected:
            self.store.set_tunnel_state(tunnel_id, "running")

    def request_source_refresh(self, name: str) -> bool:
        """Queue a source scrape on a background thread; never blocks the caller.

        Single-flight per source name: concurrent requests are acknowledged but
        do not stack a second scrape.
        """
        source = source_by_name(name)
        if source is None:
            return False
        with self._refresh_lock:
            if name in self._refreshing:
                return True
            self._refreshing.add(name)
        threading.Thread(
            target=self._refresh_worker, args=(name,), name="panel-refresh", daemon=True
        ).start()
        return True

    def _refresh_worker(self, name: str) -> None:
        try:
            source = source_by_name(name)
            if source is not None:
                refresh_source(self.store, self.settings, source)
        except Exception:  # noqa: BLE001 - a manual scrape must not kill the panel request
            logger.warning("manual source refresh failed: %s", name, exc_info=True)
        finally:
            with self._refresh_lock:
                self._refreshing.discard(name)

    def health_check(self, tunnel: Tunnel) -> tuple[int, int]:
        """One health pass for a tunnel. Returns (swapped, added)."""
        with self._lock:
            return self._health_check_locked(tunnel)

    def _health_check_locked(self, tunnel: Tunnel) -> tuple[int, int]:
        tunnel_id = tunnel.tunnel_id
        self._last_check[tunnel_id] = time.time()
        assigned = [
            n
            for n in (
                self.store.get_node(node_id)
                for node_id in self.store.node_ids_assigned_to(tunnel_id)
            )
            if n is not None
        ]
        swapped = added = 0
        changed = False

        if assigned:
            results = batch_probe(
                assigned,
                batch_size=self.settings.batch_size,
                timeout_s=self.settings.probe_timeout_s,
            )
            self.store.apply_probe_results(results)
            for node in assigned:
                key = (tunnel_id, node.node_id)
                if results[node.node_id].alive:
                    self._strikes.pop(key, None)
                    continue
                self._strikes[key] = self._strikes.get(key, 0) + 1
                if self._strikes[key] < self.settings.max_misses:
                    continue
                self._strikes.pop(key, None)
                self.store.unassign_node(node.node_id)
                self._bump_granted(tunnel_id, -1)
                self._swapped_24h[tunnel_id] = self._swapped_24h.get(tunnel_id, 0) + 1
                swapped += 1
                changed = True

        fresh = self.store.get_tunnel(tunnel_id)
        if fresh is None:
            return 0, 0
        if swapped or fresh.degraded:
            added = len(
                self.assigner.top_up(
                    self.store,
                    tunnel_id,
                    fresh.node_count_requested,
                    protocols=set(ASSIGNABLE_PROTOCOLS),
                )
            )
            if added:
                self._bump_granted(tunnel_id, added)
                changed = True

        if changed:
            self._redeploy(tunnel_id)
        return swapped, added

    def _bump_granted(self, tunnel_id: str, delta: int) -> None:
        tunnel = self.store.get_tunnel(tunnel_id)
        if tunnel is None:
            return
        tunnel.node_count_granted = max(0, tunnel.node_count_granted + delta)
        tunnel.state = "running"
        self.store.save_tunnel(tunnel)

    def _redeploy(self, tunnel_id: str) -> None:
        tunnel = self.store.get_tunnel(tunnel_id)
        if tunnel is None:
            return
        ids = self.store.node_ids_assigned_to(tunnel_id)
        nodes = [
            n
            for n in (self.store.get_node(node_id) for node_id in ids)
            if n is not None
            and n.state == "alive"
            and n.protocol in ASSIGNABLE_PROTOCOLS
        ]
        signature = tuple(sorted(n.node_id for n in nodes))
        if not signature:
            return
        if (
            self._rendered.get(tunnel_id, ("", None))[0] == signature
            and (self._rendered[tunnel_id][1])
        ):
            return
        config = render_config(
            tunnel, nodes, urltest_interval_s=self.settings.urltest_interval_s
        )
        try:
            self.controller.update(tunnel_id, config)
        except TunnelRuntimeUnavailable:
            logger.warning("docker unreachable; tunnel %s not redeployed", tunnel_id)
            return
        self._rendered[tunnel_id] = (signature, config)

    def _source_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_due()
            except Exception:  # noqa: BLE001 - the loop must survive any source failure
                logger.exception("source refresh failed")
            self._stop.wait(30)

    def _refresh_due(self) -> None:
        now = time.time()
        summaries = {s["name"]: s for s in self.store.sources_summary()}
        for source in SOURCES:
            if self._stop.is_set():
                return
            last = summaries.get(source.name, {}).get("last_fetch_s")
            if last is None or now - float(last) >= source.cadence_s:
                try:
                    refresh_source(self.store, self.settings, source)
                except Exception:  # noqa: BLE001
                    logger.exception("source %s refresh failed", source.name)

    def _health_loop(self) -> None:
        while not self._stop.is_set():
            for tunnel in self.store.load_tunnels():
                if self._stop.is_set():
                    return
                if tunnel.state == "stopped":
                    continue
                try:
                    self.health_check(tunnel)
                except Exception:  # noqa: BLE001
                    logger.exception("health check failed for %s", tunnel.tunnel_id)
            self._stop.wait(self.settings.health_interval_s)

    def engine_status(self) -> dict[str, Any]:
        summaries = {s["name"]: s for s in self.store.sources_summary()}
        now = time.time()
        pool = self.store.pool_counts()
        tunnels = self.store.load_tunnels()
        stale = 0
        sources = []
        for source in SOURCES:
            last = summaries.get(source.name, {}).get("last_fetch_s")
            sources.append(
                {
                    "name": source.name,
                    "last_fetch_s": int(now - float(last)) if last else None,
                    "cadence_s": source.cadence_s,
                }
            )
            if last is None:
                stale += 1
            elif now - float(last) > source.cadence_s * 2:
                stale += 1
        return {
            "engine": "ok",
            "uptime_s": int(now - self._started_s),
            "pool": {
                "total": pool["total"],
                "alive": pool["alive"],
                "dead": pool["dead"],
                "untested": pool["untested"],
                "in_use": pool["in_use"],
                "by_protocol": self.store.pool_by_protocol(),
                "assignable": sum(
                    1
                    for n in self.store.load_nodes(
                        state="alive", protocols=set(ASSIGNABLE_PROTOCOLS)
                    )
                    if n.node_id not in self.store.node_ids_in_use()
                ),
            },
            "tunnels": {
                "active": sum(1 for t in tunnels if t.state != "stopped"),
                "degraded": sum(
                    1 for t in tunnels if t.degraded and t.state != "stopped"
                ),
            },
            "sources": sources,
            "stale_sources": stale,
        }

    def tunnel_health(self, tunnel: Tunnel) -> dict[str, Any]:
        last = self._last_check.get(tunnel.tunnel_id)
        return {
            "last_check_s": int(time.time() - last) if last else None,
            "dead_swapped_24h": self._swapped_24h.get(tunnel.tunnel_id, 0),
        }
