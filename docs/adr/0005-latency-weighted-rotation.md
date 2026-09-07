# ADR-0005: Latency-weighted rotation via sing-box `urltest`

Status: **accepted**

Tunnels choose an exit node per request using sing-box's `urltest` outbound
group over the tunnel's assigned nodes, rather than round-robin or uniform
random.

`urltest` continuously measures each outbound and selects the lowest-latency
path per connection, which matches the rotation goal: traffic spreads across
nodes in practice while leaning on the fastest alive ones — a natural fit for
churny free nodes. It comes built into sing-box, so no custom selection code and
no extra moving parts at the tunnel layer.

**Considered options:**
- *Uniform random* — no bias, but ignores health/latency; slow or congested
  nodes get equal share.
- *Round-robin* — pure cycling, same latency blindness.
- *Engine-side weighted routing per request* — would put the Engine in the data
  path and reintroduce a single point of failure. Rejected.

**Consequences:**
- "Latency-weighted" is approximate (urltest's interval-based probing, not
  per-request recomputation). Layering a strict avoidance policy over it in a
  later tune is possible, per the [roadmap's smarter renew](../ROADMAP.md).
- The rotator's health-check cadence is set to `INFINITY_URTEST_INTERVAL_S`
  (default 30s, clamped to sing-box's 10s minimum). sing-box's stock cadence is
  3 minutes: a free node that dies right after certification would stay
  *selectable* for up to 3 minutes of client traffic — far longer than the
  engine's 30s probe loop. A short cadence makes the rotator self-heal: the
  failed node falls out of selection on the next check, independent of an
  engine-side config rewrite (which still removes it at `max_misses`).
- A single request that lands on a node which died *between* checks still
  fails: urltest never retries a failed dial on another peer. Per-node
  dial-time fallback (`load-balance`) or per-node failure attribution
  (Clash-API surfacing of each outbound's state) is the remaining
  [tunneled-failure attribution](../ROADMAP.md) item and deliberately out of
  scope here.