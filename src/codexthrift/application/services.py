from __future__ import annotations

from dataclasses import dataclass

from .base import ControllerPort, ServiceContext
from .codex_mode_service import CodexModeService
from .diagnostics_service import DiagnosticsService
from .oauth_service import OAuthService
from .probe_service import ProbeService
from .proxy_service import ProxyService
from .routing_service import RoutingService
from .runtime_service import RuntimeService
from .task_service import TaskService


@dataclass(frozen=True)
class ApplicationServices:
    runtime: RuntimeService
    oauth: OAuthService
    probe: ProbeService
    codex_mode: CodexModeService
    proxy: ProxyService
    routing: RoutingService
    task: TaskService
    diagnostics: DiagnosticsService

    @classmethod
    def create(cls, controller: ControllerPort) -> ApplicationServices:
        context = ServiceContext(
            paths=controller.paths,
            host=controller.host,
            port=controller.port,
            facade=controller,
        )
        return cls(
            runtime=RuntimeService(context),
            oauth=OAuthService(context),
            probe=ProbeService(context),
            codex_mode=CodexModeService(context),
            proxy=ProxyService(context),
            routing=RoutingService(context),
            task=TaskService(context),
            diagnostics=DiagnosticsService(context),
        )
