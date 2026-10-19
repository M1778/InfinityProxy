"""Offline-fragile HTTPS fetch of a source feed. Never raises."""

from __future__ import annotations

import random
import time

import requests

_CHUNK_SIZE = 65536
_MAX_BYTES = 32 * 1024 * 1024
_RETRY_SLEEP_S = (1.0, 3.0)


def fetch_text(
    url: str,
    timeout_s: float = 10.0,
    max_bytes: int = _MAX_BYTES,
    attempt_wait_s: tuple[float, float] = _RETRY_SLEEP_S,
) -> str | None:
    """Return the feed body as UTF-8 text, or None on any network/HTTP failure.

    Two attempts: transient TLS resets and 5xx hiccups are the common case for
    raw.githubusercontent feeds. A body larger than max_bytes is treated as
    unusable rather than read whole into memory.
    """
    last_exc: Exception | None = None
    for attempt in range(2):
        if attempt:
            time.sleep(random.uniform(*attempt_wait_s))
        try:
            with requests.get(url, timeout=timeout_s, stream=True) as response:
                response.raise_for_status()
                chunks = []
                received = 0
                for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                    received += len(chunk)
                    if received > max_bytes:
                        return None
                    chunks.append(chunk)
        except (requests.RequestException, OSError) as exc:
            last_exc = exc
            continue
        return b"".join(chunks).decode("utf-8", errors="replace")
    assert last_exc is not None
    return None
