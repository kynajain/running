"""Domain models for health data and workouts."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


class Metric(StrEnum):
    HEART_RATE = "heart_rate"
    HRV_SDNN = "hrv_sdnn"
    RESTING_HEART_RATE = "resting_heart_rate"
    RESPIRATORY_RATE = "respiratory_rate"
    ACTIVE_ENERGY = "active_energy"
    STRESS_SCORE = "stress_score"
    SKIN_CONDUCTANCE = "skin_conductance"


class HealthSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: Metric
    value: float
    unit: str
    start: datetime
    end: datetime
    source: str

    _validate_start = field_validator("start")(ensure_utc)
    _validate_end = field_validator("end")(ensure_utc)

    @model_validator(mode="after")
    def validate_range(self) -> "HealthSample":
        if self.start > self.end:
            raise ValueError("sample start must not be after end")
        return self


class GeoPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    elevation_m: float | None = None
    timestamp: datetime

    _validate_timestamp = field_validator("timestamp")(ensure_utc)


class WorkoutSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    activity: str
    start: datetime
    end: datetime
    distance_m: float
    route: list[GeoPoint] = Field(default_factory=list)
    samples: list[HealthSample] = Field(default_factory=list)

    _validate_start = field_validator("start")(ensure_utc)
    _validate_end = field_validator("end")(ensure_utc)

    @model_validator(mode="after")
    def validate_range(self) -> "WorkoutSession":
        if self.start > self.end:
            raise ValueError("workout start must not be after end")
        return self


class TimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime

    _validate_start = field_validator("start")(ensure_utc)
    _validate_end = field_validator("end")(ensure_utc)

    @model_validator(mode="after")
    def validate_range(self) -> "TimeWindow":
        if self.start >= self.end:
            raise ValueError("window start must be before end")
        return self
