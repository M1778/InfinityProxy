# ADR-0009 · Stability-scored node assignment

- Status: **accepted**
- Date: 2026-09-09
- Extends: [ADR-0006](./0006-relay-grade-liveness-probes.md),
  [ADR-0008](./0008-throughput-certified-pool.md)
- Phase: 3 of 3 (shipped — Phases 2 and 3 land as one follow-up change)

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
   | `last_probe_s` | Epoch of the last verdict of any kind (freshness, Phase 2) |
   | `last_alive_s` | Epoch of the last alive verdict (reserve hook; freshness deliberately uses `last_probe_s` so a node that never passes stays cold rather than merely stale) |
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
   the window cadence (Phase 2): halved and the window restarted whenever a
   verdict arrives after `INFINITY_STABILITY_WINDOW_S`.

## Trade-offs, accepted

- **Cold nodes are deliberately second-class.** A brand-new fast node with a
  2/2 history will lose every head-to-head against an evaluated Tier-B node,
  even if the evaluated node is slow. Accepted: stability is the point of the
  ADR; freshness is the counterweight (Phase 2), hooking off `last_probe_s` so
  any recent verdict keeps a node's level while unevaluated nodes stay cold.
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
  `tier`, and `score`. `GET /status` pool counts gain `tier_a`, `tier_b`,
  `avg_score`, `working_set`, and `working_set_next_s`.
- The docs ([docs/api.md](../api.md#configuration), [docs/scraping.md](../scraping.md#liveness-filter),
  [docs/architecture.md](../architecture.md)) and this record change in the same
  commit as the engine (doc-first).
- **Phase 2 (shipped in a follow-up change):**
  - **Working set.** A daemon thread (`infinity-working-set`) selects the top
    `INFINITY_STABILITY_WORKING_SET` (256) **alive, unassigned** nodes by cached
    availability every `_CADENCE_S` (default 300s) and handshake-probes those
    due (`INFINITY_STABILITY_REPROBE_MIN_S`, default 120s). History therefore
    covers the population the assigner draws from next, independent of
    assignment. This is a query, not a table — the same query backs the
    `working_set` count in `GET /status`.
  - **Window folding.** `apply_probe_results` halves `probe_ok`/`probe_total`
    and restarts `window_started_s` whenever a verdict arrives after
    `INFINITY_STABILITY_WINDOW_S` (default 3600s). The recomputed `score_f`
    cache is written in the same UPDATE (the fold is applied in Python, then the
    tree of column expressions is re-derived from the folded counts).
  - **Freshness decay.** A node is Tier A only while `last_probe_s` is within
    `INFINITY_STABILITY_MAX_AGE_S` (default 21600s); aged nodes report Tier B
    until the working-set or admission loops re-probe them.
  - **Panel.** The pool chart adds a dashed Tier-A series and a mean-availability
    line (secondary 0–1 axis); summary cards add Tier A and the working-set
    count; the tunnels table gains the tier mix and the nodes table availability
    / score / tier columns.
- **Phase 3 (shipped in the same follow-up change):** `tools/prod_bench.py`
  now correlates every request to the assigned-node quality vector it ran
  against, and the new `tools/stability_tune.py` buckets ok-rate on tier-a
  share and mean score to decide whether the weights move. Measured findings
  (150 s run against a near-starved live pool — 628,661 scraped, 19 alive, 1
  assignable; see [docs/benchmark.md](../benchmark.md#stability-assignment-bench-adr-0009-phase-3-2026-09-08)):

  - 13 requests, 7 ok (53.8%): the correlated HTTP leg succeeded 7/7; the
    SOCKS5 leg failed 0/6 at the sing-box dial layer (`operation not
    permitted`) before any node-quality sample existed for it.
  - No tier/score separation measurable at these volumes — **no signal**. The
    conservative defaults stay: `WEIGHT_AVAIL = 0.6`, `WEIGHT_SPEED = 0.4`,
    `MIN_AVAIL = 0.4`, and the tune is re-run once the pool escapes starvation.

## Out of scope

- Per-tunnel or per-client quality guarantees; this graduates the *pool*.
- Removing the legacy `_prefer_best` path; it stays until a benchmark shows the
  scored assigner wins on real data.