from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Mapping

from ..paths import WorkspacePaths
from .dispatcher import RpcDispatcher
from .ipc_server import serve

DEFAULT_PROXY_PORT = 4000
_MIN_PROXY_PORT = 1
_MAX_PROXY_PORT = 65535
LOGGER = logging.getLogger(__name__)


def configured_proxy_port(environ: Mapping[str, str] | None = None) -> int:
    values = os.environ if environ is None else environ
    raw_value = values.get("CODEXTHRIFT_PROXY_PORT", "").strip()
    if not raw_value:
        return DEFAULT_PROXY_PORT
    try:
        port = int(raw_value, 10)
    except ValueError:
        LOGGER.warning(
            "Invalid CODEXTHRIFT_PROXY_PORT=%r; using %s.",
            raw_value,
            DEFAULT_PROXY_PORT,
        )
        return DEFAULT_PROXY_PORT
    if not _MIN_PROXY_PORT <= port <= _MAX_PROXY_PORT:
        LOGGER.warning(
            "CODEXTHRIFT_PROXY_PORT=%r is outside %s-%s; using %s.",
            raw_value,
            _MIN_PROXY_PORT,
            _MAX_PROXY_PORT,
            DEFAULT_PROXY_PORT,
        )
        return DEFAULT_PROXY_PORT
    return port


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the CodexThrift local IPC daemon.")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        result = RpcDispatcher._handshake({"protocolMin": "1.0", "protocolMax": "1.0"})
        return 0 if result["protocolVersion"] == "1.0" else 1
    serve(WorkspacePaths.discover(), port=configured_proxy_port())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
