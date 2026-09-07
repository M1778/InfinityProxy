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
| Shadowsocks | full relay round-trip over TCP (SIP004 AEAD) | derives the session subkey with HKDF-SHA1 over the password's EVP_BytesToKey master key; sends `[salt][AE len][tag][AE addr+GET][tag]`, requires the server's `[salt][AE chunk]` to decrypt into a relayed 2xx/3xx status line. AEAD-only: `aes-128/192/256-gcm`, `chacha20-ietf-poly1305` |
| HTTP | full relay round-trip (CONNECT proxy) | completes `CONNECT target` (2xx required), then requires the relayed target response to be a 2xx/3xx `HTTP/x.y` line |
| SOCKS5 | full relay round-trip | method selection + CONNECT grant, then requires the relayed target response to be a 2xx/3xx `HTTP/x.y` line |
| VMess | **deferred** — no relay probe | full AEAD client (UUID-keyed header) not implemented; not certified |
| TUIC, Hysteria2 | **deferred** — no relay probe | QUIC-based; no probeable client; not certified |
| SSR | not assignable regardless (ADR-0002 / sing-box ≥ 1.6) | no probe; never certified |

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
- **Unprobeable ss methods are dropped, not v1-certified.** Only the ciphers in
  `_SS_AEAD_SPECS` can complete the relay handshake; stream ciphers
  (`aes-256-cfb`, `rc4-md5`, …), the 2022-blake3 family, and plugin-transport
  URIs (`plugin=`) are rejected at `probeable()`, exactly like ws/gRPC — better
  a node dropped than a web server certified. `xchacha20-ietf-poly1305` is
  likewise excluded: the `cryptography` AEAD surface on the deployed OpenSSL
  backend does not expose it, and certifying a cipher the probe cannot test
  would re-open the exact hole this ADR closes. The ss probe was validated
  against a live sing-box 1.11.6 `shadowsocks` inbound (`aes-256-gcm` and
  `chacha20-ietf-poly1305`); a wrong password is correctly reported dead.
- **The TCP-hello v1 path is removed entirely.** vmess, tuic, hysteria2 (and
  ssr) are not certified at all until a real client exists: the QUIC protocols
  particularly cannot be hello-certified — a live hysteria2 node in the pool was
  *TCP*-reachable on :443 (a TLS front or honeypot), yet sing-box dials it over
  QUIC, so it is dead-by-relay exactly like the ss collapse the probe fixes.
  Better a node dropped than a hello-answering web server certified.
- **WS/gRPC-transport nodes are rejected at probe time**, not attempted: the
  probe is plain TCP/TLS and cannot complete a WebSocket/gRPC upgrade, so a
  ws-fronted TLS server (e.g. Cloudflare Workers) answers the trojan handshake
  and then closes with `ws closed` — a false positive that assignment would
  otherwise certify. Only `type=tcp` (or an absent `type`) is relay-probeable.
  Benchmarked 0/74 across two ws-populated tunnels before this guard.
  The gate covers every probeable protocol: `_transport()` reads the
  vless/trojan `?type=` and the vmess base64 payload's `net` field.

## Consequences

- The pool shrinks discontinuously on redeploy: previously-`alive` non-relays
  are re-probed and demoted; assignment counts fall and tunnels start degraded
  until genuinely relaying nodes arrive. The v1 removal drops every
  hello-certified vmess/tuic/hysteria2 node outright (these may not relay).
- The engine's copy of `engine/filter/probe.py` reads stdlib `socket`/`ssl`/
  `hashlib` plus **`cryptography`** for the AEAD ciphers (`AESGCM`,
  `ChaCha20Poly1305`, HKDF-SHA1) — a new engine-image dependency accepted by
  this ADR in exchange for closing the ss v1-gating hole.
- HTTP and SOCKS5 forward proxies gain their own relay round-trips, so the
  previously-hello-gated plain proxies are certifiable instead of dropped;
  vmess/tuic/hysteria2/ssr have no probe and are not certified.
- Health-check misses (30s cadence) keep their current semantics; v2 raises
  the bar at admission but does not change the renewal loop.