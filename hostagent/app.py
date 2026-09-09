"""Loopback control API for the privileged hostagent (ADR-0010, docs/api.md#host)."""

from __future__ import annotations

import threading
import time
from typing import Any

from flask import Flask, jsonify, request

from hostagent.config import HostAgentSettings
from hostagent.docker_ctl import TunController, TunRuntimeError
from hostagent.platform import (
    PROXY_CAPABILITY_NONE,
    _detect_proxy_capability,
    _docker_socket_present,
    _system_proxy_set,
    _tun_present,
)
from hostagent.platform import (
    _machine as _platform_machine,
)
from hostagent.platform import (
    _system as _platform_system,
)
from hostagent.singbox import render_tun_config


def create_app(
    settings: HostAgentSettings | None = None,
    *,
    clock: Any = time.time,
    tun_controller: TunController | None = None,
    proxy_setter: Any = _system_proxy_set,
    platform_system: Any = _platform_system,
    platform_machine: Any = _platform_machine,
    docker_socket_present: Any = _docker_socket_present,
    tun_present: Any = _tun_present,
    proxy_capability: Any = _detect_proxy_capability,
) -> Flask:
    settings = settings or HostAgentSettings.from_env()
    started = clock()
    controller = tun_controller or TunController(settings)
    lock = threading.Lock()
    proxy_state = {"enabled": False, "endpoint": None, "mode": "none"}
    tun_state = {
        "enabled": False,
        "iface": settings.tun_interface,
        "endpoint": None,
    }

    app = Flask("hostagent")
    app.json.ensure_ascii = False

    def err(code: str, message: str, status: int):
        return jsonify({"error": {"code": code, "message": message}}), status

    def capabilities() -> dict[str, Any]:
        return {
            "proxy": proxy_capability(),
            "tun": tun_present(settings.tun_device),
            "docker": docker_socket_present(),
        }

    def snapshot() -> dict[str, Any]:
        return {
            "hostagent": {
                "available": True,
                "uptime_s": max(0, int(clock() - started)),
            },
            "platform": {
                "system": platform_system(),
                "machine": platform_machine(),
                "docker": docker_socket_present(),
                "tun": tun_present(settings.tun_device),
            },
            "capabilities": capabilities(),
            "proxy": {**proxy_state},
            "tun": {**tun_state, "running": controller.is_running()},
        }

    @app.get("/host")
    def host_status():
        return jsonify(snapshot())

    @app.post("/host/proxy")
    def host_proxy():
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled"))
        endpoint = body.get("endpoint")
        if enabled and not endpoint:
            return err("invalid_request", "endpoint is required when enabling", 400)
        capability = proxy_capability()
        if enabled and capability == PROXY_CAPABILITY_NONE:
            return err(
                "host_unsupported",
                "no desktop proxy setter is available on this session",
                409,
            )
        try:
            applied = proxy_setter(capability, enabled, endpoint)
        except ValueError as exc:
            return err("host_unsupported", str(exc), 409)
        except Exception as exc:  # noqa: BLE001 - subprocess/session tooling failure
            return err("host_runtime", f"failed to apply the system proxy: {exc}", 502)
        with lock:
            proxy_state["enabled"] = enabled
            proxy_state["endpoint"] = endpoint if enabled else None
            proxy_state["mode"] = applied.get("mode", "manual" if enabled else "none")
        return jsonify({"proxy": {**proxy_state}})

    @app.post("/host/tun")
    def host_tun():
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled"))
        endpoint = body.get("endpoint")
        if enabled and not endpoint:
            return err("invalid_request", "endpoint is required when enabling", 400)
        caps = capabilities()
        if not caps["tun"] or not caps["docker"]:
            return err(
                "host_privilege",
                "TUN mode needs /dev/net/tun and the Docker socket",
                502,
            )
        with lock:
            if enabled:
                try:
                    config = render_tun_config(
                        endpoint,
                        body.get("username") or "",
                        body.get("password") or "",
                        interface_name=settings.tun_interface,
                        address=settings.tun_address,
                        mtu=settings.tun_mtu,
                    )
                except ValueError as exc:
                    return err("invalid_request", str(exc), 400)
                try:
                    controller.start(config)
                except TunRuntimeError as exc:
                    return err("host_privilege", str(exc), 502)
                tun_state.update(
                    {
                        "enabled": True,
                        "iface": settings.tun_interface,
                        "endpoint": endpoint,
                    }
                )
            else:
                try:
                    controller.stop()
                except TunRuntimeError as exc:
                    return err("host_privilege", str(exc), 502)
                tun_state.update(
                    {
                        "enabled": False,
                        "iface": settings.tun_interface,
                        "endpoint": None,
                    }
                )
        return jsonify({"tun": {**tun_state, "running": enabled}})

    return app


def make_default_app() -> Flask:
    return create_app(HostAgentSettings.from_env())
