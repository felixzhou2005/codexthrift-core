from __future__ import annotations

from pathlib import Path

from ..errors import ControlError


def safe_home_path(raw_path: str, *, must_exist: bool = True) -> Path:
    candidate = Path(raw_path).expanduser().resolve()
    home = Path.home().resolve()
    try:
        candidate.relative_to(home)
    except ValueError as exc:
        raise ControlError(f"只允许操作当前用户主目录以内的路径：{candidate}") from exc
    if must_exist and not candidate.exists():
        raise ControlError(f"路径不存在：{candidate}")
    return candidate


def require_confirmation(value: str, expected: str = "APPLY") -> None:
    if value != expected:
        raise ControlError(f"本操作需要显式确认。请把 confirm 设置为 {expected!r}。")
