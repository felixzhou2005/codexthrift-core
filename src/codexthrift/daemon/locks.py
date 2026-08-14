from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import IO

from ..errors import ControlError


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        if os.name == "nt":
            raise ControlError("Windows daemon locking is not implemented yet.")
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise ControlError("codexthriftd is already running.") from exc
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(f"{os.getpid()}\n")
        self._handle.flush()
        if os.name != "nt":
            self.path.chmod(0o600)

    def release(self) -> None:
        if self._handle is None:
            return
        import fcntl

        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


class WriteCoordinator:
    def __init__(self) -> None:
        self.lock = threading.Lock()
