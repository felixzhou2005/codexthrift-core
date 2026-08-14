from __future__ import annotations

import sys

from .paths import WorkspacePaths


def main() -> int:
    path = WorkspacePaths.default().master_key_file
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(f"Cannot read LiteLLM local key: {exc}", file=sys.stderr)
        return 1
    if not value:
        print("LiteLLM local key is empty", file=sys.stderr)
        return 1
    # Codex expects the bearer token and nothing else on stdout.
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
