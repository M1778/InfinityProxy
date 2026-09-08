from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

import requests
from flask import Flask, Response, jsonify, request

from panel.client import EngineClient, EngineError
from panel.config import PanelSettings
from panel.history import SnapshotHistory

DEFAULT_POLL_S = 5


def _metrics_from(status: dict[str, Any]) -> dict[str, float]:
    pool = status.get("pool", {})
    tunnels = status.get("tunnels", {})
    return {
        "pool_total": pool.get("total", 0),
        "pool_alive": pool.get("alive", 0),
        "pool_dead": pool.get("dead", 0),
        "pool_untested": pool.get("untested", 0),
        "pool_in_use": pool.get("in_use", 0),
        "pool_assignable": pool.get("assignable", 0),
        "tunnel_active": tunnels.get("active", 0),
        "tunnel_degraded": tunnels.get("degraded", 0),
    }


def _default_dial(tunnel: dict[str, Any], test_url: str) -> dict[str, Any]:
    """Prove egress by fetching test_url through the tunnel's proxy."""
    proxy = (
        f"http://{tunnel['username']}:{tunnel['password']}@127.0.0.1:{tunnel['port']}"
    )
    try:
        res = requests.get(
            test_url, proxies={"http": proxy, "https": proxy}, timeout=(3.05, 15)
        )
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc), "latency_ms": None}
    return {
        "ok": res.status_code < 400,
        "exit_ip": res.text.strip(),
        "latency_ms": round(res.elapsed.total_seconds() * 1000, 1),
        "http_status": res.status_code,
    }


def create_app(
    settings: PanelSettings,
    engine: EngineClient | None = None,
    dial: Callable[[dict[str, Any], str], dict[str, Any]] | None = None,
    poll: bool = True,
) -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="")
    client = engine or EngineClient(settings.engine_url)
    dial_fn = dial or _default_dial
    history = SnapshotHistory(window_s=settings.history_seconds)
    state: dict[str, Any] = {
        "t": 0.0,
        "status": {},
        "tunnels": [],
        "seq": 0,
        "reachable": False,
        "error": None,
    }
    cond = threading.Condition()

    def crawl_once() -> None:
        try:
            status = client.status()
            tunnels = client.tunnels()
        except EngineError as exc:
            with cond:
                state["reachable"] = False
                state["error"] = {"code": exc.code, "message": exc.message}
                state["seq"] += 1
                cond.notify_all()
            return
        with cond:
            state["status"] = status
            state["tunnels"] = tunnels.get("tunnels", [])
            state["t"] = time.time()
            state["seq"] += 1
            state["reachable"] = True
            state["error"] = None
            cond.notify_all()
        history.add(time.time(), _metrics_from(status))

    def poll_forever() -> None:
        while True:
            crawl_once()
            time.sleep(settings.poll_interval_s)

    if poll:
        threading.Thread(target=poll_forever, name="panel-crawl", daemon=True).start()

    def snapshot() -> dict[str, Any]:
        with cond:
            return {
                "t": state["t"],
                "reachable": state["reachable"],
                "error": state["error"],
                "status": state["status"],
                "tunnels": state["tunnels"],
                "config": {
                    "engine_url": settings.engine_url,
                    "panel_host": settings.host,
                    "panel_port": settings.port,
                    "test_url": settings.test_url,
                },
            }

    def proxy(action: Callable[[], Any]) -> Any:
        try:
            result = action()
        except EngineError as exc:
            return (
                jsonify({"error": {"code": exc.code, "message": exc.message}}),
                exc.status or 502,
            )
        if result is None:
            return "", 204
        return jsonify(result)

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/snapshot")
    def api_snapshot():
        return jsonify(snapshot())

    @app.get("/api/history")
    def api_history():
        return jsonify(history.as_columns())

    @app.get("/api/events")
    def api_events():
        def stream():
            last_seq = -1
            last_beat = time.monotonic()
            while True:
                snap = None
                with cond:
                    if state["seq"] != last_seq:
                        last_seq = state["seq"]
                        snap = {
                            k: state[k]
                            for k in ("t", "status", "tunnels", "reachable", "error")
                        }
                if snap is not None:
                    last_beat = time.monotonic()
                    yield f"event: snapshot\ndata: {json.dumps(snap)}\n\n"
                elif time.monotonic() - last_beat >= settings.sse_heartbeat_s:
                    last_beat = time.monotonic()
                    yield "event: heartbeat\ndata: ping\n\n"
                else:
                    time.sleep(0.2)

        return Response(
            stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/tunnels")
    def api_tunnels():
        return proxy(client.tunnels)

    @app.post("/api/tunnels")
    def api_create_tunnel():
        body = request.get_json(silent=True) or {}
        try:
            node_count = int(body.get("node_count", 10))
        except (TypeError, ValueError):
            return (
                jsonify(
                    {
                        "error": {
                            "code": "invalid_request",
                            "message": "node_count must be an integer",
                        }
                    }
                ),
                400,
            )
        auto_renew = bool(body.get("auto_renew", False))
        return proxy(
            lambda: client.create_tunnel(node_count=node_count, auto_renew=auto_renew)
        )

    @app.post("/api/tunnels/<tunnel_id>/renew")
    def api_renew(tunnel_id: str):
        return proxy(lambda: client.renew_tunnel(tunnel_id))

    @app.delete("/api/tunnels/<tunnel_id>")
    def api_delete(tunnel_id: str):
        def act():
            client.delete_tunnel(tunnel_id)
            return {"ok": True, "id": tunnel_id}

        return proxy(act)

    @app.get("/api/nodes")
    def api_nodes():
        params = {k: v for k, v in request.args.items(True) if v}
        return proxy(lambda: client.nodes(params))

    @app.get("/api/nodes/<node_id>")
    def api_node(node_id: str):
        return proxy(lambda: client.node(node_id))

    @app.post("/api/sources/<source_name>/refresh")
    def api_refresh_source(source_name: str):
        return proxy(lambda: client.refresh_source(source_name))

    @app.post("/api/tunnels/<tunnel_id>/test")
    def api_test_tunnel(tunnel_id: str):
        try:
            tunnels = client.tunnels().get("tunnels", [])
        except EngineError as exc:
            return (
                jsonify({"error": {"code": exc.code, "message": exc.message}}),
                exc.status or 502,
            )
        tunnel = next((t for t in tunnels if t["id"] == tunnel_id), None)
        if tunnel is None:
            return jsonify(
                {"error": {"code": "not_found", "message": f"no tunnel {tunnel_id}"}}
            ), 404
        return jsonify({"ok": True, **dial_fn(tunnel, settings.test_url)})

    app.crawl_once = crawl_once  # type: ignore[attr-defined]
    app.panel_client = client  # type: ignore[attr-defined]
    app.panel_history = history  # type: ignore[attr-defined]
    app.panel_state = state  # type: ignore[attr-defined]
    return app
