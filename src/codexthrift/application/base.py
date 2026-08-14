from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..paths import WorkspacePaths


class ControllerPort(Protocol):
    paths: WorkspacePaths
    host: str
    port: int


@dataclass(frozen=True)
class ServiceContext:
    paths: WorkspacePaths
    host: str
    port: int
    facade: ControllerPort


class BaseApplicationService:
    """Shared access to the compatibility facade during staged extraction."""

    def __init__(self, context: ServiceContext) -> None:
        self.context = context
        self.paths = context.paths
        self.host = context.host
        self.port = context.port
        self._controller = context.facade

    def __getattr__(self, name: str) -> Any:
        return getattr(self._controller, name)
