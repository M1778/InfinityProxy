"""Node pool facade: joins scraper -> filter -> db for one source refresh.

The pool only contains nodes that passed a liveness probe. Raw scraped lists
are never the pool (CONTEXT.md).
"""

from __future__ import annotations

import time

from engine.config import Settings
from engine.db import Store
from engine.filter.runner import batch_probe
from engine.models import SourceManifest
from engine.scraper import SOURCES, dedup, fetch_text, parse_feed


def seed_sources(store: Store) -> None:
    for source in SOURCES:
        store.upsert_source(source.name, source.name)


def source_by_name(name: str) -> SourceManifest | None:
    for source in SOURCES:
        if source.name == name:
            return source
    return None


def refresh_source(store: Store, settings: Settings, source: SourceManifest) -> None:
    """Fetch every raw feed of one source, parse/dedup/upsert, then probe new nodes.

    Fetch failures per URL are tolerated; a source whose feeds all fail still
    records a fetch so the cadence bookkeeping stays honest.
    """
    store.upsert_source(source.name, source.name)
    for url in source.urls:
        text = fetch_text(url, timeout_s=max(settings.probe_timeout_s * 5, 20.0))
        if text is None:
            continue
        candidates = parse_feed(text, source)
        if candidates:
            store.upsert_candidates(dedup(candidates))
    _probe_new(store, settings, source)
    store.record_fetch(source.name, time.time())


def _probe_new(store: Store, settings: Settings, source: SourceManifest) -> None:
    # Everything never probed becomes a candidate; a bounded sample of this
    # source's dead nodes gets a retest on the source's own refresh cadence
    # (docs/scraping.md: a dead node is re-probed when its source refreshes).
    untested = store.load_nodes(state="untested")
    dead = store.load_nodes(state="dead", source=source.name)
    nodes = untested + dead[: settings.batch_size]
    if not nodes:
        return
    results = batch_probe(
        nodes,
        batch_size=settings.batch_size,
        timeout_s=settings.probe_timeout_s,
    )
    store.apply_probe_results(results)


def refresh_all_due(store: Store, settings: Settings) -> None:
    """Run every source whose cadence has lapsed. Serial, so fetches stay staggered."""
    now = time.time()
    by_name = {s["name"]: s for s in store.sources_summary()}
    for source in SOURCES:
        last = by_name.get(source.name, {}).get("last_fetch_s")
        if last is None or now - float(last) >= source.cadence_s:
            refresh_source(store, settings, source)
