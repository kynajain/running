"""Token-minting server for in-app safety conversations.

The browser and the iOS app never see the ElevenLabs API key: they ask this
server for a short-lived WebRTC conversation token and connect directly to
ElevenLabs with it.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from running.app.incident import (
    AcknowledgeResponse,
    CurrentIncidentResponse,
    IncidentManager,
    IncidentNotFoundError,
    IncidentSummary,
    IncidentView,
    SessionNotFoundError,
    SessionPayload,
    SessionResponse,
    TriggerPayload,
    TriggerResponse,
)
from running.telephony.elevenlabs import ElevenLabsAPIError, ElevenLabsClient
from running.telephony.twilio_sms import TwilioSMSClient

STATIC_DIR = Path(__file__).parent / "static"
_E164 = re.compile(r"^\+[1-9]\d{1,14}$")


class AppConfigurationError(RuntimeError):
    """Raised when the in-app call server is missing configuration."""


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    elevenlabs_api_key: SecretStr
    app_token: SecretStr
    runner_agent_id: str
    twilio_account_sid: SecretStr | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_from_number: str | None = None
    contact_phone_number: str | None = None
    escalation_delay_seconds: float = Field(default=120.0, gt=0)
    session_max_seconds: float = Field(default=90.0, gt=0)

    @classmethod
    def from_env(cls) -> AppConfig:
        names = {
            "ELEVENLABS_API_KEY": "elevenlabs_api_key",
            "ELEVENLABS_AGENT_ID": "runner_agent_id",
            "APP_TOKEN": "app_token",
        }
        missing = [env_name for env_name in names if not os.environ.get(env_name)]
        if missing:
            raise AppConfigurationError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )
        sms_names = (
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_FROM_NUMBER",
            "CONTACT_PHONE_NUMBER",
        )
        sms_present = [bool(os.environ.get(name)) for name in sms_names]
        if any(sms_present) and not all(sms_present):
            raise AppConfigurationError(
                "SMS configuration must provide all of: " + ", ".join(sms_names)
            )
        return cls(
            elevenlabs_api_key=SecretStr(os.environ["ELEVENLABS_API_KEY"]),
            runner_agent_id=os.environ["ELEVENLABS_AGENT_ID"],
            app_token=SecretStr(os.environ["APP_TOKEN"]),
            twilio_account_sid=(
                SecretStr(os.environ["TWILIO_ACCOUNT_SID"])
                if os.environ.get("TWILIO_ACCOUNT_SID")
                else None
            ),
            twilio_auth_token=(
                SecretStr(os.environ["TWILIO_AUTH_TOKEN"])
                if os.environ.get("TWILIO_AUTH_TOKEN")
                else None
            ),
            twilio_from_number=os.environ.get("TWILIO_FROM_NUMBER"),
            contact_phone_number=os.environ.get("CONTACT_PHONE_NUMBER"),
            escalation_delay_seconds=float(os.environ.get("ESCALATION_DELAY_SECONDS", "120")),
            session_max_seconds=float(os.environ.get("SESSION_MAX_SECONDS", "90")),
        )

    @field_validator("twilio_from_number", "contact_phone_number")
    @classmethod
    def validate_phone_number(cls, value: str | None) -> str | None:
        if value is not None and not _E164.fullmatch(value):
            raise ValueError("phone number must use E.164 format")
        return value

    @model_validator(mode="after")
    def validate_sms_configuration(self) -> AppConfig:
        sms_configured = (
            self.twilio_account_sid is not None,
            self.twilio_auth_token is not None,
            self.twilio_from_number is not None,
            self.contact_phone_number is not None,
        )
        if any(sms_configured) and not all(sms_configured):
            raise ValueError("SMS configuration must provide all four values")
        return self

    def __str__(self) -> str:
        return "AppConfig(<credentials redacted>)"

    def __repr__(self) -> str:
        return self.__str__()


class ConversationTokenPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    conversation_id: str | None = None
    agent_id: str


class SignedUrlPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signed_url: str
    agent_id: str


Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


def create_app(
    config: AppConfig,
    client: httpx.AsyncClient | None = None,
    sms_client: httpx.AsyncClient | None = None,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
) -> FastAPI:
    app = FastAPI(title="Runner safety in-app calls")
    elevenlabs = ElevenLabsClient(config.elevenlabs_api_key.get_secret_value(), client=client)
    twilio: TwilioSMSClient | None = None
    if (
        config.twilio_account_sid is not None
        and config.twilio_auth_token is not None
        and config.twilio_from_number is not None
        and config.contact_phone_number is not None
    ):
        twilio = TwilioSMSClient(
            config.twilio_account_sid.get_secret_value(),
            config.twilio_auth_token.get_secret_value(),
            client=sms_client,
        )
    incidents = IncidentManager(
        runner_agent_id=config.runner_agent_id,
        contact_phone_number=config.contact_phone_number,
        twilio_from_number=config.twilio_from_number,
        elevenlabs=elevenlabs,
        twilio=twilio,
        escalation_delay_seconds=config.escalation_delay_seconds,
        session_max_seconds=config.session_max_seconds,
        clock=clock,
        sleep=sleep,
    )
    app.state.incidents = incidents

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {config.app_token.get_secret_value()}"
        if authorization is None or not hmac.compare_digest(
            authorization.encode("utf-8"), expected.encode("utf-8")
        ):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/api/conversation-token", response_model=ConversationTokenPayload)
    async def conversation_token(
        leg: str = "runner",
        _: None = Depends(require_auth),
    ) -> ConversationTokenPayload:
        if leg != "runner":
            raise HTTPException(status_code=400, detail="Only the runner leg has an in-app session")
        agent_id = config.runner_agent_id
        try:
            token = await elevenlabs.get_conversation_token(agent_id)
        except ElevenLabsAPIError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"ElevenLabs rejected the request: {exc.status_code}",
            ) from exc
        return ConversationTokenPayload(
            token=token.token,
            conversation_id=token.conversation_id,
            agent_id=agent_id,
        )

    @app.get("/api/signed-url", response_model=SignedUrlPayload)
    async def signed_url(
        leg: str = "runner",
        _: None = Depends(require_auth),
    ) -> SignedUrlPayload:
        """WebSocket credential for the text-only fallback when there is no microphone."""
        if leg != "runner":
            raise HTTPException(status_code=400, detail="Only the runner leg has an in-app session")
        agent_id = config.runner_agent_id
        try:
            signed = await elevenlabs.get_signed_url(agent_id)
        except ElevenLabsAPIError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"ElevenLabs rejected the request: {exc.status_code}",
            ) from exc
        return SignedUrlPayload(signed_url=signed.signed_url, agent_id=agent_id)

    @app.post("/api/trigger", response_model=TriggerResponse, status_code=201)
    @app.post("/api/panic", response_model=TriggerResponse, status_code=201)
    async def trigger(
        payload: TriggerPayload,
        _: None = Depends(require_auth),
    ) -> TriggerResponse:
        incident = await incidents.create(payload)
        return TriggerResponse(
            incident_id=incident.incident_id,
            state=incident.state,
            message=incident.message,
            latitude=incident.latitude,
            longitude=incident.longitude,
            fix_age_seconds=incident.fix_age_seconds,
            pending_session_id=incident.pending_session_id,
        )

    @app.get("/api/incident/current", response_model=CurrentIncidentResponse)
    async def current(
        role: str = "runner",
        _: None = Depends(require_auth),
    ) -> CurrentIncidentResponse:
        if role not in {"runner", "contact"}:
            raise HTTPException(status_code=400, detail="Invalid role")
        incident = await incidents.current(role)
        if incident is None:
            return CurrentIncidentResponse(incident=None)
        return CurrentIncidentResponse(
            incident=IncidentSummary(
                incident_id=incident.incident_id,
                message=incident.message,
                state=incident.state,
                latitude=incident.latitude,
                longitude=incident.longitude,
                fix_age_seconds=incident.fix_age_seconds,
                pending_session_id=incident.pending_session_id,
            )
        )

    @app.post("/api/incident/{incident_id}/session", response_model=SessionResponse)
    async def session(
        incident_id: str,
        payload: SessionPayload,
        _: None = Depends(require_auth),
    ) -> SessionResponse:
        try:
            outcome = await incidents.attach_session(incident_id, payload)
        except IncidentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Incident not found") from exc
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        return SessionResponse(outcome=outcome)

    @app.post("/api/incident/{incident_id}/acknowledge")
    async def acknowledge(
        incident_id: str,
        _: None = Depends(require_auth),
    ) -> AcknowledgeResponse:
        try:
            return await incidents.acknowledge(incident_id)
        except (IncidentNotFoundError, SessionNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="Incident not found") from exc

    @app.post("/api/incident/{incident_id}/session/{session_id}/engagement")
    async def engagement(
        incident_id: str,
        session_id: str,
        _: None = Depends(require_auth),
    ) -> AcknowledgeResponse:
        try:
            await incidents.report_engagement(incident_id, session_id)
            return await incidents.acknowledge(incident_id)
        except (IncidentNotFoundError, SessionNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="Incident or session not found") from exc

    @app.get("/api/incident/{incident_id}", response_model=IncidentView)
    async def incident(
        incident_id: str,
        _: None = Depends(require_auth),
    ) -> IncidentView:
        try:
            return await incidents.view(incident_id)
        except IncidentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Incident not found") from exc

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await incidents.close()
        await elevenlabs.aclose()
        if twilio is not None:
            await twilio.aclose()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        create_app(AppConfig.from_env()),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
