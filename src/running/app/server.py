"""Token-minting server for in-app safety conversations.

The browser and the iOS app never see the ElevenLabs API key: they ask this
server for a short-lived WebRTC conversation token and connect directly to
ElevenLabs with it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, SecretStr

from running.telephony.elevenlabs import ElevenLabsAPIError, ElevenLabsClient

STATIC_DIR = Path(__file__).parent / "static"

Leg = Literal["runner", "contact"]


class AppConfigurationError(RuntimeError):
    """Raised when the in-app call server is missing configuration."""


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    elevenlabs_api_key: SecretStr
    runner_agent_id: str
    contact_agent_id: str

    @classmethod
    def from_env(cls) -> AppConfig:
        names = {
            "ELEVENLABS_API_KEY": "elevenlabs_api_key",
            "ELEVENLABS_RUNNER_AGENT_ID": "runner_agent_id",
            "ELEVENLABS_CONTACT_AGENT_ID": "contact_agent_id",
        }
        missing = [env_name for env_name in names if not os.environ.get(env_name)]
        if missing:
            raise AppConfigurationError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )
        return cls(
            elevenlabs_api_key=SecretStr(os.environ["ELEVENLABS_API_KEY"]),
            runner_agent_id=os.environ["ELEVENLABS_RUNNER_AGENT_ID"],
            contact_agent_id=os.environ["ELEVENLABS_CONTACT_AGENT_ID"],
        )

    def agent_id(self, leg: Leg) -> str:
        return self.runner_agent_id if leg == "runner" else self.contact_agent_id

    def __str__(self) -> str:
        return "AppConfig(<credentials redacted>)"

    def __repr__(self) -> str:
        return self.__str__()


class ConversationTokenPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    conversation_id: str | None
    agent_id: str


class SignedUrlPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signed_url: str
    agent_id: str


def create_app(config: AppConfig, client: httpx.AsyncClient | None = None) -> FastAPI:
    app = FastAPI(title="Runner safety in-app calls")
    elevenlabs = ElevenLabsClient(config.elevenlabs_api_key.get_secret_value(), client=client)

    @app.get("/api/conversation-token", response_model=ConversationTokenPayload)
    async def conversation_token(leg: Leg = "runner") -> ConversationTokenPayload:
        agent_id = config.agent_id(leg)
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
    async def signed_url(leg: Leg = "runner") -> SignedUrlPayload:
        """WebSocket credential for the text-only fallback when there is no microphone."""
        agent_id = config.agent_id(leg)
        try:
            signed = await elevenlabs.get_signed_url(agent_id)
        except ElevenLabsAPIError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"ElevenLabs rejected the request: {exc.status_code}",
            ) from exc
        return SignedUrlPayload(signed_url=signed.signed_url, agent_id=agent_id)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await elevenlabs.aclose()

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
