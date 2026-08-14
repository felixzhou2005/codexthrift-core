from __future__ import annotations

import subprocess

import codexthrift.platform.process as process_module
from codexthrift import util


def test_stop_process_tree_falls_back_when_child_enumeration_is_blocked(monkeypatch) -> None:
    calls: list[str] = []

    class Process:
        def children(self, recursive):
            raise PermissionError("sandbox")

        def terminate(self):
            calls.append("terminate")

        def is_running(self):
            return False

    monkeypatch.setattr(process_module, "ensure_expected_process", lambda pid, fragments: Process())
    monkeypatch.setattr(process_module.os, "name", "nt")
    monkeypatch.setattr(
        process_module.psutil,
        "wait_procs",
        lambda processes, timeout: (_ for _ in ()).throw(PermissionError("sandbox")),
    )
    util.stop_process_tree(42, ["managed"], timeout=0.01)
    assert calls == ["terminate"]


def test_listening_pid_uses_lsof_when_psutil_is_blocked(monkeypatch) -> None:
    class Process:
        def get(self, key, default=None):
            return True if key == "running" else default

    monkeypatch.setattr(
        process_module.psutil,
        "net_connections",
        lambda **_kwargs: (_ for _ in ()).throw(PermissionError("sandbox")),
    )
    monkeypatch.setattr(process_module, "process_info", lambda _pid: {"running": True})
    monkeypatch.setattr(process_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        process_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="57472\n", stderr=""
        ),
    )

    assert process_module.listening_pid("127.0.0.1", 4000) == 57472


def test_listening_pid_returns_none_when_lsof_has_no_valid_pid(monkeypatch) -> None:
    monkeypatch.setattr(
        process_module.psutil,
        "net_connections",
        lambda **_kwargs: (_ for _ in ()).throw(PermissionError("sandbox")),
    )
    monkeypatch.setattr(process_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        process_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=1, stdout="not-a-pid\n", stderr=""
        ),
    )

    assert process_module.listening_pid("127.0.0.1", 4000) is None


def test_force_kill_process_sends_sigkill_and_waits(monkeypatch) -> None:
    signals: list[tuple[int, int]] = []
    states = iter([True, False])
    monkeypatch.setattr(
        process_module.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )
    monkeypatch.setattr(
        process_module,
        "process_info",
        lambda _pid: {"running": next(states)},
    )
    monkeypatch.setattr(process_module.time, "sleep", lambda _seconds: None)

    process_module.force_kill_process(57472)

    assert signals == [(57472, process_module.signal.SIGKILL)]
