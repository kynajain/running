"""Vercel serverless entrypoint exposing the FastAPI app as an ASGI handler."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from running.app.server import AppConfig, create_app  # noqa: E402

app = create_app(AppConfig.from_env())
