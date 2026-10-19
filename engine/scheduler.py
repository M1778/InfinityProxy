"""Always-on Engine runtime: source refresh loop + per-tunnel health loop.

Vocabulary per CONTEXT.md: tunnels are continuously health-checked; a node is
swapped after `max_misses` consecutive failed probes, and degraded tunnels are
topped up on every pass.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
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
        self._swapped_24h: dict[str, deque[float]] = {}
        self._restarted_24h: dict[str, deque[float]] = {}
        self._last_check: dict[str, float] = {}
        self._threads: list[threading.Thread] = []
        self._started_s = time.time()
        self._refreshing: set[str] = set()
        self._refresh_lock = threading.Lock()
        self._health_busy: set[str] = set()
        self._last_working_set_s: float | None = None

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
            threading.Thread(
                target=self._working_set_loop, name="infinity-working-set", daemon=True
            ),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)

    def forget_tunnel(self, tunnel_id: str) -> None:
        """Drop all per-tunnel runtime bookkeeping after the row is deleted.

        Strikes and the rendered-config signature must not survive a delete:
        a recreated tunnel reusing the id would otherwise inherit the dead
        one's miss counts and skip its first redeploy.
        """
        with self._lock:
            for entry in [k for k in self._strikes if k[0] == tunnel_id]:
                self._strikes.pop(entry, None)
            self._health_busy.discard(tunnel_id)
            self._rendered.pop(tunnel_id, None)
        self._last_check.pop(tunnel_id, None)
        self._swapped_24h.pop(tunnel_id, None)
        self._restarted_24h.pop(tunnel_id, None)

    def reconcile(self) -> None:
        """Boot reconciliation: adopt live tunnels, reap orphans, mark running.

        Also free node assignments that point at tunnels we have no row for. A
        tunnel deleted outside the normal release path (crash between release
        and delete, legacy data) would otherwise leave its nodes permanently
        "in use", starving every degraded tunnel's top-up forever.
        """
        expected = {
            t.tunnel_id for t in self.store.load_tunnels() if t.state != "stopped"
        }
        try:
            self.controller.reconcile(expected)
        except TunnelRuntimeUnavailable:
            logger.warning("docker unreachable; skipping container reconciliation")
        live = {t.tunnel_id for t in self.store.load_tunnels()}
        for orphan in self.store.assigned_tunnel_ids() - live:
            freed = self.store.release_tunnel(orphan)
            if freed:
                logger.warning(
                    "freed %d node assignment(s) for missing tunnel %s", freed, orphan
                )
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
        """One health pass for a tunnel. Returns (swapped, added).

        Probes run without the engine lock: a full assigned set takes seconds
        per node, and holding the lock that long blocks every renew request
        behind it. The lock guards only strike bookkeeping and the per-tunnel
        counters. A pass already in flight for this tunnel makes another call
        a no-op instead of probing the same nodes twice in parallel.
        """
        tunnel_id = tunnel.tunnel_id
        with self._lock:
            if tunnel_id in self._health_busy:
                return 0, 0
            self._health_busy.add(tunnel_id)
        try:
            return self._run_health_pass(tunnel)
        finally:
            with self._lock:
                self._health_busy.discard(tunnel_id)

    def _run_health_pass(self, tunnel: Tunnel) -> tuple[int, int]:
        tunnel_id = tunnel.tunnel_id
        if tunnel.state == "starting":
            # The row exists but the container may still be under construction:
            # POST /tunnels flips the state to running only after the container
            # is up, and the first-use sing-box image pull makes that window
            # tens of seconds wide. Probing or redeploying now would race the
            # creator for the same container name.
            return 0, 0
        self._last_check[tunnel_id] = time.time()
        assigned = [
            n
            for n in (
                self.store.get_node(node_id)
                for node_id in self.store.node_ids_assigned_to(tunnel_id)
            )
            if n is not None
        ]
        swapped = 0

        if assigned:
            results = batch_probe(
                assigned,
                batch_size=self.settings.batch_size,
                timeout_s=self.settings.probe_timeout_s,
            )
            # Record windowed availability counters for the 30s health stream
            # (ADR-0009, Phase 1) before any swap decisions are made on it.
            if self.settings.stability_enabled:
                self.store.apply_probe_results(
                    results,
                    stability=True,
                    window_s=self.settings.stability_window_s,
                )
            else:
                self.store.apply_probe_results(results)
            with self._lock:
                swapped = self._apply_health_verdicts(tunnel, assigned, results)
        added = self._after_provision(tunnel_id)
        if self._change_marker(tunnel_id) or swapped or added:
            self._clear_change_marker(tunnel_id)
            self._redeploy(tunnel_id)
        self._self_heal(tunnel_id)
        return swapped, added

    def _apply_health_verdicts(
        self, tunnel: Tunnel, assigned: list, results: dict
    ) -> int:
        """Strike bookkeeping under the lock: count dead nodes and swap them."""
        tunnel_id = tunnel.tunnel_id
        swapped = 0
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
            self._record_event(self._swapped_24h, tunnel_id)
            swapped += 1
        return swapped

    def _self_heal(self, tunnel_id: str) -> None:
        """Redeploy a tunnel whose container stopped running but whose row is live.

        Health checks catch dead *nodes*, not a dead sing-box inside the tunnel
        container. The restart policy recovers a crashing process on its own,
        but a container that has stopped (docker never restarts a *stopped*
        container, only a *failed* one) leaves the tunnel's listener down while
        the panel still reports it live. Re-running the last rendered config is
        cheaper and more honest than reporting the tunnel healthy.
        """
        tunnel = self.store.get_tunnel(tunnel_id)
        if tunnel is None or tunnel.state == "stopped":
            return
        try:
            if self.controller.is_running(tunnel_id):
                return
        except TunnelRuntimeUnavailable:
            logger.warning("docker unreachable; cannot self-heal %s", tunnel_id)
            return
        logger.warning("tunnel %s container not running; redeploying", tunnel_id)
        if self._redeploy(tunnel_id):
            self._record_event(self._restarted_24h, tunnel_id)

    def _bump_granted(self, tunnel_id: str, delta: int) -> None:
        tunnel = self.store.get_tunnel(tunnel_id)
        if tunnel is None:
            return
        tunnel.node_count_granted = max(0, tunnel.node_count_granted + delta)
        tunnel.state = "running"
        self.store.save_tunnel(tunnel)

    def _redeploy(self, tunnel_id: str) -> bool:
        """Redeploy a tunnel's config; True when a new container was rolled."""
        tunnel = self.store.get_tunnel(tunnel_id)
        if tunnel is None:
            return False
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
            return False
        if (
            self._rendered.get(tunnel_id, ("", None))[0] == signature
            and (self._rendered[tunnel_id][1])
        ):
            return False
        config = render_config(
            tunnel, nodes, urltest_interval_s=self.settings.urltest_interval_s
        )
        try:
            self.controller.update(tunnel_id, config)
        except TunnelRuntimeUnavailable as exc:
            logger.warning("tunnel %s not redeployed: %s", tunnel_id, exc)
            return False
        self._rendered[tunnel_id] = (signature, config)
        return True

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

    def _working_set_loop(self) -> None:
        while not self._stop.is_set():
            if self.settings.stability_enabled:
                try:
                    self._working_set_pass()
                except Exception:  # noqa: BLE001 - the loop must survive one bad pass
                    logger.exception("working-set probe pass failed")
            self._stop.wait(self.settings.stability_working_set_cadence_s)

    def _working_set_pass(self) -> int:
        """Re-probe the top `stability_working_set` alive, unassigned nodes.

        The working set is a query, not a table (ADR-0009 Phase 2): the highest
        cached availability among unassigned nodes, which doubles as the
        population the assigner most often draws from next. Probing it every
        cadence keeps `score_f`-ordering fresh across the pool's most-assignable
        slice far more often than a full deep-scan would. Handshake-only (the
        30s health loop already certifies assigned nodes; throughput probing
        stays an admission-gate job). Returns the number of nodes this pass
        probed after the reprobe-min guard.
        """
        self._last_working_set_s = time.time()
        nodes = self.store.load_nodes(
            state="alive",
            in_use=False,
            order_by_score=True,
            limit=self.settings.stability_working_set,
        )
        now = time.time()
        due = [
            n
            for n in nodes
            if n.last_probe_s is None
            or now - n.last_probe_s >= self.settings.stability_reprobe_min_s
        ]
        if not due:
            return 0
        results = batch_probe(
            due,
            batch_size=self.settings.batch_size,
            timeout_s=self.settings.probe_timeout_s,
            on_batch=lambda batch: self.store.apply_probe_results(
                batch,
                stability=True,
                window_s=self.settings.stability_window_s,
            ),
        )
        return len(results)

    def engine_status(self) -> dict[str, Any]:
        summaries = {s["name"]: s for s in self.store.sources_summary()}
        now = time.time()
        if self.settings.stability_enabled:
            pool = self.store.pool_counts(
                min_probes=self.settings.stability_min_probes,
                min_avail=self.settings.stability_min_avail,
                max_age_s=self.settings.stability_max_age_s,
                working_set_size=self.settings.stability_working_set,
            )
        else:
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
        assignable = self.store.assignable_count(set(ASSIGNABLE_PROTOCOLS))
        return {
            "engine": "ok",
            "uptime_s": int(now - self._started_s),
            "pool": {
                "total": pool["total"],
                "alive": pool["alive"],
                "dead": pool["dead"],
                "untested": pool["untested"],
                "in_use": pool["in_use"],
                "tier_a": pool["tier_a"],
                "tier_b": pool["tier_b"],
                "working_set": pool["working_set"],
                "avg_score": pool["avg_score"],
                "by_protocol": self.store.pool_by_protocol(),
                "assignable": assignable,
                "working_set_next_s": (
                    max(
                        0,
                        int(
                            self._last_working_set_s
                            + self.settings.stability_working_set_cadence_s
                            - now
                        ),
                    )
                    if self._last_working_set_s is not None
                    else 0
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

    def _record_event(self, events: dict[str, deque[float]], tunnel_id: str) -> None:
        """Timestamp a swap/restart event and drop entries outside the 24h window.

        Only `_apply_health_verdicts` and `_self_heal` ever write these, and each
        writes a different dict, so the lock those callers already hold is the
        memory guard; a deque also caps churn — entries expire on read so the
        counters genuinely restart counting every day instead of growing forever
        like the previous ever-rising ints (whose only reset was an engine
        restart, which zeroes them entirely and lies the other way).
        """
        now = time.time()
        window = events.setdefault(tunnel_id, deque())
        window.append(now)
        horizon = now - 86400
        while window and window[0] < horizon:
            window.popleft()

    def _events_in_window(self, events: dict[str, deque[float]], tunnel_id: str) -> int:
        window = events.get(tunnel_id)
        if not window:
            return 0
        horizon = time.time() - 86400
        while window and window[0] < horizon:
            window.popleft()
        return len(window)

    def tunnel_health(self, tunnel: Tunnel) -> dict[str, Any]:
        last = self._last_check.get(tunnel.tunnel_id)
        return {
            "last_check_s": int(time.time() - last) if last else None,
            "dead_swapped_24h": self._events_in_window(
                self._swapped_24h, tunnel.tunnel_id
            ),
            "restarts_24h": self._events_in_window(
                self._restarted_24h, tunnel.tunnel_id
            ),
        }
