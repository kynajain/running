"""Environment-backed configuration for safety telephony."""

from __future__ import annotations

import os
import re
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

_E164 = re.compile(r"^\+[1-9]\d{1,14}$")


class TelephonyConfigurationError(RuntimeError):
    """Raised when telephony configuration is incomplete."""


class TelephonyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    elevenlabs_api_key: SecretStr
    elevenlabs_runner_agent_id: str
    elevenlabs_contact_agent_id: str
    elevenlabs_agent_phone_number_id: str
    twilio_account_sid: SecretStr
    twilio_auth_token: SecretStr
    twilio_from_number: str
    runner_phone_number: str
    emergency_contact_phone_number: str

    _phone_fields: ClassVar[tuple[str, ...]] = (
        "twilio_from_number",
        "runner_phone_number",
        "emergency_contact_phone_number",
    )

    @classmethod
    def from_env(cls) -> TelephonyConfig:
        names = {
            "elevenlabs_api_key": "ELEVENLABS_API_KEY",
            "elevenlabs_runner_agent_id": "ELEVENLABS_RUNNER_AGENT_ID",
            "elevenlabs_contact_agent_id": "ELEVENLABS_CONTACT_AGENT_ID",
            "elevenlabs_agent_phone_number_id": "ELEVENLABS_AGENT_PHONE_NUMBER_ID",
            "twilio_account_sid": "TWILIO_ACCOUNT_SID",
            "twilio_auth_token": "TWILIO_AUTH_TOKEN",
            "twilio_from_number": "TWILIO_FROM_NUMBER",
            "runner_phone_number": "RUNNER_PHONE_NUMBER",
            "emergency_contact_phone_number": "EMERGENCY_CONTACT_PHONE_NUMBER",
        }
        missing = [env_name for env_name in names.values() if not os.environ.get(env_name)]
        if missing:
            raise TelephonyConfigurationError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )
        return cls(
            elevenlabs_api_key=SecretStr(os.environ["ELEVENLABS_API_KEY"]),
            elevenlabs_runner_agent_id=os.environ["ELEVENLABS_RUNNER_AGENT_ID"],
            elevenlabs_contact_agent_id=os.environ["ELEVENLABS_CONTACT_AGENT_ID"],
            elevenlabs_agent_phone_number_id=os.environ["ELEVENLABS_AGENT_PHONE_NUMBER_ID"],
            twilio_account_sid=SecretStr(os.environ["TWILIO_ACCOUNT_SID"]),
            twilio_auth_token=SecretStr(os.environ["TWILIO_AUTH_TOKEN"]),
            twilio_from_number=os.environ["TWILIO_FROM_NUMBER"],
            runner_phone_number=os.environ["RUNNER_PHONE_NUMBER"],
            emergency_contact_phone_number=os.environ["EMERGENCY_CONTACT_PHONE_NUMBER"],
        )

    @field_validator(*_phone_fields)
    @classmethod
    def validate_phone_number(cls, value: str) -> str:
        if not _E164.fullmatch(value):
            raise ValueError("phone number must use E.164 format")
        return value

    def __str__(self) -> str:
        return "TelephonyConfig(<credentials redacted>)"

    def __repr__(self) -> str:
        return self.__str__()
