"""Canonical domain dataclasses. Vocabulary per CONTEXT.md."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

PROXY_PROTOCOLS = (
    "vless",
    "vmess",
    "ss",
    "ssr",
    "trojan",
    "tuic",
    "hysteria2",
    "http",
    "socks5",
)

NODE_STATES = ("untested", "alive", "dead")
TUNNEL_STATES = ("starting", "running", "stopping", "stopped")


@dataclass(frozen=True)
class SourceManifest:
    """A configured public feed the scraper pulls nodes from (docs/scraping.md)."""

    name: str
    urls: tuple[str, ...]
    cadence_s: int
    encoding: str  # "plain" | "base64"
    line_separated: bool
    license: str
    protocols: tuple[str, ...]


@dataclass(frozen=True)
class NodeCandidate:
    """A parsed, not-yet-tested node line from a source feed."""

    uri: str
    protocol: str
    server: str
    port: int
    user: str | None
    source: str

    def identity_key(self) -> str:
        return f"{self.server}:{self.port}:{self.user or ''}"


@dataclass
class Node:
    """A node tracked in the pool. Data, never an endpoint clients dial directly."""

    node_id: str
    uri: str
    protocol: str
    server: str
    port: int
    user: str | None
    source: str
    first_seen_s: float
    last_latency_ms: int | None = None
    state: str = "untested"
    throughput_kb_s: int | None = None
    probe_ok: int = 0
    probe_total: int = 0
    window_started_s: float | None = None
    last_probe_s: float | None = None
    last_alive_s: float | None = None
    score_f: float | None = None

    @classmethod
    def from_candidate(cls, c: NodeCandidate) -> "Node":
        key = c.identity_key()
        return cls(
            node_id=hashlib.sha1(key.encode("utf-8")).hexdigest()[:16],
            uri=c.uri,
            protocol=c.protocol,
            server=c.server,
            port=c.port,
            user=c.user,
            source=c.source,
            first_seen_s=time.time(),
        )

    @property
    def identity_key(self) -> str:
        return f"{self.server}:{self.port}:{self.user or ''}"


@dataclass(frozen=True)
class ProbeResult:
    node_id: str
    alive: bool
    latency_ms: int | None = None
    error: str | None = None
    throughput_kb_s: int | None = None


@dataclass
class Tunnel:
    """A spawned rotating proxy endpoint, stable host/port/credentials for life."""

    tunnel_id: str
    state: str = "starting"
    port: int = 0
    username: str = ""
    password: str = ""
    node_count_requested: int = 0
    node_count_granted: int = 0
    auto_renew: bool = True
    created_at_s: float = field(default_factory=time.time)
    updated_at_s: float = field(default_factory=time.time)

    @property
    def degraded(self) -> bool:
        return self.node_count_granted < self.node_count_requested
