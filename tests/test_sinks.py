from datetime import UTC, datetime, timedelta

import httpx
import pytest

from running.models import HealthSample, Metric
from running.sinks.jsonl import JsonlSink
from running.sinks.notion import NotionConfigurationError, NotionSink


def make_sample() -> HealthSample:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return HealthSample(
        metric=Metric.HEART_RATE,
        value=120,
        unit="count/min",
        start=start,
        end=start + timedelta(seconds=1),
        source="test",
    )


async def test_jsonl_sink(tmp_path) -> None:
    sink = JsonlSink(tmp_path / "records.jsonl")
    assert await sink.write_samples([make_sample()]) == 1
    assert '"metric":"heart_rate"' in (tmp_path / "records.jsonl").read_text()


async def test_notion_create_and_dedupe() -> None:
    calls: list[str] = []
    queries = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal queries
        calls.append(request.url.path)
        if request.url.path.endswith("/query"):
            queries += 1
            if queries > 1:
                return httpx.Response(200, json={"results": [{"id": "page"}]})
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"id": "page"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.notion.com/v1"
    )
    sink = NotionSink(token="token", database_id="database", client=client)
    assert await sink.write_samples([make_sample()]) == 1
    assert await sink.write_samples([make_sample()]) == 0
    assert calls.count("/v1/pages") == 1
    await client.aclose()


async def test_notion_429_retry() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.1"})
        return httpx.Response(200, json={"results": []})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.notion.com/v1"
    )
    sink = NotionSink(
        token="token",
        database_id="database",
        client=client,
        sleep=lambda delay: _record_delay(delays, delay),
    )
    assert await sink._exists("id") is False
    assert attempts == 2
    assert delays == [0.1]
    await client.aclose()


async def _record_delay(delays: list[float], delay: float) -> None:
    delays.append(delay)


def test_notion_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTION_API_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_DATABASE_ID", raising=False)
    with pytest.raises(NotionConfigurationError):
        NotionSink()
