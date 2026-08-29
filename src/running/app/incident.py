"""In-memory incident and in-app session state machine."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from running.telephony.elevenlabs import ConversationStatus, ElevenLabsClient
from running.telephony.twilio_sms import TwilioSMSClient

logger = logging.getLogger(__name__)
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class IncidentState(StrEnum):
    TRIGGERED = "triggered"
    RUNNER_SESSION_ACTIVE = "runner_session_active"
    RESOLVED = "resolved"
    ESCALATING = "escalating"
    ESCALATED = "escalated"
    ESCALATION_FAILED = "escalation_failed"


class SessionOutcome(StrEnum):
    ANSWERED = "answered"
    UNANSWERED = "unanswered"
    FAILED = "failed"


class TriggerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    fix_age_seconds: int = Field(ge=0)


class TriggerResponse(BaseModel):
    incident_id: str
    state: IncidentState


class SessionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    conversation_id: str


class SessionResponse(BaseModel):
    outcome: SessionOutcome | None


class IncidentSummary(BaseModel):
    incident_id: str
    message: str
    state: IncidentState
    latitude: float
    longitude: float
    fix_age_seconds: int
    pending_session_id: str | None


class CurrentIncidentResponse(BaseModel):
    incident: IncidentSummary | None


class SessionView(BaseModel):
    session_id: str
    role: str
    conversation_id: str | None
    outcome: SessionOutcome | None


class EscalationView(BaseModel):
    sent: bool
    dry_run: bool
    error: str | None


class IncidentView(BaseModel):
    incident_id: str
    state: IncidentState
    sessions: list[SessionView]
    escalation: EscalationView


@dataclass
class _Session:
    session_id: str
    role: str = "runner"
    conversation_id: str | None = None
    outcome: SessionOutcome | None = None
    cap_task: asyncio.Task[None] | None = None


@dataclass
class _Incident:
    incident_id: str
    message: str
    latitude: float
    longitude: float
    fix_age_seconds: int
    pending_session_id: str | None
    state: IncidentState = IncidentState.TRIGGERED
    sessions: dict[str, _Session] = field(default_factory=dict)
    escalation_sent: bool = False
    escalation_dry_run: bool = False
    escalation_attempted: bool = False
    escalation_error: str | None = None
    escalation_task: asyncio.Task[None] | None = None


class IncidentNotFoundError(LookupError):
    """Raised when an incident ID is unknown."""


class SessionNotFoundError(LookupError):
    """Raised when a session ID is unknown or not pending."""


class IncidentManager:
    def __init__(
        self,
        runner_agent_id: str,
        contact_phone_number: str | None,
        twilio_from_number: str | None,
        elevenlabs: ElevenLabsClient,
        twilio: TwilioSMSClient | None,
        escalation_delay_seconds: float = 120.0,
        session_max_seconds: float = 90.0,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.runner_agent_id = runner_agent_id
        self.contact_phone_number = contact_phone_number
        self.twilio_from_number = twilio_from_number
        self.elevenlabs = elevenlabs
        self.twilio = twilio
        self.escalation_delay_seconds = escalation_delay_seconds
        self.session_max_seconds = session_max_seconds
        self.clock = clock
        self.sleep = sleep
        self._incidents: dict[str, _Incident] = {}
        self._lock = asyncio.Lock()

    async def create(self, payload: TriggerPayload) -> _Incident:
        incident_id = uuid4().hex
        session_id = uuid4().hex
        incident = _Incident(
            incident_id=incident_id,
            message=payload.message,
            latitude=payload.latitude,
            longitude=payload.longitude,
            fix_age_seconds=payload.fix_age_seconds,
            pending_session_id=session_id,
            sessions={session_id: _Session(session_id=session_id)},
        )
        async with self._lock:
            self._incidents[incident_id] = incident
        incident.escalation_task = asyncio.create_task(self._escalation_timer(incident_id))
        return incident

    async def current(self, role: str) -> _Incident | None:
        async with self._lock:
            active = [
                incident
                for incident in self._incidents.values()
                if incident.state
                not in {
                    IncidentState.RESOLVED,
                    IncidentState.ESCALATED,
                    IncidentState.ESCALATION_FAILED,
                }
            ]
            if not active:
                return None
            incident = active[-1]
            if role == "contact":
                incident = copy.copy(incident)
                incident.pending_session_id = None
            return incident

    async def attach_session(
        self,
        incident_id: str,
        payload: SessionPayload,
    ) -> SessionOutcome | None:
        incident = await self._get_incident(incident_id)
        async with self._lock:
            session = incident.sessions.get(payload.session_id)
            if session is None or session.role != "runner":
                raise SessionNotFoundError(payload.session_id)
            if (
                session.conversation_id is not None
                and session.conversation_id != payload.conversation_id
            ):
                raise SessionNotFoundError(payload.session_id)
            session.conversation_id = payload.conversation_id
            if incident.pending_session_id == session.session_id:
                incident.pending_session_id = None
            if incident.state == IncidentState.TRIGGERED:
                incident.state = IncidentState.RUNNER_SESSION_ACTIVE
            outcome = session.outcome
            if session.cap_task is None:
                session.cap_task = asyncio.create_task(
                    self._session_cap(incident_id, session.session_id)
                )
        if outcome is None:
            outcome = await self._evaluate_session(payload.conversation_id)
            await self._set_outcome(incident, session, outcome)
        return outcome

    async def acknowledge(self, incident_id: str) -> None:
        incident = await self._get_incident(incident_id)
        async with self._lock:
            session = next(
                (item for item in incident.sessions.values() if item.role == "runner"),
                None,
            )
            if session is None:
                raise SessionNotFoundError(incident_id)
            if session.outcome != SessionOutcome.ANSWERED:
                session.outcome = SessionOutcome.ANSWERED
            if incident.state not in {
                IncidentState.ESCALATED,
                IncidentState.ESCALATION_FAILED,
            }:
                incident.state = IncidentState.RESOLVED
                self._cancel_task(incident.escalation_task)
        logger.info("incident %s acknowledged", incident_id)

    async def view(self, incident_id: str) -> IncidentView:
        incident = await self._get_incident(incident_id)
        async with self._lock:
            return self._view(incident)

    async def close(self) -> None:
        async with self._lock:
            incidents = list(self._incidents.values())
        for incident in incidents:
            self._cancel_task(incident.escalation_task)
            for session in incident.sessions.values():
                self._cancel_task(session.cap_task)

    async def _get_incident(self, incident_id: str) -> _Incident:
        async with self._lock:
            incident = self._incidents.get(incident_id)
        if incident is None:
            raise IncidentNotFoundError(incident_id)
        return incident

    async def _evaluate_session(self, conversation_id: str) -> SessionOutcome | None:
        try:
            details = await self.elevenlabs.get_conversation(conversation_id)
        except Exception:
            return SessionOutcome.FAILED
        if any(
            entry.role == "user" and entry.message is not None and entry.message.strip()
            for entry in details.transcript
        ):
            return SessionOutcome.ANSWERED
        if details.status == ConversationStatus.FAILED:
            return SessionOutcome.FAILED
        if details.status == ConversationStatus.DONE:
            return SessionOutcome.UNANSWERED
        return None

    async def _set_outcome(
        self,
        incident: _Incident,
        session: _Session,
        outcome: SessionOutcome | None,
    ) -> None:
        if outcome is None:
            return
        async with self._lock:
            if session.outcome == SessionOutcome.ANSWERED:
                return
            session.outcome = outcome
            if outcome == SessionOutcome.ANSWERED:
                if incident.state not in {
                    IncidentState.ESCALATED,
                    IncidentState.ESCALATION_FAILED,
                }:
                    incident.state = IncidentState.RESOLVED
                    self._cancel_task(incident.escalation_task)
                logger.info("incident %s runner session answered", incident.incident_id)

    async def _session_cap(self, incident_id: str, session_id: str) -> None:
        incident = await self._get_incident(incident_id)
        async with self._lock:
            session = incident.sessions.get(session_id)
            if session is None or session.outcome == SessionOutcome.ANSWERED:
                return
        try:
            await self._wait(self.session_max_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            failed_outcome = SessionOutcome.FAILED
            async with self._lock:
                session = incident.sessions.get(session_id)
            if session is not None:
                await self._set_outcome(incident, session, failed_outcome)
            return
        async with self._lock:
            session = incident.sessions.get(session_id)
            if session is None or session.outcome == SessionOutcome.ANSWERED:
                return
            conversation_id = session.conversation_id
        outcome: SessionOutcome | None = (
            await self._evaluate_session(conversation_id)
            if conversation_id is not None
            else SessionOutcome.UNANSWERED
        )
        if outcome is None:
            outcome = SessionOutcome.UNANSWERED
        await self._set_outcome(incident, session, outcome)

    async def _escalation_timer(self, incident_id: str) -> None:
        incident = await self._get_incident(incident_id)
        wait_error: str | None = None
        try:
            await self._wait(self.escalation_delay_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            wait_error = str(exc)
        async with self._lock:
            if incident.escalation_attempted or incident.state == IncidentState.RESOLVED:
                return
            if any(
                session.outcome == SessionOutcome.ANSWERED for session in incident.sessions.values()
            ):
                incident.state = IncidentState.RESOLVED
                return
            for session in incident.sessions.values():
                if session.outcome is None:
                    session.outcome = SessionOutcome.UNANSWERED
            incident.escalation_attempted = True
            incident.state = IncidentState.ESCALATING
        logger.info("incident %s escalating to emergency contact", incident_id)
        body = self._sms_body(incident)
        if (
            self.twilio is None
            or self.contact_phone_number is None
            or self.twilio_from_number is None
        ):
            logger.info("incident %s SMS dry run: %s", incident_id, body)
            async with self._lock:
                incident.state = IncidentState.ESCALATED
                incident.escalation_dry_run = True
                incident.escalation_error = wait_error
            return
        try:
            await self.twilio.send_sms(
                self.contact_phone_number,
                self.twilio_from_number,
                body,
            )
        except Exception as exc:
            async with self._lock:
                incident.state = IncidentState.ESCALATION_FAILED
                incident.escalation_error = self._join_errors(wait_error, str(exc))
            logger.info("incident %s escalation SMS failed", incident_id)
        else:
            async with self._lock:
                incident.state = IncidentState.ESCALATED
                incident.escalation_sent = True
                incident.escalation_error = wait_error
            logger.info("incident %s escalation SMS sent", incident_id)

    def _sms_body(self, incident: _Incident) -> str:
        if incident.fix_age_seconds < 60:
            age = f"{incident.fix_age_seconds} seconds"
        else:
            minutes = incident.fix_age_seconds // 60
            age = f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
        location = f"https://maps.google.com/?q={incident.latitude},{incident.longitude}"
        return f"{incident.message} Location: {location} (location fixed {age} ago)."

    def _view(self, incident: _Incident) -> IncidentView:
        return IncidentView(
            incident_id=incident.incident_id,
            state=incident.state,
            sessions=[
                SessionView(
                    session_id=session.session_id,
                    role=session.role,
                    conversation_id=session.conversation_id,
                    outcome=session.outcome,
                )
                for session in incident.sessions.values()
            ],
            escalation=EscalationView(
                sent=incident.escalation_sent,
                dry_run=incident.escalation_dry_run,
                error=incident.escalation_error,
            ),
        )

    @staticmethod
    def _cancel_task(task: asyncio.Task[None] | None) -> None:
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _wait(self, seconds: float) -> None:
        deadline = self.clock() + seconds
        await self.sleep(max(0.0, deadline - self.clock()))

    @staticmethod
    def _join_errors(first: str | None, second: str) -> str:
        return f"{first}; {second}" if first else second
