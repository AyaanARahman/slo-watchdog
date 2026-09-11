"""End-to-end contract for the API surface.

These tests drive the real ASGI stack, so they cover the wiring the unit tests
can't: that chaos is actually applied to API routes, that the middleware records
what the SLI expects, and — importantly — that probes and the scrape endpoint stay
*out* of the SLI.
"""

from __future__ import annotations

from collections.abc import Iterator
from time import perf_counter

import pytest
from fastapi.testclient import TestClient

from app.chaos import ChaosConfig, chaos
from app.main import app
from app.metrics import metrics


@pytest.fixture(autouse=True)
def healthy_service() -> Iterator[None]:
    """Every test starts from a healthy service and leaves one behind."""
    chaos.reset()
    yield
    chaos.reset()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def request_count(method: str, path: str, status: str) -> float:
    """Current value of the request counter, treating 'absent' as zero."""
    value = metrics.registry.get_sample_value(
        "http_requests_total", {"method": method, "path": path, "status": status}
    )
    return value or 0.0


class TestProbes:
    def test_healthz_is_ok(self, client: TestClient) -> None:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_readyz_is_ok(self, client: TestClient) -> None:
        assert client.get("/readyz").status_code == 200

    def test_probes_are_immune_to_chaos(self, client: TestClient) -> None:
        # A liveness probe that fails under injected chaos would get the pod
        # killed mid-scenario and destroy the experiment.
        client.post("/admin/chaos", json={"error_rate": 1.0})
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200


class TestItems:
    def test_create_returns_201_with_an_id(self, client: TestClient) -> None:
        r = client.post("/api/v1/items", json={"name": "coffee", "quantity": 2})
        assert r.status_code == 201
        body = r.json()
        assert body["id"] and body["name"] == "coffee" and body["quantity"] == 2

    def test_created_item_is_retrievable(self, client: TestClient) -> None:
        item_id = client.post("/api/v1/items", json={"name": "tea"}).json()["id"]
        r = client.get(f"/api/v1/items/{item_id}")
        assert r.status_code == 200
        assert r.json()["name"] == "tea"

    def test_created_item_appears_in_the_listing(self, client: TestClient) -> None:
        item_id = client.post("/api/v1/items", json={"name": "listed"}).json()["id"]
        assert item_id in {item["id"] for item in client.get("/api/v1/items").json()}

    def test_quantity_defaults_to_one(self, client: TestClient) -> None:
        assert client.post("/api/v1/items", json={"name": "milk"}).json()["quantity"] == 1

    def test_missing_item_is_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/items/does-not-exist").status_code == 404

    def test_invalid_payload_is_422(self, client: TestClient) -> None:
        assert client.post("/api/v1/items", json={"quantity": 1}).status_code == 422


class TestChaosEndpoint:
    def test_get_reports_current_config(self, client: TestClient) -> None:
        assert client.get("/admin/chaos").json() == {
            "error_rate": 0.0,
            "latency_ms": 0,
            "latency_jitter_ms": 0,
        }

    def test_post_applies_and_echoes_the_config(self, client: TestClient) -> None:
        r = client.post("/admin/chaos", json={"error_rate": 0.5, "latency_ms": 20})
        assert r.status_code == 200
        assert r.json() == {"error_rate": 0.5, "latency_ms": 20, "latency_jitter_ms": 0}
        assert chaos.config.error_rate == 0.5

    def test_post_is_a_partial_update(self, client: TestClient) -> None:
        client.post("/admin/chaos", json={"latency_ms": 40})
        client.post("/admin/chaos", json={"error_rate": 0.25})
        assert chaos.config == ChaosConfig(error_rate=0.25, latency_ms=40)

    def test_delete_resets_to_healthy(self, client: TestClient) -> None:
        client.post("/admin/chaos", json={"error_rate": 1.0, "latency_ms": 99})
        assert client.delete("/admin/chaos").status_code == 200
        assert chaos.config == ChaosConfig()

    @pytest.mark.parametrize(
        "payload",
        [{"error_rate": 1.5}, {"error_rate": -0.1}, {"latency_ms": -1}, {"latency_jitter_ms": -1}],
    )
    def test_out_of_range_values_are_rejected(
        self, client: TestClient, payload: dict[str, float]
    ) -> None:
        assert client.post("/admin/chaos", json=payload).status_code == 422
        assert chaos.config == ChaosConfig(), "a rejected update must not take effect"


class TestChaosBehaviour:
    def test_full_error_rate_fails_every_api_request(self, client: TestClient) -> None:
        client.post("/admin/chaos", json={"error_rate": 1.0})
        assert all(client.get("/api/v1/items").status_code == 500 for _ in range(20))

    def test_zero_error_rate_fails_nothing(self, client: TestClient) -> None:
        assert all(client.get("/api/v1/items").status_code == 200 for _ in range(20))

    def test_partial_error_rate_is_roughly_honoured(self, client: TestClient) -> None:
        # The headline acceptance criterion: 30% injected produces ~30% 5xx.
        client.post("/admin/chaos", json={"error_rate": 0.3})
        failures = sum(client.get("/api/v1/items").status_code == 500 for _ in range(300))
        assert 0.22 <= failures / 300 <= 0.38

    def test_injected_latency_actually_delays_the_response(self, client: TestClient) -> None:
        client.post("/admin/chaos", json={"latency_ms": 150})
        started = perf_counter()
        client.get("/api/v1/items")
        assert perf_counter() - started >= 0.15

    def test_admin_endpoint_stays_reachable_under_total_failure(self, client: TestClient) -> None:
        # Otherwise the harness could inject 100% errors and never recover.
        client.post("/admin/chaos", json={"error_rate": 1.0})
        assert client.delete("/admin/chaos").status_code == 200


class TestMetricsEndpoint:
    def test_exposes_both_metric_families(self, client: TestClient) -> None:
        client.get("/api/v1/items")
        body = client.get("/metrics").text
        assert "http_requests_total" in body
        assert "http_request_duration_seconds_bucket" in body

    def test_uses_the_prometheus_content_type(self, client: TestClient) -> None:
        assert "text/plain" in client.get("/metrics").headers["content-type"]

    def test_successful_request_increments_the_counter(self, client: TestClient) -> None:
        before = request_count("GET", "/api/v1/items", "200")
        client.get("/api/v1/items")
        assert request_count("GET", "/api/v1/items", "200") == before + 1

    def test_injected_failure_is_recorded_as_500(self, client: TestClient) -> None:
        before = request_count("GET", "/api/v1/items", "500")
        client.post("/admin/chaos", json={"error_rate": 1.0})
        client.get("/api/v1/items")
        assert request_count("GET", "/api/v1/items", "500") == before + 1

    def test_path_label_is_the_route_template(self, client: TestClient) -> None:
        # The cardinality guarantee, end to end.
        item_id = client.post("/api/v1/items", json={"name": "x"}).json()["id"]
        before = request_count("GET", "/api/v1/items/{item_id}", "200")
        client.get(f"/api/v1/items/{item_id}")
        assert request_count("GET", "/api/v1/items/{item_id}", "200") == before + 1
        assert f"/api/v1/items/{item_id}" not in client.get("/metrics").text

    def test_probes_and_scrapes_are_excluded_from_the_sli(self, client: TestClient) -> None:
        # Probe and scrape traffic is a stream of guaranteed 200s. Counting it
        # would dilute the error budget and flatter the service.
        for path in ("/healthz", "/readyz", "/metrics"):
            client.get(path)
        body = client.get("/metrics").text
        for path in ("/healthz", "/readyz", "/metrics"):
            assert f'path="{path}"' not in body

    def test_the_harness_control_plane_is_excluded_from_the_sli(self, client: TestClient) -> None:
        # The harness sets up every scenario through /admin/chaos. Those calls must
        # not land in the window it is about to make assertions over.
        client.get("/admin/chaos")
        client.post("/admin/chaos", json={"error_rate": 0.1})
        client.delete("/admin/chaos")
        assert 'path="/admin/chaos"' not in client.get("/metrics").text
