# ADR-0009 · Stability-scored node assignment

- Status: **accepted**
- Date: 2026-09-09
- Extends: [ADR-0006](./0006-relay-grade-liveness-probes.md),
  [ADR-0008](./0008-throughput-certified-pool.md)
- Phase: 1 of 3 (see [Consequences](#consequences))

## Context

Every verdict the engine already collects is **memoryless**:

- `apply_probe_results` overwrites a node's row with the latest probe — no
  history survives.
- The health loop's swap decision lives only in the in-memory `_strikes`
  counter, which disappears on restart.
- Assignment (`_prefer_best`, ADR-0008) orders by the *last measured*
  throughput, so a single happy or unlucky 1 MiB download decides who gets
  picked for days.

The pool is therefore a slot machine: a node that was flaking for the last
three hours and a node that has been healthy for 500 probes are equivalent as
long as the last numbers match. The production symptom is tunnels whose
`urltest` rotation keeps landing on a certified-but-flaky node that tanks
latency for everyone on the tunnel.

The engine already *performs* enough probes to know better. It just throws
the verdicts away.

## Decision

Phase 1 — **record and score what the engine already probes.** No new network
traffic. The existing admission and 30s health verdicts accumulate into
windowed counters, and assignment reads a persistent stability score.

1. **Six additive `nodes` columns** (no data migration beyond ADD COLUMN):

   | Column | Meaning |
   | --- | --- |
   | `probe_total` | Verdicts accumulated in the current window |
   | `probe_ok` | Alive verdicts within the window |
   | `window_started_s` | Epoch when the window opened (fold hook, Phase 2) |
   | `last_probe_s` | Epoch of the last verdict of any kind |
   | `last_alive_s` | Epoch of the last alive verdict (freshness hook, Phase 2) |
   | `score_f` | Cached context-free score term (see below) |

2. **Availability = Wilson lower bound.** `probe_ok / probe_total` with a 95%
   two-sided Wilson score interval (`z = 1.96`). A small sample is blended
   toward the mean, so a 2/2 node never outranks a 95/100 node ("the 2 ratings
   problem"). `score_f` caches exactly this term, computed in SQL at probe time,
   so `ORDER BY score_f` and the Python re-rank agree (asserted in tests).

3. **Speed = within-protocol throughput percentile.** At assignment time the
   candidate set is ranked per protocol (raw KiB/s scales differ a lot between
   feeds) and a node's composite sorts within its tier by
   `0.6·wilson(avail) + 0.4·speed_percentile` — `INFINITY_STABILITY_WEIGHT_*`.
   A lone measured member ranks 1.0; an unmeasured node's percentile defaults to
   0.0.

4. **Latency is deliberately excluded.** sing-box `urltest` already owns the
   request-time latency axis (ADR-0005); the engine owns membership. The
   composite is a *membership* vote, not a per-request router.

5. **Tiers keep cold nodes alive.** A node with fewer than
   `INFINITY_STABILITY_MIN_PROBES` verdicts (default 6) is **cold**:
   - cold nodes contribute no availability term (a single admission download
     can never dominate a node with history), **and**
   - cold nodes sort after every evaluated node (A Tier, then B Tier, then
     cold) but stay assignable under starvation so a fresh pool can still fill
     tunnels while it accumulates evidence.
   - Tier A = evaluated with `availability ≥ INFINITY_STABILITY_MIN_AVAIL`
     (0.4); Tier B = evaluated below the floor.

6. **A flag gates the whole feature.** `Settings()` keeps `stability_enabled =
   False`; the legacy `_prefer_best` order is untouched and byte-identical, so
   unit-test behavior is unchanged. `INFINITY_STABILITY_ENABLED` (default `1`)
   turns it on for the live engine. When off, the new columns simply fill with
   zeros/NULLs and the API reports `tier`/`score` as `None`.

7. **Assignment and admission both record.** The pool's admission gate
   (`_admit_batch`) and the 30s health loop both call
   `apply_probe_results(..., stability=True)` when enabled. The counters fold at
   the window cadence; until the Phase-2 window hook lands, each verdict is a
   `+1`.

## Trade-offs, accepted

- **Cold nodes are deliberately second-class.** A brand-new fast node with a
  2/2 history will lose every head-to-head against an evaluated Tier-B node,
  even if the evaluated node is slow. Accepted: stability is the point of the
  ADR; freshness gets its own hook (`last_alive_s`) in Phase 2.
- **The composite is a heuristic, not evidence.** Weights (0.6/0.4) and the
  0.4 availability floor are initial choices; the benchmark harness should
  tune them before Phase 3 ships. Until then the defaults are conservative.
- **No new probe traffic.** Phase 1 reuses verdicts that already exist, so a
  node's score improves merely by being admitted and health-checked. The
  population coverage gap (nodes that survive sources but are never assigned
  accumulate no history) is closed in Phase 2 by the working-set loop.
- **`score_f` is a cache.** Reading `score_f` outranks re-deriving in SQL on
  every read; the Python `wilson_lower` and the SQL expression are kept in sync
  by a round-trip test.

## Consequences

- `engine/stability.py` is the single source of scoring truth; `engine/db.py`
  holds the SQL twin of `wilson_lower` used only for the `score_f` cache and
  tier counts.
- `GET /nodes` gains `sort=score` and `min_tier=A|B` (both `400` when the
  feature is off), and node objects carry `probe_total`, `availability`,
  `tier`, and `score`. `GET /status` pool counts gain `tier_a`, `tier_b`, and
  `avg_score`.
- The docs ([docs/api.md](../api.md#configuration), [docs/scraping.md](../scraping.md#liveness-filter),
  [docs/architecture.md](../architecture.md)) and this record change in the same
  commit as the engine (doc-first).
- **Phase 2 (separate change):** the top-`INFINITY_STABILITY_WORKING_SET`
  (planned 256) youngest nodes get handshake probes on a 5-minute cadence so
  history covers the whole pool; window folding (`window_started_s`) and a
  freshness decay; the panel's pools chart adds tier/availability series.
  `avg_score` and the tier counts are wired for it now.
- **Phase 3 (separate change):** tune `INFINITY_STABILITY_WEIGHT_*` and
  `_MIN_AVAIL` from benchmark-harness evidence, not defaults.

## Out of scope

- Per-tunnel or per-client quality guarantees; this graduates the *pool*.
- Removing the legacy `_prefer_best` path; it stays until a benchmark shows the
  scored assigner wins on real data.