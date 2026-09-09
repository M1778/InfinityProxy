/* InfinityProxy panel — vanilla JS, zero build. Chat.js 4 vendored (MIT). */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

const state = {
  snap: null,           // latest /api/snapshot payload
  history: null,        // /api/history columns
  nodes: null,          // last /api/nodes result
  lastSnapAt: 0,
  panels: { pool: null, tunnel: null, proto: null, donut: null },
};

/* ---------- live connection ---------- */

function setPill(label) {
  const el = $("#conn-pill");
  el.dataset.state = label;
  $("#conn-pill .plabel").textContent = label;
}

function onSnap(snap) {
  state.snap = snap;
  state.lastSnapAt = Date.now();
  setPill(snap.reachable ? "live" : "error");
  renderOverview();
  renderTunnels();
  renderSources();
  renderFooter();
}

function connectSSE() {
  const es = new EventSource("/api/events");
  es.addEventListener("snapshot", (e) => onSnap(JSON.parse(e.data)));
  es.addEventListener("heartbeat", () => { /* keeps the connection warm */ });
  es.onopen = () => setPill("live");
  es.onerror = () => {
    setPill("reconnecting");
    // Native EventSource auto-reconnects; fall back to polling while down.
    scheduleFallbackPoll();
  };
}

function snap_reachable() {
  return !!(state.snap && state.snap.reachable);
}

function scheduleFallbackPoll() {
  window.clearInterval(state.fallbackTimer);
  state.fallbackTimer = window.setInterval(async () => {
    if (Date.now() - state.lastSnapAt > 12000) {
      const snap = await api("/api/snapshot", { silent: true }).catch(() => null);
      if (snap) onSnap(snap);
    }
  }, 5000);
}

/* ---------- api ---------- */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...(opts.method ? { method: opts.method } : {}),
    ...(opts.body ? { body: JSON.stringify(opts.body) } : {}),
  });
  let payload = null;
  try { payload = await res.json(); } catch (_) { /* 204 or empty */ }
  if (!res.ok) {
    const msg = payload && payload.error ? payload.error.message : `HTTP ${res.status}`;
    if (!opts.silent) toast("error", msg);
    throw new Error(msg);
  }
  return payload;
}

function toast(kind, message) {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = message;
  $("#toasts").append(el);
  window.setTimeout(() => el.remove(), 5000);
}

function showModal(title, bodyHtml) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = bodyHtml;
  $("#modal").hidden = false;
}
function hideModal() { $("#modal").hidden = true; }
function confirm_(message) { return window.confirm(message); }

/* ---------- views/router ---------- */

function route() {
  const name = (location.hash || "#overview").slice(1);
  $$(".view").forEach((v) => { v.hidden = v.dataset.name !== name; });
  $$(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.view === name));
  if (name === "nodes" && !state.nodes) loadNodes();
  if (name === "tunnels") renderTunnels();
}
window.addEventListener("hashchange", route);

/* ---------- overview ---------- */

const CARD_DEFS = [
  ["pool", "total", "accent", "scraped nodes"],
  ["pool", "alive", "ok", "certified by relay handshake"],
  ["pool", "in_use", "warn", "assigned to tunnels"],
  ["pool", "untested", "untested", "not yet probed"],
  ["tunnels", "active", "ok", "running sing-box listeners"],
  ["tunnels", "degraded", "dead", "granted < requested"],
  ["pool", "tier_a", "ok", "evaluated by Wilson availability"],
  ["pool", "working_set", "accent", "top candidates, re-probed every 5m"],
];

function renderOverview() {
  if (!state.snap) return;
  const { status } = state.snap;
  $("#stat-cards").innerHTML = CARD_DEFS.map(([group, key, cls, sub]) =>
    `<div class="card"><div class="label">${group}.${key}</div>` +
    `<div class="value ${cls}">${status[group][key] ?? 0}</div>` +
    `<div class="sub">${sub}</div></div>`
  ).join("");
  drawProtocolChart(status.pool && status.pool.by_protocol);
  drawPoolDonut(status.pool);
  if (state.history) redrawCharts();
}

let chartThemeApplied = false;
function applyChartTheme() {
  if (chartThemeApplied || !window.Chart) return;
  chartThemeApplied = true;
  Chart.defaults.color = "#8fa3a0";
  Chart.defaults.borderColor = "#1d272c";
  Chart.defaults.font = { family: '"IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', size: 10.5 };
  Chart.defaults.animation = false;
  Chart.defaults.scale.grid.color = "#11181c";
  Chart.defaults.plugins.legend.position = "bottom";
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.plugins.legend.labels.boxWidth = 6;
  Chart.defaults.plugins.legend.labels.color = "#8fa3a0";
  Chart.defaults.plugins.tooltip.backgroundColor = "rgba(12,16,19,.96)";
  Chart.defaults.plugins.tooltip.borderColor = "#2c3a3f";
  Chart.defaults.plugins.tooltip.borderWidth = 1;
  Chart.defaults.plugins.tooltip.cornerRadius = 8;
  Chart.defaults.plugins.tooltip.padding = 10;
  Chart.defaults.plugins.tooltip.boxPadding = 4;
  Chart.defaults.plugins.tooltip.usePointStyle = true;
  Chart.defaults.plugins.tooltip.titleColor = "#d7e2ec";
  Chart.defaults.plugins.tooltip.bodyColor = "#d7e2ec";
}

function fmtClock(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtTooltipTitle(items) {
  return new Date(items[0].parsed.x).toLocaleTimeString();
}

function pooled(key) {
  const h = state.history;
  return (h[key] || []).map((v, i) => ({ x: h.t[i] * 1000, y: v }));
}

function timeXScale() {
  return {
    type: "linear",
    ticks: { maxTicksLimit: 8, maxRotation: 0, callback: (v) => fmtClock(v) },
    grid: { display: false },
    border: { display: false },
  };
}

function redrawCharts() {
  if (!window.Chart || !state.history) return;
  applyChartTheme();
  const h = state.history;
  if (!h.t || !h.t.length) return;

  if (!state.panels.pool) {
    state.panels.pool = new Chart($("#pool-chart"), {
      type: "line",
      data: { datasets: [
        { label: "dead", data: pooled("pool_dead"), stack: "pool", fill: true, backgroundColor: "rgba(255,107,94,.18)", borderColor: "#ff6b5e", borderWidth: 1.5, pointRadius: 0, tension: 0 },
        { label: "untested", data: pooled("pool_untested"), stack: "pool", fill: true, backgroundColor: "rgba(138,153,160,.18)", borderColor: "#8a99a0", borderWidth: 1.5, pointRadius: 0, tension: 0 },
        { label: "alive", data: pooled("pool_alive"), stack: "pool", fill: true, backgroundColor: "rgba(99,230,160,.18)", borderColor: "#63e6a0", borderWidth: 1.5, pointRadius: 0, tension: 0 },
        { label: "in_use", data: pooled("pool_in_use"), stack: "overlay", fill: false, borderColor: "#f2a93b", borderWidth: 1.5, pointRadius: 0, tension: 0 },
        { label: "tier_a", data: pooled("pool_tier_a"), fill: false, borderColor: "#4fd8ff", borderWidth: 1.5, borderDash: [2, 2], pointRadius: 0, tension: 0 },
        { label: "avg_score", data: pooled("pool_avg_score"), yAxisID: "y1", fill: false, borderColor: "#9fb8d8", borderWidth: 1.5, pointRadius: 0, tension: 0 },
      ]},
      options: {
        parsing: false,
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          decimation: { enabled: true, algorithm: "lttb", threshold: 1000, samples: 300 },
          tooltip: { callbacks: { title: fmtTooltipTitle } },
        },
        scales: {
          x: timeXScale(),
          y: { beginAtZero: true, stacked: true, border: { display: false }, ticks: { precision: 0, maxTicksLimit: 5 } },
          y1: { position: "right", min: 0, max: 1, border: { display: false }, ticks: { precision: 1, maxTicksLimit: 4 }, grid: { drawOnChartArea: false } },
        },
      },
    });
  }
  const poolChart = state.panels.pool;
  ["pool_dead", "pool_untested", "pool_alive", "pool_in_use", "pool_tier_a", "pool_avg_score"].forEach((key, i) => {
    poolChart.data.datasets[i].data = pooled(key);
  });
  poolChart.update("none");

  if (!state.panels.tunnel) {
    state.panels.tunnel = new Chart($("#tunnel-chart"), {
      type: "line",
      data: { datasets: [
        { label: "active", data: pooled("tunnel_active"), fill: true, backgroundColor: "rgba(79,216,255,.15)", borderColor: "#4fd8ff", borderWidth: 1.5, pointRadius: 0, stepped: true, tension: 0 },
        { label: "degraded", data: pooled("tunnel_degraded"), fill: false, borderColor: "#ff6b5e", borderWidth: 1, borderDash: [4, 4], pointRadius: 0, stepped: true, tension: 0 },
      ]},
      options: {
        parsing: false,
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: { tooltip: { callbacks: { title: fmtTooltipTitle } } },
        scales: {
          x: timeXScale(),
          y: { beginAtZero: true, border: { display: false }, ticks: { precision: 0, maxTicksLimit: 5 } },
        },
      },
    });
  }
  const tunnelChart = state.panels.tunnel;
  tunnelChart.data.datasets[0].data = pooled("tunnel_active");
  tunnelChart.data.datasets[1].data = pooled("tunnel_degraded");
  tunnelChart.update("none");
}

function drawProtocolChart(rows) {
  if (!window.Chart || !rows || !rows.length) return;
  applyChartTheme();
  const sorted = rows.slice().sort((a, b) => (b.total || 0) - (a.total || 0));
  const labels = sorted.map((r) => r.protocol);
  const keys = ["alive", "untested", "dead"];

  if (!state.panels.proto) {
    state.panels.proto = new Chart($("#proto-chart"), {
      type: "bar",
      data: {
        labels,
        datasets: [
          { label: "alive", data: sorted.map((r) => r.alive), backgroundColor: "#63e6a0" },
          { label: "untested", data: sorted.map((r) => r.untested), backgroundColor: "#8a99a0" },
          { label: "dead", data: sorted.map((r) => r.dead), backgroundColor: "#ff6b5e" },
        ],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          tooltip: {
            callbacks: {
              footer: (items) => {
                const total = items.reduce((s, it) => s + (it.parsed.x || 0), 0);
                return `total: ${total} host${total === 1 ? "" : "s"}`;
              },
            },
          },
        },
        scales: {
          x: { stacked: true, beginAtZero: true, border: { display: false }, ticks: { precision: 0 } },
          y: { stacked: true, grid: { display: false }, border: { display: false }, ticks: { autoSkip: false } },
        },
      },
    });
  }
  const chart = state.panels.proto;
  chart.data.labels = labels;
  chart.data.datasets.forEach((ds, i) => { ds.data = sorted.map((r) => r[keys[i]]); });
  chart.update("none");
}

function drawPoolDonut(pool) {
  if (!window.Chart || !pool) return;
  applyChartTheme();
  const data = [pool.alive ?? 0, pool.untested ?? 0, pool.dead ?? 0];

  if (!state.panels.donut) {
    state.panels.donut = new Chart($("#donut-chart"), {
      type: "doughnut",
      data: {
        labels: ["alive", "untested", "dead"],
        datasets: [{
          data,
          backgroundColor: ["#63e6a0", "#8a99a0", "#ff6b5e"],
          borderColor: "#0e1417",
          borderWidth: 2,
          borderRadius: 4,
          spacing: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "68%",
        plugins: { legend: { position: "bottom" } },
      },
    });
  }
  const chart = state.panels.donut;
  chart.data.datasets[0].data = data;
  chart.update("none");
  const total = pool.total ?? data[0] + data[1] + data[2];
  const totalEl = $("#donut-total");
  if (totalEl) totalEl.textContent = total;
  const inUseEl = $("#donut-inuse");
  if (inUseEl) inUseEl.textContent = `${pool.in_use ?? 0} in use`;
}

/* ---------- tunnels view ---------- */

function stateBadge(s, degraded) {
  if (degraded) return `<span class="badge warn">degraded</span>`;
  return `<span class="badge ${s === "running" ? "ok" : s === "error" ? "dead" : "idle"}">${esc(s)}</span>`;
}

function tierMix(nodes) {
  const a = nodes.filter((n) => n.tier === "A").length;
  const b = nodes.filter((n) => n.tier === "B").length;
  const cold = nodes.length - a - b;
  const parts = [];
  if (a) parts.push(`<span class="badge ok">A ${a}</span>`);
  if (b) parts.push(`<span class="badge idle">B ${b}</span>`);
  if (cold) parts.push(`<span class="badge untested">${cold}</span>`);
  return parts.join(" ") || '<span class="muted">—</span>';
}

function renderTunnels() {
  if (!state.snap) return;
  const rows = state.snap.tunnels || [];
  $("#tunnel-table tbody").innerHTML = rows.map((t) => {
    const health = t.health || {};
    const creds = `${t.username}:${t.password}@127.0.0.1:${t.port}`;
    return `<tr>
      <td class="mono">${esc(t.id)}</td>
      <td class="mono">127.0.0.1:${t.port}</td>
      <td>${t.node_count_granted}/${t.node_count_requested}</td>
      <td>${tierMix(t.nodes || [])}</td>
      <td>${stateBadge(t.state, t.degraded)}</td>
      <td class="muted">${esc(health.healthy ? "healthy" : "unhealthy")}${health.dead_swapped_24h ? ` · ${health.dead_swapped_24h} swap` : ""}${health.restarts_24h ? ` · ${health.restarts_24h} restart` : ""}</td>
      <td>
        <div class="actions">
          <button class="btn sm" data-act="copy" data-creds="${esc(creds)}" title="copy endpoint+credentials">Copy</button>
          <button class="btn sm" data-act="test" data-id="${esc(t.id)}">Test</button>
          <button class="btn sm" data-act="renew" data-id="${esc(t.id)}">Renew</button>
          <button class="btn sm danger" data-act="delete" data-id="${esc(t.id)}">Delete</button>
        </div>
      </td>
    </tr>`;
  }).join("") || `<tr><td colspan="7" class="muted">No tunnels. Create one above.</td></tr>`;
}

async function createTunnel(e) {
  e.preventDefault();
  const count = parseInt($("#create-count").value, 10);
  const auto = $("#create-renew").checked;
  if (!Number.isFinite(count) || count < 1) return toast("error", "node_count must be >= 1");
  try {
    await api("/api/tunnels", { method: "POST", body: { node_count: count, auto_renew: auto } });
    toast("ok", "Tunnel created — it appears on the next refresh.");
  } catch (_) { /* toast shown */ }
}

async function renewTunnel(id, btn) {
  btn.disabled = true;
  try {
    await api(`/api/tunnels/${encodeURIComponent(id)}/renew`, { method: "POST" });
    toast("ok", `${id} renewed.`);
  } finally { btn.disabled = false; }
}

async function deleteTunnel(id) {
  if (!confirm_(`Delete tunnel ${id}? Its nodes return to the pool.`)) return;
  try {
    await api(`/api/tunnels/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast("ok", `${id} deleted.`);
  } catch (_) { /* toast shown */ }
}

async function testTunnel(id) {
  showModal("Testing tunnel " + id, `<div class="test-result"><span class="label">dialing…</span></div>`);
  try {
    const r = await api(`/api/tunnels/${encodeURIComponent(id)}/test`, { method: "POST" });
    const html = r.ok
      ? `<div class="test-result">
           <span class="label">exit ip</span><span class="mono">${esc(r.exit_ip)}</span>
           <span class="label">latency</span><span>${esc(r.latency_ms)} ms</span>
         </div>`
      : `<div class="test-result"><span class="label">failed</span><span>${esc(r.error || "unknown")}</span></div>`;
    showModal("Test " + id, html);
  } catch (_) { hideModal(); }
}

/* ---------- nodes view ---------- */

async function loadNodes() {
  const params = new URLSearchParams();
  const state_ = $("#f-state").value; if (state_) params.set("state", state_);
  const proto = $("#f-protocol").value; if (proto) params.set("protocol", proto);
  const source = $("#f-source").value; if (source) params.set("source", source);
  if ($("#f-inuse").checked) params.set("in_use", "1");
  const q = $("#f-q").value.trim(); if (q) params.set("q", q);
  const limit = parseInt($("#f-limit").value, 10);
  params.set("limit", Number.isFinite(limit) && limit >= 1 && limit <= 1000 ? limit : 200);
  try {
    const res = await api("/api/nodes?" + params.toString());
    state.nodes = res;
  } catch (_) { state.nodes = null; }
  renderNodes();
}

function renderNodes() {
  if (!state.nodes) return;
  const rows = state.nodes.nodes || [];
  $("#node-count").textContent = `${rows.length} node${rows.length === 1 ? "" : "s"} shown (limit ${state.nodes.limit}) — click a row for the raw URI.`;
  $("#node-table tbody").innerHTML = rows.map((n) => `<tr class="row-click" data-id="${esc(n.id)}">
    <td class="mono">${esc(n.id.slice(0, 16))}…</td>
    <td>${esc(n.protocol)}</td>
    <td class="mono">${esc(n.server)}:${n.port}</td>
    <td>${esc(n.source)}</td>
    <td><span class="badge ${n.state === "alive" ? "ok" : n.state === "dead" ? "dead" : "untested"}">${esc(n.state)}</span></td>
    <td class="mono">${n.last_latency_ms ?? "—"} ms</td>
    <td class="mono">${n.throughput_kb_s ? Math.round(n.throughput_kb_s) + " KiB/s" : "—"}</td>
    <td>${n.in_use ? '<span class="badge warn">in use</span>' : '<span class="badge idle">free</span>'}</td>
    <td class="mono">${n.availability != null ? (n.availability * 100).toFixed(0) + "%" : "—"}</td>
    <td class="mono">${n.score != null ? n.score.toFixed(2) : "—"}</td>
    <td>${n.tier ? `<span class="badge ${n.tier === "A" ? "ok" : "idle"}">${esc(n.tier)}</span>` : '<span class="badge untested">cold</span>'}</td>
  </tr>`.trim()).join("") || `<tr><td colspan="11" class="muted">No nodes match the filters.</td></tr>`;
}

async function nodeDetail(id) {
  try {
    const n = await api(`/api/nodes/${encodeURIComponent(id)}`);
    showModal("Node " + id.slice(0, 16) + "…", `
      <pre>${esc(n.uri)}</pre>
      <div class="actions"><button class="btn sm" data-copy="${esc(n.uri)}">Copy URI</button></div>
    `);
    const btn = $("#modal").querySelector("[data-copy]");
    btn.onclick = () => copyText(btn.dataset.copy);
  } catch (_) { /* toast shown */ }
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("ok", "Copied."); }
  catch (_) { toast("error", "Clipboard unavailable."); }
}

/* ---------- sources view ---------- */

function renderSources() {
  if (!state.snap) return;
  const rows = state.snap.status.sources || [];
  $("#source-table tbody").innerHTML = rows.map((s) => `
    <tr>
      <td>${esc(s.name)}</td>
      <td class="mono">${fmtAge(s.last_fetch_s)}</td>
      <td class="mono">${fmtAge(s.next_fetch_s)}</td>
      <td><button class="btn sm" data-refresh="${esc(s.name)}">Refresh now</button></td>
    </tr>`).join("") || `<tr><td colspan="4" class="muted">No sources tracked.</td></tr>`;
}

function fmtAge(sec) {
  if (sec === null || sec === undefined) return "—";
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}

async function refreshSource(name, btn) {
  btn.disabled = true;
  try {
    await api(`/api/sources/${encodeURIComponent(name)}/refresh`, { method: "POST" });
    toast("ok", `${name} scrape kicked off.`);
  } finally { btn.disabled = false; }
}

/* ---------- footer ---------- */

function renderFooter() {
  const cfg = state.snap && state.snap.config ? state.snap.config : null;
  if (!cfg) return;
  $("#footer").innerHTML =
    `engine <code>${esc(cfg.engine_url)}</code><br>` +
    `panel <code>${esc(cfg.panel_host)}:${esc(cfg.panel_port)}</code> — ` +
    `localhost only, no auth (ADR-0003) · tunnels <code>127.0.0.1:10000-59999</code>`;
}

/* ---------- history + init ---------- */

async function loadHistory() {
  try {
    state.history = await api("/api/history", { silent: true });
    redrawCharts();
  } catch (_) { /* engine may be down */ }
}

async function init() {
  const snap = await api("/api/snapshot", { silent: true }).catch(() => null);
  if (snap) onSnap(snap);
  await loadHistory();
  window.setInterval(loadHistory, 30000);

  // delegated events
  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (t.dataset.act === "copy") copyText(t.dataset.creds);
    else if (t.dataset.act === "renew") renewTunnel(t.dataset.id, t);
    else if (t.dataset.act === "delete") deleteTunnel(t.dataset.id);
    else if (t.dataset.act === "test") testTunnel(t.dataset.id);
    else if (t.dataset.refresh) refreshSource(t.dataset.refresh, t);
    else if (t.closest("#node-table tr.row-click")) nodeDetail(t.closest("tr").dataset.id);
  });
  $("#create-form").addEventListener("submit", createTunnel);
  $("#node-filter").addEventListener("submit", (e) => { e.preventDefault(); loadNodes(); });
  $("#modal-close").addEventListener("click", hideModal);
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") hideModal(); });

  connectSSE();
  route();
}

init();