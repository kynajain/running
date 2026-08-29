"""Weighted fusion of the individual signal detectors."""

from __future__ import annotations

from collections.abc import Sequence

from running.detection.models import SignalScore, StressAssessment
from running.detection.signals import DetectionContext, SignalDetector, detectors

DEFAULT_CONFIDENCE_THRESHOLD = 0.6
MIN_CONTRIBUTING_SIGNALS = 2


def fuse(
    scores: Sequence[SignalScore],
    *,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    min_signals: int = MIN_CONTRIBUTING_SIGNALS,
) -> StressAssessment:
    """Combine per-signal scores into one confidence value.

    Weights are renormalised over the signals that actually reported, so a
    missing sensor lowers precision rather than silently dragging the score
    toward zero. Nothing fires on a single signal, and scores are deduplicated
    by name first so one sensor counted twice cannot stand in for two.
    """

    unique = list({score.name: score for score in scores}.values())
    if not unique:
        return StressAssessment(
            confidence=0.0, contributions=[], triggered=False, reason="no signals"
        )

    total_weight = sum(score.weight for score in unique)
    confidence = sum(score.score * score.weight for score in unique) / total_weight
    confidence = min(max(confidence, 0.0), 1.0)

    if len(unique) < min_signals:
        return StressAssessment(
            confidence=confidence,
            contributions=unique,
            triggered=False,
            reason=f"only {len(unique)} of {min_signals} required signals available",
        )

    triggered = confidence >= threshold
    reason = (
        f"confidence {confidence:.2f} >= threshold {threshold:.2f}"
        if triggered
        else f"confidence {confidence:.2f} below threshold {threshold:.2f}"
    )
    return StressAssessment(
        confidence=confidence,
        contributions=unique,
        triggered=triggered,
        reason=reason,
    )


def assess(
    context: DetectionContext,
    *,
    signals: Sequence[SignalDetector] | None = None,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    min_signals: int = MIN_CONTRIBUTING_SIGNALS,
) -> StressAssessment:
    active = list(signals) if signals is not None else detectors()
    scores = [score for score in (detector.score(context) for detector in active) if score]
    return fuse(scores, threshold=threshold, min_signals=min_signals)
