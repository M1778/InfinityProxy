"""SQLite store: one Store shared by the Flask app and the background loops."""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Self

from engine.models import Node, NodeCandidate, ProbeResult, Tunnel

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
    assigned_to    TEXT
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
            self._conn.commit()

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
                    # Identity already known: refresh the mutable fields but keep
                    # the first-seen source as the attribution record.
                    self._conn.execute(
                        "UPDATE nodes SET uri = ?, protocol = ? WHERE node_id = ?",
                        (candidate.uri, candidate.protocol, candidate.node_id),
                    )
            self._conn.commit()
        return inserted

    def load_nodes(
        self,
        state: str | None = None,
        protocols: set[str] | None = None,
        source: str | None = None,
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
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_node(r) for r in rows]

    def get_node(self, node_id: str) -> Node | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
        return self._row_to_node(row) if row is not None else None

    def apply_probe_results(self, results: dict[str, ProbeResult]) -> None:
        with self._lock:
            for node_id, res in results.items():
                self._conn.execute(
                    "UPDATE nodes SET state = ?, last_latency_ms = ? WHERE node_id = ?",
                    ("alive" if res.alive else "dead", res.latency_ms, node_id),
                )
            self._conn.commit()

    def unassign_node(self, node_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE nodes SET assigned_to = NULL WHERE node_id = ?", (node_id,)
            )
            self._conn.commit()

    def pool_counts(self) -> dict[str, int]:
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
            in_use = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE state = 'alive' "
                    "AND assigned_to IS NOT NULL"
                ).fetchone()[0]
            )
        return {"total": total, "alive": alive, "dead": dead, "in_use": in_use}

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
