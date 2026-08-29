"""Models for sensor fusion and accident detection."""

from __future__ import annotations

import math
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from running.models import ensure_utc


class MotionSample(BaseModel):
    """A single accelerometer reading, in g, including gravity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime
    x: float
    y: float
    z: float

    _validate_timestamp = field_validator("timestamp")(ensure_utc)

    @property
    def magnitude(self) -> float:
        return math.sqrt(self.x**2 + self.y**2 + self.z**2)


class ActivitySample(BaseModel):
    """Movement intensity derived from accelerometer variance.

    ``0`` is motionless; ``1`` is roughly hard running. Values above 1 are
    possible and are not clamped, so the regressions stay linear.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime
    intensity: float = Field(ge=0.0)

    _validate_timestamp = field_validator("timestamp")(ensure_utc)


class SignalScore(BaseModel):
    """One sensor's contribution to the fused confidence score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    score: float = Field(ge=0.0, le=1.0)
    weight: float = Field(gt=0.0)
    detail: str


class StressAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    confidence: float = Field(ge=0.0, le=1.0)
    contributions: list[SignalScore]
    triggered: bool
    reason: str


class ImpactEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    at: datetime
    peak_g: float
    jerk_g_per_s: float
    orientation_change_deg: float
    still_for_s: float

    _validate_at = field_validator("at")(ensure_utc)
