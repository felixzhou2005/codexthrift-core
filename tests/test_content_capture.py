from __future__ import annotations

import pytest

from codexthrift.content_capture import (
    ARCHIVE_FORMAT_V2_IDENTITY,
    ARCHIVE_FORMAT_V2_ZLIB,
    decrypt_payload,
    encrypt_archive_payload,
    encrypt_payload,
)


def test_legacy_and_versioned_payload_envelopes_remain_decryptable() -> None:
    key = b"c" * 32
    compressible = b'{"input":"' + (b"repeated-value-" * 4096) + b'"}'
    compact = b"{}"

    legacy = encrypt_payload(key, compressible)
    compressed = encrypt_archive_payload(key, compressible)
    identity = encrypt_archive_payload(key, compact)

    assert "format" not in legacy
    assert compressed["format"] == ARCHIVE_FORMAT_V2_ZLIB
    assert identity["format"] == ARCHIVE_FORMAT_V2_IDENTITY
    assert decrypt_payload(key, legacy) == compressible
    assert decrypt_payload(key, compressed) == compressible
    assert decrypt_payload(key, identity) == compact
    assert len(compressed["ciphertext"]) < len(legacy["ciphertext"]) // 10


def test_unknown_or_tampered_payload_format_is_rejected() -> None:
    key = b"f" * 32
    envelope = encrypt_archive_payload(key, b"payload" * 100)

    with pytest.raises(ValueError, match="Unsupported"):
        decrypt_payload(key, {**envelope, "format": "unknown-v9"})

    alternate = (
        ARCHIVE_FORMAT_V2_IDENTITY
        if envelope["format"] == ARCHIVE_FORMAT_V2_ZLIB
        else ARCHIVE_FORMAT_V2_ZLIB
    )
    with pytest.raises(Exception):
        decrypt_payload(key, {**envelope, "format": alternate})
