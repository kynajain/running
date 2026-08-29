import math
from datetime import UTC, datetime, timedelta

from running.connectors.synthetic import STADIUM_COORDINATES, SyntheticAppleHealthConnector
from running.models import HealthSample, Metric, TimeWindow
from running.stress import compute_stress_scores


async def test_synthetic_is_deterministic_and_near_stadium() -> None:
    window = TimeWindow(
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 2, tzinfo=UTC),
    )
    first = [workout async for workout in SyntheticAppleHealthConnector().fetch_workouts(window)]
    second = [workout async for workout in SyntheticAppleHealthConnector().fetch_workouts(window)]
    assert first == second
    assert all(
        math.hypot(
            (point.lat - STADIUM_COORDINATES[0]) * 111_000,
            (point.lon - STADIUM_COORDINATES[1]) * 111_000,
        )
        < 500
        for point in first[0].route
    )


def sample(metric: Metric, value: float, minute: int) -> HealthSample:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute)
    return HealthSample(
        metric=metric,
        value=value,
        unit="ms" if metric == Metric.HRV_SDNN else "count/min",
        start=timestamp,
        end=timestamp + timedelta(minutes=1),
        source="test",
    )


def test_stress_is_higher_for_low_hrv_and_high_rhr() -> None:
    samples = [
        sample(Metric.HRV_SDNN, 50, 0),
        sample(Metric.RESTING_HEART_RATE, 55, 0),
        sample(Metric.HRV_SDNN, 10, 1),
        sample(Metric.RESTING_HEART_RATE, 90, 1),
    ]
    scores = compute_stress_scores(samples)
    assert scores[1].value > scores[0].value
