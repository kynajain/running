"""Command-line interface for synchronising health data."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import running.sinks as _registered_sinks  # noqa: F401
from running.connectors import get_connector, get_sink
from running.models import TimeWindow
from running.workers import SyncJob, SyncReport, SyncWorkerPool


def _duration(value: str) -> timedelta:
    if not value.endswith("d"):
        raise argparse.ArgumentTypeError("duration must look like 7d")
    try:
        days = float(value[:-1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("duration must look like 7d") from exc
    if days <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return timedelta(days=days)


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
        except (ValueError, KeyError) as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps({"records_written": report.records_written, "failures": report.failures}))


if __name__ == "__main__":
    main()
