"""State machine for the emergency response flow.

    idle -> confirming -> consenting -> recording -> alarm

The machine is pure: it takes an event, mutates its own state and returns the
effects a caller must perform. No I/O, no clock and no threads, so every branch
(including the "user is unconscious" one) is exercised in tests.

Deadlines are enforced against the timestamp of whichever event arrives first,
not against a ``Tick``: a countdown that has already run out escalates even if
the delivery of the tick was delayed behind a late user response.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

MIN_COUNTDOWN = timedelta(seconds=10)
MAX_COUNTDOWN = timedelta(seconds=20)
DEFAULT_COUNTDOWN = timedelta(seconds=15)


class ResponseState(StrEnum):
    IDLE = "idle"
    CONFIRMING = "confirming"
    CONSENTING = "consenting"
    RECORDING = "recording"
    ALARM = "alarm"


class Trigger(StrEnum):
    STRESS = "stress"
    IMPACT = "impact"


class AlarmReason(StrEnum):
    NO_RESPONSE = "no_response"
    USER_CONFIRMED = "user_confirmed"


@dataclass(frozen=True)
class ThresholdCrossed:
    at: datetime
    trigger: Trigger
    confidence: float


@dataclass(frozen=True)
class Dismissed:
    """The single-tap false-positive escape hatch."""

    at: datetime


@dataclass(frozen=True)
class DistressConfirmed:
    at: datetime


@dataclass(frozen=True)
class RecordingChoice:
    at: datetime
    accepted: bool


@dataclass(frozen=True)
class Tick:
    at: datetime


Event = ThresholdCrossed | Dismissed | DistressConfirmed | RecordingChoice | Tick


@dataclass(frozen=True)
class ShowConfirmationPrompt:
    """Ask "Are you okay?" until ``deadline``, dismissable in one tap."""

    deadline: datetime
    trigger: Trigger
    confidence: float


@dataclass(frozen=True)
class ShowRecordingPrompt:
    deadline: datetime


@dataclass(frozen=True)
class DismissPrompt:
    pass


@dataclass(frozen=True)
class StartRecording:
    at: datetime


@dataclass(frozen=True)
class StopRecording:
    at: datetime


@dataclass(frozen=True)
class RaiseAlarm:
    """Notify emergency contacts with location, plus the recording if any."""

    at: datetime
    reason: AlarmReason
    trigger: Trigger
    recording: bool


Effect = (
    ShowConfirmationPrompt
    | ShowRecordingPrompt
    | DismissPrompt
    | StartRecording
    | StopRecording
    | RaiseAlarm
)


@dataclass(frozen=True)
class ResponseConfig:
    countdown: timedelta = DEFAULT_COUNTDOWN
    recording_opt_in: bool = False
    """Set during setup, never under stress, so audio consent stays informed."""

    def __post_init__(self) -> None:
        if not MIN_COUNTDOWN <= self.countdown <= MAX_COUNTDOWN:
            raise ValueError("countdown must be between 10 and 20 seconds")


class ResponseMachine:
    def __init__(self, config: ResponseConfig | None = None) -> None:
        self.config = config or ResponseConfig()
        self.state = ResponseState.IDLE
        self.trigger: Trigger | None = None
        self.deadline: datetime | None = None
        self.recording = False

    def handle(self, event: Event) -> list[Effect]:
        match self.state, event:
            case ResponseState.IDLE, ThresholdCrossed():
                return self._confirm(event)
            case ResponseState.CONFIRMING, _ if self._expired(event.at):
                return self._alarm(event.at, AlarmReason.NO_RESPONSE, prompted=True)
            case ResponseState.CONFIRMING, Dismissed():
                return self._reset(event.at)
            case ResponseState.CONFIRMING, DistressConfirmed():
                return self._consent(event.at)
            case ResponseState.CONSENTING, _ if self._expired(event.at):
                return self._alarm(event.at, AlarmReason.USER_CONFIRMED, prompted=True)
            case ResponseState.CONSENTING, Dismissed():
                return self._reset(event.at)
            case ResponseState.CONSENTING, RecordingChoice(accepted=True):
                return self._record(event.at)
            case ResponseState.CONSENTING, RecordingChoice(accepted=False):
                return self._alarm(event.at, AlarmReason.USER_CONFIRMED, prompted=True)
            case ((ResponseState.ALARM | ResponseState.RECORDING), Dismissed()):
                return self._reset(event.at)
            case _:
                return []

    def _expired(self, now: datetime) -> bool:
        return self.deadline is not None and now >= self.deadline

    def _confirm(self, event: ThresholdCrossed) -> list[Effect]:
        self.state = ResponseState.CONFIRMING
        self.trigger = event.trigger
        self.deadline = event.at + self.config.countdown
        return [
            ShowConfirmationPrompt(
                deadline=self.deadline,
                trigger=event.trigger,
                confidence=event.confidence,
            )
        ]

    def _consent(self, at: datetime) -> list[Effect]:
        if not self.config.recording_opt_in:
            return self._alarm(at, AlarmReason.USER_CONFIRMED, prompted=True)
        self.state = ResponseState.CONSENTING
        self.deadline = at + self.config.countdown
        return [DismissPrompt(), ShowRecordingPrompt(deadline=self.deadline)]

    def _record(self, at: datetime) -> list[Effect]:
        self.recording = True
        trigger = self.trigger or Trigger.STRESS
        self.state = ResponseState.RECORDING
        self.deadline = None
        return [
            DismissPrompt(),
            StartRecording(at=at),
            RaiseAlarm(
                at=at,
                reason=AlarmReason.USER_CONFIRMED,
                trigger=trigger,
                recording=True,
            ),
        ]

    def _alarm(self, at: datetime, reason: AlarmReason, *, prompted: bool) -> list[Effect]:
        trigger = self.trigger or Trigger.STRESS
        self.state = ResponseState.ALARM
        self.deadline = None
        effects: list[Effect] = [DismissPrompt()] if prompted else []
        effects.append(RaiseAlarm(at=at, reason=reason, trigger=trigger, recording=self.recording))
        return effects

    def _reset(self, at: datetime) -> list[Effect]:
        effects: list[Effect] = [DismissPrompt()]
        if self.recording:
            effects.append(StopRecording(at=at))
        self.state = ResponseState.IDLE
        self.trigger = None
        self.deadline = None
        self.recording = False
        return effects
