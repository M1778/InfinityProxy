# Scraping & Filtering

The Engine's pool is built from public, auto-updated GitHub feeds. This document
covers the source manifest, the fetch loop, deduplication, and the liveness
filter that turns raw candidates into *alive nodes*.

## Source manifest

Sources are table-driven: adding or removing a feed is configuration, not code.

| Source | Raw feed(s) | License | Update cadence | Protocols | Format |
| --- | --- | --- | --- | --- | --- |
| [ebrasha/free-v2ray-public-list](https://github.com/ebrasha/free-v2ray-public-list) | `V2Ray-Config-By-EbraSha-All-Type.txt`, `separated-protocols/{vless,vmess,ss,ssr,trojan,tuic,hysteria2}_configs.txt` | unlicensed | ~15 min | VLESS, VMess, SS, SSR, Trojan, TUIC, Hysteria2 | plain, one URI per line |
| [Epodonios/v2ray-configs](https://github.com/Epodonios/v2ray-configs) | `Splitted-By-Protocol/{vless,vmess,ss,ssr,trojan}.txt`, `All_Configs_Sub.txt` | GPL-3.0 | ~5 min | VMess, VLESS, Trojan, TUIC, SS, SSR | plain + base64 combined |
| [free-nodes/v2rayfree](https://github.com/free-nodes/v2rayfree) | `sub` (base64 subscription), `vYYYYMMDD` snapshots | unlicensed | ~6 h | SS (currently SS-dominated) | base64 |
| [gfpcom/free-proxy-list](https://github.com/gfpcom/free-proxy-list) | wiki lists: `lists/{http,http2,socks4,socks5,ss,ssr,trojan,tuic,vless,vmess,wireguard}.txt` | MIT | ~30 min | VLESS, VMess, SS, SSR, Trojan, TUIC, HTTP, SOCKS | plain, per-protocol |
| [FreeFolksOn/abc-configs-free-vpn-proxy-list](https://github.com/FreeFolksOn/abc-configs-free-vpn-proxy-list) | `README.md` (latest ~30 configs) | Unlicense | ~10 min | mixed VLESS/VMess/SS/Trojan | embedded URI list |

### Fetch strategy

- Raw GitHub URLs are fetched over HTTPS from `raw.githubusercontent.com`.
- **Stream** large files (the ebrasha VLESS feed is ~5 MB+) rather than
  buffering whole payloads.
- Plain feeds are split on newlines; base64 feeds (`v2rayfree`, Epodonios'
  `All_Configs_base64_Sub.txt`) are decoded **before** splitting.
- Every line is parsed by its URI scheme (`vless://`, `vmess://`, `ss://`,
  `ssr://`, `trojan://`, `tuic://`, `hysteria2://`, `http://`, `socks5://`).
  `vmess://` links carry a base64 JSON payload; `ss://` links carry a base64
  method:password segment — decode those, don't guess fields.
- Snapshots that look mid-update (truncated base64, empty body) are skipped for
  that pass and retried next cadence, never panicking the loop.

## Fetch loop

- Each source runs on **its own cadence**, staggered so the engine is never
  hammering GitHub with everything at once (avoids rate limits and keeps the
  host's internet speed smooth).
- Cadences: ebrasha 15 min · Epodonios 5 min · v2rayfree 6 h ·
  gfpcom 30 min · FreeFolksOn 10 min.
- Feeds are fetched sequentially within a source; concurrent fetches across
  *different* sources are time-boxed and throttled so scraping never saturates
  the host network.
- A failed fetch (HTTP error, timeout) backs off one cadence; sources that are
  overdue beyond their cadence show as `stale_sources` in `GET /status`.

## Liveness filter

The pool only contains nodes that passed a **real protocol handshake** — the
server completes the node's own wire protocol against the probe, not just a
response to a ping. Since [ADR-0006](./adr/0006-relay-grade-liveness-probes.md),
that means:

- **VLESS and Trojan** (the TCP-capable majority of the pool) are probe-relayed:
  the probe sends a genuine protocol header for a benign target and then pushes a
  minimal HTTP `GET` through the tunnel. Liveness is a **full relay round-trip**:
  the server must parse the header, dial the target, and echo foreign bytes back.
  VLESS additionally requires the `0x00 0x00` response header before data; Trojan
  (no response header) requires any relayed bytes. A server that times out, whose
  first bytes are our own handshake (echoer), or whose bytes come without the
  VLESS header (an HTTP responder answering a raw `GET`) all fail.
- The relay target is `https`/TCP `80` at `www.google.com` by default, overridable
  with `INFINITY_RELAY_TARGET_HOST` / `INFINITY_RELAY_TARGET_PORT`; a target that
  answers on accept (or to the probe GET) without needing a browser is required —
  sing-box only emits the VLESS ack after the target returns bytes.
- **Shadowsocks, VMess, TUIC, Hysteria2** keep v1 gating (full AEAD/QUIC
  clients are deferred); these are not relay-certified.
- **WS/gRPC-transport nodes are rejected, not attempted**: the probe is plain
  TCP/TLS and cannot complete a WebSocket/gRPC upgrade, and a ws-fronted TLS
  server (e.g. Cloudflare Workers) answers the header handshake then closes —
  measured 0/74 requests across two ws-populated tunnels before the guard.
  Only `type=tcp` (or absent `type`) is relay-probeable. Reality-fronted VLESS
  can still fail false-negative: the stdlib probe cannot reproduce a browser
  TLS fingerprint.

| Setting | Default |
| --- | --- |
| `INFINITY_BATCH_SIZE` | 50 concurrent probes per batch |
| `INFINITY_PROBE_TIMEOUT_MS` | 4000 ms per node |
| `INFINITY_MAX_MISSES` | 2 consecutive misses ⇒ swap |
| `INFINITY_HEALTH_INTERVAL_S` | 30 s per-tunnel re-check |
| `INFINITY_PROBE_BUDGET_PER_REFRESH` | 5000 untested nodes probed per source refresh |

- Candidates queue through the filter in batches of 50; each node has 4s to
  complete a handshake.
- Each source refresh probes at most `INFINITY_PROBE_BUDGET_PER_REFRESH`
  untested nodes, oldest-scraped first, plus a bounded dead-retest sample of
  the refresh's own source. The pass never drains the whole untested queue in
  one refresh, so a single flooded feed cannot stall every other source's
  cadence; probes advance FIFO across refreshes instead.
- Every batch verdict is written to the store as soon as it completes, so
  `alive` nodes surface incrementally while a large pass still runs — the pool
  is never held back until the full queue drains.
- A node that passes is `alive` and joins the pool (fresh nodes are re-probed on
  next source cadence — free nodes churn fast).
- A node that the tunnel renderer cannot turn into a sing-box outbound (broken
  or garbage URIs, a shadowsocks cipher sing-box does not implement, or a
  TLS-only protocol such as trojan/tuic/hysteria2 without a `server_name`) is
  demoted to `dead` at admission, never assigned: a live socket is not a usable
  node, and an unsupported config detail makes sing-box refuse to start the
  whole tunnel. A node whose raw URI changes after it was admitted loses its
  verdict and is re-probed through the gate, and each source refresh re-sweeps
  the alive set for newly-unrenderable strays.
- A node that fails is `dead`, excluded from assignment, and re-probed when its
  source next refreshes it.
- Tunnel-assigned nodes are re-checked every 30s; 2 consecutive failures mark
  the node dead and trigger a swap (see [architecture.md](./architecture.md)).

## Deduplication

The same node frequently appears in several feeds (feeds aggregate each other).
The pool dedupes on the node's identity key — for most protocols the
`server:port` (+ user/uuid for VLESS/VMess) — keeping the first-seen source as
the attribution record. Duplicates never double-count towards a tunnel's
`node_count`.

## Filtering policy

- Only detected-alive nodes are ever assigned; a tunnel never receives a node
  that hasn't just passed a handshake.
- The engine **avoids** re-assigning a node that recently failed for a tunnel,
  and scheduling keeps a single node in at most one tunnel at a time (exclusive
  sets — see [ADR-0002](./adr/0002-sqlite-store.md)).
- **SSR is scraped but never assigned.** sing-box ≥ 1.6 removed the
  `shadowsocksr` outbound, so tunnels can only dial what sing-box supports:
  VLESS, VMess, Shadowsocks, Trojan, TUIC, Hysteria2, HTTP, and SOCKS5. SSR
  nodes stay in the pool's dead/untested bookkeeping and are never handed to a
  tunnel (they would make config rendering fail).

## Attribution

InfinityProxy (MIT) scrapes *facts published by* third parties; it does not
redistribute the upstream repositories. Still, good hygiene and the one copyleft
feed require attribution:

- **Epodonios/v2ray-configs** is **GPL-3.0** — if any node data from this feed
  is redistributed as a bundle (e.g. a future aggregate subscription feed, see
  [ROADMAP](../ROADMAP.md)), the aggregate must carry GPL-3.0 notice and source
  attribution in the manner GPL-3.0 requires.
- **gfpcom/free-proxy-list** is **MIT**; keep its copyright notice in any
  redistribution.
- **FreeFolksOn** is **Unlicense (public domain)**.
- **ebrasha** and **free-nodes/v2rayfree** carry **no license**; we credit them
  as the source of scraped nodes in project docs regardless.

This table is maintained in the repository so the obligations are never buried
in a generated config. The scraper records, per node, the source feed it was
first seen in, so future redistribution can stay license-traceable.