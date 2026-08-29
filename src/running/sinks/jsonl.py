"""Local JSON Lines sink."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from running.connectors.base import register_sink
from running.models import HealthSample, WorkoutSession


@register_sink("jsonl")
class JsonlSink:
    name = "jsonl"

    def __init__(self, path: Path = Path("running.jsonl")) -> None:
        self.path = path

    async def write_samples(self, batch: Sequence[HealthSample]) -> int:
        return await self._write([sample.model_dump(mode="json") for sample in batch])

    async def write_workouts(self, batch: Sequence[WorkoutSession]) -> int:
        return await self._write([workout.model_dump(mode="json") for workout in batch])

    async def _write(self, records: list[dict[str, object]]) -> int:
        if not records:
            return 0

        def write() -> None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as output:
                for record in records:
                    output.write(json.dumps(record, separators=(",", ":")) + "\n")

        await asyncio.to_thread(write)
        return len(records)
