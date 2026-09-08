# ADR-0007: Web panel — localhost, unauthenticated

Status: **accepted**

The dashboard is a separate process (`panel/`, `python -m panel`) with its own
loopback listener. Default layout:

| Service | Address | Env | Default |
| --- | --- | --- | --- |
| Engine control API | `127.0.0.1:8787` | `INFINITY_PORT` / `INFINITY_HOST` | `8787` / `127.0.0.1` |
| Panel | `127.0.0.1:8000` | `INFINITY_PANEL_PORT` / `INFINITY_PANEL_HOST` | `8000` / `127.0.0.1` |
| Panel → engine | — | `INFINITY_ENGINE_URL` | `http://127.0.0.1:8787` |

The panel inherits [ADR-0003](./0003-localhost-control-api.md) semantics: bound
to loopback, **unauthenticated**, and it never exposes a non-loopback route. Any
future remote/authenticated control applies to both surfaces together.

Port 8000 is the panel's home (common, memorable web port, and tunnel ports
start at 10000 so there is no overlap). The engine moved to 8787 when the
dashboard left the engine process, so it stays off the kitty of common
localhost dev ports. Neither port is a boundary — loopback binding is.

**Panel mechanics:**
- The panel crawls the engine control API every 5s (two small GETs), keeps a
  bounded 2h rolling history in memory, and pushes an assembled snapshot to the
  browser over **Server-Sent Events** (heartbeat every 15s; the client falls
  back to a 5s `fetch` poll while the stream is down). WebSockets were rejected
  as heavier than needed.
- Frontend is **zero-build** vanilla JS with Chart.js 4 vendored into
  `panel/static/vendor/` (MIT, license file included). Charts need no CDN, so
  the panel works fully offline on a LAN host.
- Every engine action is reachable from the browser: create/test/renew/delete
  tunnels, refresh sources, browse/filter nodes. The panel proxies the engine
  API; it does not re-implement engine logic.
- **Test tunnel** is the only feature needing panel-originated outbound
  traffic: the panel dials `INFINITY_PANEL_TEST_URL` (default
  `https://api.ipify.org`) *through* the tunnel's proxy to prove egress.

**Considered options:**
- *Serve the dashboard from the engine process* (v1 "read-only dashboard") —
  one port, but it locked 8000 to the engine and made the dashboard hostage to
  engine code. The panel is now a separate service sharing one image.
- *WebSockets* — bidirectional when we only need server→client pushes; SSE plus
  plain REST proxy calls is simpler and works through naive proxies.
- *A framework / build step for the frontend* — a node toolchain for one page of
  charts is a maintenance tax; vendored UMD + vanilla JS keeps the image small.

**Consequences:**
- Two localhost listeners instead of one; compose runs two services.
- The docs must keep reminding operators not to publish **either** port
  (8000 or 8787).
- The engine's `GET /` is a 302 redirect to the panel so a stray browser hit
  still lands somewhere useful; the old in-engine HTML dashboard is gone.
- Because the panel is unauthenticated like the engine, a browser open on
  `127.0.0.1:8000` can perform every tuning/delete action — acceptable for a
  single-operator localhost boundary (same threat model as ADR-0003).