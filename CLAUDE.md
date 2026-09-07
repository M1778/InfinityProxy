# CLAUDE.md

Agent onboarding map for InfinityProxy. Read this, then [CONTEXT.md](./CONTEXT.md)
before touching anything.

## What this repo is

Docs-first specification for **InfinityProxy** — a proxy engine that scrapes
free proxy nodes from public GitHub feeds, filters them for liveness, and spawns
per-tunnel sing-box containers that rotate through the alive nodes. Stack: Python,
Flask, SQLite, Docker, sing-box.

**The repo has no source code yet.** The docs are the spec. If you're asked to
implement, implement *against* these files — and any behaviour change must update
them, too.

## Where things live

| Path | What it is |
| --- | --- |
| `README.md` | Entry point: what it is, 30-second how-it-works, quickstart |
| `CONTEXT.md` | **Glossary — canonical domain vocabulary. Read it first.** |
| `docs/architecture.md` | Components, request flow, renewal loop, SQLite schema, planned src layout |
| `docs/api.md` | Control API endpoints, payloads, errors, env config table |
| `docs/scraping.md` | Source manifest, fetch loop, dedup, liveness filter, attribution |
| `docs/adr/` | 5 accepted decisions (per-tunnel containers, SQLite, localhost API, MIT+attribution, urltest rotation) |
| `docs/benchmark.md` | Real-stack benchmark harness results: first 10-minute run findings |
| `ROADMAP.md` | v1 surface + post-v1 ambitions and stated no's |
| `CONTRIBUTING.md` | Build, test, CI, and contribution conventions |

## Vocabulary trap

"Proxy" is overloaded. The engine spawns **tunnels** (sing-box listeners clients
connect to); it scrapes **nodes** (data URIs, never connected to directly).
Never call a tunnel a "proxy server" or a node a "server". Full rules in
`CONTEXT.md`.

## Key facts to not re-derive

- Control API: `127.0.0.1:8000`, unauthenticated by design, localhost-only
  (ADR-0003). Don't document or build remote exposure.
- Tunnel ports: `10000–59999` from `INFINITY_TUNNEL_RANGE`; credentials stable
  per tunnel, never change on renew.
- Liveness: protocol handshake, batch 50, 4s timeout; per-tunnel health check
  every 30s, swap after 2 misses.
- Renewal replaces nodes only — never the tunnel's host, port, or credentials.
- Sources: 5 feeds with mixed licenses including GPL-3.0 (Epodonios); listing in
  `docs/scraping.md#attribution`. The aggregate-subscription feature is blocked
  on attribution design.
- Latency-weighted rotation = sing-box `urltest` outbound (ADR-0005).
- SSR nodes are scraped but never assigned: sing-box ≥ 1.6 dropped the
  shadowsocksr outbound; tunnels only use sing-box-dialable protocols
  (see `docs/scraping.md#filtering-policy`).

## Commands

```bash
pytest                  # unit tests
ruff check .            # lint
ruff format --check .   # formatting
docker compose up -d    # run the engine (once code exists)
```

## Conventions

- Use glossary terms verbatim in code, logs, and docs.
- No comments unless they carry a non-obvious *why*.
- English only, plain tone, no emojis.
- Behaviour changes update the docs/ADR in the same change.

## Do not

- Implement features marked *out of scope* in ROADMAP.md (paid sources, guaranteed
  exit IPs, per-request credential rotation).
- Add auth/remote control to the control API without superseding ADR-0003.
- Ship the aggregate subscription feed without its attribution mechanism.