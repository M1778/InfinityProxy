"""Web panel: trends + full control over the InfinityProxy engine.

Runs as its own loopback service (default port 8000) that crawls the engine's
control API, keeps a bounded rolling history, and streams it to the browser over
SSE. Inherits ADR-0003 localhost-only, unauthenticated semantics.
"""

from __future__ import annotations
