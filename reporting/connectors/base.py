from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class HealthStatus:
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchedPage:
    endpoint: str
    request_params: dict[str, Any]
    payload: dict[str, Any]
    page_number: int = 1


class DataSourceConnector(ABC):
    @abstractmethod
    def test_connection(self) -> HealthStatus:
        raise NotImplementedError

    @abstractmethod
    def sync_objects(self) -> dict[str, list[FetchedPage]]:
        raise NotImplementedError

    @abstractmethod
    def sync_insights(self, start: date, end: date, levels: list[str]) -> dict[str, list[FetchedPage]]:
        raise NotImplementedError

    @abstractmethod
    def health_status(self) -> HealthStatus:
        raise NotImplementedError

