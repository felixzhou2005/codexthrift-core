from __future__ import annotations

import logging
import sys
from typing import Any, Callable

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .controller import RouterController
from .util import ControlError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
LOGGER = logging.getLogger("routerllm.mcp_readonly")

mcp = FastMCP("LiteLLM Codex Personal Router — Pro Read Only")
controller = RouterController(ensure_paths=False)
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _call(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        result = fn(*args, **kwargs)
        if isinstance(result, dict):
            if "ok" not in result:
                return {"ok": True, **result}
            return result
        return {"ok": True, "result": result}
    except (ControlError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
    except Exception as exc:
        LOGGER.exception("unexpected read-only MCP tool failure")
        return {
            "ok": False,
            "error": f"Unexpected local controller error: {type(exc).__name__}: {exc}",
            "error_type": type(exc).__name__,
        }


def _cli_for(tool: str | None, arguments: dict[str, Any] | None = None) -> str | None:
    arguments = arguments or {}
    profile = str(arguments.get("profile") or "gpt56")
    phase = str(arguments.get("phase") or controller.active_phase())
    mapping = {
        "prepare_phase2": f"codexthrift prepare --profile {profile} --confirm APPLY",
        "start_runtime_install": "codexthrift install-runtime --confirm APPLY",
        "begin_subscription_oauth": "codexthrift oauth --confirm APPLY",
        "probe_subscription_models": (
            f"codexthrift probe --phase {phase} --profile {profile} --confirm APPLY"
        ),
        "start_litellm_proxy": "codexthrift proxy-start --confirm APPLY",
        "configure_codex_client": "codexthrift configure-codex --confirm APPLY",
        "routing_report": "codexthrift report --days 30",
    }
    return mapping.get(str(tool))


@mcp.tool(annotations=READ_ONLY)
def pro_plan_notice() -> dict[str, Any]:
    """说明 ChatGPT Pro 当前只能连接 read/fetch MCP；本 App 因而只读。"""
    return {
        "ok": True,
        "mode": "chatgpt_pro_read_only",
        "write_tools_available": False,
        "reason": (
            "OpenAI 当前把自定义 MCP 的 write/modify 完整能力限定在 "
            "Business、Enterprise 和 Edu；Pro 仅支持 read/fetch。"
        ),
        "write_control": "请在本机使用 codexthrift；完整可写 MCP Server 命令为 codexthrift-mcp。",
    }


@mcp.tool(annotations=READ_ONLY)
def setup_guide() -> dict[str, Any]:
    """读取实施状态，并给出需要在本机终端执行的下一条白名单 CLI 命令。"""
    result = _call(controller.setup_guide)
    next_action = result.get("next_action") if isinstance(result, dict) else None
    if isinstance(next_action, dict):
        result["pro_read_only_mode"] = True
        result["local_cli_command"] = _cli_for(
            next_action.get("tool"),
            next_action.get("arguments") if isinstance(next_action.get("arguments"), dict) else None,
        )
    return result


@mcp.tool(annotations=READ_ONLY)
def environment_check() -> dict[str, Any]:
    """检查 Python、Git、Codex、tunnel-client、本机端口和工作目录。"""
    return _call(controller.environment_check)


@mcp.tool(annotations=READ_ONLY)
def router_status() -> dict[str, Any]:
    """读取 OAuth、探测、LiteLLM、Codex 配置和阶段综合状态。"""
    return _call(controller.full_status)


@mcp.tool(annotations=READ_ONLY)
def runtime_install_status() -> dict[str, Any]:
    """读取 LiteLLM runtime 安装状态。"""
    return _call(controller.runtime_install_status)


@mcp.tool(annotations=READ_ONLY)
def subscription_oauth_status() -> dict[str, Any]:
    """读取 OAuth 网址、设备码及本地 auth 状态，不返回 token。"""
    return _call(controller.oauth_status)


@mcp.tool(annotations=READ_ONLY)
def model_probe_status(profile: str | None = None, phase: str | None = None) -> dict[str, Any]:
    """读取模型与 effort 探测结果。"""
    return _call(controller.model_probe_status, profile=profile, phase=phase)


@mcp.tool(annotations=READ_ONLY)
def proxy_status() -> dict[str, Any]:
    """读取 LiteLLM 进程、健康状态和活动阶段。"""
    return _call(controller.proxy_status)


@mcp.tool(annotations=READ_ONLY)
def read_component_logs(component: str = "proxy", lines: int = 100) -> dict[str, Any]:
    """读取 proxy、oauth、probe 或 install 日志尾部。"""
    return _call(controller.logs, component=component, lines=lines)


@mcp.tool(annotations=READ_ONLY)
def routing_report(days: int = 30, unit: str = "turn") -> dict[str, Any]:
    """读取路由档位、effort、延迟、失败及标注汇总。"""
    return _call(controller.routing_report, days=days, unit=unit)


@mcp.tool(annotations=READ_ONLY)
def recent_routing_calls(limit: int = 20) -> dict[str, Any]:
    """读取最近模型调用的脱敏审计记录。"""
    return _call(controller.recent_calls, limit=limit)


@mcp.tool(annotations=READ_ONLY)
def recent_routing_turns(limit: int = 20) -> dict[str, Any]:
    """按用户任务聚合最近路由记录。"""
    return _call(controller.recent_turns, limit=limit)


@mcp.tool(annotations=READ_ONLY)
def explain_routing_turn(turn_id: str) -> dict[str, Any]:
    """解释指定用户任务的选择原因和内部调用继承情况。"""
    return _call(controller.explain_turn, turn_id=turn_id)


@mcp.tool(annotations=READ_ONLY)
def list_backups() -> dict[str, Any]:
    """列出控制器创建的配置备份。"""
    return _call(controller.list_backups)


@mcp.tool(annotations=READ_ONLY)
def codex_task_status(task_id: str) -> dict[str, Any]:
    """读取受管 Codex 任务状态。"""
    return _call(controller.codex_task_status, task_id=task_id)


@mcp.tool(annotations=READ_ONLY)
def list_codex_tasks(limit: int = 20) -> dict[str, Any]:
    """列出最近受管 Codex 任务。"""
    return _call(controller.list_codex_tasks, limit=limit)


def main() -> int:
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
