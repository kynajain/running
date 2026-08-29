import json
import sys
from datetime import UTC, datetime

import httpx
import pytest

from running.cli import main
from running.models import GeoPoint
from running.telephony.config import TelephonyConfig, TelephonyConfigurationError
from running.telephony.elevenlabs import ElevenLabsAPIError, ElevenLabsClient
from running.telephony.escalation import EscalationService, SafetyAlert, sms_body
from running.telephony.twilio_sms import TwilioAPIError, TwilioSMSClient


def config() -> TelephonyConfig:
    return TelephonyConfig(
        elevenlabs_api_key="test-key",
        elevenlabs_runner_agent_id="runner-agent",
        elevenlabs_contact_agent_id="contact-agent",
        elevenlabs_agent_phone_number_id="phone-id",
        twilio_account_sid="account-sid",
        twilio_auth_token="auth-token",
        twilio_from_number="+441234567890",
        runner_phone_number="+441111111111",
        emergency_contact_phone_number="+442222222222",
    )


def alert() -> SafetyAlert:
    timestamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    return SafetyAlert(
        location=GeoPoint(lat=51.5387, lon=-0.0166, timestamp=timestamp),
        timestamp=timestamp,
    )


async def test_elevenlabs_happy_path() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("outbound-call"):
            return httpx.Response(
                200,
                json={"success": True, "conversation_id": "conversation-1", "callSid": "CA1"},
            )
        return httpx.Response(
            200,
            json={
                "agent_id": "runner-agent",
                "status": "done",
                "metadata": {"accepted_time_unix_secs": 10, "call_duration_secs": 20},
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.elevenlabs.io"
    )
    elevenlabs = ElevenLabsClient("test-key", client=client)
    response = await elevenlabs.start_outbound_call("agent", "phone", "+441111111111")
    details = await elevenlabs.get_conversation(response.conversation_id or "")
    assert response.call_sid == "CA1"
    assert details.status.value == "done"
    assert requests[0].headers["xi-api-key"] == "test-key"
    assert json.loads(requests[0].content)["call_recording_enabled"] is False
    await client.aclose()


async def test_runner_no_answer_calls_contact_and_sends_sms() -> None:
    eleven_requests: list[httpx.Request] = []
    sms_requests: list[httpx.Request] = []

    def eleven_handler(request: httpx.Request) -> httpx.Response:
        eleven_requests.append(request)
        if request.url.path.endswith("outbound-call"):
            call_count = len(
                [item for item in eleven_requests if item.url.path.endswith("outbound-call")]
            )
            return httpx.Response(200, json={"conversation_id": f"conversation-{call_count}"})
        return httpx.Response(200, json={"status": "done", "metadata": {}})

    def sms_handler(request: httpx.Request) -> httpx.Response:
        sms_requests.append(request)
        return httpx.Response(201, json={"sid": "SM1", "status": "queued"})

    eleven_client = httpx.AsyncClient(
        transport=httpx.MockTransport(eleven_handler), base_url="https://api.elevenlabs.io"
    )
    sms_client = httpx.AsyncClient(
        transport=httpx.MockTransport(sms_handler), base_url="https://api.twilio.com"
    )
    service = EscalationService(
        config(),
        ElevenLabsClient("test-key", client=eleven_client),
        TwilioSMSClient("account-sid", "auth-token", client=sms_client),
    )
    result = await service.escalate(alert())
    outcomes = {step.name: step.outcome for step in result.steps}
    assert result.runner_engaged is False
    assert outcomes["contact_call"] == "initiated"
    assert outcomes["contact_sms"] == "sent"
    assert "https://maps.google.com/?q=51.5387,-0.0166" in sms_body(alert())
    assert len(sms_requests) == 1
    assert len([item for item in eleven_requests if item.url.path.endswith("outbound-call")]) == 2
    await eleven_client.aclose()
    await sms_client.aclose()


async def test_accepted_runner_call_after_ring_timeout_does_not_escalate() -> None:
    now = 100.0
    contact_calls = 0
    sms_calls = 0

    def eleven_handler(request: httpx.Request) -> httpx.Response:
        nonlocal now, contact_calls
        if request.url.path.endswith("outbound-call"):
            contact_calls += 1
            return httpx.Response(200, json={"conversation_id": "runner"})
        now = 131.0
        return httpx.Response(
            200,
            json={"status": "in-progress", "metadata": {"accepted_time_unix_secs": 10}},
        )

    def sms_handler(request: httpx.Request) -> httpx.Response:
        nonlocal sms_calls
        sms_calls += 1
        return httpx.Response(201, json={"sid": "SM1"})

    eleven_client = httpx.AsyncClient(
        transport=httpx.MockTransport(eleven_handler), base_url="https://api.elevenlabs.io"
    )
    sms_client = httpx.AsyncClient(
        transport=httpx.MockTransport(sms_handler), base_url="https://api.twilio.com"
    )
    service = EscalationService(
        config(),
        ElevenLabsClient("key", client=eleven_client),
        TwilioSMSClient("sid", "token", client=sms_client),
        ring_timeout=30.0,
        clock=lambda: now,
    )
    result = await service.escalate(alert())
    assert result.runner_engaged is True
    assert {step.name: step.outcome for step in result.steps}["runner_poll"] == "answered"
    assert contact_calls == 1
    assert sms_calls == 0
    await eleven_client.aclose()
    await sms_client.aclose()


async def test_unaccepted_runner_call_past_ring_timeout_escalates() -> None:
    now = 100.0
    contact_calls = 0
    sms_calls = 0

    def eleven_handler(request: httpx.Request) -> httpx.Response:
        nonlocal now, contact_calls
        if request.url.path.endswith("outbound-call"):
            contact_calls += 1
            return httpx.Response(200, json={"conversation_id": "runner"})
        now = 131.0
        return httpx.Response(200, json={"status": "in-progress", "metadata": {}})

    def sms_handler(request: httpx.Request) -> httpx.Response:
        nonlocal sms_calls
        sms_calls += 1
        return httpx.Response(201, json={"sid": "SM1"})

    eleven_client = httpx.AsyncClient(
        transport=httpx.MockTransport(eleven_handler), base_url="https://api.elevenlabs.io"
    )
    sms_client = httpx.AsyncClient(
        transport=httpx.MockTransport(sms_handler), base_url="https://api.twilio.com"
    )
    service = EscalationService(
        config(),
        ElevenLabsClient("key", client=eleven_client),
        TwilioSMSClient("sid", "token", client=sms_client),
        ring_timeout=30.0,
        clock=lambda: now,
    )
    result = await service.escalate(alert())
    assert result.runner_engaged is False
    assert {step.name: step.outcome for step in result.steps}["runner_poll"] == "timeout"
    assert contact_calls == 2
    assert sms_calls == 1
    await eleven_client.aclose()
    await sms_client.aclose()


async def test_elevenlabs_429_retries_with_retry_after() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.25"})
        return httpx.Response(200, json={"success": True, "conversation_id": "c"})

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.elevenlabs.io"
    )
    elevenlabs = ElevenLabsClient("key", client=client, sleep=record_delay)
    await elevenlabs.start_outbound_call("a", "p", "+441111111111")
    assert attempts == 2
    assert delays == [0.25]
    await client.aclose()


async def test_twilio_5xx_retries() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(502, text="temporary")
        return httpx.Response(201, json={"sid": "SM1"})

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.twilio.com"
    )
    twilio = TwilioSMSClient("sid", "token", client=client, sleep=record_delay)
    await twilio.send_sms("+441111111111", "+442222222222", "hello")
    assert attempts == 2
    assert len(delays) == 1
    await client.aclose()


async def test_four_hundred_error_does_not_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, text="bad request")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.elevenlabs.io"
    )
    elevenlabs = ElevenLabsClient("key", client=client)
    with pytest.raises(ElevenLabsAPIError) as error:
        await elevenlabs.start_outbound_call("a", "p", "+441111111111")
    assert error.value.status_code == 400
    assert attempts == 1
    await client.aclose()


async def test_sms_failure_does_not_prevent_contact_call() -> None:
    def eleven_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("outbound-call"):
            return httpx.Response(200, json={"conversation_id": "c"})
        return httpx.Response(200, json={"status": "done", "metadata": {}})

    def sms_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid destination")

    eleven_client = httpx.AsyncClient(
        transport=httpx.MockTransport(eleven_handler), base_url="https://api.elevenlabs.io"
    )
    sms_client = httpx.AsyncClient(
        transport=httpx.MockTransport(sms_handler), base_url="https://api.twilio.com"
    )
    service = EscalationService(
        config(),
        ElevenLabsClient("key", client=eleven_client),
        TwilioSMSClient("sid", "token", client=sms_client, max_attempts=1),
    )
    result = await service.escalate(alert())
    outcomes = {step.name: step.outcome for step in result.steps}
    assert outcomes["contact_call"] == "initiated"
    assert outcomes["contact_sms"] == "error"
    await eleven_client.aclose()
    await sms_client.aclose()


async def test_contact_call_failure_does_not_prevent_sms() -> None:
    def eleven_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("outbound-call"):
            return httpx.Response(500, text="unavailable")
        return httpx.Response(200, json={"status": "done", "metadata": {}})

    sms_calls = 0

    def sms_handler(request: httpx.Request) -> httpx.Response:
        nonlocal sms_calls
        sms_calls += 1
        return httpx.Response(201, json={"sid": "SM1"})

    eleven_client = httpx.AsyncClient(
        transport=httpx.MockTransport(eleven_handler), base_url="https://api.elevenlabs.io"
    )
    sms_client = httpx.AsyncClient(
        transport=httpx.MockTransport(sms_handler), base_url="https://api.twilio.com"
    )
    service = EscalationService(
        config(),
        ElevenLabsClient("key", client=eleven_client, max_attempts=1),
        TwilioSMSClient("sid", "token", client=sms_client),
    )
    result = await service.escalate(alert())
    outcomes = {step.name: step.outcome for step in result.steps}
    assert outcomes["contact_call"] == "error"
    assert outcomes["contact_sms"] == "sent"
    assert sms_calls == 1
    await eleven_client.aclose()
    await sms_client.aclose()


def test_config_missing_env_names_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(TelephonyConfigurationError, match="ELEVENLABS_API_KEY"):
        TelephonyConfig.from_env()


def test_config_repr_redacts_secrets() -> None:
    value = config()
    assert "test-key" not in str(value)
    assert "auth-token" not in repr(value)


def test_alert_dry_run_makes_no_http_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["running", "alert", "--lat", "51.5387", "--lon", "-0.0166", "--dry-run"],
    )
    main()
    output = capsys.readouterr().out
    assert "zero HTTP calls" in output
    assert "https://maps.google.com/?q=51.5387,-0.0166" in output
    assert "ELEVENLABS_RUNNER_AGENT_ID" in output


@pytest.mark.parametrize(
    ("client_factory", "secret"),
    [
        (lambda client: ElevenLabsClient("key", client=client), "key"),
        (lambda client: TwilioSMSClient("sid", "token", client=client), "token"),
    ],
)
async def test_injected_client_is_not_closed(client_factory, secret: str) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    instance = client_factory(client)
    await instance.aclose()
    assert client.is_closed is False
    assert secret
    await client.aclose()


@pytest.mark.parametrize(
    "client_factory",
    [
        lambda: ElevenLabsClient("key"),
        lambda: TwilioSMSClient("sid", "token"),
    ],
)
async def test_owned_client_is_closed(client_factory) -> None:
    instance = client_factory()
    client = instance.client
    await instance.aclose()
    assert client.is_closed


async def test_twilio_4xx_error_type() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(400, text="bad")),
        base_url="https://api.twilio.com",
    )
    twilio = TwilioSMSClient("sid", "token", client=client)
    with pytest.raises(TwilioAPIError):
        await twilio.send_sms("+441111111111", "+442222222222", "hello")
    await client.aclose()
