# CONTRIBUTING.md

Thanks for contributing. This project is docs-first: the docs (README, `docs/`,
`CONTEXT.md`, ADRs) are the spec and the code ships *against* them — so keep the
docs honest whenever you change behaviour.

## Ground rules

- **Use the glossary.** Terms like tunnel, node, pool, renew, batch, alive-check
  are defined in [CONTEXT.md](./CONTEXT.md). Don't invent synonyms ("server",
  "instance") in code, logs, or docs.
- **Docs are the spec.** A behaviour change that contradicts `docs/` or an ADR
  must update them in the same PR. Mark ADRs `deprecated` / `superseded by
  ADR-NNNN` when they stop being true, don't quietly outdate them.
- **No line-level comments unless they earn their place.** Prefer code that
  reads itself; use comments for the non-obvious *why*.
- **Follow the language** — English only, keep the tone plain and precise.

## Repo layout

```text
engine/          Flask control plane (API, scraper, filter, assigner, db, scheduler)
panel/           Flask web dashboard (SSE snapshot stream, proxied actions, static frontend)
tunnel-image/    sing-box image wrapping the tunnel runtime
tools/           benchmark harness (prod_bench.py)
tests/           unit tests (parser, filter, assigner, allocator, app, panel)
docs/            architecture, api, scraping, dashboard; adr/ decisions
CONTEXT.md       glossary
README.md        entry point
ROADMAP.md       v1 and post-v1 plan
```

## Development setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # if present; else rely on defaults in docs/api.md
```

Run the engine (control API, `127.0.0.1:8787`) and the panel (dashboard,
`127.0.0.1:8000`) directly from the venv, or both via `docker compose up -d`:

```bash
python -m engine    # control API on 127.0.0.1:8787  (INFINITY_PORT)
python -m panel     # dashboard on 127.0.0.1:8000    (INFINITY_PANEL_PORT)
```

`panel/` requires an engine reachable at `INFINITY_ENGINE_URL` (default
`http://127.0.0.1:8787`).

## What's tested (and why not more)

Unit tests cover the **pure logic**: URI parsing (including `ss://`/`vmess://`
base64), dedup identity keys, the liveness filter's batch bookkeeping, the
port/credential allocator, and pool assignment rules (exclusive sets, degraded
top-up).

Live node probing is **not** unit-tested and does not run in CI: free nodes
churn and die between runs, so any integration test asserting real liveness is a
flaky lie. Probe logic is exercised manually against the live engine instead.
The panel backend *is* unit-tested against a fake engine client
([ADR-0007](./docs/adr/0007-web-panel.md)) — no engine threads in tests.

```bash
pytest            # unit tests
ruff check .      # lint
ruff format --check .   # formatting
```

## GitHub Actions CI

Three workflows ship with the implementation:

1. **test** — `pytest` on every PR (matrix across supported Python versions).
2. **lint** — `ruff check` + `ruff format --check` on every PR.
3. **build-push** — build `engine` and `tunnel` images and push to GHCR on
   version tags (`v*`). Includes a `docker build --check`-style config lint where
   the toolchain supports it.

Flaky-network probes stay out of CI (see above).

## Docker

- The Engine runs as one container; each tunnel is its own `sing-box` container
  (see [ADR-0001](./docs/adr/0001-per-tunnel-containers.md)). Compose also runs a
  second `panel` service from the same image.
- **Never publish the engine port (`8787`) or the panel port (`8000`) externally**
  in compose files or docs — both are deliberately localhost-only
  ([ADR-0003](./docs/adr/0003-localhost-control-api.md),
  [ADR-0007](./docs/adr/0007-web-panel.md)).
- Tunnel ports are allocated from `INFINITY_TUNNEL_RANGE` (default
  `10000-59999`).

## Attribution hygiene

Node scraping carries license obligations — see
[docs/scraping.md](./docs/scraping.md#attribution). Never strip first-seen
source attribution in the scraper's node records; the aggregate-subscription
feature must not ship until its attribution mechanism is designed
([ROADMAP.md](./ROADMAP.md)).

## How to propose changes

1. Open an issue or PR against the spec first for behaviour changes — docs drive.
2. Keep PRs small and scoped; reference the docs/ADR they touch.
3. CI must be green (test + lint); no force-pushes, no rewriting history.