"""Uncertain engine creations must remain accounted for until reconciliation."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_tenancy import FakeEdge, make_settings, op_headers, register

from tenancy.app import create_app
from tenancy.engine import EngineSideError
from tenancy.store import TenantsStore


@pytest.mark.parametrize("mode", ["timeout", "missing_id", "finalize_delete_failed"])
def test_uncertain_creation_keeps_capacity_reserved(tmp_path, monkeypatch, mode):
    store = TenantsStore(str(tmp_path / "tenancy.db"))
    edge = FakeEdge()
    app = create_app(make_settings(max_tunnels_per_tenant=1), store, edge)
    client = app.test_client()
    tenant = register(client)
    original = edge.create_tunnel

    def create(*args, **kwargs):
        result = original(*args, **kwargs)
        if mode == "timeout":
            raise EngineSideError(0, "engine_unreachable", "response lost")
        if mode == "missing_id":
            return {}
        return result

    monkeypatch.setattr(edge, "create_tunnel", create)
    if mode == "finalize_delete_failed":

        def finalize(*args):
            raise sqlite3.OperationalError("database is locked")

        def delete(*args):
            raise EngineSideError(0, "engine_unreachable", "down")

        monkeypatch.setattr(store, "complete_reservation", finalize)
        monkeypatch.setattr(edge, "delete_tunnel", delete)
    try:
        assert (
            client.post("/v1/me/tunnels", headers=op_headers(tenant)).status_code == 502
        )
        assert store.reserve_tunnel(tenant["tenant_id"], cap=1) is None
        rows = store._conn.execute(
            "SELECT tunnel_id FROM tunnel_reservations"
        ).fetchall()
        assert len(rows) == 1
        if mode == "finalize_delete_failed":
            assert rows[0][0] == "tu_1"
        assert len(edge.tunnels_payload["tunnels"]) == 1
    finally:
        store.close()


def test_finalization_failure_compensates_before_releasing_slot(tmp_path, monkeypatch):
    store = TenantsStore(str(tmp_path / "tenancy.db"))
    edge = FakeEdge()
    app = create_app(make_settings(max_tunnels_per_tenant=1), store, edge)
    client = app.test_client()
    tenant = register(client)

    def fail(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "complete_reservation", fail)
    try:
        assert (
            client.post("/v1/me/tunnels", headers=op_headers(tenant)).status_code == 502
        )
        assert edge.tunnels_payload["tunnels"] == []
        assert store.reserve_tunnel(tenant["tenant_id"], cap=1) is not None
    finally:
        store.close()


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_disable_racing_create_keeps_cleanup_accounted_for(
    tmp_path, monkeypatch, cleanup_fails
):
    store = TenantsStore(str(tmp_path / "tenancy.db"))
    edge = FakeEdge()
    app = create_app(make_settings(max_tunnels_per_tenant=1), store, edge)
    client = app.test_client()
    tenant = register(client)
    entered, finish = threading.Event(), threading.Event()
    original = edge.create_tunnel

    def create(*args, **kwargs):
        entered.set()
        assert finish.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(edge, "create_tunnel", create)
    if cleanup_fails:

        def delete(*args):
            raise EngineSideError(0, "engine_unreachable", "down")

        monkeypatch.setattr(edge, "delete_tunnel", delete)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(
                lambda: app.test_client().post(
                    "/v1/me/tunnels", headers=op_headers(tenant)
                )
            )
            assert entered.wait(timeout=5)
            try:
                disabled = client.post(
                    f"/v1/admin/tenants/{tenant['tenant_id']}/disable",
                    headers={"X-Admin-Key": "adm-secret"},
                )
                assert disabled.status_code == 200
            finally:
                finish.set()
            assert result.result(timeout=5).status_code == 403
        ids = store.tunnels_of(tenant["tenant_id"])
        assert ids == (["tu_1"] if cleanup_fails else [])
        assert len(edge.tunnels_payload["tunnels"]) == (1 if cleanup_fails else 0)
        assert store.get_tenant(tenant["tenant_id"]).state == "disabled"
    finally:
        store.close()


def test_overflowing_grant_rolls_back_balance_and_ledger(tmp_path):
    store = TenantsStore(str(tmp_path / "tenancy.db"))
    app = create_app(make_settings(), store, FakeEdge())
    client = app.test_client()
    tenant = register(client)
    route = f"/v1/admin/tenants/{tenant['tenant_id']}/grant"
    headers = {"X-Admin-Key": "adm-secret"}
    try:
        assert (
            client.post(route, headers=headers, json={"amount": 1e308}).status_code
            == 200
        )
        before = store.ledger(tenant["tenant_id"])
        result = client.post(route, headers=headers, json={"amount": 1e308})
        assert result.status_code == 400
        assert store.get_tenant(tenant["tenant_id"]).credits == 1e308
        assert store.ledger(tenant["tenant_id"]) == before
    finally:
        store.close()
