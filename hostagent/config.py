"""Hostagent settings from the environment (docs/api.md#configuration)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


@dataclass(frozen=True)
class HostAgentSettings:
    host: str = "127.0.0.1"
    port: int = 8788
    uid: int = 1000
    # Container-side path where the desktop session bus socket is mounted.
    bus_path: str = ""
    tun_device: str = "/dev/net/tun"
    tun_interface: str = "tun0"
    tun_mtu: int = 1500
    tun_address: str = "172.19.0.1/30"
    singbox_image: str = "ghcr.io/sagernet/sing-box:v1.11.6"
    engine_name_prefix: str = "infinity"

    @classmethod
    def from_env(cls) -> "HostAgentSettings":
        uid = _env_int("INFINITY_HOST_UID", 1000)
        return cls(
            host=os.environ.get("INFINITY_HOSTAGENT_HOST", "127.0.0.1"),
            port=_env_int("INFINITY_HOSTAGENT_PORT", 8788),
            uid=uid,
            bus_path=os.environ.get("INFINITY_HOST_BUS", f"/run/user/{uid}/bus"),
            tun_device=os.environ.get("INFINITY_TUN_DEVICE", "/dev/net/tun"),
            tun_interface=os.environ.get("INFINITY_TUN_INTERFACE", "tun0"),
            tun_mtu=_env_int("INFINITY_TUN_MTU", 1500),
            tun_address=os.environ.get("INFINITY_TUN_ADDRESS", "172.19.0.1/30"),
            singbox_image=os.environ.get(
                "INFINITY_SINGBOX_IMAGE", "ghcr.io/sagernet/sing-box:v1.11.6"
            ),
            engine_name_prefix=os.environ.get(
                "INFINITY_ENGINE_NAME_PREFIX", "infinity"
            ),
        )
