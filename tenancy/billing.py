"""Credit metering (ADR-0012): wall-clock billing against tenant credits."""

from __future__ import annotations

import time

from tenancy.models import LedgerEntry, Tenant
from tenancy.store import TenantsStore


def drain_elapsed(
    store: TenantsStore,
    tenant: Tenant,
    credit_interval_s: int,
    now: float | None = None,
    keep: int = 50,
) -> float:
    """Charge each full period once and refresh the caller's balance.

    The store reads current credits and commits the tick, actual debit and
    shortfall ledger together; a stale caller cannot erase a concurrent grant.
    """
    if now is None:
        now = time.time()
    row, actual = store.drain(tenant.tenant_id, credit_interval_s, now, keep)
    if row is not None:
        tenant.__dict__.update(row.__dict__)
    return actual


def grant_starting(
    store: TenantsStore,
    tenant: Tenant,
    starting_credits: float,
    now: float | None = None,
    keep: int = 50,
) -> None:
    if now is None:
        now = time.time()
    tenant.credits = starting_credits
    tenant.credited_s = now
    store.save_tenant(tenant)
    store.record(
        LedgerEntry(
            tenant_id=tenant.tenant_id,
            delta=starting_credits,
            reason="signup grant",
            at_s=now,
        )
    )
    store.prune_ledger(tenant.tenant_id, keep)
