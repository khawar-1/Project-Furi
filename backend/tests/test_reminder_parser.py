"""
Phase 4 Part 4 — deterministic reminder time/text parsing.

parse_reminder() takes an explicit `now` everywhere so results are
reproducible regardless of when the suite runs.
"""
from datetime import datetime, timedelta

from app.core.reminder_parser import (
    looks_like_reminder,
    parse_reminder,
    parse_reminders,
    parse_time_reply,
)

NOON = datetime(2026, 7, 8, 12, 0, 0).astimezone()  # a Wednesday


# ================================================================== the gate

def test_looks_like_reminder_fires_on_trigger_phrases():
    assert looks_like_reminder("remind me at 6 to call jamil")
    assert looks_like_reminder("set a reminder for tomorrow at 9am to pay rent")
    assert looks_like_reminder("set reminder to check the oven at 6pm")


def test_looks_like_reminder_ignores_unrelated_use_of_remind():
    assert not looks_like_reminder("that reminds me of my brother")
    assert not looks_like_reminder("this reminder app is nice")


def test_parse_reminder_returns_none_for_non_reminder_message():
    assert parse_reminder("how are you today", now=NOON) is None
    assert parse_reminder("delete my temp files", now=NOON) is None


# ============================================================== relative time

def test_relative_minutes_is_unambiguous():
    r = parse_reminder("remind me in 20 minutes to check the oven", now=NOON)
    assert r.matched and not r.ambiguous
    assert r.due_at == NOON + timedelta(minutes=20)
    assert r.text == "check the oven"


def test_relative_hours_and_days():
    r = parse_reminder("remind me in 2 hours to call mom", now=NOON)
    assert r.due_at == NOON + timedelta(hours=2)
    r2 = parse_reminder("remind me in 3 days to renew the passport", now=NOON)
    assert r2.due_at == NOON + timedelta(days=3)


def test_relative_with_no_task_text_asks():
    r = parse_reminder("remind me in 20 minutes", now=NOON)
    assert r.ambiguous
    assert "what should i remind you" in r.question.lower()


# ============================================================ bare-hour clock

def test_bare_hour_after_noon_picks_the_only_future_candidate():
    # 6am has passed, 6pm hasn't — the only future candidate wins, no
    # PM-preference tie-break needed.
    r = parse_reminder("remind me at 6 to call jamil", now=NOON)
    assert not r.ambiguous
    assert r.due_at == NOON.replace(hour=18, minute=0)
    assert r.text == "call jamil"


def test_bare_hour_early_morning_ties_to_pm():
    early = NOON.replace(hour=2, minute=0)  # 2am: both 6am and 6pm are ahead
    r = parse_reminder("remind me at 6 to call jamil", now=early)
    assert r.due_at == early.replace(hour=18)


def test_bare_hour_both_passed_rolls_to_tomorrow_pm():
    late = NOON.replace(hour=23, minute=0)  # 11pm: 6am and 6pm both gone
    r = parse_reminder("remind me at 6 to call jamil", now=late)
    assert r.due_at == (late + timedelta(days=1)).replace(hour=18, minute=0)


def test_explicit_am_pm_is_unambiguous():
    r = parse_reminder("remind me at 6am to go for a run", now=NOON)
    assert r.due_at == (NOON + timedelta(days=1)).replace(hour=6, minute=0)  # 6am today passed -> rolls
    r2 = parse_reminder("remind me at 6:30pm to check the oven", now=NOON)
    assert r2.due_at == NOON.replace(hour=18, minute=30)


def test_today_qualifier_in_the_past_is_not_silently_rolled():
    r = parse_reminder("remind me today at 6am to go for a run", now=NOON)
    assert r.ambiguous
    assert "already passed" in r.question.lower()


def test_tomorrow_qualifier_with_explicit_time():
    r = parse_reminder("remind me tomorrow at 9am to call jamil", now=NOON)
    assert not r.ambiguous
    assert r.due_at == (NOON + timedelta(days=1)).replace(hour=9, minute=0)


def test_tomorrow_qualifier_bare_hour_defaults_pm():
    r = parse_reminder("remind me tomorrow at 6 to call jamil", now=NOON)
    assert not r.ambiguous
    assert r.due_at == (NOON + timedelta(days=1)).replace(hour=18, minute=0)


def test_24_hour_clock_is_unambiguous():
    r = parse_reminder("remind me at 18:30 to check the oven", now=NOON)
    assert not r.ambiguous
    assert r.due_at == NOON.replace(hour=18, minute=30)


def test_task_text_extracted_when_time_comes_before_task():
    r = parse_reminder("remind me at 6pm to call jamil", now=NOON)
    assert r.text == "call jamil"


def test_task_text_extracted_when_task_comes_before_time():
    r = parse_reminder("remind me to call jamil at 6pm", now=NOON)
    assert r.text == "call jamil"


# =============================================================== ISO date

def test_iso_date_with_time_is_unambiguous():
    r = parse_reminder("remind me on 2026-07-10 at 5pm to submit the report", now=NOON)
    assert not r.ambiguous
    assert r.due_at == datetime(2026, 7, 10, 17, 0, tzinfo=NOON.tzinfo)
    assert r.text == "submit the report"


def test_iso_date_without_time_asks():
    r = parse_reminder("remind me on 2026-07-10 to submit the report", now=NOON)
    assert r.ambiguous
    assert "2026-07-10" in r.question


def test_iso_date_in_the_past_asks():
    r = parse_reminder("remind me on 2026-07-01 at 5pm to submit the report", now=NOON)
    assert r.ambiguous
    assert "already passed" in r.question.lower()


def test_malformed_iso_date_asks():
    r = parse_reminder("remind me on 2026-02-30 at 5pm to submit the report", now=NOON)
    assert r.ambiguous
    assert "isn't a valid date" in r.question


# ============================================================== no time given

def test_no_time_expression_at_all_asks():
    r = parse_reminder("remind me to call jamil", now=NOON)
    assert r.ambiguous
    assert "what time" in r.question.lower()


def test_bare_day_word_without_clock_time_asks_with_hint():
    r = parse_reminder("remind me tomorrow to call jamil", now=NOON)
    assert r.ambiguous
    assert "tomorrow" in r.question.lower()


# ===================================== ambiguous results carry the known half

def test_no_time_result_still_carries_the_task_text():
    # The router parks this half so the next reply only has to give a time.
    r = parse_reminder("remind me to call mom", now=NOON)
    assert r.ambiguous
    assert r.text == "call mom"
    assert r.due_at is None


def test_no_task_result_still_carries_the_due_time():
    r = parse_reminder("remind me in 20 minutes", now=NOON)
    assert r.ambiguous
    assert r.text is None
    assert r.due_at == NOON + timedelta(minutes=20)


# ============================================================= text cleanup

def test_greeting_prefix_is_stripped_from_task_text():
    r = parse_reminder("hey jarvis remind me in 5 minutes i have a meeting", now=NOON)
    assert not r.ambiguous
    assert r.text == "i have a meeting"


def test_vocative_and_punctuation_prefix_is_stripped():
    r = parse_reminder("ok jarvis, remind me at 6pm to call mom", now=NOON)
    assert r.text == "call mom"


def test_trailing_please_is_stripped():
    r = parse_reminder("remind me at 6pm to call mom please", now=NOON)
    assert r.text == "call mom"


def test_greeting_words_inside_the_task_survive():
    r = parse_reminder("remind me at 6pm to say hello to daud", now=NOON)
    assert r.text == "say hello to daud"


# =============================================== plural trigger + spaced minutes

def test_looks_like_reminder_fires_on_plural_set_reminders():
    # "set reminders …" used to miss the trigger entirely and fall through
    # to the LLM, which fabricated "Reminder set" (live bug, 2026-07-09).
    assert looks_like_reminder("set reminders for calling ceo at 6pm and cto at 7pm")
    assert looks_like_reminder("set another reminder for the oven at 6pm")


def test_space_separated_minutes_with_meridiem():
    r = parse_reminder("set a reminder for calling ceo at 6 04 pm", now=NOON)
    assert not r.ambiguous
    assert r.due_at == NOON.replace(hour=18, minute=4)
    assert r.text == "calling ceo"


def test_space_separator_never_misreads_following_words():
    # "at 6 to call" must stay a bare-hour 6, not eat "to" as minutes.
    r = parse_reminder("remind me at 6 to call jamil", now=NOON)
    assert r.due_at == NOON.replace(hour=18, minute=0)
    assert r.text == "call jamil"


def test_leading_for_is_stripped_from_task_text():
    r = parse_reminder("set a reminder for calling driver at 6pm", now=NOON)
    assert r.text == "calling driver"


# =============================================== two-tier triggers (strong/weak)

def test_strong_trigger_verb_variants_fire_alone():
    assert looks_like_reminder("add a reminder to pay rent", now=NOON)
    assert looks_like_reminder("create a reminder to pay rent", now=NOON)
    assert looks_like_reminder("schedule a reminder for the meeting", now=NOON)
    assert looks_like_reminder("make a reminder to call mom", now=NOON)


def test_set_an_alarm_for_a_bare_time_parses():
    r = parse_reminder("set an alarm for 7am", now=NOON)
    assert not r.ambiguous
    assert r.due_at == (NOON + timedelta(days=1)).replace(hour=7, minute=0)  # 7am passed at noon
    assert r.text == "alarm"


def test_set_an_alarm_with_task_text():
    r = parse_reminder("set an alarm for 5pm to take pills", now=NOON)
    assert not r.ambiguous
    assert r.due_at == NOON.replace(hour=17, minute=0)
    assert r.text == "take pills"


def test_weak_trigger_with_a_time_is_a_reminder():
    r = parse_reminder("alert me at 6pm to take my medicine", now=NOON)
    assert r is not None and not r.ambiguous
    assert r.due_at == NOON.replace(hour=18, minute=0)
    assert r.text == "take my medicine"

    r2 = parse_reminder("notify me in 20 minutes to check the oven", now=NOON)
    assert r2 is not None and r2.text == "check the oven"

    r3 = parse_reminder("tell me to drink water at 3pm", now=NOON)
    assert r3 is not None and r3.text == "drink water"
    assert r3.due_at == NOON.replace(hour=15, minute=0)


def test_weak_trigger_without_a_time_stays_conversation():
    assert parse_reminder("alert me if anything happens", now=NOON) is None
    assert parse_reminder("let me know what you think", now=NOON) is None
    assert parse_reminder("notify me when the build finishes", now=NOON) is None


def test_dont_let_me_forget_with_a_time():
    r = parse_reminder("don't let me forget to call mom at 6pm", now=NOON)
    assert r is not None and not r.ambiguous
    assert r.text == "call mom"


def test_remember_phrasings_never_trigger_reminders():
    # "remember that X" belongs to the MEMORY engine, and "don't let me
    # forget that X" without a time falls through to it too.
    assert parse_reminder("remember that my mom's birthday is in july", now=NOON) is None
    assert parse_reminder("don't let me forget that jamil owes me money", now=NOON) is None


def test_background_task_phrases_never_trigger_reminders():
    # Part 5's background-intent phrases must keep going to the task router
    # even when the goal carries a time expression.
    assert parse_reminder("organize my downloads and tell me when you're done", now=NOON) is None
    assert (
        parse_reminder("delete files older than a week and let me know when it's finished", now=NOON)
        is None
    )


def test_remind_me_when_done_is_not_a_timed_reminder():
    # Live bug 2026-07-09: "…and remind me when you are done" got hijacked
    # by the reminder router, which asked "What time…?" for an event that
    # has no clock time. It's background-task intent — must fall through.
    assert (
        parse_reminder(
            "tell me how many files are in phase3test folder and remind me when you are done",
            now=NOON,
        )
        is None
    )
    assert parse_reminder("remind me whenever you're done", now=NOON) is None
    assert parse_reminder("remind me once it's finished", now=NOON) is None
    assert parse_reminder("remind me after you are done", now=NOON) is None


def test_remind_me_after_a_number_is_still_a_reminder():
    # The when/once/after guard must not eat "after 30 minutes" — and it
    # parses as relative time, same as "in 30 minutes".
    r = parse_reminder("remind me after 30 minutes to call mom", now=NOON)
    assert r is not None and not r.ambiguous
    assert r.due_at == NOON + timedelta(minutes=30)
    assert r.text == "call mom"


def test_completion_condition_before_remind_me_is_not_a_reminder():
    # Live bug 2026-07-10: the condition came BEFORE the trigger — "after
    # doing all this remind me" — so the forward-order lookahead never saw
    # it, the reminder router parked "What time…?" for background-task
    # intent, and the parked question then swallowed the user's retry too.
    live = (
        "hey list me all the files in the desktop and then delete all files "
        "in phase3test, and after deleting all files create a file "
        "'test.txt' in phase3test, after doing all this remind me"
    )
    assert not looks_like_reminder(live, now=NOON)
    assert parse_reminders(live, now=NOON) is None
    assert not looks_like_reminder("when you're done remind me", now=NOON)
    assert not looks_like_reminder("once everything is done, remind me", now=NOON)
    assert not looks_like_reminder("run the tests and after that's done remind me", now=NOON)


def test_forward_order_completion_phrases_are_not_reminders_either():
    # Same class, verb-first order — the extended lookahead covers
    # "after doing/finishing <the work in this message>".
    assert parse_reminder("remind me after doing all this", now=NOON) is None
    assert parse_reminder("remind me after finishing these tasks", now=NOON) is None
    assert parse_reminder("remind me after completing everything", now=NOON) is None


def test_completion_condition_with_an_explicit_time_stays_a_reminder():
    # A stated clock time wins: the user asked for a TIMED reminder, the
    # condition phrase is just sequencing. Never route this to background.
    assert looks_like_reminder("after doing all this remind me at 6pm to leave", now=NOON)


def test_user_activity_conditions_stay_reminders():
    # "after dinner remind me…" is a user activity, not completion of the
    # message's own work — the pre-condition guard must not eat it. (With
    # no parseable time it still ASKS, which is correct: ask, never guess.)
    r = parse_reminder("after dinner remind me to take my pills", now=NOON)
    assert r is not None and r.ambiguous
    r2 = parse_reminder("remind me at 6 after dinner", now=NOON)
    assert r2 is not None and not r2.ambiguous


def test_gimme_a_headup_is_a_reminder_with_default_text():
    # Live failure 2026-07-09: "hey gimme a headup at 11" missed the weak
    # trigger (spelling variants) and the LLM promised a heads-up it cannot
    # give. The heads-up IS the task — no "remind you to do what?" question.
    r = parse_reminder("hey gimme a headup at 11", now=NOON)
    assert r is not None and not r.ambiguous
    assert r.due_at == NOON.replace(hour=23, minute=0)  # bare-hour rule: 11am passed at noon
    assert r.text == "heads up"


def test_heads_up_spelling_variants_all_trigger():
    for phrasing in (
        "give me a heads up at 11pm",
        "give me a heads-up at 11pm",
        "gimme a headsup at 11pm",
        "gimme heads up at 11pm",
    ):
        assert looks_like_reminder(phrasing, now=NOON), phrasing


def test_heads_up_with_its_own_task_keeps_the_task():
    r = parse_reminder("give me a heads up at 11pm to submit the report", now=NOON)
    assert r.text == "submit the report"


def test_wake_me_up_defaults_its_own_text():
    r = parse_reminder("wake me up at 7am", now=NOON)
    assert r is not None and not r.ambiguous
    assert r.due_at == (NOON + timedelta(days=1)).replace(hour=7, minute=0)
    assert r.text == "wake up"


# ========================================================== multiple reminders

def test_two_reminders_with_explicit_times_split():
    rs = parse_reminders(
        "set reminders for calling ceo at 6 04 pm and a reminder for meeting with cto at 7",
        now=NOON,
    )
    assert len(rs) == 2
    assert rs[0].text == "calling ceo"
    assert rs[0].due_at == NOON.replace(hour=18, minute=4)
    assert rs[1].text == "meeting with cto"
    assert rs[1].due_at == NOON.replace(hour=19, minute=0)
    assert not rs[0].ambiguous and not rs[1].ambiguous


def test_second_reminder_anchored_to_the_first():
    rs = parse_reminders(
        "set reminder for calling friend at 6:01 pm and for calling dad 30 mins after it",
        now=NOON,
    )
    assert len(rs) == 2
    assert rs[0].text == "calling friend"
    assert rs[0].due_at == NOON.replace(hour=18, minute=1)
    assert rs[1].text == "calling dad"
    assert rs[1].due_at == NOON.replace(hour=18, minute=31)


def test_reversed_anchor_at_after_30_mins():
    # Live failure 2026-07-09: "at after 30 mins" (reversed word order)
    # wasn't an anchor, so everything collapsed into one reminder titled
    # "call dad and at after 30 mins to call mom".
    rs = parse_reminders(
        "set a reminder at 11 to call dad and at after 30 mins to call mom", now=NOON
    )
    assert len(rs) == 2
    assert rs[0].text == "call dad"
    assert rs[0].due_at == NOON.replace(hour=23, minute=0)
    assert rs[1].text == "call mom"
    assert rs[1].due_at == NOON.replace(hour=23, minute=30)


def test_later_anchor():
    rs = parse_reminders(
        "remind me at 6pm to call dad and 30 mins later to call mom", now=NOON
    )
    assert len(rs) == 2
    assert rs[1].text == "call mom"
    assert rs[1].due_at == NOON.replace(hour=18, minute=30)


def test_after_a_noun_never_anchors():
    # "30 mins after DINNER" is not "30 mins after the previous reminder" —
    # no pronoun, no anchor; the whole thing stays one reminder.
    rs = parse_reminders(
        "remind me at 6pm to eat dinner and take pills 30 mins after dinner", now=NOON
    )
    assert len(rs) == 1


def test_and_inside_a_single_task_never_splits():
    rs = parse_reminders("remind me to call mom and dad at 6pm", now=NOON)
    assert len(rs) == 1
    assert rs[0].text == "call mom and dad"
    assert rs[0].due_at == NOON.replace(hour=18, minute=0)


def test_merge_forward_keeps_the_and_task_and_still_splits_the_rest():
    rs = parse_reminders(
        "remind me to call mom and dad at 6pm and take pills at 7pm", now=NOON
    )
    assert len(rs) == 2
    assert rs[0].text == "call mom and dad"
    assert rs[0].due_at == NOON.replace(hour=18, minute=0)
    assert rs[1].text == "take pills"
    assert rs[1].due_at == NOON.replace(hour=19, minute=0)


def test_multi_with_an_unusable_time_falls_back_to_a_single_question():
    # Never half-schedule: one bad segment means the whole message goes
    # through the single parse, which asks instead of guessing.
    rs = parse_reminders(
        "remind me to call ali today at 5am and daud 10 minutes after it", now=NOON
    )
    assert len(rs) == 1
    assert rs[0].ambiguous
    assert "already passed" in rs[0].question.lower()


def test_parse_reminders_returns_none_without_a_trigger():
    assert parse_reminders("how are you today", now=NOON) is None


def test_anchor_alone_without_a_previous_time_falls_back():
    rs = parse_reminders("remind me to call dad 30 mins after it", now=NOON)
    assert len(rs) == 1
    assert rs[0].ambiguous  # no resolvable time — asks


# ========================================================== parse_time_reply

def test_time_reply_relative_with_preposition():
    r = parse_time_reply("in 5 mins", now=NOON)
    assert r is not None and not r.ambiguous
    assert r.due_at == NOON + timedelta(minutes=5)


def test_time_reply_bare_relative():
    r = parse_time_reply("5 minutes", now=NOON)
    assert r is not None and not r.ambiguous
    assert r.due_at == NOON + timedelta(minutes=5)


def test_time_reply_bare_clock_time():
    r = parse_time_reply("6pm", now=NOON)
    assert r is not None and not r.ambiguous
    assert r.due_at == NOON.replace(hour=18, minute=0)


def test_time_reply_with_at_and_day_qualifier():
    r = parse_time_reply("tomorrow at 9am", now=NOON)
    assert r is not None and not r.ambiguous
    assert r.due_at == (NOON + timedelta(days=1)).replace(hour=9, minute=0)


def test_time_reply_bare_hour_uses_the_documented_rule():
    r = parse_time_reply("6", now=NOON)  # 6am passed at noon → 6pm
    assert r is not None and not r.ambiguous
    assert r.due_at == NOON.replace(hour=18, minute=0)


def test_time_reply_past_stated_day_asks_never_guesses():
    r = parse_time_reply("today at 5am", now=NOON)
    assert r is not None and r.ambiguous
    assert "already passed" in r.question.lower()


def test_time_reply_without_any_time_returns_none():
    assert parse_time_reply("call mom", now=NOON) is None
    assert parse_time_reply("what do you mean?", now=NOON) is None
