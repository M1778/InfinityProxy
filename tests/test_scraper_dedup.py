"""Offline unit tests for engine.scraper.dedup."""

from __future__ import annotations

from engine.models import NodeCandidate
from engine.scraper.dedup import dedup


def _cand(
    server: str, port: int, user: str | None, protocol: str = "http"
) -> NodeCandidate:
    return NodeCandidate(
        uri="test-uri",
        protocol=protocol,
        server=server,
        port=port,
        user=user,
        source="test",
    )


def test_drops_later_duplicates_keeps_first():
    first = _cand("1.2.3.4", 8080, "alice")
    second = _cand("1.2.3.4", 8080, "alice")
    second_altered = _cand("1.2.3.4", 8080, "alice", protocol="ss")
    out = dedup([first, second, second_altered])
    assert len(out) == 1
    assert out[0] is first


def test_preserves_stable_order():
    a = _cand("1.2.3.4", 1, None)
    b = _cand("2.2.2.2", 2, None)
    a2 = _cand("1.2.3.4", 1, None)
    c = _cand("3.3.3.3", 3, None)
    out = dedup([a, b, a2, c])
    assert out == [a, b, c]


def test_distinct_users_on_same_host_port_are_kept():
    a = _cand("1.2.3.4", 8080, "alice")
    b = _cand("1.2.3.4", 8080, "bob")
    out = dedup([a, b])
    assert out == [a, b]


def test_same_host_different_ports_are_kept():
    a = _cand("1.2.3.4", 8080, None)
    b = _cand("1.2.3.4", 8081, None)
    out = dedup([a, b])
    assert out == [a, b]


def test_none_user_deduped_like_empty_user():
    a = _cand("1.2.3.4", 8080, None)
    b = _cand("1.2.3.4", 8080, "")
    out = dedup([a, b])
    assert len(out) == 1


def test_empty_input():
    assert dedup([]) == []


def test_does_not_mutate_input():
    items = [_cand("1.2.3.4", 8080, "alice"), _cand("9.9.9.9", 1080, None)]
    snapshot = list(items)
    dedup(items)
    assert items == snapshot
