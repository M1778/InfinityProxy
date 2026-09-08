# Dashboard graph research: libraries + chart types + design

Research report, no production code. Grounded in the live panel code
(`panel/static/app.js`, `app.css`, `vendor/chart.umd.min.js`, `panel/app.py`,
`panel/history.py`) and the data available to the panel. Scope: a dark,
localhost-only, single-file-vendored, zero-build dashboard whose charts must
survive a 5 s poll cadence over a 2 h rolling window (~1,440 samples/series).

## TL;DR

1. **The ugliness is mostly a theming bug, not a library bug.** `chartTheme()`
   in `app.js:131` is **dead code — it is never called and nothing is wired to
   `Chart.defaults`**. All three charts run Chart.js's stock default styling:
   default font, default gray grid, no tooltip/legend styling. The alpha
   `backgroundColor`s on the pool chart never render because `fill: false` is
   set on every dataset. Fixing the theme is the single biggest win and costs
   ~1 hour in Chart.js.
2. **You should stay on Chart.js 4** (already vendored, MIT, 206 KB) and fix
   theme + decimation + cadence. It is performance-sufficient: 6 series ×
   1,440 points is trivial once animations are off (they already are) and
   `decimation` keeps hover cheap.
3. **The one library worth the migration risk** is **uPlot (~48 KB min / ~16 KB
   gz, MIT, single IIFE)** — for the two time-series charts only. It is an
   order-of-magnitude faster to draw and stream, looks excellent with a
   Linear/Vercel-style minimal theme, and drops 160 KB off the page. Keep
   Chart.js next to it for the categorical/bar/donut charts.
4. **ApexCharts is disqualified** — as of v7 (2026) it is revenue-gated/commercial
   for orgs at or above $2 M revenue and carries watermark-able premium
   features. Not a clean permissive vendor for an MIT project. **Plot.js and
   lightweight-charts don't fit**: Plot re-renders the whole chart per data
   join (bad for a 2 h stream) and needs two bundled files (+D3); lightweight-charts
   is finance-flavored and cannot do the categorical protocol chart at all.
5. **Graph-type changes that matter:** convert the pool lines into a
   *stacked time-area* of the true state partition (alive + dead + untested =
   total) with `in_use` overlaid as a line; flip the protocol bars to
   *horizontal* stacked bars; add a *donut ring* for instantaneous pool-state
   share; add *card sparklines*. Skip streamgraphs (misleading given
   `in_use ⊆ alive` double-count) and Gantts (no per-tunnel history in the API).

---

## 1. Library landscape

Facts verified against the vendored file and each project's current docs.

| Library | Ver (cur) | Minified size | Realtime 5 s / 1,440-pt | Dark look out of box | License | Fits no-build vendored? |
| --- | --- | --- | --- | --- | --- | --- |
| **Chart.js** (current) | 4.4.7 vendored; 4.5.x current | ~206 KB min / ~62 KB gz (already in repo) | Good — needs `decimation` + `parsing:false` + `animation:false` (already off) | Weak — generic by default; every pixel of theme is manual | MIT | Yes — already vendored UMD |
| **uPlot** | 1.6.32 | ~48 KB min / ~16 KB gz | Best-in-class — canvas, ~100 k pts/s, scroll/`setData` native | None shipped — you draw your own axis/legend (easy, clean look) | MIT | Yes — `dist/uPlot.iife.min.js` drop-in |
| **ECharts** | 5.5.x | Full dist ~1 MB min / ~340 KB gz; **custom single-file build ~180–300 KB min** via official online builder | Good — `sampling` (min-max/LTTB) built in, `setOption` diffing, large-data mode | Best out of box — native `dark` theme, animations, gradients, stream series | Apache-2.0 (permissive; keep NOTICE) | Yes — one custom `echarts.min.js` from builder |
| **Observable Plot** | 0.6.17 | `plot.umd.min.js` ~204 KB min **+ separate D3 (~75 KB min)** | Poor fit — declarative, re-joins and re-renders the whole chart per update; no native streaming scroll | Excellent for static/exploratory charts | ISC (permissive) | Weak — needs two files; update model is anti-streaming |
| **lightweight-charts** | 5.x | ~35 KB base (standalone larger) | Excellent — built for tick streaming, auto-scroll time scale | Nice but finance-flavored (price/time scale, crosshair) | Apache-2.0 — **requires TradingView attribution link** in public pages | Yes as standalone IIFE, but **no bar/donut/category charts** → still need a second lib |
| **ApexCharts** | 7.x | ~500 KB–1 MB min | Poor at 1,440 pts — SVG-based; canvas renderer exists but opt-in | Good dark mode | **Dual/commercial — revenue-gated ≥ $2 M/yr; premium features watermark without key** | No — license + weight |

### Ranking against the four criteria

- **(a) Fits vendored single-file no-build:** Chart.js = uPlot (both single
  drop-in files you already/or can vendor) > ECharts (single custom file) >
  lightweight-charts (single standalone) > Plot (needs D3 too) > ApexCharts.
- **(b) Realtime 5 s + large rolling window:** uPlot > lightweight-charts ≈
  ECharts (with sampling) > Chart.js (fine here, needs decimation) > Plot >
  ApexCharts.
- **(c) Dark aesthetics out of the box:** ECharts > ApexCharts > Plot ≈
  lightweight-charts > Chart.js (no — that's precisely why it looks ugly) >
  uPlot (none, but trivial to theme).
- **(d) Permissive license:** Chart.js, uPlot (MIT), Plot (ISC) clean; ECharts,
  lightweight-charts Apache-2.0 fine (attribution/NOTICE obligations);
  **ApexCharts not clean** (revenue-gated commercial license + premium
  watermarking). None of these are GPL — Chart.js's default "no GPL" constraint
  holds across the board. Flag: only ApexCharts breaks permissiveness.

### Is uPlot or lightweight-charts a *worthwhile* upgrade?

- **uPlot — yes, barely, and only for the time-series.** The two line charts
  are the only _streaming_ visuals; uPlot turns a 30 s, occasionally-janky
  redraw into a 60 fps-native scroll, at ¼ the payload, with a dramatically
  cleaner aesthetic ceiling. The cost is a rewrite of ~80 lines
  (`redrawCharts`) and hand-rolled cursor/legend (uPlot ships no tooltip).
- **lightweight-charts — no.** It would beautify the time-series but cannot
  render the protocol chart, so you'd still vendor Chart.js beside it: two libs,
  no fewer. uPlot is the better second lib (MIT, no attribution clause, more
  flexible).
- **Conclusion:** either *stay on Chart.js and polish*, or *uPlot for lines +
  Chart.js for categories*. ECharts is the "I want maximal polish and accept a
  single ~300 KB file" option, and is *also* the only one of the three that can
  natively do every chart type recommended in §3 (lines, stacked bar, donut,
  heatmap, stream) in **one** vendored custom build.

---

## 2. Current state — exactly what reads as "ugly"

From `app.js`/`app.css`:

- **`chartTheme()` is dead** (`app.js:131` returns a theme object that nothing
  consumes). No `Chart.defaults` override anywhere → stock Chart.js font,
  untuned grid color, no tooltip/legend styling on any chart. This alone is
  most of the perceived ugliness.
- **`fill: false` on every pool dataset** (`app.js:150–153`) → the decorative
  `backgroundColor: rgba(...)` values are never drawn. Pointless code *
  invisible intent.
- **Inconsistent axis config across charts:** pool has `x.grid.display:false`
  + no y grid/beginAtZero; tunnel has `beginAtZero` + `precision:0`; proto has
  **no axis styling at all**. Same data family, three different grid looks.
- **4 over-laid pool lines** with 3 of them dashed (`borderDash`) at equal
  visual weight → "spaghetti". `tension:.25` smooths noise that the X axis
  can't justify at 1,440 points.
- **Time labels are pre-stringified** with `toLocaleTimeString()` (line-by-line
  labels), so the x-axis is a category axis: intervals drift, ticks are dense,
  and you can't zoom or use Chart.js's time scale.
- **Vertical protocol bars**, 8 long labels on a ≤260 px canvas → cramped/rotated.
- **Cadence:** history repaints on a 30 s `loadHistory` interval
  (`app.js:374`) even though SSE snapshots arrive every 5 s — the "realtime"
  charts are effectively 30 s (and full re-render each time, no decimation).
- `pool_total` and `pool_assignable` are collected (`history.py`, `_metrics_from`)
  but **never rendered** — free signal currently thrown away.

---

## 3. Graph types that fit this data

Map each candidate to the *actual* columns/fields, and honest value:

### Keep + fix

| Panel | Chart | Data | Verdict |
| --- | --- | --- | --- |
| Pool (time) | **Stacked time-area of state partition** | `pool_alive`, `pool_dead`, `pool_untested` over 2 h — note `alive+dead+untested = total` is the true partition | Keep, but restate as an area stack — composition change over time is exactly the ingestion-health signal. Do **not** naively stack `in_use` into it: `in_use ⊆ alive` (a node has one state; in-use is a flag on live nodes), stacking both double-counts. Overlay `in_use` as a distinct line and `pool_total` as a faint reference. |
| Tunnels (time) | Two small `active`/`degraded` lines | `tunnel_active`, `tunnel_degraded` (small integers) | Keep. Restyle only. |
| Protocol (snapshot) | **Horizontal** stacked bars | `pool.by_protocol[]` {alive, untested, dead} | Keep, flip horizontal. 8 protocols × 3 states fit a horizontal layout with readable labels; sort by total. |

### Add (genuinely useful, data already available)

| Chart | Data | Why it earns its place |
| --- | --- | --- |
| **Donut/ring gauge** of pool state share | `status.pool.{alive,dead,untested,total}` | One-glance answer to "is the pool healthy right now?" Center shows total. Replaces the mental math on the stat cards. Chart.js doughnut (or ECharts gauge). |
| **Sparklines in stat cards** | `pool_*`, `tunnel_*` history columns | Six tiny area strips (60–120 px) under the card values. Massive perceived polish at near-zero cost. |
| **Source freshness strip** | `status.sources[]` `{name, last_fetch_s, cadence_s}` | Turns the plain sources table into a visual: one horizontal bar per source = time-since-last-fetch vs cadence, colored healthy/overdue. Cheap, operational. |
| **Alive-node latency histogram** | `nodes[].latency_ms` (state=alive) | "Are our certified nodes actually fast?" — a single histogram of current alive latencies (fetch `/api/nodes?state=alive&limit=` per poll, or add to snapshot). Optional; strong for a relay-grade-liveness product. |

### Skip (gimmick / data-blocked)

| Idea | Why not |
| --- | --- |
| **Streamgraph / stacked-stream** (ECharts `type:'stream'`) | Pretty but noise for 4 categories; the shared-stream "full width = share of whole" reading is **actively misleading** here due to the `in_use ⊆ alive` overlap (see above). |
| **Liveness heatmap (source × state over time)** | *Would* be the best chart for spotting scrape storms, but the ring-buffer history (`history.py`) stores only the 8 pool/tunnel counters — **no per-source or per-protocol time history exists today**. Flag as a future add; it needs new `_metrics_from` columns (+ maybe a per-source fetch-success series). The client side can be built-ready now. |
| **Gantt / tunnel lifecycle** | Needs per-tunnel `created_at` / event history — not in the control API. Data-blocked; don't build against it. |
| Scatter of node latency vs node age | No node-age field. Skip. |

---

## 4. Concrete design recommendations

### 4.1 Library decision

- **Plan A (recommended, minimal risk):** stay on **Chart.js 4** for all three
  charts. Fix theme, decimation, cadence, and chart-type choices. No new vendor
  files, no migration.
- **Plan B (meaningful upgrade):** add **uPlot** and move the **two time-series
  charts** to it; keep Chart.js for the protocol horizontal bars (+donut).
  Vendor `uPlot.iife.min.js` (~48 KB, MIT) next to chart.umd.min.js. This is the
  only migration worth its risk, and it's contained to `redrawCharts()`.
- **Plan C (max polish, one dependency):** swap to **ECharts 5.5 custom build**
  (~180–300 KB single file, Apache-2.0, native `dark` theme). All three chart
  types plus future heatmap/gauge in one lib. Rewrites all chart code; payload
  grows ~half of Chart.js's — irrelevant for localhost.

### 4.2 Theme fixes (apply regardless of Plan)

Wire a real theme, once. Two moves in `app.js`:

- Delete the dead `chartTheme()` and instead set globals so all three charts
  inherit one look:
  ```js
  Chart.defaults.font = { family: "system-ui, sans-serif", size: 11 };
  Chart.defaults.color = "#7d92a5";
  Chart.defaults.borderColor = "#1e2a35";
  Chart.defaults.animation = false;               // streaming: no tween on update
  Chart.defaults.plugins.tooltip.backgroundColor = "rgba(16,22,29,.95)";
  Chart.defaults.plugins.tooltip.borderColor = "#1e2a35";
  Chart.defaults.plugins.tooltip.borderWidth = 1;
  Chart.defaults.plugins.tooltip.titleColor = "#d7e2ec";
  Chart.defaults.plugins.tooltip.bodyColor = "#d7e2ec";
  Chart.defaults.plugins.tooltip.padding = 10;
  Chart.defaults.plugins.tooltip.boxPadding = 4;
  Chart.defaults.plugins.tooltip.usePointStyle = true;
  ```
  (This mirrors the existing CSS variables in `app.css` — the palette is already
  good; it's just never applied to the charts.)

### 4.3 Per-chart spec

**Pool + tunnel time-series (Chart.js)**
- Time axis, not pre-stringified labels:
  ```js
  data: { datasets: [ { label:"alive", data: rows.map(r=>({x:r.t,y:r.pool_alive})), parsing: false } ] },
  options: {
    parsing: false,
    normalized: true,
    spanGaps: true,
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: { decimation: { enabled: true, algorithm: "lttb", samples: 300, threshold: 1000 } },
    scales: { x: { type: "time", time: { unit: "minute", displayFormats: { minute: "HH:mm" } } },
              y: { beginAtZero: true, ticks: { precision: 0, maxTicksLimit: 5 } } }
  }
  ```
  > Chart.js decimation gotcha: it copies the original series into
  > `dataset._data` and re-`data` on each decimation pass. When you then
  > *append* (stream) you must push into **both** `dataset.data` and
  > `dataset._data`, or the decimated series stalls (known issue #11929). With a
  > full-window refetch every 5 s (your model today: `state.history` is replaced
  > wholesale, datasets are re-assigned) you're safe — just don't switch to
  > incremental `.push()`. Also the auto-decimation during draw only kicks in
  > when `tension`, `stepped`, and `borderDash` are **defaults** — so drop the
  > `.25` tension and dashes on the big lines to keep it fast.
- Pool: stacked area of `alive` (+`dead`+`untested`), overlay `in_use` line,
  faint `pool_total` reference. Hero styling only on `alive`:
  ```js
  // hero series — gradient area, points off, no dash
  { label:"alive", borderColor:"#34d399", borderWidth:1.5,
    pointRadius:0, tension:0,
    fill:false, // keep fill off while streaming to avoid gradient redraw cost
  }
  // secondary series — thin, dashed, half alpha
  { label:"dead", borderColor:"#f87171", borderWidth:1, borderDash:[3,3], pointRadius:0 }
  ```
  (A scriptable `CanvasGradient` under the hero line is the classic polished
  touch but costs a per-frame gradient alloc; acceptable at 5 s cadence. If you
  want it: build it in the `beforeDatasetsDraw` hook against `chartArea`, not in
  the dataset `backgroundColor` callback, which Chart.js calls repeatedly.)
- Tunnels: same theme, `y: { precision: 0 }`, `spanGaps: true`. It's a
  step-count series — consider `stepped: true` to communicate "count changed"
  honestly instead of the current airborne `.3` tension.
- Grid: horizontal splits only, `#151d26` (or `rgba(255,255,255,.04)`), hide the
  y-border line, hide vertical grid.

**uPlot (Plan B) time-series pattern**
```js
const u = new uPlot({
  width, height: 260, legend: { show: true },
  cursor: { x: true, y: false },
  axes: [ { stroke:"#1e2a35", grid:{ stroke:"#151d26" }, ticks:{ stroke:"#1e2a35" } },
          { stroke:"#1e2a35", grid:{ stroke:"#151d26" } } ],
  scales: { y: { auto: true, range: [0, null] } },
  series: [ { label:"t" }, { label:"alive", stroke:"#34d399", width:1.5, fill:"rgba(52,211,153,.08)",
            paths: uPlot.paths.linear() /* or .stepped() for tunnels */ },
            { label:"dead", stroke:"#f87171", width:1, dash:[3,3] } ],
}, [[],[],[]], el);
// each 5 s tick: push t + values into each column, shift when > 1440, then:
u.setData(aliveSeries);   // single setData = the update
```
uPlot is columnar (`data[0]=x`, `data[i]=series`) and re-lays-out on `setData`
without a full re-create — this exact pattern is its sine-stream demo at 60 fps.
Legend/cursor are opt-in so it naturally produces the spare "Linear" look.

**Protocol chart → horizontal stacked bars**
```js
{ type: "bar",
  options: { indexAxis: "y",                                   // horizontal
    scales: { x: { stacked: true, beginAtZero: true },
              y: { stacked: true, grid: { display:false } } },
    plugins: { tooltip: { callbacks: { footer: (items) =>
        `total: ${items.reduce((s,it)=>s+it.parsed.x as number,0)}` } } } },
  data: { datasets: [
    { label:"alive",  backgroundColor:"#34d399", borderRadius: 3, borderSkipped:false },
    { label:"untested", backgroundColor:"#94a3b8", borderRadius: 3, borderSkipped:false },
    { label:"dead",   backgroundColor:"#f87171", borderRadius: 3, borderSkipped:false } ] } }
```
Sort protocols by total so the healthiest sits on top; keep the 3-state order
alive/untested/dead so stacked caps are consistent.

**Donut (pool state share)**
```js
{ type: "doughnut",
  options: { cutout: "68%", plugins: { legend: { position:"bottom" } } },
  data: { datasets: [{ data:[alive,dead,untested],
    backgroundColor:["#34d399","#f87171","#94a3b8"],
    borderColor:"#10161d", borderWidth:2, borderRadius:4, spacing:2 }] } }
```
Center total via a tiny `plugins` entry that renders `pool.total` in the middle
(box: text = tabular-nums, muted label above).

**Card sparklines (any plan):** a 60–120 px `<canvas>` per stat card, new
Chart.js line or uPlot with `axes/legend/cursor` all disabled, `animation:false`,
one color each. This is the cheapest "wow" on the page.

**ECharts (Plan C) snippets**
```js
const chart = echarts.init(el, "dark");          // dark theme is native
chart.setOption({
  animationDurationUpdate: 300,
  xAxis: { type: "time" }, yAxis: { type: "value", minInterval: 1 },
  legend: { bottom: 0, itemWidth: 8, itemHeight: 8, textStyle: { color: "#7d92a5" } },
  tooltip: { trigger: "axis", backgroundColor: "rgba(16,22,29,.95)", borderColor: "#1e2a35" },
  series: [
    { name: "alive", type: "line", showSymbol: false, sampling: "min-max",     // LTTB or min-max, built in
      areaStyle: { opacity: .15 }, data: history.map(r => [r.t*1000, r.pool_alive]) },
    { name: "in_use", type: "line", showSymbol: false, sampling: "min-max", lineStyle: { type: "dashed" } },
  ],
});
// 5 s tick:
chart.setOption({ series: [{ data: newRows }] });   // ECharts diffs, animates in the delta
```
Horizontal stacked protocol: `xAxis:{type:"value"}, yAxis:{type:"category"}` +
`series:[{type:"bar", stack:"s"}, ...]`. Donut: `type:"pie", radius:["68%","82%"]`.
Data-zoom: `dataZoom:[{type:"inside"}]` gives free zoom/pan on the 2 h window —
an ECharts-only win worth naming.

### 4.4 General polish checklist

- Typography: `Chart.defaults.font` 11 px system-ui; tooltip/label numerals via
  `font-variant-numeric: tabular-nums` on the chart card CSS so counts don't
  jitter width on refresh.
- Grid: horizontal-only, `#151d26`, 1 px; no y-axis vertical line (or a single
  `#1e2a35` baseline). Modern dashboards hide the axis frame and keep only
  ticks.
- Tooltips: fixed dark card (`--surface` bg, `--border` 1 px, radius 8, padding
  10×12), crosshair off for streams, `mode:"index"`, title `HH:mm:ss`.
- Legend: bottom (`position:"bottom"`), `usePointStyle`, tiny 6×6 handles,
  muted `#7d92a5` labels, hover-active toggle kept (free).
- Colors: **keep the existing palette** — it already matches the CSS vars
  (`--ok`#34d399, `--warn`#fbbf24, `--dead`#f87171, `--untested`#94a3b8,
  `--accent`#38bdf8). Consistency with the badges/cards is what makes it feel
  designed; do not invent a second palette.
- Animation: `update("none")` while streaming (already done), a tame
  `animation:{duration:300}` only on first paint; cursor/tooltip transitions on.
- Then disable: `fill:false` alpha leftovers, the dead `chartTheme()`, the
  vertical-grid asymmetry.
- Empty state: charts should render a muted "waiting for data…" overlay when a
  series is empty, instead of a bare grid.
- Reduce motion: respect `prefers-reduced-motion` for the few animations.
- Color-blind check: alive=green vs dead=red fails deuteranopia; keep the
  dash/solid encoding that's already there and add the legend text, so color is
  never the only channel.

### 4.5 Delivery order (least risk first)

1. Wire a single global theme (`Chart.defaults`), kill `chartTheme()`, unify
   the three charts' axes. — *1 h, biggest visual return.*
2. Pool → stacked area (true partition) + `in_use` overlay; protocol → horizontal
   stacked sorted bars; add the donut + card sparklines.
3. Move time-series to a real time x-scale (`parsing:false` + `decimation`) and
   drop the `loadHistory` cadence to the 5 s SSE tick.
4. (Optional) Plan B: uPlot for the two stream charts.
5. (Deferred) if per-source history ever lands in `history.py`, the heatmap
   becomes worth building (ECharts `heatmap` or Plot raster).

### 4.6 Files that change per plan

| Plan | New vendor file | JS edited | Backend |
| --- | --- | --- | --- |
| A (stay Chart.js) | none | `app.js` (theme, charts) + `app.css` (card polish) | none |
| B (+uPlot) | `vendor/uPlot.iife.min.js` (~48 KB) | same as A + `redrawCharts` rewritten for pool/tunnel | none |
| C (ECharts) | replace `chart.umd.min.js` with custom `echarts.min.js` (~180–300 KB) | all three chart fns rewritten | none |

All plans are frontend-only; `_metrics_from`/`history.py` stay untouched. Docs
convention: any behaviour change must update `docs/dashboard.md` + ADR-0007 in
the same change.

---

## Appendix — license status (all must be permissive; project is MIT)

| Library | License | Compatible with MIT repo? | Notes |
| --- | --- | --- | --- |
| Chart.js 4 | MIT ✓ | Yes | Already vendored + `LICENSE-CHARTS.md` present |
| uPlot | MIT ✓ | Yes | No attribution clause |
| ECharts 5/6 | Apache-2.0 | Yes | Keep the Apache NOTICE/LICENSE text alongside |
| Observable Plot | ISC | Yes | Equivalent to MIT in practice |
| lightweight-charts 5 | Apache-2.0 | Yes (conditionally) | Requires TradingView attribution link on public pages (NOTICE) |
| **ApexCharts 7** | **Commercial / revenue-gated** | **No — flag** | Free < $2 M/yr revenue; paid license above; premium features watermark without a key. Historically MIT, but current v7 docs (2026) are not. **Do not vendor.** |
| Highcharts / Plotly.js | commercial-Enterprise-style split | No / avoid | Not evaluated as candidates for this reason |

No candidate is GPL, so the repo's GPL concern (Epodonios sources) has no
chart-side analogue.