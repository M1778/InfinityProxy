# InfinityProxy Context

Shared vocabulary for the project. This glossary is the canonical source of
truth for domain terms; use exactly these words in code, logs, docs, and review.

## Core entities

**Proxy Engine (the Engine)**:
The always-running service that scrapes public feeds, filters nodes for
liveness, and spawns/rotates proxy tunnels on demand. It is the only always-on
component.
_Avoid_: "the server", "the proxy server", "manager"

**Proxy Tunnel (tunnel)**:
One spawned, rotating proxy endpoint. A sing-box listener on its own port
(e.g. `127.0.0.1:10244`) with stable per-tunnel credentials. This is the thing
external apps connect to and call "a proxy".
_Avoid_: "proxy server" (overloaded), "node", "instance"

**Upstream Node (node)**:
A single free proxy scraped from a source feed (a `vless://`, `vmess://`, `ss://`,
`trojan://`, `tuic://`, `hysteria2://` URI). A node is *data*, never an endpoint
a client connects to directly.
_Avoid_: "proxy", "server", "endpoint"

**Node Pool (the pool)**:
The Engine's working collection of detected-alive nodes, deduplicated across
sources. Raw scraped candidate lists are never the pool.
_Avoid_: "the list", "the feed", "cache"

**Rotating Proxy**:
The *behaviour* a tunnel provides: exit traffic rotates across time, across
nodes, and across geographies/protocols ("universal 3D rotating proxy").

**Source (feed)**:
A configured public repository that publishes fresh free-node URIs, with its own
cadence, format, and license. See [docs/scraping.md](./docs/scraping.md).
_Avoid_: "upstream", "repo" (in user-facing text)

## Lifecycle verbs

**Renew (auto-renew)**:
The Engine's act of re-testing a tunnel's assigned nodes and replacing dead ones
with fresh alive nodes from the pool. Performed continuously (every 30s) when
`auto_renew` is true, or on demand via `POST /tunnels/{id}/renew`.
_Avoid_: "refresh" (reserved for fetching), "restart" (container mechanics)

**Alive / Dead (node states)**:
Alive = passed a real protocol handshake within the probe timeout. Dead = failed,
and therefore excluded from assignment and swapped out of tunnels.
_Avoid_: "working", "online"

**Alive check (liveness probe)**:
A protocol handshake against a candidate node, run in batches with a 4s timeout.
_Avoid_: "ping" (implies ICMP, which this is not)

**Batch**:
A group of concurrent liveness probes (default 50) executed together to filter
nodes quickly without saturating the network.

**Swap**:
Replacing one dead assigned node with a fresh one during renewal. Never changes
the tunnel's host, port, or credentials.
_Avoid_: "rebalance"

**Degraded (tunnel)**:
A tunnel whose `granted_count` is below its `requested_count` because the pool
was starved at creation; the renewal loop tops it up.
_Avoid_: "poor performing", "downgraded"

## Scrape terms

**Fetch cadence**:
Per-source interval at which the Engine re-scrapes that feed (ebrasha 15 min,
Epodonios 5 min, v2rayfree 6 h, gfpcom 30 min, FreeFolksOn 10 min).

**Deduplicate**:
Collapsing duplicate nodes across feeds on their identity key (`server:port`,
plus user/uuid where the protocol requires), keeping first-seen source as the
attribution record.

## Interface terms

**Control API**:
The localhost REST/JSON interface on `127.0.0.1:8787` clients use to create,
renew, list, and destroy tunnels. Open and unauthenticated in v1 by design
(see [ADR-0003](./docs/adr/0003-localhost-control-api.md)).

**Panel**:
The localhost web dashboard on `127.0.0.1:8000` that crawls the Control API,
streams live pool/tunnel state over SSE, and proxies every engine action
(create/test/renew/delete tunnels, refresh sources, browse nodes).
Unauthenticated like the Control API (see [ADR-0007](./docs/adr/0007-web-panel.md)).

**Proxy protocol**:
The scheme a node speaks (VLESS, VMess, Shadowsocks SS/SSR, Trojan, TUIC,
Hysteria2) — distinct from the tunnel's inbound protocol.
_Avoid_: "the proxy format"

**Tunnel inbound protocol**:
What a client speaks to a tunnel: SOCKS5 and HTTP (CONNECT), authenticated with
the tunnel's stable credentials.

## Implementation notes (not domain)

Terms above are domain vocabulary. Implementation decisions and their why-live
in the [ADRs](./docs/adr/): a single SQLite store, per-tunnel sing-box
containers, latency-weighted rotation, and the MIT-with-attribution license
policy.