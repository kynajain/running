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
    assert created["message"] == "I've fallen"
    assert created["latitude"] == 51.5
    assert created["longitude"] == -0.1
    assert created["fix_age_seconds"] == 180
    assert created["pending_session_id"]
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


async def test_non_ascii_authorization_header_returns_401() -> None:
    browser, elevenlabs, sms, app = await make_app(
        lambda _: httpx.Response(200, json={"token": "unused"}),
    )
    request = httpx.Request(
        "GET",
        "http://testserver/api/incident/current",
        headers=[(b"authorization", b"Bearer caf\xe9")],
    )
    response = await browser.send(request)
    assert response.status_code == 401
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
    assert acknowledged.json() == {
        "state": "resolved",
        "outcome": "answered",
        "escalation": {"sent": False, "dry_run": False, "error": None},
    }
    detail = await browser.get(f"/api/incident/{created['incident_id']}", headers=AUTH)
    assert detail.json()["state"] == "resolved"
    assert detail.json()["sessions"][0]["outcome"] == "answered"
    await close_clients(browser, elevenlabs, sms, app)


async def test_acknowledge_after_escalation_reports_terminal_state() -> None:
    browser, elevenlabs, sms, app = await make_app(
        lambda _: httpx.Response(200, json={"token": "unused"}),
        twilio_account_sid=None,
        twilio_auth_token=None,
        twilio_from_number=None,
        contact_phone_number=None,
    )
    created = (
        await browser.post(
            "/api/trigger",
            headers=AUTH,
            json={"message": "Help", "latitude": 1, "longitude": 2, "fix_age_seconds": 3},
        )
    ).json()
    manager = app.state.incidents
    manager._cancel_task(manager._incidents[created["incident_id"]].escalation_task)
    manager.sleep = no_sleep
    manager.escalation_delay_seconds = 0
    await manager._escalation_timer(created["incident_id"])
    acknowledged = await browser.post(
        f"/api/incident/{created['incident_id']}/acknowledge",
        headers=AUTH,
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["state"] == "escalated"
    assert acknowledged.json()["escalation"]["dry_run"] is True
    await close_clients(browser, elevenlabs, sms, app)


async def test_engagement_endpoint_marks_session_answered_and_is_idempotent() -> None:
    browser, elevenlabs, sms, app = await make_app(
        lambda _: httpx.Response(200, json={"status": "in-progress", "transcript": []}),
    )
    created = (
        await browser.post(
            "/api/trigger",
            headers=AUTH,
            json={"message": "Help", "latitude": 1, "longitude": 2, "fix_age_seconds": 3},
        )
    ).json()
    session_id = created["pending_session_id"]
    session = await browser.post(
        f"/api/incident/{created['incident_id']}/session",
        headers=AUTH,
        json={"session_id": session_id, "conversation_id": "conversation-1"},
    )
    assert session.json() == {"outcome": None}
    engagement_url = f"/api/incident/{created['incident_id']}/session/{session_id}/engagement"
    first = await browser.post(engagement_url, headers=AUTH)
    second = await browser.post(engagement_url, headers=AUTH)
    assert first.json()["state"] == "resolved"
    assert first.json()["outcome"] == "answered"
    assert second.json() == first.json()
    assert (await browser.get(f"/api/incident/{created['incident_id']}", headers=AUTH)).json()[
        "sessions"
    ][0]["outcome"] == "answered"
    await close_clients(browser, elevenlabs, sms, app)


async def test_engagement_endpoint_requires_auth_and_unknown_ids_404() -> None:
    browser, elevenlabs, sms, app = await make_app(
        lambda _: httpx.Response(200, json={"token": "unused"}),
    )
    unauthorized = await browser.post("/api/incident/unknown/session/unknown/engagement")
    assert unauthorized.status_code == 401
    missing = await browser.post(
        "/api/incident/unknown/session/unknown/engagement",
        headers=AUTH,
    )
    assert missing.status_code == 404
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


async def test_contact_current_view_hides_pending_session() -> None:
    browser, elevenlabs, sms, app = await make_app(
        lambda _: httpx.Response(200, json={"token": "unused"}),
    )
    await browser.post(
        "/api/trigger",
        headers=AUTH,
        json={"message": "Help", "latitude": 1, "longitude": 2, "fix_age_seconds": 3},
    )
    current = await browser.get("/api/incident/current?role=contact", headers=AUTH)
    assert current.status_code == 200
    assert current.json()["incident"]["pending_session_id"] is None
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


def test_app_config_allows_unconfigured_sms(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "ELEVENLABS_API_KEY": "key",
        "ELEVENLABS_AGENT_ID": "agent",
        "APP_TOKEN": "token",
    }
    for name in (
        "ELEVENLABS_API_KEY",
        "ELEVENLABS_AGENT_ID",
        "APP_TOKEN",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "CONTACT_PHONE_NUMBER",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    config = AppConfig.from_env()
    assert config.twilio_account_sid is None
    assert config.contact_phone_number is None


def test_app_config_rejects_partial_sms(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "ELEVENLABS_API_KEY": "key",
        "ELEVENLABS_AGENT_ID": "agent",
        "APP_TOKEN": "token",
        "TWILIO_ACCOUNT_SID": "sid",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    for name in ("TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "CONTACT_PHONE_NUMBER"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(AppConfigurationError, match="SMS configuration"):
        AppConfig.from_env()


def test_app_config_direct_construction_rejects_partial_sms() -> None:
    with pytest.raises(ValueError, match="SMS configuration"):
        app_config(twilio_auth_token=None)


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


async def test_session_cap_repoll_finds_user_turn_after_live_session() -> None:
    responses = iter(
        [
            {"status": "in-progress", "transcript": []},
            {
                "status": "done",
                "transcript": [{"role": "user", "message": "Help", "time_in_call_secs": 4}],
            },
        ]
    )

    async def cap_sleep(seconds: float) -> None:
        if seconds >= 100:
            await asyncio.Event().wait()

    elevenlabs_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=next(responses)),
        ),
        base_url="https://api.elevenlabs.io",
    )
    manager = IncidentManager(
        "runner",
        None,
        None,
        ElevenLabsClient("key", client=elevenlabs_client),
        None,
        session_max_seconds=0,
        cap_repoll_count=5,
        cap_repoll_delay_seconds=0,
        sleep=cap_sleep,
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
    assert incident.sessions[session_id].cap_task is not None
    await incident.sessions[session_id].cap_task
    view = await manager.view(incident.incident_id)
    assert view.sessions[0].outcome.value == "answered"
    assert view.state.value == "resolved"
    await manager.close()
    await elevenlabs_client.aclose()


async def test_session_cap_exhausts_live_repoll_budget_and_escalates() -> None:
    requests = 0

    def eleven_handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"status": "in-progress", "transcript": []})

    async def cap_sleep(seconds: float) -> None:
        if seconds >= 100:
            await asyncio.Event().wait()

    elevenlabs_client = httpx.AsyncClient(
        transport=httpx.MockTransport(eleven_handler),
        base_url="https://api.elevenlabs.io",
    )
    sms_calls = 0

    def sms_handler(request: httpx.Request) -> httpx.Response:
        nonlocal sms_calls
        sms_calls += 1
        return httpx.Response(201, json={})

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
        session_max_seconds=0,
        cap_repoll_count=5,
        cap_repoll_delay_seconds=0,
        sleep=cap_sleep,
    )
    incident = await manager.create(
        TriggerPayload(message="Help", latitude=1, longitude=2, fix_age_seconds=3)
    )
    manager._cancel_task(incident.escalation_task)
    session_id = incident.pending_session_id
    assert session_id is not None
    await manager.attach_session(
        incident.incident_id,
        SessionPayload(session_id=session_id, conversation_id="conversation-1"),
    )
    assert incident.sessions[session_id].cap_task is not None
    await incident.sessions[session_id].cap_task
    manager.escalation_delay_seconds = 0
    await manager._escalation_timer(incident.incident_id)
    view = await manager.view(incident.incident_id)
    assert view.sessions[0].outcome.value == "unanswered"
    assert view.state.value == "escalated"
    assert requests == 7
    assert sms_calls == 1
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


async def test_timer_error_still_attempts_sms() -> None:
    sms_calls = 0

    def sms_handler(request: httpx.Request) -> httpx.Response:
        nonlocal sms_calls
        sms_calls += 1
        return httpx.Response(201, json={"sid": "SM1"})

    async def broken_sleep(seconds: float) -> None:
        raise RuntimeError("timer broke")

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
        sleep=broken_sleep,
    )
    incident = await manager.create(
        TriggerPayload(message="Help", latitude=51.5, longitude=-0.1, fix_age_seconds=180)
    )
    await manager._escalation_timer(incident.incident_id)
    view = await manager.view(incident.incident_id)
    assert view.state.value == "escalated"
    assert view.escalation.sent is True
    assert "timer broke" in (view.escalation.error or "")
    assert sms_calls == 1
    await manager.close()
    await elevenlabs_client.aclose()
    await sms_client.aclose()


async def test_unconfigured_sms_escalates_as_dry_run(caplog: pytest.LogCaptureFixture) -> None:
    elevenlabs_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
        base_url="https://api.elevenlabs.io",
    )
    manager = IncidentManager(
        "runner",
        None,
        None,
        ElevenLabsClient("key", client=elevenlabs_client),
        None,
        escalation_delay_seconds=0,
        sleep=no_sleep,
    )
    incident = await manager.create(
        TriggerPayload(message="Need help", latitude=51.5, longitude=-0.1, fix_age_seconds=180)
    )
    with caplog.at_level("INFO"):
        await manager._escalation_timer(incident.incident_id)
    view = await manager.view(incident.incident_id)
    assert view.state.value == "escalated"
    assert view.escalation.sent is False
    assert view.escalation.dry_run is True
    assert "Need help" in caplog.text
    assert "https://maps.google.com/?q=51.5,-0.1" in caplog.text
    await manager.close()
    await elevenlabs_client.aclose()


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
