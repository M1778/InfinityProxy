/* InfinityProxy demo bootstrap — drives the static panel build on GitHub Pages.
   Injected into _site/index.html by demo/build_demo.py (pages.yml) so the UI
   runs against fake data with simulated SSE. Nobody talks to a real engine:
   every /api/* and EventSource is stubbed here. Live installs never load it. */
"use strict";

(function () {
  const SOURCES = [
    { name: "ebrasha", last_fetch_s: 12, next_fetch_s: 888 },
    { name: "epodonios", last_fetch_s: 47, next_fetch_s: 253 },
    { name: "v2rayfree", last_fetch_s: 3600, next_fetch_s: 18000 },
    { name: "gfpcom", last_fetch_s: 900, next_fetch_s: 900 },
    { name: "freefolkson", last_fetch_s: 220, next_fetch_s: 380 },
  ];

  const PROTOCOLS = [
    ["vless", 234, 1421, 87],
    ["vmess", 121, 954, 63],
    ["ss", 96, 512, 41],
    ["trojan", 44, 210, 19],
    ["http", 31, 0, 12],
    ["socks5", 22, 0, 9],
  ];

  let tunnels = [
    mkTunnel("tnl-alpha", 10244, 10, "running", false, 8),
    mkTunnel("tnl-beta", 10245, 8, "running", false, 8),
    mkTunnel("tnl-gamma", 10246, 6, "running", true, 6),
    mkTunnel("tnl-delta", 10247, 5, "running", false, 5),
  ];
  let nextPort = 10248;
  let seq = 1;

  const host = {
    available: true,
    uptime_s: 4012,
    proxyOn: false,
    proxyEndpoint: null,
    proxyTunnel: null,
    tunOn: false,
    pick: null,
    lastLatency: {},
  };

  function mkTunnel(id, port, granted, state, degraded, nodes) {
    return {
      id,
      host: "127.0.0.1",
      port,
      username: "user-" + id,
      password: "pass-" + port,
      node_count_requested: 10,
      node_count_granted: granted,
      auto_renew: true,
      state,
      degraded,
      nodes: (nodes || 0) > 0
        ? Array.from({ length: nodes }, (_, i) => ({
            id: "node" + i + "-" + port,
            tier: i % 3 === 0 ? "A" : i % 3 === 1 ? "B" : "C",
          }))
        : [],
      health: {
        healthy: state === "running" && !degraded,
        dead_swapped_24h: id === "tnl-gamma" ? 2 : 0,
        restarts_24h: id === "tnl-delta" ? 1 : 0,
      },
      created_at: new Date(Date.now() - 3600e3 * 3).toISOString(),
    };
  }

  function pool() {
    let alive = 0, untested = 0, dead = 0;
    for (const [, a, u, d] of PROTOCOLS) { alive += a; untested += u; dead += d; }
    const total = alive + untested + dead;
    const in_use = tunnels.reduce((s, t) => s + (t.node_count_granted || 0), 0);
    return {
      total, alive, dead, untested, in_use,
      assignable: Math.max(0, alive - in_use),
      tier_a: 96, tier_b: 238, working_set: 256,
      avg_score: 0.71,
      by_protocol: PROTOCOLS.map(([protocol, a, u, d]) => ({
        protocol, alive: a, untested: u, dead: d, total: a + u + d,
      })),
    };
  }

  function status() {
    const p = pool();
    return {
      version: "0.1.0-demo",
      pool: p,
      tunnels: {
        active: tunnels.filter((t) => t.state === "running").length,
        degraded: tunnels.filter((t) => t.degraded).length,
      },
      sources: SOURCES.map((s) => ({
        name: s.name,
        last_fetch_s: s.last_fetch_s,
        next_fetch_s: Math.max(0, s.next_fetch_s - (s.next_fetch_s > 800 ? 2 : 1)),
      })),
    };
  }

  function snapshot() {
    return {
      t: Date.now() / 1000,
      reachable: true,
      error: null,
      status: status(),
      tunnels,
      config: {
        engine_url: "https://github.com/M1778/InfinityProxy",
        panel_host: "github.io",
        panel_port: "demo",
        test_url: "https://api.ipify.org",
      },
    };
  }

  function buildHistory() {
    const LEN = 120;
    const t = [];
    const cols = {
      pool_total: [], pool_alive: [], pool_dead: [], pool_untested: [],
      pool_in_use: [], pool_assignable: [], pool_tier_a: [], pool_tier_b: [],
      pool_working_set: [], pool_avg_score: [],
      tunnel_active: [], tunnel_degraded: [],
    };
    const now = Date.now() / 1000;
    const p = pool();
    const end = { alive: p.alive, dead: p.dead, untested: p.untested, in_use: p.in_use };
    for (let i = 0; i < LEN; i++) {
      const f = (i / (LEN - 1));
      const jitter = () => 1 + 0.02 * Math.sin(i * 0.4 + 1.7);
      t.push(now - (LEN - 1 - i) * 60);
      cols.pool_dead.push(Math.round(end.dead * f * jitter()));
      cols.pool_untested.push(Math.round((end.untested * (1 - f * 0.6)) * jitter()));
      cols.pool_alive.push(Math.round(end.alive * f * jitter()));
      cols.pool_in_use.push(Math.round(end.in_use * Math.min(1, f * 1.4)));
      cols.pool_assignable.push(Math.max(0, cols.pool_alive[i] - cols.pool_in_use[i]));
      cols.pool_tier_a.push(Math.round(96 * f));
      cols.pool_tier_b.push(Math.round(238 * Math.min(1, f * 1.15)));
      cols.pool_working_set.push(256);
      cols.pool_avg_score.push(0.55 + 0.16 * f);
      cols.tunnel_active.push(Math.round((tunnels.length) * Math.min(1, f * 1.3)));
      cols.tunnel_degraded.push(tunnels.filter((t) => t.degraded).length);
    }
    cols.pool_total = t.map((_, i) =>
      cols.pool_alive[i] + cols.pool_untested[i] + cols.pool_dead[i]);
    return { t, ...cols };
  }

  function hostPayload() {
    const running = tunnels.filter((t) => t.state === "running" && t.port);
    return {
      hostagent: { available: host.available, uptime_s: host.uptime_s },
      platform: { system: "linux", machine: "x86_64", docker: true, tun: true },
      capabilities: { proxy: "gsettings", tun: true, docker: true },
      proxy: {
        enabled: host.proxyOn,
        endpoint: host.proxyEndpoint,
        mode: host.proxyOn ? "manual" : "none",
        tunnel: host.proxyTunnel,
      },
      tun: {
        enabled: host.tunOn,
        iface: "tun0",
        endpoint: host.tunOn ? "172.19.0.2" : null,
        tunnel: host.tunOn ? "tnl-alpha" : null,
        running: host.tunOn,
      },
      tunnels: running.map((t) => {
        const m = host.lastLatency[t.id] || {};
        return {
          tunnel: t.id,
          host: "127.0.0.1",
          port: t.port,
          username: t.username,
          latency_ms: m.latency_ms != null ? m.latency_ms : null,
          throughput_kb_s: m.throughput_kb_s != null ? m.throughput_kb_s : null,
          measured_at_s: m.at_s ?? null,
        };
      }),
      pick: host.pick,
    };
  }

  function runPick(tunnelId) {
    const running = tunnels.filter((t) => t.state === "running" && t.port);
    const measurements = running.map((t, i) => ({
      tunnel: t.id,
      port: t.port,
      username: t.username,
      latency_ms: Math.round(38 + (i * 37) + (t.id.charCodeAt(4) % 11)),
      throughput_kb_s: Math.round(420 + (i * 140) + ((t.port % 97) * 7)),
    }));
    host.lastLatency = {};
    measurements.forEach((m) => {
      host.lastLatency[m.tunnel] = { latency_ms: m.latency_ms, throughput_kb_s: m.throughput_kb_s, at_s: Date.now() / 1000 };
    });
    const target = tunnelId && measurements.some((m) => m.tunnel === tunnelId)
      ? tunnelId
      : measurements.slice().sort((a, b) => a.latency_ms - b.latency_ms)[0].tunnel;
    const pick = {
      tunnel: target,
      cached: false,
      stale_in_s: 60,
      measurements,
    };
    host.pick = pick;
    return pick;
  }

  const routes = {
    "/api/snapshot": () => new Ok(snapshot()),
    "/api/history": () => new Ok(buildHistory()),
    "/api/events": () => null, // handled below by fake EventSource
    "/api/tunnels": {
      GET: () => new Ok({ tunnels }),
      POST: (body) => {
        const count = Math.max(1, Math.min(200, parseInt(body.node_count, 10) || 10));
        const id = "tnl-" + String.fromCharCode(97 + (tunnels.length % 26)) + Math.floor(Math.random() * 90 + 10);
        const t = mkTunnel(id, nextPort, count, "running", false, count);
        nextPort += 1;
        tunnels = [...tunnels, t];
        runPick(null);
        return new Ok(t, 201);
      },
    },
    "/api/nodes": () => new Ok(nodesPage()),
    "/api/host": {
      GET: () => new Ok(hostPayload()),
    },
  };

  function nodeRow(i, protocol, server, port, source, state, lat, thr, tier, inUse) {
    return {
      id: "#".repeat(16).replace(/#/g, (c, k) => "0123456789abcdef"[(i * 7 + k * 3) % 16]),
      protocol, server, port, source, state,
      last_latency_ms: lat, throughput_kb_s: thr,
      in_use: inUse,
      first_seen_s: 1700000000 - i * 3600,
      probe_total: 6 + (i % 40),
      availability: state === "alive" ? 0.7 + (i % 20) / 100 : 0,
      score: state === "alive" ? 0.5 + (i % 40) / 100 : null,
      tier: state === "alive" ? tier : null,
    };
  }

  function nodesPage() {
    const servers = [
      ["vless", "nl1.nodefarm.example", 443, "ebrasha", "alive", 34, 812, "A"],
      ["vmess", "de3.freecdn.example", 443, "epodonios", "alive", 51, 604, "A"],
      ["ss", "sg1.edge.relay", 8388, "v2rayfree", "alive", 88, 340, "B"],
      ["trojan", "us2.direct.example", 443, "gfpcom", "alive", 112, 221, "B"],
      ["vless", "jp1.anycast.dev", 8443, "epodonios", "untested", null, null, null],
      ["ss", "fr1.box.host", 443, "freefolkson", "dead", 4000, null, null],
      ["vmess", "ca4.edge2.example", 443, "ebrasha", "alive", 73, 518, "B"],
      ["ss", "de6.fast.node", 8388, "v2rayfree", "dead", 910, null, null],
      ["http", "static.gfp.http", 8080, "gfpcom", "alive", 210, 180, "C"],
      ["socks5", "socks.gfp.example", 1080, "gfpcom", "alive", 240, 150, "C"],
      ["trojan", "nl4.tj.host", 443, "epodonios", "untested", null, null, null],
    ];
    const nodes = servers.map((s, i) => nodeRow(i, ...s, i < 2));
    return { nodes, count: nodes.length, limit: 200 };
  }

  /* ---------- response plumbing ---------- */

  class Ok {
    constructor(payload, status) {
      this.ok = true;
      this.status = status || 200;
      this._payload = payload;
    }
    async json() { return this._payload; }
  }

  class Err {
    constructor(code, message, status) {
      this.ok = false;
      this.status = status || 502;
      this._payload = { error: { code, message } };
    }
    async json() { return this._payload; }
  }

  const originalFetch = window.fetch;
  window.fetch = async (url, opts = {}) => {
    const path = (url instanceof Request ? url.url : String(url)).split("?")[0];
    const method = opts.method || "GET";

    if (path.startsWith("/api/tunnels/")) {
      const m = path.match(/^\/api\/tunnels\/([^/]+)\/(renew|test|delete)$/);
      if (m) {
        const id = decodeURIComponent(m[1]);
        const action = m[2];
        const t = tunnels.find((x) => x.id === id);
        if (!t) return new Err("not_found", `no tunnel ${id}`, 404);
        if (action === "renew") return new Ok({ id, renewed: true });
        if (action === "test") return new Ok({ ok: true, exit_ip: "203.0.113.7", latency_ms: 41, http_status: 200 });
        tunnels = tunnels.filter((x) => x.id !== id);
        if (host.proxyTunnel === id) host.proxyOn = false;
        return new Ok({ ok: true, id });
      }
      const mm = path.match(/^\/api\/tunnels\/([^/]+)$/);
      if (mm && method === "DELETE") {
        const id = decodeURIComponent(mm[1]);
        tunnels = tunnels.filter((x) => x.id !== id);
        return new Ok({ ok: true, id });
      }
    }

    if (path.startsWith("/api/nodes/")) return new Ok(nodeById(path));
    if (path === "/api/nodes") return new Ok(nodesPage());
    if (path.startsWith("/api/sources/") && method === "POST") {
      const name = decodeURIComponent(path.split("/").pop());
      const s = SOURCES.find((x) => x.name === name);
      return s ? new Ok({ name, refreshing: true }, 202) : new Err("not_found", `no source ${name}`, 404);
    }

    if (path === "/api/host/pick") return new Ok(runPick(opts.body ? JSON.parse(opts.body).tunnel : null));
    if (path === "/api/host/proxy") return setHostProxy(JSON.parse(opts.body || "{}"));
    if (path === "/api/host/tun") return setHostTun(JSON.parse(opts.body || "{}"));
    if (path === "/api/host") return new Ok(hostPayload());

    if (path in routes) {
      const handler = routes[path];
      if (typeof handler === "function") return handler();
      if (handler[method]) return handler[method](opts.body ? JSON.parse(opts.body) : {});
    }
    return new Err("not_found", `demo has no route ${path}`, 404);
  };

  function setHostProxy(body) {
    const enabled = Boolean(body.enabled);
    if (enabled) {
      const target = resolveTarget(body.tunnel);
      if (!target) return new Err("host_target_unavailable", "no usable tunnel for the host target", 409);
      const pick = runPick(target.id);
      host.proxyOn = true;
      host.proxyEndpoint = `127.0.0.1:${target.port}`;
      host.proxyTunnel = target.id;
      host.pick = pick;
    } else {
      host.proxyOn = false;
      host.proxyEndpoint = null;
      host.proxyTunnel = null;
    }
    return new Ok({ proxy: hostPayload().proxy });
  }

  function setHostTun(body) {
    const enabled = Boolean(body.enabled);
    if (enabled) {
      const target = resolveTarget(body.tunnel);
      if (!target) return new Err("host_target_unavailable", "no usable tunnel for the host target", 409);
      runPick(target.id);
      host.tunOn = true;
    } else {
      host.tunOn = false;
    }
    return new Ok({ tun: hostPayload().tun });
  }

  function resolveTarget(tunnel) {
    const running = tunnels.filter((t) => t.state === "running" && t.port);
    const id = !tunnel || tunnel === "auto" ? runPick(null).tunnel : tunnel;
    return running.find((t) => t.id === id) || null;
  }

  function nodeById(path) {
    const id = decodeURIComponent(path.split("/").pop());
    const n = nodesPage().nodes.find((x) => x.id === id);
    return n ? new Ok({ ...n, uri: `vless://demo@example.test:443?security=reality#demo-${id}` }) : new Err("not_found", `no node ${id}`, 404);
  }

  /* ---------- fake EventSource ---------- */

  class FakeEventSource {
    constructor() {
      this.listeners = {};
      window.setTimeout(() => {
        if (this.onopen) this.onopen();
        this.dispatch("snapshot", snapshot());
      }, 60);
      if (!window.__demoSseTimer) {
        window.__demoSseTimer = window.setInterval(() => {
          seq += 1;
          this.dispatch("snapshot", snapshot());
        }, 4000);
      }
    }
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    }
    dispatch(type, data) {
      const ev = new Event(type);
      ev.data = JSON.stringify(data);
      (this.listeners[type] || []).forEach((fn) => fn(ev));
    }
    close() { /* keep the shared timer; the demo never reconnects */ }
  }

  window.EventSource = FakeEventSource;
})();