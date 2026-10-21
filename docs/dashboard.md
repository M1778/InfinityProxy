# Dashboard + Port Layout

Status: **delivered** — panel ships (port 8000), engine control API on port
8787. See [ADR-0007](./adr/0007-web-panel.md) for the accepted decision and
[README](../README.md) for the quickstart.

## Goal

Ship a full-control web dashboard for InfinityProxy and fix the port layout.

1. The control API leaves port 8000 for a deeper, less-collision-prone default;
   the **panel owns 8000**. Both ports stay overrideable per user via env, with
   a panel footer that shows the live config so mis-switches are visible.
2. The dashboard monitors *and* operates: live pool/tunnel graphs, node
   browsing, and every tunnel/source action the API exposes — no CLI required.
3. Real-time via Server-Sent Events pushing a server-assembled snapshot every
   5s; charts render with vendored Chart.js 4 (MIT) so the panel works offline
   with zero build step and no CDN dependency.
4. No auth or remote binding: the panel inherits ADR-0003 semantics
   (localhost-only, unauthenticated). Loopback is the security boundary.

## Port layout

| Service | Env | Default | Notes |
| --- | --- | --- | --- |
| Engine control API | `INFINITY_PORT` / `INFINITY_HOST` | `8787` / `127.0.0.1` | moved off 8000; outside tunnel range 10000–59999 |
| Panel | `INFINITY_PANEL_PORT` / `INFINITY_PANEL_HOST` | `8000` / `127.0.0.1` | new service |
| Panel → engine | `INFINITY_ENGINE_URL` | `http://127.0.0.1:8787` | panel API client base |

The engine keeps serving the JSON control API with no UI; its `GET /` becomes a
302 redirect to the panel so a stray browser hit lands somewhere useful.

## Architecture

```
browser ── SSE ──► panel:8000 (Flask)
                     │  ── crawl engine:8787 every 5s (thread) ──► engine API
                     │  │  snapshot {status, pool(+distributions), tunnels, sources}
                     │  │  ring-buffer history (2h @ 5s) → /api/events
                     │  └─ proxy actions: tunnels CRUD, refresh source, test tunnel
```

- `panel/` is a new top-level Python package (same image as the engine, added to
  the Dockerfile and setuptools find). Compose gains a `panel` service running
  `python -m panel`.
- Zero-build frontend: `panel/static/{index.html,app.css,app.js,vendor/chart.umd.js}`
  (Chart.js UMD vendored once). Vanilla JS, no framework, no node toolchain.
- SSE heartbeats every 15s; EventSource reconnects automatically. While the
  stream is down, the browser fetches `GET /api/snapshot` every 5s with at most
  one request in flight. Stream recovery stops the fallback and late HTTP
  answers cannot overwrite the recovered stream. The crawl thread only calls
  the engine while at least one SSE watcher is connected, so an idle panel
  imposes no periodic engine load. `GET /api/snapshot` also crawls on demand:
  first paint and fallback-only viewers can refresh without an SSE watcher.
  Concurrent crawls are single-flight. Crawl exceptions are logged and retried
  instead of killing the thread and freezing the dashboard.
- "Test tunnel" button: the panel dials `https://api.ipify.org` *through* the
  tunnel (CONNECT) and reports the exit IP + latency — the only feature that
  needs panel-originated outbound traffic (env `INFINITY_PANEL_TEST_URL`).

## Panel views

The Overview view renders the pool as a single Chart.js chart with two axes:
total / alive / assignable lifetimes (primary) plus the Tier-A count (dashed) and
the mean cached availability (secondary, 0–1 axis, ADR-0009). Summary cards add
pool **Tier A** and the live **working-set** count — the top
`INFINITY_STABILITY_WORKING_SET` alive-unassigned nodes the engine re-probes.

The tunnels table shows each tunnel's assigned-node **tier mix** (A/B/cold); the
nodes table carries **availability**, **score**, and **tier** columns. All of
these come from fields the engine already reports on `GET /status` and
`GET /nodes`; the panel adds no opinion about the numbers.

## Responsive layout

One light theme, no `.app-dark` variant; the frontend holds two breakpoints
instead of a framework:

- **≤760px** — tablet/desktop transition: the overview chart grid drops to one
  column, `.chart-card.wide` stops spanning two columns, and the tab bar strips
  its text labels (`.tablabel` hidden), leaving icon-only tabs with
  `aria-label`/`aria-current` still carrying the names.
- **≤560px** — phone pass: root font drops to 14px, the topbar wraps with
  `.tabs` flexed to a full row under the brand (brand text, `brand-sub` hidden)
  with horizontal scroll (`overflow-x: auto`, scrollbars hidden via
  `scrollbar-width: none`), stat cards re-grid to two columns, chart canvases
  clamp (`#pool-chart` 220px, `#proto-chart` 240px), inline forms stack
  (`label.check` checkboxes opt out of the two-column field grid and keep the
  text beside the box), primary buttons go full-width (44px targets), modals
  anchor to the bottom edge, and toasts stretch edge-to-edge.
- **Connection pill** — under 560px the pill starts collapsed
  (`data-pill-mode="collapsed"` on `body`) and shows only the colored dot;
  `#pill-toggle` (a dot-only button next to it, `aria-expanded` reflecting the
  mode) expands and collapses it, remaining visible in both modes. Manual
  expansion survives snapshots and resets only when connectivity or viewport
  mode changes. Chart grid items can shrink below the canvases' intrinsic width
  (`min-width: 0`), avoiding horizontal overflow on phones.
- **`#page-title` bar** — only visible on the phone pass; the hash router
  writes the current view's capitalized name (`route()` in `app.js`) so the
  hidden `tablabel` doesn't lose the context.
- **Card-collapse tables** — below 560px, table `thead` is hidden and every row
  renders as a card: each `td` gets `td::before { content: attr(data-label) }`
  from the `data-label` attribute the renderers emit, and columns the phones
  don't need sit on `th/td[data-desktop-only]` and vanish (e.g. nodes keeps
  id/protocol/server/state on handsets while latency/throughput/avail/score/tier
  stay desktop-only). `.row-actions` cells break the card grid: a full-width
  unlabeled footer row (dashed top border) so Copy/Test/Renew/Delete and source
  Refresh stay reachable on touch — no action can end up `data-desktop-only`.
- **Touch** — `@media (hover: none) and (pointer: coarse)` lifts `.btn` to
  40px targets; `prefers-reduced-motion` kills all pulses and transitions.

The GitHub Pages demo shares this pass: `demo/demo-bootstrap.js` re-applies the
pill mode on boot (`window.__demoPillBoot`) because it boots before app.js and
stubbed sources would otherwise render rows without `data-label`s.

## Host view (ADR-0010)

A **Host** tab operates the Docker host's own traffic through a tunnel, driven
by the privileged hostagent (see [ADR-0010](./adr/0010-hostagent.md) and
[docs/api.md](./api.md#host-control-adr-0010)):

- **System Proxy** toggle — sets the desktop session proxy to a tunnel
  (`gsettings`/KDE capability-detected); the view shows whether the host has a
  detectable desktop (`capabilities.proxy: "none"` renders the toggle disabled).
- **TUN mode** toggle — routes the whole host's network through a tunnel; the
  view shows `hostagent.capabilities.tun` and warns that a dead tunnel takes the
  host's uplink down.
- **Target selector** — every running tunnel listed with live **ping** (ms) and
  **download speed** (KiB/s) measured through the tunnel proxy, plus an **Auto
  pick best** option that asks the engine to measure all candidates and cache
  the winner for `INFINITY_HOST_PICK_TTL_S`; a **Re-measure** button forces a
  fresh pass (`POST /host/pick`). The current pick is shown with its cached
  freshness (`stale_in_s`).
- When the hostagent is unreachable the view renders the surface in a
  `hostagent.available: false` state (features disabled, last-known state shown).

The panel proxies `GET /host`, `POST /host/pick`, `POST /host/proxy`, and
`POST /host/tun` from the engine; it computes nothing about the numbers.

### Docs page (`/docs`)

The panel also serves this repository's markdown spec as a navigable page:
`GET /docs` redirects to `/docs/architecture`, and `GET /docs/<name>` renders
`README` / `CONTEXT` / the `docs/*.md` reference / every ADR through
`panel/docs.py` (markdown → HTML with fenced code, tables, sidebar nav grouped
by getting-started / reference / decisions). Topbar "Docs" link opens it; the
rendered pages reuse `app.css` and stay localhost-only like the panel itself.

### Demo build (GitHub Pages)

The frontend is zero-build, so the current design is also published as a static
**demo** to GitHub Pages by `.github/workflows/pages.yml` on every push: the
workflow copies `panel/static/` and the committed `demo/demo-bootstrap.js`
fixtures (mock snapshot/history/nodes/host data + a simulated SSE tick) into the
Pages artifact, so `https://<owner>.github.io/InfinityProxy/` always renders the
latest UI with no backend. `demo/build_demo.py` additionally pre-renders every
doc page to `_site/docs/<name>.html` (plus a `_site/docs/index.html` landing) so
the **Docs** page works on static Pages too. See `demo/` and the workflow for
the build steps.

## Engine API additions

| Endpoint | Purpose |
| --- | --- |
| `GET /nodes?state=&protocol=&source=&q=&limit=` | paginated node browse (default limit 200), server-side filters; `q` matches server/IP prefix |
| `POST /sources/<name>/refresh` | trigger a source fetch now; `404` unknown source |
| `GET /` | 302 → `INFINITY_PANEL_BASE_URL` (default `http://127.0.0.1:8000`) |
| `GET /status` | `pool` gains `untested` and `by_protocol` (counts per protocol), computed with SQL GROUP BY — no node table shipped per poll |
| `GET /host` / `POST /host/pick` / `POST /host/proxy` / `POST /host/tun` | host-wide System Proxy + TUN control via the hostagent (ADR-0010; `409 host_features_disabled` when `INFINITY_HOST_ENABLED` is `0`) |

## Out of scope

- Panel authentication / non-loopback deployment (ADR-0003 governance).
- Engine-internal metrics counters (prometheus-style); the panel graphs what the
  API already exposes plus its own rolling history.