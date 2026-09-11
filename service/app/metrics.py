"""RED metrics (Rate, Errors, Duration) and the ASGI middleware that records them.

Design notes that matter downstream:

* **Bucket boundaries.** The latency SLI asks "what fraction of requests completed
  in under 250ms?". A Prometheus histogram answers that *exactly* only if 0.25 is a
  bucket edge — `http_request_duration_seconds_bucket{le="0.25"}` is then a literal
  count of fast requests. Off a boundary you fall back to `histogram_quantile`,
  which interpolates linearly within a bucket, and the SLI becomes an estimate you
  are then paging someone on. See `docs/slo-rationale.md`.

* **Label cardinality.** `path` is the *route template* (`/api/v1/items/{item_id}`),
  never the requested URL. Labelling by URL would mint a new time series per item id
  and grow without bound. Unmatched requests collapse to a single sentinel so a 404
  scanner can't do the same thing.

* **What gets measured.** Only the API surface. The probe endpoints and `/metrics`
  itself are excluded: kubelet probes and Prometheus scrapes are a steady stream of
  guaranteed-200s that would dilute the availability SLI and make the error budget
  look healthier than the service actually is.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import generate_latest as _generate_latest

SLO_LATENCY_THRESHOLD_SECONDS = 0.25
"""The latency SLI threshold. Must appear in LATENCY_BUCKETS."""

LATENCY_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    SLO_LATENCY_THRESHOLD_SECONDS,  # <- the SLO boundary, named so it cannot drift
    0.5,
    1.0,
    2.5,
    5.0,
)

EXCLUDED_PATHS = frozenset({"/metrics", "/healthz", "/readyz"})
"""Operational endpoints, kept out of the SLI. See module docstring."""

UNMATCHED_PATH = "/__unmatched__"
"""Sentinel for requests that matched no route, so 404s can't inflate cardinality."""

CONTENT_TYPE = CONTENT_TYPE_LATEST
"""Exposition-format content type, re-exported so `main` has a single import site."""


class Metrics:
    """The metric families, bound to a registry.

    Takes its registry as an argument rather than reaching for the process-global
    default, so tests get a clean slate and never hit duplicate-registration errors.
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()

        self.requests_total = Counter(
            "http_requests_total",
            "Total HTTP requests served.",
            labelnames=("method", "path", "status"),
            registry=self.registry,
        )
        self.request_duration = Histogram(
            "http_request_duration_seconds",
            "HTTP request latency in seconds.",
            labelnames=("method", "path"),
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.in_flight = Gauge(
            "http_requests_in_flight",
            "Requests currently being served.",
            registry=self.registry,
        )

    def observe(self, method: str, path: str, status: int, duration_seconds: float) -> None:
        """Record one completed request."""
        self.requests_total.labels(method=method, path=path, status=str(status)).inc()
        self.request_duration.labels(method=method, path=path).observe(duration_seconds)

    @contextmanager
    def track_in_flight(self) -> Iterator[None]:
        """Hold the in-flight gauge up for the duration of a request.

        Decrements in a `finally` block: a handler that raises must not leak the
        gauge upward forever.
        """
        self.in_flight.inc()
        try:
            yield
        finally:
            self.in_flight.dec()

    def render(self) -> bytes:
        """The registry in Prometheus text exposition format."""
        return _generate_latest(self.registry)


def route_template(scope: dict[str, Any]) -> str:
    """The matched route's template, or a sentinel if nothing matched.

    Starlette attaches the matched `route` to the scope during routing, so this is
    only meaningful *after* the application has handled the request.
    """
    path = getattr(scope.get("route"), "path", None)
    if isinstance(path, str) and path:
        return path
    return UNMATCHED_PATH


class MetricsMiddleware:
    """Pure-ASGI middleware that times requests and records the outcome.

    Written against the raw ASGI interface rather than Starlette's
    `BaseHTTPMiddleware`, which buffers responses through an anyio stream and
    interferes with streaming responses and background tasks. Here the only thing
    wrapped is `send`, purely to observe the status code as it goes past.
    """

    def __init__(self, app: Any, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") in EXCLUDED_PATHS:
            await self.app(scope, receive, send)
            return

        # If the app raises before responding, the client sees a 500 — so that is
        # what the SLI must record.
        status = 500
        method: str = scope["method"]

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        started = perf_counter()
        with self.metrics.track_in_flight():
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                # In `finally` so a raised exception is still counted as a 500
                # rather than vanishing from the SLI entirely.
                self.metrics.observe(
                    method=method,
                    path=route_template(scope),
                    status=status,
                    duration_seconds=perf_counter() - started,
                )


metrics = Metrics()
"""Process-wide metrics used by the app."""
