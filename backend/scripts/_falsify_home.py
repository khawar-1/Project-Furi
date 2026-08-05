"""Falsification harness for the Home & IoT round (Feature 1).

Every behavioural change must be proven to FAIL when reverted IN PLACE — never
`git show :file` (2026-08-01: in a tree with a large uncommitted baseline that
is not "the code before this change"). The correct signature is:

    behavioural test FAILS   +   regression test PASSES

Lessons this harness encodes, each learned the hard way in this project:
  - the anchor must be a WHOLE LINE including its exact indentation, and UNIQUE
    (an indentation-mismatched revert produced an IndentationError and every
    test failed for the wrong reason, which reads identical to success);
  - re-read the patched file to confirm the revert actually landed before
    trusting any result (three lying falsifications so far);
  - read pytest's EXIT CODE, not its summary text — 5 means nothing was
    collected, i.e. a typo'd test name, which scores the same as a failure;
  - a falsification must remove the GUARANTEE, not one of several copies of it;
  - restore under EVERY exit path, or the harness becomes the bug it hunts.

Run:  venv\\Scripts\\python scripts\\_falsify_home.py
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent

# (label, file, [(anchor_line, replacement_line)], behavioural_test, regression_test)
CASES = [
    (
        "entity-id guard is in the planner's reject chain",
        "app/agents/planner.py",
        [("                             lambda: _entity_id_violation(steps, entity_ids or set())),",
          "                             lambda: None),")],
        "tests/test_agent_planner.py",  # placeholder, replaced below
        None,
    ),
]


def _run(test_expr: str) -> tuple[bool, str]:
    """(passed, tail). Exit code 5 = nothing collected — never a pass."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *test_expr.split(), "-q", "--no-header", "-x"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode == 5:
        return False, "NO TESTS COLLECTED (bad test id) — result is meaningless"
    tail = (proc.stdout or "").strip().splitlines()
    return proc.returncode == 0, tail[-1] if tail else "(no output)"


def falsify(label: str, rel: str, edits: list[tuple[str, str]],
            behavioural: str, regression: str) -> bool:
    path = ROOT / rel
    original = io.open(path, encoding="utf-8").read()
    patched = original
    for anchor, replacement in edits:
        count = patched.count(anchor)
        if count != 1:
            print(f"  [SKIP] anchor not unique ({count} matches): {anchor.strip()[:60]}")
            return False
        patched = patched.replace(anchor, replacement)

    io.open(path, "w", encoding="utf-8").write(patched)
    try:
        # ⚠️ Verify the revert LANDED before trusting anything below it.
        on_disk = io.open(path, encoding="utf-8").read()
        for anchor, replacement in edits:
            if replacement.strip() and replacement not in on_disk:
                print("  [INVALID] revert did not land on disk")
                return False
        b_pass, b_tail = _run(behavioural)
        r_pass, r_tail = _run(regression) if regression else (True, "(none)")
    finally:
        io.open(path, "w", encoding="utf-8").write(original)
        assert io.open(path, encoding="utf-8").read() == original, "RESTORE FAILED"

    ok = (not b_pass) and r_pass
    print(f"  behavioural: {'FAIL (correct)' if not b_pass else 'PASSED (INVALID)'}  | {b_tail}")
    if regression:
        print(f"  regression : {'PASS (correct)' if r_pass else 'FAILED (INVALID)'}  | {r_tail}")
    print(f"  => {'VALID' if ok else 'INVALID — this change is not proven'}\n")
    return ok


def main() -> int:
    cases = build_cases()
    print(f"Falsifying {len(cases)} behavioural changes (revert in place)\n")
    results = []
    for label, rel, edits, behavioural, regression in cases:
        print(f"[{label}]")
        results.append(falsify(label, rel, edits, behavioural, regression))
    valid = sum(results)
    print(f"{valid}/{len(results)} proven")
    return 0 if valid == len(results) else 1


def build_cases():
    T = "tests/test_home_tools.py"
    return [
        (
            "1. entity-id guard sits in the planner reject chain",
            "app/agents/planner.py",
            [("                             lambda: _entity_id_violation(steps, entity_ids or set())),",
              "                             lambda: None),")],
            f"{T}::test_the_planner_refuses_a_draft_that_invents_an_entity_id",
            f"{T}::test_a_concrete_entity_id_is_ungrounded_at_draft_time",
        ),
        (
            "2. an ungrounded entity id is refused",
            "app/agents/planner.py",
            [("        if value not in entity_ids:\n            return (\n"
              '                f"step \'{s.description}\' targets home device \'{value}\', but no "',
              "        if False:\n            return (\n"
              '                f"step \'{s.description}\' targets home device \'{value}\', but no "')],
            f"{T}::test_a_hallucinated_id_is_rejected_even_when_a_read_ran",
            f"{T}::test_a_pending_placeholder_is_not_judged_yet",
        ),
        (
            "3. the approval card names the real device + room",
            "app/agents/planner.py",
            [("                _enrich_entity_action_detail(plan, step)",
              "                pass  # reverted")],
            f"{T}::test_the_approval_card_names_the_device_when_the_plan_pauses",
            f"{T}::test_the_card_names_the_room_and_the_current_state",
        ),
        (
            "4. a PENDING entity id fills in code",
            "app/agents/placeholder_resolver.py",
            [("        if _ENTITY_ID_PARAMS.get(template.tool) == key:\n"
              "            return _substitute_entity_id(template, key, completed)",
              "        if False:\n"
              "            return _substitute_entity_id(template, key, completed)")],
            f"{T}::test_a_named_device_fills_in_code",
            f"{T}::test_several_plausible_devices_are_never_picked_between",
        ),
        (
            "5. a scene placeholder never resolves to a light (domain filter)",
            "app/agents/placeholder_resolver.py",
            [("            if required_domain and domain_of(eid) != required_domain:\n"
              "                continue",
              "            if False:\n"
              "                continue")],
            f"{T}::test_a_scene_placeholder_never_resolves_to_a_light",
            f"{T}::test_a_named_device_fills_in_code",
        ),
        (
            "6. unknown service attributes are dropped, not forwarded",
            "app/tools/home_tools.py",
            [("    allowed = _DOMAIN_ATTRIBUTES.get(domain_of(entity_id), ())",
              "    allowed = tuple(attributes)  # reverted: forward whatever was sent")],
            f"{T}::test_allowed_attributes_ride_along_and_unknown_ones_are_dropped",
            f"{T}::test_set_device_state_calls_the_mapped_service",
        ),
        (
            "7. a sensor cannot be switched",
            "app/tools/home_tools.py",
            [("    if domain in _READ_ONLY_DOMAINS:", "    if False:")],
            f"{T}::test_a_sensor_cannot_be_switched",
            f"{T}::test_a_lock_uses_lock_not_turn_on",
        ),
        (
            "8. an out-of-range temperature fails rather than clamping",
            "app/tools/home_tools.py",
            [("            if not (CLIMATE_MIN_C <= temperature <= CLIMATE_MAX_C):",
              "            if False:")],
            f"{T}::test_an_out_of_range_temperature_fails_rather_than_clamping",
            f"{T}::test_set_climate_sends_mode_then_temperature",
        ),
        (
            "9. run_scene refuses a non-scene entity",
            "app/tools/home_tools.py",
            [('        if domain_of(entity_id) != "scene":', "        if False:")],
            f"{T}::test_run_scene_refuses_anything_that_is_not_a_scene",
            f"{T}::test_run_scene_activates_a_real_scene",
        ),
        (
            "10. the routing gate reaches the classifier for home requests",
            "app/api/task_router.py",
            [('    r"\\blights?\\b|\\blamps?\\b|\\bbulbs?\\b|\\bdoors?\\b|\\bblinds?\\b|"',
              '    r"\\bzzzznotanoun\\b|"')],
            f"{T}::test_the_routing_gate_fires_for_home_requests",
            f"{T}::test_ordinary_conversation_about_a_home_stays_closed",
        ),
        (
            "11. get_device_state shapes its row so grounding can read it",
            "app/tools/home_tools.py",
            [('        return _ok(self, {"devices": [row], "count": 1, **row})',
              "        return _ok(self, row)")],
            f"{T}::test_get_device_state_shapes_its_row_like_list_devices",
            f"{T}::test_get_device_state_reads_live_not_the_cached_list",
        ),
        (
            "12. the base-URL gate refuses a non-http scheme",
            "app/integrations/home_assistant.py",
            [('    if parsed.scheme not in ("http", "https"):', "    if False:")],
            f"{T}::test_a_bad_base_url_is_refused",
            f"{T}::test_base_url_is_normalized",
        ),
    ]


if __name__ == "__main__":
    raise SystemExit(main())
