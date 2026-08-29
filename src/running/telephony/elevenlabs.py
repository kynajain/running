"""Typed async client for ElevenLabs Conversational AI calls."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

Sleep = Callable[[float], Awaitable[None]]


class ElevenLabsAPIError(RuntimeError):
    """An ElevenLabs request failed without exposing credentials."""

    def __init__(self, status_code: int, response_body: str) -> None:
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(f"ElevenLabs request failed ({status_code}): {response_body}")


class ConversationStatus(StrEnum):
    INITIATED = "initiated"
    IN_PROGRESS = "in-progress"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in {ConversationStatus.DONE, ConversationStatus.FAILED}


class OutboundCallResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    success: bool = True
    message: str = ""
    conversation_id: str | None = None
    call_sid: str | None = Field(default=None, validation_alias="callSid")


class ConversationMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    accepted_time_unix_secs: int | None = None
    call_duration_secs: int = 0


class ConversationDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: str = ""
    status: ConversationStatus = ConversationStatus.UNKNOWN
    metadata: ConversationMetadata = Field(default_factory=ConversationMetadata)

    @field_validator("status", mode="before")
    @classmethod
    def unknown_status(cls, value: object) -> ConversationStatus | object:
        if isinstance(value, str):
            try:
                return ConversationStatus(value)
            except ValueError:
                return ConversationStatus.UNKNOWN
        return value


class ElevenLabsClient:
    base_url = "https://api.elevenlabs.io"

    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
        max_attempts: int = 4,
        retry_backoff: float = 0.5,
    ) -> None:
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(base_url=self.base_url)
        self._owns_client = client is None
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.max_attempts = max_attempts
        self.retry_backoff = retry_backoff

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def __aenter__(self) -> ElevenLabsClient:
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        await self.aclose()

    async def start_outbound_call(
        self,
        agent_id: str,
        agent_phone_number_id: str,
        to_number: str,
        call_recording_enabled: bool = False,
    ) -> OutboundCallResponse:
        response = await self._request(
            "POST",
            "/v1/convai/twilio/outbound-call",
            {
                "agent_id": agent_id,
                "agent_phone_number_id": agent_phone_number_id,
                "to_number": to_number,
                # Docs also expose telephony_call_config.twilio_call_recording_enabled.
                "call_recording_enabled": call_recording_enabled,
            },
        )
        return OutboundCallResponse.model_validate(response.json())

    async def get_conversation(self, conversation_id: str) -> ConversationDetails:
        response = await self._request(
            "GET",
            f"/v1/convai/conversations/{conversation_id}",
        )
        return ConversationDetails.model_validate(response.json())

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> httpx.Response:
        headers = {"xi-api-key": self.api_key}
        for attempt in range(self.max_attempts):
            response = await self.client.request(
                method,
                path,
                headers=headers,
                json=payload,
            )
            retryable = response.status_code == 429 or 500 <= response.status_code <= 599
            if not retryable:
                if response.is_error:
                    raise self._error(response)
                return response
            if attempt == self.max_attempts - 1:
                raise self._error(response)
            delay = self._retry_delay(response, attempt)
            await self.sleep(delay)
        raise RuntimeError("unreachable request state")

    def _error(self, response: httpx.Response) -> ElevenLabsAPIError:
        return ElevenLabsAPIError(
            response.status_code, response.text.replace(self.api_key, "[redacted]")
        )

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
