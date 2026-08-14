from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote

from .errors import ControlError
from .paths import WorkspacePaths

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def list_codex_sessions(
    paths: WorkspacePaths,
    *,
    limit: int = 100,
    provider: str = "all",
    archived: bool = False,
) -> dict[str, Any]:
    if not 1 <= limit <= 1000:
        raise ControlError("session 数量必须在 1 到 1000 之间。")
    if provider not in {"all", "openai", "litellm"}:
        raise ControlError("provider 必须是 all、openai 或 litellm。")

    database = _latest_state_database(paths.codex_home)
    query = """
        SELECT id, model_provider, cwd, COALESCE(NULLIF(name, ''), NULLIF(title, ''), '(untitled)'),
               created_at, updated_at, recency_at, archived
        FROM threads
        WHERE source IN ('cli', 'vscode') AND archived = ?
    """
    params: list[Any] = [1 if archived else 0]
    if provider != "all":
        query += " AND model_provider = ?"
        params.append(provider)
    query += " ORDER BY recency_at DESC, updated_at DESC, created_at DESC LIMIT ?"
    params.append(limit)

    try:
        uri = f"file:{quote(str(database))}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2.0) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()
    except sqlite3.Error as exc:
        raise ControlError(f"无法只读查询 Codex session 状态库：{exc}") from exc

    sessions = [
        {
            "id": row["id"],
            "model_provider": row["model_provider"],
            "cwd": row["cwd"],
            "title": row[3],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "recency_at": row["recency_at"],
            "archived": bool(row["archived"]),
            "resume_command": ["codex", "resume", row["id"]],
        }
        for row in rows
    ]
    counts: dict[str, int] = {}
    for session in sessions:
        key = str(session["model_provider"])
        counts[key] = counts.get(key, 0) + 1
    return {
        "ok": True,
        "codex_home": str(paths.codex_home),
        "database": str(database),
        "provider_filter": provider,
        "archived": archived,
        "count": len(sessions),
        "provider_counts": counts,
        "sessions": sessions,
    }


def launch_codex_session(
    paths: WorkspacePaths,
    session_id: str,
    *,
    executable: str = "codex",
    execvp: Callable[[str, list[str]], NoReturn] = os.execvp,
) -> NoReturn:
    if _SESSION_ID_RE.fullmatch(session_id) is None:
        raise ControlError("Codex session ID 格式无效。")
    database = _latest_state_database(paths.codex_home)
    try:
        uri = f"file:{quote(str(database))}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2.0) as connection:
            row = connection.execute(
                "SELECT model_provider FROM threads WHERE id = ? AND source IN ('cli', 'vscode')",
                (session_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise ControlError(f"无法只读查询 Codex session 状态库：{exc}") from exc
    if row is None:
        raise ControlError(f"未找到可恢复的 Codex session：{session_id}")
    try:
        execvp(executable, [executable, "resume", session_id])
    except OSError as exc:
        raise ControlError(f"无法启动 Codex CLI：{exc}") from exc
    raise AssertionError("execvp returned unexpectedly")


def _latest_state_database(codex_home: Path) -> Path:
    candidates: list[tuple[int, Path]] = []
    for path in codex_home.glob("state_*.sqlite"):
        matched = re.fullmatch(r"state_(\d+)\.sqlite", path.name)
        if matched is not None and path.is_file():
            candidates.append((int(matched.group(1)), path))
    if not candidates:
        raise ControlError(f"未找到 Codex session 状态库：{codex_home}")
    return max(candidates, key=lambda item: item[0])[1]
