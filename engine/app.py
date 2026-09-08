"""Flask control API + read-only dashboard (docs/api.md, ADR-0003)."""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from flask import Flask, jsonify, redirect, request

from engine.assigner import Assigner, PortExhausted
from engine.config import Settings
from engine.db import Store
from engine.models import Node, Tunnel
from engine.scheduler import ASSIGNABLE_PROTOCOLS, Engine
from engine.stability import (
    availability,
    composite_score,
    score_nodes,
    speed_percentile_map,
    tier,
)
from engine.tunnel.config import render_config
from engine.tunnel.container import ContainerController, TunnelRuntimeUnavailable

logger = logging.getLogger("infinity.app")

TUNNEL_PROTOCOLS = ["http", "socks5"]


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    controller: ContainerController | None = None,
    assigner: Assigner | None = None,
    background: bool = True,
) -> Flask:
    settings = settings or Settings.from_env()
    store = store or Store(settings.db_path)
    store.create_schema()
    controller = controller or ContainerController(settings)
    assigner = assigner or Assigner(settings)
    engine = Engine(settings, store, assigner, controller)

    app = Flask("infinityproxy")
    app.json.ensure_ascii = False

    def err(code: str, message: str, status: int):
        return jsonify({"error": {"code": code, "message": message}}), status

    @app.get("/status")
    def status():
        return jsonify(engine.engine_status())

    @app.get("/nodes")
    def list_nodes():
        state = request.args.get("state")
        source = request.args.get("source")
        query = request.args.get("q")
        in_use_raw = request.args.get("in_use")
        in_use = {"1": True, "true": True, "0": False, "false": False}.get(
            in_use_raw or "", None
        )
        protocols: set[str] = set()
        for part in request.args.get("protocol", "").split(","):
            if part.strip():
                protocols.add(part.strip())
        try:
            limit = min(int(request.args.get("limit", 200)), 1000)
        except (TypeError, ValueError):
            return err("invalid_request", "limit must be an integer", 400)
        if limit < 1:
            return err("invalid_request", "limit must be >= 1", 400)
        sort = request.args.get("sort")
        min_tier_raw = request.args.get("min_tier")
        if min_tier_raw is not None and min_tier_raw not in ("A", "B"):
            return err("invalid_request", "min_tier must be A or B", 400)
        want_score = sort == "score" or (
            sort is None and state == "alive" and settings.stability_enabled
        )
        if sort == "score" and not settings.stability_enabled:
            return err(
                "invalid_request",
                "sort=score requires INFINITY_STABILITY_ENABLED",
                400,
            )
        if min_tier_raw is not None and not settings.stability_enabled:
            return err(
                "invalid_request",
                "min_tier requires INFINITY_STABILITY_ENABLED",
                400,
            )
        nodes = store.load_nodes(
            state=state or None,
            protocols=protocols or None,
            source=source or None,
            limit=limit,
            query=query or None,
            in_use=in_use,
            order_by_score=want_score,
        )
        if min_tier_raw is not None:
            allowed = {"A", "B"} if min_tier_raw == "B" else {"A"}
            nodes = [
                n
                for n in nodes
                if tier(
                    n,
                    min_probes=settings.stability_min_probes,
                    min_avail=settings.stability_min_avail,
                )
                in allowed
            ]
        if want_score:
            nodes = score_nodes(
                nodes,
                min_probes=settings.stability_min_probes,
                min_avail=settings.stability_min_avail,
                weight_avail=settings.stability_weight_avail,
                weight_speed=settings.stability_weight_speed,
            )
        in_use_ids = store.node_ids_in_use()
        percentiles = (
            speed_percentile_map(nodes) if settings.stability_enabled else None
        )
        return jsonify(
            {
                "nodes": [
                    _serialize_node(n, n.node_id in in_use_ids, settings, percentiles)
                    for n in nodes
                ],
                "count": len(nodes),
                "limit": limit,
            }
        )

    @app.get("/nodes/<node_id>")
    def node_detail(node_id: str):
        node = store.get_node(node_id)
        if node is None:
            return err("not_found", f"no node {node_id}", 404)
        if settings.stability_enabled:
            # Percentile context: the node's own protocol's alive population.
            cohort = store.load_nodes(state="alive", protocols={node.protocol})
            percentiles = speed_percentile_map(cohort)
        else:
            percentiles = None
        detail = _serialize_node(
            node, node.node_id in store.node_ids_in_use(), settings, percentiles
        )
        detail["uri"] = node.uri
        return jsonify(detail)

    @app.post("/sources/<source_name>/refresh")
    def refresh_source(source_name: str):
        try:
            queued = engine.request_source_refresh(source_name)
        except Exception as exc:  # noqa: BLE001 - surface schedule failures to the operator
            return err("source_refresh_failed", str(exc), 502)
        if not queued:
            return err("not_found", f"no source {source_name}", 404)
        status = engine.engine_status()
        entry = next(
            (s for s in status["sources"] if s["name"] == source_name),
            {"name": source_name, "last_fetch_s": None, "cadence_s": None},
        )
        return jsonify({**entry, "refreshing": True}), 202

    @app.get("/tunnels")
    def list_tunnels():
        tunnels = _serialize_tunnels(store, engine, settings)
        return jsonify({"tunnels": tunnels})

    @app.post("/tunnels")
    def create_tunnel():
        body = request.get_json(silent=True) or {}
        try:
            requested = int(body.get("node_count", 10))
        except (TypeError, ValueError):
            return err("invalid_request", "node_count must be an integer", 400)
        auto_renew = bool(body.get("auto_renew", True))
        if requested < 1:
            return err("invalid_request", "node_count must be >= 1", 400)
        try:
            tunnel, nodes = assigner.create_tunnel_config(
                store, requested, auto_renew, protocols=set(ASSIGNABLE_PROTOCOLS)
            )
        except PortExhausted as exc:
            return err("port_exhausted", str(exc), 503)
        try:
            config = render_config(
                tunnel, nodes, urltest_interval_s=settings.urltest_interval_s
            )
        except ValueError as exc:
            # A node set that sing-box cannot represent must never strand a
            # half-created tunnel: release and roll it back, then report why.
            assigner.release_tunnel(store, tunnel.tunnel_id)
            store.delete_tunnel(tunnel.tunnel_id)
            return err("invalid_nodes", str(exc), 502)
        try:
            controller.start(tunnel, config)
        except TunnelRuntimeUnavailable as exc:
            assigner.release_tunnel(store, tunnel.tunnel_id)
            store.delete_tunnel(tunnel.tunnel_id)
            return err("tunnel_runtime", str(exc), 503)
        tunnel.state = "running"
        store.save_tunnel(tunnel)
        engine._rendered[tunnel.tunnel_id] = (
            tuple(sorted(n.node_id for n in nodes)),
            config,
        )
        return jsonify(_serialize_tunnel(tunnel, store, settings)), 201

    @app.post("/tunnels/<tunnel_id>/renew")
    def renew_tunnel(tunnel_id: str):
        tunnel = store.get_tunnel(tunnel_id)
        if tunnel is None:
            return err("not_found", f"no tunnel {tunnel_id}", 404)
        swapped, added = engine.health_check(tunnel)
        fresh = store.get_tunnel(tunnel_id)
        return jsonify(
            {
                "id": tunnel_id,
                "swapped": swapped,
                "added": added,
                "node_count_granted": fresh.node_count_granted if fresh else 0,
                "next_check_s": settings.health_interval_s,
            }
        )

    @app.delete("/tunnels/<tunnel_id>")
    def delete_tunnel(tunnel_id: str):
        tunnel = store.get_tunnel(tunnel_id)
        if tunnel is None:
            return err("not_found", f"no tunnel {tunnel_id}", 404)
        try:
            controller.stop(tunnel_id)
        except TunnelRuntimeUnavailable:
            logger.warning(
                "docker unreachable; deleting tunnel %s without stopping it", tunnel_id
            )
        assigner.release_tunnel(store, tunnel_id)
        store.delete_tunnel(tunnel_id)
        engine._strikes.pop(tunnel_id, None)
        engine._rendered.pop(tunnel_id, None)
        engine._last_check.pop(tunnel_id, None)
        return ("", 204)

    @app.get("/")
    def dashboard():
        return redirect(settings.panel_base_url, code=302)

    @app.errorhandler(Exception)
    def on_error(exc: Exception):  # type: ignore[no-untyped-def]
        logger.exception("unhandled error")
        return err("internal", "unexpected engine failure", 500)

    if background:
        engine.start()
    app.engine = engine  # type: ignore[attr-defined]
    return app


def _serialize_tunnel(
    tunnel: Tunnel, store: Store, settings: Settings | None = None
) -> dict[str, Any]:
    assigned = [
        node
        for node_id in sorted(store.node_ids_assigned_to(tunnel.tunnel_id))
        if (node := store.get_node(node_id)) is not None
    ]
    percentiles = (
        speed_percentile_map(assigned)
        if settings and settings.stability_enabled
        else None
    )
    node_rows = []
    for node in assigned:
        node_rows.append(_serialize_tunnel_node(node, settings, percentiles))
    return {
        "id": tunnel.tunnel_id,
        "host": "127.0.0.1",
        "port": tunnel.port,
        "username": tunnel.username,
        "password": tunnel.password,
        "protocols": TUNNEL_PROTOCOLS,
        "node_count_requested": tunnel.node_count_requested,
        "node_count_granted": tunnel.node_count_granted,
        "auto_renew": tunnel.auto_renew,
        "state": tunnel.state,
        "degraded": tunnel.degraded,
        "nodes": node_rows,
        "created_at": _iso(tunnel.created_at_s),
    }


def _serialize_tunnels(
    store: Store, engine: Engine, settings: Settings | None = None
) -> list[dict[str, Any]]:
    out = []
    for tunnel in store.load_tunnels():
        item = _serialize_tunnel(tunnel, store, settings)
        item["health"] = engine.tunnel_health(tunnel)
        out.append(item)
    return out


def _iso(timestamp: float) -> str:
    return (
        _dt.datetime.fromtimestamp(timestamp, tz=_dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _serialize_node(
    node: Node,
    in_use: bool,
    settings: Settings | None = None,
    percentile_map: dict[str, float] | None = None,
) -> dict[str, Any]:
    enabled = settings is not None and settings.stability_enabled
    return {
        "id": node.node_id,
        "protocol": node.protocol,
        "server": node.server,
        "port": node.port,
        "source": node.source,
        "state": node.state,
        "last_latency_ms": node.last_latency_ms,
        "throughput_kb_s": node.throughput_kb_s,
        "in_use": in_use,
        "first_seen_s": _iso(node.first_seen_s),
        "probe_total": node.probe_total,
        "availability": availability(node),
        "tier": (
            tier(
                node,
                min_probes=settings.stability_min_probes,
                min_avail=settings.stability_min_avail,
            )
            if enabled
            else None
        ),
        "score": (
            composite_score(
                node,
                percentile_map or {},
                min_probes=settings.stability_min_probes,
                weight_avail=settings.stability_weight_avail,
                weight_speed=settings.stability_weight_speed,
            )
            if enabled
            else None
        ),
    }


def _serialize_tunnel_node(
    node: Node, settings: Settings | None, percentile_map: dict[str, float]
) -> dict[str, Any]:
    base = _serialize_node(node, True, settings, percentile_map)
    return {
        "id": base["id"],
        "protocol": base["protocol"],
        "latency_ms": base["last_latency_ms"],
        "throughput_kb_s": base["throughput_kb_s"],
        "availability": base["availability"],
        "probe_total": base["probe_total"],
        "tier": base["tier"],
        "score": base["score"],
    }
