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
| VLESS | full header relay check over TCP or stdlib TLS | expects the `0x00 0x00` response header bytes |
| Trojan | full header relay check over stdlib TLS + CRLF response | TLS is mandatory for trojan; needs `sni` |
| Shadowsocks | **deferred** — keeps v1 gating | full AEAD client needs a verified implementation; uncertified |
| VMess | **deferred** — keeps v1 gating | AEAD header (UUID keyed) singular; zero alive in the pool today |
| TUIC, Hysteria2 | **deferred** — keeps v1 gating | QUIC-based; no stdlib probe |
| SSR | not assignable regardless (ADR-0002 / sing-box ≥ 1.6) | — |

Trade-offs, intentionally accepted and visible in the docs:

- **Reality-fronted VLESS** may false-negative: sing-box probes reality hosts
  with a browser TLS fingerprint; a stdlib ClientHello is not one, so genuine
  reality nodes can be classified dead. Better a node dropped than a web server
  certified; the first benchmark observed zero working relays anyway.
- **WS/gRPC VLESS transports** are probed as plain TCP/TLS, not upgraded. A
  real ws server (which we have never observed relaying) may be dropped; every
  HTTP responder keeps being rejected, which is the failure class that exists.
- Requiring the full 2-byte response header kills "echo" certifiers — a
  listener that merely answers bytes — which v1 could not distinguish.

## Consequences

- The pool shrinks discontinuously on redeploy: previously-`alive` non-relays
  are re-probed and demoted; assignment counts fall and tunnels start degraded
  until genuinely relaying nodes arrive.
- The engine's copy of `engine/filter/probe.py` now reads only stdlib
  (`socket`, `ssl`, `hashlib`) — no new dependencies for the engine image.
- Health-check misses (30s cadence) keep their current semantics; v2 raises
  the bar at admission but does not change the renewal loop.