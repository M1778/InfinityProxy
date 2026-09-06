"""Liveness probe for a single node.

Sole comment (the non-obvious why): full per-protocol relay handshakes
(VLESS/VMess/SS/SSR/Trojan/TUIC/Hysteria2) are deferred; this TCP dial plus a
best-effort protocol hello is the batching filter's v1 liveness semantics,
keeping the spec's "batched probes, 4s timeout" promise while a deeper probe is
tracked. A TLS-shaped relay counts alive only when a response byte arrives
within the timeout; a silently accepted socket stays dead, so the filter does
not certify listeners that merely accept connections without relaying.
"""

from __future__ import annotations

import socket
import time

from engine.models import Node, ProbeResult

_HELLO = b"\x16\x03\x01\x00\x20\x01\x00\x00\x1c\x03\x03" + b"\x00" * 28

_TLSISH_PROTOCOLS = frozenset(
    ("trojan", "vless", "vmess", "ss", "ssr", "tuic", "hysteria2")
)


def probe(node: Node, timeout_s: float) -> ProbeResult:
    started = time.monotonic()
    deadline = started + timeout_s
    try:
        alive = _attempt(node, deadline)
    except OSError as exc:
        error = (
            "timeout" if isinstance(exc, socket.timeout) else (exc.strerror or str(exc))
        )
        return ProbeResult(
            node_id=node.node_id,
            alive=False,
            latency_ms=_latency_ms(started),
            error=error,
        )
    return ProbeResult(
        node_id=node.node_id, alive=alive, latency_ms=_latency_ms(started)
    )


def _attempt(node: Node, deadline: float) -> bool:
    with socket.create_connection(
        (node.server, node.port), timeout=_remaining(deadline)
    ) as sock:
        if node.protocol in _TLSISH_PROTOCOLS:
            sock.sendall(_HELLO)
            sock.settimeout(_remaining(deadline))
            if sock.recv(1) == b"":
                raise ConnectionError("peer closed without a response")
        return True


def _remaining(deadline: float) -> float:
    return max(0.001, deadline - time.monotonic())


def _latency_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
