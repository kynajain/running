import httpx
import pytest

from running.app.server import AppConfig, AppConfigurationError, create_app


def app_config() -> AppConfig:
    return AppConfig(
        elevenlabs_api_key="test-api-key",
        runner_agent_id="runner-agent",
        contact_agent_id="contact-agent",
    )


async def request_app(
    handler: httpx.MockTransport,
) -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
    elevenlabs = httpx.AsyncClient(
        transport=handler,
        base_url="https://api.elevenlabs.io",
    )
    app = create_app(app_config(), client=elevenlabs)
    browser = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    return browser, elevenlabs


async def test_conversation_token_selects_contact_agent() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"token": "short-lived-token", "conversation_id": "conversation-1"},
        )

    browser, elevenlabs = await request_app(httpx.MockTransport(handler))
    response = await browser.get("/api/conversation-token?leg=contact")
    assert response.status_code == 200
    assert response.json() == {
        "token": "short-lived-token",
        "conversation_id": "conversation-1",
        "agent_id": "contact-agent",
    }
    assert requests[0].url.params["agent_id"] == "contact-agent"
    await browser.aclose()
    await elevenlabs.aclose()


async def test_signed_url_returns_signed_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"signed_url": "wss://signed.example/session"})

    browser, elevenlabs = await request_app(httpx.MockTransport(handler))
    response = await browser.get("/api/signed-url")
    assert response.status_code == 200
    assert response.json() == {
        "signed_url": "wss://signed.example/session",
        "agent_id": "runner-agent",
    }
    await browser.aclose()
    await elevenlabs.aclose()


async def test_elevenlabs_error_maps_to_502_without_api_key() -> None:
    api_key = "test-api-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=f"invalid request for {api_key}")

    browser, elevenlabs = await request_app(httpx.MockTransport(handler))
    response = await browser.get("/api/conversation-token")
    assert response.status_code == 502
    assert api_key not in response.text
    assert response.json()["detail"] == "ElevenLabs rejected the request: 400"
    await browser.aclose()
    await elevenlabs.aclose()


def test_app_config_missing_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ELEVENLABS_API_KEY",
        "ELEVENLABS_RUNNER_AGENT_ID",
        "ELEVENLABS_CONTACT_AGENT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(AppConfigurationError) as error:
        AppConfig.from_env()
    assert "ELEVENLABS_API_KEY" in str(error.value)
    assert "ELEVENLABS_RUNNER_AGENT_ID" in str(error.value)
    assert "ELEVENLABS_CONTACT_AGENT_ID" in str(error.value)


def test_app_config_repr_redacts_key() -> None:
    config = app_config()
    assert "test-api-key" not in str(config)
    assert "test-api-key" not in repr(config)


async def test_index_serves_demo_html() -> None:
    browser, elevenlabs = await request_app(
        httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    response = await browser.get("/")
    assert response.status_code == 200
    assert "Runner safety" in response.text
    assert "Simulate incident" in response.text
    await browser.aclose()
    await elevenlabs.aclose()
