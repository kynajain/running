import httpx
import pytest

from running.telephony.elevenlabs import (
    ConversationStatus,
    ElevenLabsAPIError,
    ElevenLabsClient,
)
from running.telephony.twilio_sms import TwilioAPIError, TwilioSMSClient


async def test_conversation_token_and_signed_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"token": "token", "conversation_id": "c"})
        return httpx.Response(200, json={"signed_url": "wss://example/session"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.elevenlabs.io",
    )
    elevenlabs = ElevenLabsClient("key", client=client)
    token = await elevenlabs.get_conversation_token("agent")
    signed = await elevenlabs.get_signed_url("agent")
    assert token.token == "token"
    assert signed.signed_url == "wss://example/session"
    await client.aclose()


async def test_conversation_get_5xx_retries_then_succeeds() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, text="temporary")
        return httpx.Response(200, json={"status": "done", "transcript": []})

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.elevenlabs.io",
    )
    elevenlabs = ElevenLabsClient("key", client=client, sleep=record_delay)
    details = await elevenlabs.get_conversation("conversation-1")
    assert details.status == ConversationStatus.DONE
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
        transport=httpx.MockTransport(handler),
        base_url="https://api.elevenlabs.io",
    )
    elevenlabs = ElevenLabsClient("key", client=client)
    with pytest.raises(ElevenLabsAPIError):
        await elevenlabs.get_conversation("conversation-1")
    assert attempts == 1
    await client.aclose()


async def test_twilio_sms_error_type_and_lifecycle() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(400, text="bad")),
        base_url="https://api.twilio.com",
    )
    twilio = TwilioSMSClient("sid", "token", client=client)
    with pytest.raises(TwilioAPIError):
        await twilio.send_sms("+441111111111", "+442222222222", "hello")
    await twilio.aclose()
    assert client.is_closed is False
    await client.aclose()
