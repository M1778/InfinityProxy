"""Offline tests for the liveness filter against local loopback sockets."""

from __future__ import annotations

import socket
import ssl
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

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
            threading.Thread(target=self._relay, args=(conn,), daemon=True).start()

    @staticmethod
    def _relay(conn: socket.socket) -> None:
        with conn:
            try:
                data = conn.recv(18)  # version + uuid + addons len (1 byte)
                if data and data[0] == 0 and len(data) >= 18:
                    conn.recv(5)  # command + port + addr type + host len
                    conn.sendall(b"\x00\x00" + b"HTTP/1.1 200 OK\r\n")
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self.sock.close()


class TrojanRelayEmulator:
    """TLS-wraps the connection and answers a valid trojan header with CRLF."""

    def __init__(self) -> None:
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(
            certfile=str(_FIXTURES / "cert.pem"),
            keyfile=str(_FIXTURES / "key.pem"),
        )
        self._ctx = ctx
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
                    tls.sendall(b"HTTP/1.1 200 OK\r\n")
        except (OSError, ssl.SSLError):
            pass

    def close(self) -> None:
        self._stop.set()
        self.sock.close()


def test_probe_vless_dead_on_echo_responder() -> None:
    server = EchoServer()
    try:
        result = real_probe(make_node("live", server.port, "vless"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is False
    assert result.latency_ms is not None


def test_probe_alive_on_plain_listener() -> None:
    server = EchoServer()
    try:
        result = real_probe(make_node("plain", server.port, "http"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is True
    assert result.error is None


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


def test_batch_probe_returns_a_result_for_every_node() -> None:
    vless = VlessRelayEmulator()
    silent = SilentServer()
    refused = RefusedPort()
    echo = EchoServer()
    try:
        nodes = [
            make_node("nd_live", vless.port, "vless"),
            make_node("nd_silent", silent.port, "vless"),
            make_node("nd_closed", refused.port, "trojan"),
            make_node("nd_plain", echo.port, "socks5"),
        ]
        results = batch_probe(nodes, batch_size=3, timeout_s=0.3)
    finally:
        vless.close()
        silent.close()
        refused.close()
        echo.close()
    assert set(results) == {"nd_live", "nd_silent", "nd_closed", "nd_plain"}
    assert all(isinstance(r, ProbeResult) for r in results.values())
    assert results["nd_live"].alive is True
    assert results["nd_plain"].alive is True
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
