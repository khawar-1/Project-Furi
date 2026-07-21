"""
Phase 15.2 — the autofill profile: the grounded data source for form-filling.

Pins the CRUD accessor, the document path-safety reuse, and the FillProfile
snapshot's central guarantee: a SECRET value is never a grounding value and
never appears in what the LLM sees — only its key does, and code resolves the
real value at fill time (the password-never-read rule).
"""
import pytest

from app.core import autofill
from app.core.autofill import (
    FillProfile,
    answer_is_skip,
    derive_field_identity,
    normalize_key,
    secret_ref,
    to_snapshot,
)
from app.db.models import AutofillField


# ------------------------------------------------ field learning (2026-07-19)
# A raw form field name → a clean, canonical profile identity, so a value the
# user supplies once is remembered under a sensible key and never asked again.
class TestDeriveFieldIdentity:
    def test_framework_mangled_email(self):
        key, label, kind = derive_field_identity("ctl00$ContentPlaceHolder1$txtEmail")
        assert (key, label, kind) == ("email", "Email", "text")

    def test_array_bracketed_first_name(self):
        key, label, kind = derive_field_identity("applicant[first_name]")
        assert (key, label, kind) == ("first_name", "First name", "text")

    def test_camelcase_last_name(self):
        assert derive_field_identity("txtLastName")[0] == "last_name"

    def test_first_name_beats_bare_name(self):
        # Order matters: "first name" must not be classified as the full name.
        assert derive_field_identity("Your First Name")[0] == "first_name"
        assert derive_field_identity("full_name")[0] == "full_name"
        assert derive_field_identity("applicant_name")[0] == "full_name"

    def test_phone_variants(self):
        for raw in ("phone", "mobileNumber", "tel", "cellphone"):
            assert derive_field_identity(raw)[0] == "phone"

    def test_link_fields_are_kind_link(self):
        assert derive_field_identity("linkedin_url") == ("linkedin", "LinkedIn", "link")
        assert derive_field_identity("githubProfile")[0] == "github"
        assert derive_field_identity("portfolio")[2] == "link"

    def test_cover_letter_and_company(self):
        assert derive_field_identity("why_do_you_want_this_job")[0] == "cover_letter"
        assert derive_field_identity("currentEmployer")[0] == "company"

    def test_unknown_field_falls_back_to_a_readable_slug(self):
        key, label, kind = derive_field_identity("ctl00$txtVisaSponsorship")
        # Not dropped — kept under a slug of its own cleaned name.
        assert key and key == normalize_key(key)
        assert "visa" in key and kind == "text"
        assert label  # human-ish

    def test_a_urlish_answer_to_an_unknown_field_is_a_link(self):
        assert derive_field_identity("personal_page", "https://me.dev")[2] == "link"
        assert derive_field_identity("personal_page", "just text")[2] == "text"

    def test_never_raises_on_garbage(self):
        assert derive_field_identity("")[0]
        assert derive_field_identity("$$$[][]")[0]


def test_answer_is_skip():
    assert answer_is_skip("continue")
    assert answer_is_skip(" Skip ")
    assert answer_is_skip("")
    # A real value that merely starts with an affirmative-ish word is NOT a skip.
    assert not answer_is_skip("yes.man@example.com")
    assert not answer_is_skip("okayama-city")
    assert not answer_is_skip("me@example.com")


# ------------------------------------------------------------------ accessor
async def test_upsert_and_list_roundtrip(db_session):
    await autofill.upsert_field(db_session, "email", "Email", "me@example.com", "text")
    rows = await autofill.list_fields(db_session)
    assert [r.key for r in rows] == ["email"]
    assert rows[0].value == "me@example.com"


async def test_upsert_is_an_update_not_a_duplicate(db_session):
    await autofill.upsert_field(db_session, "email", "Email", "a@x.com")
    await autofill.upsert_field(db_session, "email", "Email", "b@x.com")
    rows = await autofill.list_fields(db_session)
    assert len(rows) == 1 and rows[0].value == "b@x.com"


async def test_delete(db_session):
    await autofill.upsert_field(db_session, "phone", "Phone", "555")
    assert await autofill.delete_field(db_session, "phone") is True
    assert await autofill.delete_field(db_session, "phone") is False


async def test_a_document_reuses_file_path_safety(db_session, tmp_path):
    good = tmp_path / "resume.pdf"
    good.write_text("cv")
    row = await autofill.upsert_field(db_session, "resume", "Resume", str(good), "document")
    assert row.kind == "document"
    # a nonexistent path is refused by the file-tools path safety
    with pytest.raises(ValueError):
        await autofill.upsert_field(
            db_session, "resume2", "Resume2", str(tmp_path / "nope.pdf"), "document"
        )


async def test_bad_kind_and_empty_value_rejected(db_session):
    with pytest.raises(ValueError):
        await autofill.upsert_field(db_session, "x", "X", "v", "weird")
    with pytest.raises(ValueError):
        await autofill.upsert_field(db_session, "y", "Y", "", "text")


async def test_load_profile_snapshot(db_session):
    await autofill.upsert_field(db_session, "name", "Name", "Khawar", "text")
    await autofill.upsert_field(db_session, "pin", "PIN", "1234", "secret")
    profile = await autofill.load_profile(db_session)
    assert "Khawar" in profile.grounding_values()
    assert "1234" not in profile.grounding_values()
    assert profile.secret_value("pin") == "1234"


def test_normalize_key():
    assert normalize_key("  Full Name ") == "full_name"
    assert normalize_key("Email") == "email"


# ------------------------------------------------------------- FillProfile
def test_a_secret_is_never_a_grounding_value_or_in_the_prompt():
    profile = to_snapshot([
        AutofillField(key="name", label="Name", value="Khawar", kind="text"),
        AutofillField(key="password", label="Password", value="hunter2", kind="secret"),
    ])
    vals = profile.grounding_values()
    assert "Khawar" in vals
    assert "hunter2" not in vals                       # never a grounding value
    block = profile.prompt_block()
    assert "hunter2" not in block                      # never shown to the LLM
    assert "password" in block                         # only the key/placeholder is
    assert secret_ref("password") in block
    assert profile.secret_value("password") == "hunter2"  # only code can read it


def test_resolve_secret_ref():
    profile = to_snapshot([AutofillField(key="pin", label="PIN", value="1234", kind="secret")])
    assert profile.resolve_secret_ref(secret_ref("pin")) == "1234"
    assert profile.resolve_secret_ref("{{secret:pin}}") == "1234"
    assert profile.resolve_secret_ref("just text") is None
    assert profile.resolve_secret_ref("{{secret:unknown}}") is None


def test_a_document_basename_grounds_an_upload_path():
    profile = to_snapshot([
        AutofillField(key="resume", label="Resume", value=r"C:\docs\my_resume.pdf", kind="document"),
    ])
    assert "my_resume.pdf" in profile.grounding_values()


def test_an_empty_profile_renders_nothing():
    assert FillProfile().is_empty
    assert FillProfile().prompt_block() == ""
