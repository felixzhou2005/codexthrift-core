from __future__ import annotations

import plistlib
from pathlib import Path

from codexthrift.launchd import LAUNCH_AGENT_LABEL, render_launch_agent


def test_launch_agent_is_user_scoped_and_runs_only_codexthriftd(tmp_path: Path) -> None:
    payload = plistlib.loads(
        render_launch_agent(
            executable="/Applications/CodexThrift.app/Contents/MacOS/codexthriftd",
            working_directory=tmp_path / "runtime",
            log_path=tmp_path / "runtime" / "logs" / "codexthriftd.log",
        )
    )

    assert payload["Label"] == LAUNCH_AGENT_LABEL
    assert payload["ProgramArguments"] == [
        "/Applications/CodexThrift.app/Contents/MacOS/codexthriftd"
    ]
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert payload["EnvironmentVariables"]["CODEX_ROUTER_HOME"] == str(tmp_path / "runtime")
