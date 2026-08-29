"""ASGI entrypoint for one long-lived process.

This entrypoint is not supported on Vercel or other serverless platforms:
delayed escalation and incident state live in-process, so a serverless
invocation ending would silently break escalation. Run it as a single
always-on ASGI process instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from running.app.server import AppConfig, create_app  # noqa: E402

app = create_app(AppConfig.from_env())
