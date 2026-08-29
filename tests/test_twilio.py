from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from running.models import HealthSample, Metric
from running.sinks.twilio import TwilioAlertSink, TwilioConfigurationError

CREDENTIALS = {
    "account_sid": "AC123",
    "auth_token": "token",
    "from_number": "+15005550006",
    "to_number": "+447700900123",
}


def stress(value: float, minutes: int = 0) -> HealthSample:
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minutes)
    return HealthSample(
        metric=Metric.STRESS_SCORE,
        value=value,
        unit="score",
        start=start,
        end=start,
        source="Derived stress heuristic",
    )


def sink(
    tmp_path: Path,
    handler: httpx.MockTransport | None = None,
    **overrides: object,
) -> tuple[TwilioAlertSink, list[dict[str, list[str]]]]:
    sent: list[dict[str, list[str]]] = []

    def default(request: httpx.Request) -> httpx.Response:
        sent.append(parse_qs(request.content.decode()))
        return httpx.Response(201, json={"sid": "SM1"})

    client = httpx.AsyncClient(
        transport=handler or httpx.MockTransport(default),
        base_url="https://api.twilio.com",
    )
    return (
        TwilioAlertSink(
            **CREDENTIALS,
            client=client,
            state_path=tmp_path / "state.json",
            **overrides,
        ),
        sent,
    )


async def test_alerts_once_for_the_worst_score_in_a_batch(tmp_path: Path) -> None:
    alerts, sent = sink(tmp_path)
    written = await alerts.write_samples([stress(78), stress(91, minutes=5), stress(40)])
    assert written == 1
    assert len(sent) == 1
    assert sent[0]["To"] == ["+447700900123"]
    assert "91/100" in sent[0]["Body"][0]
    await alerts.client.aclose()


async def test_ignores_scores_below_the_threshold_and_other_metrics(tmp_path: Path) -> None:
    alerts, sent = sink(tmp_path)
    heart_rate = stress(99).model_copy(update={"metric": Metric.HEART_RATE})
    assert await alerts.write_samples([stress(74), heart_rate]) == 0
    assert sent == []
    await alerts.client.aclose()


async def test_cooldown_suppresses_a_second_alert(tmp_path: Path) -> None:
    alerts, sent = sink(tmp_path, cooldown=timedelta(hours=6))
    assert await alerts.write_samples([stress(90)]) == 1
    assert await alerts.write_samples([stress(95, minutes=60)]) == 0
    assert await alerts.write_samples([stress(95, minutes=7 * 60)]) == 1
    assert len(sent) == 2
    await alerts.client.aclose()


async def test_workouts_never_trigger_alerts(tmp_path: Path) -> None:
    alerts, _ = sink(tmp_path)
    assert await alerts.write_workouts([]) == 0
    await alerts.client.aclose()


async def test_retries_throttled_requests(tmp_path: Path) -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.1"})
        return httpx.Response(201, json={"sid": "SM1"})

    async def record(delay: float) -> None:
        delays.append(delay)

    alerts, _ = sink(tmp_path, httpx.MockTransport(handler), sleep=record)
    assert await alerts.write_samples([stress(90)]) == 1
    assert attempts == 2
    assert delays == [0.1]
    await alerts.client.aclose()


def test_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "TWILIO_TO_NUMBER",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(TwilioConfigurationError):
        TwilioAlertSink()


def test_rejects_numbers_that_are_not_e164(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    with pytest.raises(TwilioConfigurationError, match="TWILIO_FROM_NUMBER"):
        TwilioAlertSink(from_number="07700900123", to_number="+447700900123")
