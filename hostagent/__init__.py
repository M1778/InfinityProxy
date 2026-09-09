"""Privileged host-side agent: desktop system proxy + TUN mode (ADR-0010).

Runs as an opt-in companion container (compose profile `host`) with the host
network namespace, the Docker socket, /dev/net/tun, and the desktop session
bus mounted in. The engine and the panel only ever talk to its loopback API.
"""

from __future__ import annotations
