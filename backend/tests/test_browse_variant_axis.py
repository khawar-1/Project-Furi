"""
Which SIZE? — the axis gate on the armed commit form (2026-08-08).

WHAT WAS MISSING. The 2026-08-02 round grounded a commit-mode `select_option`,
so a value the MODEL typed into a <select> faces the user's own words. It cannot
cover a storefront's size axis, which is a RADIO group: the model clicks a
swatch, or never touches it at all, and reaches `submit` with the axis unset.

⚠️ EVERY FIXTURE BELOW IS THE REAL DOM, captured 2026-08-08 by
scripts/_measure_variant_form.py against a live product page
(junaidjamshed.com/products/grey-formal-kurta-jjka50589). That matters more than
usual, because the measurement contradicted four things a hand-written fixture
would have encoded — and the previous round deferred this work precisely because
it had only a fragrance page, which has no multi-valued axis at all:

  1. NOTHING IS CHECKED and the hidden `id` is EMPTY. The contract Jarvis would
     have shown and submitted carried NO variant whatsoever.
  2. AVAILABILITY IS SIGNALLED TWO DIFFERENT WAYS ON ONE PAGE. The main product
     leaves the input enabled and marks only the LABEL (`is-disabled`); the
     related-product cards mark the INPUT (`disabled`). Reading either alone
     offers sizes that cannot be bought.
  3. SIX SIZES, ONE IN STOCK. A gate that offered "every value" would have
     offered five dead ends — and when exactly one is buyable there is nothing
     to ask at all.
  4. THE AXIS NAME IS SOMETIMES MACHINE NOISE. The main form names its axes
     `Size` / `Color` / `Style`; the cards name the identical axis
     `option-15623440335008-1`, which no question should read out.

The Shopify JSON for the same product agrees on the ground truth
(Size: XS,S,M,L,XL,XXL with only L available), which is what makes (3) a fact
about the world rather than about our parsing.
"""
import pytest

from app.agents import browser_loop
from app.browser import choice
from app.browser import session as browser_session
from tests.test_browser_loop import FakeProvider as LoopProvider, ScriptedPage, _el, _page

pytestmark = pytest.mark.asyncio


# ===================================================== the measured fixtures
# Verbatim from scripts/bench-results/variant-form.json. `available` is what
# _FORM_CONTRACT_JS_BODY's rule yields for the signals recorded there; the raw
# signal behind each row is named in the comment, and test_browser_axes_js.py
# runs the real JS against the same markup shape to close that loop.

# form 0 — THE MAIN PRODUCT. input.disabled is False on all six; only the LABEL
# class carries the stock signal, and only "L" lacks `is-disabled`.
KURTA_MAIN_AXES = [
    {
        "name": "Size",
        "options": [
            {"value": "XS", "label": "", "chosen": False, "available": False},
            {"value": "S", "label": "", "chosen": False, "available": False},
            {"value": "M", "label": "", "chosen": False, "available": False},
            {"value": "L", "label": "", "chosen": False, "available": True},
            {"value": "XL", "label": "", "chosen": False, "available": False},
            {"value": "XXL", "label": "", "chosen": False, "available": False},
        ],
    }
]

# form 3 — a RELATED-PRODUCT card. Same axis, opaque name, and here the INPUT
# carries `disabled` (XS / XL / XXL), which the main form never does.
KURTA_CARD_AXES = [
    {
        "name": "option-15623440335008-1",
        "options": [
            {"value": "XS", "label": "", "chosen": False, "available": False},
            {"value": "S", "label": "", "chosen": False, "available": True},
            {"value": "M", "label": "", "chosen": False, "available": True},
            {"value": "L", "label": "", "chosen": False, "available": True},
            {"value": "XL", "label": "", "chosen": False, "available": False},
            {"value": "XXL", "label": "", "chosen": False, "available": False},
        ],
    }
]

# The single-valued axes the same form carries. Measured: EVERY product in the
# sample has Color and Style with exactly one value, so an "ask about every
# multi-valued control" gate is quiet here only because of the >1 rule.
SINGLE_VALUED = [
    {"name": "Color", "options": [{"value": "Grey", "available": True}]},
    {"name": "Style", "options": [{"value": "JJK-A-50589/S26/JJ10489-FL", "available": True}]},
]

# A perfume, where the size axis is the thing the user names in words.
PERFUME_AXES = [
    {
        "name": "Size",
        "options": [
            {"value": "30ml", "label": "30ml", "chosen": False, "available": True},
            {"value": "100ml", "label": "100ml", "chosen": False, "available": True},
            {"value": "200ml", "label": "200ml", "chosen": False, "available": True},
        ],
    }
]

PRODUCT_URL = "https://www.junaidjamshed.com/products/grey-formal-kurta-jjka50589"


def _words(goal: str, url: str = PRODUCT_URL, extra=()):
    return choice.target_tokens(goal, url, extra=extra)


def _contract(axes, fields=None):
    return {
        "action": "https://www.junaidjamshed.com/cart/add",
        "method": "POST",
        "fields": list(fields or []),
        "has_password": False,
        "axes": axes,
    }


# ============================================ 1. the measured decisions
async def test_six_sizes_one_in_stock_is_not_a_question():
    """THE INCIDENT'S OWN NUMBERS. The grey kurta lists six sizes and sells one.
    A question here would offer five things that cannot be bought, so code takes
    the only one there is — and the user is never interrupted for a choice the
    site has already made for them."""
    got = choice.unresolved_axis(choice.axes_of(_contract(KURTA_MAIN_AXES)),
                                 _words("add the grey formal kurta to cart"))
    assert got is not None
    assert got.settled == "L"
    assert got.options == ()
    assert "only one in stock" in got.why


async def test_a_genuinely_open_axis_asks_with_only_what_can_be_bought():
    """Three sizes in stock and nothing in the user's words picks one: ask — and
    offer S/M/L, never the sold-out XS/XL/XXL sitting beside them."""
    got = choice.unresolved_axis(choice.axes_of(_contract(KURTA_CARD_AXES)),
                                 _words("add the grey formal kurta to cart"))
    assert got is not None
    assert got.settled == ""
    assert got.options == ("S", "M", "L")


async def test_the_users_own_words_settle_it_without_asking():
    """'add janan sports 100ml' — the size is IN the request, so this must not
    become a question. The user's requirement, in their words: "but i told size
    also so it should pick that option not ask me which size i meant"."""
    got = choice.unresolved_axis(
        choice.axes_of(_contract(PERFUME_AXES)),
        _words("add janan sports 100ml to cart",
               url="https://www.junaidjamshed.com/products/janan-sport"),
    )
    assert got is not None
    assert got.settled == "100ml"
    assert got.why == "your own words name it"


async def test_the_users_words_beat_a_value_the_page_preselected():
    """Enforce, never trust — the 2026-08-02 rule. The page has 30ml selected and
    the user said 100ml: code corrects it rather than submitting the default."""
    axes = [{"name": "Size", "options": [
        {"value": "30ml", "label": "30ml", "chosen": True, "available": True},
        {"value": "100ml", "label": "100ml", "chosen": False, "available": True},
    ]}]
    got = choice.unresolved_axis(choice.axes_of(_contract(axes)),
                                 _words("add janan sports 100ml to cart"))
    assert got is not None and got.settled == "100ml"


async def test_an_axis_the_page_already_settled_is_left_alone():
    """A site that pre-selects has made a visible choice, and the approval card
    names it in words (2026-08-02). Re-asking every such form would make the gate
    noise on every well-behaved storefront."""
    axes = [{"name": "Size", "options": [
        {"value": "30ml", "label": "30ml", "chosen": True, "available": True},
        {"value": "100ml", "label": "100ml", "chosen": False, "available": True},
    ]}]
    assert choice.unresolved_axis(choice.axes_of(_contract(axes)),
                                  _words("add janan sports to cart")) is None


async def test_a_sold_out_axis_is_not_our_question():
    """Every value gone is the SITE's answer, not the user's. Asking which
    unbuyable size they want is nonsense; the submit reports what the site says."""
    axes = [{"name": "Size", "options": [
        {"value": "S", "available": False}, {"value": "M", "available": False},
    ]}]
    assert choice.unresolved_axis(choice.axes_of(_contract(axes)),
                                  _words("add the kurta to cart")) is None


async def test_single_valued_axes_are_never_questions():
    """Color and Style carry exactly one value on every product measured. They
    are not choices, and a gate that asked about them would fire on every add."""
    assert choice.axes_of(_contract(SINGLE_VALUED)) == []
    assert choice.unresolved_axis(choice.axes_of(_contract(SINGLE_VALUED)),
                                  _words("add the kurta")) is None


async def test_a_form_with_no_axes_at_all_is_untouched():
    """The 2026-08-02 fragrance page, and every plain form in the product: no
    axes key, nothing to settle, nothing to ask."""
    assert choice.unresolved_axis(choice.axes_of({"fields": []}), _words("x")) is None
    assert choice.unresolved_axis(choice.axes_of(None), _words("x")) is None


# ============================================ 2. reading the contract
async def test_axes_of_is_tolerant_of_anything_malformed():
    """A contract that cannot be parsed must degrade to "this form offers no
    choices" — the behaviour that existed before axes were read at all — never
    raise inside a commit discovery."""
    assert choice.axes_of({"axes": "not a list"}) == []
    assert choice.axes_of({"axes": [None, 3, {"name": "", "options": []}]}) == []
    assert choice.axes_of({"axes": [{"name": "S", "options": [{"value": "a"}]}]}) == []
    assert choice.axes_of({"axes": [{"name": "S", "options": ["a", "b"]}]}) == []


async def test_a_missing_available_flag_reads_as_available():
    """ABSENT MEANS AVAILABLE. A contract from an older build, or a control whose
    markup carries no stock signal at all, must not read as "everything is sold
    out" — that would silently switch the gate off."""
    axes = choice.axes_of({"axes": [{"name": "Size", "options": [
        {"value": "S"}, {"value": "M"}]}]})
    assert [o.available for o in axes[0].options] == [True, True]
    got = choice.unresolved_axis(axes, _words("add the kurta"))
    assert got is not None and got.options == ("S", "M")


@pytest.mark.parametrize("raw,shown", [
    ("Size", "Size"),
    ("Color", "Color"),
    ("option-15623440335008-1", ""),        # the real card-form name
    ("id", ""),
    ("variant_id", ""),
    ("a3f5c9e17b42d901", ""),
    ("properties[_Charge Code]", "Charge Code"),
    ("waist_size", "waist size"),
])
async def test_a_name_a_person_cannot_read_is_not_shown(raw, shown):
    """The question says "this option" rather than reading a variant id out loud.
    Measured: the SAME axis is `Size` on the main form and
    `option-15623440335008-1` on the cards."""
    assert choice.readable_axis_name(raw) == shown


async def test_the_token_scorer_cannot_see_a_clothing_size():
    """⚠️ THE MEASUREMENT BEHIND THE EXACT-ANSWER RULE. `_tokens("M")` is EMPTY:
    the module's token rule drops anything under _MIN_STEM_CHARS because it was
    built to match product NAMES. Sizes are single letters, so a reply of "M"
    scores 0 against every option and could never settle the question it was
    answering. Pinned so the rule is not "simplified" away later."""
    assert choice._tokens("M") == []
    assert choice._score(choice.target_tokens("add the kurta", "", extra=["M"]), "M") == 0


@pytest.mark.parametrize("reply,expected", [
    ("M", "M"),
    ("m", "M"),
    ("100 ml", "100ml"),
    ("100ML", "100ml"),
    ("XL", "XL"),
    ("", ""),            # no reply settles nothing
    ("whatever", ""),    # a reply naming no option is fail-closed
    # THE SAME SIZE SAID ANOTHER WAY (2026-08-10). MEASURED on the real product
    # page: the axis offers XS/S/M/L/XL and the user said "large", which matched
    # nothing — `_compact` needs an exact string and the token scorer drops
    # one-letter options — so their own answer was ignored and they were asked a
    # question they had just answered.
    ("large", "L"),
    ("Large", "L"),
    ("extra large", "XL"),
])
async def test_a_direct_reply_is_matched_exactly(reply, expected):
    """An ANSWER is a direct reply to "which one?", so an exact hit on the page's
    own label or value settles it — including the sizes and volumes the token
    scorer is blind to. Anything else returns "" and the caller asks again."""
    axes = choice.axes_of(_contract([{"name": "Size", "options": [
        {"value": "M", "available": True}, {"value": "L", "available": True},
        {"value": "XL", "available": True}, {"value": "100ml", "available": True},
    ]}]))
    got = choice.unresolved_axis(axes, _words("add the thing"), answer=reply)
    assert (got.settled if got else "") == expected


async def test_prose_is_never_matched_by_the_exact_rule():
    """The exact rule applies to a REPLY, not to a goal: scanning a sentence for
    the bare letter "s" would match almost anything, so the goal keeps the token
    scorer and an under-specified request still asks."""
    axes = choice.axes_of(_contract([{"name": "Size", "options": [
        {"value": "S", "available": True}, {"value": "M", "available": True},
    ]}]))
    got = choice.unresolved_axis(axes, _words("add the small grey shirt to my cart"))
    assert got is not None and got.settled == "" and got.options == ("S", "M")


# ================================== 3. THE SAFETY CLAIM: fingerprint invariance
async def test_axes_cannot_move_the_approval_fingerprint():
    """⚠️ THE LOAD-BEARING PROPERTY. `axes` is a SEPARATE key from `fields`
    precisely so it can never change what an approval binds to: _commit_
    fingerprint reads (name, value) over fields plus (name, path) over uploads,
    and nothing else. If this ever fails, an approved submit could be refused —
    or worse, accepted — because of a cosmetic read."""
    fields = [{"name": "id", "value": "123"}, {"name": "quantity", "value": "1"}]
    without = {"action": "https://x.test/cart/add", "method": "POST", "fields": fields}
    with_axes = {**without, "axes": KURTA_MAIN_AXES}
    assert (browser_session._commit_fingerprint(without)
            == browser_session._commit_fingerprint(with_axes))

    # And a DIFFERENT set of axes still fingerprints identically.
    other = {**without, "axes": PERFUME_AXES}
    assert (browser_session._commit_fingerprint(other)
            == browser_session._commit_fingerprint(with_axes))


async def test_the_chosen_value_does_move_the_fingerprint():
    """The mirror of the above, so the first test cannot pass vacuously: what a
    submit SENDS is still bound. Setting the variant changes `fields`, so the
    approval is re-signed on the real contract."""
    a = {"action": "https://x.test/cart/add", "method": "POST",
         "fields": [{"name": "id", "value": ""}]}
    b = {"action": "https://x.test/cart/add", "method": "POST",
         "fields": [{"name": "id", "value": "56798395400352"}]}
    assert (browser_session._commit_fingerprint(a)
            != browser_session._commit_fingerprint(b))


# ============================================ 4. end to end through the loop
class _AxisSession:
    """A commit session that models THE CONTRACT, not a convenient shape: the
    form starts with an unset axis, choose_form_option really sets it, and the
    re-read reflects that. A fake that could not express "the axis changed" would
    pass whichever way the gate went."""

    def __init__(self, page, axes, *, accept=True):
        self.page = page
        self.allowlist = {"junaidjamshed.com"}
        self.stats = _Stats()
        self.browse_history = []
        self.last_redirect_offsite = None
        self.uploads: list = []
        self._axes = [dict(a, options=[dict(o) for o in a["options"]]) for a in axes]
        self._accept = accept
        self.chosen: list = []
        self.rereads = 0
        self._resolved_id = ""

    def _contract(self):
        return {
            "action": "https://www.junaidjamshed.com/cart/add",
            "method": "POST",
            # ⚠️ MODELS THE MEASURED CONTRACT: the hidden variant id is resolved
            # by the THEME'S OWN change handler, not by the click. So it is empty
            # until a settle, exactly as the live site behaves — a fake that
            # filled it in immediately could not see a missing settle.
            "fields": [{"name": "id", "value": self._resolved_id}],
            "has_password": False,
            "axes": self._axes,
        }

    async def settle(self):
        chosen = next(
            (o for a in self._axes for o in a["options"] if o.get("chosen")), None
        )
        if chosen:
            self._resolved_id = chosen["value"]

    async def goto(self, url):
        self.page.navigate(url)

    async def read_commit_target(self, obs, index):
        return self._contract()

    async def reread_commit_form(self):
        self.rereads += 1
        return self._contract()

    async def choose_form_option(self, name, value):
        self.chosen.append((name, value))
        if not self._accept:
            return "refused"
        for axis in self._axes:
            if axis["name"] != name:
                continue
            for opt in axis["options"]:
                opt["chosen"] = opt["value"] == value
            return "ok"
        return "no-option"


class _Stats:
    def as_dict(self):
        return {"blocked_mutations": 0, "blocked_navigations": 0,
                "blocked_hosts": 0, "mutation_urls": []}


def _submit_page():
    return ScriptedPage([
        _page([_el(1, role="button", name="Add to Cart")], url=PRODUCT_URL,
              title="GREY FORMAL KURTA"),
    ])


async def test_the_loop_asks_rather_than_submitting_a_form_with_no_size():
    """END TO END. The model says submit; three sizes are buyable and the user
    named none. The run STOPS with the page's own labels as options, and
    commit_state is never built — nothing is queued for approval."""
    session = _AxisSession(_submit_page(), KURTA_CARD_AXES)
    provider = LoopProvider(['{"action":"submit","index":1}'])

    out = await browser_loop.run_browse(
        session, "Add the grey formal kurta to the cart", provider, commit=True,
    )

    assert out.target_choice_required is True
    assert out.choice_kind == "option"
    assert out.choice_options == ["S", "M", "L"]
    assert out.commit_required is False
    assert out.commit_state == {}
    assert session.chosen == []          # nothing was set on the page


async def test_an_opaque_axis_name_is_not_read_out_in_the_question():
    """The card form's axis is `option-15623440335008-1`. The pause must not
    quote it; the planner then says "this option" instead."""
    session = _AxisSession(_submit_page(), KURTA_CARD_AXES)
    provider = LoopProvider(['{"action":"submit","index":1}'])
    out = await browser_loop.run_browse(
        session, "Add the grey formal kurta to the cart", provider, commit=True)
    assert out.choice_field == ""
    assert "option-1562" not in (out.error or "")


async def test_the_only_size_in_stock_is_taken_and_the_submit_proceeds():
    """THE INCIDENT'S OWN PAGE, end to end. Six sizes, one buyable: code sets it,
    re-reads the form, and the contract that reaches the approval card carries a
    real variant id instead of the measured EMPTY one."""
    session = _AxisSession(_submit_page(), KURTA_MAIN_AXES)
    provider = LoopProvider(['{"action":"submit","index":1}'])

    out = await browser_loop.run_browse(
        session, "Add the grey formal kurta to the cart", provider, commit=True,
    )

    assert session.chosen == [("Size", "L")]
    assert session.rereads == 1
    assert out.commit_required is True
    assert out.commit_state["fields"] == [{"name": "id", "value": "L"}]
    assert out.target_choice_required is False


async def test_the_users_size_is_applied_end_to_end_without_a_question():
    """"add janan sports 100ml" — the loop sets 100ml itself and goes straight to
    the approval card. This is the half of the user's request that must NOT
    interrupt them."""
    session = _AxisSession(_submit_page(), PERFUME_AXES)
    provider = LoopProvider(['{"action":"submit","index":1}'])

    out = await browser_loop.run_browse(
        session, "Add janan sports 100ml to the cart", provider, commit=True,
    )

    assert session.chosen == [("Size", "100ml")]
    assert out.commit_required is True
    assert out.target_choice_required is False


async def test_the_answer_settles_the_axis_on_the_resumed_run():
    """THE PAUSE IS TERMINAL. The user's reply rides back as chosen_option, joins
    the scoring corpus, and code applies it — so the resumed run reaches the
    approval card instead of asking the same question again."""
    session = _AxisSession(_submit_page(), KURTA_CARD_AXES)
    provider = LoopProvider(['{"action":"submit","index":1}'])

    out = await browser_loop.run_browse(
        session, "Add the grey formal kurta to the cart", provider, commit=True,
        chosen_option="M",
    )

    assert session.chosen == [("option-15623440335008-1", "M")]
    assert out.commit_required is True
    assert out.target_choice_required is False


async def test_a_control_that_refuses_the_value_asks_instead_of_submitting():
    """Fail toward asking. If the page will not take the value code decided, the
    form still has no variant — submitting it anyway is the defect this gate
    exists to stop, so the user picks in the window instead."""
    session = _AxisSession(_submit_page(), KURTA_CARD_AXES, accept=False)
    provider = LoopProvider(['{"action":"submit","index":1}'])

    out = await browser_loop.run_browse(
        session, "Add the grey formal kurta in M to the cart", provider,
        commit=True, chosen_option="M",
    )

    assert session.chosen == [("option-15623440335008-1", "M")]
    assert out.target_choice_required is True
    assert out.choice_options == ["S", "M", "L"]
    assert out.commit_required is False


async def test_a_second_axis_the_same_reply_named_is_not_asked_about_again():
    """⚠️ FOUND BY FALSIFICATION. The re-check after code applies a value ran
    WITHOUT the user's answer. On a one-axis form that masks itself — the axis is
    `chosen` by then, so it is skipped anyway — but on a two-axis form the reply
    "100ml" would settle Size and then Volume would be asked about with the
    answer thrown away."""
    two = [
        {"name": "Size", "options": [
            {"value": "S", "available": True}, {"value": "M", "available": True}]},
        {"name": "Volume", "options": [
            {"value": "100ml", "available": True}, {"value": "200ml", "available": True}]},
    ]
    session = _AxisSession(_submit_page(), two)
    provider = LoopProvider(['{"action":"submit","index":1}'])

    out = await browser_loop.run_browse(
        session, "add the thing to the cart", provider, commit=True,
        chosen_option="M",
    )

    # Size is settled from the reply; Volume genuinely is not named, so THAT is
    # what gets asked — never Size again, and never with the answer discarded.
    assert ("Size", "M") in session.chosen
    assert out.target_choice_required is True
    assert out.choice_field == "Volume"
    assert out.choice_options == ["100ml", "200ml"]


async def test_a_read_only_browse_never_reaches_the_gate():
    """A read-only browse cannot submit at all, so the axis gate is unreachable
    there — and adding it must not change what a read-only run does."""
    session = _AxisSession(_submit_page(), KURTA_CARD_AXES)
    provider = LoopProvider(['{"action":"submit","index":1}', '{"action":"done"}'])

    out = await browser_loop.run_browse(
        session, "look at the grey formal kurta", provider, commit=False)

    assert out.target_choice_required is False
    assert session.chosen == []


# ============================ 5. the corpus is the USER's words, not the goal
async def test_the_size_is_read_from_the_users_words_not_the_planners_goal():
    """⚠️ The planner AUTHORS the goal, and its paraphrase drops the size: "add
    janan sports 100ml to cart" becomes "Find the Janan Sports perfume and add it
    to the cart". Scoring that would ask the user a question they had already
    answered — the 2026-08-07 lesson ("it asked the right question of the wrong
    string"), in a function written five days earlier."""
    session = _AxisSession(_submit_page(), PERFUME_AXES)
    provider = LoopProvider(['{"action":"submit","index":1}'])

    out = await browser_loop.run_browse(
        session,
        "Find the Janan Sports perfume on junaidjamshed.com and add it to the cart",
        provider, commit=True,
        intent_text="add janan sports 100ml to cart",
    )

    assert session.chosen == [("Size", "100ml")]
    assert out.target_choice_required is False


async def test_without_user_words_the_goal_is_still_used():
    """The regression half: a caller that stamps no user_words behaves exactly as
    it did before, because intent falls back to the goal."""
    session = _AxisSession(_submit_page(), PERFUME_AXES)
    provider = LoopProvider(['{"action":"submit","index":1}'])

    out = await browser_loop.run_browse(
        session, "add janan sports 100ml to cart", provider, commit=True)

    assert session.chosen == [("Size", "100ml")]
    assert out.target_choice_required is False
