"""Host-wide client control via the privileged hostagent (ADR-0010).

The engine never touches the desktop's proxy settings or network namespace
itself: the dedicated hostagent container does, and this controller talks to
its loopback control API. Auto-pick mirrors ADR-0005's spirit for the host
target: measure every running tunnel's egress and choose the best.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from engine.config import Settings
from engine.models import Tunnel

_AGENT_TIMEOUT_S = 5.0


class HostAgentError(RuntimeError):
    """The hostagent answered non-2xx, or was unreachable for a mutation."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class HostController:
    """Front controller for host features, measured and cached per call."""

    def __init__(
        self,
        settings: Settings,
        session: Any | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._session = session if session is not None else requests.Session()
        self._measure: dict[str, dict[str, Any]] = {}
        self._pick: dict[str, Any] | None = None
        self._pick_at = 0.0
        self._pick_signature: tuple | None = None
        self._lock = threading.Lock()

    def _agent(self, path: str, method: str = "GET", body: dict | None = None):
        url = self._settings.hostagent_url.rstrip("/") + path
        try:
            if method == "POST":
                response = self._session.post(url, json=body, timeout=_AGENT_TIMEOUT_S)
            else:
                response = self._session.get(url, timeout=_AGENT_TIMEOUT_S)
        except requests.RequestException as exc:
            raise HostAgentError(502, "hostagent_unreachable", str(exc)) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise HostAgentError(
                502,
                "host_unsupported",
                "the hostagent returned a non-JSON response",
            ) from exc
        if response.status_code >= 400:
            error = (payload or {}).get("error", {})
            raise HostAgentError(
                response.status_code,
                error.get("code", "host_unsupported"),
                error.get("message", "the hostagent rejected the request"),
            )
        return payload

    def host_state(self) -> dict | None:
        """The hostagent's current state, or None when it is unreachable."""
        try:
            return self._agent("/host")
        except HostAgentError:
            return None

    def measure(self, tunnel: Tunnel) -> dict[str, Any] | None:
        """Egress latency + throughput through a running tunnel's mixed proxy."""
        if not tunnel.port:
            return None
        netloc = f"127.0.0.1:{tunnel.port}"
        user = requests.utils.quote(tunnel.username, safe="")
        password = requests.utils.quote(tunnel.password, safe="")
        proxy_url = f"http://{user}:{password}@{netloc}"
        proxies = {"http": proxy_url, "https": proxy_url}
        target = self._settings.host_pick_test_url
        timeout = self._settings.host_pick_timeout_s
        target_bytes = self._settings.host_pick_sample_bytes
        started = self._clock()
        first_byte = None
        downloaded = 0
        try:
            with self._session.get(
                target, proxies=proxies, stream=True, timeout=timeout
            ) as response:
                if response.status_code >= 400:
                    return None
                for chunk in response.iter_content(chunk_size=65536):
                    if chunk:
                        if first_byte is None:
                            first_byte = self._clock()
                        downloaded += len(chunk)
                        if downloaded >= target_bytes:
                            break
        except requests.RequestException:
            return None
        if first_byte is None or downloaded == 0:
            return None
        latency_ms = (first_byte - started) * 1000.0
        download_s = self._clock() - first_byte
        throughput_kb_s = (downloaded / 1024.0) / download_s if download_s > 0 else 0.0
        return {
            "latency_ms": round(latency_ms, 1),
            "throughput_kb_s": round(throughput_kb_s, 1),
        }

    def _tunnels(self, tunnels: list[Tunnel]) -> list[Tunnel]:
        return [t for t in tunnels if t.state != "stopped" and t.port]

    def _signature(self, tunnels: list[Tunnel]) -> tuple:
        return tuple(
            sorted((t.tunnel_id, t.port, t.username) for t in self._tunnels(tunnels))
        )

    def _candidates(self, tunnels: list[Tunnel]) -> list[dict[str, Any]]:
        candidates = []
        for t in self._tunnels(tunnels):
            cached = self._measure.get(t.tunnel_id)
            candidates.append(
                {
                    "tunnel": t.tunnel_id,
                    "host": "127.0.0.1",
                    "port": t.port,
                    "username": t.username,
                    "latency_ms": cached["latency_ms"] if cached else None,
                    "throughput_kb_s": cached["throughput_kb_s"] if cached else None,
                    "measured_at_s": cached["at_s"] if cached else None,
                }
            )
        return candidates

    def pick_best(self, tunnels: list[Tunnel], force: bool = False) -> dict[str, Any]:
        """Measure every running tunnel and rank by latency, then throughput."""
        tuns = self._tunnels(tunnels)
        if not tuns:
            raise HostAgentError(
                409, "host_target_unavailable", "no running tunnels to measure"
            )
        signature = self._signature(tuns)
        now = self._clock()
        with self._lock:
            if (
                not force
                and self._pick is not None
                and signature == self._pick_signature
                and now - self._pick_at < self._settings.host_pick_ttl_s
            ):
                return {
                    "tunnel": self._pick["tunnel"],
                    "cached": True,
                    "stale_in_s": int(
                        self._settings.host_pick_ttl_s - (now - self._pick_at)
                    ),
                    "measurements": self._pick["measurements"],
                }
        measurements = []
        for t in tuns:
            cached = self._measure.get(t.tunnel_id)
            if (
                force
                or cached is None
                or now - cached["at_s"] >= self._settings.host_pick_ttl_s
            ):
                fresh = self.measure(t)
                if fresh is None:
                    self._measure.pop(t.tunnel_id, None)
                    continue
                fresh["at_s"] = self._clock()
                self._measure[t.tunnel_id] = fresh
                cached = fresh
            measurements.append(
                {
                    "tunnel": t.tunnel_id,
                    "port": t.port,
                    "username": t.username,
                    "latency_ms": cached["latency_ms"],
                    "throughput_kb_s": cached["throughput_kb_s"],
                }
            )
        if not measurements:
            raise HostAgentError(
                409,
                "host_target_unavailable",
                "tunnels are up but none are measurable",
            )
        measurements.sort(key=lambda m: (m["latency_ms"], -m["throughput_kb_s"]))
        with self._lock:
            self._pick = {
                "tunnel": measurements[0]["tunnel"],
                "measurements": measurements,
            }
            self._pick_at = self._clock()
            self._pick_signature = signature
        return {
            "tunnel": measurements[0]["tunnel"],
            "cached": False,
            "stale_in_s": self._settings.host_pick_ttl_s,
            "measurements": measurements,
        }

    def current_pick(self, tunnels: list[Tunnel]) -> dict[str, Any] | None:
        """The cached pick, if the candidate set and TTL still hold. Never measures."""
        tuns = self._tunnels(tunnels)
        if not tuns or self._pick is None:
            return None
        if self._signature(tuns) != self._pick_signature:
            return None
        now = self._clock()
        if now - self._pick_at >= self._settings.host_pick_ttl_s:
            return None
        return {
            "tunnel": self._pick["tunnel"],
            "cached": True,
            "stale_in_s": int(self._settings.host_pick_ttl_s - (now - self._pick_at)),
            "measurements": self._pick["measurements"],
        }

    def _endpoint_tunnel(self, state: dict | None, tunnels: list[Tunnel]) -> str | None:
        if not state or not state.get("enabled"):
            return None
        port = self._endpoint_port(state.get("endpoint"))
        for t in self._tunnels(tunnels):
            if t.port == port:
                return t.tunnel_id
        return None

    @staticmethod
    def _endpoint_port(endpoint: Any) -> int | None:
        if not endpoint:
            return None
        try:
            return int(str(endpoint).rsplit(":", 1)[-1])
        except (TypeError, ValueError, IndexError):
            return None

    def status(self, tunnels: list[Tunnel]) -> dict[str, Any]:
        """Combine hostagent state with the engine's tunnel view. Never measures."""
        host_state = self.host_state()
        if host_state is None:
            return {
                "hostagent": {
                    "available": False,
                    "error": f"hostagent unreachable at {self._settings.hostagent_url}",
                },
                "proxy": None,
                "tun": None,
                "tunnels": self._candidates(tunnels),
                "pick": self.current_pick(tunnels),
            }
        proxy_target = self._endpoint_tunnel(host_state.get("proxy"), tunnels)
        tun_target = self._endpoint_tunnel(host_state.get("tun"), tunnels)
        return {
            "hostagent": host_state.get("hostagent"),
            "proxy": {**host_state.get("proxy", {}), "tunnel": proxy_target},
            "tun": {**host_state.get("tun", {}), "tunnel": tun_target},
            "tunnels": self._candidates(tunnels),
            "pick": self.current_pick(tunnels),
        }

    def set_proxy(self, enabled: bool, tunnel: Tunnel | None) -> dict[str, Any]:
        body = {"enabled": bool(enabled)}
        if enabled:
            if tunnel is None or not tunnel.port:
                raise HostAgentError(
                    409, "host_target_unavailable", "choose a running tunnel or 'auto'"
                )
            body["endpoint"] = f"127.0.0.1:{tunnel.port}"
        result = self._agent("/host/proxy", method="POST", body=body)
        return {
            "proxy": result.get("proxy"),
            "tunnel": tunnel.tunnel_id if (enabled and tunnel) else None,
        }

    def set_tun(self, enabled: bool, tunnel: Tunnel | None) -> dict[str, Any]:
        body = {"enabled": bool(enabled)}
        if enabled:
            if tunnel is None or not tunnel.port:
                raise HostAgentError(
                    409, "host_target_unavailable", "choose a running tunnel or 'auto'"
                )
            body.update(
                {
                    "endpoint": f"127.0.0.1:{tunnel.port}",
                    "username": tunnel.username,
                    "password": tunnel.password,
                }
            )
        result = self._agent("/host/tun", method="POST", body=body)
        return {
            "tun": result.get("tun"),
            "tunnel": tunnel.tunnel_id if (enabled and tunnel) else None,
        }
