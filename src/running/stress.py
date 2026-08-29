"""Heuristic stress scoring, not an Apple Health metric.

For each HRV/RHR observation, calculate a rolling baseline from the preceding
30 observations. The score blends the inverted z-score of ln(HRV) (70%) with
the z-score of resting heart rate (30%), then clamps the result to 0-100.
Early observations use their current value as a one-point baseline. Resting
heart rate observations more than 24 hours from an HRV reading are ignored
for that reading and the rolling RHR baseline mean is used instead.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from statistics import mean, pstdev

from running.models import HealthSample, Metric


def compute_stress_scores(samples: Iterable[HealthSample]) -> list[HealthSample]:
    relevant = [
        sample
        for sample in samples
        if sample.metric in {Metric.HRV_SDNN, Metric.RESTING_HEART_RATE}
    ]
    relevant.sort(key=lambda sample: sample.start)
    all_rhr = [sample for sample in relevant if sample.metric == Metric.RESTING_HEART_RATE]
    if not any(sample.metric == Metric.HRV_SDNN for sample in relevant) or not all_rhr:
        return []
    hrv_history: list[float] = []
    rhr_history: list[float] = []
    scores: list[HealthSample] = []
    for sample in relevant:
        if sample.metric == Metric.RESTING_HEART_RATE:
            rhr_history.append(sample.value)
            continue
        matching_rhr = min(
            all_rhr,
            key=lambda candidate: abs((candidate.start - sample.start).total_seconds()),
        )
        hrv_baseline = hrv_history[-30:] or [sample.value]
        rhr_baseline = rhr_history[-30:] or [matching_rhr.value]
        hrv_stress = (
            mean(math.log(value) for value in hrv_baseline) - math.log(max(sample.value, 0.001))
        ) / (pstdev([math.log(value) for value in hrv_baseline]) or 1.0)
        rhr_value = matching_rhr.value
        if abs((matching_rhr.start - sample.start).total_seconds()) > 24 * 60 * 60:
            rhr_value = mean(rhr_baseline)
        rhr_stress = (rhr_value - mean(rhr_baseline)) / (pstdev(rhr_baseline) or 1.0)
        score = max(0.0, min(100.0, 50.0 + 15.0 * (0.7 * hrv_stress + 0.3 * rhr_stress)))
        scores.append(
            HealthSample(
                metric=Metric.STRESS_SCORE,
                value=score,
                unit="score",
                start=sample.start,
                end=sample.end,
                source="Derived stress heuristic",
            )
        )
        hrv_history.append(sample.value)
    return scores
