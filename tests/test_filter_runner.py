"""Offline tests for the liveness filter against local loopback sockets."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from engine.filter.probe import probe as real_probe
from engine.filter.runner import batch_probe
from engine.models import Node, ProbeResult


def make_node(node_id: str, port: int, protocol: str = "vless") -> Node:
    return Node(
        node_id=node_id,
        uri=f"{protocol}://u@127.0.0.1:{port}",
        protocol=protocol,
        server="127.0.0.1",
        port=port,
        user="u",
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


def test_probe_alive_on_open_listener() -> None:
    server = EchoServer()
    try:
        result = real_probe(make_node("live", server.port, "vless"), timeout_s=1.0)
    finally:
        server.close()
    assert result.alive is True
    assert result.error is None
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


def test_batch_probe_returns_a_result_for_every_node() -> None:
    echo = EchoServer()
    silent = SilentServer()
    refused = RefusedPort()
    try:
        nodes = [
            make_node("nd_live", echo.port, "vless"),
            make_node("nd_silent", silent.port, "vless"),
            make_node("nd_closed", refused.port, "trojan"),
            make_node("nd_plain", echo.port, "socks5"),
        ]
        results = batch_probe(nodes, batch_size=3, timeout_s=0.3)
    finally:
        echo.close()
        silent.close()
        refused.close()
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
