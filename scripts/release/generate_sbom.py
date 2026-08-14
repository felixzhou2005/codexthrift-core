from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from release_lib import project_version, release_artifact_names, sha256, write_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    args = parser.parse_args()
    runtime = json.loads(
        (args.release_dir / "runtime-manifest.json").read_text(encoding="utf-8")
    )
    packages: dict[tuple[str, str], set[str]] = {}
    for architecture, entries in runtime["architectures"].items():
        for entry in entries:
            packages.setdefault((entry["name"], entry["version"]), set()).add(
                architecture
            )
    for architecture, version in runtime["opensslVersions"].items():
        packages.setdefault(("OpenSSL", version), set()).add(architecture)
    spdx_packages = [
        {
            "SPDXID": "SPDXRef-Package-CodexThrift",
            "name": "codexthrift",
            "versionInfo": project_version(),
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "MIT",
            "licenseDeclared": "MIT",
            "copyrightText": "NOASSERTION",
        }
    ]
    for index, ((name, version), architectures) in enumerate(
        sorted(packages.items()), start=1
    ):
        if name.lower() == "codexthrift":
            continue
        spdx_packages.append(
            {
                "SPDXID": f"SPDXRef-Package-{index}",
                "name": name,
                "versionInfo": version,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
                "copyrightText": "NOASSERTION",
                "comment": f"Runtime architectures: {', '.join(sorted(architectures))}",
            }
        )
    files = []
    excluded = {"SHA256SUMS", f"codexthrift-core-v{project_version()}.spdx.json"}
    artifact_names = sorted(release_artifact_names(project_version()) - excluded)
    for index, name in enumerate(artifact_names, start=1):
        path = args.release_dir / name
        if not path.is_file():
            raise RuntimeError(f"cannot describe missing release artifact: {name}")
        files.append(
            {
                "SPDXID": f"SPDXRef-File-{index}",
                "fileName": path.name,
                "checksums": [{"algorithm": "SHA256", "checksumValue": sha256(path)}],
                "licenseConcluded": "NOASSERTION",
                "copyrightText": "NOASSERTION",
            }
        )
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    created = (
        datetime.fromtimestamp(int(source_date_epoch), UTC)
        if source_date_epoch is not None
        else datetime.now(UTC)
    )
    document = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"CodexThrift Core {project_version()}",
        "documentNamespace": f"https://codexthrift.dev/spdx/core-{project_version()}-{runtime['sourceCommit']}",
        "creationInfo": {
            "created": created.replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "creators": ["Tool: CodexThrift release tooling"],
        },
        "packages": spdx_packages,
        "files": files,
        "relationships": [
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": "SPDXRef-Package-CodexThrift",
            }
        ],
    }
    write_json(
        args.release_dir / f"codexthrift-core-v{project_version()}.spdx.json", document
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
