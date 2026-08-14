from __future__ import annotations

import json
from pathlib import Path

from codexthrift.daemon.build_identity import build_identity


def test_build_identity_uses_runtime_manifest_and_stable_build_id(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    manifest = {
        "schemaVersion": 1,
        "coreVersion": "0.1.1",
        "sourceCommit": "source-commit-test",
    }
    manifest_path = runtime_root / "runtime-manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    first = build_identity(
        data_root=tmp_path / "data",
        instance_id="instance-one",
        runtime_root=runtime_root,
        executable_path="/tmp/codexthriftd",
    )
    second = build_identity(
        data_root=tmp_path / "data",
        instance_id="instance-two",
        runtime_root=runtime_root,
        executable_path="/tmp/codexthriftd",
    )

    assert first["sourceCommit"] == "source-commit-test"
    assert len(first["runtimeManifestSha256"]) == 64
    assert first["coreBuildId"] == second["coreBuildId"]
    assert first["instanceId"] != second["instanceId"]
    assert first["dataRoot"] == str((tmp_path / "data").resolve())
    assert first["runtimeRoot"] == str(runtime_root.resolve())
    assert first["executablePath"] == str(Path("/tmp/codexthriftd").resolve())


def test_build_identity_falls_back_without_manifest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CODEXTHRIFT_SOURCE_COMMIT", "env-source")
    identity = build_identity(runtime_root=tmp_path, instance_id="instance-test")

    assert identity["sourceCommit"] == "env-source"
    assert identity["runtimeManifestSha256"] == "unavailable"
    assert identity["instanceId"] == "instance-test"
