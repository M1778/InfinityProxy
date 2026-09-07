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
- No container-level health: a crash-looping tunnel container is not swapped
  because node probes keep passing. The scheduler needs to also treat a
  non-running tunnel container as a health miss.
- Free feeds are overwhelmingly stale (25,729 scraped; single-digit genuinely
  usable after the gate). Expect low success rates from a cold pool regardless
  of engine correctness.