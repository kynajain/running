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
        retry_backoff: float = 0.5,
    ) -> None:
        self.token = token or os.environ.get("NOTION_API_TOKEN")
        self.database_id = database_id or os.environ.get("NOTION_DATABASE_ID")
        if not self.token or not self.database_id:
            raise NotionConfigurationError("NOTION_API_TOKEN and NOTION_DATABASE_ID must be set")
        self.client = client or httpx.AsyncClient(base_url="https://api.notion.com/v1")
        self._owns_client = client is None
        self._written_ids: set[str] = set()
        self.sleep = sleep
        self.max_attempts = max_attempts
        self.retry_backoff = retry_backoff

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def __aenter__(self) -> NotionSink:
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        await self.aclose()

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }

    async def write_samples(self, batch: Sequence[HealthSample]) -> int:
        records = [(self._sample_id(sample), sample) for sample in batch]
        existing = await self._existing_ids([external_id for external_id, _ in records])
        written = 0
        for external_id, sample in records:
            if external_id in existing or external_id in self._written_ids:
                continue
            await self._create_page(self._sample_properties(sample, external_id))
            self._written_ids.add(external_id)
            written += 1
        return written

    async def write_workouts(self, batch: Sequence[WorkoutSession]) -> int:
        records = [(workout.id, workout) for workout in batch]
        existing = await self._existing_ids([external_id for external_id, _ in records])
        written = 0
        for external_id, workout in records:
            if external_id in existing or external_id in self._written_ids:
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
            self._written_ids.add(external_id)
            written += 1
        return written

    async def _existing_ids(self, external_ids: Sequence[str]) -> set[str]:
        missing = [
            external_id
            for external_id in dict.fromkeys(external_ids)
            if external_id not in self._written_ids
        ]
        existing: set[str] = set()
        for offset in range(0, len(missing), 100):
            chunk = missing[offset : offset + 100]
            if not chunk:
                continue
            conditions: list[dict[str, object]] = [
                {"property": "External ID", "rich_text": {"equals": external_id}}
                for external_id in chunk
            ]
            query_filter: dict[str, object]
            if len(conditions) == 1:
                query_filter = conditions[0]
            else:
                query_filter = {"or": conditions}
            cursor: str | None = None
            while True:
                payload: dict[str, object] = {
                    "filter": query_filter,
                    "page_size": 100,
                }
                if cursor is not None:
                    payload["start_cursor"] = cursor
                response = await self._request(
                    "POST",
                    f"/databases/{self.database_id}/query",
                    payload,
                )
                body = response.json()
                if not isinstance(body, dict):
                    break
                results = body.get("results", [])
                if isinstance(results, list):
                    for page in results:
                        external_id = self._page_external_id(page)
                        if external_id is not None:
                            existing.add(external_id)
                next_cursor = body.get("next_cursor")
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                cursor = next_cursor
        return existing

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
            retryable = response.status_code == 429 or 500 <= response.status_code <= 599
            if not retryable:
                response.raise_for_status()
                return response
            if attempt == self.max_attempts - 1:
                response.raise_for_status()
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "1")
                try:
                    delay = max(0.0, float(retry_after))
                except ValueError:
                    delay = 1.0
            else:
                delay = self.retry_backoff * (2**attempt)
            await self.sleep(delay)
        raise RuntimeError("unreachable request state")

    @staticmethod
    def _page_external_id(page: object) -> str | None:
        if not isinstance(page, dict):
            return None
        properties = page.get("properties")
        if not isinstance(properties, dict):
            return None
        external_property = properties.get("External ID")
        if not isinstance(external_property, dict):
            return None
        rich_text = external_property.get("rich_text")
        if not isinstance(rich_text, list):
            return None
        for item in rich_text:
            if not isinstance(item, dict):
                continue
            plain_text = item.get("plain_text")
            if isinstance(plain_text, str):
                return plain_text
            text = item.get("text")
            if isinstance(text, dict):
                content = text.get("content")
                if isinstance(content, str):
                    return content
        return None

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
