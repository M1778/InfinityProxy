"""Render a sing-box TUN config that routes the whole namespace via a tunnel."""

from __future__ import annotations

from typing import Any

_TUN_INET4 = "172.19.0.1/30"


def render_tun_config(
    endpoint: str,
    username: str,
    password: str,
    *,
    interface_name: str = "tun0",
    address: str = _TUN_INET4,
    mtu: int = 1500,
) -> dict[str, Any]:
    """TUN inbound with auto route + strict route, socks out to the tunnel.

    Only the tunnel's mixed worker must be reachable from the host namespace
    (it is: engine tunnels listen on 127.0.0.1 with host networking).
    """
    host, _, port_s = endpoint.rpartition(":")
    if not host or not port_s.isdigit():
        raise ValueError(f"endpoint must look like host:port, got {endpoint!r}")
    socks: dict[str, Any] = {
        "type": "socks",
        "tag": "host-tunnel",
        "server": host,
        "server_port": int(port_s),
        "version": "5",
    }
    if username or password:
        socks["username"] = username
        socks["password"] = password
    return {
        "log": {"level": "warn"},
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "interface_name": interface_name,
                "inet4_address": [address],
                "mtu": mtu,
                "auto_route": True,
                "strict_route": True,
                "stack": "system",
            }
        ],
        "outbounds": [
            socks,
            {"type": "direct", "tag": "direct"},
        ],
        "route": {"final": "host-tunnel"},
        "dns": {
            "servers": [
                {
                    "tag": "remote",
                    "address": "udp://1.1.1.1",
                    "address_resolver": "local",
                    "detour": "host-tunnel",
                },
                {"tag": "local", "address": "local"},
            ],
            "final": "remote",
        },
    }
