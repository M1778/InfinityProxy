"""Transactional tenancy regressions, including independent SQLite connections."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_tenancy import FakeEdge, make_settings, op_headers, register

from tenancy import billing
from tenancy.app import create_app
from tenancy.models import Tenant
from tenancy.store import TenantsStore


def seeded_store(path, credits=48.0):
    store = TenantsStore(str(path))
    store.create_schema()
    tenant = Tenant.create("acme", "ops@acme.co")
    billing.grant_starting(store, tenant, credits, now=1000.0)
    return store, tenant


def test_two_connections_drain_one_boundary_once(tmp_path):
    path = tmp_path / "tenancy.db"
    first, tenant = seeded_store(path)
    second = TenantsStore(str(path))
    barrier = threading.Barrier(2)

    def drain(store):
        row = store.get_tenant(tenant.tenant_id)
        barrier.wait(timeout=5)
        return billing.drain_elapsed(store, row, 3600, now=4600.0)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            debits = list(executor.map(drain, [first, second]))
        assert sorted(debits) == [0.0, 1.0]
        assert first.get_tenant(tenant.tenant_id).credits == 47.0
        ledger = first.ledger(tenant.tenant_id)
        assert len([e for e in ledger if "metered" in e.reason]) == 1
        assert sum(e.delta for e in ledger) == 47.0
    finally:
        first.close()
        second.close()


def test_stale_balance_does_not_lose_or_misrecord_grant(tmp_path):
    store, tenant = seeded_store(tmp_path / "tenancy.db", credits=0.4)
    try:
        store.grant(tenant.tenant_id, 4.0, now=4500.0)
        assert billing.drain_elapsed(store, tenant, 3600, now=4600.0) == 1.0
        assert tenant.credits == pytest.approx(3.4)
        ledger = store.ledger(tenant.tenant_id)
        assert sum(e.delta for e in ledger) == pytest.approx(3.4)
        assert "shortfall" not in ledger[0].reason
    finally:
        store.close()


def test_shortfall_records_actual_debit_and_advances_boundary(tmp_path):
    store, tenant = seeded_store(tmp_path / "tenancy.db", credits=0.4)
    try:
        assert billing.drain_elapsed(store, tenant, 3600, now=8200.0) == 0.4
        assert tenant.credits == 0.0
        assert tenant.credited_s == 8200.0
        ledger = store.ledger(tenant.tenant_id)
        assert ledger[0].delta == -0.4
        assert "shortfall 1.6" in ledger[0].reason
        assert sum(e.delta for e in ledger) == 0.0
        assert billing.drain_elapsed(store, tenant, 3600, now=8200.0) == 0.0
    finally:
        store.close()


def test_ledger_failure_rolls_back_credit_and_tick(tmp_path):
    store, tenant = seeded_store(tmp_path / "tenancy.db")
    try:
        with store._conn:
            store._conn.execute(
                "CREATE TRIGGER reject_ledger BEFORE INSERT ON ledger "
                "BEGIN SELECT RAISE(ABORT, 'ledger unavailable'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="ledger unavailable"):
            billing.drain_elapsed(store, tenant, 3600, now=4600.0)
        row = store.get_tenant(tenant.tenant_id)
        assert row.credits == 48.0
        assert row.credited_s == 1000.0
        assert len(store.ledger(tenant.tenant_id)) == 1
        with pytest.raises(sqlite3.IntegrityError):
            store.grant(tenant.tenant_id, 2.0, now=4600.0)
        assert store.get_tenant(tenant.tenant_id).credits == 48.0
    finally:
        store.close()


def test_ledger_retention_handles_ties_and_zero_debits(tmp_path):
    store, tenant = seeded_store(tmp_path / "tenancy.db", credits=0.0)
    try:
        for _ in range(8):
            store.grant(tenant.tenant_id, 0.1, now=1000.0, keep=3)
        assert len(store.ledger(tenant.tenant_id)) == 3
        for now in (4600.0, 8200.0, 11800.0, 15400.0):
            billing.drain_elapsed(store, tenant, 3600, now=now, keep=3)
        ledger = store.ledger(tenant.tenant_id)
        assert len(ledger) == 3
        assert all("shortfall" in e.reason for e in ledger)
    finally:
        store.close()


def test_reservations_enforce_cap_across_connections(tmp_path):
    path = tmp_path / "tenancy.db"
    first, tenant = seeded_store(path)
    second = TenantsStore(str(path))
    barrier = threading.Barrier(2)

    def reserve(store):
        barrier.wait(timeout=5)
        return store.reserve_tunnel(tenant.tenant_id, cap=1)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            slots = list(executor.map(reserve, [first, second]))
        assert sum(slot is not None for slot in slots) == 1
        slot = next(slot for slot in slots if slot is not None)
        assert first.complete_reservation(tenant.tenant_id, slot, "tu_one")
        assert second.reserve_tunnel(tenant.tenant_id, cap=1) is None
        first.unlink_tunnel(tenant.tenant_id, "tu_one")
        assert second.reserve_tunnel(tenant.tenant_id, cap=1) is not None
    finally:
        first.close()
        second.close()


def test_create_reserves_before_engine_call_and_releases_on_failure(tmp_path):
    store = TenantsStore(str(tmp_path / "tenancy.db"))
    edge = FakeEdge()
    app = create_app(make_settings(max_tunnels_per_tenant=1), store, edge)
    client = app.test_client()
    tenant = register(client)
    entered = threading.Event()
    finish = threading.Event()
    original = edge.create_tunnel

    def create(*args, **kwargs):
        entered.set()
        assert finish.wait(timeout=5)
        return original(*args, **kwargs)

    edge.create_tunnel = create
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(
                lambda: app.test_client().post(
                    "/v1/me/tunnels", headers=op_headers(tenant)
                )
            )
            assert entered.wait(timeout=5)
            try:
                second = client.post("/v1/me/tunnels", headers=op_headers(tenant))
                assert second.status_code == 409
            finally:
                finish.set()
            assert first.result(timeout=5).status_code == 201
        assert edge.calls.count(("POST", "/tunnels")) == 1
        tunnel_id = store.tunnels_of(tenant["tenant_id"])[0]
        store.unlink_tunnel(tenant["tenant_id"], tunnel_id)

        def reject(*args, **kwargs):
            from tenancy.engine import EngineSideError

            raise EngineSideError(400, "invalid_request", "creation rejected")

        edge.create_tunnel = reject
        assert (
            client.post("/v1/me/tunnels", headers=op_headers(tenant)).status_code == 502
        )
        edge.create_tunnel = original
        assert (
            client.post("/v1/me/tunnels", headers=op_headers(tenant)).status_code == 201
        )
    finally:
        store.close()


def test_auth_uses_post_meter_balance(tmp_path, monkeypatch):
    store, tenant = seeded_store(tmp_path / "tenancy.db", credits=1.0)
    edge = FakeEdge()
    app = create_app(make_settings(), store, edge)
    monkeypatch.setattr("tenancy.billing.time.time", lambda: 4600.0)
    try:
        client = app.test_client()
        headers = {"X-Operator-Key": tenant.tenant_key}
        assert client.post("/v1/me/tunnels", headers=headers).status_code == 402
        assert client.get("/v1/me", headers=headers).get_json()["me"]["credits"] == 0.0
        assert ("POST", "/tunnels") not in edge.calls
    finally:
        store.close()


def test_disable_reports_pending_cleanup_and_retries(tmp_path, monkeypatch):
    store = TenantsStore(str(tmp_path / "tenancy.db"))
    edge = FakeEdge()
    app = create_app(make_settings(), store, edge)
    client = app.test_client()
    tenant = register(client)
    headers = op_headers(tenant)
    one = client.post("/v1/me/tunnels", headers=headers).get_json()
    two = client.post("/v1/me/tunnels", headers=headers).get_json()
    assert set(one) == {"id", "host", "port", "state"}
    original = edge.delete_tunnel

    def delete(tunnel_id):
        if tunnel_id == one["id"]:
            from tenancy.engine import EngineSideError

            raise EngineSideError(502, "engine_unreachable", "down")
        original(tunnel_id)

    monkeypatch.setattr(edge, "delete_tunnel", delete)
    try:
        route = f"/v1/admin/tenants/{tenant['tenant_id']}/disable"
        admin = {"X-Admin-Key": "adm-secret"}
        result = client.post(route, headers=admin)
        assert result.status_code == 200
        assert result.get_json()["cleanup_pending"] == [one["id"]]
        assert store.tunnels_of(tenant["tenant_id"]) == [one["id"]]
        assert ("DELETE", f"/tunnels/{two['id']}") in edge.calls
        assert client.get("/v1/me", headers=headers).status_code == 403
        monkeypatch.setattr(edge, "delete_tunnel", original)
        assert client.post(route, headers=admin).get_json()["cleanup_pending"] == []
        assert store.tunnels_of(tenant["tenant_id"]) == []
    finally:
        store.close()


def test_public_stats_does_not_meter_overdue_accounts(tmp_path):
    store, tenant = seeded_store(tmp_path / "tenancy.db")
    app = create_app(make_settings(), store, FakeEdge())
    try:
        data = app.test_client().get("/v1/public/stats").get_json()
        assert set(data) == {"tenants", "credits_outstanding", "metered_since_s"}
        assert store.get_tenant(tenant.tenant_id).credits == 48.0
        assert store.get_tenant(tenant.tenant_id).credited_s == 1000.0
        assert len(store.ledger(tenant.tenant_id)) == 1
    finally:
        store.close()


def test_configured_ledger_and_meter_off_settings(tmp_path, monkeypatch):
    from tenancy.config import TenancySettings

    monkeypatch.setenv("INFINITY_TENANCY_LEDGER_KEEP", "3")
    assert TenancySettings.from_env().ledger_keep == 3
    store, tenant = seeded_store(tmp_path / "tenancy.db", credits=0.0)
    app = create_app(
        make_settings(credit_interval_s=0, ledger_keep=3), store, FakeEdge()
    )
    try:
        client = app.test_client()
        assert (
            client.post(
                "/v1/me/tunnels", headers={"X-Operator-Key": tenant.tenant_key}
            ).status_code
            == 201
        )
        for _ in range(5):
            store.grant(tenant.tenant_id, 1.0, now=1000.0, keep=3)
        ledger = client.get(
            "/v1/me/ledger", headers={"X-Operator-Key": tenant.tenant_key}
        ).get_json()["ledger"]
        assert len(ledger) == 3
    finally:
        store.close()
