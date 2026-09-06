"""Offline-fragile HTTPS fetch of a source feed. Never raises."""

from __future__ import annotations

import requests

_CHUNK_SIZE = 65536


def fetch_text(url: str, timeout_s: float = 10.0) -> str | None:
    """Return the feed body as UTF-8 text, or None on any network/HTTP failure."""
    try:
        with requests.get(url, timeout=timeout_s, stream=True) as response:
            response.raise_for_status()
            chunks = [chunk for chunk in response.iter_content(chunk_size=_CHUNK_SIZE)]
    except (requests.RequestException, OSError):
        return None
    body = b"".join(chunks)
    return body.decode("utf-8", errors="replace")
