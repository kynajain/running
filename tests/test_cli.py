from datetime import timedelta

import pytest

from running.cli import _duration


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("90m", timedelta(minutes=90)),
        ("12h", timedelta(hours=12)),
        ("7d", timedelta(days=7)),
    ],
)
def test_duration_accepts_minutes_hours_and_days(value: str, expected: timedelta) -> None:
    assert _duration(value) == expected
