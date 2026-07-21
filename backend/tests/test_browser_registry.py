"""The generic held-session registry (browser refactor Phase 2).

The property under test is LEAK-PROOFNESS BY CONSTRUCTION: five formerly
copy-pasted (session, meta, lock) triples are one class + one slot table, and
every aggregate teardown iterates that table — so "shutdown forgot a slot"
(the bug that leaked the profile-lock orphan) is no longer a writable state.
These tests drive the registry with inert fakes; the conftest hermetic
fixtures guarantee no real browser is ever near this file.
"""
import asyncio

from app.browser import registry


class FakeSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def _fresh(slot: str = "test") -> registry.HeldSessionRegistry:
    return registry.HeldSessionRegistry(slot)


# ------------------------------------------------------------- slot semantics
async def test_hold_then_take_returns_the_session_without_closing_it():
    reg = _fresh()
    fake = FakeSession()
    await reg.hold(fake, {"goal": "apply"})
    assert reg.peek() == {"goal": "apply"}
    taken = await reg.take()
    assert taken is fake
    assert not fake.closed          # take transfers ownership, never closes
    assert reg.peek() is None


async def test_take_empties_the_slot_so_a_second_take_gets_none():
    reg = _fresh()
    await reg.hold(FakeSession(), {})
    assert await reg.take() is not None
    assert await reg.take() is None     # a taken session can never be taken twice


async def test_hold_replaces_and_closes_the_previous_occupant():
    reg = _fresh()
    first, second = FakeSession(), FakeSession()
    await reg.hold(first, {"n": 1})
    await reg.hold(second, {"n": 2})
    assert first.closed             # one slot — the superseded session dies
    assert not second.closed
    assert reg.peek() == {"n": 2}


async def test_re_holding_the_same_session_does_not_close_it():
    reg = _fresh()
    fake = FakeSession()
    await reg.hold(fake, {"n": 1})
    await reg.hold(fake, {"n": 2})   # meta refresh, same session
    assert not fake.closed
    assert reg.peek() == {"n": 2}


async def test_discard_closes_and_reports_true_then_is_idempotent():
    reg = _fresh()
    fake = FakeSession()
    await reg.hold(fake, {})
    assert await reg.discard() is True
    assert fake.closed
    assert await reg.discard() is False    # discarding nothing is not an error


async def test_peek_returns_a_copy_never_a_live_reference():
    reg = _fresh()
    await reg.hold(FakeSession(), {"title": "x"})
    peeked = reg.peek()
    peeked["title"] = "mutated"
    assert reg.peek() == {"title": "x"}


async def test_clear_nowait_drops_without_closing():
    reg = _fresh()
    fake = FakeSession()
    await reg.hold(fake, {})
    reg.clear_nowait()
    assert not fake.closed          # test hygiene only — no teardown side effects
    assert reg.peek() is None


def test_lock_survives_a_new_event_loop():
    """Production runs every mutating call on the ONE browser loop, but the
    suite gives each test a fresh loop — a naively bound asyncio.Lock raises
    'bound to a different event loop' on the second test to touch a module
    registry. The lock must rebind when the running loop changes."""
    reg = _fresh()
    first, second = FakeSession(), FakeSession()
    asyncio.run(reg.hold(first, {"n": 1}))
    asyncio.run(reg.hold(second, {"n": 2}))     # a different loop entirely
    assert first.closed
    assert reg.peek() == {"n": 2}


# ------------------------------------------------------- the aggregate sweeps
async def test_close_all_held_covers_every_slot_in_the_table():
    """THE meta-test: hold a fake in every REGISTRIES slot and demand the
    aggregate closes them all. Adding a slot to the table automatically adds
    it here — 'shutdown forgot the new slot' can never quietly regress."""
    held = {}
    for slot, reg in registry.REGISTRIES.items():
        fake = FakeSession()
        held[slot] = fake
        await reg.hold(fake, {"slot": slot})

    await registry.close_all_held()

    for slot, fake in held.items():
        assert fake.closed, f"close_all_held missed slot {slot!r}"
        assert registry.REGISTRIES[slot].peek() is None


async def test_close_all_held_survives_a_failing_close():
    class Boom:
        async def close(self):
            raise RuntimeError("half-dead window")

    survivor = FakeSession()
    slots = list(registry.REGISTRIES)
    await registry.REGISTRIES[slots[0]].hold(Boom(), {})
    await registry.REGISTRIES[slots[-1]].hold(survivor, {})

    await registry.close_all_held()    # must not raise

    assert survivor.closed             # one failure never blocks the rest
    assert registry.REGISTRIES[slots[0]].peek() is None


async def test_reset_for_tests_clears_every_slot_without_closing():
    fakes = []
    for reg in registry.REGISTRIES.values():
        fake = FakeSession()
        fakes.append(fake)
        await reg.hold(fake, {})

    registry.reset_for_tests()

    assert all(not f.closed for f in fakes)
    assert all(reg.peek() is None for reg in registry.REGISTRIES.values())


def test_the_expected_slots_exist():
    """The five held-session situations the stack currently has. A rename or
    removal here must be deliberate — session.py's domain wrappers and the
    API/StatusBar surfaces key on these names."""
    assert set(registry.REGISTRIES) == {
        "media", "result_window", "commit", "challenge", "discovery",
    }
