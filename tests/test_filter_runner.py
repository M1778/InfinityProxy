"""Offline tests for the liveness filter against local loopback sockets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import socket
import ssl
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import (
    AESGCM,
    ChaCha20Poly1305,
)

from engine.filter.probe import probe as real_probe
from engine.filter.runner import batch_probe
from engine.models import Node, ProbeResult

_FIXTURES = Path(__file__).parent / "fixtures"
FAKE_UUID = "00000000-0000-0000-0000-000000000001"
_TLS_ALERT = b"\x15\x03\x03\x00\x02\x02\x28"
_HTTP_200 = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"


def make_node(node_id: str, port: int, protocol: str = "vless") -> Node:
    user = "u"
    if protocol == "vless":
        user = FAKE_UUID
    uri = f"{protocol}://{user}@127.0.0.1:{port}"
    return Node(
        node_id=node_id,
        uri=uri,
        protocol=protocol,
        server="127.0.0.1",
        port=port,
        user=user,
        source="test",
        first_seen_s=time.time(),
    )


class EchoServer:
    """Accepts loopback connections and echoes one byte back."""

    def __init__(self) -> None:
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _echo(conn: socket.socket) -> None:
        with conn:
            try:
                data = conn.recv(1)
                if data:
                    conn.sendall(data)
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self.sock.close()


class SilentServer:
    """Accepts connections but never reads from or answers them."""

    def __init__(self) -> None:
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()
        self._conns: list[socket.socket] = []

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self._conns.append(conn)

    def close(self) -> None:
        self._stop.set()
        self.sock.close()
        for conn in self._conns:
            conn.close()


class RefusedPort:
    """Holds a reserved loopback port with no listener, so connects are refused."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]

    def close(self) -> None:
        self.sock.close()


class Responder:
    """Sends fixed bytes on accept, without reading the client's probe."""

    def __init__(self, payload: bytes) -> None:
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._payload = payload
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._send, args=(conn,), daemon=True).start()

    def _send(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.sendall(self._payload)
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self.sock.close()


class VlessRelayEmulator:
    """Answers a well-formed VLESS CONNECT header with the 2-byte response."""

    def __init__(self, payload: bytes = b"\x00\x00" + b"HTTP/1.1 200 OK\r\n") -> None:
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._payload = payload
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._relay, args=(conn,), daemon=True).start()

    def _relay(self, conn: socket.socket) -> None:
        with conn:
            try:
                data = conn.recv(18)  # version + uuid + addons len (1 byte)
                if data and data[0] == 0 and len(data) >= 18:
                    conn.recv(5)  # command + port + addr type + host len
                    conn.sendall(self._payload)
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self.sock.close()


class TrojanRelayEmulator:
    """TLS-wraps the connection and answers a valid trojan header with CRLF."""

    def __init__(self, payload: bytes = b"HTTP/1.1 200 OK\r\n") -> None:
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(
            certfile=str(_FIXTURES / "cert.pem"),
            keyfile=str(_FIXTURES / "key.pem"),
        )
        self._ctx = ctx
        self._payload = payload
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._relay, args=(conn,), daemon=True).start()

    def _relay(self, conn: socket.socket) -> None:
        try:
            with self._ctx.wrap_socket(conn, server_side=True) as tls:
                data = tls.recv(2)
                if data and len(data) >= 2:
                    tls.sendall(self._payload)
        except (OSError, ssl.SSLError):
            pass

    def close(self) -> None:
        self._stop.set()
        self.sock.close()


def make_ss_node(
    node_id: str,
    port: int,
    method: str = "chacha20-ietf-poly1305",
    password: str = "probe-secret",
    suffix: str = "",
) -> Node:
    cred = (
        base64.urlsafe_b64encode(f"{method}:{password}".encode()).decode().rstrip("=")
    )
    uri = f"ss://{cred}@127.0.0.1:{port}{suffix}"
    return Node(
        node_id=node_id,
        uri=uri,
        protocol="ss",
        server="127.0.0.1",
        port=port,
        user=password,
        source="test",
        first_seen_s=time.time(),
    )


# Shadowsocks SIP004 AEAD primitives. These are a second implementation of the
# published wire format (RFC 5869 HKDF-SHA1 + sing-box shadowaead framing), kept
# in the test on purpose: the emulator is an independent server-side counterpart
# to engine/filter/probe.py, so an interop bug in either side fails these tests.
_SS_AEAD = {
    "aes-128-gcm": (16, 16, AESGCM),
    "aes-256-gcm": (32, 32, AESGCM),
    "chacha20-ietf-poly1305": (32, 32, ChaCha20Poly1305),
}


def _evp_master_key(password: bytes, size: int) -> bytes:
    out = bytearray()
    prev = b""
    while len(out) < size:
        prev = hashlib.md5(prev + password).digest()
        out += prev
    return bytes(out[:size])


def _hkdf_sha1(master: bytes, salt: bytes, size: int) -> bytes:
    prk = hmac.new(salt, master, hashlib.sha1).digest()
    out = b""
    block = b""
    counter = 1
    while len(out) < size:
        block = hmac.new(
            prk, block + b"ss-subkey" + bytes([counter]), hashlib.sha1
        ).digest()
        out += block
        counter += 1
    return out[:size]


def _bump_nonce(nonce: bytearray) -> None:
    for i in range(len(nonce)):
        nonce[i] = (nonce[i] + 1) & 0xFF
        if nonce[i]:
            return


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    total = bytearray()
    while len(total) < n:
        chunk = conn.recv(n - len(total))
        if not chunk:
            return bytes(total)
        total += chunk
    return bytes(total)


class SsRelayEmulator:
    """Answers a SIP004 AEAD ss client with a relayed target response.

    The emulator plays the sing-box server role: it reads the client salt and
    chunk, then replies with its own salt and a chunk of `payload`. Wrong
    credentials or malformed frames make it close quietly, like a real server.
    """

    def __init__(
        self,
        method: str = "chacha20-ietf-poly1305",
        password: str = "probe-secret",
        payload: bytes = _HTTP_200,
    ) -> None:
        key_len, salt_len, aead = _SS_AEAD[method]
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.method = method
        self.password = password
        self.key_len, self.salt_len, self._aead = key_len, salt_len, aead
        self._payload = payload
        self.last_target: tuple[bytes, int] | None = None
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._relay, args=(conn,), daemon=True).start()

    def _relay(self, conn: socket.socket) -> None:
        try:
            with conn:
                salt = _recv_exact(conn, self.salt_len)
                if len(salt) < self.salt_len:
                    return
                cipher = self._aead(
                    _hkdf_sha1(
                        _evp_master_key(self.password.encode(), self.key_len),
                        salt,
                        self.key_len,
                    )
                )
                nonce = bytearray(12)
                head = _recv_exact(conn, 2 + 16)
                if len(head) < 2 + 16:
                    return
                length = int.from_bytes(cipher.decrypt(bytes(nonce), head, None), "big")
                _bump_nonce(nonce)
                if length == 0 or length > 0x3FFF:
                    return
                body = _recv_exact(conn, length + 16)
                if len(body) < length + 16:
                    return
                decoded = cipher.decrypt(bytes(nonce), body, None)
                if decoded[:1] == b"\x03" and len(decoded) >= 3:
                    host_len = decoded[1]
                    fqdn = decoded[2 : 2 + host_len]
                    port = decoded[2 + host_len : 2 + host_len + 2]
                    self.last_target = (fqdn, int.from_bytes(port, "big"))
                resp_salt = os.urandom(self.salt_len)
                resp = self._aead(
                    _hkdf_sha1(
                        _evp_master_key(self.password.encode(), self.key_len),
                        resp_salt,
                        self.key_len,
                    )
                )
                nonce = bytearray(12)
                framed = resp.encrypt(
                    bytes(nonce), len(self._payload).to_bytes(2, "big"), None
                )
                _bump_nonce(nonce)
                framed += resp.encrypt(bytes(nonce), self._payload, None)
                conn.sendall(resp_salt + framed)
        except InvalidTag:
            pass
        except OSError:
            pass

    def close(self) -> None:
        self._stop.set()
        self.sock.close()


def _read_until(conn: socket.socket, sentinel: bytes) -> bytes:
    buf = bytearray()
    while not buf.endswith(sentinel):
        chunk = conn.recv(256)
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


class HttpRelayEmulator:
    """Answers an HTTP CONNECT with 200, then relays a canned target response."""

    def __init__(self, payload: bytes = _HTTP_200) -> None:
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._payload = payload
        self.last_target: bytes | None = None
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._relay, args=(conn,), daemon=True).start()

    def _relay(self, conn: socket.socket) -> None:
        with conn:
            try:
                request = _read_until(conn, b"\r\n\r\n")
                if not request.startswith(b"CONNECT "):
                    return
                self.last_target = request.split(b"\r\n", 1)[0]
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if conn.recv(64):  # the GET through the tunnel
                    conn.sendall(self._payload)
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self.sock.close()


class Socks5RelayEmulator:
    """Answers SOCKS5 negotiation + CONNECT, then relays a canned response."""

    def __init__(self, payload: bytes = _HTTP_200, grant: bytes | None = None) -> None:
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._payload = payload
        self._grant = grant or b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        self.last_target: tuple[bytes, int] | None = None
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._relay, args=(conn,), daemon=True).start()

    def _relay(self, conn: socket.socket) -> None:
        with conn:
            try:
                if conn.recv(3)[:1] != b"\x05":
                    return
                conn.sendall(b"\x05\x00")
                head = _recv_exact(conn, 4)
                if len(head) < 4 or head[3] != 0x03:
                    return
                host_len = _recv_exact(conn, 1)
                if not host_len:
                    return
                host = _recv_exact(conn, host_len[0])
                port = _recv_exact(conn, 2)
                if len(port) < 2:
                    return
                self.last_target = (host, int.from_bytes(port, "big"))
                conn.sendall(self._grant)
                if conn.recv(64):  # the GET through the tunnel
                    conn.sendall(self._payload)
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self.sock.close()


def _canned_nginx_400(payload: bytes) -> bytes:
    return payload + b"HTTP/1.1 400 Bad Request\r\nServer: nginx/1.25.2\r\n\r\n"


def test_probe_vless_dead_on_echo_responder() -> None:
    server = EchoServer()
    try:
        result = real_probe(make_node("live", server.port, "vless"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is False
    assert result.latency_ms is not None


def test_probe_http_alive_on_relay_emulator() -> None:
    server = HttpRelayEmulator()
    try:
        result = real_probe(make_node("ph", server.port, "http"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is True
    assert result.error is None
    assert server.last_target is not None
    assert b"www.google.com:80" in server.last_target


def test_probe_http_dead_on_echo_responder() -> None:
    server = EchoServer()
    try:
        result = real_probe(make_node("eh", server.port, "http"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is False


def test_probe_http_dead_on_canned_400_after_connect() -> None:
    # The proxy grants CONNECT but answers the tunneled GET with its own 400:
    # not a target response, so the honeypot must not certify.
    server = HttpRelayEmulator(payload=_canned_nginx_400(b""))
    try:
        result = real_probe(make_node("rh", server.port, "http"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is False
    assert "HTTP" in (result.error or "")


def test_probe_socks5_alive_on_relay_emulator() -> None:
    server = Socks5RelayEmulator()
    try:
        result = real_probe(make_node("ps", server.port, "socks5"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is True
    assert result.error is None
    assert server.last_target == (b"www.google.com", 80)


def test_probe_socks5_dead_on_echo_responder() -> None:
    server = EchoServer()
    try:
        result = real_probe(make_node("es", server.port, "socks5"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is False


def test_probe_socks5_dead_on_grant_refusal() -> None:
    # RFC 1928 CONNECT failure reply (VER REP, code 0x01 general failure).
    server = Socks5RelayEmulator(grant=b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
    try:
        result = real_probe(make_node("gs", server.port, "socks5"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is False
    assert "CONNECT grant" in (result.error or "")


def test_probe_vmess_demoted_without_relay_client() -> None:
    node = make_node("vm", 10001, "vmess")
    result = real_probe(node, timeout_s=1.0)
    assert result.alive is False
    assert "relay-probeable" in (result.error or "")


def test_probe_tuic_and_hysteria2_demoted_without_relay_client() -> None:
    for proto in ("tuic", "hysteria2"):
        node = make_node(proto, 10001, proto)
        result = real_probe(node, timeout_s=1.0)
        assert result.alive is False
        assert "relay-probeable" in (result.error or "")


def test_probe_dead_on_closed_port() -> None:
    port = RefusedPort()
    try:
        result = real_probe(make_node("closed", port.port, "vless"), timeout_s=1.0)
    finally:
        port.close()
    assert result.alive is False
    assert result.error
    assert result.latency_ms is not None


def test_probe_dead_with_timeout_on_silent_listener() -> None:
    server = SilentServer()
    try:
        result = real_probe(make_node("silent", server.port, "vless"), timeout_s=0.3)
    finally:
        server.close()
    assert result.alive is False
    assert result.error == "timeout"
    assert result.latency_ms is not None and result.latency_ms >= 200


def test_probe_vless_alive_on_relay_emulator() -> None:
    server = VlessRelayEmulator()
    try:
        result = real_probe(make_node("rv", server.port, "vless"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is True
    assert result.error is None


def test_probe_relay_rejects_ws_transport() -> None:
    server = VlessRelayEmulator()
    try:
        node = make_node("wsv", server.port, "vless")
        node = replace(node, uri=node.uri + "?type=ws")
        result = real_probe(node, timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is False
    assert "ws" in (result.error or "")
    assert result.error and "relay-probeable" in result.error


def test_probe_vmess_url_form_rejects_ws_transport() -> None:
    node = make_node("wsv2", 10001, "vmess")
    node = replace(node, uri=node.uri + "?net=ws")
    result = real_probe(node, timeout_s=1.0)
    assert result.alive is False
    assert "ws" in (result.error or "")
    assert result.error and "relay-probeable" in result.error


def test_probe_vmess_json_payload_rejects_ws_transport() -> None:
    import base64
    import json

    payload = json.dumps(
        {
            "add": "127.0.0.1",
            "port": 10001,
            "id": FAKE_UUID,
            "aid": 0,
            "net": "ws",
            "path": "/probe",
        }
    ).encode()
    uri = "vmess://" + base64.urlsafe_b64encode(payload).decode().rstrip("=")
    node = make_node("wsv3", 10001, "vmess")
    node = replace(node, uri=uri)
    assert not real_probe(node, timeout_s=1.0).alive


def test_probe_vless_dead_on_http_responder() -> None:
    server = Responder(_HTTP_200)
    try:
        result = real_probe(make_node("hv", server.port, "vless"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is False
    assert "non-relay" in (result.error or "")


def test_probe_vless_dead_on_tls_alert_responder() -> None:
    server = Responder(_TLS_ALERT)
    try:
        result = real_probe(make_node("tv", server.port, "vless"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is False
    assert "non-relay" in (result.error or "")


def test_probe_trojan_alive_on_relay_emulator() -> None:
    server = TrojanRelayEmulator()
    try:
        result = real_probe(make_node("rt", server.port, "trojan"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is True
    assert result.error is None


def test_probe_trojan_dead_on_plain_http_responder() -> None:
    server = Responder(_HTTP_200)
    try:
        # A trojan probe must TLS-handshake first; the responder cannot,
        # so the probe dies at the TLS layer (no cert, plaintext server).
        result = real_probe(make_node("ht", server.port, "trojan"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is False
    assert result.error


def test_probe_trojan_dead_on_canned_http_400_responder() -> None:
    # A peer that answers the relay CONNECT with its own canned HTTP error is a
    # honeypot: it is not relaying to the requested target. Its reply must not
    # certify it as alive even though a full handshake completed.
    server = TrojanRelayEmulator(payload=_canned_nginx_400(b""))
    try:
        result = real_probe(make_node("ht", server.port, "trojan"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is False
    assert "HTTP" in (result.error or "")


def test_probe_vless_dead_on_canned_http_400_responder() -> None:
    # Same honeypot class over VLESS: the relay ack (0x00 0x00) completes but
    # the "relayed" bytes are the peer's own canned HTTP error, not a target.
    server = VlessRelayEmulator(payload=_canned_nginx_400(b"\x00\x00"))
    try:
        result = real_probe(make_node("hv", server.port, "vless"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is False
    assert "HTTP" in (result.error or "")


def test_probe_ss_alive_on_aead_relay_emulator() -> None:
    server = SsRelayEmulator()
    try:
        result = real_probe(make_ss_node("rss", server.port), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is True
    assert result.error is None
    assert server.last_target == (b"www.google.com", 80)


def test_probe_ss_alive_aes_256_gcm_flow() -> None:
    server = SsRelayEmulator(method="aes-256-gcm")
    try:
        result = real_probe(
            make_ss_node("rssaes", server.port, "aes-256-gcm"), timeout_s=1.0
        )
    finally:
        server.close()
    assert result.alive is True
    assert result.error is None


def test_probe_ss_alive_on_legacy_uri_form() -> None:
    server = SsRelayEmulator()
    try:
        legacy = (
            base64.urlsafe_b64encode(
                b"chacha20-ietf-poly1305:probe-secret@127.0.0.1:"
                + str(server.port).encode()
            )
            .decode()
            .rstrip("=")
        )
        node = Node(
            node_id="leg",
            uri=f"ss://{legacy}",
            protocol="ss",
            server="127.0.0.1",
            port=server.port,
            user="probe-secret",
            source="test",
            first_seen_s=time.time(),
        )
        result = real_probe(node, timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is True
    assert result.error is None


def test_probe_ss_dead_on_canned_http_400_responder() -> None:
    # A peer that answers the AEAD handshake with its own canned HTTP error is a
    # honeypot: it is not relaying to the requested target and must not certify.
    server = SsRelayEmulator(payload=_canned_nginx_400(b""))
    try:
        result = real_probe(make_ss_node("hss", server.port), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is False
    assert "HTTP" in (result.error or "")


def test_probe_ss_dead_on_wrong_password() -> None:
    server = SsRelayEmulator(password="other-secret")
    try:
        result = real_probe(make_ss_node("wp", server.port), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is False
    assert result.error


def test_probe_ss_dead_on_plain_http_responder() -> None:
    server = Responder(_HTTP_200)
    try:
        result = real_probe(make_ss_node("httpss", server.port), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is False


def test_probe_ss_dead_on_echo_responder() -> None:
    server = EchoServer()
    try:
        result = real_probe(make_ss_node("echoss", server.port), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is False


def test_probe_ss_rejects_stream_cipher() -> None:
    result = real_probe(make_ss_node("stream", 10001, "aes-256-cfb"), timeout_s=1.0)
    assert result.alive is False
    assert "aes-256-cfb" in (result.error or "")
    assert "relay-probeable" in (result.error or "")


def test_probe_ss_rejects_2022_blake3_cipher() -> None:
    result = real_probe(
        make_ss_node("b3", 10001, "2022-blake3-aes-256-gcm"), timeout_s=1.0
    )
    assert result.alive is False
    assert "relay-probeable" in (result.error or "")


def test_probe_ss_rejects_plugin() -> None:
    result = real_probe(
        make_ss_node("plug", 10001, suffix="?plugin=obfs-local"), timeout_s=1.0
    )
    assert result.alive is False
    assert "plugin" in (result.error or "")


def test_probe_ss_rejects_garble_uri() -> None:
    node = make_ss_node("baduri", 10001)
    node = replace(node, uri="ss://not-a-real-uri")
    result = real_probe(node, timeout_s=1.0)
    assert result.alive is False
    assert "cannot parse" in (result.error or "")


def test_batch_probe_returns_a_result_for_every_node() -> None:
    vless = VlessRelayEmulator()
    silent = SilentServer()
    refused = RefusedPort()
    socks5 = Socks5RelayEmulator()
    ss = SsRelayEmulator()
    try:
        nodes = [
            make_node("nd_live", vless.port, "vless"),
            make_node("nd_silent", silent.port, "vless"),
            make_node("nd_closed", refused.port, "trojan"),
            make_node("nd_plain", socks5.port, "socks5"),
            make_ss_node("nd_ss", ss.port),
        ]
        results = batch_probe(nodes, batch_size=3, timeout_s=0.3)
    finally:
        vless.close()
        silent.close()
        refused.close()
        socks5.close()
        ss.close()
    assert set(results) == {"nd_live", "nd_silent", "nd_closed", "nd_plain", "nd_ss"}
    assert all(isinstance(r, ProbeResult) for r in results.values())
    assert results["nd_live"].alive is True
    assert results["nd_plain"].alive is True
    assert results["nd_ss"].alive is True
    assert results["nd_closed"].alive is False
    assert results["nd_closed"].error
    assert results["nd_silent"].alive is False
    assert results["nd_silent"].error == "timeout"


def _run_with_counter(
    monkeypatch, nodes, *, batch_size, max_workers=None, timeout_s=1.0
) -> int:
    peak = 0
    in_flight = 0
    lock = threading.Lock()

    def counting_probe(node: Node, timeout_s: float) -> ProbeResult:
        nonlocal peak, in_flight
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            time.sleep(0.05)
            return real_probe(node, timeout_s)
        finally:
            with lock:
                in_flight -= 1

    monkeypatch.setattr("engine.filter.runner.probe", counting_probe)
    batch_probe(
        nodes, batch_size=batch_size, max_workers=max_workers, timeout_s=timeout_s
    )
    return peak


def test_batch_probe_never_runs_more_than_batch_size_concurrently(monkeypatch) -> None:
    server = EchoServer()
    try:
        nodes = [make_node(f"nd_{i}", server.port) for i in range(7)]
        assert _run_with_counter(monkeypatch, nodes, batch_size=2) == 2
        assert _run_with_counter(monkeypatch, nodes, batch_size=2, max_workers=10) == 2
        assert _run_with_counter(monkeypatch, nodes, batch_size=2, max_workers=1) == 1
    finally:
        server.close()


def test_batch_probe_never_raises_on_probe_failure(monkeypatch) -> None:
    node = make_node("nd_boom", 9)

    def boom(node: Node, timeout_s: float) -> ProbeResult:
        raise RuntimeError("boom")

    monkeypatch.setattr("engine.filter.runner.probe", boom)
    results = batch_probe([node], batch_size=1)
    assert results["nd_boom"].alive is False
    assert "boom" in (results["nd_boom"].error or "")


def test_batch_probe_empty_input() -> None:
    assert batch_probe([]) == {}


def test_batch_probe_fires_on_batch_callback_per_chunk(monkeypatch) -> None:
    echo = EchoServer()
    batches: list[list[str]] = []

    def recorder(chunk: dict[str, ProbeResult]) -> None:
        batches.append(sorted(chunk))

    try:
        nodes = [make_node(f"nd_{i}", echo.port) for i in range(5)]
        results = batch_probe(nodes, batch_size=2, timeout_s=0.5, on_batch=recorder)
    finally:
        echo.close()

    assert len(batches) == 3
    assert batches == [
        ["nd_0", "nd_1"],
        ["nd_2", "nd_3"],
        ["nd_4"],
    ]
    assert set(results) == {f"nd_{i}" for i in range(5)}


def test_batch_probe_rejects_nonpositive_batch_size() -> None:
    with pytest.raises(ValueError):
        batch_probe([make_node("nd_0", 9)], batch_size=0)
