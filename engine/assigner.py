"""Exclusive node assignment plus port/credential allocation.

See docs/architecture.md "Assigner" and ADR-0002 for the exclusivity rule.
"""

from __future__ import annotations

import secrets
import socket
from typing import Protocol

from .config import Settings
from .models import Node, Tunnel
from .stability import score_nodes


class Store(Protocol):
    """Minimal surface the assigner needs from engine.db. Real Store has more."""

    def get_tunnel(self, tunnel_id: str) -> Tunnel | None: ...

    def save_tunnel(self, tunnel: Tunnel) -> None: ...

    def load_nodes(
        self, state: str | None = None, protocols: set[str] | None = None
    ) -> list[Node]: ...

    def node_ids_in_use(self) -> list[str]: ...

    def assign_nodes(self, node_ids: list[str], tunnel_id: str) -> None: ...

    def release_tunnel(self, tunnel_id: str) -> None: ...

    def port_in_use(self, port: int) -> bool: ...


def _host_port_free(host: str, port: int) -> bool:
    # Tunnels run on the host network, so a port free in SQLite can still be
    # taken on the OS. Probe a bind before handing it out.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
    except OSError:
        return False
    return True


class PortExhausted(Exception):
    """No free port left in the configured tunnel range."""


def _prefer_best(nodes: list[Node]) -> list[Node]:
    # Throughput-certified nodes first (pick the best downloaders for the
    # proxy servers), their fastest first; tested-but-unmeasured next; then
    # untested alive nodes.
    return sorted(
        nodes,
        key=lambda n: (
            n.throughput_kb_s is None,
            -(n.throughput_kb_s or 0),
            n.last_latency_ms is None,
        ),
    )


class Assigner:
    """Exclusive alive-node assignment plus port/credential allocation."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def allocate_port(self, store: Store) -> int:
        for port in self.settings.tunnel_ports:
            if store.port_in_use(port):
                continue
            if not _host_port_free(self.settings.host, port):
                continue
            return port
        raise PortExhausted(
            "no free port in "
            f"{self.settings.tunnel_port_start}-{self.settings.tunnel_port_end}"
        )

    def generate_credentials(self) -> tuple[str, str]:
        # Fresh username/password: entropy makes collision checks unnecessary.
        return f"u_{secrets.token_urlsafe(8)}", f"p_{secrets.token_urlsafe(8)}"

    def _free_nodes(self, store: Store, protocols: set[str] | None) -> list[Node]:
        in_use = set(store.node_ids_in_use())
        nodes = [
            n
            for n in store.load_nodes(state="alive", protocols=protocols)
            if n.node_id not in in_use
        ]
        if self.settings.stability_enabled:
            # ADR-0009 membership vote: Tier A, then Tier B, then cold nodes,
            # each ordered by weighted availability + within-protocol speed.
            return score_nodes(
                nodes,
                min_probes=self.settings.stability_min_probes,
                min_avail=self.settings.stability_min_avail,
                weight_avail=self.settings.stability_weight_avail,
                weight_speed=self.settings.stability_weight_speed,
            )
        return _prefer_best(nodes)

    def assign_new(
        self,
        store: Store,
        tunnel_id: str,
        requested: int,
        protocols: set[str] | None = None,
    ) -> list[Node]:
        """Grant up to `requested` exclusive alive nodes and record the assignment."""
        if requested <= 0:
            return []
        granted = self._free_nodes(store, protocols)[:requested]
        if granted:
            store.assign_nodes([n.node_id for n in granted], tunnel_id)
        return granted

    def top_up(
        self,
        store: Store,
        tunnel_id: str,
        target: int,
        protocols: set[str] | None = None,
    ) -> list[Node]:
        """Add exclusive nodes toward a tunnel's requested count; returns only new ones."""  # noqa: E501
        tunnel = store.get_tunnel(tunnel_id)
        if tunnel is None:
            return []
        room = target - tunnel.node_count_granted
        if room <= 0:
            return []
        added = self._free_nodes(store, protocols)[:room]
        if added:
            store.assign_nodes([n.node_id for n in added], tunnel_id)
        return added

    def release_tunnel(self, store: Store, tunnel_id: str) -> int:
        """Free every node assigned to the tunnel; returns the number freed.

        The release is a pure node-table update and must run even when the tunnel
        row is already gone, or its assignments leak and starve the pool forever.
        """
        freed = store.release_tunnel(tunnel_id)
        tunnel = store.get_tunnel(tunnel_id)
        if tunnel is not None and tunnel.node_count_granted:
            tunnel.node_count_granted = 0
            store.save_tunnel(tunnel)
        return freed

    def create_tunnel_config(
        self,
        store: Store,
        requested: int,
        auto_renew: bool,
        protocols: set[str] | None = None,
    ) -> tuple[Tunnel, list[Node]]:
        """Documented create path: port, credentials, assignment, and a saved tunnel."""
        tunnel_id = f"tu_{secrets.token_hex(3)}"
        port = self.allocate_port(store)
        username, password = self.generate_credentials()
        nodes = self.assign_new(store, tunnel_id, requested, protocols=protocols)
        tunnel = Tunnel(
            tunnel_id=tunnel_id,
            state="starting",
            port=port,
            username=username,
            password=password,
            node_count_requested=requested,
            node_count_granted=len(nodes),
            auto_renew=auto_renew,
        )
        store.save_tunnel(tunnel)
        return tunnel, nodes
