# ROADMAP.md · InfinityProxy

Status of this roadmap: the docs are the spec; nothing is implemented yet. This
file lists the v1 surface (implemented in docs) and the post-v1 ambitions that
survived the design interview. Unstated ideas are deliberately not here.

## v1 (specified in the docs)

- Engine with 5-source scraper, liveness filter (batch 50 / 4s), exclusive node
  assignment.
- Control API on `127.0.0.1:8000`: `POST/GET /tunnels`, `POST /tunnels/{id}/renew`,
  `DELETE /tunnels/{id}`, `GET /status`; read-only dashboard.
- Per-tunnel sing-box containers (SOCKS5 + HTTP inbound, stable credentials,
  latency-weighted rotation via sing-box `urltest`).
- Automatic renewal: 30s health checks, swap after 2 strikes, degraded tunnels
  top up to their requested count.
- SQLite persistence (tunnels + node cache) with boot reconciliation.
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
Move the read-only dashboard toward full tunnel management: create, delete,
force-renew, and inspect node latency live from the browser.

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

### Operational extras (parked)
- Prometheus-format metrics endpoint for pool/tunnel health.
- Per-tunnel connection/bandwidth limits (QoS) via sing-box policies.
- Telegram/webhook notifications on pool starvation.

### Out of scope (stated no's)
- Sourcing nodes from paywalled/paid proxy providers.
- Guaranteeing specific exit IPs (nodes are free and churn by nature).
- Breaking the stable-credentials promise to rotate exit credentials per request.