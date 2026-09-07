# ADR-0006 · Relay-grade liveness probes

- Status: **accepted**
- Date: 2026-09-07
- Supersedes: the v1 probe semantics described in `docs/scraping.md` (ported
  off in [benchmark.md](../benchmark.md))

## Context

v1 liveness certified any listener that answered a bare TCP `ClientHello`
with a single response byte. The production benchmark proved this certifies
non-relays at scale: of ~3,100 "alive" nodes, none relayed (0/44 requests in
20 minutes), and sing-box logged the cause precisely — `unknown version: 72`
(bit 0x48 = `H`, an HTTP responder answering the port), `unknown version: 21`
(a TLS alert), and tunneled `429`s. A socket that answers TCP is not a node
that relays.

## Decision

Replace the pass/fail heuristic with **per-protocol relay handshakes** for the
TCP-capable protocols that dominate the pool. A node is `alive` only when the
*server itself* completes its wire protocol against the probe's request —
not when any bytes come back.

| Protocol | v2 probe | Notes |
| --- | --- | --- |
| VLESS | full relay round-trip over TCP or stdlib TLS | sends header + a `GET`; requires the `0x00 0x00` response header followed by foreign bytes |
| Trojan | full relay round-trip over stdlib TLS | TLS is mandatory; sends header + a `GET`, accepts any relayed bytes (no response header exists) |
| Shadowsocks | **deferred** — keeps v1 gating | full AEAD client needs a verified implementation; uncertified |
| VMess | **deferred** — keeps v1 gating | AEAD header (UUID keyed) singular; zero alive in the pool today |
| TUIC, Hysteria2 | **deferred** — keeps v1 gating | QUIC-based; no stdlib probe |
| SSR | not assignable regardless (ADR-0002 / sing-box ≥ 1.6) | — |

Trade-offs, intentionally accepted and visible in the docs:

- **Reality-fronted VLESS** may false-negative: sing-box probes reality hosts
  with a browser TLS fingerprint; a stdlib ClientHello is not one, so genuine
  reality nodes can be classified dead. Better a node dropped than a web server
  certified; the first benchmark observed zero working relays anyway.
- Requiring the full relay round-trip (header ack + foreign bytes) kills both
  "echo" certifiers — a listener that merely answers back the bytes it received —
  and HTTP responders, which v1 could not distinguish.
- **WS/gRPC-transport nodes are rejected at probe time**, not attempted: the
  probe is plain TCP/TLS and cannot complete a WebSocket/gRPC upgrade, so a
  ws-fronted TLS server (e.g. Cloudflare Workers) answers the trojan handshake
  and then closes with `ws closed` — a false positive that assignment would
  otherwise certify. Only `type=tcp` (or an absent `type`) is relay-probeable.
  Benchmarked 0/74 across two ws-populated tunnels before this guard.

## Consequences

- The pool shrinks discontinuously on redeploy: previously-`alive` non-relays
  are re-probed and demoted; assignment counts fall and tunnels start degraded
  until genuinely relaying nodes arrive.
- The engine's copy of `engine/filter/probe.py` now reads only stdlib
  (`socket`, `ssl`, `hashlib`) — no new dependencies for the engine image.
- Health-check misses (30s cadence) keep their current semantics; v2 raises
  the bar at admission but does not change the renewal loop.