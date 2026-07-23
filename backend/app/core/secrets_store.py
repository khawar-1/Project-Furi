"""
Jarvis OS — secrets-at-rest (Browser refactor Phase 7)

Autofill SECRET field values are sensitive (a form password, an API key). They
were stored PLAINTEXT in the autofill_fields.value column — any local process, a
backup, or a synced copy of jarvis.db read them in the clear (the API masks them
on READ, but at-rest storage was not protected). This module encrypts them at
rest with Windows DPAPI (CryptProtectData / CryptUnprotectData via ctypes — zero
new dependency; the encryption key is derived and held by the OS for the logged-
in Windows user account, so the ciphertext is bound to this user+machine and
needs no key file of our own to guard — the same posture as ~/.jarvis/auth_token,
one level stronger).

Storage shape: encrypted values live in the SAME value column, discriminated by
a "dpapi:" prefix over base64 ciphertext. A value WITHOUT the prefix is a legacy
plaintext secret (or a non-secret field) — decrypt_secret() returns it unchanged,
and the idempotent startup migration (autofill.encrypt_plaintext_secrets) rewrites
every unprefixed SECRET row in place. This keeps the change forward/backward
tolerant: an old DB reads fine, and a machine where DPAPI is unavailable (a non-
Windows host, a stubbed backend) falls back to plaintext with a LOUD warning
rather than losing the value.

CRYPTO_BACKEND is the injectable seam (the STT_MODEL_FACTORY pattern): tests swap
a reversible fake so the suite never calls the real Win32 API (conftest autouse
_hermetic_secrets is the backstop). Only SECRET-kind values are ever encrypted —
plain text/link/document fields stay readable (they are grounding data the user
curates, not sensitive).
"""
from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes
from typing import Protocol

from loguru import logger

# Encrypted values carry this prefix over base64 ciphertext in the same column.
# A value without it is legacy plaintext (or a non-secret field) — passed through.
_PREFIX = "dpapi:"

# CRYPTPROTECT_UI_FORBIDDEN — never raise a UI prompt (we run headless).
_CRYPTPROTECT_UI_FORBIDDEN = 0x1

# Warn at most once per process about a missing/failed backend, so a machine
# without DPAPI does not spam the log on every secret write.
_warned = False


def _warn_once(message: str) -> None:
    global _warned
    if not _warned:
        logger.warning(message)
        _warned = True


class CryptoBackend(Protocol):
    """The at-rest crypto seam. `available()` gates whether encryption is even
    attempted; protect/unprotect operate on raw bytes (base64 + the prefix are
    handled by this module, not the backend)."""

    def available(self) -> bool: ...

    def protect(self, data: bytes) -> bytes: ...

    def unprotect(self, data: bytes) -> bytes: ...


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_call(data: bytes, *, protect: bool) -> bytes:
    """One CryptProtectData/CryptUnprotectData round-trip. Windows-only — the
    caller guards with available()."""
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    src = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(src, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(
        ctypes.byref(blob_in),
        None,  # szDataDescr
        None,  # pOptionalEntropy
        None,  # pvReserved
        None,  # pPromptStruct
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        name = "CryptProtectData" if protect else "CryptUnprotectData"
        raise OSError(f"{name} failed (error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


class _DpapiBackend:
    """Default backend: Windows DPAPI, bound to the current OS user account."""

    def available(self) -> bool:
        return sys.platform == "win32"

    def protect(self, data: bytes) -> bytes:
        return _dpapi_call(data, protect=True)

    def unprotect(self, data: bytes) -> bytes:
        return _dpapi_call(data, protect=False)


# The injectable seam. Tests replace this with a reversible fake.
CRYPTO_BACKEND: CryptoBackend = _DpapiBackend()


def is_encrypted(value: str | None) -> bool:
    """True when a stored value is our encrypted form (has the dpapi: prefix)."""
    return bool(value) and value.startswith(_PREFIX)


def encrypt_secret(value: str) -> str:
    """The at-rest form of a secret value. Idempotent (an already-encrypted value
    is returned unchanged). If the backend is unavailable or fails, the plaintext
    is returned with a LOUD one-time warning — a secret is never lost to a crypto
    hiccup, only left unprotected (and the log says so)."""
    if not value or is_encrypted(value):
        return value or ""
    backend = CRYPTO_BACKEND
    if not backend.available():
        _warn_once(
            "secrets_store: at-rest encryption unavailable on this host — "
            "autofill secret stored WITHOUT encryption"
        )
        return value
    try:
        cipher = backend.protect(value.encode("utf-8"))
        return _PREFIX + base64.b64encode(cipher).decode("ascii")
    except Exception as exc:  # pragma: no cover - defensive; falls back to plaintext
        _warn_once(
            f"secrets_store: encryption failed ({exc.__class__.__name__}) — "
            "autofill secret stored WITHOUT encryption"
        )
        return value


def decrypt_secret(value: str | None) -> str:
    """The plaintext of a stored secret. An unprefixed value is legacy plaintext
    (or was stored on a host without DPAPI) and is returned unchanged. A prefixed
    value that cannot be decrypted (a corrupt blob, or one sealed for a different
    Windows user) yields "" with a warning — an unusable secret is safer empty
    than surfaced as ciphertext (the fill then pauses to ask)."""
    if not value or not is_encrypted(value):
        return value or ""
    try:
        raw = base64.b64decode(value[len(_PREFIX):].encode("ascii"))
        return CRYPTO_BACKEND.unprotect(raw).decode("utf-8")
    except Exception as exc:
        logger.warning(
            f"secrets_store: could not decrypt an autofill secret "
            f"({exc.__class__.__name__}) — treating it as unset"
        )
        return ""
