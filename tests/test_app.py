import asyncio
from collections.abc import Callable

import httpx
import pytest

from running.app.incident import IncidentManager, SessionPayload, TriggerPayload
from running.app.server import AppConfig, AppConfigurationError, create_app
from running.telephony.elevenlabs import ElevenLabsClient
from running.telephony.twilio_sms import TwilioSMSClient

APP_TOKEN = "app-token"
AUTH = {"Authorization": f"Bearer {APP_TOKEN}"}


def app_config(**overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "elevenlabs_api_key": "test-api-key",
        "app_token": APP_TOKEN,
        "runner_agent_id": "runner-agent",
        "twilio_account_sid": "account-sid",
        "twilio_auth_token": "auth-token",
        "twilio_from_number": "+441234567890",
        "contact_phone_number": "+442222222222",
    }
    values.update(overrides)
    return AppConfig(**values)


class BlockedSleep:
    async def __call__(self, seconds: float) -> None:
        await asyncio.Event().wait()


async def no_sleep(seconds: float) -> None:
    return None


async def make_app(
    eleven_handler: Callable[[httpx.Request], httpx.Response],
    sms_handler: Callable[[httpx.Request], httpx.Response] | None = None,
    **config_overrides: object,
) -> tuple[httpx.AsyncClient, httpx.AsyncClient, httpx.AsyncClient, object]:
    elevenlabs = httpx.AsyncClient(
        transport=httpx.MockTransport(eleven_handler),
        base_url="https://api.elevenlabs.io",
    )
    sms = httpx.AsyncClient(
        transport=httpx.MockTransport(sms_handler or (lambda _: httpx.Response(201, json={}))),
        base_url="https://api.twilio.com",
    )
    app = create_app(
        app_config(**config_overrides),
        client=elevenlabs,
        sms_client=sms,
        sleep=BlockedSleep(),
    )
    browser = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    return browser, elevenlabs, sms, app


async def close_clients(
    browser: httpx.AsyncClient,
    elevenlabs: httpx.AsyncClient,
    sms: httpx.AsyncClient,
    app: object,
) -> None:
    await app.state.incidents.close()
    await browser.aclose()
    await elevenlabs.aclose()
    await sms.aclose()


async def test_trigger_and_current_contract() -> None:
    browser, elevenlabs, sms, app = await make_app(
        lambda _: httpx.Response(200, json={"token": "unused"}),
    )
    response = await browser.post(
        "/api/trigger",
        headers=AUTH,
        json={
            "message": "I've fallen",
            "latitude": 51.5,
            "longitude": -0.1,
            "fix_age_seconds": 180,
        },
    )
    assert response.status_code == 201
    created = response.json()
    current = await browser.get("/api/incident/current?role=runner", headers=AUTH)
    assert current.status_code == 200
    incident = current.json()["incident"]
    assert incident == {
        "incident_id": created["incident_id"],
        "message": "I've fallen",
        "state": "triggered",
        "latitude": 51.5,
        "longitude": -0.1,
        "fix_age_seconds": 180,
        "pending_session_id": incident["pending_session_id"],
    }
    await close_clients(browser, elevenlabs, sms, app)


async def test_panic_alias_and_authentication() -> None:
    browser, elevenlabs, sms, app = await make_app(
        lambda _: httpx.Response(200, json={"token": "unused"}),
    )
    payload = {
        "message": "Panic",
        "latitude": 51.5,
        "longitude": -0.1,
        "fix_age_seconds": 0,
    }
    assert (await browser.get("/healthz")).status_code == 200
    assert (await browser.post("/api/panic", json=payload)).status_code == 401
    assert (
        await browser.post(
            "/api/panic",
            headers={"Authorization": "Bearer wrong"},
            json=payload,
        )
    ).status_code == 401
    assert (await browser.post("/api/panic", headers=AUTH, json=payload)).status_code == 201
    await close_clients(browser, elevenlabs, sms, app)


async def test_acknowledge_marks_runner_answered() -> None:
    browser, elevenlabs, sms, app = await make_app(
        lambda _: httpx.Response(200, json={"token": "unused"}),
    )
    created = (
        await browser.post(
            "/api/trigger",
            headers=AUTH,
            json={"message": "Help", "latitude": 1, "longitude": 2, "fix_age_seconds": 3},
        )
    ).json()
    acknowledged = await browser.post(
        f"/api/incident/{created['incident_id']}/acknowledge",
        headers=AUTH,
    )
    assert acknowledged.json() == {"state": "resolved", "outcome": "answered"}
    detail = await browser.get(f"/api/incident/{created['incident_id']}", headers=AUTH)
    assert detail.json()["state"] == "resolved"
    assert detail.json()["sessions"][0]["outcome"] == "answered"
    await close_clients(browser, elevenlabs, sms, app)


async def test_user_transcript_answers_session() -> None:
    def eleven_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "in-progress",
                "transcript": [{"role": "user", "message": "I am okay", "time_in_call_secs": 2}],
            },
        )

    browser, elevenlabs, sms, app = await make_app(eleven_handler)
    created = (
        await browser.post(
            "/api/trigger",
            headers=AUTH,
            json={"message": "Help", "latitude": 1, "longitude": 2, "fix_age_seconds": 3},
        )
    ).json()
    current = (await browser.get("/api/incident/current", headers=AUTH)).json()["incident"]
    session = await browser.post(
        f"/api/incident/{created['incident_id']}/session",
        headers=AUTH,
        json={
            "session_id": current["pending_session_id"],
            "conversation_id": "conversation-1",
        },
    )
    assert session.json() == {"outcome": "answered"}
    await close_clients(browser, elevenlabs, sms, app)


async def test_credential_routes_require_auth_and_reject_contact_leg() -> None:
    requests: list[httpx.Request] = []

    def eleven_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"token": "temporary-token"})

    browser, elevenlabs, sms, app = await make_app(eleven_handler)
    assert (await browser.get("/api/conversation-token")).status_code == 401
    contact = await browser.get("/api/conversation-token?leg=contact", headers=AUTH)
    assert contact.status_code == 400
    token = await browser.get("/api/conversation-token?leg=runner", headers=AUTH)
    assert token.status_code == 200
    assert token.json()["agent_id"] == "runner-agent"
    assert requests[0].url.params["agent_id"] == "runner-agent"
    await close_clients(browser, elevenlabs, sms, app)


async def test_signed_url_and_provider_errors_are_safely_mapped() -> None:
    def eleven_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("get-signed-url"):
            return httpx.Response(200, json={"signed_url": "wss://example/session"})
        return httpx.Response(401, text="invalid test-api-key")

    browser, elevenlabs, sms, app = await make_app(eleven_handler)
    signed = await browser.get("/api/signed-url", headers=AUTH)
    assert signed.status_code == 200
    assert signed.json() == {
        "signed_url": "wss://example/session",
        "agent_id": "runner-agent",
    }
    error = await browser.get("/api/conversation-token", headers=AUTH)
    assert error.status_code == 502
    assert "test-api-key" not in error.text
    await close_clients(browser, elevenlabs, sms, app)


def test_app_config_missing_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ELEVENLABS_API_KEY", "ELEVENLABS_AGENT_ID", "APP_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(AppConfigurationError) as error:
        AppConfig.from_env()
    message = str(error.value)
    assert "ELEVENLABS_API_KEY" in message
    assert "ELEVENLABS_AGENT_ID" in message
    assert "APP_TOKEN" in message


def test_app_config_repr_redacts_key() -> None:
    config = app_config()
    assert "test-api-key" not in str(config)
    assert "test-api-key" not in repr(config)


async def test_incident_manager_silent_session_fails_at_cap() -> None:
    elevenlabs_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"status": "in-progress", "transcript": []})
        ),
        base_url="https://api.elevenlabs.io",
    )
    sms_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(201, json={})),
        base_url="https://api.twilio.com",
    )
    manager = IncidentManager(
        "runner",
        "+442222222222",
        "+441234567890",
        ElevenLabsClient("key", client=elevenlabs_client),
        TwilioSMSClient("sid", "token", client=sms_client),
        session_max_seconds=90,
        sleep=no_sleep,
    )
    incident = await manager.create(
        TriggerPayload(message="Help", latitude=1, longitude=2, fix_age_seconds=3)
    )
    session_id = incident.pending_session_id
    assert session_id is not None
    await manager.attach_session(
        incident.incident_id,
        SessionPayload(session_id=session_id, conversation_id="conversation-1"),
    )
    await manager._session_cap(incident.incident_id, session_id)
    assert (await manager.view(incident.incident_id)).sessions[0].outcome.value == "unanswered"
    await manager.close()
    await elevenlabs_client.aclose()
    await sms_client.aclose()


async def test_failed_session_has_failed_outcome_and_escalates() -> None:
    sms_calls = 0

    def eleven_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "failed", "transcript": []})

    def sms_handler(request: httpx.Request) -> httpx.Response:
        nonlocal sms_calls
        sms_calls += 1
        return httpx.Response(201, json={"sid": "SM1"})

    elevenlabs_client = httpx.AsyncClient(
        transport=httpx.MockTransport(eleven_handler),
        base_url="https://api.elevenlabs.io",
    )
    sms_client = httpx.AsyncClient(
        transport=httpx.MockTransport(sms_handler),
        base_url="https://api.twilio.com",
    )

    manager = IncidentManager(
        "runner",
        "+442222222222",
        "+441234567890",
        ElevenLabsClient("key", client=elevenlabs_client),
        TwilioSMSClient("sid", "token", client=sms_client),
        escalation_delay_seconds=0,
        sleep=no_sleep,
    )
    incident = await manager.create(
        TriggerPayload(message="Help", latitude=1, longitude=2, fix_age_seconds=180)
    )
    outcome = await manager.attach_session(
        incident.incident_id,
        SessionPayload(session_id=incident.pending_session_id, conversation_id="conversation-1"),
    )
    assert outcome.value == "failed"
    await manager._escalation_timer(incident.incident_id)
    view = await manager.view(incident.incident_id)
    assert view.state.value == "escalated"
    assert view.escalation.sent is True
    assert sms_calls == 1
    await manager.close()
    await elevenlabs_client.aclose()
    await sms_client.aclose()


async def test_escalation_sms_is_sent_once() -> None:
    sms_calls = 0

    def sms_handler(request: httpx.Request) -> httpx.Response:
        nonlocal sms_calls
        sms_calls += 1
        return httpx.Response(201, json={"sid": "SM1"})

    elevenlabs_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
        base_url="https://api.elevenlabs.io",
    )
    sms_client = httpx.AsyncClient(
        transport=httpx.MockTransport(sms_handler),
        base_url="https://api.twilio.com",
    )

    manager = IncidentManager(
        "runner",
        "+442222222222",
        "+441234567890",
        ElevenLabsClient("key", client=elevenlabs_client),
        TwilioSMSClient("sid", "token", client=sms_client),
        sleep=no_sleep,
    )
    incident = await manager.create(
        TriggerPayload(message="Help", latitude=51.5, longitude=-0.1, fix_age_seconds=180)
    )
    await manager._escalation_timer(incident.incident_id)
    await manager._escalation_timer(incident.incident_id)
    assert sms_calls == 1
    await manager.close()
    await elevenlabs_client.aclose()
    await sms_client.aclose()


async def test_session_cap_does_not_downgrade_answered_session() -> None:
    def eleven_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "in-progress",
                "transcript": [{"role": "user", "message": "Okay", "time_in_call_secs": 1}],
            },
        )

    elevenlabs_client = httpx.AsyncClient(
        transport=httpx.MockTransport(eleven_handler),
        base_url="https://api.elevenlabs.io",
    )
    sms_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(201, json={})),
        base_url="https://api.twilio.com",
    )
    manager = IncidentManager(
        "runner",
        "+442222222222",
        "+441234567890",
        ElevenLabsClient("key", client=elevenlabs_client),
        TwilioSMSClient("sid", "token", client=sms_client),
        sleep=BlockedSleep(),
    )
    incident = await manager.create(
        TriggerPayload(message="Help", latitude=1, longitude=2, fix_age_seconds=3)
    )
    session_id = incident.pending_session_id
    assert session_id is not None
    await manager.attach_session(
        incident.incident_id,
        SessionPayload(session_id=session_id, conversation_id="conversation-1"),
    )
    await manager._session_cap(incident.incident_id, session_id)
    assert (await manager.view(incident.incident_id)).sessions[0].outcome.value == "answered"
    await manager.close()
    await elevenlabs_client.aclose()
    await sms_client.aclose()
