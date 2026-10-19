"""Offline unit tests for engine.scraper.fetch_text retry and size cap."""

from __future__ import annotations

import requests

from engine.scraper.fetch import fetch_text


class _FakeResponse:
    def __init__(self, chunks, status=200, raise_exc=None):
        self.chunks = chunks
        self.status_code = status
        self._raise = raise_exc

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise

    def iter_content(self, chunk_size):
        yield from self.chunks

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _patch_get(monkeypatch, responses):
    """Patch requests.get to pop responses in order; calls are counted."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("engine.scraper.fetch.requests.get", fake_get)
    return calls


def test_first_success_means_no_retry(monkeypatch):
    response = _FakeResponse([b"hello", b" world"])
    calls = _patch_get(monkeypatch, [response])
    assert fetch_text("https://example.invalid/feed") == "hello world"
    assert len(calls) == 1


def test_retries_once_after_transport_error(monkeypatch):
    calls = _patch_get(
        monkeypatch, [requests.ConnectionError("reset"), _FakeResponse([b"body"])]
    )
    assert fetch_text("https://example.invalid/feed") == "body"
    assert len(calls) == 2


def test_two_failures_return_none_without_raising(monkeypatch):
    _patch_get(
        monkeypatch,
        [requests.ConnectionError("reset"), requests.Timeout("stalled")],
    )
    assert fetch_text("https://example.invalid/feed") is None


def test_body_over_cap_returns_none(monkeypatch):
    response = _FakeResponse([b"a" * 10, b"b" * 10])
    _patch_get(monkeypatch, [response])
    assert fetch_text("https://example.invalid/feed", max_bytes=15) is None


def test_body_exactly_at_cap_is_kept(monkeypatch):
    response = _FakeResponse([b"a" * 10, b"b" * 5])
    _patch_get(monkeypatch, [response])
    assert (
        fetch_text("https://example.invalid/feed", max_bytes=15) == "a" * 10 + "b" * 5
    )


def test_retry_sleep_bounds(monkeypatch):
    sleeps = []
    monkeypatch.setattr("engine.scraper.fetch.time.sleep", lambda s: sleeps.append(s))
    calls = _patch_get(
        monkeypatch, [requests.ConnectionError("reset"), _FakeResponse([b"x"])]
    )
    assert fetch_text("https://example.invalid/feed") == "x"
    assert len(calls) == 2
    assert len(sleeps) == 1
    assert 1.0 <= sleeps[0] <= 3.0
