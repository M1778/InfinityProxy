from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class PanelSettings:
    host: str = "127.0.0.1"
    port: int = 8000
    engine_url: str = "http://127.0.0.1:8787"
    test_url: str = "https://api.ipify.org"
    poll_interval_s: int = 5
    history_seconds: int = 7200
    sse_heartbeat_s: int = 15

    @classmethod
    def from_env(cls) -> "PanelSettings":
        return cls(
            host=os.environ.get("INFINITY_PANEL_HOST", "127.0.0.1"),
            port=_env_int("INFINITY_PANEL_PORT", 8000),
            engine_url=os.environ.get("INFINITY_ENGINE_URL", "http://127.0.0.1:8787"),
            test_url=os.environ.get("INFINITY_PANEL_TEST_URL", "https://api.ipify.org"),
        )
