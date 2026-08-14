from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import zlib
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .paths import WorkspacePaths
from .util import atomic_write_text

_AAD = b"routerllm-call-payload-v1"
_AAD_V2_IDENTITY = b"routerllm-call-payload-v2:json"
_AAD_V2_ZLIB = b"routerllm-call-payload-v2:zlib-json"
ARCHIVE_FORMAT_V2_IDENTITY = "aesgcm-json-v2"
ARCHIVE_FORMAT_V2_ZLIB = "aesgcm-zlib-json-v2"
MAX_DECOMPRESSED_PAYLOAD_BYTES = 512 * 1024 * 1024
_KEYCHAIN_SERVICE = "RouterLLM Call Content"


def encryption_key(paths: WorkspacePaths, *, create: bool = False) -> bytes | None:
    """Read the local content key, creating it only for an explicit capture path."""
    paths.ensure()
    account = hashlib.sha256(str(paths.root).encode("utf-8")).hexdigest()[:32]
    if os.sys.platform == "darwin":
        key = _keychain_key(account)
        if key is None and os.environ.get("CODEXTHRIFT_MACOS_APP_DATA") == "1":
            legacy_root = (Path.home() / ".litellm-codex-router").resolve()
            if paths.root != legacy_root:
                legacy_account = hashlib.sha256(str(legacy_root).encode("utf-8")).hexdigest()[:32]
                key = _keychain_key(legacy_account)
                if key is not None:
                    try:
                        _store_keychain_key(account, key)
                    except RuntimeError:
                        pass
        if key is not None or not create:
            return key
        generated = os.urandom(32)
        _store_keychain_key(account, generated)
        return generated

    if paths.content_key_file.exists():
        return _decode_key(paths.content_key_file.read_text(encoding="utf-8"))
    if not create:
        return None
    generated = os.urandom(32)
    atomic_write_text(paths.content_key_file, base64.urlsafe_b64encode(generated).decode("ascii") + "\n", mode=0o600)
    return generated


def encrypt_payload(key: bytes, payload: bytes) -> dict[str, str]:
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, payload, _AAD)
    return {
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
    }


def encrypt_archive_payload(key: bytes, payload: bytes) -> dict[str, str]:
    """Compress a new archive payload before encryption when that reduces its size."""
    compressed = zlib.compress(payload, level=6)
    if len(compressed) + 32 < len(payload):
        body = compressed
        payload_format = ARCHIVE_FORMAT_V2_ZLIB
        aad = _AAD_V2_ZLIB
    else:
        body = payload
        payload_format = ARCHIVE_FORMAT_V2_IDENTITY
        aad = _AAD_V2_IDENTITY
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, body, aad)
    return {
        "format": payload_format,
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
    }


def decrypt_payload(key: bytes, envelope: dict[str, str]) -> bytes:
    nonce = base64.urlsafe_b64decode(envelope["nonce"])
    ciphertext = base64.urlsafe_b64decode(envelope["ciphertext"])
    payload_format = envelope.get("format")
    if payload_format is None:
        return AESGCM(key).decrypt(nonce, ciphertext, _AAD)
    if payload_format == ARCHIVE_FORMAT_V2_IDENTITY:
        return AESGCM(key).decrypt(nonce, ciphertext, _AAD_V2_IDENTITY)
    if payload_format == ARCHIVE_FORMAT_V2_ZLIB:
        compressed = AESGCM(key).decrypt(nonce, ciphertext, _AAD_V2_ZLIB)
        return _bounded_zlib_decompress(compressed)
    raise ValueError("Unsupported encrypted payload format.")


def _bounded_zlib_decompress(payload: bytes) -> bytes:
    decompressor = zlib.decompressobj()
    decoded = decompressor.decompress(payload, MAX_DECOMPRESSED_PAYLOAD_BYTES + 1)
    if len(decoded) > MAX_DECOMPRESSED_PAYLOAD_BYTES or decompressor.unconsumed_tail:
        raise ValueError("Compressed payload exceeds the safe decoded size limit.")
    tail = decompressor.flush()
    if (
        len(decoded) + len(tail) > MAX_DECOMPRESSED_PAYLOAD_BYTES
        or not decompressor.eof
        or decompressor.unused_data
    ):
        raise ValueError("Compressed payload is invalid or exceeds the safe decoded size limit.")
    return decoded + tail


def _decode_key(value: str) -> bytes | None:
    try:
        decoded = base64.urlsafe_b64decode(value.strip())
    except (ValueError, TypeError):
        return None
    return decoded if len(decoded) == 32 else None


def _keychain_key(account: str) -> bytes | None:
    completed = subprocess.run(
        ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", account, "-w"],
        check=False,
        capture_output=True,
        text=True,
    )
    return _decode_key(completed.stdout) if completed.returncode == 0 else None


def _store_keychain_key(account: str, key: bytes) -> None:
    value = base64.urlsafe_b64encode(key).decode("ascii")
    completed = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", _KEYCHAIN_SERVICE, "-a", account, "-w", value],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("Unable to store the RouterLLM content-encryption key in macOS Keychain.")
