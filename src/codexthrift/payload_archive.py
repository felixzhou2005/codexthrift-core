from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PayloadArchiveEntry:
    payload_ref: str
    byte_offset: int
    byte_length: int


def scan_payload_archive(
    path: Path,
    *,
    start_offset: int,
    max_records: int = 250,
    max_bytes: int = 128 * 1024 * 1024,
    max_seconds: float = 0.35,
) -> dict[str, Any]:
    if max_records < 1 or max_bytes < 1 or max_seconds <= 0:
        raise ValueError("Payload archive scan limits must be positive.")
    stat = path.stat()
    offset = max(0, min(start_offset, stat.st_size))
    entries: list[PayloadArchiveEntry] = []
    skipped = 0
    bytes_read = 0
    started = time.monotonic()
    complete = False

    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            line_offset = handle.tell()
            raw_line = handle.readline()
            if not raw_line:
                complete = True
                break
            if not raw_line.endswith(b"\n"):
                handle.seek(line_offset)
                break
            bytes_read += len(raw_line)
            try:
                envelope = json.loads(raw_line)
                payload_ref = (
                    envelope.get("payload_ref") if isinstance(envelope, dict) else None
                )
                if not isinstance(payload_ref, str) or not payload_ref:
                    skipped += 1
                else:
                    entries.append(
                        PayloadArchiveEntry(payload_ref, line_offset, len(raw_line))
                    )
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                skipped += 1
            offset = handle.tell()
            processed = len(entries) + skipped
            if (
                processed >= max_records
                or bytes_read >= max_bytes
                or time.monotonic() - started >= max_seconds
            ):
                complete = offset >= stat.st_size
                break

    return {
        "entries": entries,
        "skipped": skipped,
        "offset": offset,
        "file_size": stat.st_size,
        "file_mtime_ns": stat.st_mtime_ns,
        "file_inode": stat.st_ino,
        "complete": complete,
    }


def read_payload_envelope(
    path: Path,
    *,
    byte_offset: int,
    byte_length: int,
    expected_payload_ref: str,
) -> dict[str, str]:
    if byte_offset < 0 or byte_length < 1:
        raise ValueError("Invalid payload archive location.")
    with path.open("rb") as handle:
        handle.seek(byte_offset)
        raw_line = handle.read(byte_length)
    if len(raw_line) != byte_length or not raw_line.endswith(b"\n"):
        raise ValueError("Payload archive entry is incomplete.")
    envelope = json.loads(raw_line)
    if (
        not isinstance(envelope, dict)
        or envelope.get("payload_ref") != expected_payload_ref
    ):
        raise ValueError("Payload archive reference mismatch.")
    nonce = envelope.get("nonce")
    ciphertext = envelope.get("ciphertext")
    if not isinstance(nonce, str) or not isinstance(ciphertext, str):
        raise TypeError("Payload archive envelope is invalid.")
    result = {
        "payload_ref": expected_payload_ref,
        "nonce": nonce,
        "ciphertext": ciphertext,
    }
    payload_format = envelope.get("format")
    if payload_format is not None:
        if not isinstance(payload_format, str):
            raise TypeError("Payload archive format is invalid.")
        result["format"] = payload_format
    return result
