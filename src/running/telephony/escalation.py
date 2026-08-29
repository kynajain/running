"""Runner safety escalation orchestration."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator

from running.models import GeoPoint
from running.telephony.config import TelephonyConfig
from running.telephony.elevenlabs import ConversationStatus, ElevenLabsClient
from running.telephony.twilio_sms import TwilioSMSClient

logger = logging.getLogger(__name__)
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


class SafetyAlert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: GeoPoint
    timestamp: datetime

    _validate_timestamp = field_validator("timestamp")(_utc_datetime)


class EscalationStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    outcome: str
    error: str | None = None


class EscalationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runner_engaged: bool
    steps: list[EscalationStep]


class EscalationService:
    def __init__(
        self,
        config: TelephonyConfig,
        elevenlabs: ElevenLabsClient,
        twilio: TwilioSMSClient,
        ring_timeout: float = 30.0,
        poll_interval: float = 1.0,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.config = config
        self.elevenlabs = elevenlabs
        self.twilio = twilio
        self.ring_timeout = ring_timeout
        self.poll_interval = poll_interval
        self.clock = clock
        self.sleep = sleep

    async def escalate(self, alert: SafetyAlert) -> EscalationResult:
        steps: list[EscalationStep] = []
        runner_engaged = await self._runner_call(alert, steps)
        if runner_engaged:
            logger.info("runner engaged; escalation complete")
            return EscalationResult(runner_engaged=True, steps=steps)

        logger.info("runner did not engage; contacting emergency contact")
        await asyncio.gather(
            self._contact_call(steps),
            self._contact_sms(alert, steps),
        )
        return EscalationResult(runner_engaged=False, steps=steps)

    async def _runner_call(self, alert: SafetyAlert, steps: list[EscalationStep]) -> bool:
        try:
            call = await self.elevenlabs.start_outbound_call(
                self.config.elevenlabs_runner_agent_id,
                self.config.elevenlabs_agent_phone_number_id,
                self.config.runner_phone_number,
            )
        except Exception as exc:
            steps.append(EscalationStep(name="runner_call", outcome="error", error=str(exc)))
            return False
        steps.append(
            EscalationStep(
                name="runner_call",
                outcome="initiated" if call.conversation_id else "no_conversation_id",
            )
        )
        if call.conversation_id is None:
            return False

        deadline = self.clock() + self.ring_timeout
        while True:
            try:
                details = await self.elevenlabs.get_conversation(call.conversation_id)
            except Exception as exc:
                steps.append(EscalationStep(name="runner_poll", outcome="error", error=str(exc)))
                return False
            if details.metadata.accepted_time_unix_secs is not None:
                steps.append(EscalationStep(name="runner_poll", outcome="answered"))
                return True
            if details.status == ConversationStatus.DONE:
                steps.append(EscalationStep(name="runner_poll", outcome="no_answer"))
                return False
            if details.status == ConversationStatus.FAILED:
                steps.append(EscalationStep(name="runner_poll", outcome="failed"))
                return False
            if self.clock() >= deadline:
                steps.append(EscalationStep(name="runner_poll", outcome="timeout"))
                return False
            await self.sleep(min(self.poll_interval, max(0.0, deadline - self.clock())))

    async def _contact_call(self, steps: list[EscalationStep]) -> None:
        try:
            call = await self.elevenlabs.start_outbound_call(
                self.config.elevenlabs_contact_agent_id,
                self.config.elevenlabs_agent_phone_number_id,
                self.config.emergency_contact_phone_number,
            )
            outcome = "initiated" if call.conversation_id else "no_conversation_id"
            steps.append(EscalationStep(name="contact_call", outcome=outcome))
        except Exception as exc:
            steps.append(EscalationStep(name="contact_call", outcome="error", error=str(exc)))

    async def _contact_sms(self, alert: SafetyAlert, steps: list[EscalationStep]) -> None:
        try:
            await self.twilio.send_sms(
                self.config.emergency_contact_phone_number,
                self.config.twilio_from_number,
                sms_body(alert),
            )
            steps.append(EscalationStep(name="contact_sms", outcome="sent"))
        except Exception as exc:
            steps.append(EscalationStep(name="contact_sms", outcome="error", error=str(exc)))


def sms_body(alert: SafetyAlert) -> str:
    link = f"https://maps.google.com/?q={alert.location.lat},{alert.location.lon}"
    return f"Runner safety alert at {alert.timestamp.isoformat()}. Location: {link}"


def dry_run_plan(alert: SafetyAlert) -> str:
    return "\n".join(
        [
            "Escalation plan (dry run; zero HTTP calls):",
            "1. Call runner agent $ELEVENLABS_RUNNER_AGENT_ID",
            "   Number: $RUNNER_PHONE_NUMBER",
            "2. If the runner does not engage:",
            "   Call contact agent $ELEVENLABS_CONTACT_AGENT_ID",
            "   Number: $EMERGENCY_CONTACT_PHONE_NUMBER",
            f"   SMS from $TWILIO_FROM_NUMBER: {sms_body(alert)}",
        ]
    )
