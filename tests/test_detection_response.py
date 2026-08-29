from datetime import UTC, datetime, timedelta

import pytest

from running.detection.response import (
    AlarmReason,
    Dismissed,
    DismissPrompt,
    DistressConfirmed,
    RaiseAlarm,
    RecordingChoice,
    ResponseConfig,
    ResponseMachine,
    ResponseState,
    ShowConfirmationPrompt,
    ShowRecordingPrompt,
    StartRecording,
    StopRecording,
    ThresholdCrossed,
    Tick,
    Trigger,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


def crossed(trigger: Trigger = Trigger.STRESS) -> ThresholdCrossed:
    return ThresholdCrossed(at=START, trigger=trigger, confidence=0.82)


def test_threshold_opens_a_countdown_prompt() -> None:
    machine = ResponseMachine()

    effects = machine.handle(crossed(Trigger.IMPACT))

    assert machine.state is ResponseState.CONFIRMING
    assert effects == [
        ShowConfirmationPrompt(deadline=at(15), trigger=Trigger.IMPACT, confidence=0.82)
    ]


def test_no_response_escalates_to_alarm() -> None:
    machine = ResponseMachine()
    machine.handle(crossed(Trigger.IMPACT))

    assert machine.handle(Tick(at=at(14))) == []
    effects = machine.handle(Tick(at=at(15)))

    assert machine.state is ResponseState.ALARM
    assert effects == [
        DismissPrompt(),
        RaiseAlarm(
            at=at(15),
            reason=AlarmReason.NO_RESPONSE,
            trigger=Trigger.IMPACT,
            recording=False,
        ),
    ]


def test_single_dismissal_clears_the_prompt() -> None:
    machine = ResponseMachine()
    machine.handle(crossed())

    effects = machine.handle(Dismissed(at=at(3)))

    assert machine.state is ResponseState.IDLE
    assert effects == [DismissPrompt()]
    assert machine.handle(Tick(at=at(60))) == []


def test_confirmed_distress_alarms_immediately_without_recording_opt_in() -> None:
    machine = ResponseMachine()
    machine.handle(crossed())

    effects = machine.handle(DistressConfirmed(at=at(4)))

    assert machine.state is ResponseState.ALARM
    assert effects == [
        DismissPrompt(),
        RaiseAlarm(
            at=at(4),
            reason=AlarmReason.USER_CONFIRMED,
            trigger=Trigger.STRESS,
            recording=False,
        ),
    ]


def test_confirmed_distress_offers_recording_when_opted_in() -> None:
    machine = ResponseMachine(ResponseConfig(recording_opt_in=True))
    machine.handle(crossed())

    effects = machine.handle(DistressConfirmed(at=at(4)))

    assert machine.state is ResponseState.CONSENTING
    assert effects == [DismissPrompt(), ShowRecordingPrompt(deadline=at(19))]


def test_accepting_recording_starts_capture_and_alarms_with_it() -> None:
    machine = ResponseMachine(ResponseConfig(recording_opt_in=True))
    machine.handle(crossed())
    machine.handle(DistressConfirmed(at=at(4)))

    effects = machine.handle(RecordingChoice(at=at(6), accepted=True))

    assert machine.state is ResponseState.RECORDING
    assert effects == [
        DismissPrompt(),
        StartRecording(at=at(6)),
        RaiseAlarm(
            at=at(6),
            reason=AlarmReason.USER_CONFIRMED,
            trigger=Trigger.STRESS,
            recording=True,
        ),
    ]


def test_declining_recording_still_alarms() -> None:
    machine = ResponseMachine(ResponseConfig(recording_opt_in=True))
    machine.handle(crossed())
    machine.handle(DistressConfirmed(at=at(4)))

    effects = machine.handle(RecordingChoice(at=at(6), accepted=False))

    assert machine.state is ResponseState.ALARM
    assert effects[-1] == RaiseAlarm(
        at=at(6),
        reason=AlarmReason.USER_CONFIRMED,
        trigger=Trigger.STRESS,
        recording=False,
    )


def test_unanswered_recording_prompt_alarms_without_capture() -> None:
    machine = ResponseMachine(ResponseConfig(recording_opt_in=True))
    machine.handle(crossed())
    machine.handle(DistressConfirmed(at=at(4)))

    effects = machine.handle(Tick(at=at(19)))

    assert machine.state is ResponseState.ALARM
    assert effects[-1] == RaiseAlarm(
        at=at(19),
        reason=AlarmReason.USER_CONFIRMED,
        trigger=Trigger.STRESS,
        recording=False,
    )


def test_resolving_an_alarm_stops_the_recording() -> None:
    machine = ResponseMachine(ResponseConfig(recording_opt_in=True))
    machine.handle(crossed())
    machine.handle(DistressConfirmed(at=at(4)))
    machine.handle(RecordingChoice(at=at(6), accepted=True))

    effects = machine.handle(Dismissed(at=at(40)))

    assert machine.state is ResponseState.IDLE
    assert effects == [DismissPrompt(), StopRecording(at=at(40))]
    assert not machine.recording


def test_repeated_detections_do_not_restart_the_countdown() -> None:
    machine = ResponseMachine()
    machine.handle(crossed())

    assert machine.handle(ThresholdCrossed(at=at(5), trigger=Trigger.STRESS, confidence=0.9)) == []
    assert machine.deadline == at(15)


def test_countdown_must_stay_inside_the_ten_to_twenty_second_band() -> None:
    with pytest.raises(ValueError, match="between 10 and 20"):
        ResponseConfig(countdown=timedelta(seconds=45))


def test_late_dismissal_cannot_suppress_the_no_response_alarm() -> None:
    machine = ResponseMachine()
    machine.handle(crossed(Trigger.IMPACT))

    effects = machine.handle(Dismissed(at=at(16)))

    assert machine.state is ResponseState.ALARM
    assert effects[-1] == RaiseAlarm(
        at=at(16),
        reason=AlarmReason.NO_RESPONSE,
        trigger=Trigger.IMPACT,
        recording=False,
    )


def test_late_distress_confirmation_alarms_rather_than_prompting() -> None:
    machine = ResponseMachine(ResponseConfig(recording_opt_in=True))
    machine.handle(crossed())

    effects = machine.handle(DistressConfirmed(at=at(20)))

    assert machine.state is ResponseState.ALARM
    assert effects[-1].reason is AlarmReason.NO_RESPONSE


def test_recording_prompt_can_be_dismissed() -> None:
    machine = ResponseMachine(ResponseConfig(recording_opt_in=True))
    machine.handle(crossed())
    machine.handle(DistressConfirmed(at=at(4)))

    effects = machine.handle(Dismissed(at=at(8)))

    assert machine.state is ResponseState.IDLE
    assert effects == [DismissPrompt()]


def test_late_dismissal_of_the_recording_prompt_still_alarms() -> None:
    machine = ResponseMachine(ResponseConfig(recording_opt_in=True))
    machine.handle(crossed())
    machine.handle(DistressConfirmed(at=at(4)))

    effects = machine.handle(Dismissed(at=at(20)))

    assert machine.state is ResponseState.ALARM
    assert effects[-1].reason is AlarmReason.USER_CONFIRMED
