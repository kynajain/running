"""Concurrent, retrying synchronisation workers."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Iterable
from dataclasses import dataclass, field

from running.connectors.base import HealthConnector, Sink
from running.models import TimeWindow
from running.stress import compute_stress_scores

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncJob:
    connector_name: str
    window: TimeWindow
    sink_name: str


@dataclass
class SyncReport:
    records_written: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def jobs_failed(self) -> int:
        return len(self.failures)


class SyncWorkerPool:
    def __init__(
        self,
        connectors: dict[str, HealthConnector],
        sinks: dict[str, Sink],
        concurrency: int = 4,
        max_attempts: int = 3,
        base_backoff: float = 0.2,
        max_backoff: float = 10.0,
        rng: random.Random | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.connectors = connectors
        self.sinks = sinks
        self.concurrency = concurrency
        self.max_attempts = max_attempts
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.rng = rng or random.Random()
        self.queue: asyncio.Queue[SyncJob | None] = asyncio.Queue()
        self.report = SyncReport()
        self._report_lock = asyncio.Lock()

    async def run(self, jobs: Iterable[SyncJob]) -> SyncReport:
        job_list = list(jobs)
        for job in job_list:
            await self.queue.put(job)
        workers = [asyncio.create_task(self._worker(index)) for index in range(self.concurrency)]
        for _ in workers:
            await self.queue.put(None)
        await self.queue.join()
        await asyncio.gather(*workers)
        return self.report

    async def _worker(self, worker_id: int) -> None:
        while True:
            item = await self.queue.get()
            try:
                if item is None:
                    return
                job = item
                logger.info(
                    "sync job started", extra={"worker": worker_id, "source": job.connector_name}
                )
                try:
                    written = await self._run_with_retries(job)
                except Exception as exc:
                    failure = f"{job.connector_name}/{job.sink_name}: {exc}"
                    async with self._report_lock:
                        self.report.failures.append(failure)
                    logger.exception(
                        "sync job failed",
                        extra={"worker": worker_id, "source": job.connector_name},
                    )
                else:
                    async with self._report_lock:
                        self.report.records_written += written
                    logger.info(
                        "sync job finished",
                        extra={
                            "worker": worker_id,
                            "source": job.connector_name,
                            "written": written,
                        },
                    )
            finally:
                self.queue.task_done()

    async def _run_with_retries(self, job: SyncJob) -> int:
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await self._run_job(job)
            except Exception:
                if attempt == self.max_attempts:
                    raise
                delay = min(self.max_backoff, self.base_backoff * (2 ** (attempt - 1)))
                delay *= 0.5 + self.rng.random()
                logger.warning(
                    "sync job retrying",
                    extra={"source": job.connector_name, "attempt": attempt, "delay": delay},
                )
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable retry state")

    async def _run_job(self, job: SyncJob) -> int:
        connector = self.connectors[job.connector_name]
        sink = self.sinks[job.sink_name]
        samples = [sample async for sample in connector.fetch_samples(job.window)]
        workouts = [workout async for workout in connector.fetch_workouts(job.window)]
        samples.extend(compute_stress_scores(samples))
        return await sink.write_samples(samples) + await sink.write_workouts(workouts)
