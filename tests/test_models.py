from datetime import UTC, datetime

import pytest

from running.models import HealthSample, Metric, TimeWindow


def test_models_normalize_aware_datetimes_to_utc() -> None:
    sample = HealthSample(
        metric=Metric.HEART_RATE,
        value=120,
        unit="count/min",
        start="2026-08-20T07:15:03+01:00",
        end="2026-08-20T07:15:04+01:00",
        source="watch",
    )
    assert sample.start.tzinfo == UTC


def test_models_reject_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        TimeWindow(
            start=datetime(2026, 1, 1),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )
