from datetime import UTC, datetime, timedelta

from running.detection.impact import detect_impact
from running.detection.models import MotionSample

START = datetime(2026, 1, 1, tzinfo=UTC)
HZ = 20


def at(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


def steady(
    seconds: float,
    duration: float,
    *,
    upright: bool,
    jitter: float = 0.0,
) -> list[MotionSample]:
    """Gravity along z when upright, along x when face-down on the ground."""

    out: list[MotionSample] = []
    steps = int(duration * HZ)
    for step in range(steps):
        offset = jitter if step % 2 else -jitter
        magnitude = 1.0 + offset
        out.append(
            MotionSample(
                timestamp=at(seconds + step / HZ),
                x=magnitude if not upright else 0.0,
                y=0.0,
                z=0.0 if not upright else magnitude,
            )
        )
    return out


def fall() -> list[MotionSample]:
    return [
        *steady(0, 3, upright=True, jitter=0.25),
        MotionSample(timestamp=at(3.0), x=0.0, y=0.0, z=0.1),
        MotionSample(timestamp=at(3.05), x=0.0, y=0.0, z=6.0),
        *steady(3.5, 8, upright=False),
    ]


def test_detects_fall_with_impact_orientation_change_and_stillness() -> None:
    event = detect_impact(fall())

    assert event is not None
    assert event.at == at(3.05)
    assert event.peak_g >= 3.0
    assert event.orientation_change_deg > 60
    assert event.still_for_s >= 5


def test_ignores_impact_when_movement_resumes() -> None:
    picked_up = [
        *steady(0, 3, upright=True, jitter=0.25),
        MotionSample(timestamp=at(3.0), x=0.0, y=0.0, z=0.1),
        MotionSample(timestamp=at(3.05), x=0.0, y=0.0, z=6.0),
        *steady(3.5, 8, upright=False, jitter=0.4),
    ]

    assert detect_impact(picked_up) is None


def test_ignores_impact_without_orientation_change() -> None:
    upright_after = [
        *steady(0, 3, upright=True, jitter=0.25),
        MotionSample(timestamp=at(3.0), x=0.0, y=0.0, z=0.1),
        MotionSample(timestamp=at(3.05), x=0.0, y=0.0, z=6.0),
        *steady(3.5, 8, upright=True),
    ]

    assert detect_impact(upright_after) is None


def test_ignores_hard_running_without_impact() -> None:
    strides = [
        MotionSample(
            timestamp=at(step / HZ),
            x=0.0,
            y=0.0,
            z=3.4 if step % 4 == 0 else 0.4,
        )
        for step in range(HZ * 20)
    ]

    assert detect_impact(strides) is None


def test_ignores_slow_settle_without_jerk() -> None:
    ramp = [
        MotionSample(timestamp=at(step / HZ), x=0.0, y=0.0, z=1.0 + step * 0.05)
        for step in range(HZ * 3)
    ]
    gentle = [*ramp, *steady(3.5, 8, upright=False)]

    assert detect_impact(gentle) is None


def test_stillness_window_must_be_covered() -> None:
    truncated = [
        *steady(0, 3, upright=True, jitter=0.25),
        MotionSample(timestamp=at(3.0), x=0.0, y=0.0, z=0.1),
        MotionSample(timestamp=at(3.05), x=0.0, y=0.0, z=6.0),
        *steady(3.5, 3, upright=False),
    ]

    assert detect_impact(truncated) is None


def test_empty_input() -> None:
    assert detect_impact([]) is None


def test_stillness_tolerates_sampling_jitter_around_the_deadline() -> None:
    """Readings that straddle the deadline without landing on it still count."""

    offset = [
        MotionSample(timestamp=at(3.53 + step * 0.07), x=1.0, y=0.0, z=0.0)
        for step in range(int(9 / 0.07))
    ]
    jittered = [
        *steady(0, 3, upright=True, jitter=0.25),
        MotionSample(timestamp=at(3.0), x=0.0, y=0.0, z=0.1),
        MotionSample(timestamp=at(3.05), x=0.0, y=0.0, z=6.0),
        *offset,
    ]

    event = detect_impact(jittered)

    assert event is not None
    assert event.still_for_s == 7.0


def test_stillness_rejects_a_hole_in_the_window() -> None:
    gapped = [
        *steady(0, 3, upright=True, jitter=0.25),
        MotionSample(timestamp=at(3.0), x=0.0, y=0.0, z=0.1),
        MotionSample(timestamp=at(3.05), x=0.0, y=0.0, z=6.0),
        *steady(3.5, 2, upright=False),
        *steady(8.0, 4, upright=False),
    ]

    assert detect_impact(gapped) is None


def test_recording_ending_just_before_the_deadline_is_not_a_fall() -> None:
    """Absent evidence through the window is not evidence of stillness."""

    truncated = [
        *steady(0, 3, upright=True, jitter=0.25),
        MotionSample(timestamp=at(3.0), x=0.0, y=0.0, z=0.1),
        MotionSample(timestamp=at(3.05), x=0.0, y=0.0, z=6.0),
        *[
            MotionSample(timestamp=at(3.53 + step * 0.07), x=1.0, y=0.0, z=0.0)
            for step in range(int((10.2 - 3.53) / 0.07))
        ],
    ]

    assert detect_impact(truncated) is None
