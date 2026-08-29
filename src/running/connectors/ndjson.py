"""Connector for NDJSON batches emitted by the RunningHealth iOS app.

Each line is ``{"type": "sample" | "workout", "record": {...}}``. The envelope
exists because the domain models forbid extra keys, so the discriminator
cannot live alongside the record fields.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

from running.connectors.base import register_connector
from running.models import HealthSample, TimeWindow, WorkoutSession


@register_connector("ndjson")
class NDJSONConnector:
    name = "ndjson"

    def __init__(self, path: Path) -> None:
        self.path = path

    def _records(self, kind: str) -> Iterator[dict[str, Any]]:
        with self.path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    envelope = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{self.path}:{number} is not valid JSON") from exc
                if not isinstance(envelope, dict) or "record" not in envelope:
                    raise ValueError(f"{self.path}:{number} is missing a record envelope")
                if envelope.get("type") != kind:
                    continue
                record = envelope["record"]
                if not isinstance(record, dict):
                    raise ValueError(f"{self.path}:{number} has a non-object record")
                yield record

    async def fetch_samples(self, window: TimeWindow) -> AsyncIterator[HealthSample]:
        for record in self._records("sample"):
            sample = HealthSample.model_validate(record)
            if sample.start < window.end and sample.end > window.start:
                yield sample

    async def fetch_workouts(self, window: TimeWindow) -> AsyncIterator[WorkoutSession]:
        for record in self._records("workout"):
            workout = WorkoutSession.model_validate(record)
            if workout.start < window.end and workout.end > window.start:
                yield workout
