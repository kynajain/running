"""Health data connectors."""

from running.connectors.apple_health import AppleHealthExportConnector
from running.connectors.base import (
    HealthConnector,
    Sink,
    get_connector,
    get_sink,
    register_connector,
    register_sink,
)
from running.connectors.ndjson import NDJSONConnector
from running.connectors.synthetic import SyntheticAppleHealthConnector

__all__ = [
    "AppleHealthExportConnector",
    "HealthConnector",
    "NDJSONConnector",
    "Sink",
    "SyntheticAppleHealthConnector",
    "get_connector",
    "get_sink",
    "register_connector",
    "register_sink",
]
