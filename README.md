# InfinityProxy

**Universal 3D rotating proxy on demand, from free public node feeds.**

InfinityProxy is an open-source, self-hostable proxy engine. It continuously
scrapes free proxy nodes from public GitHub feeds, tests them for liveness, and
spawns *rotating proxy tunnels* on demand. Any application can request a tunnel,
get back a stable `host:port` plus credentials, and instantly start routing
traffic through fresh, alive proxy nodes — with automatic renewal when nodes die.

- **Free** — nodes are scraped from public, auto-updated open-source feeds.
- **Universal** — each tunnel is a sing-box listener that accepts **SOCKS5 and
  HTTP (CONNECT)** and dials upstream over VLESS, VMess, Shadowsocks, Trojan,
  TUIC, or Hysteria2.
- **3D rotating** — rotation happens across three axes:
  [**time**](#rotation-axes), **node switching**, and **geo/protocol diversity**.
- **Self-hosted** — runs in Docker; your nodes, your privacy. MIT licensed.

```text
                ┌────────────────────────────────────────────────┐
                │                  INFINITYPROXY                 │
                │                                                │
  public feeds  │   ┌────────────┐   ┌─────────────┐             │
 ─────────────► │   │ SCRAPER    │──►│ NODE POOL   │             │
  github 5x     │   │ (batched)  │   │ (filtered)  │             │
                │   └────────────┘   └──────┬──────┘             │
                │                           │ healthy nodes      │
                │              ┌────────────▼───────┐            │
   SomeApp ───► │              │  PROXY ENGINE      │            │
   POST /tunnels│              │  (Flask, :8000)    │            │
                │              └───────┬────────────┘            │
                │              spawns / renews / routes          │
                │        ┌───────┴────────┐                      │
                │        │  TUNNEL (sing- │  SOCKS5+HTTP, :PORT  │
                │        │    box)        │◄──────────────────────┼── client
                │        │  rotating      │  user:pass           │
                │        └────────────────┘                      │
                └────────────────────────────────────────────────┘
```

## How it works — in 30 seconds

1. **The Engine is always running.** It scrapes five public node feeds on their
   own cadences, deduplicates, and tests every candidate with a real protocol
   handshake (batches of 50, 4s timeout). Only live nodes enter the node pool.
2. **An app requests a tunnel.** `POST /tunnels` with `{ "node_count": 10,
   "auto_renew": true }`. The engine rents a free port from 10000–59999, grants
   the tunnel an exclusive private set of alive nodes, and starts a sing-box
   container that rotates through them.
3. **The app gets back connection info.** `127.0.0.1:10244` plus generated
   credentials. It wires that route into whatever it needs a proxy for.
4. **Nodes die? No problem.** The engine health-checks each tunnel's nodes every
   30s and swaps any node out after 2 failed checks, refetching from the pool.
   The `host:port` and credentials never change.

## Rotation axes

3D rotation means rotation along three independent axes:

| Axis | What rotates | Mechanism |
| --- | --- | --- |
| **Time** | Which nodes are assigned | Continuous renewal: dead nodes are replaced automatically as sources refresh |
| **Node** | Which node a single request exits through | sing-box latency-weighted (`urltest`) outbound selection per request |
| **Geo/protocol** | *Where* and *how* traffic exits | Nodes span many countries and protocols (VLESS/VMess/SS/Trojan/TUIC/Hy2), so each tunnel's address space is broad and churn is spread |

## Quick start

Requires [Docker](https://docs.docker.com/get-docker/) with Docker Compose.

```bash
git clone https://github.com/M1778/InfinityProxy.git
cd InfinityProxy
docker compose up -d
```

Wait a moment for the first scrape (up to one fetch cycle), then request a tunnel
with 10 alive nodes:

```bash
# 1. Ask the Engine for a tunnel
curl -s -X POST http://127.0.0.1:8000/tunnels \
  -H 'Content-Type: application/json' \
  -d '{"node_count": 10, "auto_renew": true}'
```

```json
{
  "id": "tu_8f3k9a",
  "host": "127.0.0.1",
  "port": 10244,
  "username": "u_5x2m",
  "password": "p_9q7z",
  "node_count_requested": 10,
  "node_count_granted": 10,
  "auto_renew": true,
  "protocols": ["http", "socks5"]
}
```

```bash
# 2. Use it like any HTTP proxy: curl exits through a live node
curl -x http://u_5x2m:p_9q7z@127.0.0.1:10244 https://api.ipify.org

# SOCKS5 also works
curl --socks5-hostname u_5x2m:p_9q7z@127.0.0.1:10244 https://api.ipify.org

# 3. Tear it down when done
curl -s -X DELETE http://127.0.0.1:8000/tunnels/tu_8f3k9a
```

> **Note:** the Engine listens on `127.0.0.1:8000` — localhost only, no auth, by
> design in v1. Don't publish it to the network.

## Configuration

Everything is overridable via environment variables (see
[`docs/api.md`](./docs/api.md#configuration) for the full table):

| Variable | Default | Purpose |
| --- | --- | --- |
| `INFINITY_PORT` | `8000` | Engine control API + dashboard port |
| `INFINITY_TUNNEL_RANGE` | `10000-59999` | Ports available for tunnels |
| `INFINITY_BATCH_SIZE` | `50` | Concurrent liveness probes |
| `INFINITY_PROBE_TIMEOUT_MS` | `4000` | Liveness handshake timeout |

## Source feeds

The pool is built from these public, auto-updated feeds (full details in
[`docs/scraping.md`](./docs/scraping.md)):

| Feed | License | Refresh | Formats |
| --- | --- | --- | --- |
| [ebrasha/free-v2ray-public-list](https://github.com/ebrasha/free-v2ray-public-list) | unlicensed | ~15 min | VLESS · VMess · SS · SSR · Trojan · TUIC · Hysteria2 |
| [Epodonios/v2ray-configs](https://github.com/Epodonios/v2ray-configs) | GPL-3.0 | ~5 min | VMess · VLESS · Trojan · TUIC · SS · SSR |
| [free-nodes/v2rayfree](https://github.com/free-nodes/v2rayfree) | unlicensed | ~6 h | SS (currently SS-dominated) |
| [gfpcom/free-proxy-list](https://github.com/gfpcom/free-proxy-list) | MIT | ~30 min | VLESS · VMess · SS · SSR · Trojan · TUIC · HTTP · SOCKS |
| [FreeFolksOn/abc-configs-free-vpn-proxy-list](https://github.com/FreeFolksOn/abc-configs-free-vpn-proxy-list) | Unlicense | ~10 min | mixed VLESS/VMess/SS/Trojan |

## Documentation

- [**Architecture**](./docs/architecture.md) — components, request flow, renewal loop, SQLite schema
- [**API reference**](./docs/api.md) — endpoints, payloads, errors, configuration
- [**Scraping & filtering**](./docs/scraping.md) — sources, batching, liveness, dedup
- [**Glossary**](./CONTEXT.md) — canonical project vocabulary
- [**Architecture decisions**](./docs/adr/) — why things are the way they are
- [**Roadmap**](./ROADMAP.md) — what's planned next
- [**Contributing**](./CONTRIBUTING.md) — build, test, and CI conventions

## License

MIT. See [LICENSE](./LICENSE).

Node data is scraped from third-party feeds under their own licenses;
see [Attribution](./docs/scraping.md#attribution).