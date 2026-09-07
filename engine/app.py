"""Flask control API + read-only dashboard (docs/api.md, ADR-0003)."""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from flask import Flask, jsonify, request

from engine.assigner import Assigner, PortExhausted
from engine.config import Settings
from engine.db import Store
from engine.models import Tunnel
from engine.scheduler import ASSIGNABLE_PROTOCOLS, Engine
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

    @app.get("/tunnels")
    def list_tunnels():
        tunnels = _serialize_tunnels(store, engine)
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
        return jsonify(_serialize_tunnel(tunnel, store)), 201

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
        return _render_dashboard(store, engine, settings)

    @app.errorhandler(Exception)
    def on_error(exc: Exception):  # type: ignore[no-untyped-def]
        logger.exception("unhandled error")
        return err("internal", "unexpected engine failure", 500)

    if background:
        engine.start()
    app.engine = engine  # type: ignore[attr-defined]
    return app


def _serialize_tunnel(tunnel: Tunnel, store: Store) -> dict[str, Any]:
    node_rows = []
    for node_id in sorted(store.node_ids_assigned_to(tunnel.tunnel_id)):
        node = store.get_node(node_id)
        if node is None:
            continue
        node_rows.append(
            {
                "id": node.node_id,
                "protocol": node.protocol,
                "latency_ms": node.last_latency_ms,
            }
        )
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


def _serialize_tunnels(store: Store, engine: Engine) -> list[dict[str, Any]]:
    out = []
    for tunnel in store.load_tunnels():
        item = _serialize_tunnel(tunnel, store)
        item["health"] = engine.tunnel_health(tunnel)
        out.append(item)
    return out


def _iso(timestamp: float) -> str:
    return (
        _dt.datetime.fromtimestamp(timestamp, tz=_dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _render_dashboard(store: Store, engine: Engine, settings: Settings) -> str:
    status = engine.engine_status()
    tunnels = "\n".join(
        f"<tr><td>{t.tunnel_id}</td><td>127.0.0.1:{t.port}</td>"
        f"<td>{'yes' if t.auto_renew else 'no'}</td>"
        f"<td>{t.node_count_granted}/{t.node_count_requested}</td>"
        f"<td>{t.state}</td>"
        f"<td>{'degraded' if t.degraded else 'ok'}</td></tr>"
        for t in store.load_tunnels()
    )
    sources = "\n".join(
        f"<tr><td>{s['name']}</td><td>{s['cadence_s']}s</td>"
        f"<td>{s['last_fetch_s']}s ago</td></tr>"
        for s in status["sources"]
    )
    pool = status["pool"]
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>InfinityProxy</title><style>"
        "body{font:14px/1.5 system-ui,PingFang SC,Microsoft YaHei,sans-serif;"
        "margin:2rem auto;max-width:64rem;padding:0 1rem;color:#1a1a1a}"
        "h1{font-size:1.4rem}table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #ddd;padding:.4rem .6rem;text-align:left}"
        "th{background:#f5f5f5}.badge{color:#b45309;font-weight:600}</style></head><body>"
        "<h1>InfinityProxy</h1>"
        f"<p>Engine {status['engine']} · up {status['uptime_s']}s. "
        f"Control API: 127.0.0.1:{settings.port} (localhost only, no auth).</p>"
        "<h2>Pool</h2>"
        f"<p>total {pool['total']} · alive {pool['alive']} · "
        f"dead {pool['dead']} · in use {pool['in_use']} · "
        f"assignable {pool['assignable']}</p>"
        "<h2>Tunnels</h2>"
        f"<table><tr><th>id</th><th>endpoint</th><th>auto renew</th>"
        f"<th>granted/requested</th><th>state</th><th>status</th></tr>{tunnels}</table>"
        "<h2>Sources</h2>"
        f"<table><tr><th>name</th><th>cadence</th><th>last fetch</th>"
        f"</tr>{sources}</table>"
        "</body></html>"
    )
