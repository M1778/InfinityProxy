"""Control center tests (ADR-0011): fake engine edge, temp store, no network."""

from __future__ import annotations

import os
import tempfile
from typing import Any

from tenancy.app import create_app
from tenancy.config import TenancySettings
from tenancy.engine import EngineSideError, shaped_tunnels
from tenancy.store import TenantsStore


class FakeEdge:
    """Scriptable engine edge: records calls, plays canned responses."""

    def __init__(self, tunnels: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.tunnels_payload = {"tunnels": tunnels or []}
        self.fail = False

    def status(self) -> dict[str, Any]:
        self.calls.append(("GET", "/status"))
        if self.fail:
            raise EngineSideError(502, "engine_unreachable", "down")
        return {"pool": {"total": 500, "alive": 400, "dead": 10, "untested": 90}}

    def tunnels(self) -> dict[str, Any]:
        self.calls.append(("GET", "/tunnels"))
        if self.fail:
            raise EngineSideError(502, "engine_unreachable", "down")
        return self.tunnels_payload

    def create_tunnel(self, node_count: int, auto_renew: bool = True) -> dict[str, Any]:
        self.calls.append(("POST", "/tunnels"))
        if self.fail:
            raise EngineSideError(502, "engine_unreachable", "down")
        tunnel = {
            "id": f"tu_{len(self.tunnels_payload['tunnels']) + 1}",
            "host": "127.0.0.1",
            "port": 12000 + node_count,
            "username": "u_x",
            "password": "p_y",
            "state": "running",
        }
        self.tunnels_payload = {"tunnels": self.tunnels_payload["tunnels"] + [tunnel]}
        return tunnel

    def renew_tunnel(self, tunnel_id: str) -> dict[str, Any]:
        self.calls.append(("POST", f"/tunnels/{tunnel_id}/renew"))
        if self.fail:
            raise EngineSideError(502, "engine_unreachable", "down")
        return {"id": tunnel_id, "swapped": 0, "added": 0}

    def delete_tunnel(self, tunnel_id: str) -> None:
        self.calls.append(("DELETE", f"/tunnels/{tunnel_id}"))
        if self.fail:
            raise EngineSideError(502, "engine_unreachable", "down")
        self.tunnels_payload = {
            "tunnels": [
                t for t in self.tunnels_payload["tunnels"] if t["id"] != tunnel_id
            ]
        }

    def nodes(self, params: dict[str, str] | None = None) -> dict[str, Any]:
        return {"nodes": [], "count": 0}


def make_settings(**kw: Any) -> TenancySettings:
    return TenancySettings(
        admin_key="adm-secret",
        starting_credits=48,
        max_tunnels_per_tenant=2,
        meter_enabled=True,
        **kw,
    )


def fresh_store() -> TenantsStore:
    return TenantsStore(os.path.join(tempfile.mkdtemp(), "tenancy.db"))


def boot_app(**kw: Any) -> tuple[Any, FakeEdge, TenantsStore]:
    edge = FakeEdge()
    store = fresh_store()
    app = create_app(
        settings=make_settings(**kw), store=store, engine=edge, proxy_clients={}
    )
    app.testing = True
    return app.test_client(), edge, store


def register(client: Any, **body: Any) -> dict[str, Any]:
    res = client.post(
        "/v1/tenants/register",
        json={"title": "acme", "email": "ops@acme.co", **body},
    )
    assert res.status_code == 201
    return res.get_json()


def op_headers(tenant: dict[str, Any]) -> dict[str, str]:
    return {"X-Operator-Key": tenant["tenant_key"]}


# ---------------------------------------------------------------- auth


def test_health_and_public_pool_have_no_keys() -> None:
    client, edge, _ = boot_app()
    assert client.get("/health").status_code == 200
    res = client.get("/v1/public/pool")
    assert res.status_code == 200
    assert res.get_json()["alive"] == 400
    assert edge.calls == [("GET", "/status")]


def test_register_issues_key_and_grants_credits() -> None:
    client, _, _ = boot_app()
    tenant = register(client)
    assert tenant["state"] == "active"
    assert tenant["tenant_key"].startswith("user_")
    assert tenant["credits"] == 48


def test_register_rejects_missing_fields() -> None:
    client, _, _ = boot_app()
    res = client.post("/v1/tenants/register", json={"title": "acme"})
    assert res.status_code == 400
    res = client.post("/v1/tenants/register", json={"email": "a@b.co"})
    assert res.status_code == 400


def test_me_requires_key() -> None:
    client, _, _ = boot_app()
    assert client.get("/v1/me").status_code == 401
    res = client.get("/v1/me", headers={"X-Operator-Key": "nope"})
    assert res.status_code == 401
    assert res.get_json()["error"]["code"] == "invalid_key"


def test_me_scopes_leases_and_hides_credentials() -> None:
    client, edge, _ = boot_app()
    mine = register(client, node_count=6)
    other = register(client, title="rival", email="r@val.io")
    mine_res = client.post("/v1/me/tunnels", headers=op_headers(mine), json={})
    assert mine_res.status_code == 201
    mine_tunnel = mine_res.get_json()["id"]
    # A foreign tunnel the engine carries but this tenant never provisioned.
    edge.tunnels_payload["tunnels"] = edge.tunnels_payload["tunnels"] + [
        {
            "id": "tu_foreign",
            "host": "127.0.0.1",
            "port": 12002,
            "username": "u_2",
            "password": "p_2",
            "state": "running",
        }
    ]
    store = client.application.tenants
    store.link_tunnel(other["tenant_id"], "tu_foreign")
    res = client.get("/v1/me", headers=op_headers(mine))
    assert res.status_code == 200
    me = res.get_json()["me"]
    assert [t["id"] for t in me["leases"]] == [mine_tunnel]
    assert all("username" not in t and "password" not in t for t in me["leases"])


# ---------------------------------------------------------------- tunnels


def test_create_tunnel_within_cap() -> None:
    client, edge, _ = boot_app()
    tenant = register(client)
    first = client.post("/v1/me/tunnels", headers=op_headers(tenant), json={})
    assert first.status_code == 201
    second = client.post("/v1/me/tunnels", headers=op_headers(tenant), json={})
    assert second.status_code == 201
    cap_hit = client.post("/v1/me/tunnels", headers=op_headers(tenant), json={})
    assert cap_hit.status_code == 409
    assert cap_hit.get_json()["error"]["code"] == "lease_limit"


def test_create_tunnel_node_count_clamped() -> None:
    client, edge, _ = boot_app()
    tenant = register(client)
    res = client.post(
        "/v1/me/tunnels",
        headers=op_headers(tenant),
        json={"node_count": 500},
    )
    assert res.status_code == 201
    assert edge.calls[0] == ("POST", "/tunnels")


def test_delete_tunnel_rejects_foreign_id() -> None:
    client, edge, _ = boot_app()
    tenant = register(client)
    res = client.delete("/v1/me/tunnels/tu_foreign", headers=op_headers(tenant))
    assert res.status_code == 404
    assert ("DELETE", "/tunnels/tu_foreign") not in edge.calls


def test_delete_owned_tunnel_forwards() -> None:
    client, edge, _ = boot_app()
    tenant = register(client)
    created = client.post("/v1/me/tunnels", headers=op_headers(tenant), json={})
    tunnel_id = created.get_json()["id"]
    res = client.delete(f"/v1/me/tunnels/{tunnel_id}", headers=op_headers(tenant))
    assert res.status_code == 204
    assert ("DELETE", f"/tunnels/{tunnel_id}") in edge.calls
    store = client.application.tenants
    assert store.tunnels_of(tenant["tenant_id"]) == []


def test_renew_owned_tunnel_forwards() -> None:
    client, edge, _ = boot_app()
    tenant = register(client)
    created = client.post("/v1/me/tunnels", headers=op_headers(tenant), json={})
    tunnel_id = created.get_json()["id"]
    res = client.post(f"/v1/me/tunnels/{tunnel_id}/renew", headers=op_headers(tenant))
    assert res.status_code == 200
    assert res.get_json()["id"] == tunnel_id


def test_engine_down_surfaces_engine_error() -> None:
    client, edge, _ = boot_app()
    tenant = register(client)
    created = client.post("/v1/me/tunnels", headers=op_headers(tenant), json={})
    assert created.status_code == 201
    edge.fail = True
    res = client.get("/v1/me", headers=op_headers(tenant))
    assert res.status_code == 502
    assert res.get_json()["error"]["code"] == "engine_unreachable"


# ---------------------------------------------------------------- credits


def test_out_of_credits_blocks_provision() -> None:
    client, _, store = boot_app()
    tenant = register(client)
    tenant_id = tenant["tenant_id"]
    row = store.get_tenant(tenant_id)
    assert row is not None
    row.credits = 0.0
    store.save_tenant(row)
    res = client.post("/v1/me/tunnels", headers=op_headers(tenant), json={})
    assert res.status_code == 402
    assert res.get_json()["error"]["code"] == "out_of_credits"


def test_admin_grant_restocks_credits_and_passes_ledger() -> None:
    client, _, store = boot_app()
    tenant = register(client)
    tid = tenant["tenant_id"]
    row = store.get_tenant(tid)
    assert row is not None
    row.credits = 0.0
    store.save_tenant(row)
    res = client.post(
        f"/v1/admin/tenants/{tid}/grant",
        headers={"X-Admin-Key": "adm-secret"},
        json={"amount": 24},
    )
    assert res.status_code == 200
    refreshed = store.get_tenant(tid)
    assert refreshed is not None
    assert refreshed.credits == 24
    ledger = client.get("/v1/me/ledger", headers=op_headers(tenant)).get_json()[
        "ledger"
    ]
    reasons = [e["reason"] for e in ledger]
    assert "admin grant" in reasons and "signup grant" in reasons


def test_metered_drain_debits_per_elapsed_period() -> None:
    client, _, _ = boot_app(credit_interval_s=3600)
    tenant = register(client)
    tid = tenant["tenant_id"]

    # 90 minutes after signup: one full hour debited, boundary moved 60m.
    store = client.application.tenants
    row = store.get_tenant(tid)
    assert row is not None
    assert row.credited_s is not None
    start = row.credited_s
    from tenancy import billing

    debit = billing.drain_elapsed(store, row, 3600, start + 5400)
    assert debit == 1.0
    refreshed = store.get_tenant(tid)
    assert refreshed is not None
    assert refreshed.credits == 47


def test_disabled_key_is_rejected() -> None:
    client, _, store = boot_app()
    tenant = register(client)
    tid = tenant["tenant_id"]
    row = store.get_tenant(tid)
    assert row is not None
    row.state = "disabled"
    store.save_tenant(row)
    res = client.get("/v1/me", headers=op_headers(tenant))
    assert res.status_code == 403
    assert res.get_json()["error"]["code"] == "disabled"


# ---------------------------------------------------------------- admin


def test_admin_requires_configured_key() -> None:
    client, _, _ = boot_app()
    res = client.get("/v1/admin/tenants", headers={"X-Admin-Key": "adm-secret"})
    assert res.status_code == 200
    no_admin = create_app(
        settings=TenancySettings(admin_key=""),
        store=fresh_store(),
        engine=FakeEdge(),
        proxy_clients={},
    )
    no_admin.testing = True
    assert (
        no_admin.test_client()
        .get("/v1/admin/tenants", headers={"X-Admin-Key": "x"})
        .status_code
        == 409
    )
    assert no_admin.test_client().get("/v1/admin/tenants").status_code == 409


def test_admin_lists_and_disables_tenants() -> None:
    client, edge, _ = boot_app()
    mine = register(client)
    tid = mine["tenant_id"]
    created = client.post("/v1/me/tunnels", headers=op_headers(mine), json={})
    tunnel_id = created.get_json()["id"]
    res = client.post(
        f"/v1/admin/tenants/{tid}/disable",
        headers={"X-Admin-Key": "adm-secret"},
    )
    assert res.status_code == 200
    assert res.get_json()["tenant"]["state"] == "disabled"
    # Disabling tears the tenant's leases down on the engine too.
    assert ("DELETE", f"/tunnels/{tunnel_id}") in edge.calls
    res = client.get("/v1/me", headers=op_headers(mine))
    assert res.status_code == 403


def test_admin_rotate_key_breaks_old_key() -> None:
    client, _, store = boot_app()
    tenant = register(client)
    tid = tenant["tenant_id"]
    res = client.post(
        f"/v1/admin/tenants/{tid}/rotate",
        headers={"X-Admin-Key": "adm-secret"},
    )
    assert res.status_code == 200
    fresh_key = res.get_json()["tenant"]["tenant_key"]
    assert fresh_key != tenant["tenant_key"]
    assert client.get("/v1/me", headers=op_headers(tenant)).status_code == 401
    assert (
        client.get("/v1/me", headers={"X-Operator-Key": fresh_key}).status_code == 200
    )


def test_public_tunnels_list_active_tenants_only() -> None:
    client, edge, _ = boot_app()
    active = register(client)
    created = client.post("/v1/me/tunnels", headers=op_headers(active), json={})
    tunnel_id = created.get_json()["id"]
    # A disabled tenant's leases must not appear.
    from tenancy.models import Tenant as T

    store = client.application.tenants
    store.save_tenant(
        T(
            tenant_id="tnz",
            tenant_key="user_z",
            title="ghost",
            email="g@h.st",
            state="disabled",
        )
    )
    rows = client.get("/v1/public/tunnels").get_json()["tunnels"]
    assert [r["title"] for r in rows] == ["acme"]
    assert all("username" not in t and "password" not in t for t in rows[0]["tunnels"])
    assert rows[0]["tunnels"][0]["id"] == tunnel_id


# ---------------------------------------------------------------- shaping


def test_shaped_tunnels_projects_only_public_columns() -> None:
    payload = {
        "tunnels": [
            {
                "id": "a",
                "host": "127.0.0.1",
                "port": 12001,
                "state": "running",
                "username": "u",
                "password": "p",
                "degraded": True,
            },
            {"id": "b", "host": "127.0.0.1", "port": 12002, "state": "stopped"},
        ]
    }
    shaped = shaped_tunnels(payload, {"a"})
    assert shaped == [
        {"id": "a", "host": "127.0.0.1", "port": "12001", "state": "running"}
    ]


def test_read_pool_projects_public_fields() -> None:
    from tenancy.engine import read_pool

    shaped = read_pool(
        {
            "pool": {
                "total": 3,
                "alive": 2,
                "dead": 1,
                "untested": 0,
                "in_use": 1,
                "assignable": 1,
                "tier_a": 2,
                "tier_b": 0,
                "working_set": 256,
                "avg_score": 0.5,
                "by_protocol": [{"protocol": "ss"}],
                "stale_sources": 0,
            }
        }
    )
    assert shaped["alive"] == 2
    assert set(shaped) == {
        "total",
        "alive",
        "dead",
        "untested",
        "in_use",
        "assignable",
        "tier_a",
        "tier_b",
        "working_set",
        "avg_score",
    }  # operator-only extras (by_protocol, stale_sources) never cross


def test_credit_interval_zero_disables_metering() -> None:
    client, _, store = boot_app(credit_interval_s=0)
    tenant = register(client)
    from tenancy import billing

    row = store.get_tenant(tenant["tenant_id"])
    assert row is not None
    debit = billing.drain_elapsed(store, row, 0, row.credited_s + 999999)
    assert debit == 0.0


def test_board_full_rejects_registrations() -> None:
    app = create_app(
        settings=TenancySettings(max_tenants=1, admin_key="k", starting_credits=10),
        store=fresh_store(),
        engine=FakeEdge(),
        proxy_clients={},
    )
    app.testing = True
    c = app.test_client()
    assert (
        c.post(
            "/v1/tenants/register", json={"title": "a", "email": "a@b.co"}
        ).status_code
        == 201
    )
    assert (
        c.post(
            "/v1/tenants/register", json={"title": "b", "email": "b@b.co"}
        ).status_code
        == 503
    )


def test_tenant_shape_never_leaks_internal_columns() -> None:
    client, _, _ = boot_app()
    tenant = register(client)
    assert set(tenant) == {
        "tenant_id",
        "tenant_key",
        "title",
        "email",
        "state",
        "node_count_requested",
        "created_at",
        "credits",
        "credited_at",
    }  # internal columns (seeded, timestamps) stay store-side
