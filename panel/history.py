from __future__ import annotations

import threading


class SnapshotHistory:
    """Bounded rolling store of per-poll metric samples (chart series)."""

    def __init__(self, window_s: int = 7200, max_samples: int = 5000) -> None:
        self._window_s = window_s
        self._max_samples = max_samples
        self._lock = threading.Lock()
        self._samples: list[tuple[float, dict[str, float]]] = []

    def add(self, t: float, metrics: dict[str, float]) -> None:
        with self._lock:
            self._samples.append((t, metrics))
            cutoff = t - self._window_s
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.pop(0)
            if len(self._samples) > self._max_samples:
                del self._samples[: len(self._samples) - self._max_samples]

    def series(self) -> list[tuple[float, dict[str, float]]]:
        """Column-friendly view for charts: ordered (t, metrics) tuples."""
        with self._lock:
            return list(self._samples)

    def as_columns(self) -> dict[str, list[float]]:
        samples = self.series()
        columns: dict[str, list[float]] = {}
        for _t, metrics in samples:
            for name, value in metrics.items():
                columns.setdefault(name, []).append(value)
        if samples:
            columns["t"] = [t for t, _m in samples]
        return columns
