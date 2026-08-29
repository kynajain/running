"""Protocols and registries for pluggable sources and sinks."""

from collections.abc import AsyncIterator, Callable, Sequence
from typing import Protocol, TypeVar, runtime_checkable

from running.models import HealthSample, TimeWindow, WorkoutSession


class HealthConnector(Protocol):
    name: str

    def fetch_samples(self, window: TimeWindow) -> AsyncIterator[HealthSample]: ...

    def fetch_workouts(self, window: TimeWindow) -> AsyncIterator[WorkoutSession]: ...


class Sink(Protocol):
    name: str

    async def write_samples(self, batch: Sequence[HealthSample]) -> int: ...

    async def write_workouts(self, batch: Sequence[WorkoutSession]) -> int: ...


@runtime_checkable
class ClosableSink(Protocol):
    async def aclose(self) -> None: ...


ConnectorFactory = Callable[..., HealthConnector]
SinkFactory = Callable[..., Sink]

_CONNECTORS: dict[str, ConnectorFactory] = {}
_SINKS: dict[str, SinkFactory] = {}
T = TypeVar("T")


def register_connector(name: str) -> Callable[[T], T]:
    def decorator(factory: T) -> T:
        if not callable(factory):
            raise TypeError("connector factory must be callable")
        _CONNECTORS[name] = factory
        return factory

    return decorator


def register_sink(name: str) -> Callable[[T], T]:
    def decorator(factory: T) -> T:
        if not callable(factory):
            raise TypeError("sink factory must be callable")
        _SINKS[name] = factory
        return factory

    return decorator


def get_connector(name: str) -> ConnectorFactory:
    try:
        return _CONNECTORS[name]
    except KeyError as exc:
        raise KeyError(f"unknown connector: {name}") from exc


def get_sink(name: str) -> SinkFactory:
    try:
        return _SINKS[name]
    except KeyError as exc:
        raise KeyError(f"unknown sink: {name}") from exc
