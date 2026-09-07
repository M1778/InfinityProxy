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
| VLESS | full relay round-trip over TCP or stdlib TLS | sends header + a `GET`; requires the `0x00 0x00` response header followed by a relayed 2xx/3xx `HTTP/x.y` status line |
| Trojan | full relay round-trip over stdlib TLS | TLS is mandatory; sends header + a `GET`, requires the relayed bytes to be a 2xx/3xx `HTTP/x.y` status line |
| Shadowsocks | **deferred** — keeps v1 gating | full AEAD client needs a verified implementation; uncertified |
| VMess | **deferred** — keeps v1 gating | AEAD header (UUID keyed) singular; zero alive in the pool today |
| TUIC, Hysteria2 | **deferred** — keeps v1 gating | QUIC-based; no stdlib probe |
| SSR | not assignable regardless (ADR-0002 / sing-box ≥ 1.6) | — |

Trade-offs, intentionally accepted and visible in the docs:

- **Reality-fronted VLESS** may false-negative: sing-box probes reality hosts
  with a browser TLS fingerprint; a stdlib ClientHello is not one, so genuine
  reality nodes can be classified dead. Better a node dropped than a web server
  certified; the first benchmark observed zero working relays anyway.
- Requiring the full relay round-trip (header ack + a relayed 2xx/3xx HTTP
  status line) kills both "echo" certifiers — a listener that merely answers
  back the bytes it received — and HTTP responders, which v1 could not
  distinguish.
- **Canned-response honeypots are rejected.** A peer that completes the wire
  handshake and then answers the CONNECT with its *own* HTTP error (e.g. a
  canned `HTTP/1.1 400 Bad Request` from a local nginx) was, before this
  amendment, certified alive: handshake bytes arrived, so relays "worked".
  The probe now demands the relayed bytes look like a genuine `2xx/3xx`
  response *from the requested target*. 66/104 alive trojans in the live pool
  (all from one feed's `rooster465` subdomains) were canned-400 honeypots; they
  relayed the canned error to every tunnel client (`SSLError` on the bench).
  A real target (`www.google.com:80`) returns `HTTP/1.0 200 OK` to the probe's
  `GET /`, so genuine relays pass and the honeypots fail.
- The status-line gate can false-negative a genuine relay whose target replies
  with a 4xx/5xx (the probe always `GET /`es a benign target, so this is rare);
  better a working node dropped than a honeypot certified.
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