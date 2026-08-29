"""Twilio SMS alerts for elevated stress scores.

The sink is deliberately quiet: it only reacts to derived
:attr:`running.models.Metric.STRESS_SCORE` samples, sends at most one message
per batch (for the worst score in it), and honours a cooldown persisted across
runs so an hourly sync cannot turn a bad day into a stream of texts.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from running.connectors.base import register_sink
from running.models import HealthSample, Metric, WorkoutSession

Sleep = Callable[[float], Awaitable[None]]

E164 = re.compile(r"^\+[1-9]\d{7,14}$")
DEFAULT_THRESHOLD = 75.0
DEFAULT_COOLDOWN = timedelta(hours=6)


class TwilioConfigurationError(RuntimeError):
    """Raised when required Twilio credentials or numbers are missing."""


def _env_number(name: str, value: str | None) -> str:
    resolved = value or os.environ.get(name, "")
    if not E164.match(resolved):
        raise TwilioConfigurationError(f"{name} must be an E.164 number such as +447700900123")
    return resolved


@register_sink("twilio")
class TwilioAlertSink:
    name = "twilio"

    def __init__(
        self,
        account_sid: str | None = None,
        auth_token: str | None = None,
        from_number: str | None = None,
        to_number: str | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        cooldown: timedelta = DEFAULT_COOLDOWN,
        state_path: Path = Path(".running-twilio-state.json"),
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        max_attempts: int = 4,
        retry_backoff: float = 0.5,
    ) -> None:
        self.account_sid = account_sid or os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.auth_token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN", "")
        if not self.account_sid or not self.auth_token:
            raise TwilioConfigurationError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
        self.from_number = _env_number("TWILIO_FROM_NUMBER", from_number)
        self.to_number = _env_number("TWILIO_TO_NUMBER", to_number)
        self.threshold = threshold
        self.cooldown = cooldown
        self.state_path = state_path
        self.client = client or httpx.AsyncClient(base_url="https://api.twilio.com")
        self._owns_client = client is None
        self.sleep = sleep
        self.max_attempts = max_attempts
        self.retry_backoff = retry_backoff

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def __aenter__(self) -> TwilioAlertSink:
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        await self.aclose()

    async def write_samples(self, batch: Sequence[HealthSample]) -> int:
        elevated = [
            sample
            for sample in batch
            if sample.metric is Metric.STRESS_SCORE and sample.value >= self.threshold
        ]
        if not elevated:
            return 0
        worst = max(elevated, key=lambda sample: sample.value)
        if not self._cooldown_elapsed(worst.start):
            return 0
        await self._send(self._body(worst))
        self._record_alert(worst.start)
        return 1

    async def write_workouts(self, batch: Sequence[WorkoutSession]) -> int:
        return 0

    def _body(self, sample: HealthSample) -> str:
        local_time = sample.start.astimezone().strftime("%H:%M")
        return (
            f"Stress score {sample.value:.0f}/100 at {local_time}. "
            "Pause for a minute: slow exhale, unclench your jaw, drink some water. "
            "You have got this."
        )

    def _last_alert(self) -> datetime | None:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        raw = state.get("last_alert_at") if isinstance(state, dict) else None
        if not isinstance(raw, str):
            return None
        try:
            return datetime.fromisoformat(raw).astimezone(UTC)
        except ValueError:
            return None

    def _cooldown_elapsed(self, observed_at: datetime) -> bool:
        last = self._last_alert()
        return last is None or observed_at - last >= self.cooldown

    def _record_alert(self, observed_at: datetime) -> None:
        payload = json.dumps({"last_alert_at": observed_at.astimezone(UTC).isoformat()})
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(payload, encoding="utf-8")

    async def _send(self, body: str) -> None:
        path = f"/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        form = {"From": self.from_number, "To": self.to_number, "Body": body}
        for attempt in range(self.max_attempts):
            response = await self.client.post(
                path,
                data=form,
                auth=(self.account_sid, self.auth_token),
            )
            retryable = response.status_code == 429 or 500 <= response.status_code <= 599
            if not retryable:
                response.raise_for_status()
                return
            if attempt == self.max_attempts - 1:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            try:
                delay = max(0.0, float(retry_after)) if retry_after else -1.0
            except ValueError:
                delay = -1.0
            if delay < 0:
                delay = self.retry_backoff * (2**attempt)
            await self.sleep(delay)
        raise RuntimeError("unreachable request state")
