# ADR-0008 · Throughput-certified pool

- Status: **accepted**
- Date: 2026-09-09
- Extends: [ADR-0006](./0006-relay-grade-liveness-probes.md)

## Context

ADR-0006 certifies a node alive only when the node's *own server* completes its
wire protocol against the probe — a relayed 2xx/3xx status line from the target
is enough. Admission therefore measures *that a tunnel does not break*, not
*that it is usable*.

The production pool replays this gap at scale: free nodes that relay faithfully
still fail in the field because the relay is useless — throughput under a few
hundred KiB/s, or a node that answers the probe's GET and then stalls before the
body arrives. Handshake-liveness alone cannot tell a working tunnel from a
throttled or body-less one, so tunnels can spin urltest rotations over nodes
that are certifiably alive and still unusable for real traffic.

Separately, the renewal loop's conceptual contract — "the tunnel is alive" —
relied on re-probing handshakes. Nothing verified that a running tunnel's
container was actually still up, so a layer where the container exits (or the
host kills it) left the row `running` and the health pass silently driving a
dead tunnel.

## Decision

1. **Admission = relay handshake + throughput certification.** At admission, a
   node is `alive` only if its relay handshake passes (ADR-0006 semantics) **and**
   it delivers the probe's download sample through the tunnel at or above
   `INFINITY_THROUGHPUT_MIN_KB_S`.

   - The probe opens one connection, completes the relay handshake, then reads a
     downloaded body from the configured throughput target (default a public
     file server — `speedtest.tele2.net:80 /1MB.bin`, `INFINITY_THROUGHPUT_HOST`
     / `_PORT` / `_PATH`) until it has `INFINITY_THROUGHPUT_SAMPLE_BYTES` bytes,
     EOF, or `INFINITY_THROUGHPUT_TIMEOUT_S` elapses.
   - `throughput_kb_s = received / 1024 / elapsed`.
   - A node whose body never arrives (`throughput_kb_s is None`) — handshake-
     honest but relaying nothing — is **demoted**, not certified.
   - A body that arrives below the floor is **demoted** too, and the demotion
     carries the measured rate in the error so the panel can show why.
   - The handshake target and the download target are separate on purpose: the
     handshake needs a host that answers quickly (`www.google.com`), the
     throughput target needs a large body. `_HEALTH` cadence stays on the cheap
     handshake so the 30s loop does not saturate the host link.
   - The gate is **off by default** in `Settings()` (`throughput_enabled=False`)
     so unit tests that probe loopback emulators keep their handshake-only
     verdicts; `INFINITY_THROUGHPUT_ENABLED` turns it on for live operation.
     The pool only applies the gate when a `Settings` object is passed to
     `_admit_batch` — the health loop never re-runs throughput.

2. **Throughput is measured at admission, stored, and reused.** Fresh nodes are
   probed with the gate; the winning number is persisted on the node
   (`nodes.throughput_kb_s`) and survives later handshake-only health passes
   (`apply_probe_results` applies `COALESCE(?, throughput_kb_s)`, never erasing
   a measured value with a `NULL`). The assigner uses it to prefer fast nodes.

3. **Health checks self-heal the container.** The 30s health pass ends by
   asking the container driver `is_running(tunnel_id)`; if the row is live and
   the container is not, the tunnel is redeployed from the store and the event
   is counted in `tunnel_health.restarts_24h`. This closes the window where the
   engine believed a tunnel was serving when its container was gone.

## Trade-offs, accepted

- **Admission cost rises.** Every fresh node now performs a download
  (default 1 MiB sample, 12s cap). At batch size 50 this is more traffic than a
  handshake-only pass. Accepted: the pool's job is to certify *usable* nodes,
  and a certifying probe that spends a little more time per node already
  existed since ADR-0006.
- **The throughput target is a second remote dependency.** A target outage makes
  `probe()` return no throughput for all nodes (they are demoted, the pool
  drains). The target is a large, low-policy public file server and configurable
  per host; a future ADR may add a fallback target list.
- **Health checks do not re-measure throughput.** Throughput degrades over time;
  a node that was fast at admission can go slow later and the health loop will
  not catch it until a fresh probe pass re-certifies it. Accepted for cost; the
  renewal cycle re-probes fresh nodes on their source cadence.
- **COALESCE keeps stale-but-bounded numbers.** A node admitted once keeps its
  old throughput figure through handshake-only health passes; the number can go
  stale between refreshes. Accepted: the alternative (NULLing on every health
  pass) would make the assigner's preference data vanish hourly.

## Consequences

- `engine/models.py` grows `throughput_kb_s` on `Node` and `ProbeResult`;
  `engine/filter/probe.py` grows `ThroughputSpec` and the body-measure path;
  `engine/filter/runner.py` passes the spec through `batch_probe`.
- `engine/db.py` migrates existing databases: `_migrate()` ADDS
  `nodes.throughput_kb_s INTEGER` when the column is missing (additive, no data
  loss).
- `engine/pool.py` gates admission when given settings (`_admit_batch(store,
  batch, settings)`); unit tests call without settings and keep handshake-only
  semantics.
- `engine/assigner.py`'s preference sort becomes throughput-first
  (`_prefer_best`: NULL throughput last, then higher KiB/s first, then tested
  latency, then untested), so tunnels draw the fastest certified nodes first.
- `engine/scheduler.py` self-heals down containers and exposes `restarts_24h`
  on `GET /tunnels/{id}` / tunneling health.
- The docs learned in [docs/scraping.md](../scraping.md#liveness-filter),
  [docs/api.md](../api.md#configuration), and this record all changed in the
  same commit as the engine.

## Out of scope

- Re-measuring throughput on the 30s health cadence (cost), and gating renewal
  on it. A future ADR could add a slow-but-bounded throughput check to the
  health loop.
- Per-tunnel or per-client throughput guarantees. This certifies the pool, it
  does not police tunnels.