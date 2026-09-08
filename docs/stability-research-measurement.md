# Stability measurement — research findings

Status: research note. This is *not* a decision. It lays out metric/storage/scoring
options for judging how *stable* a node is (not just whether it is alive right
now), grounded in primary sources, and ends with a recommendation shaped to this
engine's constraints (~600k tracked rows, 30s health loop, admission throughput
cert). A future ADR would supersede this note.

## Problem restated

The engine's admission gate certifies that a fresh node completed one relay
round-trip *and* one 1 MiB download at or above the throughput floor (ADR-0008,
`INFINITY_THROUGHPUT_MIN_KB_S=200`). That is a point-in-time snapshot. The
research literature is blunt about what a point-in-time pass means for free
proxies: fewer than 2% of announced proxies are reachable and correct at any
time, roughly half of the ones that work stop working within a few days, and the
median observed lifetime is only ~15 days when the data is interpreted
conservatively (75 with more lenient attribution) (Perino et al.). Without a
*history* signal the pool keeps re-admitting short-lived nodes, and the 30s
`urltest` rotation keeps offering them to clients — which is exactly what the
first benchmark run measured (most nodes dead by the time tunnels were placed,
0/59 requests ok). We need cheap, persistent per-node history that can separate
"reliably fast", "sluggish but stable", and "about to die", and rank by it —
and it must survive restarts, which today's in-memory `_strikes` does not.

Today the loops already exist but forget nothing relevant:

- **Admission:** relay-grade probe + throughput cert at first probe
  (`engine/pool.py:50`, `engine/pool.py:69`, ADR-0006, ADR-0008).
- **Health:** handshake-only probe every `health_interval_s` (30s) on assigned
  nodes; failure increments an in-memory `_strikes[(tunnel, node)]`, a success
  clears it, and a node is unassigned after `max_misses` (2) consecutive misses
  (`engine/scheduler.py:116-155`).
- **Resurfacing:** dead rows are re-probed in a bounded slice on each source
  refresh, so a healed node can come back (`engine/pool.py:133-143`).
- **Rotation:** sing-box `urltest` outbound picks the latency-winner every
  `urltest_interval_s` (30s; stock urltest interval is 3m) (ADR-0005,
  `engine/tunnel/config.py:97`).

The gap is not cadence — it is that nothing about a node's *past* influences
state, ranking, or pruning.

## What the literature says

### Free-proxy ecosystems churn hard and in classes

Perino, Varvello, Soriente, "Long-term Measurement and Analysis of the Free
Proxy Ecosystem" (ACM, monitored for months across multiple families):

- Fewer than 2% of *announced* proxies are reachable and correctly proxy.
- About half of working nodes stop working within a few days ("a fifth of the
  available proxies disappear each day", per the paper's summary of turnover).
- Median lifetime is ~15 days when only *unambiguously* attributed nodes count,
  ~75 days with lenient attribution; ~55% of nodes are available for their whole
  connected lifetime.

Mehanna et al., "Free Proxies Unmasked" (arXiv:2403.02445, 30-month daily tests
of ~640k proxies):

- Only ~3.36% of proxies verify working in a given run; only ~34.5% are ever
  active at least once during the study.
- Proxies fall into five activity classes by share of days active: Active
  (>90%), Intermittent (50–90%), Rarely active (10–50%), Short-lived (≤10%),
  Never active.
- A short failure streak is not death: about half of inactive proxies resume,
  up to ~10 days after last activity, and ~22% of nodes take more than 10 days
  to become active at all.
- Protocol is a strong prior: HTTP proxies are far more stable than SOCKS.

### Production systems converge on streaks + rolling windows + peer-relative deviation

Envoy outlier detection (active health checks):

- **Consecutive failures**: eject after N consecutive 5xx/failed probes.
- **Temporal success rate**: compare a host's success rate in a rolling window
  against the *pool's* mean; a host is an outlier when its rate falls below
  `mean − stdev × success_rate_stdev_factor` — the z-score-style, peer-relative
  threshold that needs no hand-tuned absolute value.
- **Guards**: `success_rate_request_volume` (min requests in the window, default
  100) and `success_rate_minimum_hosts` (min comparable hosts, default 5) stop
  low-traffic populations from flapping.
- **Ejection backoff**: ejected hosts return after `base_ejection_time ×`
  number of consecutive ejections — exponential growth, success-dependent.

Netflix Hystrix (client-side circuit breaker, the canonical rolling-window
implementation):

- Metrics roll in a 10s window; the circuit opens only when requests exceed
  `requestVolumeThreshold` (default 20) *and* error percentage exceeds
  `errorThresholdPercentage` (default >50%). The sample-size floor is the point
  — a single failure in a window must never trip anything.

RFC 3550 §6.4.1 (RTP interarrival jitter):

- Defines jitter as an exponentially-weighted running average with a 1/16
  smoothing factor: `J += (|D| − J) / 16`. The RFC explicitly positions jitter
  as "a second short-term measure of network congestion ... may indicate
  congestion before it leads to packet loss." It is a standard, compact way to
  fold variability into a single number.

### Rotation internals

sing-box `urltest` (ADR-0005's basis): selects the outbound with the lowest
latency to a fixed test URL, default interval 3 minutes, default URL
`https://www.gstatic.com/generate_204`. Our 30s interval is already far more aggressive than stock, which strengthens the case for feeding urltest a
*candidate set pre-sorted by stability* rather than relying on it to filter.

### Throwaway-of-the-day tooling

- `scrapy-rotating-proxies`: marks a proxy dead after failures and skips it,
  retrying after a growing delay; a success marks it good again.
- `gproxynet/proxyspin`: benches a proxy after N consecutive failures with
  exponential backoff and unbenches on a success; treats HTTP 403/407/429 as
  proxy failures, not client errors. (Practical precedent for counting
  session-level "proxy refused" as the same signal as a dead check.)

## What could work

| # | Approach | Mechanism | Data needed | Cost | False-positive risk | Primary source |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | **Consecutive-failure streak + exponential backoff** | Bench node after N straight health-check misses; re-test after `delay × 2^k`; one success resets streak. Formalizes what `max_misses=2` already half-does. | `consecutive_failures` (persisted), `last_fail_s`, `backoff_exp` | ~2 columns | A local network blip benches a good node; capped backoff + streak-reset-on-success contain it | Envoy (consecutive). scrapy-rotating-proxies, proxyspin |
| 2 | **Rolling-window success rate (EWMA or bucketed)** | Success fraction over a sliding window; low-volume nodes protected by a min-sample floor. | `ok_count`, `fail_count` over a retention window (or one EWMA float) | 1–2 columns | Single-sample windows trip on noise → floor required (Hystrix volume, Envoy request volume) | Hystrix rolling window + thresholds; Envoy temporal success rate |
| 3 | **Latency jitter (EWMA of round-trip variation)** | RFC 3550-style smoothed jitter per node; catches congestion/volatility before failures appear. | `jitter_ema_ms` (1 float, updated per probe) | 1 column | Far-geo high-latency nodes carry intrinsic jitter → use as tiebreaker, not gate | RFC 3550 §6.4.1 |
| 4 | **Activity-class prior** | Classify by lifetime shape (Never / Short-lived / Rarely / Intermittent / Active) from daily alive-bits; use class to weight ranking and to prune. | `days_seen`, `days_alive`, `class` (2 counters + 1 label); daily bit optional | ~3 columns | A young healthy node is mis-classified → weight only after ≥N days of history | Mehanna et al. (5 classes, protocol prior) |
| 5 | **Peer-relative deviation (z-score vs pool mean)** | Downrank nodes whose success rate (or latency) sits far below the current pool mean ± k·stdev — the threshold is relative, so it survives pool composition drift. | Streaming mean/stdev per protocol over candidates | O(1) per node, recomputed at ranking time | Pool heterogeneity inflates stdev → compare within protocol (HTTP vs SOCKS prior) | Envoy `success_rate_stdev_factor` |
| 6 | **Throughput variance / EMA over time** | Keep a small throughput history (or EMA) instead of one admission snapshot; detect collapse trend. | `throughput_ema_kb_s` + `throughput_samples` | ~1 float + small counter; ring of ~5 values for alive nodes only | Transient network conditions → require ≥2 samples, score relative drop | ADR-0008 rationale; Perino et al. turnover |

Every row is a *single-node aggregate*, not an event log — that keeps the store
at 600k rows regardless of how many observations we make.

## Recommendation for a proxy churn pool

Fit to this engine's three hard constraints: ~600k+ tracked rows (`docs/api.md`),
a handshake-only 30s health loop (we do not want to burn probe budget or
bandwidth re-downloading 1 MiB samples every loop), and ADR-0008's throughput
gate on admission. The proposal is five layers, each independently adoptable:

**1. Persist the streak (start here).** Promote the in-memory `_strikes`
(`engine/scheduler.py:143-150`) into node columns: `consecutive_failures`,
`last_fail_s`. A success in a health probe or admission probe resets
`consecutive_failures` to 0; a miss increments it. This alone turns the swap
rule into data that survives restarts and that other layers can read, and it
terminates the "which node was flapping before the reboot?" class of bugs.

**2. Add cheap aggregates, one row per node.** Extend the `nodes` table (one
`UPDATE` per probe event, no new tables) with:

- `ewma_ok` — rolling success fraction (approach 2), updated in place;
- `jitter_ema_ms` — RFC 3550-style smoothed round-trip variation (approach 3),
  fed by `last_latency_ms` on every probe and health pass;
- `throughput_ema_kb_s` + `throughput_samples` — the ADR-0008 admission value
  folded into an EMA, so one great 1 MiB download stops outranking a node that
  is boringly, reliably fast (approach 6);
- `days_seen`, `days_alive` — the class prior (approach 4).

Keep ring buffers (last ~16 latencies, last ~5 throughput samples) *only* for
rows in state `alive`; `untested` and `dead` rows get aggregates only. No table
grows with observation count.

**3. Retention that respires.** Dead/never-active rows are currently re-probed
in bounded slices each refresh (`engine/pool.py:142-143`) but never evicted.
Because ~22% of healthy proxies take >10 days to wake up at all (Mehanna),
prune "never alive" rows only after a grace period **longer** than that ceiling
(≥14 days), and let the activity class of dead rows decide between "evict",
"keep aggregate, re-test slowly", and "resurrect now". This bounds the table
while keeping the pool's ability to re-discover healed nodes.

**4. Score for the assigner, then let urltest pick.** Keep ADR-0008's
throughput floor as the *gate* (no history), then rank candidates for
`assigner._prefer_best` and per-tunnel configs by a stability-weighted score:

```
score = w_thr · norm(throughput_ema)        # approach 6
      − w_jit · norm(jitter_ema)            # approach 3
      + w_ok  · rel(ewma_ok)                # approach 2, peer-relative (approach 5)
      · class_weight(node)                  # approach 4, only after ≥N days
```

All weights relative and fitted to proxy-churn data (see open questions). The
history decides *which* nodes the sing-box `urltest` outbound (ADR-0005) sees;
urltest keeps picking the live, lowest-latency winner among them at 30s. "Fast
right now but dead tomorrow" must not rank above "half the speed, alive all
week" — the first benchmark run proved urltest alone cannot make that call.

**5. Respect the guards.** Copy the anti-flapping floors literally from the
production systems: a Hystrix-style sample floor (volume ≥ some N in the
window) and Envoy's `success_rate_minimum_hosts` / `success_rate_request_volume`
before any score changes. A 30s handshake loop on a 1-node tunnel must not
oscillate a rank on two samples.

## Open questions

- **Session-level signals.** Should real client failures count toward the
  streak? proxyspin treats proxy-returned 403/407/429 as proxy failures; our
  health loop only sees probe outcomes (`engine/scheduler.py:136-141`). We have
  no agreed error taxonomy for "the proxy refused a session" vs "the client's
  request was bad". This is a prerequisite for feeding the streak anything but
  probes.
- **Retest scheduling.** Dead rows resurface at up to `batch_size` (50) per
  source refresh, stampede-shaped, with no delay logic. Is a dedicated,
  backoff-paced retest lane (approach 1) better than the FIFO slice, and does
  it coexist with the probe budget (`probe_budget_per_refresh`)?
- **Re-certification cost.** `throughput_ema` wants periodic samples, but a
  1 MiB download every health round would gut the probe budget. Is a small,
  bounded sample (e.g. 256 KiB every Nth health pass, or only on admission and
  after a streak reset) enough, or does EMA on latency alone cover it?
- **Deadline/10-day tension.** Mehanna's "~22% take >10 days to activate" pushes
  retention up; memory budget at 600k rows pushes it down. Where is the measured
  sweet spot for *this* feed set?
- **urltest interplay.** sing-box's urltest runs its own health checks inside
  the tunnel container while the engine probes from outside. Two health
  judgements on the same node can disagree; do we want the engine's stability
  history to *influence* urltest's candidate ordering (via config render order)
  or to *preempt it* (swap-before-urltest)?
- **Do the EWMA smoothing factors need calibration?** RFC 3550's 1/16 is tuned
  for RTP congestion; proxy-churn wants a different α (probably faster decay to
  notice "about to die" within the 30s loop). These are empirical, benchmarkable
  choices — the benchmark harness in `docs/benchmark.md` is the right place to
  measure them first.

## Sources and access notes

- Perino, Varvello, Soriente — "Long-term Measurement and Analysis of the Free
  Proxy Ecosystem", ACM `https://dl.acm.org/doi/fullHtml/10.1145/3360695`.
  Direct HTML fetch returns 403; the facts above were extracted from the paper's
  abstract, figures, and search-indexed excerpts. Verify against the PDF before
  an ADR leans on specific numbers.
- Mehanna et al. — "Free Proxies Unmasked: A Longitudinal Study of Free Proxy
  Servers", 30-month study, `https://arxiv.org/abs/2403.02445`.
- Envoy outlier detection — `https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/outlier`
- Netflix Hystrix circuit breaker + rolling window semantics via Spring Cloud
  reference, `https://cloud.spring.io/spring-cloud-netflix/multi/multi__circuit_breaker_hystrix_clients.html`
- RFC 3550 §6.4.1 (interarrival jitter), `https://www.rfc-editor.org/rfc/rfc3550`
- sing-box urltest outbound — `https://sing-box.sagernet.org/configuration/outbound/urltest/`
- scrapy-rotating-proxies — `https://github.com/TeamHG-Memex/scrapy-rotating-proxies`
- gproxynet/proxyspin — `https://github.com/gproxynet/proxyspin`