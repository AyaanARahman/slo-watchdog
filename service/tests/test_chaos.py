"""Contract for the chaos injector.

The whole project rests on this module: if failure injection isn't precise and
repeatable, the harness can't prove anything about the alerts. So the controller
takes an injectable RNG and the behaviour here is asserted deterministically
rather than statistically wherever that's possible.
"""

from __future__ import annotations

import random
from dataclasses import FrozenInstanceError

import pytest

from app.chaos import ChaosConfig, ChaosController


class TestChaosConfig:
    def test_defaults_are_a_healthy_service(self) -> None:
        c = ChaosConfig()
        assert c.error_rate == 0.0
        assert c.latency_ms == 0
        assert c.latency_jitter_ms == 0

    @pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0, -1.0])
    def test_error_rate_must_be_a_probability(self, bad: float) -> None:
        with pytest.raises(ValueError, match="error_rate"):
            ChaosConfig(error_rate=bad)

    @pytest.mark.parametrize("bad", [-1, -500])
    def test_latency_must_not_be_negative(self, bad: int) -> None:
        with pytest.raises(ValueError, match="latency_ms"):
            ChaosConfig(latency_ms=bad)

    def test_jitter_must_not_be_negative(self) -> None:
        with pytest.raises(ValueError, match="latency_jitter_ms"):
            ChaosConfig(latency_jitter_ms=-1)

    @pytest.mark.parametrize("ok", [0.0, 0.5, 1.0])
    def test_error_rate_boundaries_are_inclusive(self, ok: float) -> None:
        assert ChaosConfig(error_rate=ok).error_rate == ok

    def test_is_immutable(self) -> None:
        # Config is swapped wholesale under a lock rather than mutated in place,
        # so a request handler can read it without synchronising.
        with pytest.raises(FrozenInstanceError):
            ChaosConfig().error_rate = 0.5  # type: ignore[misc]


class TestShouldFail:
    def test_zero_error_rate_never_fails(self) -> None:
        ctl = ChaosController(rng=random.Random(1234))
        assert not any(ctl.should_fail() for _ in range(1000))

    def test_full_error_rate_always_fails(self) -> None:
        ctl = ChaosController(ChaosConfig(error_rate=1.0), rng=random.Random(1234))
        assert all(ctl.should_fail() for _ in range(1000))

    def test_draw_is_compared_strictly_against_the_threshold(self) -> None:
        # random() < error_rate, so a draw landing exactly on the rate must NOT fail.
        class FixedRNG(random.Random):
            def random(self) -> float:
                return 0.30

        ctl = ChaosController(ChaosConfig(error_rate=0.30), rng=FixedRNG())
        assert not ctl.should_fail()

        ctl.update(error_rate=0.31)
        assert ctl.should_fail()

    def test_rate_is_approximately_honoured_over_many_draws(self) -> None:
        ctl = ChaosController(ChaosConfig(error_rate=0.30), rng=random.Random(42))
        failures = sum(ctl.should_fail() for _ in range(10_000))
        assert 0.28 <= failures / 10_000 <= 0.32


class TestDelay:
    def test_no_latency_configured_means_no_delay(self) -> None:
        assert ChaosController(rng=random.Random(7)).next_delay_seconds() == 0.0

    def test_zero_jitter_gives_an_exact_delay(self) -> None:
        ctl = ChaosController(ChaosConfig(latency_ms=800), rng=random.Random(7))
        assert all(ctl.next_delay_seconds() == pytest.approx(0.8) for _ in range(100))

    def test_jitter_is_symmetric_about_the_base(self) -> None:
        ctl = ChaosController(
            ChaosConfig(latency_ms=100, latency_jitter_ms=25), rng=random.Random(7)
        )
        draws = [ctl.next_delay_seconds() for _ in range(2000)]
        assert all(0.075 <= d <= 0.125 for d in draws)
        assert min(draws) < 0.08 and max(draws) > 0.12  # actually spans the range
        assert sum(draws) / len(draws) == pytest.approx(0.100, abs=0.003)

    def test_jitter_wider_than_base_clamps_at_zero(self) -> None:
        # asyncio.sleep must never be handed a negative duration.
        ctl = ChaosController(
            ChaosConfig(latency_ms=10, latency_jitter_ms=50), rng=random.Random(7)
        )
        assert all(ctl.next_delay_seconds() >= 0.0 for _ in range(2000))


class TestUpdate:
    def test_partial_update_leaves_other_fields_alone(self) -> None:
        ctl = ChaosController(ChaosConfig(error_rate=0.1, latency_ms=50, latency_jitter_ms=5))
        ctl.update(error_rate=0.9)
        assert ctl.config == ChaosConfig(error_rate=0.9, latency_ms=50, latency_jitter_ms=5)

    def test_update_returns_the_new_config(self) -> None:
        assert ChaosController().update(latency_ms=250).latency_ms == 250

    def test_rejected_update_does_not_take_effect(self) -> None:
        ctl = ChaosController(ChaosConfig(error_rate=0.2))
        with pytest.raises(ValueError, match="error_rate"):
            ctl.update(error_rate=5.0)
        assert ctl.config.error_rate == 0.2

    def test_update_rejects_unknown_fields(self) -> None:
        with pytest.raises(TypeError):
            ChaosController().update(cpu_burn=True)  # type: ignore[call-arg]

    def test_reset_restores_a_healthy_service(self) -> None:
        ctl = ChaosController(ChaosConfig(error_rate=1.0, latency_ms=900))
        assert ctl.reset() == ChaosConfig()
        assert ctl.config == ChaosConfig()
