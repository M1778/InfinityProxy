"""SQLite store for the control center (ADR-0012): tenants, credits, ledger.

Separate database file from the engine pool (`tenancy.db`): the engine's
row shape stays untouched and a control center can be dropped without
migrating anything the engine wrote.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Self

from tenancy.models import LedgerEntry, Tenant

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id     TEXT PRIMARY KEY,
    tenant_key    TEXT UNIQUE,
    title         TEXT,
    email         TEXT,
    state         TEXT,
    node_count_requested INTEGER,
    renewed_at    REAL,
    created_at_s  REAL,
    updated_at_s  REAL,
    credits       REAL,
    credited_s    REAL,
    seeded        INTEGER
);

CREATE TABLE IF NOT EXISTS ledger (
    at_s      REAL,
    tenant_id TEXT,
    delta     REAL,
    reason    TEXT
);

CREATE TABLE IF NOT EXISTS tenant_tunnels (
    tenant_id TEXT,
    tunnel_id TEXT,
    created_at_s REAL,
    PRIMARY KEY (tenant_id, tunnel_id)
);
"""

_LEDGER_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_ledger_tenant ON ledger(tenant_id, at_s)"
)


class TenantsStore:
    """Thread-safe wrapper over one sqlite3 connection (WAL, lock-guarded)."""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(_LEDGER_INDEX)
            self._conn.commit()

    def _row_to_tenant(self, row: sqlite3.Row) -> Tenant:
        return Tenant(
            tenant_id=row["tenant_id"],
            tenant_key=row["tenant_key"] or "",
            title=row["title"] or "",
            email=row["email"] or "",
            state=row["state"] or "draft",
            node_count_requested=row["node_count_requested"] or 0,
            renewed_at=row["renewed_at"],
            created_at_s=row["created_at_s"] or 0.0,
            updated_at_s=row["updated_at_s"] or 0.0,
            credits=row["credits"] or 0.0,
            credited_s=row["credited_s"],
            seeded=bool(row["seeded"]),
        )

    @staticmethod
    def _tuples(tenant: Tenant) -> tuple[object, ...]:
        return (
            tenant.tenant_id,
            tenant.tenant_key,
            tenant.title,
            tenant.email,
            tenant.state,
            tenant.node_count_requested,
            tenant.renewed_at,
            tenant.created_at_s,
            tenant.updated_at_s,
            tenant.credits,
            tenant.credited_s,
            int(tenant.seeded),
        )

    def save_tenant(self, tenant: Tenant) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO tenants
                       (tenant_id, tenant_key, title, email, state,
                        node_count_requested, renewed_at, created_at_s,
                        updated_at_s, credits, credited_s, seeded)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id) DO UPDATE SET
                       tenant_key = excluded.tenant_key,
                       title = excluded.title,
                       email = excluded.email,
                       state = excluded.state,
                       node_count_requested = excluded.node_count_requested,
                       renewed_at = excluded.renewed_at,
                       created_at_s = excluded.created_at_s,
                       updated_at_s = excluded.updated_at_s,
                       credits = excluded.credits,
                       credited_s = excluded.credited_s,
                       seeded = excluded.seeded""",
                self._tuples(tenant),
            )
            self._conn.commit()

    def load_tenants(self) -> list[Tenant]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tenants ORDER BY created_at_s"
            ).fetchall()
        return [self._row_to_tenant(r) for r in rows]

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
        return self._row_to_tenant(row) if row is not None else None

    def get_by_key(self, tenant_key: str) -> Tenant | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tenants WHERE tenant_key = ?", (tenant_key,)
            ).fetchone()
        return self._row_to_tenant(row) if row is not None else None

    def delete_tenant(self, tenant_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM tenants WHERE tenant_id = ?", (tenant_id,))
            self._conn.execute("DELETE FROM ledger WHERE tenant_id = ?", (tenant_id,))
            self._conn.execute(
                "DELETE FROM tenant_tunnels WHERE tenant_id = ?", (tenant_id,)
            )
            self._conn.commit()

    def tenant_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0])

    def active_count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM tenants WHERE state = 'active'"
                ).fetchone()[0]
            )

    def record(self, entry: LedgerEntry) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO ledger (at_s, tenant_id, delta, reason) "
                "VALUES (?, ?, ?, ?)",
                (entry.at_s, entry.tenant_id, entry.delta, entry.reason),
            )
            self._conn.commit()

    def ledger(self, tenant_id: str, keep: int = 20) -> list[LedgerEntry]:
        """Latest `keep` entries, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT at_s, tenant_id, delta, reason FROM ledger "
                "WHERE tenant_id = ? ORDER BY at_s DESC LIMIT ?",
                (tenant_id, keep),
            ).fetchall()
        return [
            LedgerEntry(
                at_s=row["at_s"] or 0.0,
                tenant_id=row["tenant_id"],
                delta=row["delta"] or 0.0,
                reason=row["reason"] or "",
            )
            for row in rows
        ]

    def prune_ledger(self, tenant_id: str, keep: int) -> None:
        """Drop all but the newest `keep` entries for a tenant."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM ledger WHERE tenant_id = ? AND at_s NOT IN "
                "(SELECT at_s FROM ledger WHERE tenant_id = ? "
                " ORDER BY at_s DESC LIMIT ?)",
                (tenant_id, tenant_id, keep),
            )
            self._conn.commit()

    def total_credits(self) -> float:
        with self._lock:
            row = self._conn.execute("SELECT SUM(credits) FROM tenants").fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    def link_tunnel(self, tenant_id: str, tunnel_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO tenant_tunnels "
                "(tenant_id, tunnel_id, created_at_s) VALUES (?, ?, ?)",
                (tenant_id, tunnel_id, time.time()),
            )
            self._conn.commit()

    def unlink_tunnel(self, tenant_id: str, tunnel_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM tenant_tunnels WHERE tenant_id = ? AND tunnel_id = ?",
                (tenant_id, tunnel_id),
            )
            self._conn.commit()

    def tunnels_of(self, tenant_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tunnel_id FROM tenant_tunnels WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def drop_tenant_tunnels(self, tenant_id: str) -> list[str]:
        ids = self.tunnels_of(tenant_id)
        with self._lock:
            self._conn.execute(
                "DELETE FROM tenant_tunnels WHERE tenant_id = ?", (tenant_id,)
            )
            self._conn.commit()
        return ids

    def spend(self, tenant_id: str, amount: float, now: float) -> bool:
        """Atomic debit: `true` only when the tenant held enough credits.

        The guard lives in the UPDATE, not a read-then-write check: a tenant
        racing its own renewal (double click, key retry loop) cannot double
        spend. `time.time()` is passed in so callers shape the clock, tests
        included.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tenants SET credits = credits - ?, updated_at_s = ? "
                "WHERE tenant_id = ? AND state = 'active' AND credits >= ?",
                (amount, now, tenant_id, amount),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def grant(self, tenant_id: str, amount: float, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tenants SET credits = credits + ?, updated_at_s = ? "
                "WHERE tenant_id = ?",
                (amount, now, tenant_id),
            )
            self._conn.commit()


class TenantNotFound(Exception):
    pass


def now_s() -> float:
    return time.time()
