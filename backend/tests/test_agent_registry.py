"""Domain agent registry (boss + specialized agents)."""
import app.tools  # noqa: F401 — registers every tool into the global registry
from app.agents.agent_registry import (
    AGENTS,
    GENERAL,
    agent_for_key,
    agent_for_label,
)
from app.tools.registry import registry


def test_every_agent_tool_is_a_real_registered_tool():
    """A tool subset that names a tool the registry doesn't have would silently
    starve an agent — the LLM would never be shown it. Guard against a typo."""
    known = set(registry.names())
    for key, spec in AGENTS.items():
        if spec.tools is None:
            continue  # general = all tools
        missing = spec.tools - known
        assert not missing, f"agent '{key}' references unknown tools: {missing}"


def test_general_agent_sees_all_tools():
    # None = no filtering; the pre-agent behavior, so nothing regresses.
    assert GENERAL.tools is None
    assert GENERAL.key == "general"
    assert GENERAL.persona == ""


def test_label_to_agent_mapping():
    assert agent_for_label("TASK").key == "file"
    assert agent_for_label("EMAIL").key == "email"
    assert agent_for_label("CALENDAR").key == "calendar"
    assert agent_for_label("WEB").key == "research"
    assert agent_for_label("BROWSE").key == "browser"


def test_unknown_or_chat_label_falls_to_general():
    assert agent_for_label("CHAT").key == "general"
    assert agent_for_label("NONSENSE").key == "general"
    assert agent_for_label(None).key == "general"
    assert agent_for_label("").key == "general"


def test_label_matching_is_case_insensitive():
    assert agent_for_label("email").key == "email"
    assert agent_for_label(" Browse ").key == "browser"


def test_agent_for_key_roundtrips_and_defaults():
    for key in AGENTS:
        assert agent_for_key(key).key == key
    assert agent_for_key("does-not-exist").key == "general"
    assert agent_for_key(None).key == "general"


def test_shared_reads_present_in_every_domain_agent():
    """"Tools not starved": every domain agent keeps the shared read tools so a
    cross-domain read chains (the email agent can still search_files)."""
    shared = {"recall_memory", "lookup_contact", "search_files", "read_file"}
    for key, spec in AGENTS.items():
        if spec.tools is None:
            continue
        assert shared <= spec.tools, f"agent '{key}' is missing shared reads"


def test_email_agent_cannot_see_file_destructive_tools():
    """Specialization is real: the email agent's catalog has send_email but NOT
    delete_file / run_command (another domain's destructive tools)."""
    email = agent_for_label("EMAIL")
    assert "send_email" in email.tools
    assert "search_files" in email.tools      # shared read — chaining allowed
    assert "delete_file" not in email.tools
    assert "run_command" not in email.tools
    assert "browse_commit" not in email.tools
