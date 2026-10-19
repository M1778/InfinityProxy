"""Canonical tenancy dataclasses. Vocabulary per CONTEXT.md: a tenant's
tunnel is the same Proxy Tunnel the engine spawns; credits and keys live here.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

TENANT_DISABLED = "disabled"
TENANT_ACTIVE = "active"
TENANT_DRAFT = "draft"

TENANT_STATES = (TENANT_ACTIVE, TENANT_DISABLED, TENANT_DRAFT)


def slug(stem: str) -> str:
    base = "tn" + stem.replace("-", "")[:7]
    return base or "tnx"


@dataclass(frozen=True)
class TenantRef:
    """Minimal identity the engine forwards with each provision call."""

    tenant_id: str


@dataclass
class Tenant:
    """One registered operator of the control center: its key, plan, credits.

    An `invite` stays in `draft` until the admin submits the form filling the
    seeded profile; only then does it become `active` and can sign in.
    """

    tenant_id: str
    tenant_key: str
    title: str
    email: str
    state: str = TENANT_ACTIVE
    node_count_requested: int = 4
    renewed_at: float | None = None
    created_at_s: float = field(default_factory=time.time)
    updated_at_s: float = field(default_factory=time.time)
    credits: float = 0.0
    credited_s: float | None = None
    seeded: bool = False

    @classmethod
    def create(
        cls,
        title: str,
        email: str,
        node_count_requested: int = 4,
        seeded: bool = True,
    ) -> "Tenant":
        stem = uuid.uuid4().hex[:8]
        return cls(
            tenant_id=f"{slug(stem)}",
            tenant_key=f"user_{stem}{uuid.uuid4().hex[:10]}",
            title=title,
            email=email,
            node_count_requested=node_count_requested,
            seeded=seeded,
        )


@dataclass
class LedgerEntry:
    """One-metered credit event on a tenant's ledger."""

    tenant_id: str
    delta: float
    reason: str
    at_s: float = field(default_factory=time.time)
