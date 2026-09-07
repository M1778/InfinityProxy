#!/usr/bin/env python3
"""Production-grade benchmark for InfinityProxy.

Brings up the composed stack, requests a set of tunnels, then hammers them
with real HTTP + SOCKS5 traffic for a fixed window while sampling engine
status, per-node health, and correlated engine/sing-box logs. Writes a JSON
results bundle plus a human-readable report under --outdir. Output is the
raw material for later improvements: every slow or failed request is
correlated to the engine state (probe latency, health swaps, source feeds)
and the adjacent sing-box log lines at that instant.

Run from the repo root, e.g.:
    python3 tools/prod_bench.py --duration 600 --tunnels 3 --requests-per-min 6
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import quote_plus

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BENCHMARK_TARGET = "https://api.ipify.org"
CONTROL = "http://127.0.0.1:8000"

STATUS_SAMPLE_S = 15
MAP_SAMPLE_S = 10


def iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _percentile(sorted_values: list[int], pct: float) -> int:
    if not sorted_values:
        return 0
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * pct))]


class Bench:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.outdir = Path(args.outdir)
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.results: dict = {
            "started_at": iso(),
            "args": vars(args),
            "tunnels": [],
            "engine_samples": [],
            "per_request": [],
            "exit_ips": {},
            "probe_history": [],
            "singbox_log": {},
            "errors": [],
        }
        self.tunnel_ports_by_id: dict[str, int] = {}

    # -- engine API -----------------------------------------------------------
    def api(self, method: str, path: str, **kwargs):
        try:
            resp = requests.request(method, f"{CONTROL}{path}", timeout=10, **kwargs)
            return resp
        except requests.RequestException as exc:
            self.results["errors"].append({"at": iso(), "api": path, "error": str(exc)})
            return None

    def create(self, n: int) -> dict | None:
        resp = self.api("POST", "/tunnels", json={"node_count": n, "auto_renew": True})
        if resp is None or resp.status_code != 201:
            self.results["errors"].append(
                {
                    "at": iso(),
                    "op": "create_tunnel",
                    "status": resp.status_code if resp is not None else None,
                    "body": resp.text[:200] if resp is not None else None,
                }
            )
            return None
        return resp.json()

    def delete(self, tunnel_id: str) -> None:
        self.api("DELETE", f"/tunnels/{tunnel_id}")

    def status(self) -> dict:
        resp = self.api("GET", "/status")
        return resp.json() if resp is not None else {}

    def tunnels(self) -> list[dict]:
        resp = self.api("GET", "/tunnels")
        return resp.json()["tunnels"] if resp is not None else []

    def renew(self, tunnel_id: str) -> dict:
        resp = self.api("POST", f"/tunnels/{tunnel_id}/renew")
        return resp.json() if resp is not None else {}

    # -- traffic --------------------------------------------------------------
    def bench_request(self, tunnel: dict, mode: str) -> dict:
        port = tunnel["port"]
        user = tunnel["username"]
        password = tunnel["password"]
        auth = quote_plus(f"{user}:{password}")
        start = monotonic()

        proxy_url = (
            f"http://{auth}@127.0.0.1:{port}"
            if mode == "http"
            else f"socks5h://{auth}@127.0.0.1:{port}"
        )
        proxies = {"http": proxy_url, "https": proxy_url}
        try:
            resp = requests.get(
                BENCHMARK_TARGET, proxies=proxies, timeout=20, verify=False
            )
            latency_ms = round((monotonic() - start) * 1000)
            success = resp.ok
            exit_ip = resp.text.strip()
            http_status = resp.status_code
            error = None
        except requests.RequestException as exc:
            latency_ms = round((monotonic() - start) * 1000)
            success = False
            exit_ip = None
            http_status = None
            error = str(exc.__class__.__name__)
            if isinstance(exc, requests.Timeout):
                error = "timeout"

        rec = {
            "at": iso(),
            "tunnel_id": tunnel["id"],
            "port": port,
            "mode": mode,
            "success": success,
            "latency_ms": latency_ms,
            "status_code": http_status,
            "exit_ip": exit_ip,
            "error": error,
        }
        self.results["per_request"].append(rec)
        return rec

    # -- sampling -------------------------------------------------------------
    def sample_status(self) -> None:
        self.results["engine_samples"].append({"at": iso(), "status": self.status()})

    def sample_tunnels(self) -> None:
        for tunnel in self.tunnels():
            self.results["probe_history"].append(
                {
                    "at": iso(),
                    "tunnel_id": tunnel["id"],
                    "port": tunnel["port"],
                    "granted": tunnel["node_count_granted"],
                    "degraded": tunnel["degraded"],
                    "nodes": tunnel.get("nodes"),
                    "health": tunnel.get("health"),
                }
            )

    def check_exit(self, tunnel: dict, mode: str) -> dict:
        """Fetch a fresh IP and record it per tunnel+exit (kept for diagnostics)."""
        return self.bench_request(tunnel, mode)

    # -- logs ------------------------------------------------------------------
    def capture_logs(self) -> None:
        try:
            import docker
        except ImportError:
            self.results["errors"].append(
                {"at": iso(), "note": "docker SDK unavailable"}
            )
            return
        try:
            client = docker.from_env()
        except Exception as exc:  # noqa: BLE001
            self.results["errors"].append(
                {"at": iso(), "note": f"docker from_env: {exc}"}
            )
            return
        container_ids: dict[str, str] = {}
        try:
            for container in client.containers.list(all=True):
                name = (container.name or "").lower()
                if name.startswith("infinityproxy-engine"):
                    container_ids["engine"] = container.id
                elif name.startswith("infinity-tu_"):
                    container_ids[name] = container.id
        except Exception as exc:  # noqa: BLE001
            self.results["errors"].append(
                {"at": iso(), "note": f"list containers: {exc}"}
            )
            return
        for label, cid in container_ids.items():
            try:
                logs = (
                    client.containers.get(cid)
                    .logs(timestamps=True)
                    .decode("utf-8", errors="replace")
                )
                if label == "engine":
                    self.results["engine_log"] = logs
                else:
                    self.results["singbox_log"][label] = logs
            except Exception as exc:  # noqa: BLE001
                self.results["errors"].append(
                    {"at": iso(), "note": f"logs {label}: {exc}"}
                )

    # -- orchestration ---------------------------------------------------------
    def run(self) -> None:
        args = self.args
        created: list[dict] = []
        for _ in range(args.tunnels):
            tunnel = self.create(args.node_count)
            if tunnel is not None:
                created.append(tunnel)

        self.results["tunnels"] = created
        for t in created:
            self.results["exit_ips"][str(t["port"])] = {"http": None, "socks5": None}

        if not created:
            failures = sum(
                1 for e in self.results["errors"] if e.get("op") == "create_tunnel"
            )
            raise RuntimeError(
                f"no tunnels could be created; {failures} create attempts failed"
            )

        window = args.duration
        deadline = monotonic() + window
        count = 0
        next_status = 0.0
        next_probe = 0.0
        requests_left = args.requests_per_min * (window / 60.0)

        while monotonic() < deadline:
            now = monotonic()
            if now >= next_status:
                self.sample_status()
                next_status = now + STATUS_SAMPLE_S
            if now >= next_probe:
                self.sample_tunnels()
                next_probe = now + MAP_SAMPLE_S

            # rotate across modes and tunnels
            tunnel = created[count % len(created)]
            mode = "http" if (count % 2 == 0) else "socks5"
            rec = self.bench_request(tunnel, mode)
            count += 1
            requests_left -= 1

            # refresh an exit IP on first success of each tunnel/mode
            ip_key = str(tunnel["port"])
            current = self.results["exit_ips"][ip_key][mode]
            if rec["success"] and current is None:
                self.results["exit_ips"][ip_key][mode] = rec["exit_ip"]
            elif rec["success"] and rec["exit_ip"] and rec["exit_ip"] != current:
                self.results["exit_ips"][ip_key][mode] = f"{current}|{rec['exit_ip']}"

            # pace within the minute budget
            if requests_left <= 0 and monotonic() < deadline:
                break
            sleep(60.0 / max(args.requests_per_min, 1))

        self.capture_logs()

    # -- report ----------------------------------------------------------------
    def write_report(self) -> str:
        total = len(self.results["per_request"])
        ok = [r for r in self.results["per_request"] if r["success"]]
        lat = sorted([r["latency_ms"] for r in ok] or [0])

        p = lambda x: _percentile(lat, x)  # noqa: E731

        text = [
            "InfinityProxy production benchmark report",
            f"started: {self.results['started_at']}",
            f"target:  {BENCHMARK_TARGET}",
            "",
            f"tunnels:         {len(self.results['tunnels'])} "
            f"(requested {self.args.node_count} nodes each)",
            f"duration:        {self.args.duration}s",
            f"requests sent:   {total}",
            f"requests ok:     {len(ok)} "
            f"({100.0 * len(ok) / total if total else 0:.1f}%)",
            f"latency p50/p90/p95/max: {p(0.5):.0f}/{p(0.9):.0f}"
            f"/{p(0.95):.0f}/{lat[-1]:.0f} ms",
        ]

        # stability per tunnel/protocol
        by_key: dict[str, list[int]] = {}
        for r in self.results["per_request"]:
            key = f"{r['tunnel_id']}/{r['mode']}"
            by_key.setdefault(key, []).append(r["latency_ms"] if r["success"] else -1)
        text.append("")
        text.append("per tunnel/mode:")
        text.append("  tunnel       mode    ok/tot   p50ms  p95ms  maxms  errors")
        for key, vals in sorted(by_key.items()):
            oks = sorted([v for v in vals if v >= 0]) or [0]
            errs = vals.count(-1)
            text.append(
                f"  {key:<13} {len([v for v in vals if v >= 0])}/{len(vals):<4} "
                f"{_percentile(oks, 0.5):>6.0f} {_percentile(oks, 0.95):>6.0f} "
                f"{max(oks):>6.0f} {errs:>5}"
            )

        # degradation
        degraded = [r for r in self.results["probe_history"] if r["degraded"]]
        text.append("")
        text.append(
            f"degraded samples: {len(degraded)}/{len(self.results['probe_history'])}"
        )

        # exit IP sticking / rotation
        text.append("")
        text.append("exit IPs observed per tunnel (ip1|ip2 = rotated):")
        for port, m in self.results["exit_ips"].items():
            text.append(f"  port {port}: http={m['http']} socks5={m['socks5']}")

        # singbox errors
        err_lines = []
        for label, logs in self.results["singbox_log"].items():
            for line in logs.splitlines():
                if "ERROR" in line or "FATAL" in line:
                    err_lines.append(f"  [{label}] {line[:200]}")
        text.append("")
        text.append(f"sing-box ERROR/FATAL lines: {len(err_lines)}")
        text.extend(err_lines[:30])
        if len(err_lines) > 30:
            text.append(f"  ... and {len(err_lines) - 30} more")

        report = "\n".join(text)
        (self.outdir / "report.txt").write_text(report + "\n")
        (self.outdir / "results.json").write_text(json.dumps(self.results, indent=2))
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration", type=int, default=600, help="benchmark window seconds"
    )
    parser.add_argument(
        "--tunnels", type=int, default=3, help="number of tunnels to request"
    )
    parser.add_argument(
        "--node-count",
        type=int,
        default=10,
        help="nodes requested per tunnel",
    )
    parser.add_argument("--requests-per-min", type=int, default=6)
    parser.add_argument("--outdir", default="bench_out")
    args = parser.parse_args()
    bench = Bench(args)
    bench.run()
    report = bench.write_report()
    print(report)


if __name__ == "__main__":
    main()
