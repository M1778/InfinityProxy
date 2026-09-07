"""Liveness probe for a single node (ADR-0006).

v2 semantics: a node is alive only when the server itself completes its wire
protocol against a probe request, never because it answered bytes. The TCP-plus-
hello v1 check (which certified HTTP responders as proxies) is kept only for
protocols whose full client is deferred: ss/vmess/tuic/hysteria2.

Full relay checks:
- VLESS over TCP or stdlib TLS: send a VLESS CONNECT header for a benign target,
  expect the server's 2-byte response header (`0x00 0x00`). "Echo" listeners and
  HTTP responders both fail: an HTTP reply's first byte is 0x48, not version 0.
- Trojan over stdlib TLS: send the CRLF/CMD/ATYP/ADDR/PORT/CRLF header with the
  password's SHA224 hex payload, expect `\\r\\n`.

Deliberately not emulated (documented in ADR-0006): reality's browser TLS
fingerprint (stdlib cannot reproduce it, real hosts may false-negative), and
ws/gRPC VLESS upgrades (the peer is always probed over plain TCP/TLS).
"""

from __future__ import annotations

import dataclasses
import hashlib
import socket
import ssl
import time
import uuid
from urllib.parse import parse_qs, unquote, urlparse

from engine.models import Node, ProbeResult

_HELLO = b"\x16\x03\x01\x00\x20\x01\x00\x00\x1c\x03\x03" + b"\x00" * 28

_RELAY_PROTOCOLS = frozenset(("vless", "trojan"))

_V1_FALLBACK_PROTOCOLS = frozenset(
    ("vmess", "ss", "tuic", "hysteria2", "socks5", "http", "ssr")
)

_PATH_TARGET = b"www.google.com"
_TARGET_PORT = 80


def probe(node: Node, timeout_s: float) -> ProbeResult:
    started = time.monotonic()
    deadline = started + timeout_s
    try:
        if node.protocol in _RELAY_PROTOCOLS:
            alive, error = _attempt_relay(node, deadline)
        else:
            alive, error = _attempt_v1(node, deadline)
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
    except Exception as exc:  # noqa: BLE001 - never let one bad URI take a batch down
        return ProbeResult(
            node_id=node.node_id,
            alive=False,
            latency_ms=_latency_ms(started),
            error=f"probe raised: {exc}",
        )
    return ProbeResult(
        node_id=node.node_id,
        alive=alive,
        latency_ms=_latency_ms(started),
        error=error,
    )


# -- v2: per-protocol relay handshakes ----------------------------------------


def _attempt_relay(node: Node, deadline: float) -> tuple[bool, str | None]:
    conf = _relay_config(node)
    raw = socket.create_connection(
        (node.server, node.port), timeout=_remaining(deadline)
    )
    with raw:
        raw.settimeout(_remaining(deadline))
        sent = _challenge_for(node.protocol, conf, node)
        if conf.tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with ctx.wrap_socket(raw, server_hostname=conf.sni or node.server) as tls:
                tls.settimeout(_remaining(deadline))
                tls.sendall(sent)
                expected = _expected_reply(node.protocol)
                reply = tls.recv(2)
        else:
            raw.sendall(sent)
            expected = _expected_reply(node.protocol)
            reply = raw.recv(2)
        if reply == expected:
            return True, None
        return False, (
            f"expected {node.protocol} reply {expected!r}, got {reply[:2]!r} "
            "(non-relay responder)"
        )
    return False, "closed before reply"


@dataclasses.dataclass(frozen=True)
class _RelayConfig:
    tls: bool
    sni: str | None


def _relay_config(node: Node) -> _RelayConfig:
    u = urlparse(node.uri)
    q = parse_qs(u.query)
    if node.protocol == "trojan":
        return _RelayConfig(tls=True, sni=_first(q, "sni") or u.hostname)
    security = (_first(q, "security") or "").lower()
    tls = security in ("tls", "reality", "xtls") or _first(q, "sni") is not None
    sni = _first(q, "sni")
    return _RelayConfig(tls=tls, sni=sni)


def _challenge_for(protocol: str, conf: _RelayConfig, node: Node) -> bytes:
    if protocol == "trojan":
        return _trojan_challenge(node, conf, _PATH_TARGET, _TARGET_PORT)
    return _vless_challenge(node, conf, _PATH_TARGET, _TARGET_PORT)


def _vless_challenge(node: Node, conf: _RelayConfig, host: bytes, port: int) -> bytes:
    u = urlparse(node.uri)
    raw_uuid = unquote(u.username or "")
    try:
        uuid_bytes = uuid.UUID(raw_uuid).bytes
    except (ValueError, TypeError) as exc:
        raise ValueError(f"node {node.node_id!r}: cannot parse vless uuid") from exc
    return (
        b"\x00"  # version
        + uuid_bytes  # 16 bytes
        + b"\x00"  # addons length
        + b"\x01"  # command: CONNECT
        + port.to_bytes(2, "big")
        + b"\x03"  # address type: domain
        + bytes([len(host)])
        + host
    )


def _trojan_challenge(node: Node, conf: _RelayConfig, host: bytes, port: int) -> bytes:
    u = urlparse(node.uri)
    password = unquote(u.username or "")
    sha_hex = hashlib.sha224(password.encode("utf-8")).hexdigest().encode("ascii")
    return (
        b"\x0d\x0a"
        + b"\x01"  # command: CONNECT
        + b"\x03"  # address type: domain
        + bytes([len(host)])
        + host
        + port.to_bytes(2, "big")
        + b"\x0d\x0a"
        + sha_hex
    )


def _expected_reply(protocol: str) -> bytes:
    return b"\r\n" if protocol == "trojan" else b"\x00\x00"


# -- v1 gating for deferred protocols ------------------------------------------


def _attempt_v1(node: Node, deadline: float) -> tuple[bool, str | None]:
    with socket.create_connection(
        (node.server, node.port), timeout=_remaining(deadline)
    ) as sock:
        if node.protocol in _V1_FALLBACK_PROTOCOLS:
            sock.sendall(_HELLO)
            sock.settimeout(_remaining(deadline))
            if sock.recv(1) == b"":
                raise ConnectionError("peer closed without a response")
        return True, None


def _remaining(deadline: float) -> float:
    return max(0.001, deadline - time.monotonic())


def _latency_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _first(q: dict[str, list[str]], key: str) -> str | None:
    values = q.get(key)
    return values[0] if values else None
