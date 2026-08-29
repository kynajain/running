"""Streaming connector for Apple Health export.xml and export.zip files."""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import IO
from xml.etree import ElementTree as ET

from dateutil import parser as date_parser

from running.connectors.base import register_connector
from running.models import GeoPoint, HealthSample, Metric, TimeWindow, WorkoutSession

_RECORD_METRICS: dict[str, tuple[Metric, str]] = {
    "HKQuantityTypeIdentifierHeartRate": (Metric.HEART_RATE, "count/min"),
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": (Metric.HRV_SDNN, "ms"),
    "HKQuantityTypeIdentifierRestingHeartRate": (Metric.RESTING_HEART_RATE, "count/min"),
    "HKQuantityTypeIdentifierRespiratoryRate": (Metric.RESPIRATORY_RATE, "count/min"),
    "HKQuantityTypeIdentifierActiveEnergyBurned": (Metric.ACTIVE_ENERGY, "kcal"),
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_apple_date(value: str) -> datetime:
    parsed = date_parser.parse(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Apple date is missing timezone: {value}")
    return parsed.astimezone(UTC)


def _float_attribute(element: ET.Element, name: str, default: float = 0.0) -> float:
    value = element.attrib.get(name)
    if value is None:
        return default
    return float(value)


def _distance_m(element: ET.Element) -> float:
    distance = _float_attribute(element, "totalDistance")
    unit = element.attrib.get("totalDistanceUnit", "m").lower()
    if unit in {"km", "kilometer", "kilometers"}:
        return distance * 1000
    if unit in {"mi", "mile", "miles"}:
        return distance * 1609.344
    return distance


@register_connector("apple_health")
class AppleHealthExportConnector:
    name = "apple_health"

    def __init__(self, path: Path) -> None:
        self.path = path

    def _export_xml(self) -> tuple[IO[bytes], zipfile.ZipFile | None]:
        path = self.path
        if path.is_dir():
            return (path / "export.xml").open("rb"), None
        if path.suffix.lower() == ".zip":
            archive = zipfile.ZipFile(path)
            candidates = [name for name in archive.namelist() if name.endswith("export.xml")]
            if not candidates:
                archive.close()
                raise FileNotFoundError("export.zip does not contain export.xml")
            return archive.open(candidates[0], "r"), archive
        return path.open("rb"), None

    def _read_route(self, route_path: str) -> list[GeoPoint]:
        normalised = route_path.lstrip("/")
        if self.path.is_dir():
            root = self.path
            candidates = [root / normalised, root / "apple_health_export" / normalised]
            route_file = next((candidate for candidate in candidates if candidate.exists()), None)
            if route_file is None:
                return []
            payload = route_file.read_bytes()
        elif self.path.suffix.lower() != ".zip":
            root = self.path.parent
            candidates = [root / normalised, root / "apple_health_export" / normalised]
            route_file = next((candidate for candidate in candidates if candidate.exists()), None)
            if route_file is None:
                return []
            payload = route_file.read_bytes()
        elif self.path.suffix.lower() == ".zip":
            with zipfile.ZipFile(self.path) as archive:
                names = [normalised, normalised.removeprefix("workout-routes/")]
                archive_names = archive.namelist()
                member = next((name for name in names if name in archive_names), None)
                if member is None:
                    member = next(
                        (name for name in archive_names if name.endswith(normalised)),
                        None,
                    )
                if member is None:
                    return []
                payload = archive.read(member)
        else:
            return []
        return self._parse_gpx(payload)

    @staticmethod
    def _parse_gpx(payload: bytes) -> list[GeoPoint]:
        points: list[GeoPoint] = []
        root = ET.fromstring(payload)
        for point in root.iter():
            if _local_name(point.tag) != "trkpt":
                continue
            timestamp: datetime | None = None
            elevation: float | None = None
            for child in point:
                name = _local_name(child.tag)
                if name == "time" and child.text:
                    parsed = date_parser.isoparse(child.text)
                    if parsed.tzinfo is None or parsed.utcoffset() is None:
                        raise ValueError("GPX timestamp is missing timezone")
                    timestamp = parsed.astimezone(UTC)
                elif name == "ele" and child.text:
                    elevation = float(child.text)
            if timestamp is not None:
                points.append(
                    GeoPoint(
                        lat=float(point.attrib["lat"]),
                        lon=float(point.attrib["lon"]),
                        elevation_m=elevation,
                        timestamp=timestamp,
                    )
                )
        return points

    async def fetch_samples(self, window: TimeWindow) -> AsyncIterator[HealthSample]:
        stream, archive = self._export_xml()
        try:
            for _, element in ET.iterparse(stream, events=("end",)):
                if _local_name(element.tag) != "Record":
                    continue
                record_type = element.attrib.get("type", "")
                metric_unit = _RECORD_METRICS.get(record_type)
                if metric_unit is not None:
                    start = _parse_apple_date(element.attrib["startDate"])
                    end = _parse_apple_date(
                        element.attrib.get("endDate", element.attrib["startDate"])
                    )
                    if start < window.end and end > window.start:
                        metric, default_unit = metric_unit
                        yield HealthSample(
                            metric=metric,
                            value=float(element.attrib["value"]),
                            unit=element.attrib.get("unit", default_unit),
                            start=start,
                            end=end,
                            source=element.attrib.get("sourceName", "Apple Health"),
                        )
                element.clear()
        finally:
            stream.close()
            if archive is not None:
                archive.close()

    async def fetch_workouts(self, window: TimeWindow) -> AsyncIterator[WorkoutSession]:
        stream, archive = self._export_xml()
        try:
            for _, element in ET.iterparse(stream, events=("end",)):
                if _local_name(element.tag) != "Workout":
                    continue
                start = _parse_apple_date(element.attrib["startDate"])
                end = _parse_apple_date(element.attrib["endDate"])
                if start < window.end and end > window.start:
                    route_refs = [
                        child.attrib["path"]
                        for child in element.iter()
                        if _local_name(child.tag) == "FileReference" and "path" in child.attrib
                    ]
                    route = [point for ref in route_refs for point in self._read_route(ref)]
                    identity = "|".join(
                        [
                            element.attrib.get("workoutActivityType", "unknown"),
                            start.isoformat(),
                            end.isoformat(),
                            str(_distance_m(element)),
                            ",".join(route_refs),
                        ]
                    )
                    yield WorkoutSession(
                        id=hashlib.sha256(identity.encode()).hexdigest(),
                        activity=element.attrib.get("workoutActivityType", "unknown"),
                        start=start,
                        end=end,
                        distance_m=_distance_m(element),
                        route=route,
                    )
                element.clear()
        finally:
            stream.close()
            if archive is not None:
                archive.close()
