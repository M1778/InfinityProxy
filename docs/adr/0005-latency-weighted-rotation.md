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