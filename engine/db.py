"""SQLite store: one Store shared by the Flask app and the background loops."""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Self

from engine.models import Node, NodeCandidate, ProbeResult, Tunnel


def _wilson_sql(ok_expr: str, total_expr: str, z: float = 1.96) -> str:
    """SQL reproduction of stability.wilson_lower for in-UPDATE score caching.

    Mirrors the Python formula exactly so the cached `score_f` and the
    read-time Python availability agree (asserted in tests).
    """
    z2 = z * z
    # Leading 1.0 * forces float division: SQLite truncates integer division,
    # which would zero the point estimate for any probe_ok < probe_total.
    p = f"(1.0 * ({ok_expr}) / ({total_expr}))"
    den = f"(1 + {z2} / ({total_expr}))"
    center = f"({p} + {z2} / (2 * ({total_expr}))) / {den}"
    half = (
        f"{z} * SQRT({p} * (1 - {p}) / ({total_expr})"
        f" + {z2} / (4 * ({total_expr}) * ({total_expr}))) / {den}"
    )
    return f"MAX(0.0, {center} - {half})"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tunnels (
    tunnel_id            TEXT PRIMARY KEY,
    state                TEXT,
    port                 INTEGER UNIQUE,
    username             TEXT,
    password             TEXT,
    node_count_requested INTEGER,
    node_count_granted   INTEGER,
    auto_renew           INTEGER,
    created_at_s         REAL,
    updated_at_s         REAL
);

CREATE TABLE IF NOT EXISTS nodes (
    node_id        TEXT PRIMARY KEY,
    uri            TEXT,
    protocol       TEXT,
    server         TEXT,
    port           INTEGER,
    user           TEXT,
    source         TEXT,
    first_seen_s   REAL,
    last_latency_ms INTEGER,
    state          TEXT NOT NULL DEFAULT 'untested',
    assigned_to    TEXT,
    throughput_kb_s INTEGER,
    probe_ok       INTEGER,
    probe_total    INTEGER,
    window_started_s REAL,
    last_probe_s   REAL,
    last_alive_s   REAL,
    score_f        REAL
);

CREATE TABLE IF NOT EXISTS sources (
    name         TEXT PRIMARY KEY,
    name_scm     TEXT,
    last_fetch_s REAL,
    next_fetch_s REAL
);
"""


class Store:
    """Thread-safe wrapper over one sqlite3 connection (WAL, single writer via lock)."""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")

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
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        # Column migrations for databases created before a schema change. The
        # base schema is CREATE TABLE IF NOT EXISTS, so existing rows keep the
        # pre-change columns until renamed/rebuilt; an additive column is the
        # cheapest forward-compatible migration and must be additive-only.
        cols = {
            r["name"] for r in self._conn.execute("PRAGMA table_info(nodes)").fetchall()
        }
        if "throughput_kb_s" not in cols:
            self._conn.execute("ALTER TABLE nodes ADD COLUMN throughput_kb_s INTEGER")
        for name, decl in (
            ("probe_ok", "INTEGER"),
            ("probe_total", "INTEGER"),
            ("window_started_s", "REAL"),
            ("last_probe_s", "REAL"),
            ("last_alive_s", "REAL"),
            ("score_f", "REAL"),
        ):
            if name not in cols:
                self._conn.execute(f"ALTER TABLE nodes ADD COLUMN {name} {decl}")

    def _row_to_tunnel(self, row: sqlite3.Row) -> Tunnel:
        return Tunnel(
            tunnel_id=row["tunnel_id"],
            state=row["state"],
            port=row["port"],
            username=row["username"],
            password=row["password"],
            node_count_requested=row["node_count_requested"],
            node_count_granted=row["node_count_granted"],
            auto_renew=bool(row["auto_renew"]),
            created_at_s=row["created_at_s"],
            updated_at_s=row["updated_at_s"],
        )

    def _row_to_node(self, row: sqlite3.Row) -> Node:
        return Node(
            node_id=row["node_id"],
            uri=row["uri"],
            protocol=row["protocol"],
            server=row["server"],
            port=row["port"],
            user=row["user"],
            source=row["source"],
            first_seen_s=row["first_seen_s"],
            last_latency_ms=row["last_latency_ms"],
            state=row["state"],
            throughput_kb_s=row["throughput_kb_s"],
            probe_ok=row["probe_ok"] or 0,
            probe_total=row["probe_total"] or 0,
            window_started_s=row["window_started_s"],
            last_probe_s=row["last_probe_s"],
            last_alive_s=row["last_alive_s"],
            score_f=row["score_f"],
        )

    def save_tunnel(self, t: Tunnel) -> None:
        # ON CONFLICT(tunnel_id) lets a re-save update the row while still
        # surfacing an IntegrityError when another tunnel holds the port.
        with self._lock:
            self._conn.execute(
                """INSERT INTO tunnels
                       (tunnel_id, state, port, username, password,
                        node_count_requested, node_count_granted, auto_renew,
                        created_at_s, updated_at_s)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tunnel_id) DO UPDATE SET
                       state = excluded.state,
                       port = excluded.port,
                       username = excluded.username,
                       password = excluded.password,
                       node_count_requested = excluded.node_count_requested,
                       node_count_granted = excluded.node_count_granted,
                       auto_renew = excluded.auto_renew,
                       created_at_s = excluded.created_at_s,
                       updated_at_s = excluded.updated_at_s""",
                (
                    t.tunnel_id,
                    t.state,
                    t.port,
                    t.username,
                    t.password,
                    t.node_count_requested,
                    t.node_count_granted,
                    int(t.auto_renew),
                    t.created_at_s,
                    t.updated_at_s,
                ),
            )
            self._conn.commit()

    def load_tunnels(self) -> list[Tunnel]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tunnels ORDER BY created_at_s"
            ).fetchall()
        return [self._row_to_tunnel(r) for r in rows]

    def get_tunnel(self, tunnel_id: str) -> Tunnel | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tunnels WHERE tunnel_id = ?", (tunnel_id,)
            ).fetchone()
        return self._row_to_tunnel(row) if row is not None else None

    def delete_tunnel(self, tunnel_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM tunnels WHERE tunnel_id = ?", (tunnel_id,))
            self._conn.commit()

    def set_tunnel_state(self, tunnel_id: str, state: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tunnels SET state = ?, updated_at_s = ? WHERE tunnel_id = ?",
                (state, time.time(), tunnel_id),
            )
            self._conn.commit()

    def tunnel_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM tunnels").fetchone()[0])

    def port_in_use(self, port: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tunnels WHERE port = ?", (port,)
            ).fetchone()
        return row is not None

    def tunnels_summary(self, active: bool) -> int:
        # Status counts live tunnels as everything that has not reached stopped.
        with self._lock:
            if active:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM tunnels WHERE state NOT IN ('stopped')"
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM tunnels WHERE state IN ('stopped')"
                ).fetchone()
        return int(row[0])

    def upsert_candidates(self, candidates: list[NodeCandidate]) -> int:
        inserted = 0
        with self._lock:
            for c in candidates:
                candidate = Node.from_candidate(c)
                existing = self._conn.execute(
                    "SELECT uri, protocol FROM nodes WHERE node_id = ?",
                    (candidate.node_id,),
                ).fetchone()
                if existing is None:
                    self._conn.execute(
                        """INSERT INTO nodes
                               (node_id, uri, protocol, server, port, user,
                                source, first_seen_s)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            candidate.node_id,
                            candidate.uri,
                            candidate.protocol,
                            candidate.server,
                            candidate.port,
                            candidate.user,
                            candidate.source,
                            candidate.first_seen_s,
                        ),
                    )
                    inserted += 1
                elif (
                    existing["uri"] != candidate.uri
                    or existing["protocol"] != candidate.protocol
                ):
                    # Identity already known: change the URI/protocol, keep the
                    # first-seen source as attribution. A changed URI makes any
                    # previous liveness verdict stale, so the node is sent back
                    # to the admission gate (untested) rather than keeping a
                    # possibly-garbage "alive" state.
                    self._conn.execute(
                        "UPDATE nodes SET uri = ?, protocol = ?, state = 'untested' "
                        "WHERE node_id = ?",
                        (candidate.uri, candidate.protocol, candidate.node_id),
                    )
            self._conn.commit()
        return inserted

    def load_nodes(
        self,
        state: str | None = None,
        protocols: set[str] | None = None,
        source: str | None = None,
        limit: int | None = None,
        oldest_first: bool = False,
        query: str | None = None,
        in_use: bool | None = None,
        order_by_score: bool = False,
    ) -> list[Node]:
        sql = "SELECT * FROM nodes"
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if protocols:
            clauses.append(f"protocol IN ({','.join('?' for _ in protocols)})")
            params.extend(sorted(protocols))
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if query:
            clauses.append("(server LIKE ? OR node_id LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like])
        if in_use is not None:
            clauses.append(
                "assigned_to IS NOT NULL" if in_use else "assigned_to IS NULL"
            )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if order_by_score:
            # score_f desc is the availability term (ADR-0009): NULL (cold or
            # feature-off) rows sort last, then the existing state/latency order.
            sql += " ORDER BY score_f DESC NULLS LAST, state, last_latency_ms"
        elif oldest_first:
            sql += " ORDER BY first_seen_s, node_id"
        else:
            sql += " ORDER BY state, last_latency_ms"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_node(r) for r in rows]

    def get_node(self, node_id: str) -> Node | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
        return self._row_to_node(row) if row is not None else None

    def apply_probe_results(
        self,
        results: dict[str, ProbeResult],
        *,
        stability: bool = False,
        window_s: float | None = None,
    ) -> None:
        with self._lock:
            now = time.time()
            for node_id, res in results.items():
                # COALESCE keeps the last non-empty throughput reading when a
                # handshake-only probe (the 30s health loop) reports None, so a
                # transient health pass never erases the admission metric.
                if stability:
                    # Windowed counters + the cached availability term for every
                    # verdict the health/admission loops already run (ADR-0009).
                    # Phase-2 fold: when the node's counter window outlives
                    # `window_s` the old counters halve (integer division mirrors
                    # Python //) and the window restarts, giving geometric
                    # recency weighting with O(1) storage. The fold is computed
                    # here in Python and inlined into the UPDATE because SQLite
                    # evaluates every SET expression against the pre-update row.
                    row = self._conn.execute(
                        "SELECT probe_ok, probe_total, window_started_s "
                        "FROM nodes WHERE node_id = ?",
                        (node_id,),
                    ).fetchone()
                    ok = row["probe_ok"] or 0 if row is not None else 0
                    total = row["probe_total"] or 0 if row is not None else 0
                    started = row["window_started_s"] if row is not None else None
                    if window_s is not None and (
                        started is None or now - started > window_s
                    ):
                        ok //= 2
                        total //= 2
                        started = now
                    if started is None:
                        started = now
                    ok += 1 if res.alive else 0
                    total += 1
                    self._conn.execute(
                        f"""UPDATE nodes SET
                                state = ?,
                                last_latency_ms = ?,
                                throughput_kb_s = COALESCE(?, throughput_kb_s),
                                probe_ok = ?,
                                probe_total = ?,
                                window_started_s = ?,
                                last_probe_s = ?,
                                last_alive_s = CASE WHEN ? THEN ? ELSE last_alive_s END,
                                score_f = {_wilson_sql(str(ok), str(total))}
                            WHERE node_id = ?""",
                        (
                            "alive" if res.alive else "dead",
                            res.latency_ms,
                            res.throughput_kb_s,
                            ok,
                            total,
                            started,
                            now,
                            res.alive,
                            now,
                            node_id,
                        ),
                    )
                else:
                    self._conn.execute(
                        "UPDATE nodes SET state = ?, last_latency_ms = ?, "
                        "throughput_kb_s = COALESCE(?, throughput_kb_s) "
                        "WHERE node_id = ?",
                        (
                            "alive" if res.alive else "dead",
                            res.latency_ms,
                            res.throughput_kb_s,
                            node_id,
                        ),
                    )
            self._conn.commit()

    def unassign_node(self, node_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE nodes SET assigned_to = NULL WHERE node_id = ?", (node_id,)
            )
            self._conn.commit()

    def pool_counts(
        self,
        *,
        min_probes: int | None = None,
        min_avail: float | None = None,
        max_age_s: float | None = None,
        working_set_size: int | None = None,
    ) -> dict[str, int | float | None]:
        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
            alive = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE state = 'alive'"
                ).fetchone()[0]
            )
            dead = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE state = 'dead'"
                ).fetchone()[0]
            )
            untested = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE state = 'untested'"
                ).fetchone()[0]
            )
            in_use = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE state = 'alive' "
                    "AND assigned_to IS NOT NULL"
                ).fetchone()[0]
            )
            tier_a = tier_b = 0
            working_set = 0
            avg_score: float | None = None
            if min_probes is not None and min_avail is not None:
                wilson = _wilson_sql(
                    "COALESCE(probe_ok, 0)", "COALESCE(probe_total, 0)"
                )
                probed = "state = 'alive' AND COALESCE(probe_total, 0) >= ?"
                # Freshness gate (ADR-0009 Phase 2): an evaluated node whose
                # verdict aged past max_age_s leaves Tier A for Tier B.
                fresh = ""
                if max_age_s is not None:
                    now = time.time()
                    fresh = (
                        f" AND last_probe_s IS NOT NULL "
                        f"AND {now:g} - last_probe_s <= {max_age_s:g}"
                    )
                tier_a = int(
                    self._conn.execute(
                        f"SELECT COUNT(*) FROM nodes WHERE {probed}"
                        f" AND {wilson} >= ?{fresh}",
                        (min_probes, min_avail),
                    ).fetchone()[0]
                )
                tier_b = int(
                    self._conn.execute(
                        f"SELECT COUNT(*) FROM nodes WHERE {probed}"
                        f" AND NOT ({wilson} >= ?{fresh})",
                        (min_probes, min_avail),
                    ).fetchone()[0]
                )
                row = self._conn.execute(
                    "SELECT AVG(score_f) FROM nodes WHERE state = 'alive'"
                ).fetchone()
                if row and row[0] is not None:
                    avg_score = float(row[0])
                if working_set_size is not None:
                    working_set = self._working_set_count_locked(working_set_size)
        return {
            "total": total,
            "alive": alive,
            "dead": dead,
            "untested": untested,
            "in_use": in_use,
            "tier_a": tier_a,
            "tier_b": tier_b,
            "working_set": working_set,
            "avg_score": avg_score,
        }

    def working_set_count(self, limit: int) -> int:
        with self._lock:
            return self._working_set_count_locked(limit)

    def _working_set_count_locked(self, limit: int) -> int:
        # The working set is a query, not a table: the top-`limit` alive,
        # unassigned nodes by cached availability, the same population the
        # scheduler's working-set loop re-probes (ADR-0009 Phase 2).
        row = self._conn.execute(
            """SELECT COUNT(*) FROM (
                   SELECT node_id FROM nodes
                   WHERE state = 'alive' AND assigned_to IS NULL
                   ORDER BY score_f DESC NULLS LAST, last_probe_s DESC
                   LIMIT ?
               )""",
            (limit,),
        ).fetchone()
        return int(row[0])

    def pool_by_protocol(self) -> list[dict[str, int | str]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT protocol,
                       COUNT(*) AS total,
                       SUM(CASE WHEN state = 'alive' THEN 1 ELSE 0 END) AS alive,
                       SUM(CASE WHEN state = 'dead' THEN 1 ELSE 0 END) AS dead,
                       SUM(CASE WHEN state = 'untested' THEN 1 ELSE 0 END) AS untested
                FROM nodes
                GROUP BY protocol
                ORDER BY total DESC
                """
            ).fetchall()
        return [
            {
                "protocol": r["protocol"],
                "total": int(r["total"]),
                "alive": int(r["alive"]),
                "dead": int(r["dead"]),
                "untested": int(r["untested"]),
            }
            for r in rows
        ]

    def assign_nodes(self, node_ids: list[str], tunnel_id: str) -> None:
        # Single assigned_to column makes double-assignment impossible; a node
        # claimed by another tunnel is moved, never duplicated.
        with self._lock:
            self._conn.executemany(
                "UPDATE nodes SET assigned_to = ? WHERE node_id = ?",
                [(tunnel_id, node_id) for node_id in node_ids],
            )
            self._conn.commit()

    def release_tunnel(self, tunnel_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE nodes SET assigned_to = NULL WHERE assigned_to = ?",
                (tunnel_id,),
            )
            self._conn.commit()
        return cur.rowcount

    def node_ids_assigned_to(self, tunnel_id: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT node_id FROM nodes WHERE assigned_to = ?", (tunnel_id,)
            ).fetchall()
        return {r[0] for r in rows}

    def node_ids_in_use(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT node_id FROM nodes WHERE assigned_to IS NOT NULL"
            ).fetchall()
        return {r[0] for r in rows}

    def upsert_source(self, name: str, name_scm: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO sources (name, name_scm)
                   VALUES (?, ?)
                   ON CONFLICT(name) DO UPDATE SET name_scm = excluded.name_scm""",
                (name, name_scm),
            )
            self._conn.commit()

    def record_fetch(self, name: str, fetched_s: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sources SET last_fetch_s = ? WHERE name = ?", (fetched_s, name)
            )
            self._conn.commit()

    def sources_summary(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, name_scm, last_fetch_s, next_fetch_s "
                "FROM sources ORDER BY name"
            ).fetchall()
        return [
            {
                "name": r["name"],
                "name_scm": r["name_scm"],
                "last_fetch_s": r["last_fetch_s"],
                "next_fetch_s": r["next_fetch_s"],
            }
            for r in rows
        ]
