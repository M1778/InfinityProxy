# API Reference

The Engine exposes a JSON REST control API on **`127.0.0.1:8787`** (configured
via `INFINITY_PORT`). `GET /` redirects (302) to the web panel at
`INFINITY_PANEL_BASE_URL` (default `http://127.0.0.1:8000`); the panel itself is
documented in [dashboard.md](./dashboard.md). Host-wide client features (System
Proxy, TUN mode) are driven through the privileged **hostagent** at
`INFINITY_HOSTAGENT_URL` (default `http://127.0.0.1:8788`) — see
[Host control](#host-control-adr-0010) below and
[ADR-0010](./adr/0010-hostagent.md). For deployments serving other operators,
the **control center** (`tenancy` package, `127.0.0.1:8790`) fronts the engine
with tenant accounts, API keys, tunnel scoping, and credit metering — see
[Control center](#control-center-adr-0012) below and
[ADR-0012](./adr/0012-multi-tenant-control-center.md).

> **Security:** in v1 the control API and the panel are bound to localhost and
> are **unauthenticated by design**. Don't publish them to any network. See
> [ADR-0003](./adr/0003-localhost-control-api.md). The control center adds
> tenant-scoped keys *on top of* that posture, as a separate service; the
> engine surface itself is unchanged.

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
    "working_set": 256,
    "working_set_next_s": 180,
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

The response is cached for **1s** and carries a weak `ETag`; a poller that
repeats the same digest gets an empty **304** instead of a fresh body. The
panel's 5s poll and the bench harness both funnel through this — repeat calls
within a second cost no SQL, and unchanged bodies cost no serialization.

| Field | Meaning |
| --- | --- |
| `pool.total` | Nodes currently tracked in the pool |
| `pool.alive` | Nodes that passed the liveness filter |
| `pool.dead` | Nodes that failed the liveness filter |
| `pool.untested` | Nodes not yet probed |
| `pool.in_use` | Alive nodes currently assigned to tunnels |
| `pool.assignable` | Alive nodes not assigned to any tunnel |
| `pool.tier_a` | Alive nodes evaluated at/above the availability floor and freshly probed (ADR-0009; `0` when the feature is off) |
| `pool.tier_b` | Alive nodes evaluated below the floor, or whose verdicts aged past `INFINITY_STABILITY_MAX_AGE_S` (ADR-0009; `0` when off) |
| `pool.avg_score` | Mean cached availability over alive nodes (ADR-0009; `null` when off) |
| `pool.working_set` | Alive, unassigned nodes in the top `INFINITY_STABILITY_WORKING_SET` by cached availability — the population the working-set loop re-probes (ADR-0009; `0` when off) |
| `pool.working_set_next_s` | Seconds until the next working-set probe pass; `null` when the feature is off |
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
| `sort` | `score` — stability order (Tier A, then B, then cold); `latency` — increasing probe latency, untested last; `throughput` — decreasing measured speed, unmeasured last; `400` when `sort=score` is requested while the feature is off |
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
      "last_error": null,
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
(ADR-0009). `last_error` carries the latest handshakes' failure text: an alive
verdict clears it, a dead verdict writes it. A node is Tier A only while its
verdicts are fresh (within `INFINITY_STABILITY_MAX_AGE_S`); aged nodes report
Tier B until the working-set loop re-probes them. The speed term decays on the
same horizon: a `throughput_kb_s` measured longer ago than
`INFINITY_STABILITY_MAX_AGE_S` stops anchoring the percentile until a fresh
download re-stamps it.

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

> **First use:** tunnel containers run `INFINITY_SINGBOX_IMAGE`
> (`ghcr.io/sagernet/sing-box:v1.11.6` by default). When that image is not
> present locally, the engine pulls it before starting the container, so the
> very first `POST /tunnels` on a fresh host can take as long as the pull.

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

`dead_swapped_24h` and `restarts_24h` are rolling 24h windows of timestamped
events, pruned on write and on read — an entry that happened 25h ago stops
counting without needing a midnight reset or an engine restart.

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

### Host control (ADR-0010)

Host-wide features are orchestrated by the engine against the privileged
hostagent (see [ADR-0010](./adr/0010-hostagent.md)). The engine is the only
caller of the hostagent; the panel proxies through the engine. All host endpoints
are **gated**: when `INFINITY_HOST_ENABLED` is `0`, they answer `409
(code: "host_features_disabled")`. When the hostagent is unreachable, `GET
/host` reports `hostagent.available: false` (still 200, so the panel can render
the feature as down) while the mutating endpoints answer **502**
(`hostagent_unreachable`).

**Auto Pick Best:** a target of `"auto"` makes the engine measure every running
tunnel's egress — latency through the tunnel proxy (GET `INFINITY_HOST_PICK_TEST_URL`),
then a download-throughput sample (`INFINITY_HOST_PICK_SAMPLE_BYTES`,
`INFINITY_HOST_PICK_TIMEOUT_S`) — and pick the lowest latency, tie-broken by
throughput. Results are cached `INFINITY_HOST_PICK_TTL_S` (default 60s);
`POST /host/pick` forces a fresh pass.

#### `GET /host` — host state + candidates

Hostagent reachability, platform/capabilities, the current System Proxy and TUN
state, the candidate tunnels with their last measurements, and the cached pick.

| Field | Meaning |
| --- | --- |
| `hostagent.available` | Whether the hostagent answered (false → every other shape is `null`) |
| `hostagent.capabilities.proxy` | Detected proxy setter: `"gsettings"` \| `"kwriteconfig"` \| `"none"` |
| `hostagent.capabilities.tun` | Whether the host can run TUN mode (privilege + `/dev/net/tun`) |
| `proxy` | Current System Proxy state (`{enabled, tunnel, endpoint}`) |
| `tun` | Current TUN state (`{enabled, iface, tunnel, endpoint}`) |
| `tunnels[]` | Running tunnels with `{id, host, port, username, latency_ms, throughput_kb_s}` (latency/throughput `null` until measured) |
| `pick` | Last auto-pick result `{tunnel, latency_ms, throughput_kb_s, cached, stale_in_s}`; after a fresh pass it also carries `measurements[]` — one `{tunnel, port, username, latency_ms, throughput_kb_s}` row per candidate (the same list the host table renders) |

```json
{
  "hostagent": {
    "available": true,
    "platform": { "system": "linux", "machine": "x86_64", "docker": true, "tun": true },
    "capabilities": { "proxy": "gsettings", "tun": true },
    "uptime_s": 120
  },
  "proxy": { "enabled": true, "tunnel": "tu_8f3k9a", "endpoint": "127.0.0.1:10244" },
  "tun": { "enabled": false, "iface": null, "tunnel": null, "endpoint": null },
  "tunnels": [
    { "id": "tu_8f3k9a", "host": "127.0.0.1", "port": 10244, "username": "u_5x2m", "latency_ms": 45, "throughput_kb_s": 812 },
    { "id": "tu_9b1c2d", "host": "127.0.0.1", "port": 10456, "username": "u_4p8r", "latency_ms": 210, "throughput_kb_s": 344 }
  ],
  "pick": { "tunnel": "tu_8f3k9a", "latency_ms": 45, "throughput_kb_s": 812, "cached": true, "stale_in_s": 42 }
}
```

#### `POST /host/pick` — (re)measure and auto-pick best

```bash
curl -s -X POST http://127.0.0.1:8787/host/pick
```

```json
{
  "tunnel": "tu_8f3k9a", "latency_ms": 45, "throughput_kb_s": 812, "cached": false,
  "measurements": [
    { "tunnel": "tu_8f3k9a", "port": 10244, "username": "u_5x2m", "latency_ms": 45, "throughput_kb_s": 812 },
    { "tunnel": "tu_9b1c2d", "port": 10456, "username": "u_4p8r", "latency_ms": 210, "throughput_kb_s": 344 }
  ]
}
```

Errors: `409` (`host_target_unavailable`) when no tunnel is running to measure;
`502` (`hostagent_unreachable`) when the hostagent is down (the measurement
reads through each candidate tunnel and needs the engine to reach the tunnel
loopback — the hostagent is only contacted to confirm reachability).

#### `POST /host/proxy` — System Proxy on/off

Body `{ "enabled": true, "tunnel": "auto" | "<tunnel id>" }`. `tunnel` is
required to enable and defaults to the last pick when omitted; `"auto"` runs
auto-pick. Disabling restores the desktop's proxy `mode 'none'`.

```json
{ "proxy": { "enabled": true, "tunnel": "tu_8f3k9a", "endpoint": "127.0.0.1:10244" } }
```

Errors: `409` (`host_target_unavailable`) for a missing/stopped tunnel when
`enabled`; `409` (`host_unsupported`) when `capabilities.proxy` is `"none"`;
`502` (`hostagent_unreachable`).

#### `POST /host/tun` — TUN mode on/off

Body `{ "enabled": true, "tunnel": "auto" | "<tunnel id>" }`. Enabling spawns a
privileged sing-box TUN container in the host network namespace routing all host
traffic through the chosen tunnel; disabling removes it. **A dead tunnel with
TUN on takes the host's uplink down** until the mode is toggled.

```json
{ "tun": { "enabled": true, "iface": "infinity-tun0", "tunnel": "tu_8f3k9a", "endpoint": "127.0.0.1:10244" } }
```

Errors: `409` (`host_target_unavailable`) for a missing/stopped tunnel when
`enabled`; `502` (`host_privilege`) when the host lacks TUN capability;
`502` (`hostagent_unreachable`).

## Control center (ADR-0012)

The `tenancy` package is the multi-tenant front for the engine, a separate
Flask service on **`127.0.0.1:8790`** (`python -m tenancy`, compose profile
`tenancy`). It keeps its own SQLite store (`tenancy.db`); the engine and panel
are untouched and stay unauthenticated (ADR-0003/0007).

### Key scopes

| Audience | Header | Notes |
| --- | --- | --- |
| Visitor | — | `/v1/public/*` and `/health` only |
| Tenant | `X-Operator-Key` | Key issued at registration. The key **is** the identity: no header/IP/body contributes identity. A live tenant without one is served exactly one 401. |
| Host operator | `X-Admin-Key` | `INFINITY_TENANCY_ADMIN_KEY`; admin routes answer `409 admin_not_configured` until it is set. |

A "lease" is a tenant-provisioned Proxy Tunnel. Ownership is recorded in a
`tenant_tunnels` mapping; every listing is filtered through it, and tunnels
gone from the engine (deleted elsewhere) are unlinked on read. A tenant's
endpoint and credentials never cross the control center: lease rows carry
`id/host/port/state` only — connecting uses the tunnel's own credentials,
obtained the same way as for the engine (the tunnel's SOCKS/HTTP listener
authenticates clients itself; see the engine session below).

### Tenant surface

| Endpoint | Notes |
| --- | --- |
| `POST /v1/tenants/register` | Body `{"title","email","node_count"?}`. Issues a `user_…` operator key, grants `INFINITY_TENANCY_STARTING_CREDITS`, records the signup grant on the ledger. |
| `GET /v1/me` | Yours: profile (`tenant_id`, `tenant_key`, `title`, `email`, `state`, `node_count_requested`, `created_at`, `credits`, `credited_at`) + scoped, cred-free `leases[]`. |
| `GET /v1/me/ledger` | Newest-first `{at_s, tenant_id, delta, reason}` entries, limited by `INFINITY_TENANCY_LEDGER_KEEP` (50 by default). Meter deltas are actual debits; unpaid shortfall is recorded in `reason`, not negative balance or debt. |
| `POST /v1/me/tunnels` | Reserves a lease slot before provisioning through the engine (`node_count` clamped to `INFINITY_TENANCY_MAX_NODES`). Returns only `{id,host,port,state}`. Gated on credits ≥ 1 when metering is enabled and on the per-tenant cap including in-flight reservations. Definite rejection releases the slot; uncertain creation or failed ownership finalization keeps it counted until deletion or host reconciliation. |
| `DELETE /v1/me/tunnels/{id}` | Engine delete + unlink. A foreign id is `404` — checked before the engine is called at all. |
| `POST /v1/me/tunnels/{id}/renew` | Engine renewal, mapped through the scope. |
| `GET /v1/me/proxy-clients` | The panel's observed proxy-client rows from the tenant's leases (when the panel feeds them in via the admin surface below). |

### Admin surface

| Endpoint | Notes |
| --- | --- |
| `GET /v1/admin/tenants` | All accounts (`tenant_key` included) + counts. |
| `POST /v1/admin/tenants` | Manually issue a seeded account (`{"title","email"?}`). |
| `POST /v1/admin/tenants/{id}/grant` | `{"amount"}` → adds credits + `admin grant` ledger row. |
| `POST /v1/admin/tenants/{id}/rotate` | New key; the old `X-Operator-Key` becomes `401` immediately. |
| `POST /v1/admin/tenants/{id}/disable` | Freezes the account (`403 disabled`) before attempting all owned lease deletes. Returns 200 `{tenant,cleanup_pending:[ids]}`; failed deletes stay linked and repeating disable retries them. Missing tunnels (404) are unlinked. A pending teardown may still accept the tunnel's own credentials. |
| `GET /v1/admin/proxy-clients` | Recently observed proxy clients per account (the panel maps client rows to tenants). |

### Public surface

| Endpoint | Notes |
| --- | --- |
| `GET /health` | Liveness echo. |
| `GET /v1/public/pool` | The ten public pool fields the panel charts. Engine-only extras do not cross (whitelist + shape). |
| `GET /v1/public/tunnels` | Active tenants and their leases, endpoint-only (`id/port/state`, no credentials). |
| `GET /v1/public/stats` | Read-only `{tenants, credits_outstanding, metered_since_s}` snapshot; never drains credits or writes the ledger. |
| `GET /` | Service name, configured `title`, and links to registration, tenant, admin and public routes. |

### Engine failure passes through

Engine errors surface with their own codes and `502` (`engine_unreachable`
when the connection fails outright) rather than being swallowed as generic
500s — the control center is a thin principal, not a cache.

### Example session

```bash
# sign up
TEN=$(curl -s -X POST http://127.0.0.1:8790/v1/tenants/register \
  -d '{"title":"acme","email":"ops@acme.co"}')
KEY=$(echo "$TEN" | python3 -c "import sys,json;print(json.load(sys.stdin)['tenant_key'])")

# provision a tunnel on your credits
curl -s -X POST http://127.0.0.1:8790/v1/me/tunnels \
  -H "X-Operator-Key: $KEY" -d '{}'

# your leases (endpoint-only; credentials live with the tunnel itself)
curl -s http://127.0.0.1:8790/v1/me -H "X-Operator-Key: $KEY"

# teardown
curl -s -X DELETE "http://127.0.0.1:8790/v1/me/tunnels/$(
  echo '{"id":"tu_abcd"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])"
)" -H "X-Operator-Key: $KEY"
```

## Configuration

All overridable via environment variables (defaults in brackets). The shipped
`docker-compose.yml` forwards every variable in this table — plus the rest of
the `INFINITY_*` knobs `engine/config.py` reads — to the engine container, so
setting them in `.env` works.

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
| `INFINITY_DEAD_RETEST_BUDGET` | `200` | Already-dead nodes re-tested FIFO per source refresh, in addition to the untested budget |
| `INFINITY_HEALTH_INTERVAL_S` | `30` | Per-tunnel health-check interval |
| `INFINITY_MAX_MISSES` | `2` | Consecutive failures before a node is swapped |
| `INFINITY_URTEST_INTERVAL_S` | `30` | Per-tunnel sing-box `urltest` health-check cadence; the rotator re-probes its nodes at this rate and excludes dead ones from selection (clamped to ≥ 10s, sing-box's minimum) |
| `INFINITY_PANEL_TEST_URL` | `https://api.ipify.org` | URL the panel dials through a tunnel to prove egress |
| `INFINITY_THROUGHPUT_ENABLED` | `1` | Measure a download through each fresh node at admission and gate the pool on its rate (ADR-0008). Set `0`/`false`/`no` to admit on handshake only |
| `INFINITY_THROUGHPUT_MIN_KB_S` | `200` | Admission floor: nodes certified below this many KiB/s are demoted at probe time |
| `INFINITY_THROUGHPUT_SAMPLE_BYTES` | `1048576` | Bytes the probe reads from the throughput target per node (1 MiB default) |
| `INFINITY_THROUGHPUT_TIMEOUT_S` | `12` | Per-node cap on the throughput download; a node that cannot deliver the sample in this window drops below the floor |
| `INFINITY_THROUGHPUT_HOST` / `_PORT` / `_PATH` | `speed.cloudflare.com` / `80` / `/__down?bytes=10485760` | Throughput target the probe downloads through each node (ADR-0012); every relayed GET carries a `Host:` header |
| `INFINITY_STABILITY_ENABLED` | `1` | Accumulate windowed probe counters and assign nodes by stability score (ADR-0009). Set `0`/`false`/`no` for the legacy throughput-first order |
| `INFINITY_STABILITY_MIN_PROBES` | `6` | Verdicts a node needs before it is *evaluated*; fewer probes = cold, sorted after every evaluated node |
| `INFINITY_STABILITY_MIN_AVAIL` | `0.4` | Availability floor separating Tier A (reliable) from Tier B |
| `INFINITY_STABILITY_WEIGHT_AVAIL` | `0.6` | Weight of the Wilson availability term in the membership score |
| `INFINITY_STABILITY_WEIGHT_SPEED` | `0.4` | Weight of the within-protocol throughput-percentile term |
| `INFINITY_STABILITY_WINDOW_S` | `3600` | Probe counters fold (halve) when a verdict arrives this long after `window_started_s`, then the window restarts |
| `INFINITY_STABILITY_MAX_AGE_S` | `21600` | A node whose last verdict is older than this leaves Tier A for Tier B until re-probed |
| `INFINITY_STABILITY_WORKING_SET` | `256` | Size of the alive-unassigned handshake working set re-probed by the dedicated loop |
| `INFINITY_STABILITY_WORKING_SET_CADENCE_S` | `300` | Cadence of the working-set probe loop |
| `INFINITY_STABILITY_REPROBE_MIN_S` | `120` | Minimum gap a node needs before the working-set loop re-probes it |
| `INFINITY_HOST_ENABLED` | `0` | Enable host-wide features (System Proxy, TUN). `1`/`0`; a privileged hostagent container must be running (see [ADR-0010](./adr/0010-hostagent.md)) |
| `INFINITY_HOSTAGENT_URL` | `http://127.0.0.1:8788` | Engine → hostagent base URL; tracks `INFINITY_HOSTAGENT_PORT` unless set explicitly |
| `INFINITY_HOSTAGENT_PORT` | `8788` | Hostagent control API bind port (loopback by design) |
| `INFINITY_HOST_PICK_TTL_S` | `60` | Auto-pick-best result cache TTL |
| `INFINITY_HOST_PICK_TEST_URL` | `https://api.ipify.org` | URL dialed through each candidate tunnel to measure latency + throughput for auto-pick |
| `INFINITY_HOST_PICK_SAMPLE_BYTES` | `262144` | Bytes read per candidate during auto-pick throughput measurement (256 KiB default) |
| `INFINITY_HOST_PICK_TIMEOUT_S` | `8.0` | Per-candidate measurement cap |
| `INFINITY_DB` | `infinity.db` | SQLite file path |

Control center settings (`INFINITY_TENANCY_*`, ADR-0012):

| Variable | Default | Description |
| --- | --- | --- |
| `INFINITY_TENANCY_HOST` | `127.0.0.1` | Control center bind address (public boards must set this deliberately) |
| `INFINITY_TENANCY_PORT` | `8790` | Control center bind port |
| `INFINITY_TENANCY_ENGINE_URL` | `http://127.0.0.1:8787` | Control center → engine base URL |
| `INFINITY_TENANCY_DB` | `tenancy.db` | Control center SQLite path (separate from `INFINITY_DB`) |
| `INFINITY_TENANCY_ADMIN_KEY` | — |Admin key for `/v1/admin/*`; unset = admin routes answer `409 admin_not_configured` |
| `INFINITY_TENANCY_PAGE_TITLE` | `InfinityProxy control center` | Title the board advertises on its index |
| `INFINITY_TENANCY_TUNNEL_HOST` | — | Host advertised to tenants for connecting to leases (default: whatever `shaped_tunnels` reflects from the engine) |
| `INFINITY_TENANCY_STARTING_CREDITS` | `720` | Signup grant (30 days at one credit per hour) |
| `INFINITY_TENANCY_MAX_TENANTS` | `50` | Registration cap; signups beyond answer `503 board_full` |
| `INFINITY_TENANCY_MAX_TUNNELS` | `4` | Per-tenant lease cap; provisioning beyond answers `409 lease_limit` |
| `INFINITY_TENANCY_MAX_NODES` | `20` | Per-tunnel node-count ceiling for tenant requests (clamped, not rejected) |
| `INFINITY_TENANCY_CREDIT_INTERVAL_S` | `3600` | Wall-clock seconds one credit covers; ≤ 0 disables metering and the provisioning credit gate |
| `INFINITY_TENANCY_METER_ENABLED` | `1` | Metering on/off. Off = provisioning stays credit-free |
| `INFINITY_TENANCY_LEDGER_KEEP` | `50` | Ledger rows kept per tenant |

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
| `renew_failed` | 502 | Renewal probe/store/config failure; details are logged locally, not returned |
| `docker_unreachable` | 502 | Tunnel runtime unavailable during manual renewal |
| `internal` | 500 | Unexpected engine failure |
| `host_features_disabled` | 409 | Host endpoints hit while `INFINITY_HOST_ENABLED` is `0` |
| `hostagent_unreachable` | 502 | The hostagent control API is down (`INFINITY_HOSTAGENT_URL`) |
| `host_target_unavailable` | 409 | The requested target tunnel is missing/stopped, or no tunnel is running for `"auto"` |
| `host_unsupported` | 409 | Host capability missing for the operation (e.g. `proxy` on a host with no detected desktop) |
| `host_privilege` | 502 | TUN mode is impossible on this host (no `/dev/net/tun`, no privilege) |

Control center error codes (ADR-0012):

| Code | HTTP | Meaning |
| --- | --- | --- |
| `missing_key` | 401 | `X-Operator-Key` absent |
| `invalid_key` | 401 | Unknown key (or a rejected admin key) |
| `disabled` | 403 | Tenant disabled by the host operator |
| `admin_not_configured` | 409 | `/v1/admin/*` on a board without `INFINITY_TENANCY_ADMIN_KEY` |
| `out_of_credits` | 402 | Provisioning while below one credit |
| `board_full` | 503 | Registration at `INFINITY_TENANCY_MAX_TENANTS` |
| `lease_finalize_failed` | 502 | Engine created a tunnel but its ownership could not be saved; deletion is attempted, and unresolved capacity remains reserved |
| `lease_limit` | 409 | Provisioning at `INFINITY_TENANCY_MAX_TUNNELS` |
| `engine_unreachable` | 502 | Engine request failed outright (timeout/refused) |

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