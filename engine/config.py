"""Settings loaded from the environment. Defaults per docs/api.md#configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    tunnel_port_start: int = 10000
    tunnel_port_end: int = 59999
    batch_size: int = 50
    probe_timeout_ms: int = 4000
    probe_budget_per_refresh: int = 5000
    health_interval_s: int = 30
    max_misses: int = 2
    db_path: str = "infinity.db"
    singbox_image: str = "ghcr.io/sagernet/sing-box:v1.11.6"
    engine_name_prefix: str = "infinity"

    @classmethod
    def from_env(cls) -> "Settings":
        range_raw = os.environ.get("INFINITY_TUNNEL_RANGE", "10000-59999")
        start_s, _, end_s = range_raw.partition("-")
        return cls(
            host=os.environ.get("INFINITY_HOST", "127.0.0.1"),
            port=_env_int("INFINITY_PORT", 8000),
            tunnel_port_start=_env_int("INFINITY_TUNNEL_RANGE_START", int(start_s)),
            tunnel_port_end=_env_int("INFINITY_TUNNEL_RANGE_END", int(end_s)),
            batch_size=_env_int("INFINITY_BATCH_SIZE", 50),
            probe_timeout_ms=_env_int("INFINITY_PROBE_TIMEOUT_MS", 4000),
            health_interval_s=_env_int("INFINITY_HEALTH_INTERVAL_S", 30),
            max_misses=_env_int("INFINITY_MAX_MISSES", 2),
            probe_budget_per_refresh=_env_int(
                "INFINITY_PROBE_BUDGET_PER_REFRESH", 5000
            ),
            db_path=os.environ.get("INFINITY_DB", "infinity.db"),
            singbox_image=os.environ.get(
                "INFINITY_SINGBOX_IMAGE", "ghcr.io/sagernet/sing-box:v1.11.6"
            ),
            engine_name_prefix=os.environ.get(
                "INFINITY_ENGINE_NAME_PREFIX", "infinity"
            ),
        )

    @property
    def tunnel_ports(self) -> range:
        if self.tunnel_port_start > self.tunnel_port_end:
            raise ValueError("INFINITY_TUNNEL_RANGE start must be <= end")
        return range(self.tunnel_port_start, self.tunnel_port_end + 1)

    @property
    def probe_timeout_s(self) -> float:
        return self.probe_timeout_ms / 1000.0
