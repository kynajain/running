from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

import pytest

from running.connectors.apple_health import AppleHealthExportConnector
from running.models import Metric, TimeWindow

XML = """\
<HealthData>
  <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Watch"
    unit="count/min" value="130" startDate="2026-08-20 07:15:03 +0100"
    endDate="2026-08-20 07:15:04 +0100" />
  <Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN" sourceName="Watch"
    unit="ms" value="48" startDate="2026-08-20 07:15:03 +0100"
    endDate="2026-08-20 07:15:04 +0100" />
  <Record type="HKQuantityTypeIdentifierUnknown" value="1"
    startDate="2026-08-20 07:15:03 +0100" endDate="2026-08-20 07:15:04 +0100" />
  <Workout workoutActivityType="HKWorkoutActivityTypeRunning"
    startDate="2026-08-20 07:00:00 +0100" endDate="2026-08-20 08:00:00 +0100"
    totalDistance="1.2" totalDistanceUnit="km">
    <WorkoutRoute><FileReference path="/workout-routes/route_1.gpx" /></WorkoutRoute>
  </Workout>
</HealthData>
"""
GPX = """\
<gpx><trk><trkseg>
  <trkpt lat="51.5387" lon="-0.0166"><ele>12</ele><time>2026-08-20T06:10:00Z</time></trkpt>
  <trkpt lat="51.5390" lon="-0.0166"><time>2026-08-20T06:10:01Z</time></trkpt>
</trkseg></trk></gpx>
"""


def window() -> TimeWindow:
    return TimeWindow(
        start=datetime(2026, 8, 20, 5, tzinfo=UTC),
        end=datetime(2026, 8, 20, 9, tzinfo=UTC),
    )


@pytest.mark.parametrize("kind", ["xml", "zip"])
async def test_apple_export_xml_and_zip(tmp_path: Path, kind: str) -> None:
    if kind == "xml":
        export = tmp_path / "export.xml"
        export.write_text(XML)
        (tmp_path / "workout-routes").mkdir()
        (tmp_path / "workout-routes" / "route_1.gpx").write_text(GPX)
    else:
        export = tmp_path / "export.zip"
        with ZipFile(export, "w") as archive:
            archive.writestr("apple_health_export/export.xml", XML)
            archive.writestr("apple_health_export/workout-routes/route_1.gpx", GPX)
    connector = AppleHealthExportConnector(export)
    samples = [sample async for sample in connector.fetch_samples(window())]
    workouts = [workout async for workout in connector.fetch_workouts(window())]
    assert [sample.metric for sample in samples] == [Metric.HEART_RATE, Metric.HRV_SDNN]
    assert samples[0].start == datetime(2026, 8, 20, 6, 15, 3, tzinfo=UTC)
    assert workouts[0].distance_m == 1200
    assert len(workouts[0].route) == 2
