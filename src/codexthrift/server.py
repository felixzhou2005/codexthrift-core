from __future__ import annotations

import json
import logging
import sys
from typing import Any, Callable

from fastmcp import FastMCP

from .controller import RouterController
from .util import ControlError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
LOGGER = logging.getLogger("routerllm.mcp")

mcp = FastMCP("LiteLLM Codex Personal Router")
controller = RouterController(ensure_paths=True)


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
    except Exception as exc:  # Keep stack traces out of the ChatGPT-visible tool output.
        LOGGER.exception("unexpected MCP tool failure")
        return {
            "ok": False,
            "error": f"Unexpected local controller error: {type(exc).__name__}: {exc}",
            "error_type": type(exc).__name__,
        }


@mcp.tool()
def setup_guide() -> dict[str, Any]:
    """检查完整安装状态，并返回当前唯一建议的下一步；这是开始或恢复实施时的首选工具。"""
    return _call(controller.setup_guide)


@mcp.tool()
def environment_check() -> dict[str, Any]:
    """检查 Python、Git、Codex、tunnel-client、本机端口和工作目录是否满足要求。只读。"""
    return _call(controller.environment_check)


@mcp.tool()
def router_status() -> dict[str, Any]:
    """读取 OAuth、模型探测、LiteLLM、Codex 配置和当前阶段的综合状态。只读。"""
    return _call(controller.full_status)


@mcp.tool()
def prepare_phase2(profile: str = "gpt56", confirm: str = "") -> dict[str, Any]:
    """生成第二/三阶段 YAML、审计回调和本地密钥，并把第二阶段设为活动配置。当前唯一支持的 profile 为 gpt56。写操作要求 confirm='APPLY'。"""
    return _call(controller.prepare_configs, profile=profile, confirm=confirm)


@mcp.tool()
def start_runtime_install(confirm: str = "") -> dict[str, Any]:
    """在隔离 venv 中安装已锁定的 LiteLLM Proxy 版本。异步启动；写操作要求 confirm='APPLY'。"""
    return _call(controller.start_runtime_install, confirm=confirm)


@mcp.tool()
def runtime_install_status() -> dict[str, Any]:
    """读取 LiteLLM runtime 安装进度、结果和日志尾部。只读。"""
    return _call(controller.runtime_install_status)


@mcp.tool()
def begin_subscription_oauth(confirm: str = "") -> dict[str, Any]:
    """启动 LiteLLM ChatGPT 订阅 OAuth device flow。随后调用 subscription_oauth_status 获取网址和设备码。要求 confirm='APPLY'。"""
    return _call(controller.begin_oauth, confirm=confirm)


@mcp.tool()
def subscription_oauth_status() -> dict[str, Any]:
    """读取 OAuth 验证网址、设备码、auth.json 状态和日志；不会返回 OAuth token。只读。"""
    return _call(controller.oauth_status)


@mcp.tool()
def probe_subscription_models(
    profile: str = "gpt56",
    phase: str = "phase2",
    confirm: str = "",
) -> dict[str, Any]:
    """通过 Responses API 验证指定阶段的模型与 effort。phase2 只测 medium；phase3 测 low/medium/high/xhigh。会消耗少量订阅额度；要求 confirm='APPLY'。"""
    return _call(
        controller.start_model_probe,
        profile=profile,
        phase=phase,
        confirm=confirm,
    )


@mcp.tool()
def model_probe_status(
    profile: str | None = None,
    phase: str | None = None,
) -> dict[str, Any]:
    """读取指定 phase/profile 的探测结果、当前探测任务和所有已保存结果。只读。"""
    return _call(controller.model_probe_status, profile=profile, phase=phase)


@mcp.tool()
def configure_codex_client(phase: str | None = None, confirm: str = "") -> dict[str, Any]:
    """备份并更新 ~/.codex/config.toml，使 Codex 通过 Responses API 连接当前活动阶段的本机 LiteLLM；要求 confirm='APPLY'。"""
    return _call(controller.configure_codex, phase=phase, confirm=confirm)


@mcp.tool()
def codex_mode_status() -> dict[str, Any]:
    """读取 Codex 当前连接模式、目标 CODEX_HOME、配置验证及客户端重启要求。"""
    return _call(controller.codex_mode_status)


@mcp.tool()
def enable_smart_routing(confirm: str = "", allow_unverified: bool = False) -> dict[str, Any]:
    """启动并验证 LiteLLM 后切换 Codex 到智能路由；要求 confirm='ENABLE_ROUTER'。"""
    return _call(
        controller.enable_smart_routing,
        confirm=confirm,
        allow_unverified=allow_unverified,
    )


@mcp.tool()
def switch_to_native_codex(confirm: str = "") -> dict[str, Any]:
    """字段级切回内置 OpenAI Provider，不停止 LiteLLM Proxy；要求 confirm='SWITCH_NATIVE'。"""
    return _call(controller.switch_to_native_codex, confirm=confirm)


@mcp.tool()
def start_litellm_proxy(confirm: str = "", allow_unverified: bool = False) -> dict[str, Any]:
    """只在 127.0.0.1:4000 启动 LiteLLM。默认要求目标模型已全部探测成功；写操作要求 confirm='APPLY'。"""
    return _call(
        controller.start_proxy,
        confirm=confirm,
        allow_unverified=allow_unverified,
    )


@mcp.tool()
def stop_litellm_proxy(confirm: str = "") -> dict[str, Any]:
    """仅终止本控制器记录且命令行匹配的 LiteLLM 进程。要求 confirm='APPLY'。"""
    return _call(controller.stop_proxy, confirm=confirm)


@mcp.tool()
def restart_litellm_proxy(confirm: str = "", allow_unverified: bool = False) -> dict[str, Any]:
    """安全重启受管 LiteLLM 进程。要求 confirm='APPLY'。"""
    return _call(
        controller.restart_proxy,
        confirm=confirm,
        allow_unverified=allow_unverified,
    )


@mcp.tool()
def proxy_status() -> dict[str, Any]:
    """读取 LiteLLM PID、本机健康状态、活动阶段和日志尾部。只读。"""
    return _call(controller.proxy_status)


@mcp.tool()
def activate_phase3(profile: str = "gpt56", confirm: str = "") -> dict[str, Any]:
    """在 phase3/profile 探测全部通过后，启用自动 effort：SIMPLE=Luna/low、MEDIUM=Terra/medium、COMPLEX=Sol/high、REASONING=Sol/xhigh。要求 confirm='APPLY'。"""
    return _call(controller.activate_phase, phase="phase3", profile=profile, confirm=confirm)


@mcp.tool()
def return_to_phase2(profile: str = "gpt56", confirm: str = "") -> dict[str, Any]:
    """把所有路由档位恢复为 medium effort，保留模型自动选择。要求 confirm='APPLY'。"""
    return _call(controller.activate_phase, phase="phase2", profile=profile, confirm=confirm)


@mcp.tool()
def prepare_phase21_candidate(profile: str = "gpt56", confirm: str = "") -> dict[str, Any]:
    """生成 Phase 2.1 候选配置和路由模块，但不激活、不重启、不调用订阅模型。要求 confirm='APPLY'。"""
    return _call(controller.prepare_phase21_candidate, profile=profile, confirm=confirm)


@mcp.tool()
def activate_phase21(profile: str = "gpt56", confirm: str = "") -> dict[str, Any]:
    """备份当前配置并激活 Phase 2.1 任务级路由；会重启本机代理。要求 confirm='APPLY'。"""
    return _call(controller.activate_phase21, profile=profile, confirm=confirm)


@mcp.tool()
def read_component_logs(component: str = "proxy", lines: int = 100) -> dict[str, Any]:
    """读取 proxy、oauth、probe 或 install 的日志尾部；本地 master key 会被遮蔽。只读。"""
    return _call(controller.logs, component=component, lines=lines)


@mcp.tool()
def routing_report(days: int = 30, unit: str = "turn") -> dict[str, Any]:
    """汇总模型档位、reasoning effort、延迟、失败和人工标注；默认审计不保存提示词正文。只读。"""
    return _call(controller.routing_report, days=days, unit=unit)


@mcp.tool()
def recent_routing_calls(limit: int = 20) -> dict[str, Any]:
    """读取最近模型调用的脱敏审计记录。"""
    return _call(controller.recent_calls, limit=limit)


@mcp.tool()
def recent_routing_turns(limit: int = 20) -> dict[str, Any]:
    """按用户任务聚合最近路由记录，排除无法可靠归组的旧版调用。"""
    return _call(controller.recent_turns, limit=limit)


@mcp.tool()
def explain_routing_turn(turn_id: str) -> dict[str, Any]:
    """解释指定用户任务的选择原因和内部调用继承情况。"""
    return _call(controller.explain_turn, turn_id=turn_id)


@mcp.tool()
def record_routing_feedback(
    expected_tier: str,
    outcome: str,
    prompt_fingerprint: str | None = None,
    logical_turn_id: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    """给一条审计记录标注理想档位与结果。expected_tier 为 SIMPLE/MEDIUM/COMPLEX/REASONING；outcome 为 success/partial/failure。"""
    return _call(
        controller.record_feedback,
        prompt_fingerprint=prompt_fingerprint,
        logical_turn_id=logical_turn_id,
        expected_tier=expected_tier,
        outcome=outcome,
        note=note,
    )


@mcp.tool()
def list_backups() -> dict[str, Any]:
    """列出控制器创建的 Codex 与 LiteLLM 配置备份。只读。"""
    return _call(controller.list_backups)


@mcp.tool()
def restore_codex_config(backup_path: str, confirm: str = "") -> dict[str, Any]:
    """从控制器自己的备份目录恢复 ~/.codex/config.toml；要求 confirm='APPLY'。"""
    return _call(controller.restore_codex_config, backup_path=backup_path, confirm=confirm)


@mcp.tool()
def run_escalating_codex_task(
    repo_path: str,
    task: str,
    test_profile: str = "auto",
    max_escalations: int = 1,
    allow_dirty: bool = False,
    confirm: str = "",
) -> dict[str, Any]:
    """在主目录内的 Git 根目录运行受沙箱保护的 Codex 任务，并按白名单测试验证；失败最多升级一次到 Sol/xhigh。干净工作区用 confirm='RUN_CODEX'；明确允许脏工作区时用 allow_dirty=true 且 confirm='RUN_CODEX_DIRTY'。"""
    return _call(
        controller.start_codex_task,
        repo_path=repo_path,
        task=task,
        test_profile=test_profile,
        max_escalations=max_escalations,
        allow_dirty=allow_dirty,
        confirm=confirm,
    )


@mcp.tool()
def codex_task_status(task_id: str) -> dict[str, Any]:
    """读取受管 Codex 任务的执行、验证、升级和最终 Git 状态。只读。"""
    return _call(controller.codex_task_status, task_id=task_id)


@mcp.tool()
def list_codex_tasks(limit: int = 20) -> dict[str, Any]:
    """列出最近受管 Codex 任务及其运行和成功状态。只读。"""
    return _call(controller.list_codex_tasks, limit=limit)


@mcp.tool()
def stop_codex_task(task_id: str, confirm: str = "") -> dict[str, Any]:
    """停止指定的受管任务及其子进程；要求 confirm='STOP_CODEX'。"""
    return _call(controller.stop_codex_task, task_id=task_id, confirm=confirm)


def main() -> int:
    # stdio is required for OpenAI Secure MCP Tunnel. Nothing except MCP frames
    # may be printed to stdout; logging is configured to stderr above.
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
