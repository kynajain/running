from datetime import UTC, datetime
from pathlib import Path

import pytest

from running.connectors.ndjson import NDJSONConnector
from running.models import Metric, TimeWindow

PAYLOAD = "\n".join(
    [
        '{"type":"sample","record":{"metric":"heart_rate","value":130,"unit":"count/min",'
        '"start":"2026-08-20T06:15:03Z","end":"2026-08-20T06:15:04Z","source":"Apple Watch"}}',
        '{"type":"sample","record":{"metric":"hrv_sdnn","value":48,"unit":"ms",'
        '"start":"2026-08-20T02:00:00Z","end":"2026-08-20T02:00:01Z","source":"Apple Watch"}}',
        '{"type":"workout","record":{"id":"1D0B","activity":"HKWorkoutActivityTypeRunning",'
        '"start":"2026-08-20T06:00:00Z","end":"2026-08-20T07:00:00Z","distance_m":1200,'
        '"route":[{"lat":51.5387,"lon":-0.0166,"elevation_m":12,'
        '"timestamp":"2026-08-20T06:10:00Z"}],"samples":[]}}',
        "",
    ]
)


def window() -> TimeWindow:
    return TimeWindow(
        start=datetime(2026, 8, 20, 5, tzinfo=UTC),
        end=datetime(2026, 8, 20, 9, tzinfo=UTC),
    )


def connector(tmp_path: Path, payload: str = PAYLOAD) -> NDJSONConnector:
    export = tmp_path / "batch.ndjson"
    export.write_text(payload)
    return NDJSONConnector(export)


async def test_reads_samples_and_workouts_inside_the_window(tmp_path: Path) -> None:
    source = connector(tmp_path)
    samples = [sample async for sample in source.fetch_samples(window())]
    workouts = [workout async for workout in source.fetch_workouts(window())]
    assert [sample.metric for sample in samples] == [Metric.HEART_RATE]
    assert samples[0].source == "Apple Watch"
    assert workouts[0].distance_m == 1200
    assert workouts[0].route[0].elevation_m == 12


async def test_rejects_malformed_lines(tmp_path: Path) -> None:
    source = connector(tmp_path, '{"type":"sample"}\n')
    with pytest.raises(ValueError, match="missing a record envelope"):
        [sample async for sample in source.fetch_samples(window())]


async def test_rejects_invalid_json(tmp_path: Path) -> None:
    source = connector(tmp_path, "not json\n")
    with pytest.raises(ValueError, match="not valid JSON"):
        [sample async for sample in source.fetch_samples(window())]
