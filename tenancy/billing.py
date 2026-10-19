"""Credit metering (ADR-0011): walls-clock billing against tenant credits."""

from __future__ import annotations

import time

from tenancy.models import LedgerEntry, Tenant
from tenancy.store import TenantsStore


def drain_elapsed(
    store: TenantsStore,
    tenant: Tenant,
    credit_interval_s: int,
    now: float | None = None,
) -> float:
    """Debit wall-clock credits the tenant earned since last touch.

    A live tenant accrues `credit_interval_s` per period; passing the period
    boundary moves `credited_s` forward. Returns the debit (>= 0). Exit state:
    the tenant's `renewed_at` stays None except when the credit run forces an
    implicit refresh; callers own the engine-facing bookkeeping.
    """
    if now is None:
        now = time.time()
    if tenant.state != "active" or credit_interval_s <= 0 or tenant.credited_s is None:
        return 0.0
    elapsed = now - tenant.credited_s
    if elapsed < credit_interval_s:
        return 0.0
    periods = int(elapsed // credit_interval_s)
    debit = float(periods)
    tenant.credits = max(0.0, tenant.credits - debit)
    tenant.credited_s = tenant.credited_s + periods * credit_interval_s
    store.save_tenant(tenant)
    store.record(
        LedgerEntry(
            tenant_id=tenant.tenant_id,
            delta=-debit,
            reason=f"{periods * credit_interval_s // 3600}h metered",
            at_s=now,
        )
    )
    return debit


def grant_starting(
    store: TenantsStore,
    tenant: Tenant,
    starting_credits: float,
    now: float | None = None,
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
