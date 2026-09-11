"""Contract for the RED metrics.

Two things here are load-bearing for the rest of the project:

1. The histogram must have a bucket boundary at exactly 0.25, because the latency
   SLI is "fraction of requests served under 250ms" and Prometheus can answer that
   exactly only at a bucket edge. Anywhere else it's interpolation.
2. The `path` label must be the *route template*, not the requested URL, or
   `/api/v1/items/<uuid>` mints a new time series per item and cardinality grows
   without bound.

Both are asserted, not assumed.
"""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry

from app.metrics import (
    LATENCY_BUCKETS,
    SLO_LATENCY_THRESHOLD_SECONDS,
    UNMATCHED_PATH,
    Metrics,
    route_template,
)


@pytest.fixture
def metrics() -> Metrics:
    # A private registry per test: no global state, no duplicate-registration errors.
    return Metrics(registry=CollectorRegistry())


class TestBuckets:
    def test_slo_threshold_is_an_exact_bucket_boundary(self) -> None:
        # If this fails, the latency SLI silently degrades into an estimate.
        assert SLO_LATENCY_THRESHOLD_SECONDS in LATENCY_BUCKETS

    def test_buckets_are_sorted_and_unique(self) -> None:
        assert list(LATENCY_BUCKETS) == sorted(set(LATENCY_BUCKETS))

    def test_buckets_span_the_interesting_range(self) -> None:
        # Fine enough to resolve a healthy p50, coarse enough that an 800ms
        # latency scenario doesn't just pile into +Inf.
        assert LATENCY_BUCKETS[0] <= 0.005
        assert LATENCY_BUCKETS[-1] >= 1.0

    def test_bucket_count_is_bounded(self) -> None:
        # Every bucket is a time series per (method, path). Keep it cheap.
        assert len(LATENCY_BUCKETS) <= 12


class TestMetricFamilies:
    def test_exposes_both_required_families(self, metrics: Metrics) -> None:
        metrics.observe("GET", "/api/v1/items", 200, 0.01)
        body = metrics.render().decode()
        assert "http_requests_total" in body
        assert "http_request_duration_seconds_bucket" in body
        assert "http_request_duration_seconds_count" in body
        assert "http_request_duration_seconds_sum" in body

    def test_counter_is_labelled_by_method_path_status(self, metrics: Metrics) -> None:
        metrics.observe("POST", "/api/v1/items", 201, 0.02)
        assert (
            metrics.registry.get_sample_value(
                "http_requests_total",
                {"method": "POST", "path": "/api/v1/items", "status": "201"},
            )
            == 1.0
        )

    def test_histogram_is_not_labelled_by_status(self, metrics: Metrics) -> None:
        # Latency carries method and path only. Adding status would multiply the
        # series count by the number of status codes for no analytical gain.
        metrics.observe("GET", "/api/v1/items", 200, 0.01)
        assert (
            metrics.registry.get_sample_value(
                "http_request_duration_seconds_count",
                {"method": "GET", "path": "/api/v1/items"},
            )
            == 1.0
        )

    def test_status_is_the_full_three_digit_code(self, metrics: Metrics) -> None:
        # The availability SLI matches status=~"5..", so the label must be the
        # numeric code, not a class like "5xx".
        metrics.observe("GET", "/api/v1/items", 503, 0.01)
        assert (
            metrics.registry.get_sample_value(
                "http_requests_total",
                {"method": "GET", "path": "/api/v1/items", "status": "503"},
            )
            == 1.0
        )


class TestCounting:
    def test_counter_increments_per_observation(self, metrics: Metrics) -> None:
        for _ in range(7):
            metrics.observe("GET", "/api/v1/items", 200, 0.01)
        assert (
            metrics.registry.get_sample_value(
                "http_requests_total",
                {"method": "GET", "path": "/api/v1/items", "status": "200"},
            )
            == 7.0
        )

    def test_distinct_statuses_are_counted_separately(self, metrics: Metrics) -> None:
        for _ in range(3):
            metrics.observe("GET", "/api/v1/items", 200, 0.01)
        for _ in range(2):
            metrics.observe("GET", "/api/v1/items", 500, 0.01)

        def count(status: str) -> float | None:
            return metrics.registry.get_sample_value(
                "http_requests_total",
                {"method": "GET", "path": "/api/v1/items", "status": status},
            )

        assert count("200") == 3.0
        assert count("500") == 2.0

    def test_slow_request_is_excluded_from_the_slo_bucket(self, metrics: Metrics) -> None:
        metrics.observe("GET", "/api/v1/items", 200, 0.3)  # 300ms: over threshold

        def bucket(le: str) -> float | None:
            return metrics.registry.get_sample_value(
                "http_request_duration_seconds_bucket",
                {"method": "GET", "path": "/api/v1/items", "le": le},
            )

        assert bucket("0.25") == 0.0, "300ms must not count as under the 250ms threshold"
        assert bucket("0.5") == 1.0
        assert bucket("+Inf") == 1.0

    def test_fast_request_counts_as_within_slo(self, metrics: Metrics) -> None:
        metrics.observe("GET", "/api/v1/items", 200, 0.05)
        assert (
            metrics.registry.get_sample_value(
                "http_request_duration_seconds_bucket",
                {"method": "GET", "path": "/api/v1/items", "le": "0.25"},
            )
            == 1.0
        )

    def test_sum_accumulates_observed_seconds(self, metrics: Metrics) -> None:
        for d in (0.1, 0.2, 0.3):
            metrics.observe("GET", "/api/v1/items", 200, d)
        assert metrics.registry.get_sample_value(
            "http_request_duration_seconds_sum",
            {"method": "GET", "path": "/api/v1/items"},
        ) == pytest.approx(0.6)


class TestRouteTemplate:
    def test_uses_the_template_not_the_requested_url(self) -> None:
        # The cardinality guarantee. Ten thousand item ids must collapse to one series.
        class Route:
            path = "/api/v1/items/{item_id}"

        scope = {"route": Route(), "path": "/api/v1/items/9f2c-deadbeef"}
        assert route_template(scope) == "/api/v1/items/{item_id}"

    def test_unmatched_request_collapses_to_a_sentinel(self) -> None:
        # Otherwise a 404 scanner walking random URLs is a cardinality bomb.
        assert route_template({"path": "/wp-admin.php"}) == UNMATCHED_PATH

    def test_route_without_a_usable_path_falls_back(self) -> None:
        class Weird:
            path = ""

        assert route_template({"route": Weird(), "path": "/x"}) == UNMATCHED_PATH


class TestInFlight:
    def test_starts_at_zero(self, metrics: Metrics) -> None:
        assert metrics.registry.get_sample_value("http_requests_in_flight") == 0.0

    def test_tracks_concurrent_requests(self, metrics: Metrics) -> None:
        with metrics.track_in_flight():
            assert metrics.registry.get_sample_value("http_requests_in_flight") == 1.0
            with metrics.track_in_flight():
                assert metrics.registry.get_sample_value("http_requests_in_flight") == 2.0
            assert metrics.registry.get_sample_value("http_requests_in_flight") == 1.0
        assert metrics.registry.get_sample_value("http_requests_in_flight") == 0.0

    def test_decrements_even_when_the_handler_raises(self, metrics: Metrics) -> None:
        # A leaked gauge climbs forever and quietly poisons the dashboard.
        with pytest.raises(RuntimeError), metrics.track_in_flight():
            raise RuntimeError("handler blew up")
        assert metrics.registry.get_sample_value("http_requests_in_flight") == 0.0
