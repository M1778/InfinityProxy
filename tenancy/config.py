"""Settings for the multi-tenant control center (ADR-0011).

Env prefix `INFINITY_TENANCY_*`; every value overridable via the environment
table in docs/api.md#control-center-configuration.
"""

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


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).lower() not in ("0", "false", "no")


@dataclass(frozen=True)
class TenancySettings:
    host: str = "127.0.0.1"
    port: int = 8790
    engine_url: str = "http://127.0.0.1:8787"
    db_path: str = "tenancy.db"
    admin_key: str = ""
    tenant_page_title: str = "InfinityProxy control center"
    public_tunnel_host: str = ""
    starting_credits: float = 720.0
    max_tenants: int = 50
    max_tunnels_per_tenant: int = 4
    max_nodes_per_tunnel: int = 20
    credit_interval_s: int = 3600
    meter_enabled: bool = True
    ledger_keep: int = 50

    @classmethod
    def from_env(cls) -> "TenancySettings":
        port = _env_int("INFINITY_TENANCY_PORT", 8790)
        return cls(
            host=os.environ.get("INFINITY_TENANCY_HOST", "127.0.0.1"),
            port=port,
            engine_url=os.environ.get(
                "INFINITY_TENANCY_ENGINE_URL", "http://127.0.0.1:8787"
            ),
            db_path=os.environ.get("INFINITY_TENANCY_DB", "tenancy.db"),
            admin_key=os.environ.get("INFINITY_TENANCY_ADMIN_KEY", ""),
            tenant_page_title=os.environ.get(
                "INFINITY_TENANCY_PAGE_TITLE", "InfinityProxy control center"
            ),
            public_tunnel_host=os.environ.get("INFINITY_TENANCY_TUNNEL_HOST", ""),
            starting_credits=_env_float("INFINITY_TENANCY_STARTING_CREDITS", 720.0),
            max_tenants=_env_int("INFINITY_TENANCY_MAX_TENANTS", 50),
            max_tunnels_per_tenant=_env_int("INFINITY_TENANCY_MAX_TUNNELS", 4),
            max_nodes_per_tunnel=_env_int("INFINITY_TENANCY_MAX_NODES", 20),
            credit_interval_s=_env_int("INFINITY_TENANCY_CREDIT_INTERVAL_S", 3600),
            meter_enabled=_env_bool("INFINITY_TENANCY_METER_ENABLED", "1"),
        )
