"""Desktop proxy setters and host capability detection (ADR-0010)."""

from __future__ import annotations

import os
import platform as _platform
import shutil
import subprocess
import time
from typing import Any

PROXY_CAPABILITY_NONE = "none"

_GNOME_SCHEMAS = (
    "org.gnome.system.proxy.http",
    "org.gnome.system.proxy.https",
    "org.gnome.system.proxy.socks",
)

_KDE_MODE_MANUAL = "manual"
_KDE_MODE_NONE = "none"


def _system() -> str:
    return _platform.system().lower()


def _machine() -> str:
    return _platform.machine()


def _docker_socket_present(socket_path: str = "/var/run/docker.sock") -> bool:
    return os.path.exists(socket_path)


def _tun_present(device_path: str = "/dev/net/tun") -> bool:
    return os.path.exists(device_path)


def _detect_proxy_capability() -> str:
    """Name the desktop proxy setter available on this session, or 'none'."""
    if shutil.which("gsettings") is not None:
        return "gsettings"
    if shutil.which("kwriteconfig5") is not None:
        return "kwriteconfig"
    return PROXY_CAPABILITY_NONE


def _split_endpoint(endpoint: str) -> tuple[str, int]:
    host, _, port_s = endpoint.rpartition(":")
    if not host or not port_s.isdigit():
        raise ValueError(f"endpoint must look like host:port, got {endpoint!r}")
    return host, int(port_s)


def _gsettings_set(enabled: bool, endpoint: str | None, run: Any) -> dict[str, str]:
    gnome = "org.gnome.system.proxy"
    if enabled and endpoint:
        host, port = _split_endpoint(endpoint)
        for schema in _GNOME_SCHEMAS:
            run(["gsettings", "set", schema, "host", host], check=True)
            run(["gsettings", "set", schema, "port", str(port)], check=True)
        run(["gsettings", "set", gnome, "mode", _KDE_MODE_MANUAL], check=True)
    else:
        run(["gsettings", "set", gnome, "mode", _KDE_MODE_NONE], check=True)
    return {"mode": _KDE_MODE_MANUAL if enabled else _KDE_MODE_NONE}


def _kwriteconfig_set(enabled: bool, endpoint: str | None, run: Any) -> dict[str, str]:
    """KDE kioslaverc. Plasma applies it on KConfig change signals; the agent
    writes the file through kwriteconfig5 rather than re-sending D-Bus config
    signals it cannot reliably target from inside a container."""
    group = ["--file", "kioslaverc", "--group", "Proxy Settings"]
    if enabled and endpoint:
        host, port = _split_endpoint(endpoint)
        for scheme, key in (
            ("http", "httpProxy"),
            ("https", "httpsProxy"),
            ("socks", "socksProxy"),
        ):
            run(
                ["kwriteconfig5", *group, "--key", key, f"{scheme}://{host}:{port}"],
                check=True,
            )
        run(
            ["kwriteconfig5", *group, "--key", "ProxyType", "1"],
            check=True,
        )
    else:
        run(["kwriteconfig5", *group, "--key", "ProxyType", "0"], check=True)
    return {"mode": _KDE_MODE_MANUAL if enabled else _KDE_MODE_NONE}


def _system_proxy_set(
    capability: str, enabled: bool, endpoint: str | None, run: Any = subprocess.run
) -> dict[str, str]:
    """Apply the system proxy state through the detected desktop setter."""
    if capability == "gsettings":
        return _gsettings_set(enabled, endpoint, run)
    if capability == "kwriteconfig":
        return _kwriteconfig_set(enabled, endpoint, run)
    raise ValueError(f"no system proxy setter for capability {capability!r}")


def _uptime_s(started: float, clock: Any = time.time) -> int:
    return int(clock() - started)
