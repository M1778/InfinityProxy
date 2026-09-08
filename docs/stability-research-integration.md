# Stability-scored assignment: engine integration research

Research-only note. Objective: give the Engine a memory of *how a node behaves
after admission* so assignment prefers the most stable **and** fastest nodes,
instead of the current single-measurement throughput-first rule. This document
maps where the feature must plug into the current architecture, proposes a
concrete schema and scoring design, and lays out the integration surface. It is
not an ADR; the accepted decision should live in a new
[ADR-0009](./adr/0009-stability-scored-assignment.md).

## Where these features plug in today

Every mechanism the stability signal can reuse already exists. The gap is
storage and ordering, not probing.

| Concern | Current mechanism | Gap |
| --- | --- | --- |
| Probe verdicts | `batch_probe` → `apply_probe_results` writes `state`, `last_latency_ms`, `throughput_kb_s` (`engine/db.py:297`) | Verdicts are *ephemeral*: two consecutive misses only exist as in-memory strikes (`engine/scheduler.py:143-149`) and vanish on restart. No accumulation of success/failure over time. |
| Admission gate | `_admit_batch` gates on throughput floor (`engine/pool.py:69-105`), `_probe_new` probes untested + a bounded dead-retest sample (`engine/pool.py:133-153`) | A single measurement brands a node; nothing tracks whether that number holds. |
| Assignment order | `_prefer_best` sorts `(throughput NULL last, kb/s desc, tested-before-untested)` (`engine/assigner.py:51-62`) | Throughput dominates everything; a flapping fast node is re-granted as eagerly as a stable one. Latency is only a boolean tiebreak. |
| Health cadence | 30s per-tunnel handshake for *assigned* nodes (`engine/scheduler.py:135-141`); swap after `max_misses` (`engine/scheduler.py:147-155`); self-heal + `restarts_24h` (`engine/scheduler.py:179-200`) | Produces a rich stability stream for assigned nodes that is currently discarded. Unassigned alive nodes are never re-probed between source refreshes (5 min–6 h), so their verdicts go stale. |
| Rotation | sing-box `urltest` group picks the lowest-delay member per request at its own interval (`engine/tunnel/config.py:82-111`; ADR-0005) | `urltest` owns *request-time* latency. The engine owns *membership*. It must not duplicate the latency role. |
| API surfaces | `_serialize_node` (`engine/app.py:251`), `_serialize_tunnel` (`engine/app.py:203`), `engine_status` (`engine/scheduler.py:276`), `pool_counts` (`engine/db.py:323`) | No score/availability field exists anywhere. |
| Persistence conventions | Additive `ALTER TABLE` migrations threaded through `_migrate` (`engine/db.py:77-86`), set by ADR-0002 and ADR-0008 | The same additive pattern is available for new columns. |
| Config | `Settings` dataclass + `from_env` (`engine/config.py:14-91`), documented in `docs/api.md#configuration` | New knobs follow the existing `INFINITY_*` pattern. |

Two facts constrain the design:

1. **`urltest` selects by delay, not by config order**, on its own interval
   (default 3m, engine ships 30s), with a default 50 ms tolerance band
   ([sing-box urltest docs](http://sing-box.sagernet.org/configuration/outbound/urltest)).
   It never fails a picked-but-dead peer over per connection
   ([docs/benchmark.md](./benchmark.md) second run). "Fastest at request time"
   is therefore already solved per-request.
2. **Stock sing-box v1.11.6 (the pinned image) has no per-node weight, no
   `max_fail`/`max_rtt`, no config-order fallback inside `urltest`.** Those live
   only in sing-box forks
   ([sing-box-plus loadbalance](https://github.com/you10069/sing-box-plus/blob/main/docs/configuration/outbound/loadbalance.md)).
   Anything that must order nodes by quality has to happen engine-side, before
   the config is rendered — which is exactly where the assigner sits.

## Proposed scoring + storage design

### Principle: the engine owns membership, sing-box owns the fastest-now pick

The composite score decides *which* nodes get into a tunnel's `urltest` tag
list and *how long they stay before a swap*. It deliberately excludes latency:
`urltest` already re-ranks the members by delay every interval, so a latency
term in the score would fight it (a slow-stable node would lose score despite
being the only dependable member, and a fast-flappy node would enter ahead of
stable ones — the exact symptom reported). The score is a *slow-moving
membership vote*, not a request scheduler.

### Storage: reuse the `nodes` row, additive columns, no history table

A per-node history table over a ~600k pool is the wrong shape: unbounded row
growth on a single-writer WAL database, and the pool is a cache of verdicts,
not an audit log. Instead, persist **window counters** on the existing row,
following the ADR-0008 `throughput_kb_s` precedent.

Additive migration (`engine/db.py:_migrate`, one `ALTER TABLE` per missing
column):

| Column | Type | Meaning |
| --- | --- | --- |
| `probe_ok` | INTEGER | Successful probe outcomes since the window last slid |
| `probe_total` | INTEGER | All probe outcomes since the window last slid (`0` = never probed) |
| `window_started_s` | REAL | When the counter window began |
| `last_probe_s` | REAL | Last probe attempt (any verdict) — freshness |
| `last_alive_s` | REAL | Last time the node probed alive — freshness |
| `score_f` | REAL NULL | Cached composite score for API/panel display and `GET /nodes` ordering |

Six additive columns over 600k rows is negligible and migration-safe per
ADR-0002. The engine already tolerates an unmeasured `throughput_kb_s`, so a
`NULL` `score_f` (cold nodes, feature disabled) sorts last without breaking the
state machine (`NODE_STATES` in `engine/models.py:21` stays `untested/alive/dead`
— see Tier design below).

**Window sliding (the "bounded history"):** when `now - window_started_s >
INFINITY_STABILITY_WINDOW_S` (default 3600 s), fold the counters rather than
growing a log:

```
probe_ok    = probe_ok    // 2
probe_total = probe_total // 2
window_started_s = now
```

O(1) storage, geometric recency weighting, matches how free nodes churn. This is
the same halve-and-reset pattern TCP-style congestion windows use; it needs no
ring buffer or second table.

**Bounded per-tunnel "top N" working set:** not a stored table. The working set
is a *query* — the top-N alive, unassigned candidates ordered by `score_f`
(`LIMIT N` on a `state='alive' AND assigned_to IS NULL` read). Recomputed on a
slow cadence (below) and handed to the existing `batch_probe` (handshake-only,
no throughput, so it stays cheap). Default `N = 256`, i.e. ~5 batches of 50 —
the same order of traffic as one dead-retest pass already spends per refresh
(`engine/pool.py:142`).

### Scoring formula

For node *n* over its window counters:

```
avail_lb(n) = Wilson lower bound of (probe_ok, probe_total)      # 0..1
speed_pct(n) = percentile rank of throughput_kb_s(n) among the    # 0..1
               alive candidates of the SAME protocol at assign time;
               0.0 when throughput_kb_s is NULL
score(n) = w_avail * avail_lb(n) + w_speed * speed_pct(n)
```

Defaults `w_avail = 0.6`, `w_speed = 0.4`.

- **Availability term.** Probe outcomes are a Bernoulli stream with small
  sample sizes — exactly the "2 ratings" problem ranking literature addresses.
  The canonical solution is the lower bound of the [Wilson score interval]
  (https://www.evanmiller.org/how-not-to-sort-by-average-rating.html), which
  blends observed proportion with sample size so a 2/2 node does not outrank a
  95/100 node. A simpler Laplace-smoothed ratio `(ok + 1)/(total + 2)`
  ([planspace comparison](https://planspace.org/2014/08/17/how-to-sort-by-average-rating/))
  is a fair drop-in; the ADR should pick one. Wilson is recommended because
  availability estimates here are genuinely small-sample.
- **Speed term.** A *percentile within protocol*, computed at assign time over
  the in-memory candidate list, not a raw log value. Raw KiB/s is not
  comparable across protocols (vless feeds publish very different scales than
  ss), and a percentile is stable as the pool changes. Computed in the assigner
  (`engine/assigner.py:_free_nodes`) where the candidate list already loads —
  no per-row global state.
- **No latency term** (see the principle above).
- **Cold-start rule.** Nodes with `probe_total < INFINITY_STABILITY_MIN_PROBES`
  get `avail_lb` treated as a *tiebreak only* (they remain assignable under pool
  starvation, but never outrank an evaluated node). This keeps fresh certified
  nodes usable without letting one measurement dominate.
- **Freshness / decay.** A node unprobed for longer than
  `INFINITY_STABILITY_MAX_AGE_S` (default 21600 s) drops out of Tier A —
  free-node verdicts are only trustworthy for roughly a source cadence, and the
  window fold already halves confidence as it ages. `last_alive_s` feeds the
  decay gate.

### Disqualified vs. deprioritized

Two distinct outcomes, mapped onto the existing state machine — no new states:

| Outcome | Condition | Mechanism |
| --- | --- | --- |
| **Deprioritized (Tier B)** | `probe_total >= MIN_PROBES` and `avail_lb < MIN_AVAIL` (default 0.4), or still under `MIN_PROBES` | Scored below Tier A; assigner only draws Tier B when Tier A is exhausted (pool starvation keeps the degraded-tunnel contract from `docs/architecture.md#pool-starvation` intact). |
| **Disqualified (dead, swapped)** | 2 consecutive misses on an assigned node | Existing `max_misses` path (`engine/scheduler.py:147-155`) — unchanged. A Tier-B node can still be *assigned* when the pool is starved, but if it then flaps twice it is swapped and demoted to `dead` exactly as today. |

Rationale: the score should *order* and *prioritize*; hard liveness verdicts
stay with the existing probe loop. Introducing a `degraded` node state would
ripple through `pool_counts`, `load_nodes`, and the panel for no behavioral gain.

### Cadence: what refreshes stability, and how often

| Signal | Source | Cadence |
| --- | --- | --- |
| Availability, assigned nodes | Existing 30s health handshake (`engine/scheduler.py:135-141`) — the highest-value stream, currently **discarded** | 30s |
| Availability, admission | Existing admission probe (`engine/pool.py`) handshake + throughput | Per source refresh |
| Availability, unassigned top-N working set | New working-set loop (below) | `INFINITY_STABILITY_WORKING_SET_CADENCE_S` (default 300 s) |
| Speed percentile | In-memory recompute over the alive candidate list | Per working-set refresh and per assignment |
| Verdict freshness | Window fold + `INFINITY_STABILITY_MAX_AGE_S` gate | Continuous |

The working-set loop sits inside the health loop (`engine/scheduler.py:_health_loop`)
or on its own daemon thread: every working-set cadence, load the top-N alive
unassigned nodes by `score_f`, probe them handshake-only with `batch_probe`, feed
the results through the same counter-recording path, and recompute `score_f`.
A `INFINITY_STABILITY_REPROBE_MIN_S` guard (default 120 s) skips any unassigned
node probed more recently, so churny free nodes are never hammered — the same
anti-thrash intent as the bounded dead-retest in `_probe_new`
(`engine/pool.py:142-143`). Only the top-N (default 256) are re-probed, so the
cheap handshake cost is bounded to ~5 batches per cadence.

**Do not re-run throughput certification on the 30s cadence** — ADR-0008
explicitly deferred that on cost grounds, and the health loop must stay a cheap
handshake. Optional Phase 3 extension: re-certify throughput only for the top-N
working set (256 nodes, occasionally), which re-solves ADR-0008's stale-
throughput gap for exactly the nodes most likely to be assigned next.

### Interaction with `urltest` (the "complement, don't fight" check)

- `urltest` re-probes every member at `interval` (engine sets 30s) and selects
  the member with the lowest delay on an interval basis; it does not fail over a
  peer picked-but-dead per connection ([sing-box urltest docs](
  http://sing-box.sagernet.org/configuration/outbound/urltest);
  [ADR-0005](./adr/0005-latency-weighted-rotation.md)).
- Therefore: request-time latency weighting already exists and is fine. The
  engine's score is a *set-level* decision that changes slowly (on working-set
  refresh and on swap). Keeping latency out of the score means the rotator still
  leans on whatever low-latency member is currently alive, while flapping
  members are prevented from occupying set slots in the first place. A stable
  mid-latency node keeps its slot (it serves when selected); a fast node that
  dies twice still loses its slot via the existing swap path.
- The `tolerance` band (default 50 ms) means several members are often
  interchangeable per request, which is precisely why *membership* quality
  dominates *within-set* ordering quality.

## Integration impact map

### Code

| File | Change |
| --- | --- |
| `engine/db.py` | Add 6 columns in `_SCHEMA` + `_migrate` (additive `ALTER TABLE` per ADR-0002); extend `_row_to_node`; record counters in `apply_probe_results` (`:297`) — an in-window leaf `UPDATE` per verdict; extend `load_nodes` with `ORDER BY score_f DESC` and an in-window `WHERE` for the working set; `pool_counts` adds Tier-A/`avg_score` aggregates. |
| `engine/models.py` | `Node` gains the 6 fields (defaults `None`/0); keep `NODE_STATES` unchanged. |
| `engine/assigner.py` | Replace `_prefer_best` (`:51`) sort key with the composite score; compute per-protocol speed percentile in `_free_nodes`; Tier-A-then-B fill order in `assign_new`/`top_up`; cold-start tiebreak for `probe_total < MIN_PROBES`. |
| `engine/pool.py` | Admission path feeds counters automatically via `apply_probe_results`; no gate change needed (throughput floor unchanged). |
| `engine/scheduler.py` | New working-set loop in `_health_loop` (`:263`); record counters for assigned-node health verdicts (they already pass through `apply_probe_results`); `engine_status` (`:276`) gains working-set/tier stats. |
| `engine/config.py` | New `INFINITY_STABILITY_*` settings + `from_env` parsing (mirror the `throughput_enabled` pattern: `Settings()` default off for tests, `from_env` default on). |
| `engine/app.py` | `_serialize_node` (`:251`) and `_serialize_tunnel` (`:203`) add `score`/`availability`/`probe_total`/`tier`; `list_nodes` (`:48`) accepts `sort=score` (default for `state=alive` in the panel), `min_tier`; `/status` reflects new pool counters. |
| `panel/app.py` | `_metrics_from` (`:18`) adds working-set + avg-score metrics feeding the chart history; node browse and tunnel views show score/tier. |
| `tests/` | `test_assigner.py` gains scored-sort cases (existing preference tests stay green when the feature flag is off); `test_db.py` migration/round-trip; `test_pool.py` counter-recording in `apply_probe_results`. |

### Config (new `INFINITY_*` knobs)

| Variable | Default | Phase |
| --- | --- | --- |
| `INFINITY_STABILITY_ENABLED` | `1` (live), `Settings()` default off | 1 |
| `INFINITY_STABILITY_MIN_PROBES` | `6` | 1 |
| `INFINITY_STABILITY_MIN_AVAIL` | `0.4` | 1 |
| `INFINITY_STABILITY_WEIGHT_AVAIL` | `0.6` | 1 |
| `INFINITY_STABILITY_WEIGHT_SPEED` | `0.4` | 1 |
| `INFINITY_STABILITY_WINDOW_S` | `3600` | 2 |
| `INFINITY_STABILITY_MAX_AGE_S` | `21600` | 2 |
| `INFINITY_STABILITY_WORKING_SET` | `256` | 2 |
| `INFINITY_STABILITY_WORKING_SET_CADENCE_S` | `300` | 2 |
| `INFINITY_STABILITY_REPROBE_MIN_S` | `120` | 2 |

### API responses

- `GET /nodes`: each node gains `score`, `availability`, `probe_total`, `tier`;
  new params `sort=score` and `min_tier=A`; default ordering for `state=alive`
  becomes score-desc when the feature is on.
- `GET /status` → `pool`: add `tier_a`, `tier_b`, `working_set`, `avg_score`
  (computed in `pool_counts` via `AVG(score_f)` over alive rows).
- `GET /tunnels` → `nodes[]`: add `score`, `availability`, `tier` so the panel
  can show why a node holds its slot.

### Docs (repo rule: behavior change ships docs in the same change)

| Doc | Change |
| --- | --- |
| `docs/adr/0009-stability-scored-assignment.md` | **New ADR**: decision, Wilson-vs-Laplace pick, window-fold semantics, tier vs. dead distinction, urltest-membership principle, trade-offs. |
| `docs/scraping.md` | New "Stability certification" section after Liveness filter (scoring formula, window fold, working set, tier rules); extend the settings table. |
| `docs/architecture.md` | Assigner paragraph (scored sort, Tier A/B), Renewal-loop paragraph (working set cadence, counter recording, fold), SQLite-store bullets (new columns), Decisions list (+0009). |
| `docs/api.md` | Config table (`INFINITY_STABILITY_*`), `GET /nodes` params/fields, `GET /status` pool counters. |
| `docs/dashboard.md` | Panel node-list columns, stability/avg-score chart metric. |
| `CONTEXT.md` | Glossary additions: **Availability** (window success rate), **Stability score**, **Working set**, **Tier A/B**; refine the Alive/Dead entries. |
| `ROADMAP.md` | Update "Smarter renew avoidance" and "Interactive dashboard" entries; note stability scoring ships. |
| `CLAUDE.md` | Key-facts line for the stability gate next to the ADR-0008 sentence. |

## Phased rollout

**Phase 1 — "Record what we already probe" (smallest useful slice).** No new
probing. Add columns + migration; record counters from every existing verdict
(30s health loop + admission); scored `_prefer_best` with per-protocol speed
percentile and Tier A/B fill; Wilson (or Laplace) availability with the
cold-start tiebreak; `GET /nodes` fields + `sort=score`; pool counters in `/status`;
ADR-0009 + scraping/architecture/api/CONTEXT doc edits; feature-flag off keeps
today's bytes-identical behavior. This alone reorders assignment toward nodes
that have *proven* they stay up, which is the reported pain.

**Phase 2 — "Freshness for the top of the pool."** Working-set loop
(unassigned top-256 re-probed every 5 min, handshake-only, reprobe-min guard);
window fold + max-age decay; remaining `INFINITY_STABILITY_*` knobs; panel node
columns + stability chart; `GET /tunnels` per-node score/tier.

**Phase 3 — "Tune with evidence."** Extend `tools/prod_bench.py`
([docs/benchmark.md](./benchmark.md)) to correlate score thresholds with
per-request ok-rate and swap rate; choose Wilson `z`, weights, and `MIN_AVAIL`
from measured data; optionally re-certify throughput for the top-N working set
(closing ADR-0008's deferred stale-throughput gap in the narrow case); then
update ADR-0009 with the measured defaults.

## Open questions

1. **Wilson vs. Laplace.** Repo culture favors empirically-grounded choices;
   Wilson is statistically canonical for small samples
   ([Evan Miller](https://www.evanmiller.org/how-not-to-sort-by-average-rating.html))
   but is negatively biased everywhere and needs a sqrt; Laplace is O(1) and
   simpler ([planspace](https://planspace.org/2014/08/17/how-to-sort-by-average-rating/)).
   Which to lock in the ADR, and at what `z`?
2. **Recovery speed.** A node that flaps then recovers needs enough ProbeOk in
   the window to climb back into Tier A. Does the window fold/halving give the
   right hysteresis, or do we need a faster recovery multiplier?
3. **Cross-protocol fairness of the speed percentile.** Per-protocol ranking
   avoids ss being outbid by inflated vless values, but collapses intra-protocol
   variance. Verify with benchmark data before Phase 2.
4. **Failure attribution at request time.** `urltest` still hides *which* member
   failed a client dial (`outbound/rotator`, [docs/benchmark.md](./benchmark.md)
   outstanding items). Real request-time success would be a stronger stability
   signal than engine probes, but needs the load-balance/Clash-API work in the
   roadmap — out of scope here, worth sequencing after.
5. **Interaction of Tier B with starvation.** Tier-B fallback keeps degraded
   tunnels fillable, but a fleet of tunnels during pool starvation could
   re-assign the same flappy node repeatedly. Is the existing "don't re-assign a
   just-failed node" intent strong enough, or does the working set need an
   explicit per-node cooldown before re-grant?
6. **Score staleness on long-lived assignments.** Assigned nodes get fresh
   verdicts every 30s, so their scores are accurate; unassigned top-N gets fresh
   data every 5 min. Everything else decays. Is that asymmetry acceptable, or
   should `INFINITY_STABILITY_MAX_AGE_S` also gate *assigned* nodes out of
   urltest membership after the window folds them to ~0 signal?