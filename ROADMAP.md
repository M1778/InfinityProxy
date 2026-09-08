# ROADMAP.md · InfinityProxy

Status of this roadmap: v1 is implemented (engine + panel + CI deployed) and this
file lists the shipped v1 surface, the post-v1 ambitions that survived the design
interview, and stated no's. Unstated ideas are deliberately not here.

## v1 (implemented)

- Engine with 5-source scraper, liveness filter (batch 50 / 4s), exclusive node
  assignment.
- Control API on `127.0.0.1:8787`: `POST/GET /tunnels`, `POST /tunnels/{id}/renew`,
  `DELETE /tunnels/{id}`, `GET /status`, `GET /nodes`, `POST /sources/{name}/refresh`.
- Web panel on `127.0.0.1:8000`: live pool/tunnel graphs (SSE + Chart.js), create /
  test / renew / delete tunnels, browse nodes, refresh sources. See
  [docs/dashboard.md](./docs/dashboard.md).
- Per-tunnel sing-box containers (SOCKS5 + HTTP inbound, stable credentials,
  latency-weighted rotation via sing-box `urltest`).
- Automatic renewal: 30s health checks, swap after 2 strikes, degraded tunnels
  top up to their requested count.
- SQLite persistence (tunnels + node cache) with boot reconciliation.
- Stability-scored assignment (Phase 1, [ADR-0009](./docs/adr/0009-stability-scored-assignment.md)): windowed
  probe counters, Wilson availability + within-protocol speed percentile,
  Tier A/B/cold ordering, feature-flagged (on in live, off by default).
- MIT license with upstream attribution (see [docs/scraping.md](./docs/scraping.md)).

## Post-v1 roadmap

### Protocol filters
Let a tunnel request restrict its node set to specific protocols, e.g.
`POST /tunnels { "protocols": ["vless", "ss"] }`. The pool and assigner filter
by node scheme before handing out the exclusive set.

### Geo-aware selection
Create a tunnel limited to nodes from given countries
`{ "countries": ["DE", "FR"] }`, based on the node's host/IP geoinfo recorded
during filtering. This sharpens the **geo/diversity** rotation axis into an
explicit requirement rather than an emergent property.

### Tunnel-scoped API keys
Authenticated, remote-capable control API. Each key manages only its own
tunnels, so the engine can leave localhost and be deployed as a shared service
without exposing other tenants' tunnels. Resolves the
[ADR-0003](./docs/adr/0003-localhost-control-api.md) "path forward".

### Interactive dashboard
**Shipped** — the panel (port 8000) is a full management surface: create, delete,
force-renew, test a tunnel's egress, browse the node pool with filters, and
refresh any source, all live from the browser. Remaining ideas that are *not* in
the panel yet: per-tunnel connection graphs and historical latency per node.

### Relay-grade liveness v2
Real per-protocol relay handshakes replace the TCP-dial-plus-hello v1 probe;
v1 admitted any echoing port. **Shipped**: VLESS, Trojan, Shadowsocks-AEAD,
HTTP, and SOCKS5 are relay-certified (server response header bytes or a decrypted
or proxied 2xx/3xx target response required), and the first benchmark traced the
old failure class — HTTP responders certified alive — to a dead end. The TCP-
hello v1 path is removed entirely: VMess, TUIC, and Hysteria2 have no relay
probe (deferred AEAD/QUIC clients) so they are not certified at all. ss methods
outside the AEAD probe table (stream ciphers, 2022-blake3, plugin URIs) are
demoted, not certified. The health loop needs a container-level miss in the
same change: today a crash-looping tunnel container is invisible to node probes.

### Tunneled-failure attribution
The rotator's health-check cadence is now tunable (`INFINITY_URTEST_INTERVAL_S`,
default 30s), so sing-box's *container-side* view excludes a just-died node
within one cadence instead of the stock 3 minutes. What remains: mapping a
failed client dial *to a node* for engine-side, immediate demotion. ADR-0005's
`urltest` aggregate hides the failing peer (`outbound/rotator`), so sing-box
relay errors like `unknown version: 72` (an HTTP responder on the port) cannot
be named. A per-node outbound with dial-time fallback (`load-balance`), or a
Clash-API surface per tunnel exposing each node's `alive`/delay for the engine
to scrape, would close it.

### Smarter renew avoidance
Make renewal strictly avoid re-picking nodes that recently failed for a tunnel,
and prefer candidates whose source refreshed most recently. Harder version of
the current "don't re-assign a just-failed node" rule; needs per-tunnel failure
history in SQLite.

### Aggregate subscription feed
Publish the node pool as a single aggregated subscription link (vless/vmess/ss
URI bundle). **Attribution boundary:** this feed is a redistribution of scraped
data, so it must honor the [source licenses](./docs/scraping.md#attribution),
including GPL-3.0 obligations from Epodonios. Do not ship until the mechanism is
designed.

### Stability-scored assignment (phases 2–3)
Phase 1 (shipped) records and scores what the engine already probes. Phase 2:
a top-working-set handshake loop so pool coverage does not depend on a node
being assigned, window folding on `window_started_s`, a freshness decay from
`last_alive_s`, and tier/availability series in the panel's pool charts.
Phase 3: tune `INFINITY_STABILITY_WEIGHT_*` / `_MIN_AVAIL` from benchmark-harness
evidence instead of the conservative defaults.

### Operational extras (parked)
- Prometheus-format metrics endpoint for pool/tunnel health.
- Per-tunnel connection/bandwidth limits (QoS) via sing-box policies.
- Telegram/webhook notifications on pool starvation.

### Out of scope (stated no's)
- Sourcing nodes from paywalled/paid proxy providers.
- Guaranteeing specific exit IPs (nodes are free and churn by nature).
- Breaking the stable-credentials promise to rotate exit credentials per request.