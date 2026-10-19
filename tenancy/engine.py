"""Engine client for the control center: authenticated tunnel CRUD on :8787.

The forwarding surface stays narrow on purpose — a tenant reaches only
create/list/renew/delete/proxy-check of its own tunnels and only the public
read fields of the pool. Everything board-wide (sources, host control, host
agent) is admin-only machinery and never proxied for tenants.
"""

from __future__ import annotations

from typing import Any

import requests


class EngineSideError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class EngineEdge:
    """Calls the engine Control API as the board's single local principal."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _call(
        self, method: str, path: str, timeout: float | None = None, **kw: Any
    ) -> Any:
        try:
            res = requests.request(
                method,
                f"{self.base_url}{path}",
                timeout=timeout or self.timeout,
                **kw,
            )
        except requests.RequestException as exc:
            raise EngineSideError(0, "engine_unreachable", str(exc)) from exc
        if res.status_code == 204:
            return None
        if not 200 <= res.status_code < 300:
            payload: Any = {}
            try:
                payload = res.json()
            except ValueError:
                payload = {}
            err = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = err.get("code") or "engine_error"
            message = err.get("message") or res.reason
            raise EngineSideError(res.status_code, code, message)
        return res.json()

    def status(self) -> Any:
        return self._call("GET", "/status")

    def tunnels(self) -> Any:
        return self._call("GET", "/tunnels")

    def create_tunnel(self, node_count: int, auto_renew: bool = True) -> Any:
        return self._call(
            "POST",
            "/tunnels",
            json={"node_count": node_count, "auto_renew": auto_renew},
        )

    def renew_tunnel(self, tunnel_id: str) -> Any:
        return self._call(
            "POST", f"/tunnels/{tunnel_id}/renew", timeout=self.timeout * 12
        )

    def delete_tunnel(self, tunnel_id: str) -> None:
        self._call("DELETE", f"/tunnels/{tunnel_id}")

    def nodes(self, params: dict[str, str] | None = None) -> Any:
        return self._call("GET", "/nodes", params=params or {})


def shaped_tunnels(payload: Any, scope: set[str]) -> list[dict[str, str]]:
    """Project every engine tunnel row into the tenant's public shape.

    Only `id`, `port`, `host`, `state` survive: credentials and node lists are
    operator fields, never a tenant's. Tunnels outside the scope set are
    dropped, so one tenant's listing never leaks another's rows.
    """
    rows: list[dict[str, Any]] = list(payload.get("tunnels", []))
    shaped = []
    for row in rows:
        if row.get("id") not in scope:
            continue
        shaped.append(
            {
                "id": row["id"],
                "host": row.get("host") or "",
                "port": str(row.get("port") or ""),
                "state": row.get("state") or "",
            }
        )
    return shaped


def read_pool(payload: Any) -> dict[str, Any]:
    """Project the public pool snapshot — the same fields the panel charts."""
    pool = payload.get("pool", {})
    if not isinstance(pool, dict):
        pool = {}
    keys = (
        "total",
        "alive",
        "dead",
        "untested",
        "in_use",
        "assignable",
        "tier_a",
        "tier_b",
        "working_set",
        "avg_score",
    )
    return {k: pool.get(k) for k in keys}
