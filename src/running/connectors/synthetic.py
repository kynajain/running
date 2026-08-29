"""Deterministic synthetic Apple Health data for development and demos."""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from running.connectors.base import register_connector
from running.models import GeoPoint, HealthSample, Metric, TimeWindow, WorkoutSession

STADIUM_COORDINATES = (51.5387, -0.0166)
_EARTH_RADIUS_M = 6_371_000.0
_TRACK_RADIUS_M = 63.66


def _offset_point(north_m: float, east_m: float, timestamp: datetime) -> GeoPoint:
    lat, lon = STADIUM_COORDINATES
    lat_offset = math.degrees(north_m / _EARTH_RADIUS_M)
    lon_offset = math.degrees(east_m / (_EARTH_RADIUS_M * math.cos(math.radians(lat))))
    return GeoPoint(lat=lat + lat_offset, lon=lon + lon_offset, timestamp=timestamp)


@register_connector("synthetic")
class SyntheticAppleHealthConnector:
    name = "synthetic"

    def __init__(self, seed: int = 42, duration_seconds: int = 600) -> None:
        self.seed = seed
        self.duration_seconds = duration_seconds

    async def fetch_samples(self, window: TimeWindow) -> AsyncIterator[HealthSample]:
        workout = await self._workout(window)
        for sample in workout.samples:
            yield sample

    async def fetch_workouts(self, window: TimeWindow) -> AsyncIterator[WorkoutSession]:
        yield await self._workout(window)

    async def _workout(self, window: TimeWindow) -> WorkoutSession:
        end = window.end
        start = max(window.start, end - timedelta(seconds=self.duration_seconds))
        duration = max(1, int((end - start).total_seconds()))
        rng = random.Random(self.seed)
        route = [
            _offset_point(
                _TRACK_RADIUS_M * math.sin(2 * math.pi * (second / 100)),
                _TRACK_RADIUS_M * math.cos(2 * math.pi * (second / 100)),
                start + timedelta(seconds=second),
            )
            for second in range(duration + 1)
        ]
        samples: list[HealthSample] = []
        for second in range(0, duration, 10):
            timestamp = start + timedelta(seconds=second)
            heart_rate = 145 + rng.uniform(-5, 5)
            hrv = 42 + rng.uniform(-4, 4)
            samples.extend(
                [
                    HealthSample(
                        metric=Metric.HEART_RATE,
                        value=heart_rate,
                        unit="count/min",
                        start=timestamp,
                        end=timestamp + timedelta(seconds=10),
                        source="Synthetic Apple Watch",
                    ),
                    HealthSample(
                        metric=Metric.HRV_SDNN,
                        value=hrv,
                        unit="ms",
                        start=timestamp,
                        end=timestamp + timedelta(seconds=10),
                        source="Synthetic Apple Watch",
                    ),
                    HealthSample(
                        metric=Metric.RESTING_HEART_RATE,
                        value=58 + rng.uniform(-2, 2),
                        unit="count/min",
                        start=timestamp,
                        end=timestamp + timedelta(seconds=10),
                        source="Synthetic Apple Watch",
                    ),
                ]
            )
        identity = f"synthetic|{self.seed}|{start.isoformat()}|{end.isoformat()}"
        return WorkoutSession(
            id=hashlib.sha256(identity.encode()).hexdigest(),
            activity="Running",
            start=start,
            end=end,
            distance_m=duration * 4.0,
            route=route,
            samples=samples,
        )
