"""Command-line interface for synchronising health data."""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

import running.sinks as _registered_sinks  # noqa: F401
from running.connectors import get_connector, get_sink
from running.connectors.base import ClosableSink
from running.models import GeoPoint, TimeWindow
from running.telephony.config import TelephonyConfig, TelephonyConfigurationError
from running.telephony.elevenlabs import ElevenLabsClient
from running.telephony.escalation import EscalationService, SafetyAlert, dry_run_plan
from running.telephony.twilio_sms import TwilioSMSClient
from running.workers import SyncJob, SyncReport, SyncWorkerPool


def _duration(value: str) -> timedelta:
    units = {"m": "minutes", "h": "hours", "d": "days"}
    if not value or value[-1] not in units:
        raise argparse.ArgumentTypeError("duration must look like 90m, 12h, or 7d")
    try:
        amount = float(value[:-1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("duration must look like 90m, 12h, or 7d") from exc
    if amount <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return timedelta(**{units[value[-1]]: amount})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="running")
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync = subparsers.add_parser("sync")
    sync.add_argument("--source", choices=("synthetic", "apple_health"), required=True)
    sync.add_argument("--export", type=Path)
    sync.add_argument("--sink", choices=("jsonl", "notion"), default="jsonl")
    sync.add_argument("--since", type=_duration, default=timedelta(days=7))
    sync.add_argument("--concurrency", type=int, default=4)
    sync.add_argument("--output", type=Path, default=Path("running.jsonl"))
    alert = subparsers.add_parser("alert")
    alert.add_argument("--lat", type=float, required=True)
    alert.add_argument("--lon", type=float, required=True)
    alert.add_argument("--dry-run", action="store_true")
    return parser


async def _sync(args: argparse.Namespace) -> SyncReport:
    if args.source == "apple_health" and args.export is None:
        raise ValueError("--export is required for apple_health")
    if args.source == "apple_health":
        connector = get_connector(args.source)(args.export)
    else:
        connector = get_connector(args.source)()
    if args.sink == "jsonl":
        sink = get_sink(args.sink)(args.output)
    else:
        sink = get_sink(args.sink)()
    now = datetime.now(UTC)
    window = TimeWindow(start=now - args.since, end=now)
    async with AsyncExitStack() as stack:
        if isinstance(sink, ClosableSink):
            stack.push_async_callback(sink.aclose)
        pool = SyncWorkerPool(
            connectors={args.source: connector},
            sinks={args.sink: sink},
            concurrency=args.concurrency,
        )
        return await pool.run([SyncJob(args.source, window, args.sink)])


async def _alert(args: argparse.Namespace) -> None:
    timestamp = datetime.now(UTC)
    alert = SafetyAlert(
        location=GeoPoint(lat=args.lat, lon=args.lon, timestamp=timestamp),
        timestamp=timestamp,
    )
    if args.dry_run:
        print(dry_run_plan(alert))
        return
    config = TelephonyConfig.from_env()
    async with AsyncExitStack() as stack:
        elevenlabs = ElevenLabsClient(config.elevenlabs_api_key.get_secret_value())
        twilio = TwilioSMSClient(
            config.twilio_account_sid.get_secret_value(),
            config.twilio_auth_token.get_secret_value(),
        )
        await stack.enter_async_context(elevenlabs)
        await stack.enter_async_context(twilio)
        service = EscalationService(config, elevenlabs, twilio)
        result = await service.escalate(alert)
    print(result.model_dump_json())


def main() -> None:
    args = _parser().parse_args()
    if args.command == "sync":
        try:
            report = asyncio.run(_sync(args))
        except (ValueError, KeyError) as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps({"records_written": report.records_written, "failures": report.failures}))
    elif args.command == "alert":
        try:
            asyncio.run(_alert(args))
        except (TelephonyConfigurationError, ValueError, KeyError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
