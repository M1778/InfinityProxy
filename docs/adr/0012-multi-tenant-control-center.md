# ADR-0012: Multi-tenant control center as a separate edge service

Status: **accepted**

Multi-tenant operator signups are served by a **tenancy** package — a separate
Flask service on `127.0.0.1:8790` that fronts the engine — not by adding auth to
the Control API. Tenants register, receive an operator key, spend credits, and
provision tunnels through it; the engine and panel stay unauthenticated
localhost services (ADR-0003, ADR-0007) and gain nothing.

## Context

Turning InfinityProxy into something an operator can host for others means
people who are *not* the host operator come over the network and spawn tunnels.
The Control API was deliberately built the other way: unauthenticated, bound to
`127.0.0.1:8787` ([ADR-0003](0003-localhost-control-api.md)) — every caller is
implicitly the host operator by virtue of being on the host. The panel inherits
that posture ([ADR-0007](0007-web-panel.md)).

"Add auth to the engine" and "leave the API unauthenticated" are both wrong out
of the box:

- **Auth in the engine** — breaks the ADR-0003 threat model (loopback is the
  boundary) for every existing deployment, couples multi-tenant billing state
  (credits, tenants, ledgers) into the engine's SQLite pool store, and makes
  `python -m engine` carry an account database nobody asked for.
- **Expose engine as-is** — one shared key at best, no per-operator scope, and
  a tenant that owns the host's full control surface (sources, host agent
  triggers, every tunnel's credentials).

## Decision

Add a `tenancy` package and compose service (`python -m tenancy`), following the
hostagent pattern ([ADR-0010](0010-hostagent.md)): a third opt-in Flask
component with its own address and store, chosen over engine-side auth.

| Service | Address | Env | Default |
| --- | --- | --- | --- |
| Engine control API | `127.0.0.1:8787` | `INFINITY_PORT` | `8787` |
| Panel | `127.0.0.1:8000` | `INFINITY_PANEL_PORT` | `8000` |
| Hostagent | `127.0.0.1:8788` | `INFINITY_HOSTAGENT_PORT` | `8788` |
| Control center | `127.0.0.1:8790` | `INFINITY_TENANCY_PORT` | `8790` |

- **Deployment**: same image, `network_mode: host`, compose profile
  `--profile tenancy`, its own SQLite file (`tenancy.db`, separate from the
  engine's `infinity.db` so the control center can be dropped without touching
  engine state). Default binding stays loopback; a public-board operator flips
  `INFINITY_TENANCY_HOST` and fronts it as they would any web app.
- **Principal = one API key** (`X-Operator-Key`), issued at `POST
  /v1/tenants/register`; it is the *only* trusted identity. No request bodies,
  headers, or IPs are consulted as identity. Admin actions carry a separate
  `X-Admin-Key` (`INFINITY_TENANCY_ADMIN_KEY`); a board with no admin key
  configured refuses admin routes with `409 admin_not_configured` — signups
  nobody can review are worse than no signups.
- **Ownership is a mapping, not a guess**: a `tenant_tunnels(tenant_id,
  tunnel_id)` table records which engine tunnels a tenant provisioned. Every
  lease read is scoped through it, and tombstones (tunnels gone from the
  engine's listing, e.g. deleted by the host panel or a pruning container) are
  unlinked on read, so the scope stays truthful without a reconciler.
- **Engine access stays loopback and unauthenticated**: the control center is
  the single local principal, calling the engine exactly as the host panel
  does. It forwards only a narrow surface (create/list/renew/delete own
  tunnel, public pool reads); always forwards the engine's own JSON errors
  (`502` with the engine's `error.code`).
- **Credential hygiene**: projections here deliberately separate three shapes —
  engine rows (`shaped_tunnels`) keep only `id/host/port/state`; tenant
  payload and the public board keep those same keys, so no username/password
  crosses either. Username/password are surfaced only at panel / engine
  level. `read_pool` projects the ten public pool counters; operator-only
  engine extras (`by_protocol`, `stale_sources`) do not reach the board.
- **Credits**: registration grants `INFINITY_TENANCY_STARTING_CREDITS`
  (default 720 — 30 days at the default one credit per hour); every meter
  pulls the elapsed boundary forward (`credited_s`) and appends to a
  per-tenant `ledger` (newest-first, kept to the last 50). Tunnel provisioning
  requires credits ≥ 1 and (when metering is enabled) debits
  `credit_interval_s` per period on touch. Meter off = creation stays allowed.
- **Caps**: `max_tenants` (`board_full` on signup at 503), per-tenant
  `max_tunnels_per_tenant` (`lease_limit`, 409), per-tunnel
  `max_nodes_per_tunnel` (registers request node_count, clamped).

## Consequences

- The engine threat model does not move: engine, panel and the control
  center's default bind are loopback. A public board is an explicit operator
  choice of a bind address or reverse proxy — not a default path — and
  everything that crosses it carries an operator/account key.
- Tenants' power is scoped twice: engine permissions live behind the
  control-center's narrow forwarding surface, and tunnel ownership lives in
  the mapping table — so even a web board cannot accidentally share one
  tenant's tunnels with another.
- Ledger + credit metering is estimate-by-touch, not realtime: metering runs
  on authenticated reads and signups, not a background biller; a signed-up
  tenant never called an endpoint accrues nothing beyond the signup grant
  tail.
- Overflow of the credits-possible surface stays possible: an operator with
  credits but at the lease cap gets a 409, not a silent skip; board-full and
  reach-cap tests cover both.
- Multi-tenant-scale features that this deliberately does *not* cover:
  queues of allowed tunnel ports shared with the engine's panel, cron for
  metering, and user-facing subscription feeds (aggregate feed remains
  blocked on attribution — [ROADMAP.md](../../ROADMAP.md)).
