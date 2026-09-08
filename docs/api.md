# API Reference

The Engine exposes a JSON REST control API on **`127.0.0.1:8787`** (configured
via `INFINITY_PORT`). `GET /` redirects (302) to the web panel at
`INFINITY_PANEL_BASE_URL` (default `http://127.0.0.1:8000`); the panel itself is
documented in [dashboard.md](./dashboard.md).

> **Security:** in v1 the control API and the panel are bound to localhost and
> are **unauthenticated by design**. Don't publish them to any network. See
> [ADR-0003](./adr/0003-localhost-control-api.md). Remote/authenticated control
> (tunnel-scoped API keys) is on the [roadmap](../ROADMAP.md).

All request/response bodies are JSON (`Content-Type: application/json`).
Errors use the shape `{ "error": { "code": "...", "message": "..." } }`.

## Endpoints

### `GET /status` — engine health

```bash
curl -s http://127.0.0.1:8787/status
```

```json
{
  "engine": "ok",
  "uptime_s": 86400,
  "pool": {
    "total": 480,
    "alive": 412,
    "dead": 32,
    "untested": 36,
    "in_use": 120,
    "assignable": 292,
    "tier_a": 41,
    "tier_b": 203,
    "avg_score": 0.61,
    "by_protocol": [
      { "protocol": "ss", "total": 400, "alive": 380, "dead": 12, "untested": 8 },
      { "protocol": "vless", "total": 80, "alive": 32, "dead": 20, "untested": 28 }
    ],
    "stale_sources": 0
  },
  "tunnels": {
    "active": 7,
    "degraded": 1
  },
  "sources": [
    { "name": "ebrasha", "last_fetch_s": 90, "next_fetch_s": 810 },
    { "name": "epodonios", "last_fetch_s": 12, "next_fetch_s": 288 }
  ],
  "last_fetch_per_source": { "ebrasha": "2026-09-07T02:00:00Z" }
}
```

| Field | Meaning |
| --- | --- |
| `pool.total` | Nodes currently tracked in the pool |
| `pool.alive` | Nodes that passed the liveness filter |
| `pool.dead` | Nodes that failed the liveness filter |
| `pool.untested` | Nodes not yet probed |
| `pool.in_use` | Alive nodes currently assigned to tunnels |
| `pool.assignable` | Alive nodes not assigned to any tunnel |
| `pool.tier_a` | Alive nodes evaluated at/above the availability floor (ADR-0009; `0` when the feature is off) |
| `pool.tier_b` | Alive nodes evaluated below the floor (ADR-0009; `0` when off) |
| `pool.avg_score` | Mean cached availability over alive nodes (ADR-0009; `null` when off) |
| `pool.by_protocol` | Per-protocol `total`/`alive`/`dead`/`untested` breakdown |
| `tunnels.degraded` | Tunnels with `granted_count < requested_count` |
| `stale_sources` | Sources whose fetch is overdue beyond their cadence |

### `GET /nodes` — list nodes

Server-side filtered view of the pool. The full pool can exceed 600k nodes, so
a pagination-less bounded read is used: **`limit` defaults to 200 and caps at
1000**. By default results are ordered by `state` then latency so alive nodes
surface first; when the stability feature is on (ADR-0009) and `state=alive`,
results are ordered by stability score instead (`sort=score` forces it for any
state). Every node carries an `in_use` flag (alive nodes that are assigned to a
running tunnel), and — when the feature is on — its stability `score`, `tier`,
`probe_total`, and `availability`.

```bash
curl -s "http://127.0.0.1:8787/nodes?state=alive&protocol=ss&q=5.6.7&limit=50&in_use=1"
```

| Query param | Meaning |
| --- | --- |
| `state` | `untested` \| `alive` \| `dead` |
| `protocol` | Comma-separated protocol list, e.g. `ss,vless` |
| `source` | Manifest source name |
| `in_use` | `1`/`true` — only assigned nodes; `0`/`false` — only unassigned |
| `q` | Substring match on server address or node id |
| `limit` | Max rows, 1–1000, default 200 |
| `sort` | `score` — stability order (Tier A, then B, then cold); `400` when the feature is off |
| `min_tier` | `A` — only Tier A nodes; `B` — Tier A and B (cold excluded); `400` when off or invalid |

```json
{
  "nodes": [
    {
      "id": "sha256-...",
      "protocol": "ss",
      "server": "5.6.7.8",
      "port": 8388,
      "source": "ebrasha",
      "state": "alive",
      "last_latency_ms": 45,
      "throughput_kb_s": 812,
      "in_use": false,
      "first_seen_s": "2026-09-07T02:00:00Z",
      "probe_total": 312,
      "availability": 0.82,
      "tier": "A",
      "score": 0.71
    }
  ],
  "count": 1,
  "limit": 200
}
```

`probe_total`, `availability`, `tier`, and `score` are present on every node,
but `tier`/`score` are `null` while the stability feature is off.
`availability` is the Wilson lower bound over `probe_total` verdicts; `score`
additionally blends the throughput percentile within the node's protocol
(ADR-0009).

**Errors:** `400` on non-integer or out-of-range `limit`, on `sort=score` or
`min_tier` while the feature is off, and on an invalid `min_tier`.

### `GET /nodes/{id}` — single node

Full record including the raw `uri`. **Errors:** `404`.

### `POST /sources/{name}/refresh` — force a source scrape

Queues a manifest fetch + probe pass for one source and acknowledges **202**
immediately — the scrape runs in the background (single-flight per source, so
spamming the button stacks nothing). The panel's "refresh this feed" button uses
this.

```bash
curl -s -X POST http://127.0.0.1:8787/sources/ebrasha/refresh
```

```json
{ "name": "ebrasha", "last_fetch_s": 3, "cadence_s": 900, "refreshing": true }
```

Timestamps follow the same convention as `GET /status`: `last_fetch_s` is
**seconds ago**, not an epoch; `cadence_s` is the source's refresh cadence.
`refreshing` stays `true` until the queued scrape finishes; calls made while it
runs are acknowledged (still `202`) but do not stack a second scrape.

**Errors:** `404` for an unknown source name; `502` (`code:
source_refresh_failed`) when the scrape cannot even be queued.

### `POST /tunnels` — create a tunnel

```bash
curl -s -X POST http://127.0.0.1:8787/tunnels \
  -H 'Content-Type: application/json' \
  -d '{"node_count": 10, "auto_renew": true}'
```

Body:

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `node_count` | int | `10` | Number of alive, exclusively-assigned nodes (min `1`) |
| `auto_renew` | bool | `true` | Whether the Engine health-checks and swaps nodes continuously |
| `protocols` | `["all"]` | `["all"]` | Node protocol filter, e.g. `["vless", "ss"]` *(roadmap)* |
| `countries` | `["all"]` | `["all"]` | Node country filter, e.g. `["DE", "FR"]` *(roadmap)* |

Responses **201**:

```json
{
  "id": "tu_8f3k9a",
  "host": "127.0.0.1",
  "port": 10244,
  "username": "u_5x2m",
  "password": "p_9q7z",
  "protocols": ["socks5", "http"],
  "node_count_requested": 10,
  "node_count_granted": 10,
  "auto_renew": true,
  "created_at": "2026-09-07T02:01:00Z",
  "state": "running"
}
```

> **Degradation:** if the pool has fewer qualified nodes than requested, the
> tunnel is still created with what's available — `node_count_granted` will be
> lower and the renewal loop tops the tunnel up. This is **not** an error.

Errors: `400` on invalid body (e.g. `node_count: 0`).

### `GET /tunnels` — list tunnels

```bash
curl -s http://127.0.0.1:8787/tunnels
```

```json
{
  "tunnels": [
    {
      "id": "tu_8f3k9a",
      "host": "127.0.0.1",
      "port": 10244,
      "username": "u_5x2m",
      "password": "p_9q7z",
      "node_count_requested": 10,
      "node_count_granted": 9,
      "auto_renew": true,
      "state": "running",
      "degraded": true,
      "health": { "last_check_s": 18, "dead_swapped_24h": 3, "restarts_24h": 1 },
      "nodes": [
        { "id": "nd_1", "protocol": "vless", "latency_ms": 140, "availability": 0.82, "probe_total": 312, "tier": "A", "score": 0.71 },
        { "id": "nd_2", "protocol": "ss", "latency_ms": 220, "throughput_kb_s": 812, "availability": 0.13, "probe_total": 120, "tier": "B", "score": 0.12 }
      ]
    }
  ]
}
```

### `POST /tunnels/{id}/renew` — force renewal

```bash
curl -s -X POST http://127.0.0.1:8787/tunnels/tu_8f3k9a/renew
```

Re-tests all assigned nodes immediately and swaps every dead one, then tops the
tunnel up toward its requested count. Credentials, host, and port are
**unchanged**.

```json
{
  "id": "tu_8f3k9a",
  "swapped": 2,
  "added": 1,
  "node_count_granted": 10,
  "next_check_s": 30
}
```

Errors: `404` unknown tunnel id.

### `DELETE /tunnels/{id}` — destroy a tunnel

```bash
curl -s -X DELETE http://127.0.0.1:8787/tunnels/tu_8f3k9a
```

Stops and removes the tunnel container, frees the port, and releases its nodes
back to the pool. Responds **204** with an empty body. Errors: `404`.

### `GET /` — panel redirect

Responds **302** to the web panel at `INFINITY_PANEL_BASE_URL` (default
`http://127.0.0.1:8000`). The engine itself serves no web UI anymore — the
dashboard is the panel (see [dashboard.md](./dashboard.md)).

## Configuration

All overridable via environment variables (defaults in brackets).

| Variable | Default | Description |
| --- | --- | --- |
| `INFINITY_PORT` | `8787` | Engine control API bind port |
| `INFINITY_HOST` | `127.0.0.1` | Engine bind address (keep loopback in v1) |
| `INFINITY_PANEL_PORT` | `8000` | Panel bind port |
| `INFINITY_PANEL_HOST` | `127.0.0.1` | Panel bind address (keep loopback in v1) |
| `INFINITY_ENGINE_URL` | `http://127.0.0.1:8787` | Panel → engine base URL; tracks `INFINITY_PORT` unless set explicitly |
| `INFINITY_PANEL_BASE_URL` | `http://127.0.0.1:8000` | Engine `GET /` redirect target; the public-facing panel URL |
| `INFINITY_TUNNEL_RANGE` | `10000-59999` | Ports allocatable for tunnels, `"start-end"` |
| `INFINITY_BATCH_SIZE` | `50` | Concurrent liveness probes per batch |
| `INFINITY_PROBE_TIMEOUT_MS` | `4000` | Per-node handshake timeout |
| `INFINITY_PROBE_BUDGET_PER_REFRESH` | `5000` | Untested nodes probed per source refresh |
| `INFINITY_HEALTH_INTERVAL_S` | `30` | Per-tunnel health-check interval |
| `INFINITY_MAX_MISSES` | `2` | Consecutive failures before a node is swapped |
| `INFINITY_URTEST_INTERVAL_S` | `30` | Per-tunnel sing-box `urltest` health-check cadence; the rotator re-probes its nodes at this rate and excludes dead ones from selection (clamped to ≥ 10s, sing-box's minimum) |
| `INFINITY_PANEL_TEST_URL` | `https://api.ipify.org` | URL the panel dials through a tunnel to prove egress |
| `INFINITY_THROUGHPUT_ENABLED` | `1` | Measure a download through each fresh node at admission and gate the pool on its rate (ADR-0008). Set `0`/`false`/`no` to admit on handshake only |
| `INFINITY_THROUGHPUT_MIN_KB_S` | `200` | Admission floor: nodes certified below this many KiB/s are demoted at probe time |
| `INFINITY_THROUGHPUT_SAMPLE_BYTES` | `1048576` | Bytes the probe reads from the throughput target per node (1 MiB default) |
| `INFINITY_THROUGHPUT_TIMEOUT_S` | `12` | Per-node cap on the throughput download; a node that cannot deliver the sample in this window drops below the floor |
| `INFINITY_THROUGHPUT_HOST` / `_PORT` / `_PATH` | `speedtest.tele2.net` / `80` / `/1MB.bin` | Throughput target the probe downloads through each node |
| `INFINITY_STABILITY_ENABLED` | `1` | Accumulate windowed probe counters and assign nodes by stability score (ADR-0009). Set `0`/`false`/`no` for the legacy throughput-first order |
| `INFINITY_STABILITY_MIN_PROBES` | `6` | Verdicts a node needs before it is *evaluated*; fewer probes = cold, sorted after every evaluated node |
| `INFINITY_STABILITY_MIN_AVAIL` | `0.4` | Availability floor separating Tier A (reliable) from Tier B |
| `INFINITY_STABILITY_WEIGHT_AVAIL` | `0.6` | Weight of the Wilson availability term in the membership score |
| `INFINITY_STABILITY_WEIGHT_SPEED` | `0.4` | Weight of the within-protocol throughput-percentile term |
| `INFINITY_DB` | `infinity.db` | SQLite file path |

Source cadences are per-source (see [docs/scraping.md](./scraping.md#source-manifest)).

## Error codes

| Code | HTTP | Meaning |
| --- | --- | --- |
| `invalid_request` | 400 | Malformed JSON or out-of-range params |
| `not_found` | 404 | Unknown tunnel or node id, unknown source name |
| `conflict` | 409 | Operation not valid for the tunnel state (e.g. renew while deleting) |
| `port_exhausted` | 503 | No free ports left in `INFINITY_TUNNEL_RANGE` |
| `source_refresh_failed` | 502 | Manual source scrape could not be queued |
| `tunnel_runtime` | 503 | Cannot spawn/roll a tunnel: docker daemon refused the operation (e.g. overlay mount busy); the tunnel is rolled back and no nodes are left stranded |
| `dial_failed` | 502 | Panel egress test (`POST /tunnels/{id}/test`) could not connect through the tunnel (all its assigned nodes dead, or the tunnel is down) |
| `internal` | 500 | Unexpected engine failure |

## Example session

```bash
# create
CREATE=$(curl -s -X POST http://127.0.0.1:8787/tunnels -d '{"node_count":5,"auto_renew":true}')
PORT=$(echo "$CREATE" | python3 -c "import sys,json;print(json.load(sys.stdin)['port'])")
USER=$(echo "$CREATE" | python3 -c "import sys,json;print(json.load(sys.stdin)['username'])")
PASS=$(echo "$CREATE" | python3 -c "import sys,json;print(json.load(sys.stdin)['password'])")

# use over HTTP and SOCKS5
curl -x "http://$USER:$PASS@127.0.0.1:$PORT" https://api.ipify.org
curl --socks5-hostname "$USER:$PASS@127.0.0.1:$PORT" https://api.ipify.org

# forced renewal
curl -s -X POST "http://127.0.0.1:8787/tunnels/$(
  echo "$CREATE" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])"
)/renew"

# teardown
curl -s -X DELETE "http://127.0.0.1:8787/tunnels/$(
  echo "$CREATE" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])"
)"
```