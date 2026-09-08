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
  panels: { pool: null, tunnel: null, proto: null },
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
  if (state.history) redrawCharts();
}

function chartTheme() {
  return {
    borderColor: "#1e2a35",
    font: { family: "system-ui, sans-serif", color: "#7d92a5" },
    grid: { color: "#151d26" },
    ticks: { color: "#7d92a5" },
    pointBackgroundColor: "#10161d",
  };
}

function redrawCharts() {
  if (!window.Chart) return;
  const h = state.history;
  const labels = h.t.map((s) => new Date(s * 1000).toLocaleTimeString());
  const series = (key) => h[key] || [];

  const poolChart = state.panels.pool || new Chart($("#pool-chart"), {
    type: "line",
    data: { labels, datasets: [
      { label: "alive", data: series("pool_alive"), borderColor: "#34d399", backgroundColor: "rgba(52,211,153,.15)", fill: false, tension: .25 },
      { label: "in_use", data: series("pool_in_use"), borderColor: "#fbbf24", borderDash: [4, 3], fill: false, tension: .25 },
      { label: "untested", data: series("pool_untested"), borderColor: "#94a3b8", borderDash: [2, 3], fill: false, tension: .25 },
      { label: "dead", data: series("pool_dead"), borderColor: "#f87171", borderDash: [2, 3], fill: false, tension: .25 },
    ]},
    options: { responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: "nearest" }, scales: { x: { grid: { display: false } } } },
  });
  poolChart.data.labels = labels;
  poolChart.data.datasets.forEach((ds, i) => { ds.data = [series(["pool_alive", "pool_in_use", "pool_untested", "pool_dead"][i])]; });
  poolChart.update("none");

  const tunnelChart = state.panels.tunnel || new Chart($("#tunnel-chart"), {
    type: "line",
    data: { labels, datasets: [
      { label: "active", data: series("tunnel_active"), borderColor: "#38bdf8", fill: { target: "origin" }, tension: .3 },
      { label: "degraded", data: series("tunnel_degraded"), borderColor: "#f87171", borderDash: [4, 3], fill: false, tension: .3 },
    ]},
    options: { responsive: true, maintainAspectRatio: false, animation: false, scales: { y: { beginAtZero: true, ticks: { precision: 0 } }, x: { grid: { display: false } } } },
  });
  tunnelChart.data.labels = labels;
  tunnelChart.data.datasets[0].data = series("tunnel_active");
  tunnelChart.data.datasets[1].data = series("tunnel_degraded");
  tunnelChart.update("none");

  state.panels.pool = poolChart;
  state.panels.tunnel = tunnelChart;
}

function drawProtocolChart(rows) {
  if (!window.Chart || !rows || !rows.length) return;
  const names = rows.map((r) => r.protocol);
  const chart = state.panels.proto || new Chart($("#proto-chart"), {
    type: "bar",
    data: {
      labels: names,
      datasets: [
        { label: "alive", data: rows.map((r) => r.alive), backgroundColor: "#34d399" },
        { label: "untested", data: rows.map((r) => r.untested), backgroundColor: "#94a3b8" },
        { label: "dead", data: rows.map((r) => r.dead), backgroundColor: "#f87171" },
      ],
    },
    options: { responsive: true, maintainAspectRatio: false, animation: false, scales: { x: { stacked: true }, y: { stacked: true } } },
  });
  chart.data.labels = names;
  chart.data.datasets.forEach((ds, i) => { ds.data = rows.map((r) => ({ alive: r.alive, untested: r.untested, dead: r.dead }[["alive", "untested", "dead"][i]])); });
  chart.update("none");
  state.panels.proto = chart;
}

/* ---------- tunnels view ---------- */

function stateBadge(s, degraded) {
  if (degraded) return `<span class="badge warn">degraded</span>`;
  return `<span class="badge ${s === "running" ? "ok" : s === "error" ? "dead" : "idle"}">${esc(s)}</span>`;
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
      <td>${stateBadge(t.state, t.degraded)}</td>
      <td class="muted">${esc(health.healthy ? "healthy" : "unhealthy")}${health.dead_swapped_24h ? ` · ${health.dead_swapped_24h} swap` : ""}</td>
      <td>
        <div class="actions">
          <button class="btn sm" data-act="copy" data-creds="${esc(creds)}" title="copy endpoint+credentials">Copy</button>
          <button class="btn sm" data-act="test" data-id="${esc(t.id)}">Test</button>
          <button class="btn sm" data-act="renew" data-id="${esc(t.id)}">Renew</button>
          <button class="btn sm danger" data-act="delete" data-id="${esc(t.id)}">Delete</button>
        </div>
      </td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">No tunnels. Create one above.</td></tr>`;
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
    <td>${n.in_use ? '<span class="badge warn">in use</span>' : '<span class="badge idle">free</span>'}</td>
  </tr>`.trim()).join("") || `<tr><td colspan="7" class="muted">No nodes match the filters.</td></tr>`;
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