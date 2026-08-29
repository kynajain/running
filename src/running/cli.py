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
from running.models import TimeWindow
from running.sinks.twilio import DEFAULT_THRESHOLD, TwilioConfigurationError
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


_EXPORT_SOURCES = frozenset({"apple_health", "ndjson"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="running")
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync = subparsers.add_parser("sync")
    sync.add_argument("--source", choices=("synthetic", "apple_health", "ndjson"), required=True)
    sync.add_argument("--export", type=Path)
    sync.add_argument("--sink", choices=("jsonl", "notion", "twilio"), default="jsonl")
    sync.add_argument(
        "--stress-threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="stress score at or above which the twilio sink sends an SMS",
    )
    sync.add_argument("--since", type=_duration, default=timedelta(days=7))
    sync.add_argument("--concurrency", type=int, default=4)
    sync.add_argument("--output", type=Path, default=Path("running.jsonl"))
    return parser


async def _sync(args: argparse.Namespace) -> SyncReport:
    if args.source in _EXPORT_SOURCES:
        if args.export is None:
            raise ValueError(f"--export is required for {args.source}")
        connector = get_connector(args.source)(args.export)
    else:
        connector = get_connector(args.source)()
    if args.sink == "jsonl":
        sink = get_sink(args.sink)(args.output)
    elif args.sink == "twilio":
        sink = get_sink(args.sink)(threshold=args.stress_threshold)
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


def main() -> None:
    args = _parser().parse_args()
    if args.command == "sync":
        try:
            report = asyncio.run(_sync(args))
        except (ValueError, KeyError, TwilioConfigurationError) as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps({"records_written": report.records_written, "failures": report.failures}))


if __name__ == "__main__":
    main()
