import math
from datetime import UTC, datetime, timedelta

import pytest

from running.detection.fusion import assess, fuse
from running.detection.models import ActivitySample, MotionSample, SignalScore
from running.detection.signals import (
    HEART_RATE_DETECTOR,
    HRV_DETECTOR,
    ActivityAdjustedDetector,
    DetectionContext,
    PhasicEDADetector,
    activity_series,
)
from running.models import HealthSample, Metric

START = datetime(2026, 1, 1, tzinfo=UTC)


def sample(metric: Metric, value: float, minutes: float, unit: str = "count/min") -> HealthSample:
    at = START + timedelta(minutes=minutes)
    return HealthSample(
        metric=metric,
        value=value,
        unit=unit,
        start=at,
        end=at,
        source="test",
    )


def activity(intensity: float, minutes: float) -> ActivitySample:
    return ActivitySample(timestamp=START + timedelta(minutes=minutes), intensity=intensity)


def history(
    metric: Metric,
    *,
    at_rest: float,
    per_intensity: float,
    latest: float,
    unit: str = "count/min",
) -> DetectionContext:
    """20 minutes of alternating easy/hard effort, then one latest observation."""

    samples: list[HealthSample] = []
    activities: list[ActivitySample] = []
    for minute in range(20):
        intensity = 0.2 if minute % 2 else 0.8
        samples.append(sample(metric, at_rest + per_intensity * intensity, minute, unit))
        activities.append(activity(intensity, minute))
    samples.append(sample(metric, latest, 20, unit))
    activities.append(activity(0.2, 20))
    return DetectionContext(now=START + timedelta(minutes=20), samples=samples, activity=activities)


def test_heart_rate_flags_excess_above_activity_expectation() -> None:
    context = history(Metric.HEART_RATE, at_rest=60, per_intensity=60, latest=110)
    score = HEART_RATE_DETECTOR.score(context)

    assert score is not None
    assert score.score > 0.8, score.detail


def test_heart_rate_ignores_high_absolute_value_explained_by_effort() -> None:
    context = history(Metric.HEART_RATE, at_rest=60, per_intensity=60, latest=72)
    hard = DetectionContext(
        now=context.now,
        samples=[*context.samples[:-1], sample(Metric.HEART_RATE, 168, 20)],
        activity=[*context.activity[:-1], activity(1.8, 20)],
    )

    score = HEART_RATE_DETECTOR.score(hard)

    assert score is not None
    assert score.score == 0.0, score.detail


def test_hrv_flags_suppression_steeper_than_exertion_predicts() -> None:
    context = history(Metric.HRV_SDNN, at_rest=60, per_intensity=-40, latest=18, unit="ms")
    score = HRV_DETECTOR.score(context)

    assert score is not None
    assert score.score > 0.5, score.detail


def test_detector_needs_enough_history() -> None:
    context = DetectionContext(
        now=START + timedelta(minutes=3),
        samples=[sample(Metric.HEART_RATE, 70 + minute, minute) for minute in range(4)],
        activity=[activity(0.3, minute) for minute in range(4)],
    )

    assert HEART_RATE_DETECTOR.score(context) is None


def test_detector_needs_matching_activity_data() -> None:
    context = DetectionContext(
        now=START + timedelta(minutes=20),
        samples=[sample(Metric.HEART_RATE, 70, minute) for minute in range(20)],
        activity=[],
    )

    assert HEART_RATE_DETECTOR.score(context) is None


def test_eda_scores_phasic_bursts_not_tonic_drift() -> None:
    detector = PhasicEDADetector()
    tonic = [
        sample(Metric.SKIN_CONDUCTANCE, 2.0 + 0.01 * step, step * 0.5, "uS") for step in range(10)
    ]
    bursty = [
        sample(Metric.SKIN_CONDUCTANCE, value, index * 0.1, "uS")
        for index, value in enumerate([2.0, 2.3, 2.1, 2.0, 2.4, 2.1, 2.0, 2.5, 2.1, 2.0])
    ]
    now = START + timedelta(minutes=5)

    slow = detector.score(DetectionContext(now=now, samples=tonic, activity=[]))
    fast = detector.score(DetectionContext(now=now, samples=bursty, activity=[]))

    assert slow is not None and fast is not None
    assert slow.score == 0.0
    assert fast.score > slow.score


def test_eda_dormant_without_a_sensor() -> None:
    context = history(Metric.HEART_RATE, at_rest=60, per_intensity=60, latest=110)

    assert PhasicEDADetector().score(context) is None


def test_activity_series_separates_still_from_moving() -> None:
    still = [
        MotionSample(timestamp=START + timedelta(seconds=step), x=0.0, y=0.0, z=1.0)
        for step in range(30)
    ]
    running = [
        MotionSample(
            timestamp=START + timedelta(seconds=step),
            x=0.0,
            y=0.0,
            z=1.0 + 0.6 * math.sin(step),
        )
        for step in range(30)
    ]

    assert activity_series(still)[0].intensity == 0.0
    assert activity_series(running)[0].intensity > 0.5


def test_fusion_never_fires_on_a_single_signal() -> None:
    lone = SignalScore(name="heart_rate_excess", score=1.0, weight=0.4, detail="")

    assessment = fuse([lone])

    assert assessment.confidence == 1.0
    assert not assessment.triggered
    assert "required signals" in assessment.reason


def test_fusion_renormalises_weights_over_reporting_signals() -> None:
    scores = [
        SignalScore(name="a", score=1.0, weight=0.4, detail=""),
        SignalScore(name="b", score=0.5, weight=0.4, detail=""),
    ]

    assessment = fuse(scores, threshold=0.7)

    assert assessment.confidence == pytest.approx(0.75)
    assert assessment.triggered


def test_fusion_with_no_signals() -> None:
    assessment = fuse([])

    assert assessment.confidence == 0.0
    assert not assessment.triggered


def test_assess_runs_registered_detectors() -> None:
    context = history(Metric.HEART_RATE, at_rest=60, per_intensity=60, latest=110)
    extra = ActivityAdjustedDetector(
        name="respiratory_excess",
        metric=Metric.HEART_RATE,
        weight=0.2,
        direction=1.0,
    )

    assessment = assess(context, signals=[HEART_RATE_DETECTOR, extra], threshold=0.5)

    assert {score.name for score in assessment.contributions} == {
        "heart_rate_excess",
        "respiratory_excess",
    }
    assert assessment.triggered
