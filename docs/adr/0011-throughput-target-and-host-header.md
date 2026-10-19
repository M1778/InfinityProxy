# ADR-0011 · Throughput target and probe Host header

- Status: **accepted**
- Date: 2026-10-17
- Amends: [ADR-0008](./0008-throughput-certified-pool.md)

## Context

ADR-0008 certified a node by downloading through it from a public file server
(`speedtest.tele2.net:80 /1MB.bin`). By October 2026 that path answers
`HTTP/1.1 404 Not Found`, so every node that passed the relay handshake was
demoted at the body check: a fresh install admitted 0/2000 probed nodes and
every tunnel came up degraded with `node_count_granted=0`. This was a dead
target, not a probe regression.

The probe also relayed a bare `HTTP/1.0` GET with no `Host` header. HTTP/1.0
does not require Host, but virtual-hosted servers do: `cachefly.cachefly.net`
answers 400 to the Host-less request and 200 to the same request with Host, so
that candidate false-negatived working nodes.

Candidate targets measured from a live vantage with the probe's exact request
shape:

| Target | Host-less | With Host | Body |
| --- | --- | --- | --- |
| speedtest.tele2.net /1MB.bin | 404 | 404 | — |
| www.google.com / | 200 | 200 | ~89 KiB (too small to score) |
| ipv4.download.thinkbroadband.com /100MB.zip | 403 | 403 | — |
| cachefly.cachefly.net /100mb.test | 400 | 200 | stalls at 304 B |
| test-debit.free.fr /10485760.rnd | 200 | 200 | 10 MiB |
| ping.online.net /100Mo.dat | 200 | 200 | 100 MiB |
| speedtest.fra1.de.leaseweb.net /100mb.bin | 200 | 200 | 100 MiB |
| speed.cloudflare.com /__down?bytes=10485760 | 200 | 200 | 10 MiB, anycast |

Separately, the Shadowsocks path judged the relayed response and read the body
*after* its socket had closed, so even against a live target no ss node could
ever certify at admission. The body read now runs while the socket is open,
like the VLESS, Trojan, and forward-proxy paths already did.

The body readers also timed their transfer rate with `time.monotonic()`, which
on Windows is the ~15.6 ms system tick: a loopback-fast read collapsed to
`elapsed == 0` and the measurement was discarded as invalid. They now time
with `time.perf_counter()` (the whole probe module does, so every deadline is
built and consumed on one clock), and only a genuinely empty read returns
`None`.

## Decision

1. **Relayed GETs carry a `Host:` header.** `engine/filter/probe.py` builds one
   request shape (`_probe_request`) used by VLESS/Trojan, Shadowsocks, and the
   HTTP/SOCKS5 forward paths.
2. **Default target = `speed.cloudflare.com:80 /__down?bytes=10485760`**
   (`INFINITY_THROUGHPUT_HOST` / `_PORT` / `_PATH`). Anycast, plain HTTP,
   multi-MiB, and it answered the probe's exact request shape with a 2xx from
   every vantage measured. Operators can point the probe at a regional file
   server; the requirement is only that it serves a multi-MiB plaintext body
   over port 80.
3. **The target is checked in CI.** `tests/test_throughput_target.py` fails
   when the default stops returning a 2xx with at least 512 KiB of body.

## Trade-offs, accepted

- Cloudflare can rate-limit or challenge a node's egress; a node demoted that
  way is treated like any slow node.
- A 10 MiB target under the 1 MiB default sample leaves headroom for larger
  samples without changing the URL.
- One remote dependency remains; ADR-0008's "future fallback target list" is
  still future. Anycast infrastructure is the closest a single default gets to
  being region-independent.

## Consequences

- Fresh installs admit nodes again; the pool gate now measures a rate that can
  exceed the 200 KiB/s floor meaningfully (the 89 KiB Google body could not).
- `Settings()` / `Settings.from_env()` defaults change, and
  `docker-compose.yml` forwards the whole `INFINITY_THROUGHPUT_*` family, so
  values set in `.env` now reach the engine container.
- The ss admission path certifies nodes end to end; v2rayfree's ss-dominated
  feed can contribute to the pool.
- docs/api.md, docs/scraping.md, and this ADR changed with the code.
