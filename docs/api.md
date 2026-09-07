# API Reference

The Engine exposes a JSON REST control API on **`127.0.0.1:8000`** (configured
via `INFINITY_PORT`). The same Flask app serves a read-only web dashboard at
`GET /`.

> **Security:** in v1 the control API is bound to localhost and is **unauthenticated
> by design**. Don't publish it to any network. See
> [ADR-0003](./adr/0003-localhost-control-api.md). Remote/authenticated control
> (tunnel-scoped API keys) is on the [roadmap](../ROADMAP.md).

All request/response bodies are JSON (`Content-Type: application/json`).
Errors use the shape `{ "error": { "code": "...", "message": "..." } }`.

## Endpoints

### `GET /status` — engine health

```bash
curl -s http://127.0.0.1:8000/status
```

```json
{
  "engine": "ok",
  "uptime_s": 86400,
  "pool": {
    "total": 480,
    "alive": 412,
    "in_use": 120,
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
| `pool.in_use` | Alive nodes currently assigned to tunnels |
| `tunnels.degraded` | Tunnels with `granted_count < requested_count` |
| `stale_sources` | Sources whose fetch is overdue beyond their cadence |

### `POST /tunnels` — create a tunnel

```bash
curl -s -X POST http://127.0.0.1:8000/tunnels \
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
curl -s http://127.0.0.1:8000/tunnels
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
      "health": { "last_check_s": 18, "dead_swapped_24h": 3 },
      "nodes": [
        { "id": "nd_1", "protocol": "vless", "latency_ms": 140 },
        { "id": "nd_2", "protocol": "ss", "latency_ms": 220 }
      ]
    }
  ]
}
```

### `POST /tunnels/{id}/renew` — force renewal

```bash
curl -s -X POST http://127.0.0.1:8000/tunnels/tu_8f3k9a/renew
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
curl -s -X DELETE http://127.0.0.1:8000/tunnels/tu_8f3k9a
```

Stops and removes the tunnel container, frees the port, and releases its nodes
back to the pool. Responds **204** with an empty body. Errors: `404`.

### `GET /` — dashboard

Read-only HTML/CSS inspector: tunnel list, pool health, per-source last-fetch
status. No editing actions in v1 (see [ROADMAP](../ROADMAP.md)).

## Configuration

All overridable via environment variables (defaults in brackets).

| Variable | Default | Description |
| --- | --- | --- |
| `INFINITY_PORT` | `8000` | Engine control API + dashboard bind port |
| `INFINITY_HOST` | `127.0.0.1` | Engine bind address (keep loopback in v1) |
| `INFINITY_TUNNEL_RANGE` | `10000-59999` | Ports allocatable for tunnels, `"start-end"` |
| `INFINITY_BATCH_SIZE` | `50` | Concurrent liveness probes per batch |
| `INFINITY_PROBE_TIMEOUT_MS` | `4000` | Per-node handshake timeout |
| `INFINITY_PROBE_BUDGET_PER_REFRESH` | `5000` | Untested nodes probed per source refresh |
| `INFINITY_HEALTH_INTERVAL_S` | `30` | Per-tunnel health-check interval |
| `INFINITY_MAX_MISSES` | `2` | Consecutive failures before a node is swapped |
| `INFINITY_DB` | `infinity.db` | SQLite file path |

Source cadences are per-source (see [docs/scraping.md](./scraping.md#source-manifest)).

## Error codes

| Code | HTTP | Meaning |
| --- | --- | --- |
| `invalid_request` | 400 | Malformed JSON or out-of-range params |
| `not_found` | 404 | Unknown tunnel id |
| `conflict` | 409 | Operation not valid for the tunnel state (e.g. renew while deleting) |
| `port_exhausted` | 503 | No free ports left in `INFINITY_TUNNEL_RANGE` |
| `tunnel_runtime` | 503 | Cannot spawn/roll a tunnel: docker daemon refused the operation (e.g. overlay mount busy); the tunnel is rolled back and no nodes are left stranded |
| `internal` | 500 | Unexpected engine failure |

## Example session

```bash
# create
CREATE=$(curl -s -X POST http://127.0.0.1:8000/tunnels -d '{"node_count":5,"auto_renew":true}')
PORT=$(echo "$CREATE" | python3 -c "import sys,json;print(json.load(sys.stdin)['port'])")
USER=$(echo "$CREATE" | python3 -c "import sys,json;print(json.load(sys.stdin)['username'])")
PASS=$(echo "$CREATE" | python3 -c "import sys,json;print(json.load(sys.stdin)['password'])")

# use over HTTP and SOCKS5
curl -x "http://$USER:$PASS@127.0.0.1:$PORT" https://api.ipify.org
curl --socks5-hostname "$USER:$PASS@127.0.0.1:$PORT" https://api.ipify.org

# forced renewal
curl -s -X POST "http://127.0.0.1:8000/tunnels/$(
  echo "$CREATE" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])"
)/renew"

# teardown
curl -s -X DELETE "http://127.0.0.1:8000/tunnels/$(
  echo "$CREATE" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])"
)"
```