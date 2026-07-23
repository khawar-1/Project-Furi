"""
Tests for secrets-at-rest (Browser refactor Phase 7).

The conftest autouse `_hermetic_secrets` fixture installs a reversible fake
backend, so these run deterministically on any platform. One test bypasses it to
exercise the REAL Windows DPAPI (skipped off win32).
"""
from __future__ import annotations

import sys

import pytest

from app.core import autofill, secrets_store
from app.db.models import AutofillField


# ------------------------------------------------------------- module funcs
def test_encrypt_produces_the_prefixed_at_rest_form():
    stored = secrets_store.encrypt_secret("hunter2")
    assert stored != "hunter2"
    assert secrets_store.is_encrypted(stored)
    assert stored.startswith("dpapi:")


def test_encrypt_decrypt_round_trips():
    for plaintext in ("hunter2", "a", "p@ss w0rd!", "unicode-ключ-🔒", "x" * 500):
        stored = secrets_store.encrypt_secret(plaintext)
        assert secrets_store.decrypt_secret(stored) == plaintext


def test_encrypt_is_idempotent():
    once = secrets_store.encrypt_secret("s3cret")
    twice = secrets_store.encrypt_secret(once)
    assert twice == once  # an already-encrypted value is returned unchanged
    assert secrets_store.decrypt_secret(twice) == "s3cret"


def test_unprefixed_value_is_legacy_plaintext_passthrough():
    # A value without the prefix is a legacy plaintext secret / non-secret field.
    assert secrets_store.decrypt_secret("plain-value") == "plain-value"
    assert not secrets_store.is_encrypted("plain-value")


def test_empty_and_none_are_safe():
    assert secrets_store.encrypt_secret("") == ""
    assert secrets_store.decrypt_secret("") == ""
    assert secrets_store.decrypt_secret(None) == ""
    assert not secrets_store.is_encrypted(None)
    assert not secrets_store.is_encrypted("")


def test_unavailable_backend_falls_back_to_plaintext():
    class _Down:
        def available(self) -> bool:
            return False

        def protect(self, data: bytes) -> bytes:  # pragma: no cover - never called
            raise AssertionError("protect must not be called when unavailable")

        def unprotect(self, data: bytes) -> bytes:  # pragma: no cover
            raise AssertionError

    original = secrets_store.CRYPTO_BACKEND
    secrets_store.CRYPTO_BACKEND = _Down()
    try:
        # Falls back to plaintext (LOUD-logged) rather than losing the value.
        assert secrets_store.encrypt_secret("keepme") == "keepme"
    finally:
        secrets_store.CRYPTO_BACKEND = original


def test_undecryptable_blob_yields_empty_not_ciphertext():
    # A prefixed value the backend cannot unprotect (corrupt / different user)
    # is treated as unset — never surfaced as ciphertext.
    class _Boom:
        def available(self) -> bool:
            return True

        def protect(self, data: bytes) -> bytes:  # pragma: no cover
            return data

        def unprotect(self, data: bytes) -> bytes:
            raise OSError("cannot decrypt")

    original = secrets_store.CRYPTO_BACKEND
    secrets_store.CRYPTO_BACKEND = _Boom()
    try:
        assert secrets_store.decrypt_secret("dpapi:AAAA") == ""
    finally:
        secrets_store.CRYPTO_BACKEND = original


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_real_dpapi_round_trips():
    original = secrets_store.CRYPTO_BACKEND
    secrets_store.CRYPTO_BACKEND = secrets_store._DpapiBackend()
    try:
        stored = secrets_store.encrypt_secret("real-secret-🔑")
        assert stored.startswith("dpapi:")
        assert stored != "real-secret-🔑"
        assert secrets_store.decrypt_secret(stored) == "real-secret-🔑"
    finally:
        secrets_store.CRYPTO_BACKEND = original


# --------------------------------------------------------- DB integration
async def test_secret_is_encrypted_in_the_row_and_decrypts_on_load(db_session):
    row = await autofill.upsert_field(db_session, "pw", "Password", "hunter2", "secret")
    # The at-rest row value is NOT the plaintext.
    assert row.value != "hunter2"
    assert secrets_store.is_encrypted(row.value)
    # ...but the loop's snapshot recovers it in code.
    profile = await autofill.load_profile(db_session)
    assert profile.secret_value("pw") == "hunter2"
    # And it never appears in the model-facing prompt block.
    assert "hunter2" not in profile.prompt_block()


async def test_non_secret_values_are_stored_readable(db_session):
    row = await autofill.upsert_field(db_session, "email", "Email", "a@b.com", "text")
    assert row.value == "a@b.com"  # text/link/document stay plaintext
    assert not secrets_store.is_encrypted(row.value)


async def test_migration_encrypts_plaintext_secrets_in_place(db_session):
    # Simulate a pre-Phase-7 row: a plaintext secret written straight to the table.
    db_session.add(AutofillField(key="legacy", label="Legacy", value="plainpw", kind="secret"))
    db_session.add(AutofillField(key="mail", label="Mail", value="x@y.com", kind="text"))
    await db_session.commit()

    changed = await autofill.encrypt_plaintext_secrets(db_session)
    assert changed == 1  # only the secret row

    legacy = await autofill.get_field(db_session, "legacy")
    assert secrets_store.is_encrypted(legacy.value)
    assert secrets_store.decrypt_secret(legacy.value) == "plainpw"
    # The non-secret field is untouched.
    mail = await autofill.get_field(db_session, "mail")
    assert mail.value == "x@y.com"

    # Idempotent: a second pass rewrites nothing.
    assert await autofill.encrypt_plaintext_secrets(db_session) == 0
    profile = await autofill.load_profile(db_session)
    assert profile.secret_value("legacy") == "plainpw"
