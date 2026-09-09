from __future__ import annotations

from typing import Any

import requests


class EngineError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class EngineClient:
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
            raise EngineError(0, "engine_unreachable", str(exc)) from exc
        if res.status_code < 200 or res.status_code >= 300:
            payload = res.json() if res.content else {}
            err = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = err.get("code") or "engine_error"
            message = err.get("message") or res.reason
            raise EngineError(res.status_code, code, message)
        if res.status_code == 204:
            return None
        return res.json()

    def status(self) -> dict[str, Any]:
        return self._call("GET", "/status")

    def tunnels(self) -> dict[str, Any]:
        return self._call("GET", "/tunnels")

    def create_tunnel(
        self, node_count: int, auto_renew: bool = False
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            "/tunnels",
            json={"node_count": node_count, "auto_renew": auto_renew},
        )

    def renew_tunnel(self, tunnel_id: str) -> dict[str, Any]:
        return self._call("POST", f"/tunnels/{tunnel_id}/renew")

    def delete_tunnel(self, tunnel_id: str) -> None:
        self._call("DELETE", f"/tunnels/{tunnel_id}")

    def nodes(self, params: dict[str, str] | None = None) -> dict[str, Any]:
        return self._call("GET", "/nodes", params=params or {})

    def node(self, node_id: str) -> dict[str, Any]:
        return self._call("GET", f"/nodes/{node_id}")

    def refresh_source(self, source_name: str) -> dict[str, Any]:
        """Manual scrape can run a full fetch + probe pass; allow more time."""
        return self._call(
            "POST", f"/sources/{source_name}/refresh", timeout=self.timeout * 12
        )

    def host(self) -> dict[str, Any]:
        return self._call("GET", "/host")

    def host_pick(self) -> dict[str, Any]:
        return self._call("POST", "/host/pick")

    def host_set_proxy(
        self, enabled: bool, tunnel: str | None = None
    ) -> dict[str, Any]:
        return self._call(
            "POST", "/host/proxy", json={"enabled": enabled, "tunnel": tunnel}
        )

    def host_set_tun(self, enabled: bool, tunnel: str | None = None) -> dict[str, Any]:
        return self._call(
            "POST", "/host/tun", json={"enabled": enabled, "tunnel": tunnel}
        )
