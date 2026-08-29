import asyncio
from datetime import UTC, datetime

from running.models import HealthSample, Metric, TimeWindow
from running.workers import SyncJob, SyncWorkerPool


class FakeConnector:
    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def fetch_samples(self, window: TimeWindow):
        if self.fail:
            raise RuntimeError("broken source")
        yield HealthSample(
            metric=Metric.HEART_RATE,
            value=100,
            unit="count/min",
            start=window.start,
            end=window.start,
            source="fake",
        )

    async def fetch_workouts(self, window: TimeWindow):
        if False:
            yield None


class FakeSink:
    name = "fake"

    def __init__(self, failures: int = 0, delay: float = 0) -> None:
        self.failures = failures
        self.delay = delay
        self.writes = 0

    async def write_samples(self, batch):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary")
        self.writes += len(batch)
        return len(batch)

    async def write_workouts(self, batch):
        return 0


def make_job(name: str = "fake") -> SyncJob:
    return SyncJob(
        connector_name=name,
        sink_name=name,
        window=TimeWindow(
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        ),
    )


async def test_worker_retries_and_isolates_failure() -> None:
    sink = FakeSink(failures=1)
    pool = SyncWorkerPool(
        connectors={"fake": FakeConnector(), "bad": FakeConnector(fail=True)},
        sinks={"fake": sink, "bad": sink},
        max_attempts=2,
        base_backoff=0,
    )
    report = await pool.run([make_job(), make_job("bad")])
    assert report.records_written == 1
    assert report.jobs_failed == 1


async def test_worker_concurrency() -> None:
    sink = FakeSink(delay=0.03)
    pool = SyncWorkerPool(
        connectors={"fake": FakeConnector()},
        sinks={"fake": sink},
        concurrency=2,
        base_backoff=0,
    )
    jobs = [make_job() for _ in range(4)]
    start = asyncio.get_running_loop().time()
    report = await pool.run(jobs)
    elapsed = asyncio.get_running_loop().time() - start
    assert report.records_written == 4
    assert elapsed < 0.11
