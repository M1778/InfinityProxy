"""Live checks for the built-in throughput target (ADR-0011).

Admission cannot certify a single node when the default throughput target stops
answering the probe's request shape with a large 2xx body — the exact failure
mode that left a fresh install with a zero-node pool. This test talks to the
real target on purpose.
"""

from __future__ import annotations

import socket
import time

from engine.config import Settings
from engine.filter.probe import _is_successful_http, _probe_request

_MIN_BODY_BYTES = 512 * 1024


def test_default_throughput_target_returns_a_large_2xx_body() -> None:
    settings = Settings()
    request = _probe_request(
        settings.throughput_path, settings.throughput_host.encode("ascii")
    )
    deadline = time.monotonic() + 25.0
    with socket.create_connection(
        (settings.throughput_host, settings.throughput_port), timeout=10.0
    ) as sock:
        sock.settimeout(10.0)
        sock.sendall(request)
        received = b""
        while len(received) < settings.throughput_sample_bytes:
            if time.monotonic() > deadline:
                break
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            received += chunk
    assert _is_successful_http(received), received[:64]
    assert len(received) >= _MIN_BODY_BYTES, (
        f"target returned only {len(received)} bytes of body"
    )
