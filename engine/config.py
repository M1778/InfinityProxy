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
    port: int = 8787
    panel_port: int = 8000
    panel_host: str = "127.0.0.1"
    engine_url: str = ""
    panel_base_url: str = ""
    tunnel_port_start: int = 10000
    tunnel_port_end: int = 59999
    batch_size: int = 50
    probe_timeout_ms: int = 4000
    probe_budget_per_refresh: int = 5000
    throughput_enabled: bool = True
    throughput_min_kb_s: int = 200
    throughput_sample_bytes: int = 1_048_576
    throughput_timeout_s: float = 12.0
    throughput_host: str = "speedtest.tele2.net"
    throughput_port: int = 80
    throughput_path: str = "/1MB.bin"
    health_interval_s: int = 30
    max_misses: int = 2
    urltest_interval_s: int = 30
    stability_enabled: bool = False
    stability_min_probes: int = 6
    stability_min_avail: float = 0.4
    stability_weight_avail: float = 0.6
    stability_weight_speed: float = 0.4
    stability_window_s: int = 3600
    stability_max_age_s: int = 21600
    stability_working_set: int = 256
    stability_working_set_cadence_s: int = 300
    stability_reprobe_min_s: int = 120
    db_path: str = "infinity.db"
    singbox_image: str = "ghcr.io/sagernet/sing-box:v1.11.6"
    engine_name_prefix: str = "infinity"
    host_enabled: bool = False
    hostagent_url: str = "http://127.0.0.1:8788"
    hostagent_port: int = 8788
    host_pick_ttl_s: int = 60
    host_pick_test_url: str = "https://api.ipify.org"
    host_pick_sample_bytes: int = 262144
    host_pick_timeout_s: float = 8.0

    @classmethod
    def from_env(cls) -> "Settings":
        range_raw = os.environ.get("INFINITY_TUNNEL_RANGE", "10000-59999")
        start_s, _, end_s = range_raw.partition("-")
        port = _env_int("INFINITY_PORT", 8787)
        panel_port = _env_int("INFINITY_PANEL_PORT", 8000)
        return cls(
            host=os.environ.get("INFINITY_HOST", "127.0.0.1"),
            port=port,
            panel_port=panel_port,
            panel_host=os.environ.get("INFINITY_PANEL_HOST", "127.0.0.1"),
            engine_url=os.environ.get(
                "INFINITY_ENGINE_URL", f"http://127.0.0.1:{port}"
            ),
            panel_base_url=os.environ.get(
                "INFINITY_PANEL_BASE_URL", f"http://127.0.0.1:{panel_port}"
            ),
            tunnel_port_start=_env_int("INFINITY_TUNNEL_RANGE_START", int(start_s)),
            tunnel_port_end=_env_int("INFINITY_TUNNEL_RANGE_END", int(end_s)),
            batch_size=_env_int("INFINITY_BATCH_SIZE", 50),
            probe_timeout_ms=_env_int("INFINITY_PROBE_TIMEOUT_MS", 4000),
            throughput_enabled=os.environ.get(
                "INFINITY_THROUGHPUT_ENABLED", "1"
            ).lower()
            not in ("0", "false", "no"),
            throughput_min_kb_s=_env_int("INFINITY_THROUGHPUT_MIN_KB_S", 200),
            throughput_sample_bytes=_env_int(
                "INFINITY_THROUGHPUT_SAMPLE_BYTES", 1048576
            ),
            throughput_timeout_s=float(
                os.environ.get("INFINITY_THROUGHPUT_TIMEOUT_S", "12.0")
            ),
            throughput_host=os.environ.get(
                "INFINITY_THROUGHPUT_HOST", "speedtest.tele2.net"
            ),
            throughput_port=_env_int("INFINITY_THROUGHPUT_PORT", 80),
            throughput_path=os.environ.get("INFINITY_THROUGHPUT_PATH", "/1MB.bin"),
            health_interval_s=_env_int("INFINITY_HEALTH_INTERVAL_S", 30),
            max_misses=_env_int("INFINITY_MAX_MISSES", 2),
            urltest_interval_s=max(10, _env_int("INFINITY_URTEST_INTERVAL_S", 30)),
            stability_enabled=os.environ.get("INFINITY_STABILITY_ENABLED", "1").lower()
            not in ("0", "false", "no"),
            stability_min_probes=_env_int("INFINITY_STABILITY_MIN_PROBES", 6),
            stability_min_avail=float(
                os.environ.get("INFINITY_STABILITY_MIN_AVAIL", "0.4")
            ),
            stability_weight_avail=float(
                os.environ.get("INFINITY_STABILITY_WEIGHT_AVAIL", "0.6")
            ),
            stability_weight_speed=float(
                os.environ.get("INFINITY_STABILITY_WEIGHT_SPEED", "0.4")
            ),
            stability_window_s=_env_int("INFINITY_STABILITY_WINDOW_S", 3600),
            stability_max_age_s=_env_int("INFINITY_STABILITY_MAX_AGE_S", 21600),
            stability_working_set=_env_int("INFINITY_STABILITY_WORKING_SET", 256),
            stability_working_set_cadence_s=_env_int(
                "INFINITY_STABILITY_WORKING_SET_CADENCE_S", 300
            ),
            stability_reprobe_min_s=_env_int("INFINITY_STABILITY_REPROBE_MIN_S", 120),
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
            hostagent_port=_env_int("INFINITY_HOSTAGENT_PORT", 8788),
            host_enabled=os.environ.get("INFINITY_HOST_ENABLED", "0").lower()
            not in ("0", "false", "no"),
            hostagent_url=os.environ.get(
                "INFINITY_HOSTAGENT_URL",
                f"http://127.0.0.1:{_env_int('INFINITY_HOSTAGENT_PORT', 8788)}",
            ),
            host_pick_ttl_s=_env_int("INFINITY_HOST_PICK_TTL_S", 60),
            host_pick_test_url=os.environ.get(
                "INFINITY_HOST_PICK_TEST_URL", "https://api.ipify.org"
            ),
            host_pick_sample_bytes=_env_int("INFINITY_HOST_PICK_SAMPLE_BYTES", 262144),
            host_pick_timeout_s=float(
                os.environ.get("INFINITY_HOST_PICK_TIMEOUT_S", "8.0")
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
