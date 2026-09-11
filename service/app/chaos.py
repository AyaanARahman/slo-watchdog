"""Runtime-settable failure injection.

Three knobs, deliberately: error rate, latency, and latency jitter. Between them
they can violate either of the two SLOs this project defines — availability and
latency — independently of each other, which is exactly what the harness needs in
order to assert that the right alert fires while the other one stays quiet.

Concurrency note: `ChaosConfig` is frozen and swapped wholesale under a lock, so a
request handler reads a single consistent snapshot with no synchronisation and can
never observe a half-applied update.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, replace
from threading import Lock

_MS_PER_SECOND = 1000.0


@dataclass(frozen=True, slots=True)
class ChaosConfig:
    """An immutable snapshot of the injected-failure settings."""

    error_rate: float = 0.0
    """Fraction of requests to fail with a 500, in [0.0, 1.0]."""

    latency_ms: int = 0
    """Base delay injected before the handler runs."""

    latency_jitter_ms: int = 0
    """Delay is drawn uniformly from latency_ms +/- this, clamped at zero."""

    def __post_init__(self) -> None:
        if not 0.0 <= self.error_rate <= 1.0:
            raise ValueError(f"error_rate must be in [0.0, 1.0], got {self.error_rate!r}")
        if self.latency_ms < 0:
            raise ValueError(f"latency_ms must be >= 0, got {self.latency_ms!r}")
        if self.latency_jitter_ms < 0:
            raise ValueError(f"latency_jitter_ms must be >= 0, got {self.latency_jitter_ms!r}")


class ChaosController:
    """Holds the current config and makes the per-request failure decisions.

    The RNG is injectable so tests can pin the draws instead of relying on
    statistical flakiness.
    """

    def __init__(self, config: ChaosConfig | None = None, rng: random.Random | None = None) -> None:
        self._config = config if config is not None else ChaosConfig()
        self._rng = rng if rng is not None else random.Random()
        self._lock = Lock()

    @property
    def config(self) -> ChaosConfig:
        """The current snapshot. Atomic: never a partially-applied update."""
        return self._config

    def update(
        self,
        *,
        error_rate: float | None = None,
        latency_ms: int | None = None,
        latency_jitter_ms: int | None = None,
    ) -> ChaosConfig:
        """Apply a partial update.

        Validation runs inside `replace` before the swap, so a rejected update
        leaves the previous config in place rather than half-applying.
        """
        changes: dict[str, float | int] = {}
        if error_rate is not None:
            changes["error_rate"] = error_rate
        if latency_ms is not None:
            changes["latency_ms"] = latency_ms
        if latency_jitter_ms is not None:
            changes["latency_jitter_ms"] = latency_jitter_ms

        with self._lock:
            candidate = replace(self._config, **changes)  # type: ignore[arg-type]
            self._config = candidate
        return candidate

    def reset(self) -> ChaosConfig:
        """Return the service to healthy."""
        with self._lock:
            self._config = ChaosConfig()
        return self._config

    def should_fail(self) -> bool:
        """True if this request should be failed with a 500.

        Strict `<` means error_rate=0.0 never fails (random() returns [0, 1))
        and error_rate=1.0 always does.
        """
        return self._rng.random() < self._config.error_rate

    def next_delay_seconds(self) -> float:
        """Draw this request's injected delay, in seconds."""
        cfg = self._config
        if cfg.latency_ms == 0 and cfg.latency_jitter_ms == 0:
            return 0.0
        low = cfg.latency_ms - cfg.latency_jitter_ms
        high = cfg.latency_ms + cfg.latency_jitter_ms
        drawn_ms = self._rng.uniform(low, high) if high > low else float(cfg.latency_ms)
        return max(0.0, drawn_ms) / _MS_PER_SECOND

    async def apply_latency(self) -> float:
        """Sleep for this request's injected delay. Returns the delay applied."""
        delay = self.next_delay_seconds()
        if delay > 0.0:
            await asyncio.sleep(delay)
        return delay


chaos = ChaosController()
"""Process-wide controller used by the app."""
