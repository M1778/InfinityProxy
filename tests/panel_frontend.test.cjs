"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "panel/static/app.js"), "utf8");

function harness() {
  const timers = new Map();
  const clicks = [];
  const attributes = {};
  const media = { matches: true, addEventListener() {} };
  const toggle = {
    addEventListener(event, listener) { if (event === "click") clicks.push(listener); },
    setAttribute(key, value) { attributes[key] = value; },
  };
  const document = {
    body: { dataset: {} },
    querySelector() { return toggle; },
  };
  let nextTimer = 1;
  let stream;
  const context = vm.createContext({
    console, document,
    window: {
      addEventListener() {},
      matchMedia() { return media; },
      setInterval(callback, delay) {
        assert.equal(delay, 5000);
        const id = nextTimer++;
        timers.set(id, callback);
        return id;
      },
      clearInterval(id) { timers.delete(id); },
    },
    EventSource: class {
      constructor() { this.events = {}; stream = this; }
      addEventListener(event, callback) { this.events[event] = callback; }
    },
  });
  vm.runInContext(source.replace(/\ninit\(\);\s*$/, ""), context);
  vm.runInContext(`
    setPill = () => {};
    renderOverview = renderTunnels = renderSources = renderFooter = () => {};
  `, context);
  return {
    context, timers, clicks, attributes, document,
    run(code) { return vm.runInContext(code, context); },
    get stream() { return stream; },
    tick() { return timers.values().next().value(); },
  };
}

const live = { reachable: true, status: {}, tunnels: [] };

test("fallback polls every five seconds despite its own snapshots", async () => {
  const h = harness();
  let calls = 0;
  h.context.answer = async () => { calls++; return live; };
  h.run("api = answer; connectSSE();");
  h.stream.onerror();
  h.stream.onerror();
  assert.equal(h.timers.size, 1);
  await h.tick();
  await h.tick();
  await h.tick();
  assert.equal(calls, 3);
  h.stream.onopen();
  assert.equal(h.timers.size, 0);
});

test("fallback does not overlap or overwrite a recovered stream", async () => {
  const h = harness();
  let resolve;
  let calls = 0;
  h.context.answer = () => { calls++; return new Promise(done => { resolve = done; }); };
  h.run("api = answer; connectSSE();");
  h.stream.onerror();
  const pending = h.tick();
  await h.tick();
  assert.equal(calls, 1);
  h.stream.events.snapshot({ data: JSON.stringify({ ...live, t: 20 }) });
  resolve({ ...live, t: 10 });
  await pending;
  assert.equal(h.run("state.snap.t"), 20);
  assert.equal(h.timers.size, 0);
  assert.equal(h.run("state.fallbackInFlight"), false);
});

test("failed HTTP polls release the in-flight guard", async () => {
  const h = harness();
  h.context.answer = async () => { throw new Error("offline"); };
  h.run("api = answer; scheduleFallbackPoll();");
  await h.tick();
  assert.equal(h.run("state.fallbackInFlight"), false);
  assert.equal(h.timers.size, 1);
});

test("one pill click changes mode once and snapshots preserve manual expansion", () => {
  const h = harness();
  h.run("connectSSE();");
  assert.equal(h.clicks.length, 1);
  h.run(`onSnap(${JSON.stringify(live)});`);
  h.clicks[0]();
  assert.equal(h.document.body.dataset.pillMode, "expanded");
  assert.equal(h.attributes["aria-expanded"], "true");
  h.run(`onSnap(${JSON.stringify(live)});`);
  assert.equal(h.document.body.dataset.pillMode, "expanded");
  h.clicks[0]();
  assert.equal(h.document.body.dataset.pillMode, "collapsed");
  assert.equal(h.attributes["aria-expanded"], "false");
});

test("demo boot does not register its own pill click listener", () => {
  const demo = fs.readFileSync(path.join(root, "demo/demo-bootstrap.js"), "utf8");
  const start = demo.indexOf("function applyPillModeBoot()");
  const end = demo.indexOf("window.__demoPillBoot", start);
  assert.equal(demo.slice(start, end).includes('addEventListener("click"'), false);
});
