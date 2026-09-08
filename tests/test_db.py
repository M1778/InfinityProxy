import sqlite3

import pytest

from engine.db import Store
from engine.models import NodeCandidate, ProbeResult, Tunnel


def make_tunnel(tag: str, port: int, **kw) -> Tunnel:
    values = {
        "tunnel_id": f"tu_{tag}",
        "state": "running",
        "port": port,
        "username": "u_x",
        "password": "p_y",
        "node_count_requested": 10,
        "node_count_granted": 10,
        "auto_renew": True,
        "created_at_s": 1000.0,
        "updated_at_s": 1000.0,
    }
    values.update(kw)
    return Tunnel(**values)


def make_candidate(
    source: str, server: str = "1.2.3.4", port: int = 443, uri: str | None = None
) -> NodeCandidate:
    return NodeCandidate(
        uri=uri or f"vless://abc@{server}:{port}?type=tcp#x",
        protocol="vless",
        server=server,
        port=port,
        user="abc",
        source=source,
    )


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(str(tmp_path / "infinity.db"))
    s.create_schema()
    yield s
    s.close()


def test_schema_creation_and_idempotence(store: Store) -> None:
    assert store.tunnel_count() == 0
    assert store.pool_counts() == {
        "total": 0,
        "alive": 0,
        "dead": 0,
        "in_use": 0,
        "untested": 0,
        "tier_a": 0,
        "tier_b": 0,
        "avg_score": None,
    }
    assert store.sources_summary() == []
    store.create_schema()
    store.create_schema()
    assert store.tunnel_count() == 0


_OLD_SCHEMA = """
CREATE TABLE tunnels (
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
CREATE TABLE nodes (
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
CREATE TABLE sources (
    name         TEXT PRIMARY KEY,
    name_scm     TEXT,
    last_fetch_s REAL,
    next_fetch_s REAL
);
"""


def test_create_schema_migrates_pre_throughput_databases(tmp_path) -> None:
    path = str(tmp_path / "legacy.db")
    import sqlite3

    with sqlite3.connect(path) as conn:
        conn.executescript(_OLD_SCHEMA)
        conn.execute(
            "INSERT INTO nodes (node_id, uri, protocol, server, port, user, "
            "source, first_seen_s, last_latency_ms, state) "
            "VALUES ('nd_old', 'vless://x@1.2.3.4:443', 'vless', '1.2.3.4', 443, "
            "'x', 'legacy', 1000.0, 42, 'alive')"
        )
        conn.commit()

    store = Store(path)
    try:
        store.create_schema()  # IF NOT EXISTS is a no-op, the ALTER must add the column
        node = store.get_node("nd_old")
        assert node is not None
        assert node.last_latency_ms == 42
        assert node.throughput_kb_s is None  # migrated column defaults to NULL

        store.apply_probe_results(
            {"nd_old": ProbeResult("nd_old", True, latency_ms=50, throughput_kb_s=900)}
        )
        assert store.get_node("nd_old").throughput_kb_s == 900
    finally:
        store.close()


def test_apply_probe_results_preserves_throughput_on_handshake_only_pass(
    store: Store,
) -> None:
    store.upsert_candidates([make_candidate(source="a")])
    node = store.load_nodes()[0]
    store.apply_probe_results(
        {
            node.node_id: ProbeResult(
                node.node_id, True, latency_ms=40, throughput_kb_s=810
            )
        }
    )
    assert store.get_node(node.node_id).throughput_kb_s == 810

    # The 30s health loop probes without a download: throughput must not erase.
    store.apply_probe_results(
        {node.node_id: ProbeResult(node.node_id, True, latency_ms=60)}
    )
    assert store.get_node(node.node_id).throughput_kb_s == 810
    assert store.get_node(node.node_id).last_latency_ms == 60


def test_save_list_get_delete_tunnel(store: Store) -> None:
    t = make_tunnel("a", 10001)
    store.save_tunnel(t)

    listed = store.load_tunnels()
    assert len(listed) == 1
    assert listed[0].tunnel_id == "tu_a"
    assert listed[0].port == 10001
    assert listed[0].auto_renew is True
    assert listed[0].state == "running"

    got = store.get_tunnel("tu_a")
    assert got == t
    assert got.auto_renew is True

    t2 = make_tunnel("a", 10001, auto_renew=False, state="stopped")
    store.save_tunnel(t2)
    assert store.tunnel_count() == 1
    assert store.get_tunnel("tu_a").auto_renew is False
    assert store.get_tunnel("tu_a").state == "stopped"

    store.delete_tunnel("tu_a")
    assert store.get_tunnel("tu_a") is None
    assert store.tunnel_count() == 0


def test_port_uniqueness_raises_integrity_error(store: Store) -> None:
    store.save_tunnel(make_tunnel("a", 10001))
    with pytest.raises(sqlite3.IntegrityError):
        store.save_tunnel(make_tunnel("b", 10001))
    assert store.tunnel_count() == 1


def test_set_state_and_summary(store: Store) -> None:
    store.save_tunnel(make_tunnel("a", 10001))
    store.save_tunnel(make_tunnel("b", 10002, state="stopped"))
    assert store.tunnel_count() == 2
    assert store.port_in_use(10001) is True
    assert store.port_in_use(10003) is False
    store.set_tunnel_state("tu_a", "stopped")
    assert store.get_tunnel("tu_a").state == "stopped"
    assert store.tunnels_summary(active=True) == 0
    assert store.tunnels_summary(active=False) == 2


def test_upsert_candidates_inserts_once_and_keeps_first_seen_source(
    store: Store,
) -> None:
    first = make_candidate(source="ebrasha")
    inserted = store.upsert_candidates([first])
    assert inserted == 1

    nodes = store.load_nodes()
    assert len(nodes) == 1
    stored = nodes[0]
    first_seen = stored.first_seen_s
    assert stored.source == "ebrasha"

    same_identity_other_source = make_candidate(
        source="epodonios", uri="vless://abc@1.2.3.4:443?path=/x#y"
    )
    inserted = store.upsert_candidates([same_identity_other_source])
    assert inserted == 0

    nodes = store.load_nodes()
    assert len(nodes) == 1
    kept = nodes[0]
    assert kept.source == "ebrasha"
    assert kept.first_seen_s == first_seen
    assert kept.uri == same_identity_other_source.uri

    fresh = make_candidate(source="gfpcom", server="9.9.9.9", port=8443)
    assert store.upsert_candidates([fresh]) == 1
    assert len(store.load_nodes()) == 2


def test_uri_change_resets_state_for_gate_rejudgement(store: Store) -> None:
    store.upsert_candidates([make_candidate(source="a")])
    node = store.load_nodes()[0]
    result = ProbeResult(node.node_id, alive=True, latency_ms=1)
    store.apply_probe_results({node.node_id: result})
    assert store.load_nodes(state="alive") != []

    # Same identity (server:port:user) re-ingested with a different URI must not
    # keep its old "alive" verdict: the admission gate has to re-judge it.
    store.upsert_candidates(
        [make_candidate(source="a", uri="vless://abc@1.2.3.4:443?type=ws#y")]
    )
    assert store.load_nodes(state="alive") == []
    assert len(store.load_nodes(state="untested")) == 1


def test_load_node_filters(store: Store) -> None:
    store.upsert_candidates(
        [
            make_candidate(source="a"),
            NodeCandidate(
                uri="ss://eGZhh@5.6.7.8:8388#s",
                protocol="ss",
                server="5.6.7.8",
                port=8388,
                user="eGZhh",
                source="b",
            ),
        ]
    )
    assert len(store.load_nodes()) == 2
    assert len(store.load_nodes(protocols={"ss"})) == 1
    assert len(store.load_nodes(protocols={"vless", "ss"})) == 2
    assert len(store.load_nodes(state="untested")) == 2
    assert store.load_nodes(state="alive") == []


def test_load_nodes_limit_and_oldest_first(store: Store) -> None:
    store.upsert_candidates(
        [
            make_candidate(source="a", server="1.1.1.1"),
            make_candidate(source="b", server="2.2.2.2"),
            make_candidate(source="c", server="3.3.3.3"),
        ]
    )
    all_nodes = store.load_nodes()

    limited = store.load_nodes(limit=2)
    assert len(limited) == 2
    fifo = store.load_nodes(oldest_first=True)
    assert {n.node_id for n in fifo} == {n.node_id for n in all_nodes}
    first_two = store.load_nodes(limit=2, oldest_first=True)
    assert first_two[0].first_seen_s <= first_two[1].first_seen_s


def test_apply_probe_results_flips_states(store: Store) -> None:
    store.upsert_candidates(
        [
            make_candidate(source="a"),
            make_candidate(
                source="b", server="5.6.7.8", uri="vless://abc@5.6.7.8:443#z"
            ),
        ]
    )
    nodes = store.load_nodes()
    n1, n2 = nodes[0], nodes[1]

    store.apply_probe_results(
        {
            n1.node_id: ProbeResult(node_id=n1.node_id, alive=True, latency_ms=120),
            n2.node_id: ProbeResult(node_id=n2.node_id, alive=False),
        }
    )

    got1 = store.get_node(n1.node_id)
    got2 = store.get_node(n2.node_id)
    assert got1.state == "alive"
    assert got1.last_latency_ms == 120
    assert got2.state == "dead"

    store.apply_probe_results(
        {n1.node_id: ProbeResult(node_id=n1.node_id, alive=False, latency_ms=999)}
    )
    assert store.get_node(n1.node_id).state == "dead"
    assert store.get_node(n1.node_id).last_latency_ms == 999

    assert len(store.load_nodes(state="alive")) == 0
    assert len(store.load_nodes(state="dead")) == 2


def test_assignment_exclusivity(store: Store) -> None:
    store.upsert_candidates([make_candidate(source="a")])
    node = store.load_nodes()[0]

    store.assign_nodes([node.node_id], "tu_a")
    assert store.node_ids_assigned_to("tu_a") == {node.node_id}
    assert store.node_ids_in_use() == {node.node_id}

    store.assign_nodes([node.node_id], "tu_b")
    assert store.node_ids_assigned_to("tu_b") == {node.node_id}
    assert store.node_ids_assigned_to("tu_a") == set()
    assert store.node_ids_in_use() == {node.node_id}


def test_release_tunnel_returns_released_count_and_frees_nodes(store: Store) -> None:
    store.upsert_candidates(
        [
            make_candidate(source="a"),
            make_candidate(
                source="b", server="5.6.7.8", uri="vless://abc@5.6.7.8:443#z"
            ),
        ]
    )
    node_ids = [n.node_id for n in store.load_nodes()]

    store.assign_nodes(node_ids, "tu_a")
    assert store.node_ids_in_use() == set(node_ids)

    assert store.release_tunnel("tu_a") == 2
    assert store.node_ids_in_use() == set()
    assert store.node_ids_assigned_to("tu_a") == set()

    store.assign_nodes(node_ids, "tu_a")
    store.unassign_node(node_ids[0])
    assert store.node_ids_assigned_to("tu_a") == {node_ids[1]}


def test_pool_counts(store: Store) -> None:
    store.upsert_candidates(
        [
            make_candidate(source="a"),
            make_candidate(
                source="b", server="5.6.7.8", uri="vless://abc@5.6.7.8:443#z"
            ),
            make_candidate(
                source="c",
                server="9.9.9.9",
                port=8443,
                uri="vless://abc@9.9.9.9:8443#w",
            ),
            make_candidate(
                source="d", server="8.8.8.8", port=80, uri="vless://abc@8.8.8.8:80#v"
            ),
        ]
    )
    nodes = store.load_nodes()
    alive_ids, dead_id = nodes[0].node_id, nodes[1].node_id
    untested_id = nodes[2].node_id

    store.apply_probe_results(
        {
            alive_ids: ProbeResult(node_id=alive_ids, alive=True, latency_ms=50),
            dead_id: ProbeResult(node_id=dead_id, alive=False),
        }
    )
    assert store.pool_counts() == {
        "total": 4,
        "alive": 1,
        "dead": 1,
        "in_use": 0,
        "untested": 2,
        "tier_a": 0,
        "tier_b": 0,
        "avg_score": None,
    }

    store.assign_nodes([alive_ids, dead_id, untested_id], "tu_a")
    assert store.pool_counts()["in_use"] == 1
    assert store.pool_counts()["total"] == 4


def test_pool_by_protocol_groups_counts(store: Store) -> None:
    store.upsert_candidates(
        [
            make_candidate(source="a"),
            NodeCandidate(
                uri="ss://x@5.6.7.8:8388",
                protocol="ss",
                server="5.6.7.8",
                port=8388,
                user="x",
                source="b",
            ),
            NodeCandidate(
                uri="ss://y@9.9.9.9:8388",
                protocol="ss",
                server="9.9.9.9",
                port=8388,
                user="y",
                source="c",
            ),
            make_candidate(source="d", server="8.8.8.8"),
        ]
    )
    nodes = store.load_nodes()
    store.apply_probe_results(
        {
            n.node_id: ProbeResult(
                n.node_id, alive=n.server != "8.8.8.8", latency_ms=10
            )
            for n in nodes
        }
    )

    by = {p["protocol"]: p for p in store.pool_by_protocol()}
    assert set(by) == {"vless", "ss"}
    assert by["vless"] == {
        "protocol": "vless",
        "total": 2,
        "alive": 1,
        "dead": 1,
        "untested": 0,
    }
    assert by["ss"] == {
        "protocol": "ss",
        "total": 2,
        "alive": 2,
        "dead": 0,
        "untested": 0,
    }


def test_load_nodes_default_ordering_state_then_latency(store: Store) -> None:
    store.upsert_candidates(
        [
            make_candidate(source="a", server="1.1.1.1"),
            make_candidate(source="b", server="2.2.2.2"),
            make_candidate(source="c", server="3.3.3.3"),
            make_candidate(source="d", server="4.4.4.4"),
        ]
    )
    nodes = store.load_nodes()
    results = {}
    for n in nodes:
        if n.server == "4.4.4.4":
            results[n.node_id] = ProbeResult(n.node_id, alive=True, latency_ms=5)
        elif n.server == "2.2.2.2":
            results[n.node_id] = ProbeResult(n.node_id, alive=True, latency_ms=300)
        elif n.server == "1.1.1.1":
            results[n.node_id] = ProbeResult(n.node_id, alive=False)
    store.apply_probe_results(results)

    order = store.load_nodes()
    assert [n.state for n in order] == ["alive", "alive", "dead", "untested"]
    alive = [n for n in order if n.state == "alive"]
    assert [n.server for n in alive] == ["4.4.4.4", "2.2.2.2"]
    assert [n.last_latency_ms for n in alive] == [5, 300]


def test_load_nodes_query_matches_host_or_node_id(store: Store) -> None:
    store.upsert_candidates([make_candidate(source="a", server="free-node.example")])
    node = store.load_nodes()[0]
    assert len(store.load_nodes(query="FREE-NODE")) == 1
    assert len(store.load_nodes(query="node.example")) == 1
    assert len(store.load_nodes(query=node.node_id)) == 1
    assert len(store.load_nodes(query="NO-SUCH")) == 0


def test_load_nodes_in_use_filter(store: Store) -> None:
    store.upsert_candidates(
        [
            make_candidate(source="a"),
            make_candidate(
                source="b", server="5.6.7.8", uri="vless://abc@5.6.7.8:443#z"
            ),
        ]
    )
    nodes = store.load_nodes()
    store.assign_nodes([nodes[0].node_id], "tu_a")

    assert {n.node_id for n in store.load_nodes(in_use=True)} == {nodes[0].node_id}
    assert {n.node_id for n in store.load_nodes(in_use=False)} == {nodes[1].node_id}
    assert len(store.load_nodes(in_use=None)) == 2
    assert store.node_ids_in_use() == {nodes[0].node_id}


def test_load_nodes_limit_applies_after_filters(store: Store) -> None:
    store.upsert_candidates(
        [
            make_candidate(source="a"),
            NodeCandidate(
                uri="ss://x@5.6.7.8:8388",
                protocol="ss",
                server="5.6.7.8",
                port=8388,
                user="x",
                source="b",
            ),
            make_candidate(source="c", server="8.8.8.8"),
            NodeCandidate(
                uri="ss://y@9.9.9.9:8388",
                protocol="ss",
                server="9.9.9.9",
                port=8388,
                user="y",
                source="d",
            ),
        ]
    )
    assert len(store.load_nodes(protocols={"ss"})) == 2
    assert len(store.load_nodes(protocols={"ss"}, limit=1)) == 1
    assert len(store.load_nodes(state="untested", limit=3)) == 3
    assert len(store.load_nodes(oldest_first=True, limit=2)) == 2


def test_record_fetch_unknown_source_is_noop(store: Store) -> None:
    store.upsert_source("ebrasha", "ebrasha/ebrasha")
    store.record_fetch("ebrasha", 123.0)
    store.record_fetch("not-seen", 999.0)
    entries = {e["name"]: e for e in store.sources_summary()}
    assert set(entries) == {"ebrasha"}
    assert entries["ebrasha"]["last_fetch_s"] == 123.0
    assert entries["ebrasha"]["next_fetch_s"] is None


def test_get_node_missing_returns_none(store: Store) -> None:
    assert store.get_node("nope") is None


def test_sources_ledger(store: Store) -> None:
    store.upsert_source("ebrasha", "ebrasha/ebrasha")
    store.upsert_source("epodonios", "Epodonios/")
    assert store.sources_summary() == [
        {
            "name": "ebrasha",
            "name_scm": "ebrasha/ebrasha",
            "last_fetch_s": None,
            "next_fetch_s": None,
        },
        {
            "name": "epodonios",
            "name_scm": "Epodonios/",
            "last_fetch_s": None,
            "next_fetch_s": None,
        },
    ]

    store.upsert_source("ebrasha", "ebrasha/new-path")
    store.record_fetch("ebrasha", 120.0)
    entries = {e["name"]: e for e in store.sources_summary()}
    assert entries["ebrasha"]["name_scm"] == "ebrasha/new-path"
    assert entries["ebrasha"]["last_fetch_s"] == 120.0
    assert entries["epodonios"]["last_fetch_s"] is None


def test_context_manager(tmp_path) -> None:
    db_path = tmp_path / "infinity.db"
    with Store(str(db_path)) as store:
        store.create_schema()
        store.save_tunnel(make_tunnel("a", 10001))
        assert store.get_tunnel("tu_a") is not None


def test_create_schema_migrates_pre_stability_databases(tmp_path) -> None:
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.executescript(_OLD_SCHEMA)
        conn.commit()

    store = Store(path)
    try:
        store.create_schema()
        cols = {
            r[1] for r in store._conn.execute("PRAGMA table_info(nodes)").fetchall()
        }
        assert {
            "probe_ok",
            "probe_total",
            "window_started_s",
            "last_probe_s",
            "last_alive_s",
            "score_f",
        } <= cols

        store.upsert_candidates([make_candidate(source="legacy")])
        node = store.load_nodes()[0]
        assert node.probe_ok == 0
        assert node.probe_total == 0
        assert node.score_f is None
    finally:
        store.close()


def test_apply_probe_results_score_f_matches_wilson(store: Store) -> None:
    from engine.stability import wilson_lower

    store.upsert_candidates([make_candidate(source="a")])
    node = store.load_nodes()[0]

    # Three distinct probe events on the same node, accumulated windowed.
    for alive in (True, True, False):
        store.apply_probe_results(
            {node.node_id: ProbeResult(node.node_id, alive=alive, latency_ms=10)},
            stability=True,
        )

    got = store.get_node(node.node_id)
    assert got.probe_total == 3
    assert got.probe_ok == 2
    assert got.state == "dead"
    assert got.last_probe_s is not None
    assert got.last_alive_s is not None  # set by the two alive probes
    assert got.score_f is not None
    assert got.score_f == pytest.approx(wilson_lower(2, 3))


def test_apply_probe_results_feature_off_leaves_counters_null(store: Store) -> None:
    store.upsert_candidates([make_candidate(source="a")])
    node = store.load_nodes()[0]

    store.apply_probe_results(
        {node.node_id: ProbeResult(node.node_id, alive=True, latency_ms=10)}
    )

    got = store.get_node(node.node_id)
    assert got.state == "alive"
    assert got.probe_total == 0
    assert got.score_f is None


def test_pool_counts_tiers_and_avg_score(store: Store) -> None:
    from engine.stability import wilson_lower

    store.upsert_candidates(
        [
            make_candidate(source="a", server="1.1.1.1"),
            make_candidate(source="b", server="2.2.2.2"),
            make_candidate(source="c", server="3.3.3.3"),
        ]
    )
    nodes = {n.server: n for n in store.load_nodes()}

    for server, ok, total in (("1.1.1.1", 90, 100), ("2.2.2.2", 20, 100)):
        store._conn.execute(
            "UPDATE nodes SET state = 'alive', probe_ok = ?, probe_total = ?, "
            "score_f = ? WHERE node_id = ?",
            (ok, total, wilson_lower(ok, total), nodes[server].node_id),
        )
    store._conn.commit()

    counts = store.pool_counts(min_probes=6, min_avail=0.4)
    assert counts["tier_a"] == 1  # 1.1.1.1: avail 0.90 >= 0.4
    assert counts["tier_b"] == 1  # 2.2.2.2: avail ~0.13 < 0.4
    assert counts["avg_score"] == pytest.approx(
        (wilson_lower(90, 100) + wilson_lower(20, 100)) / 2
    )

    # Cold (third) node has no counters; unevaluated alive nodes are in neither tier.
    assert counts["tier_a"] + counts["tier_b"] == 2
