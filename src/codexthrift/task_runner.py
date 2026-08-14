from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .util import atomic_write_json, normalized_child_env, utc_now_iso

CODEX_TIMEOUT_SECONDS = 2 * 60 * 60
TEST_TIMEOUT_SECONDS = 30 * 60
MAX_CAPTURE_CHARS = 80_000


_SAFE_ENV_KEYS = {
    "APPDATA",
    "CODEX_HOME",
    "CODEX_ROUTER_HOME",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LOCALAPPDATA",
    "LOGNAME",
    "NO_COLOR",
    "NUMBER_OF_PROCESSORS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PYTHONIOENCODING",
    "PYTHONPATH",
    "SHELL",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USER",
    "USERPROFILE",
    "VIRTUAL_ENV",
    "WINDIR",
}
_SAFE_ENV_PREFIXES = ("LC_",)
_INFRA_FAILURE_MARKERS = (
    "authentication",
    "oauth",
    "unauthorized",
    "forbidden",
    "rate limit",
    "quota",
    "subscription",
    "connection refused",
    "failed to connect",
    "network error",
    "timed out",
    "timeout",
    "model not found",
    "unsupported model",
)


def _sanitized_env(*, for_tests: bool = False) -> dict[str, str]:
    """Build a minimal environment so repo tools cannot inherit API secrets.

    This reduces accidental credential leakage. It is not an operating-system
    network sandbox; only run verification commands from repositories you trust.
    """
    source = os.environ
    env = {
        key: value
        for key, value in source.items()
        if key in _SAFE_ENV_KEYS or key.startswith(_SAFE_ENV_PREFIXES)
    }
    env["NO_COLOR"] = "1"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    if for_tests:
        env["CI"] = "1"
    return normalized_child_env(env)


def _looks_like_infrastructure_failure(record: dict[str, Any]) -> bool:
    combined = (str(record.get("stdout", "")) + "\n" + str(record.get("stderr", ""))).lower()
    return any(marker in combined for marker in _INFRA_FAILURE_MARKERS)


def _log(message: str) -> None:
    print(f"[{utc_now_iso()}] {message}", flush=True)


def _tail(value: str, limit: int = 12_000) -> str:
    if len(value) <= limit:
        return value
    return "…<truncated>…\n" + value[-limit:]


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    stdin_text: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            input=stdin_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
            check=False,
        )
        return {
            "command": command,
            "returncode": completed.returncode,
            "stdout": _tail(completed.stdout, MAX_CAPTURE_CHARS),
            "stderr": _tail(completed.stderr, MAX_CAPTURE_CHARS),
            "duration_seconds": round(time.monotonic() - started, 3),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {
            "command": command,
            "returncode": 124,
            "stdout": _tail(stdout, MAX_CAPTURE_CHARS),
            "stderr": _tail(stderr, MAX_CAPTURE_CHARS),
            "duration_seconds": round(time.monotonic() - started, 3),
            "timed_out": True,
        }
    except OSError as exc:
        return {
            "command": command,
            "returncode": 127,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "duration_seconds": round(time.monotonic() - started, 3),
            "timed_out": False,
        }


def _project_python(repo: Path) -> str:
    candidates = [
        repo / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
        repo / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("python3") or shutil.which("python") or sys.executable


def _package_has_test_script(repo: Path) -> bool:
    package = repo / "package.json"
    if not package.is_file():
        return False
    try:
        value = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    scripts = value.get("scripts") if isinstance(value, dict) else None
    return isinstance(scripts, dict) and isinstance(scripts.get("test"), str)


def _has_python_tests(repo: Path) -> bool:
    if (repo / "tests").is_dir() or (repo / "pytest.ini").is_file() or (repo / "tox.ini").is_file():
        return True
    try:
        if any(repo.glob("test_*.py")) or any(repo.glob("*_test.py")):
            return True
    except OSError:
        pass
    for config_path, marker in (
        (repo / "pyproject.toml", "[tool.pytest"),
        (repo / "setup.cfg", "[tool:pytest]"),
    ):
        try:
            if marker in config_path.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            pass
    return False


def _detect_test_commands(repo: Path, profile: str) -> tuple[list[tuple[str, list[str]]], str]:
    commands: list[tuple[str, list[str]]] = []

    if profile in {"auto", "python"}:
        if profile == "python" or _has_python_tests(repo):
            commands.append(("python", [_project_python(repo), "-m", "pytest", "-q"]))

    if profile in {"auto", "node"}:
        if profile == "node" or _package_has_test_script(repo):
            npm = shutil.which("npm") or "npm"
            commands.append(("node", [npm, "test"]))

    if profile in {"auto", "go"}:
        if profile == "go" or (repo / "go.mod").is_file():
            go = shutil.which("go") or "go"
            commands.append(("go", [go, "test", "./..."]))

    if profile in {"auto", "rust"}:
        if profile == "rust" or (repo / "Cargo.toml").is_file():
            cargo = shutil.which("cargo") or "cargo"
            commands.append(("rust", [cargo, "test", "--quiet"]))

    # Always perform a non-executing whitespace/conflict check. This is not a
    # substitute for tests, but gives a useful minimum verification signal.
    git = shutil.which("git") or "git"
    commands.append(("git-diff-check", [git, "diff", "--check"]))
    commands.append(("git-index-diff-check", [git, "diff", "--cached", "--check"]))
    return commands, "auto-detected" if profile == "auto" else profile


def _write_command_output(path: Path, record: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _test_failure_context(test_records: list[dict[str, Any]]) -> str:
    failures = [row for row in test_records if row.get("returncode") != 0]
    if not failures:
        return ""
    chunks = []
    for row in failures[-3:]:
        chunks.append(
            f"Verification command {row.get('name')} failed with exit code {row.get('returncode')}.\n"
            f"STDOUT:\n{_tail(str(row.get('stdout') or ''), 5000)}\n"
            f"STDERR:\n{_tail(str(row.get('stderr') or ''), 5000)}"
        )
    return "\n\n".join(chunks)


def _build_prompt(original_task: str, attempt_index: int, previous_failure: str) -> str:
    safety = (
        "Work only inside the current Git repository. Do not access credentials, home-directory files, "
        "or network services unless the repository's existing tests strictly require them. Do not commit, "
        "push, reset, clean, or rewrite Git history. Preserve unrelated user changes. Implement the task, "
        "inspect the resulting diff, and run the most relevant tests available in the repository."
    )
    if attempt_index == 0:
        return f"{safety}\n\nTask:\n{original_task.strip()}"
    return (
        f"{safety}\n\nThis is escalation attempt {attempt_index}. A previous attempt did not pass verification. "
        "Inspect the current working tree rather than starting over. Find and fix the root cause, then rerun tests.\n\n"
        f"Original task:\n{original_task.strip()}\n\nPrevious verification failure:\n{previous_failure}"
    )


def _model_for_attempt(attempt_index: int) -> str:
    if attempt_index == 0:
        return "codex-task-auto"
    return "sol-xhigh"


def _run_codex(repo: Path, prompt: str, model: str, attempt_dir: Path) -> dict[str, Any]:
    codex = shutil.which("codex")
    if not codex:
        return {
            "command": ["codex"],
            "returncode": 127,
            "stdout": "",
            "stderr": "codex executable was not found on PATH",
            "duration_seconds": 0,
            "timed_out": False,
            "model": model,
        }

    final_message = attempt_dir / "final-message.md"
    command = [
        codex,
        "exec",
        "--ephemeral",
        "--json",
        "--color",
        "never",
        "--cd",
        str(repo),
        "--sandbox",
        "workspace-write",
        "--model",
        model,
        "-c",
        'model_provider="openai"',
        "-c",
        'approval_policy="never"',
        "-c",
        "sandbox_workspace_write.network_access=false",
        "--output-last-message",
        str(final_message),
        "-",
    ]
    env = _sanitized_env()
    record = _run(
        command,
        cwd=repo,
        timeout=CODEX_TIMEOUT_SECONDS,
        stdin_text=prompt,
        env=env,
    )
    record["model"] = model
    record["final_message_path"] = str(final_message)
    try:
        record["final_message"] = _tail(final_message.read_text(encoding="utf-8"), 20_000)
    except OSError:
        record["final_message"] = None
    return record


def _run_tests(repo: Path, commands: list[tuple[str, list[str]]], attempt_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, (name, command) in enumerate(commands, start=1):
        _log(f"verification {index}/{len(commands)}: {name}")
        record = _run(
            command,
            cwd=repo,
            timeout=TEST_TIMEOUT_SECONDS,
            env=_sanitized_env(for_tests=True),
        )
        record["name"] = name
        records.append(record)
        _write_command_output(attempt_dir / f"verification-{index}-{name}.json", record)
    return records


def _git_snapshot(repo: Path) -> dict[str, Any]:
    git = shutil.which("git") or "git"
    env = _sanitized_env()
    head = _run([git, "rev-parse", "HEAD"], cwd=repo, timeout=30, env=env)
    status = _run([git, "status", "--porcelain=v1"], cwd=repo, timeout=30, env=env)
    diff_stat = _run([git, "diff", "--stat"], cwd=repo, timeout=30, env=env)
    return {
        "head": head.get("stdout", "").strip() if head.get("returncode") == 0 else None,
        "status_porcelain": _tail(str(status.get("stdout") or ""), 30_000),
        "diff_stat": _tail(str(diff_stat.get("stdout") or ""), 30_000),
    }


def execute(spec_path: Path, result_path: Path) -> dict[str, Any]:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    repo = Path(spec["repo_path"]).expanduser().resolve()
    task = str(spec["task"])
    profile = str(spec.get("test_profile") or "auto")
    max_escalations = max(0, min(int(spec.get("max_escalations", 1)), 1))
    run_dir = result_path.parent

    commands, verification_mode = _detect_test_commands(repo, profile)
    result: dict[str, Any] = {
        "task_id": spec.get("task_id"),
        "started_at": utc_now_iso(),
        "finished_at": None,
        "repo_path": str(repo),
        "test_profile": profile,
        "verification_mode": verification_mode,
        "detected_verifications": [{"name": name, "command": command} for name, command in commands],
        "initial_git": _git_snapshot(repo),
        "attempts": [],
        "success": False,
        "verification_strength": (
            "tests"
            if any(name not in {"git-diff-check", "git-index-diff-check"} for name, _ in commands)
            else "diff-check-only"
        ),
    }
    atomic_write_json(result_path, result, mode=0o600)

    previous_failure = ""
    for attempt_index in range(max_escalations + 1):
        model = _model_for_attempt(attempt_index)
        attempt_dir = run_dir / f"attempt-{attempt_index + 1}-{model}"
        attempt_dir.mkdir(parents=True, exist_ok=False)
        prompt = _build_prompt(task, attempt_index, previous_failure)
        # Keep the full user prompt local to the protected task run directory;
        # routing audit logs contain only its fingerprint and length.
        (attempt_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

        _log(f"attempt {attempt_index + 1}/{max_escalations + 1}: model={model}")
        codex_record = _run_codex(repo, prompt, model, attempt_dir)
        _write_command_output(attempt_dir / "codex.json", codex_record)

        test_records: list[dict[str, Any]] = []
        if codex_record.get("returncode") == 0:
            test_records = _run_tests(repo, commands, attempt_dir)

        verification_ok = all(row.get("returncode") == 0 for row in test_records)
        if codex_record.get("returncode") != 0:
            verification_ok = False

        attempt = {
            "attempt": attempt_index + 1,
            "model": model,
            "started_with_escalation": attempt_index > 0,
            "codex": codex_record,
            "verification": test_records,
            "verification_ok": verification_ok,
            "git_after": _git_snapshot(repo),
        }
        result["attempts"].append(attempt)
        result["success"] = verification_ok
        result["final_model"] = model
        result["final_git"] = attempt["git_after"]
        atomic_write_json(result_path, result, mode=0o600)

        if verification_ok:
            _log(f"task completed successfully with {model}")
            break

        infrastructure_failure = (
            codex_record.get("returncode") != 0
            and _looks_like_infrastructure_failure(codex_record)
        )
        attempt["infrastructure_failure"] = infrastructure_failure
        if codex_record.get("returncode") != 0:
            previous_failure = (
                f"Codex exited with code {codex_record.get('returncode')}.\n"
                f"STDOUT:\n{_tail(str(codex_record.get('stdout') or ''), 5000)}\n"
                f"STDERR:\n{_tail(str(codex_record.get('stderr') or ''), 5000)}"
            )
        else:
            previous_failure = _test_failure_context(test_records) or "Verification failed without diagnostic output."

        if infrastructure_failure:
            result["escalation_skipped_reason"] = (
                "Codex failed for authentication, quota, model compatibility, connection, or timeout reasons; "
                "changing to a more expensive model would not be a meaningful recovery."
            )
            _log("infrastructure failure detected; model escalation skipped")
            atomic_write_json(result_path, result, mode=0o600)
            break

        if attempt_index < max_escalations:
            _log("verification failed; escalating model/effort for the next attempt")

    result["finished_at"] = utc_now_iso()
    result["attempt_count"] = len(result["attempts"])
    result["escalations_used"] = max(0, len(result["attempts"]) - 1)
    if result["success"] and result["verification_strength"] == "diff-check-only":
        result["caveat"] = (
            "No supported project test command was detected; success means Codex exited cleanly and git diff --check passed, "
            "not that application behavior was proven correct."
        )
    if not result["success"]:
        result["error"] = "All permitted attempts failed Codex execution or verification. Review per-attempt logs before manual recovery."
    atomic_write_json(result_path, result, mode=0o600)
    router_home = os.environ.get("CODEX_ROUTER_HOME")
    try:
        if not router_home:
            return result
        from .codex_session_analyzer import import_task_run
        from .history_store import HistoryStore
        from .paths import WorkspacePaths

        import_task_run(HistoryStore(WorkspacePaths.default().adaptive_db), run_dir)
    except Exception as exc:
        result["adaptive_history_error"] = f"{type(exc).__name__}: {exc}"
        atomic_write_json(result_path, result, mode=0o600)
    return result


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python -m codexthrift.task_runner SPEC.json RESULT.json", file=sys.stderr)
        return 2
    spec_path = Path(sys.argv[1]).expanduser().resolve()
    result_path = Path(sys.argv[2]).expanduser().resolve()
    try:
        execute(spec_path, result_path)
        return 0
    except Exception as exc:
        failure = {
            "success": False,
            "fatal_error": f"{type(exc).__name__}: {exc}",
            "finished_at": utc_now_iso(),
        }
        try:
            atomic_write_json(result_path, failure, mode=0o600)
        except Exception:
            pass
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
