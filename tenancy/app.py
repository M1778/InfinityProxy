"""Flask control center for the multi-tenant deployment (ADR-0012).

Three key scopes, one per audience:
- operator `X-Operator-Key` — the tenant API key issued at registration;
  client_id is **only** the key, no headers or IPs are trusted.
- admin `X-Admin-Key` (`INFINITY_TENANCY_ADMIN_KEY`), issues keys and lifts
  runtime caps; a board without an admin key accepts registrations nobody can
  review, so it's required before any key is issued.
- visitor `/v1/public/*` — the unauthenticated public metrics surface.

A tenant tunnel's SOCKS/HTTP listener is the engine's container on the Docker
host, not the engine's loopback API; client authentication is the tunnel's own
username/password, checked by sing-box. The panel's proxy-client table enforces
the same rule for metrics.
"""

from __future__ import annotations

import logging
import math
import secrets
import sqlite3
import time
from typing import Any

from flask import Flask, jsonify, request

from tenancy import billing
from tenancy.config import TenancySettings
from tenancy.engine import EngineEdge, EngineSideError, read_pool, shaped_tunnels
from tenancy.models import Tenant
from tenancy.store import TenantsStore

PROXY_CLIENT_COLUMNS = (
    "ts",
    "ip",
    "user_agent",
    "connection_id",
    "target",
    "key_id",
)


def _now() -> float:
    return time.time()


def _iso(ts: float | None) -> str | None:
    import datetime as _dt

    if ts is None:
        return None
    return (
        _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def create_app(
    settings: TenancySettings | None = None,
    store: TenantsStore | None = None,
    engine: EngineEdge | None = None,
    proxy_clients: dict[tuple[str, str], str] | None = None,
) -> Flask:
    settings = settings or TenancySettings.from_env()
    store = store or TenantsStore(settings.db_path)
    store.create_schema()
    engine = engine or EngineEdge(settings.engine_url)

    # Per-connection client identity. Local panel/dev processes seed items, a
    # far-end tenant never can — the map is board state, not request data.
    known_clients: dict[tuple[str, str], str] = proxy_clients if proxy_clients else {}
    logger = logging.getLogger("infinityproxy-control-center")

    app = Flask("infinityproxy-control-center")
    app.json.ensure_ascii = False

    def err(code: str, message: str, status: int):
        return jsonify({"error": {"code": code, "message": message}}), status

    def tenant_keys(tenant: Tenant) -> dict[str, Any]:
        return {
            "tenant_id": tenant.tenant_id,
            "tenant_key": tenant.tenant_key,
            "title": tenant.title,
            "email": tenant.email,
            "state": tenant.state,
            "node_count_requested": tenant.node_count_requested,
            "created_at": _iso(tenant.created_at_s),
            "credits": round(tenant.credits, 4),
            "credited_at": _iso(tenant.credited_s),
        }

    def tenant_public(tenant: Tenant, leases: list[dict[str, Any]]) -> dict[str, Any]:
        row = tenant_keys(tenant)
        row["leases"] = leases
        return row

    def metering_enabled() -> bool:
        return settings.meter_enabled and settings.credit_interval_s > 0

    def auth_tenant() -> tuple[Tenant | None, dict[str, Any] | None]:
        header = request.headers.get("X-Operator-Key", "")
        if not header:
            return None, err("missing_key", "provide X-Operator-Key", 401)
        tenant = store.get_by_key(header)
        if tenant is None:
            return None, err("invalid_key", "unknown operator key", 401)
        if tenant.state != "active":
            return None, err("disabled", "operator key disabled", 403)
        if metering_enabled():
            billing.drain_elapsed(
                store, tenant, settings.credit_interval_s, keep=settings.ledger_keep
            )
        # Revalidate after the meter transaction: disable/rotate can race lookup.
        tenant = store.get_by_key(header)
        if tenant is None:
            return None, err("invalid_key", "unknown operator key", 401)
        if tenant.state != "active":
            return None, err("disabled", "operator key disabled", 403)
        return tenant, None

    def auth_admin() -> dict[str, Any] | None:
        if not settings.admin_key:
            return err(
                "admin_not_configured",
                "set INFINITY_TENANCY_ADMIN_KEY before issuing keys",
                409,
            )
        if request.headers.get("X-Admin-Key", "") != settings.admin_key:
            return err("invalid_key", "admin key rejected", 401)
        return None

    def proxy_client_row(entry: tuple[str, str]) -> dict[str, Any]:
        ip, conn = entry
        return {"ip": ip, "connection_id": conn, "key_id": known_clients[entry]}

    def shaped_for_tenant(payload: Any, scope: set[str]) -> list[dict[str, str]]:
        shaped = shaped_tunnels(payload, scope)
        if settings.public_tunnel_host:
            shaped = [{**row, "host": settings.public_tunnel_host} for row in shaped]
        return shaped

    def tunnels_for_tenant(tenant_id: str) -> list[dict[str, Any]]:
        """The tenant's leases, projected without credentials (ADR-0012).

        Scoped by the stored tenant→tunnel mapping — rows the tenant's
        principal provisioned. Tombstones (rows the engine no longer returns)
        are unlinked on read so the scope stays truthful without a reconciler.
        """
        scope = set(store.tunnels_of(tenant_id))
        if not scope:
            return []
        payload = engine.tunnels()
        shaped = shaped_for_tenant(payload, scope)
        for tunnel_id in scope - {row["id"] for row in shaped}:
            store.unlink_tunnel(tenant_id, tunnel_id)
        return shaped

    @app.get("/health")
    def health():
        return jsonify({"engine": "tenancy-ok", "uptime_s": int(_now())})

    # ------------------------------------------------------------------
    # Visitor surface
    # ------------------------------------------------------------------

    @app.get("/v1/public/pool")
    def public_pool():
        try:
            return jsonify(read_pool(engine.status()))
        except EngineSideError as exc:
            return err(exc.code, exc.message, 502)

    @app.get("/v1/public/tunnels")
    def public_tunnels():
        """Running leases, endpoint-only: no credentials leave the board."""
        rows: list[dict[str, Any]] = []
        for tenant in store.load_tenants():
            if tenant.state != "active":
                continue
            try:
                leases = tunnels_for_tenant(tenant.tenant_id)
            except EngineSideError:
                continue
            rows.append(
                {
                    "title": tenant.title,
                    "tunnels": [
                        {"id": t["id"], "port": t["port"], "state": t["state"]}
                        for t in leases
                    ],
                }
            )
        return jsonify({"tunnels": rows})

    @app.get("/v1/public/stats")
    def public_stats():
        """Read-only rollup. Metering never runs here: this route is
        unauthenticated, so per-GET writes would let anyone churn the ledger."""
        return jsonify(
            {
                "tenants": store.active_count(),
                "credits_outstanding": round(store.total_credits(), 4),
                "metered_since_s": settings.credit_interval_s,
            }
        )

    # ------------------------------------------------------------------
    # Tenant surface
    # ------------------------------------------------------------------

    @app.post("/v1/tenants/register")
    def register():
        body = request.get_json(silent=True) or {}
        title = str(body.get("title") or "").strip()
        email = str(body.get("email") or "").strip().lower()
        node_count = body.get("node_count", 4)
        if not title:
            return err("invalid_request", "signup form requires a title", 400)
        if not email or "@" not in email:
            return err("invalid_request", "signup form requires an email", 400)
        if store.tenant_count() >= settings.max_tenants:
            return err("board_full", "operator seats exhausted", 503)
        try:
            node_count = int(node_count)
        except (TypeError, ValueError):
            return err("invalid_request", "node_count must be an integer", 400)
        node_count = max(1, min(settings.max_nodes_per_tunnel, node_count))
        tenant = Tenant.create(
            title=title, email=email, node_count_requested=node_count
        )
        store.save_tenant(tenant)
        billing.grant_starting(
            store, tenant, settings.starting_credits, _now(), keep=settings.ledger_keep
        )
        return jsonify(tenant_keys(tenant)), 201

    @app.get("/v1/me")
    def me():
        tenant, failure = auth_tenant()
        if failure is not None:
            return failure
        try:
            leases = tunnels_for_tenant(tenant.tenant_id)
        except EngineSideError as exc:
            return err(exc.code, exc.message, 502)
        return jsonify({"me": tenant_public(tenant, leases)})

    @app.get("/v1/me/ledger")
    def me_ledger():
        tenant, failure = auth_tenant()
        if failure is not None:
            return failure
        return jsonify(
            {
                "ledger": [
                    entry.__dict__
                    for entry in store.ledger(
                        tenant.tenant_id, keep=settings.ledger_keep
                    )
                ]
            }
        )

    @app.post("/v1/me/tunnels")
    def create_tenant_tunnel():
        tenant, failure = auth_tenant()
        if failure is not None:
            return failure
        body = request.get_json(silent=True) or {}
        try:
            node_count = int(body.get("node_count", tenant.node_count_requested))
        except (TypeError, ValueError):
            return err("invalid_request", "node_count must be an integer", 400)
        node_count = max(1, min(settings.max_nodes_per_tunnel, node_count))
        if metering_enabled() and tenant.credits < 1.0:
            return err("out_of_credits", "operator credit balance exhausted", 402)
        try:
            tunnels_for_tenant(tenant.tenant_id)
        except EngineSideError as exc:
            return err(exc.code, exc.message, 502)
        reservation = store.reserve_tunnel(
            tenant.tenant_id,
            settings.max_tunnels_per_tenant,
            min_credits=1.0 if metering_enabled() else 0.0,
        )
        if reservation is None:
            fresh = store.get_tenant(tenant.tenant_id)
            if fresh is None or fresh.state != "active":
                return err("disabled", "operator key disabled", 403)
            if metering_enabled() and fresh.credits < 1.0:
                return err("out_of_credits", "operator credit balance exhausted", 402)
            return err("lease_limit", "tunnel cap reached", 409)
        release_slot = True
        tunnel_id = None
        try:
            # Timeout/malformed success can hide a completed engine creation.
            # Keep uncertain slots counted until the host reconciles them.
            release_slot = False
            payload = engine.create_tunnel(node_count, auto_renew=True)
            tunnel_id = payload.get("id") if isinstance(payload, dict) else None
            if not tunnel_id:
                return err("engine_error", "engine returned no tunnel row", 502)
            tunnel_id = str(tunnel_id)
            store.note_reservation_tunnel(reservation, tunnel_id)
            active = store.complete_reservation(
                tenant.tenant_id, reservation, tunnel_id
            )
            release_slot = True
            if not active:
                try:
                    engine.delete_tunnel(tunnel_id)
                    store.unlink_tunnel(tenant.tenant_id, tunnel_id)
                except EngineSideError:
                    logger.warning("disabled tenant cleanup pending for %s", tunnel_id)
                return err("disabled", "operator disabled during provisioning", 403)
            shaped = shaped_for_tenant({"tunnels": [payload]}, {tunnel_id})
            return jsonify(shaped[0]), 201
        except EngineSideError as exc:
            release_slot = 400 <= exc.status < 500
            return err(exc.code, exc.message, 502)
        except sqlite3.Error:
            logger.exception("lease finalization failed for %s", tunnel_id)
            if tunnel_id:
                try:
                    engine.delete_tunnel(tunnel_id)
                    release_slot = True
                except EngineSideError as exc:
                    release_slot = exc.status == 404
            return err(
                "lease_finalize_failed", "lease ownership could not be saved", 502
            )
        finally:
            if release_slot:
                store.release_reservation(reservation)

    @app.delete("/v1/me/tunnels/<tunnel_id>")
    def delete_tenant_tunnel(tunnel_id: str):
        tenant, failure = auth_tenant()
        if failure is not None:
            return failure
        try:
            leases = tunnels_for_tenant(tenant.tenant_id)
        except EngineSideError as exc:
            return err(exc.code, exc.message, 502)
        if tunnel_id not in {t["id"] for t in leases}:
            return err("not_found", f"no tunnel {tunnel_id} for this operator", 404)
        try:
            engine.delete_tunnel(tunnel_id)
        except EngineSideError as exc:
            return err(exc.code, exc.message, 502)
        store.unlink_tunnel(tenant.tenant_id, tunnel_id)
        return ("", 204)

    @app.post("/v1/me/tunnels/<tunnel_id>/renew")
    def renew_tenant_tunnel(tunnel_id: str):
        tenant, failure = auth_tenant()
        if failure is not None:
            return failure
        try:
            leases = tunnels_for_tenant(tenant.tenant_id)
        except EngineSideError as exc:
            return err(exc.code, exc.message, 502)
        if tunnel_id not in {t["id"] for t in leases}:
            return err("not_found", f"no tunnel {tunnel_id} for this operator", 404)
        try:
            payload = engine.renew_tunnel(tunnel_id)
        except EngineSideError as exc:
            return err(exc.code, exc.message, 502)
        return jsonify(payload)

    @app.get("/v1/me/proxy-clients")
    def proxy_clients():
        tenant, failure = auth_tenant()
        if failure is not None:
            return failure
        rows = [
            proxy_client_row(entry)
            for entry in known_clients
            if known_clients[entry] == tenant.tenant_id
        ]
        return jsonify({"proxy_clients": rows[:100]})

    # ------------------------------------------------------------------
    # Admin surface
    # ------------------------------------------------------------------

    @app.get("/v1/admin/tenants")
    def admin_tenants():
        failure = auth_admin()
        if failure is not None:
            return failure
        return jsonify({"tenants": [tenant_keys(t) for t in store.load_tenants()]})

    @app.post("/v1/admin/tenants")
    def admin_create():
        failure = auth_admin()
        if failure is not None:
            return failure
        body = request.get_json(silent=True) or {}
        title = str(body.get("title") or "").strip()
        if not title:
            return err("invalid_request", "title required", 400)
        tenant = Tenant.create(
            title=title,
            email=str(body.get("email") or "").strip().lower(),
            node_count_requested=4,
        )
        store.save_tenant(tenant)
        billing.grant_starting(
            store, tenant, settings.starting_credits, _now(), keep=settings.ledger_keep
        )
        return jsonify(tenant_keys(tenant)), 201

    @app.post("/v1/admin/tenants/<tenant_id>/grant")
    def admin_grant(tenant_id: str):
        failure = auth_admin()
        if failure is not None:
            return failure
        tenant = store.get_tenant(tenant_id)
        if tenant is None:
            return err("not_found", f"no tenant {tenant_id}", 404)
        body = request.get_json(silent=True) or {}
        try:
            amount = float(body.get("amount", 0))
        except (TypeError, ValueError):
            return err("invalid_request", "amount must be a number", 400)
        if not math.isfinite(amount) or amount <= 0:
            return err("invalid_request", "amount must be positive and finite", 400)
        try:
            store.grant(tenant_id, amount, _now(), keep=settings.ledger_keep)
        except ValueError as exc:
            return err("invalid_request", str(exc), 400)
        return jsonify({"tenant": tenant_keys(store.get_tenant(tenant_id))}), 200

    @app.post("/v1/admin/tenants/<tenant_id>/rotate")
    def admin_rotate(tenant_id: str):
        failure = auth_admin()
        if failure is not None:
            return failure
        tenant = store.get_tenant(tenant_id)
        if tenant is None:
            return err("not_found", f"no tenant {tenant_id}", 404)
        store.set_key(tenant_id, f"user_{secrets.token_urlsafe(18)}", _now())
        return jsonify({"tenant": tenant_keys(store.get_tenant(tenant_id))}), 200

    @app.post("/v1/admin/tenants/<tenant_id>/disable")
    def admin_disable(tenant_id: str):
        failure = auth_admin()
        if failure is not None:
            return failure
        tenant = store.get_tenant(tenant_id)
        if tenant is None:
            return err("not_found", f"no tenant {tenant_id}", 404)
        store.disable(tenant_id, _now())
        # Delete from the durable ownership mapping, not an engine listing that
        # may fail. Keep failed ids linked so repeating disable retries them.
        pending: list[str] = []
        for tunnel_id in store.tunnels_of(tenant_id):
            try:
                engine.delete_tunnel(tunnel_id)
            except EngineSideError as exc:
                if exc.status != 404:
                    pending.append(tunnel_id)
                    continue
            store.unlink_tunnel(tenant_id, tunnel_id)
        return jsonify(
            {
                "tenant": tenant_keys(store.get_tenant(tenant_id)),
                "cleanup_pending": pending,
            }
        ), 200

    @app.get("/v1/admin/proxy-clients")
    def admin_proxy_clients():
        failure = auth_admin()
        if failure is not None:
            return failure
        return jsonify(
            {"proxy_clients": [proxy_client_row(e) for e in sorted(known_clients)]}
        )

    @app.get("/")
    def index():
        return jsonify(
            {
                "service": "infinityproxy-control-center",
                "title": settings.tenant_page_title,
                "tenants": "/v1/tenants/register",
                "operator": "/v1/me",
                "admin": "/v1/admin/tenants",
                "public": "/v1/public/pool",
                "stats": "/v1/public/stats",
            }
        )

    @app.errorhandler(Exception)
    def on_error(exc: Exception):  # type: ignore[no-untyped-def]
        app.logger.exception("unhandled error")
        return err("internal", "unexpected control center failure", 500)

    app.tenants = store  # type: ignore[attr-defined]
    app.engine_edge = engine  # type: ignore[attr-defined]
    app.tenancy_settings = settings  # type: ignore[attr-defined]
    app.proxy_clients = known_clients  # type: ignore[attr-defined]
    return app
