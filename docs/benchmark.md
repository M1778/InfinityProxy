# Production benchmark

How InfinityProxy is exercised end-to-end against the real stack, and what the
first 10-minute run found. The harness lives in `tools/prod_bench.py`; run with
an up-and-running compose stack and a warm pool:

```bash
docker compose up -d
# wait for GET /status: pool.assignable >= requested node_count
python3 tools/prod_bench.py --duration 600 --tunnels 3 --node-count 3 \
    --requests-per-min 6 --outdir bench_out
```

What it does during the window:

- Creates `--tunnels` tunnels via the control API, then for `--duration`
  seconds alternates real HTTP and SOCKS5 requests (with tunnel credentials)
  against `https://api.ipify.org` through every tunnel.
- Samples `GET /status` every 15 s and the per-tunnel node assignment every
  10 s, and tail-captures engine plus per-tunnel sing-box logs.
- Every request is recorded with a timestamp and the engine state at that
  moment, so a slow or failed request can be correlated to a probe result,
  a health swap, a source freshness, or the adjacent sing-box log line.
- Writes `results.json` (raw bundle) and `report.txt` (human summary: ok rate,
  latency percentiles, exit-IP changes per tunnel, sing-box error lines).

## First run (2026-09-07): summary

10 minutes, 3 tunnels, 3 real nodes each, 59 requests. All three sing-box
containers stayed Up for the full window — tunnel orchestration held. But
0/59 requests returned a payload:

- The HTTP leg reached sing-box and authenticated (`CONNECT` answered 200), so
  credentials and the mixed inbound work.
- Every connection then died at the outbound: urltest picked an assigned node,
  sing-box dialed it, and the node refused (e.g. a hysteria2 node that answers
  TCP but drops QUIC → `outbound/urltest: quic: ... connection refused` →
  client saw `Connection reset by peer`).
- sing-box `url-test` selects once per connection and does not fail over on a
  picked-but-missing peer, and the health loop only swaps a node after probe
  misses — but these nodes keep "passing" the probe, so the tunnel stays broken
  while the engine reports it healthy.

Root cause is the liveness model (see [`filter/probe.py`](../engine/filter/probe.py),
comment): v1 liveness is a TCP dial plus a best-effort TLS hello; any response
byte within the timeout means "alive". An open port that does not actually relay
for its protocol therefore enters the pool, and nothing downstream can recover
a tunnel from it.

## Benchmark-harness correction (runs 2 and 3)

Runs 1 and 2 under-reported: `requests` 2.34.x silently drops the
`Proxy-Authorization` header when the proxy URL's userinfo is percent-encoded,
so every HTTP-mode request reached sing-box unauthenticated
(`authentication failed, no Proxy-Authorization`) and the harness counted the
resulting `ProxyError`. The credentials token is now inserted unencoded
(engine-generated usernames/passwords are URL-safe), which moves failures to
the outbound layer where they belong. Verified three ways: curl with correct
credentials gets past CONNECT, a wrong username now reappears in sing-box's
`username=` error, and run 3's per-request breakdown switched from
`ProxyError` (30/30) to `SSLError`/`ConnectionError` (44/44).

## Second run (2026-09-07, post-fix, 20 minutes)

Warm pool of ~3,100 "alive" nodes (2,540 vless / 461 trojan / 75 ss /
3 hysteria2), 3 tunnels × 10 nodes, 44 requests, **0 ok, no exit IP ever
observed**. Every failure is now at the relay layer, and sing-box names the
cause precisely:

| count | sing-box line | meaning |
| --- | --- | --- |
| 17 | `connection download closed: unknown version: 72` | peer answered in HTTP (`0x48` = `H`); a web server, not a proxy |
| 13 | `connection download closed: unknown version: 21` | peer returned a non-protocol version byte |

So the port-open liveness filter is certifying, at scale, endpoints that answer
TCP with unrelated traffic. After auth was fixed, 44/44 of the assigned nodes
(nearly all vless) failed to relay within the window. The engine kept every
container Up the whole 20 minutes — orchestration, credentials, and scheduling
held: the pool-content quality is the sole remaining blocker.

## Improvements landed as a consequence of this run

All changes were made test-first and are covered by the offline suite; the
docs ([`scraping.md`](./scraping.md), [`architecture.md`](./architecture.md))
were updated in the same change.

1. **Probe results apply incrementally** (`engine/filter/runner.py`,
   `engine/pool.py`). A full source pass used to withhold every verdict until
   the entire serial probe run finished (~30+ min for 25k nodes), so a fresh
   engine had an empty pool for tens of minutes. Each batch now applies as it
   completes; the pool fills in the first seconds.
2. **Admission gate** (`engine/pool.py`): a node that probes alive but cannot
   be rendered into a sing-box outbound (garbage URIs, unsupported ss cipher,
   TLS-only protocol without `server_name`) is demoted to `dead` at admission
   and never assigned. A URI change after admission resets the verdict
   (`engine/db.py`), and each source refresh re-sweeps alive nodes for
   newly-unrenderable strays.
3. **Sing-box validity**: `_SS_METHODS` whitelist rejects ciphers sing-box does
   not implement, `_required_tls` rejects trojan/tuic/hysteria2 nodes without a
   `server_name`, and TLS outbounds emit `enabled: true` (`engine/tunnel/config.py`).
   Each of these previously made sing-box refuse to start and crash-loop the
   whole tunnel.
4. **No zombie tunnels** (`engine/app.py`): an unrenderable node set used to
   strand a half-created `starting` tunnel row; now it is released and rolled
   back with a clean 502 `invalid_nodes`.

## Outstanding, tracked for a future change

- Liveness still certifies any echoing TCP port. A real per-protocol relay
  handshake is the documented v2 filter; until it lands, the pool will keep
  admitting dead-ends and tunnels will route through them.
- **Failure attribution is invisible today**: the `url-test` outbound hides which
  node failed (logs say `outbound/rotator`, not the node), so even when sing-box
  reports a relay error like `unknown version: 72` the engine cannot demote the
  offender. A per-node outbound with dial-time fallback (load-balance, not
  urltest, per connection) trades a little latency optimality for a name we can
  act on — the overdue amendment to ADR-0005.
- No container-level health: a crash-looping tunnel container is not swapped
  because node probes keep passing. The scheduler needs to also treat a
  non-running tunnel container as a health miss.
- Free feeds are overwhelmingly stale or hostile (25,729 scraped → ~3,100
  pass port-open liveness → 0/44 relayed within the window). Relay-grade
  abduction still certifies sparsley: a 20-minute window over a v2-certified
  pool relayed 42/444 total, and 0/74 across two ws-transport-populated
  tunnels (the ws guard in ADR-0006 now rejects those at probe time). Expect
  low success rates from any pool regardless of engine correctness.