"""Liveness probe for a single node (ADR-0006).

v2 semantics: a node is alive only when the server itself completes its wire
protocol against a probe request, never because it answered bytes. There is no
TCP-hello v1 fallback: protocols without an implemented client here (vmess,
tuic, hysteria2, ssr) are rejected, not hello-certified.

Full relay checks:
- VLESS over TCP or stdlib TLS: send a VLESS CONNECT header for a benign target,
  expect the server's 2-byte response header (`0x00 0x00`). "Echo" listeners and
  HTTP responders both fail: an HTTP reply's first byte is 0x48, not version 0.
- Trojan over stdlib TLS: send the CRLF/CMD/ATYP/ADDR/PORT/CRLF header with the
  password's SHA224 hex payload, expect `\r\n`.
- Shadowsocks (SIP004 AEAD ciphers): derive the session subkey with HKDF-SHA1
  from the EVP_BytesToKey MD5 master key plus a random salt, send
  `[salt][AE len][tag][AE addr+GET][tag]`, and require the server's own
  `[salt][AE len][tag][AE relayed][tag]` to decrypt to a target 2xx/3xx status
  line. Chunk nonce is a little-endian counter incremented per AEAD op, exactly
  as sing-box's shadowaead framing. Stream ciphers and 2022-blake3 methods
  cannot be relay-certified and are rejected at probe time.
- HTTP/SOCKS5 forward proxies: complete the CONNECT handshake to the target,
  then require the relayed target response to be a 2xx/3xx `HTTP/x.y` line.

The relayed bytes themselves must be a plausible `HTTP/x.y 2xx/3xx` status line
from the requested target. A peer that answers the CONNECT with its own canned
HTTP error (a honeypot fronting the target) is rejected even though the wire
handshake completed.

Deliberately not emulated (documented in ADR-0006): reality's browser TLS
fingerprint (stdlib cannot reproduce it, real hosts may false-negative), and
ws/gRPC upgrades (the peer is always probed over plain TCP/TLS).
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import hmac
import json as _json
import os
import re
import socket
import ssl
import time
import uuid
from urllib.parse import parse_qs, unquote, urlparse

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305

from engine.models import Node, ProbeResult

_RELAY_PROTOCOLS = frozenset(("vless", "trojan"))

_FORWARD_PROTOCOLS = frozenset(("http", "socks5"))

_PATH_TARGET = os.environ.get("INFINITY_RELAY_TARGET_HOST", "www.google.com").encode(
    "ascii"
)
_TARGET_PORT = int(os.environ.get("INFINITY_RELAY_TARGET_PORT", "80"))


@dataclasses.dataclass(frozen=True)
class _SSAEADSpec:
    key_len: int
    salt_len: int
    nonce_len: int
    aead: object  # a cryptography.hazmat AEAD cipher class


# SIP004 AEAD ciphers as sing-box shadowaead defines them (key/salt/nonce sizes).
# Anything not in this table (stream ciphers, 2022-blake3 methods, xchacha20-
# ietf-poly1305) stays unprobeable: this probe can only complete the AEAD relay
# round-trip with the AEAD ciphers cryptography exposes on the OpenSSL backend,
# and un-tested methods would weaken live assignments exactly like ws/gRPC did.
_SS_AEAD_SPECS = {
    "aes-128-gcm": _SSAEADSpec(16, 16, 12, AESGCM),
    "aes-192-gcm": _SSAEADSpec(24, 24, 12, AESGCM),
    "aes-256-gcm": _SSAEADSpec(32, 32, 12, AESGCM),
    "chacha20-ietf-poly1305": _SSAEADSpec(32, 32, 12, ChaCha20Poly1305),
}

_SS_TAG_LEN = 16
_SS_MAX_PACKET_SIZE = 16 * 1024 - 1  # 0x3FFF, sing-box / SIP004


def probeable(node: Node) -> bool:
    """True when the node can complete a relay round-trip under this probe.

    Every certified protocol has a real client handshake here: VLESS and Trojan
    (relay), Shadowsocks AEAD (relay), and HTTP/SOCKS5 forward proxies
    (CONNECT). WS/gRPC transports cannot: the probe speaks plain TCP/TLS, so a
    ws-fronted TLS server answers the header handshake and then closes — a
    false positive documented in ADR-0006.

    Protocols with no implemented client (vmess, tuic, hysteria2, ssr) are
    rejected even on plain TCP: a hello-able socket is not a proxy, so there is
    no v1 fallback. Shadowsocks is additionally gated on the URI: only SIP004
    AEAD methods on a plain TCP conn (no plugin) can be relay-certified.
    """
    if node.protocol == "ss":
        try:
            method, _password, plugin = _ss_params(node)
        except ValueError:
            return False
        return plugin is None and method in _SS_AEAD_SPECS
    if node.protocol in _RELAY_PROTOCOLS or node.protocol in _FORWARD_PROTOCOLS:
        return _transport(node) in ("tcp", "")
    return False


def probe(node: Node, timeout_s: float) -> ProbeResult:
    started = time.monotonic()
    deadline = started + timeout_s
    try:
        if not probeable(node):
            return ProbeResult(
                node_id=node.node_id,
                alive=False,
                latency_ms=_latency_ms(started),
                error=_unprobeable_reason(node),
            )
        if node.protocol in _RELAY_PROTOCOLS:
            alive, error = _attempt_relay(node, deadline)
        elif node.protocol == "ss":
            alive, error = _attempt_ss_relay(node, deadline)
        else:
            alive, error = _attempt_forward(node, deadline)
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
        sent = _challenge_for(node.protocol, conf, node) + b"GET / HTTP/1.0\r\n\r\n"
        if conf.tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with ctx.wrap_socket(raw, server_hostname=conf.sni or node.server) as tls:
                tls.settimeout(_remaining(deadline))
                tls.sendall(sent)
                return _judge(node, tls, sent)
        raw.sendall(sent)
        return _judge(node, raw, sent)


def _judge(node: Node, conn: socket.socket, sent: bytes) -> tuple[bool, str | None]:
    reply = _recvn(conn, 2)
    if node.protocol == "vless" and reply != b"\x00\x00":
        return False, (
            f"expected vless reply b'\\x00\\x00', got {reply[:2]!r} "
            "(non-relay responder)"
        )
    got = reply + _recvn(conn, 14)
    if node.protocol == "trojan" and not got:
        return False, "closed before any relayed bytes (non-relay responder)"
    if sent.startswith(got):
        return False, (
            "server relayed our handshake bytes back (echo, non-relay responder)"
        )
    if not _is_successful_http(got):
        status = _http_status(got)
        return False, (
            f"peer answered {got[:24]!r} (status {status or 'unknown'}), not "
            "a relayed 2xx/3xx response from the target (canned-response honeypot)"
        )
    return True, None


_HTTP_STATUS_RE = re.compile(rb"HTTP/[0-9.]+ ([0-9]{3})")


def _http_status(data: bytes) -> int | None:
    match = _HTTP_STATUS_RE.search(data)
    return int(match.group(1)) if match else None


def _is_successful_http(data: bytes) -> bool:
    status = _http_status(data)
    return status is not None and 200 <= status < 400


@dataclasses.dataclass(frozen=True)
class _RelayConfig:
    tls: bool
    sni: str | None


def _transport(node: Node) -> str | None:
    u = urlparse(node.uri)
    if node.protocol == "vmess":
        # The vmess transport is not in the URI query: it lives inside the
        # base64 JSON payload as `net`. Base64 payloads may contain '/', which
        # urlparse would mangle, so strip the scheme and fragment by hand (the
        # same trap the renderer documents). A `?` form (uuid@host?net=ws)
        # puts it in the query like any other protocol.
        body = node.uri.removeprefix("vmess://").partition("#")[0]
        q = parse_qs(u.query)
        if "@" not in body:
            try:
                payload = _json.loads(_b64decode(body))
            except (ValueError, TypeError):
                return None
            net = payload.get("net") if isinstance(payload, dict) else None
            return (net or "tcp").lower()
        return (_first(q, "net") or "tcp").lower()
    return (_first(parse_qs(u.query), "type") or "tcp").lower()


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
        + b"\x00"  # addons length (1 byte, uint8; sing-box vless v0.2.0)
        + b"\x01"  # command: TCP connect
        + port.to_bytes(2, "big")  # target port, before the address
        + b"\x02"  # address type: FQDN (sing-box serializer: 0x02, not 0x03)
        + bytes([len(host)])
        + host
    )


def _trojan_challenge(node: Node, conf: _RelayConfig, host: bytes, port: int) -> bytes:
    u = urlparse(node.uri)
    password = unquote(u.username or "")
    sha_hex = hashlib.sha224(password.encode("utf-8")).hexdigest().encode("ascii")
    return (
        sha_hex
        + b"\x0d\x0a"
        + b"\x01"  # command: TCP connect
        + b"\x03"  # address type: FQDN (sing serializer: 0x03, not 0x02)
        + bytes([len(host)])
        + host
        + port.to_bytes(2, "big")
        + b"\x0d\x0a"
    )


# -- ss (SIP004 AEAD) relay handshake ------------------------------------------
#
# Wire framing mirrors sing-box shadowaead exactly:
#   client: random salt (keySaltLength), then chunks
#   chunk:  [AEAD(2-byte BE length)][tag][AEAD(payload)][tag]
#   subkey: HKDF-SHA1(master_key, salt, info="ss-subkey")
#   master: EVP_BytesToKey(MD5) over the password, key-length bytes
#   nonce:  little-endian counter, incremented after every AEAD op
#   target: SOCKS5 ATYP (0x03 FQDN) + 1-byte length + host + 2-byte BE port


def _unprobeable_reason(node: Node) -> str:
    transport = _transport(node)
    if node.protocol in _RELAY_PROTOCOLS or node.protocol in _FORWARD_PROTOCOLS:
        if transport not in ("tcp", ""):
            return (
                f"transport {transport!r} is not relay-probeable "
                "(only plain TCP/TLS can complete a relay round-trip)"
            )
    if node.protocol == "ss":
        try:
            method, _password, plugin = _ss_params(node)
        except ValueError:
            return f"cannot parse ss URI {node.uri[:40]!r} for relay probing"
        if plugin is not None:
            return (
                f"ss plugin {plugin!r} is not relay-probeable "
                "(the AEAD client cannot speak the plugin)"
            )
        supported = ", ".join(sorted(_SS_AEAD_SPECS))
        return (
            f"ss method {method!r} is not relay-probeable (only {supported} "
            "are SIP004 AEAD; stream and 2022-blake3 ciphers are not "
            "relay-certified)"
        )
    if transport not in ("tcp", ""):
        return (
            f"transport {transport!r} is not relay-probeable "
            "(only plain TCP/TLS can complete a relay round-trip)"
        )
    return (
        f"protocol {node.protocol!r} has no relay probe (client not "
        "implemented); not relay-probeable"
    )


def _ss_params(node: Node) -> tuple[str, str, str | None]:
    """Return (method, password, plugin) parsed from an ss:// URI.

    Accepts the same SIP002 shapes the renderer does (engine/tunnel/config.py):
    `method:password@host:port`, base64(`method:password`)@host:port, and the
    legacy all-base64 `base64(method:password@host:port)` form. Raises ValueError
    when there is no usable method:password pair.
    """
    body = node.uri.removeprefix("ss://").partition("#")[0]
    body, _, query = body.partition("?")
    query = unquote(query)
    plugin = query[7:] if query.startswith("plugin=") else None
    if "@" in body:
        userinfo, _, _ = body.partition("@")
        if ":" in userinfo:
            method, _, password = userinfo.partition(":")
        else:
            method, _, password = _b64decode(userinfo).partition(":")
    else:
        decoded = _b64decode(body)
        userinfo, _, _ = decoded.partition("@")
        if not userinfo:
            raise ValueError(f"node {node.node_id!r}: ss payload lacks userinfo")
        method, _, password = userinfo.partition(":")
    if not method or not password:
        raise ValueError(f"node {node.node_id!r}: ss userinfo lacks method:password")
    return unquote(method), unquote(password), plugin


def _ss_master_key(password: bytes, key_len: int) -> bytes:
    """EVP_BytesToKey(MD5) master key (sing-box's shadowaead `Key`).

    D_1 = MD5(password); D_i = MD5(D_{i-1} | password), concatenated to key_len.
    """
    out = bytearray()
    prev = b""
    while len(out) < key_len:
        prev = hashlib.md5(prev + password).digest()
        out.extend(prev)
    return bytes(out[:key_len])


def _ss_subkey(master: bytes, salt: bytes, length: int) -> bytes:
    """HKDF-SHA1 (RFC 5869) with info "ss-subkey", matching sing-box."""
    prk = hmac.new(salt, master, hashlib.sha1).digest()
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(
            prk, block + b"ss-subkey" + bytes([counter]), hashlib.sha1
        ).digest()
        out += block
        counter += 1
    return out[:length]


def _ss_inc_nonce(nonce: bytearray) -> None:
    for i in range(len(nonce)):
        nonce[i] = (nonce[i] + 1) & 0xFF
        if nonce[i]:
            return


def _ss_addr_header() -> bytes:
    return (
        b"\x03"  # ATYP: FQDN (SOCKS5 serializer, sing-box default)
        + bytes([len(_PATH_TARGET)])
        + _PATH_TARGET
        + _TARGET_PORT.to_bytes(2, "big")
    )


def _attempt_ss_relay(node: Node, deadline: float) -> tuple[bool, str | None]:
    method, password, _plugin = _ss_params(node)
    spec = _SS_AEAD_SPECS[method]  # probeable() already gated the method
    master = _ss_master_key(password.encode("utf-8"), spec.key_len)
    raw = socket.create_connection(
        (node.server, node.port), timeout=_remaining(deadline)
    )
    with raw:
        raw.settimeout(_remaining(deadline))
        salt = os.urandom(spec.salt_len)
        client = spec.aead(_ss_subkey(master, salt, spec.key_len))
        payload = _ss_addr_header() + b"GET / HTTP/1.0\r\n\r\n"
        nonce = bytearray(spec.nonce_len)
        framed = client.encrypt(bytes(nonce), len(payload).to_bytes(2, "big"), None)
        _ss_inc_nonce(nonce)
        framed += client.encrypt(bytes(nonce), payload, None)
        raw.sendall(salt + framed)

        server_salt = _recvn(raw, spec.salt_len)
        if len(server_salt) < spec.salt_len:
            return False, (
                "peer closed before its salt (wrong credentials or non-relay responder)"
            )
        server = spec.aead(_ss_subkey(master, server_salt, spec.key_len))
        nonce = bytearray(spec.nonce_len)
        try:
            head = _recvn(raw, 2 + _SS_TAG_LEN)
            if len(head) < 2 + _SS_TAG_LEN:
                return False, "peer closed before a length chunk (non-relay responder)"
            length = int.from_bytes(server.decrypt(bytes(nonce), head, None), "big")
            _ss_inc_nonce(nonce)
            if length == 0 or length > _SS_MAX_PACKET_SIZE:
                return False, f"peer chunk length {length} out of range"
            body = _recvn(raw, length + _SS_TAG_LEN)
            if len(body) < length + _SS_TAG_LEN:
                return False, "peer closed mid-chunk (non-relay responder)"
            relayed = server.decrypt(bytes(nonce), body, None)
        except InvalidTag:
            return False, "decrypt failed (wrong credentials or non-relay responder)"
    if payload.startswith(relayed):
        return False, (
            "server relayed our request bytes back (echo, non-relay responder)"
        )
    if not _is_successful_http(relayed):
        status = _http_status(relayed)
        return False, (
            f"peer answered {relayed[:24]!r} (status {status or 'unknown'}), not "
            "a relayed 2xx/3xx response from the target (canned-response honeypot)"
        )
    return True, None


def _recvn(sock: socket.socket, n: int) -> bytes:
    total = bytearray()
    while len(total) < n:
        chunk = sock.recv(n - len(total))
        if not chunk:
            return bytes(total)
        total.extend(chunk)
    return bytes(total)


# -- http / socks5 forward-proxy relay round-trips -----------------------------


def _attempt_forward(node: Node, deadline: float) -> tuple[bool, str | None]:
    raw = socket.create_connection(
        (node.server, node.port), timeout=_remaining(deadline)
    )
    with raw:
        raw.settimeout(_remaining(deadline))
        if node.protocol == "socks5":
            alive, error = _socks5_setup(raw, node)
            if not alive:
                return False, error
        else:
            alive, error = _http_connect_setup(raw, node)
            if not alive:
                return False, error
        sent = b"GET / HTTP/1.0\r\n\r\n"
        raw.sendall(sent)
        relayed = _recvn(raw, 16)
        if sent.startswith(relayed):
            return False, (
                "proxy relayed our request bytes back (echo, non-proxy responder)"
            )
        if not _is_successful_http(relayed):
            status = _http_status(relayed)
            return False, (
                f"peer answered {relayed[:24]!r} (status {status or 'unknown'}), "
                "not a relayed 2xx/3xx response from the target "
                "(canned-response honeypot)"
            )
        return True, None


def _http_connect_setup(raw: socket.socket, node: Node) -> tuple[bool, str | None]:
    target = _PATH_TARGET + b":" + str(_TARGET_PORT).encode("ascii")
    raw.sendall(b"CONNECT " + target + b" HTTP/1.1\r\nHost: " + target + b"\r\n\r\n")
    resp = _read_until(raw, b"\r\n\r\n")
    status = _http_status(resp)
    if status is None or not 200 <= status < 400:
        return False, f"http proxy rejected CONNECT with {resp[:40]!r}"
    return True, None


def _socks5_setup(raw: socket.socket, node: Node) -> tuple[bool, str | None]:
    raw.sendall(b"\x05\x01\x00")
    reply = _recvn(raw, 2)
    if reply != b"\x05\x00":
        return False, f"expected socks5 method selection b'\\x05\\x00', got {reply!r}"
    connect = (
        b"\x05\x01\x00\x03"
        + bytes([len(_PATH_TARGET)])
        + _PATH_TARGET
        + _TARGET_PORT.to_bytes(2, "big")
    )
    raw.sendall(connect)
    grant = _recvn(raw, 10)
    if grant[:2] != b"\x05\x00":
        return False, f"expected socks5 CONNECT grant, got {grant[:12]!r}"
    return True, None


def _read_until(conn: socket.socket, sentinel: bytes) -> bytes:
    buf = bytearray()
    while not buf.endswith(sentinel):
        chunk = conn.recv(256)
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _remaining(deadline: float) -> float:
    return max(0.001, deadline - time.monotonic())


def _latency_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _first(q: dict[str, list[str]], key: str) -> str | None:
    values = q.get(key)
    return values[0] if values else None


def _b64decode(data: str) -> str:
    """Decode a base64 payload, tolerating URL-safe alphabets and missing padding.

    Mirrors the renderer's decoder in engine/tunnel/config.py so the probe and
    the sing-box config agree on what a vmess payload means.
    """
    if "%" in data:
        data = unquote(data)
    padded = data + "=" * (-len(data) % 4)
    for alt in (None, b"-_"):
        try:
            raw = base64.b64decode(padded, altchars=alt, validate=False)
            return raw.decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            continue
    raise ValueError(f"invalid base64 payload {data[:16]!r}")
