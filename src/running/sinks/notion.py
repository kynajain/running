"""Notion database sink with deterministic external-ID upserts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

import httpx

from running.connectors.base import register_sink
from running.models import HealthSample, WorkoutSession

Sleep = Callable[[float], Awaitable[None]]


class NotionConfigurationError(RuntimeError):
    """Raised when required Notion credentials are missing."""


@register_sink("notion")
class NotionSink:
    name = "notion"

    def __init__(
        self,
        token: str | None = None,
        database_id: str | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        max_attempts: int = 4,
    ) -> None:
        self.token = token or os.environ.get("NOTION_API_TOKEN")
        self.database_id = database_id or os.environ.get("NOTION_DATABASE_ID")
        if not self.token or not self.database_id:
            raise NotionConfigurationError("NOTION_API_TOKEN and NOTION_DATABASE_ID must be set")
        self.client = client or httpx.AsyncClient(base_url="https://api.notion.com/v1")
        self.sleep = sleep
        self.max_attempts = max_attempts

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }

    async def write_samples(self, batch: Sequence[HealthSample]) -> int:
        written = 0
        for sample in batch:
            external_id = self._sample_id(sample)
            if await self._exists(external_id):
                continue
            await self._create_page(self._sample_properties(sample, external_id))
            written += 1
        return written

    async def write_workouts(self, batch: Sequence[WorkoutSession]) -> int:
        written = 0
        for workout in batch:
            if await self._exists(workout.id):
                continue
            title = f"{workout.activity} {workout.start.isoformat()}"
            properties = {
                "Title": {"title": [{"text": {"content": title}}]},
                "Number": {"number": workout.distance_m},
                "Date": {
                    "date": {
                        "start": workout.start.isoformat(),
                        "end": workout.end.isoformat(),
                    }
                },
                "Source": {"rich_text": [{"text": {"content": "Apple Health"}}]},
                "External ID": {"rich_text": [{"text": {"content": workout.id}}]},
            }
            await self._create_page(properties)
            written += 1
        return written

    async def _exists(self, external_id: str) -> bool:
        response = await self._request(
            "POST",
            f"/databases/{self.database_id}/query",
            {
                "filter": {
                    "property": "External ID",
                    "rich_text": {"equals": external_id},
                }
            },
        )
        results = response.json().get("results", [])
        return isinstance(results, list) and bool(results)

    async def _create_page(self, properties: dict[str, object]) -> None:
        await self._request(
            "POST",
            "/pages",
            {"parent": {"database_id": self.database_id}, "properties": properties},
        )

    async def _request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> httpx.Response:
        for attempt in range(self.max_attempts):
            response = await self.client.request(
                method,
                path,
                headers=self.headers,
                json=payload,
            )
            if response.status_code != 429:
                response.raise_for_status()
                return response
            if attempt == self.max_attempts - 1:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After", "1")
            try:
                delay = max(0.0, float(retry_after))
            except ValueError:
                delay = 1.0
            await self.sleep(delay)
        raise RuntimeError("unreachable request state")

    @staticmethod
    def _sample_id(sample: HealthSample) -> str:
        payload = {
            "metric": sample.metric.value,
            "value": sample.value,
            "unit": sample.unit,
            "start": sample.start.isoformat(),
            "end": sample.end.isoformat(),
            "source": sample.source,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _sample_properties(sample: HealthSample, external_id: str) -> dict[str, object]:
        timestamp: datetime = sample.start
        return {
            "Title": {
                "title": [{"text": {"content": f"{sample.metric.value} {timestamp.isoformat()}"}}]
            },
            "Number": {"number": sample.value},
            "Date": {"date": {"start": timestamp.isoformat(), "end": sample.end.isoformat()}},
            "Source": {"rich_text": [{"text": {"content": sample.source}}]},
            "External ID": {"rich_text": [{"text": {"content": external_id}}]},
        }
