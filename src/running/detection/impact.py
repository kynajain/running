"""Fall and accident detection from accelerometer data.

A fall is only reported when four conditions hold together: a hard impact, the
sharp deceleration preceding it, a change of orientation across the impact, and
a post-impact window with almost no movement. Vigorous exercise clears the first
two but never the stillness window; a dropped device is usually picked up well
inside it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from running.detection.models import ImpactEvent, MotionSample

IMPACT_G = 3.0
MIN_JERK_G_PER_S = 20.0
MIN_ORIENTATION_CHANGE_DEG = 60.0
STILLNESS_WINDOW = timedelta(seconds=7)
STILLNESS_TOLERANCE_G = 0.15
SETTLE = timedelta(seconds=0.5)
ORIENTATION_WINDOW = timedelta(seconds=2)
JERK_WINDOW = timedelta(seconds=0.3)
MAX_GAP = timedelta(seconds=1)
"""Largest tolerated hole in the stillness window; above it, coverage is unproven."""


def detect_impact(
    motion: Iterable[MotionSample],
    *,
    impact_g: float = IMPACT_G,
    min_jerk: float = MIN_JERK_G_PER_S,
    min_orientation_change_deg: float = MIN_ORIENTATION_CHANGE_DEG,
    stillness_window: timedelta = STILLNESS_WINDOW,
    stillness_tolerance_g: float = STILLNESS_TOLERANCE_G,
) -> ImpactEvent | None:
    """Return the first accelerometer pattern that satisfies every condition."""

    samples = sorted(motion, key=lambda sample: sample.timestamp)
    if len(samples) < 2:
        return None

    for index, candidate in enumerate(samples):
        if candidate.magnitude < impact_g:
            continue

        jerk = _peak_jerk(samples, index)
        if jerk < min_jerk:
            continue

        before = _mean_direction(
            _slice(
                samples,
                candidate.timestamp - SETTLE - ORIENTATION_WINDOW,
                candidate.timestamp - SETTLE,
            )
        )
        after = _mean_direction(
            _slice(
                samples,
                candidate.timestamp + SETTLE,
                candidate.timestamp + SETTLE + ORIENTATION_WINDOW,
            )
        )
        if before is None or after is None:
            continue
        change = _angle_deg(before, after)
        if change < min_orientation_change_deg:
            continue

        still_for = _stillness(
            samples,
            start=candidate.timestamp + SETTLE,
            limit=stillness_window,
            tolerance=stillness_tolerance_g,
        )
        if still_for is None:
            continue

        return ImpactEvent(
            at=candidate.timestamp,
            peak_g=candidate.magnitude,
            jerk_g_per_s=jerk,
            orientation_change_deg=change,
            still_for_s=still_for.total_seconds(),
        )

    return None


def _slice(
    samples: Sequence[MotionSample],
    start: datetime,
    end: datetime,
) -> list[MotionSample]:
    return [sample for sample in samples if start <= sample.timestamp <= end]


def _peak_jerk(samples: Sequence[MotionSample], index: int) -> float:
    peak = samples[index]
    jerk = 0.0
    for previous, current in zip(samples[:index], samples[1 : index + 1], strict=False):
        if current.timestamp < peak.timestamp - JERK_WINDOW:
            continue
        seconds = (current.timestamp - previous.timestamp).total_seconds()
        if seconds <= 0:
            continue
        jerk = max(jerk, abs(current.magnitude - previous.magnitude) / seconds)
    return jerk


def _mean_direction(samples: Sequence[MotionSample]) -> tuple[float, float, float] | None:
    """Average acceleration as a unit vector, which is dominated by gravity."""

    if not samples:
        return None
    x = sum(sample.x for sample in samples) / len(samples)
    y = sum(sample.y for sample in samples) / len(samples)
    z = sum(sample.z for sample in samples) / len(samples)
    norm = math.sqrt(x**2 + y**2 + z**2)
    if norm < 1e-6:
        return None
    return x / norm, y / norm, z / norm


def _angle_deg(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    dot = sum(left * right for left, right in zip(a, b, strict=True))
    return math.degrees(math.acos(min(max(dot, -1.0), 1.0)))


def _stillness(
    samples: Sequence[MotionSample],
    *,
    start: datetime,
    limit: timedelta,
    tolerance: float,
) -> timedelta | None:
    """Duration of near-zero motion from ``start``, or ``None`` if too short.

    The whole interval must be covered by readings that are all at rest.
    Coverage is judged by gaps rather than by a sample landing exactly on the
    deadline, which real timestamps rarely do.
    """

    deadline = start + limit
    window = _slice(samples, start, deadline)
    if not window:
        return None

    for sample in window:
        if abs(sample.magnitude - 1.0) > tolerance:
            return None

    edges = [start, *(sample.timestamp for sample in window), deadline]
    if any(later - earlier > MAX_GAP for earlier, later in zip(edges, edges[1:], strict=False)):
        return None
    return limit
