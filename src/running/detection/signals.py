"""Activity-adjusted signal detectors.

Every detector answers one question: is this signal further from its
*expected value for the current activity level* than normal noise explains?
Absolute values are never compared against a fixed cut-off, because a heart
rate of 165 means nothing without knowing whether the wearer is sitting or
sprinting.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from running.detection.models import ActivitySample, MotionSample, SignalScore
from running.models import HealthSample, Metric

MIN_HISTORY = 12
MATCH_TOLERANCE = timedelta(minutes=2)
Z_SATURATION = 3.0


@dataclass(frozen=True)
class DetectionContext:
    """Everything a detector may look at for a single assessment."""

    now: datetime
    samples: Sequence[HealthSample]
    activity: Sequence[ActivitySample]

    def series(self, metric: Metric) -> list[HealthSample]:
        return sorted(
            (sample for sample in self.samples if sample.metric is metric),
            key=lambda sample: sample.start,
        )

    def intensity_at(self, when: datetime) -> float | None:
        best: float | None = None
        closest = MATCH_TOLERANCE
        for sample in self.activity:
            delta = abs(sample.timestamp - when)
            if delta <= closest:
                closest = delta
                best = sample.intensity
        return best


class SignalDetector(Protocol):
    """A pluggable contributor to the fused confidence score.

    New sensors are added by implementing this protocol and registering the
    instance; the fusion model needs no changes.
    """

    name: str
    weight: float

    def score(self, context: DetectionContext) -> SignalScore | None:
        """Return a 0-1 score, or ``None`` when the signal has no usable data."""


_DETECTORS: dict[str, SignalDetector] = {}


def register_detector(detector: SignalDetector) -> SignalDetector:
    _DETECTORS[detector.name] = detector
    return detector


def detectors() -> list[SignalDetector]:
    return list(_DETECTORS.values())


def activity_series(
    motion: Iterable[MotionSample],
    *,
    window: timedelta = timedelta(seconds=30),
) -> list[ActivitySample]:
    """Reduce raw accelerometer data to a movement-intensity series.

    Intensity is the standard deviation of total acceleration over the window,
    which cancels gravity and static orientation. The 0.6 g divisor puts hard
    running near 1.0.
    """

    ordered = sorted(motion, key=lambda sample: sample.timestamp)
    if not ordered:
        return []

    out: list[ActivitySample] = []
    bucket: list[MotionSample] = []
    bucket_end = ordered[0].timestamp + window
    for sample in ordered:
        if sample.timestamp >= bucket_end:
            if bucket:
                out.append(_intensity(bucket))
            bucket = []
            while sample.timestamp >= bucket_end:
                bucket_end += window
        bucket.append(sample)
    if bucket:
        out.append(_intensity(bucket))
    return out


def _intensity(bucket: Sequence[MotionSample]) -> ActivitySample:
    magnitudes = [sample.magnitude for sample in bucket]
    mean = sum(magnitudes) / len(magnitudes)
    variance = sum((value - mean) ** 2 for value in magnitudes) / len(magnitudes)
    midpoint = bucket[len(bucket) // 2].timestamp
    return ActivitySample(timestamp=midpoint, intensity=math.sqrt(variance) / 0.6)


def _fit(pairs: Sequence[tuple[float, float]]) -> tuple[float, float, float] | None:
    """Least-squares fit of ``value ~ intensity``.

    Returns slope, intercept and residual standard deviation.
    """

    count = len(pairs)
    if count < MIN_HISTORY:
        return None

    mean_x = sum(x for x, _ in pairs) / count
    mean_y = sum(y for _, y in pairs) / count
    variance_x = sum((x - mean_x) ** 2 for x, _ in pairs)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    slope = covariance / variance_x if variance_x > 1e-9 else 0.0
    intercept = mean_y - slope * mean_x

    residuals = [y - (intercept + slope * x) for x, y in pairs]
    residual_variance = sum(value**2 for value in residuals) / (count - 2)
    return slope, intercept, math.sqrt(residual_variance)


@dataclass
class ActivityAdjustedDetector:
    """Flags deviation from the activity-conditioned expectation of a metric."""

    name: str
    metric: Metric
    weight: float
    direction: float
    """``+1`` flags values above expectation, ``-1`` flags suppression below it."""
    log_transform: bool = False
    min_residual_sd: float = 1.0

    def score(self, context: DetectionContext) -> SignalScore | None:
        series = context.series(self.metric)
        if not series:
            return None

        latest = series[-1]
        intensity = context.intensity_at(latest.start)
        if intensity is None:
            return None

        history: list[tuple[float, float]] = []
        for sample in series[:-1]:
            paired = context.intensity_at(sample.start)
            if paired is not None:
                history.append((paired, self._transform(sample.value)))

        fit = _fit(history)
        if fit is None:
            return None

        slope, intercept, residual_sd = fit
        expected = intercept + slope * intensity
        observed = self._transform(latest.value)
        z = self.direction * (observed - expected) / max(residual_sd, self.min_residual_sd)
        score = min(max(z / Z_SATURATION, 0.0), 1.0)
        return SignalScore(
            name=self.name,
            score=score,
            weight=self.weight,
            detail=(
                f"observed {latest.value:.1f}, expected {self._invert(expected):.1f} "
                f"at intensity {intensity:.2f} (z={z:+.2f})"
            ),
        )

    def _transform(self, value: float) -> float:
        return math.log(max(value, 1e-6)) if self.log_transform else value

    def _invert(self, value: float) -> float:
        return math.exp(value) if self.log_transform else value


@dataclass
class PhasicEDADetector:
    """Scores skin-conductance responses by burst rate, not tonic level.

    Tonic conductance climbs with thermoregulatory sweat during exercise, so it
    cannot separate stress from exertion. Phasic bursts — fast rises of small
    amplitude — can.

    No Apple device reports EDA and HealthKit has no type for it, so this
    detector stays dormant unless a third-party sensor supplies
    ``Metric.SKIN_CONDUCTANCE`` samples in microsiemens.
    """

    name: str = "eda_phasic"
    weight: float = 0.2
    lookback: timedelta = timedelta(minutes=5)
    min_rise_us_per_s: float = 0.02
    min_amplitude_us: float = 0.05
    saturation_per_minute: float = 1.0
    _minimum_samples: int = field(default=4, init=False)

    def score(self, context: DetectionContext) -> SignalScore | None:
        window = [
            sample
            for sample in context.series(Metric.SKIN_CONDUCTANCE)
            if sample.start >= context.now - self.lookback
        ]
        if len(window) < self._minimum_samples:
            return None

        bursts = 0
        trough = window[0].value
        rising = False
        counted = False
        for previous, current in zip(window, window[1:], strict=False):
            seconds = (current.start - previous.start).total_seconds()
            if seconds <= 0:
                continue
            rate = (current.value - previous.value) / seconds
            if rate >= self.min_rise_us_per_s:
                if not rising:
                    trough = previous.value
                    rising = True
                if not counted and current.value - trough >= self.min_amplitude_us:
                    bursts += 1
                    counted = True
            elif rate < 0:
                # A single monotonic rise is one response however far it goes;
                # only a decline can open the next one.
                rising = False
                counted = False

        minutes = max((window[-1].start - window[0].start).total_seconds() / 60, 1e-6)
        per_minute = bursts / minutes
        return SignalScore(
            name=self.name,
            score=min(per_minute / self.saturation_per_minute, 1.0),
            weight=self.weight,
            detail=f"{bursts} phasic bursts over {minutes:.1f} min",
        )


HEART_RATE_DETECTOR = register_detector(
    ActivityAdjustedDetector(
        name="heart_rate_excess",
        metric=Metric.HEART_RATE,
        weight=0.4,
        direction=1.0,
        min_residual_sd=2.0,
    )
)

HRV_DETECTOR = register_detector(
    ActivityAdjustedDetector(
        name="hrv_suppression",
        metric=Metric.HRV_SDNN,
        weight=0.4,
        direction=-1.0,
        log_transform=True,
        min_residual_sd=0.1,
    )
)

EDA_DETECTOR = register_detector(PhasicEDADetector())
