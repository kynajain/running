"""Typed async client for Twilio SMS."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import cast

import httpx
from pydantic import BaseModel, ConfigDict, Field

Sleep = Callable[[float], Awaitable[None]]


class TwilioAPIError(RuntimeError):
    """A Twilio request failed without exposing credentials."""

    def __init__(self, status_code: int, response_body: str) -> None:
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(f"Twilio request failed ({status_code}): {response_body}")


class TwilioMessageResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sid: str = ""
    status: str = ""
    to: str = ""
    from_: str = Field(default="", validation_alias="from")


class TwilioSMSClient:
    base_url = "https://api.twilio.com"

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
        max_attempts: int = 4,
        retry_backoff: float = 0.5,
    ) -> None:
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.client = client or httpx.AsyncClient(base_url=self.base_url)
        self._owns_client = client is None
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.max_attempts = max_attempts
        self.retry_backoff = retry_backoff

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def __aenter__(self) -> TwilioSMSClient:
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        await self.aclose()

    async def send_sms(self, to_number: str, from_number: str, body: str) -> TwilioMessageResponse:
        response = await self._request(
            "POST",
            f"/2010-04-01/Accounts/{self.account_sid}/Messages.json",
            {"To": to_number, "From": from_number, "Body": body},
        )
        return TwilioMessageResponse.model_validate(response.json())

    async def _request(
        self,
        method: str,
        path: str,
        form: dict[str, str],
    ) -> httpx.Response:
        for attempt in range(self.max_attempts):
            response = await self.client.request(
                method,
                path,
                auth=(self.account_sid, self.auth_token),
                data=form,
            )
            # Message creation is not idempotent: a 5xx can follow an accepted message,
            # so only a 429 (rejected before any work) is repeated.
            idempotent = method.upper() in {"GET", "HEAD"}
            retryable = response.status_code == 429 or (
                idempotent and 500 <= response.status_code <= 599
            )
            if not retryable:
                if response.is_error:
                    raise self._error(response)
                return response
            if attempt == self.max_attempts - 1:
                raise self._error(response)
            delay = self._retry_delay(response, attempt)
            await self.sleep(delay)
        raise RuntimeError("unreachable request state")

    def _error(self, response: httpx.Response) -> TwilioAPIError:
        body = response.text.replace(self.auth_token, "[redacted]")
        body = body.replace(self.account_sid, "[redacted]")
        return TwilioAPIError(response.status_code, body)

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        if response.status_code == 429:
            value = response.headers.get("Retry-After")
            if value is not None:
                try:
                    return max(0.0, float(value))
                except ValueError:
                    pass
        return cast(
            float,
            self.retry_backoff * (2**attempt) * (0.5 + float(self.rng.random())),
        )
