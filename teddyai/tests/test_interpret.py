"""
Tests for interpret.py.

Only `verify()` is exercised, deliberately: it is the half that enforces the
rules, it needs no API key, and it is the half that would fail silently in
production if it were wrong. `interpret()` is a thin HTTP call around it.

Each test feeds a narrative that breaks exactly one rule and checks the checker
names it.
"""
import json

import pytest

import interpret
from interpret import verify, permitted_numbers, permitted_prices, permitted_pcts


DOC = {
    "reference": {"spot": 29538.75, "ref_settle": 29538.75, "last_print": None,
                  "em": 243.0, "rail_lo": 29295.0, "rail_hi": 29781.0, "iv_atm": 0.1306},
    "flips": {"0DTE": 29504.6, "NEAR": 29378.9, "FULL": 29342.1},
    "levels": [
        {"price": 29600.0, "quality": 0.88, "sign": 1, "gamma": 254.5, "oi": 1809,
         "distance": 61.0, "distance_ems": 0.251, "topology": "wall", "labels": ["cluster"]},
        {"price": 29700.0, "quality": 0.79, "sign": 1, "gamma": 217.6, "oi": 1682,
         "distance": 161.0, "distance_ems": 0.663, "topology": "wall", "labels": []},
    ],
    "topology": [{"kind": "wall", "lower": 29578.0, "upper": 29703.0, "center": 29640.5,
                  "sign": 1, "width": 125.0, "strength": 0.41, "labels": ["cluster"]}],
    "scenarios": [
        {"rank": 1, "label": "base case", "side": "up",
         "trigger": "a 30-minute close above 29,515",
         "targets": [29600.0, 29650.0, 29700.0],
         "invalidation": "a 30-minute close below 29,450",
         "risks": [], "opposing": []},
        {"rank": 2, "label": "alternative", "side": "down",
         "trigger": "a 30-minute close below 29,515",
         "targets": [29450.0, 29400.0],
         "invalidation": "a 30-minute close above 29,600",
         "risks": [], "opposing": []},
    ],
    "confidence": {"directional": {"value": None, "limited_by": ["no directional input"]},
                   "structural": {"value": 0.85, "limited_by": []}},
    "data_quality": {"score": 1.0, "issues": []},
}

CLEAN = {
    "thesis": ("The reference sits at 29,538.75, above every flip, with the nearest heavy "
               "structure the 29,600 wall 61 points overhead. Structure carries no direction "
               "here; the tape decides."),
    "base_case": ("On a 30-minute close above 29,515 the path runs 29,600, then 29,650 and "
                  "29,700. It is wrong on a 30-minute close below 29,450."),
    "alternatives": ["On a 30-minute close below 29,515 the downside runs 29,450 then 29,400."],
    "risks": ["The book is unchanged from the settle."],
    "no_trade": "Between 29,450 and 29,600 there is nothing worth acting on.",
}


# ------------------------------------------------------------- permitted ---

def test_permitted_numbers_include_document_values_and_their_roundings():
    p = permitted_numbers(DOC)
    assert 29538.75 in p
    assert 29539 in p


def test_permitted_numbers_include_differences_between_prices():
    """'61 points above 29,600' is arithmetic on published values, not invention."""
    p = permitted_numbers(DOC)
    assert 61 in p                      # 29600 - 29539
    assert 100 in p                     # 29700 - 29600


def test_permitted_prices_gather_every_kind_of_level():
    p = permitted_prices(DOC)
    for v in (29600, 29700, 29505, 29379, 29342, 29295, 29781, 29515, 29450, 29650):
        assert v in p, f"{v} should be a permitted price"


def test_permitted_percentages_cover_decimals_written_as_percents():
    p = permitted_pcts(DOC)
    assert 88 in p                      # quality 0.88
    assert 13.1 in p or 13 in p         # iv 0.1306


def test_percentages_are_not_harvested_out_of_strings():
    """The 30 in "a 30-minute close" must not become a permitted percentage, or
    the strictest check in the file stops catching fabricated rates."""
    doc = {"scenarios": [{"trigger": "a 37-minute close above 29,515", "label": "x"}],
           "confidence": {"directional": {"value": 0.5}}}
    assert 37 not in permitted_pcts(doc)
    ok, problems = verify({"thesis": "It holds 37% of the time."}, doc)
    assert not ok
    assert any("invented percentage" in p for p in problems)


# ------------------------------------------------------------ clean pass ---

def test_a_faithful_narrative_verifies():
    ok, problems = verify(CLEAN, DOC)
    assert ok, problems


def test_a_plain_string_narrative_is_accepted():
    ok, _ = verify("The reference is 29,538.75 and structure carries no direction.", DOC)
    assert ok


# --------------------------------------------------------- invented level ---

def test_an_invented_level_is_caught():
    bad = dict(CLEAN, thesis="The key level today is 29,555, just above the reference.")
    ok, problems = verify(bad, DOC)
    assert not ok
    assert any("invented level" in p and "29,555" in p for p in problems)


def test_a_real_level_written_without_a_comma_passes():
    ok, _ = verify(dict(CLEAN, thesis="The 29600 wall is the structure. "
                                      "Structure carries no direction."), DOC)
    assert ok


def test_a_target_from_a_scenario_is_a_permitted_level():
    ok, _ = verify(dict(CLEAN, thesis="29,650 is the second target. "
                                      "Structure carries no direction."), DOC)
    assert ok


# ----------------------------------------------------- invented percentage ---

def test_an_invented_percentage_is_caught():
    """The failure mode with the worst consequences: a fabricated rate reads as
    evidence and is not."""
    bad = dict(CLEAN, base_case="The 29,600 wall holds 72% of the time.")
    ok, problems = verify(bad, DOC)
    assert not ok
    assert any("invented percentage" in p for p in problems)


def test_a_percentage_that_is_in_the_document_passes():
    ok, problems = verify(dict(CLEAN, thesis="Node quality reaches 88% at 29,600. "
                                             "Structure carries no direction."), DOC)
    assert ok, problems


def test_the_percentage_check_explains_why_it_is_strict():
    bad = dict(CLEAN, base_case="It works 63% of the time.")
    _, problems = verify(bad, DOC)
    assert any("calibrates nothing" in p for p in problems)


# ------------------------------------------------------------- triggers ---

def test_a_reworded_trigger_is_caught():
    bad = dict(CLEAN, base_case="The base case triggers on a break above 29,515.")
    ok, problems = verify(bad, DOC)
    assert not ok
    assert any("verbatim" in p for p in problems)


def test_quoting_the_trigger_exactly_passes():
    ok, problems = verify(dict(CLEAN,
                               base_case="The base case needs a 30-minute close above 29,515."), DOC)
    assert ok, problems


def test_an_unnamed_scenario_does_not_require_its_trigger():
    """Only a scenario the narrative NAMES must carry its trigger verbatim."""
    ok, _ = verify({"thesis": "The reference is 29,538.75. Structure carries no direction."}, DOC)
    assert ok


# --------------------------------------------------------- missing data ---

def test_an_unacknowledged_missing_direction_is_caught():
    bad = {"thesis": "The reference is 29,538.75 and the 29,600 wall is overhead."}
    ok, problems = verify(bad, DOC)
    assert not ok
    assert any("NO directional confidence" in p for p in problems)


def test_acknowledging_the_missing_direction_passes():
    ok, _ = verify({"thesis": "The reference is 29,538.75. The map carries no direction."}, DOC)
    assert ok


# ------------------------------------------------- forbidden conversion ---

def test_positive_gamma_called_support_is_caught():
    bad = dict(CLEAN, thesis="Positive gamma at 29,600 makes it support. "
                             "Structure carries no direction.")
    ok, problems = verify(bad, DOC)
    assert not ok
    assert any("forbidden conversion" in p for p in problems)


def test_a_gamma_reading_called_bullish_is_caught():
    bad = dict(CLEAN, thesis="The gamma profile is bullish today. "
                             "Structure carries no direction.")
    ok, problems = verify(bad, DOC)
    assert not ok
    assert any("forbidden conversion" in p for p in problems)


def test_the_conversion_check_states_the_principle():
    bad = dict(CLEAN, thesis="Negative gamma below makes 29,450 resistance. "
                             "Structure carries no direction.")
    _, problems = verify(bad, DOC)
    assert any("not which way price goes" in p for p in problems)


# ------------------------------------------------------------ behaviour ---

def test_the_checker_names_every_problem_not_just_the_first():
    bad = {"thesis": "29,555 holds 72% of the time."}
    ok, problems = verify(bad, DOC)
    assert not ok
    assert len(problems) >= 2


def test_an_empty_narrative_still_fails_the_missing_direction_check():
    ok, problems = verify({}, DOC)
    assert not ok


def test_interpret_refuses_without_an_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as e:
        interpret.interpret(DOC)
    assert "ANTHROPIC_API_KEY" in str(e.value)


def test_the_brief_states_the_hard_rules():
    # the brief is wrapped for readability, so compare on collapsed whitespace
    b = " ".join(interpret.BRIEF.split()).lower()
    assert "must appear in the json" in b
    assert "calibrates nothing" in b
    assert "reworded trigger is a different condition" in b
    assert "is not \"support\"" in b


def test_the_brief_and_the_checker_agree_on_what_is_forbidden():
    """A rule stated in the prompt but not enforced is decoration."""
    b = interpret.BRIEF.lower()
    for word in ("percentage", "verbatim", "difference", "null"):
        assert word in b


# --------------------------------------------- false positives, found live ---

def test_a_large_non_price_number_from_the_document_is_not_an_invented_level():
    """Found running the checker against a real state.json. Open interest, gamma
    and contract counts all run into the thousands, and testing "is it
    price-shaped" BEFORE "is it in the document" flagged an open-interest figure
    of 1,809 as an invented level. A number that appears anywhere in the document
    is legitimate whatever its size."""
    ok, problems = verify(
        {"thesis": "The 29,600 wall carries 1,809 contracts. Structure carries no direction."},
        DOC)
    assert ok, problems


def test_price_shape_is_judged_against_the_documents_own_reference():
    """A fixed floor of 1,000 would never fire on QQQ near 718, so an invented
    ETF level would slip through as merely an 'unsupported number'."""
    qqq = {
        "reference": {"spot": 718.40, "em": 5.36},
        "flips": {"FULL": 716.0},
        "levels": [{"price": 720.0, "quality": 0.8}],
        "scenarios": [],
        "confidence": {"directional": {"value": None}},
    }
    ok, problems = verify({"thesis": "The level is 733.0. It carries no direction."}, qqq)
    assert not ok
    assert any("invented level" in p for p in problems)


def test_a_real_low_priced_level_still_passes():
    qqq = {
        "reference": {"spot": 718.40, "em": 5.36},
        "flips": {"FULL": 716.0},
        "levels": [{"price": 720.0, "quality": 0.8}],
        "scenarios": [],
        "confidence": {"directional": {"value": None}},
    }
    ok, problems = verify({"thesis": "The 720.0 wall is overhead. It carries no direction."}, qqq)
    assert ok, problems


# ------------------------------------------------- the commentary integration ---

TRINITY = {
    "spot": 29448.75, "last_print": 29375.75, "rail_lo": 29159.0, "rail_hi": 29739.0,
    "panels": {
        "0DTE": {"hvl": [29430.6], "gamma": {"29500.0": 40, "29600.0": 90}},
        "NEAR": {"hvl": [29302.4], "gamma": {"29380.0": 108, "29450.0": -30, "29600.0": 255}},
        "FULL": {"hvl": [29265.8], "gamma": {"29200.0": 20, "29700.0": 60}},
    },
}


def _card(**kw):
    d = {"name": "A", "side": "long", "entry": 29385.0, "sl": 29318.0,
         "tp": [29500.0, 29600.0, 29700.0], "why": "the 29,380 rung"}
    d.update(kw)
    return d


# -- check_card_levels -------------------------------------------------------

def test_real_card_levels_pass():
    """Measured on the 2026-09-10 sheet: every target landed exactly on a strike,
    entries and stops sat 2 to 5 points off."""
    ok, probs = interpret.check_card_levels([_card()], TRINITY)
    assert ok, probs


def test_an_invented_target_is_caught():
    """The hole the existing checks leave open. _check_cards tests the card's
    shape and _structure_check recomputes its stop; neither asks whether a TARGET
    is a price that exists."""
    ok, probs = interpret.check_card_levels([_card(tp=[29555.0, 29600.0, 29700.0])], TRINITY)
    assert not ok
    assert any("target 1 at 29,555 is not a level" in p for p in probs)


def test_an_entry_offset_a_few_points_from_its_level_passes():
    """Entries sit a few points past the level by design, so they get a wider
    tolerance than targets."""
    ok, _ = interpret.check_card_levels([_card(entry=29385.0)], TRINITY)
    assert ok


def test_an_entry_in_open_space_is_caught():
    ok, probs = interpret.check_card_levels([_card(entry=29000.0)], TRINITY)
    assert not ok
    assert any("open space" in p for p in probs)


def test_a_flip_is_a_valid_target():
    ok, _ = interpret.check_card_levels([_card(tp=[29430.6, 29500.0, 29600.0])], TRINITY)
    assert ok


def test_a_rail_is_a_valid_target():
    ok, _ = interpret.check_card_levels([_card(tp=[29500.0, 29600.0, 29739.0])], TRINITY)
    assert ok


def test_no_panels_means_no_check_rather_than_a_fabricated_failure():
    ok, probs = interpret.check_card_levels([_card()], {})
    assert ok and probs == []


def test_no_cards_is_handled():
    assert interpret.check_card_levels(None, TRINITY) == (True, [])


# -- prose_of ----------------------------------------------------------------

def test_prose_of_excludes_the_card_numbers():
    """A card's entry and stop are offset from their levels by design, so putting
    them through the prose level check would reject a correct card."""
    p = interpret.prose_of({"lead": "x", "reading": ["y"], "vex_note": "z",
                            "setups": [_card()]})
    blob = json.dumps(p)
    assert "29385" not in blob and "29318" not in blob
    assert "the 29,380 rung" in blob          # the reasoning IS kept


def test_prose_of_keeps_every_block_a_trader_reads():
    p = interpret.prose_of({"lead": "L", "reading": ["R"], "vex_note": "V", "setups": []})
    assert p["lead"] == "L" and p["reading"] == ["R"] and p["vex_note"] == "V"


# -- pools_from --------------------------------------------------------------

def test_pools_from_harvests_dict_keys():
    """In trinity.json the STRIKE IS THE KEY. Walking values alone would harvest
    the gamma and miss the level."""
    nums, prices = interpret.pools_from(TRINITY)
    assert 29600 in prices
    assert 29380 in prices


def test_pools_from_lets_the_narrative_quote_trinity_figures():
    """Checking prose against state.json alone produced 63 false positives on a
    real sheet, flagging figures the model was handed in trinity.json."""
    ok, probs = interpret.verify(
        {"thesis": "The last print is 29,375.75. The map carries no direction."},
        DOC, also=[TRINITY])
    assert ok, probs


# -- verify_commentary -------------------------------------------------------

def _full(**kw):
    d = {"lead": "The 29,600 wall is overhead.", "reading": ["Yesterday A filled."],
         "vex_note": "", "setups": [_card()]}
    d.update(kw)
    return d


def test_a_fabricated_rate_blocks():
    b, a = interpret.verify_commentary(
        _full(lead="The 29,600 wall holds 72% of the time."), DOC, TRINITY)
    assert any("invented percentage" in p for p in b)


def test_gamma_written_as_a_direction_blocks():
    b, a = interpret.verify_commentary(
        _full(lead="Positive gamma at 29,600 makes it support."), DOC, TRINITY)
    assert any("forbidden conversion" in p for p in b)


def test_a_retrospective_level_is_advisory_not_blocking():
    """The READING block reviews yesterday's cards and quotes yesterday's levels,
    which are in none of today's documents. On the 2026-09-10 sheet that alone
    accounts for nine findings, and blocking on them would reject a correct plan."""
    b, a = interpret.verify_commentary(
        _full(reading=["Yesterday A filled at 29,412 and stopped at 29,482."]), DOC, TRINITY)
    assert not any("invented level" in p for p in b)
    assert any("invented level" in p for p in a)


def test_a_missing_directional_note_is_advisory_in_the_commentary():
    """It blocks on the standalone path, where the narrative is the whole output.
    On the sheet, build.py's _caveat() already carries the disclaimer, so this is
    an incompleteness rather than a falsehood."""
    b, a = interpret.verify_commentary(_full(), DOC, TRINITY)
    assert not any("NO directional confidence" in p for p in b)


def test_the_missing_directional_note_still_blocks_on_the_standalone_path():
    ok, probs = interpret.verify({"thesis": "The 29,600 wall is overhead."}, DOC)
    assert not ok
    assert any("NO directional confidence" in p for p in probs)


# --------------------------------------------------------------------------
# The engine's own state.json is a DIFFERENT SHAPE from the report view, and
# the gate crashed on it. Both are legitimate inputs; both must be accepted.
# --------------------------------------------------------------------------

ENGINE_STATE = {
    "reference": {"spot": 29151.0, "ref_settle": 29448.75, "last_print": None,
                  "rail_lo": 29019.8, "rail_hi": 29282.2},
    "flips": {"0DTE": 29204.6, "NEAR": 29255.2, "FULL": 29218.2},
    # engine.run() nests the list under its own metadata; the report view is flat.
    "scenarios": {"scenarios": [{"label": "base case",
                                 "trigger": {"level": 29126.83,
                                             "rule": "a 30-minute close above 29,127"},
                                 "targets": [29150.0, 29200.0, 29330.0]}],
                  "generated": 6, "dedupe_basis": "side + target sequence"},
    "topology": {"features": [{"kind": "wall", "lower": 29126.83, "upper": 29226.83,
                               "center": 29176.83, "peak_price": 29189.0}],
                 "scale": 102.435, "unit": "contracts of delta per 1% move"},
}


def test_permitted_prices_accepts_the_engines_nested_state():
    """Regression, 2026-09-10.

    `permitted_prices` did `doc.get("scenarios") or doc["report"]["scenarios"]`.
    On a real state.json the first operand is a truthy DICT, so the `or` never
    reached the flat list and the loop iterated the dict's KEYS -- dying on
    "'str' object has no attribute 'get'". `topology` had the identical bug.
    The whole commentary gate was unrunnable against the file the engine writes.
    """
    prices = permitted_prices(ENGINE_STATE)
    assert 29127 in prices          # the nested scenario's trigger level
    assert 29330 in prices          # the nested scenario's third target
    assert 29227 in prices          # the nested topology feature's upper edge
    assert 29189 in prices          # ...and its peak
    assert 29205 in prices          # flips, which were never in doubt


def test_permitted_prices_still_accepts_the_flat_report_view():
    """The other shape must keep working; this is the one the sheet path uses."""
    flat = {"reference": ENGINE_STATE["reference"], "flips": ENGINE_STATE["flips"],
            "scenarios": [{"label": "base case", "trigger": "a 30-minute close above 29,127",
                           "targets": [29150.0, 29330.0]}],
            "topology": [{"lower": 29126.83, "upper": 29226.83, "center": 29176.83}]}
    prices = permitted_prices(flat)
    assert 29127 in prices and 29330 in prices and 29227 in prices


def test_verify_runs_end_to_end_on_the_engines_nested_state():
    """The crash was in verify()'s first call, so nothing downstream ever ran."""
    ok, probs = verify({"thesis": "Price sits below the 29,205 flip.",
                        "caveats": ["no directional confidence is computed"]},
                       ENGINE_STATE)
    assert all("object has no attribute" not in p for p in probs)
    assert not any("invented level" in p and "29,205" in p for p in probs)


# --- check_card_reach ------------------------------------------------------
# The cards are written by the commentary model, not generated, so
# scenario.Config.boundary_ems -- which has always said trigger levels are sought
# within one expected move -- never applied to them. 39% of the archive sat beyond
# it and filled at 22%, against 46% inside. See interpret.check_card_reach.

def _T(ref=29000.0, em=200.0, scale=1.0):
    return {"last_print": ref, "em": em, "rail_scale": scale}


def test_reach_passes_a_card_inside_half_a_band():
    ok, pr = interpret.check_card_reach([{"name": "near", "entry": 29080.0}], _T())   # 0.40 EM
    assert ok and pr == []


def test_reach_flags_only_what_is_off_the_day_s_map():
    """2026-09-16: half a band is where a card stops being LIVE, not where it stops
    existing. Beyond it the card ships flagged 'watch'; only beyond the session
    expected move is it refused."""
    ok, pr = interpret.check_card_reach([{"name": "far", "entry": 29260.0}], _T())
    assert not ok and len(pr) == 1                       # 1.30 EM -- off the map
    assert "1.30 of the session expected move" in pr[0]
    assert "28,800" in pr[0] and "29,200" in pr[0]       # the day window it must land in
    # 0.75 EM used to be refused by the same call; now it is a watch and it ships
    ok2, pr2 = interpret.check_card_reach([{"name": "w", "entry": 29150.0}], _T())
    assert ok2 and pr2 == []


def test_live_is_measured_on_the_band_actually_drawn():
    """An Asia sheet draws em x rail_scale, so the same entry is live on the full band
    and only a watch on the narrow one -- but it is on the map either way, because the
    DAY frame is the unscaled session move and does not shrink with the clock."""
    entry = {"name": "c", "entry": 29080.0}
    assert interpret.reach_state(entry, _T(em=200.0, scale=1.0))["state"] == "live"
    assert interpret.reach_state(entry, _T(em=200.0, scale=0.37))["state"] == "watch"
    assert interpret.check_card_reach([entry], _T(em=200.0, scale=0.37))[0]


def test_reach_invents_no_failure_without_a_reference_or_band():
    assert interpret.check_card_reach([{"name": "c", "entry": 1.0}], {}) == (True, [])
    assert interpret.check_card_reach([{"name": "c", "entry": 1.0}], {"last_print": 29000.0}) == (True, [])
    assert interpret.check_card_reach([{"name": "c", "entry": 1.0}], {"em": 200.0}) == (True, [])


def test_reach_ignores_a_card_with_no_numeric_entry():
    assert interpret.check_card_reach([{"name": "c"}, {"name": "d", "entry": None}], _T()) == (True, [])


def test_live_defaults_to_half_the_drawn_band():
    """0.5 EM, measured on the card archive -- NOT scenario.Config.boundary_ems (1.0),
    which governs the scenario generator and loses to 0.5 on both fill and win rate."""
    import scenario
    assert interpret.REACH_EMS == 0.5
    assert scenario.Config().boundary_ems == 1.0          # the one it is deliberately not
    assert interpret.check_card_reach.__defaults__[0] is None
    ref, em = 29000.0, 200.0
    edge = ref + interpret.REACH_EMS * em                 # 29,100
    assert interpret.reach_state({"entry": edge - 1}, _T(ref, em))["state"] == "live"
    assert interpret.reach_state({"entry": edge + 1}, _T(ref, em))["state"] == "watch"
    assert interpret.check_card_reach([{"name": "c", "entry": edge + 1}], _T(ref, em))[0]


def test_the_day_s_map_is_the_session_move_and_does_not_shrink():
    """DAY_EMS is measured on em_full (the session move), not on the drawn band, so a
    level stays on the map all afternoon. This is the 2026-09-16 defect: nine sheets
    were built and the top node on the board was carded by none of them."""
    ref = 29000.0
    T = {"last_print": ref, "em": 200.0, "em_full": 200.0, "rail_scale": 0.25}   # late session
    assert interpret.bands(T) == (ref, 50.0, 200.0)
    assert interpret.reach_state({"entry": ref + 150}, T)["state"] == "watch"    # on the map
    assert interpret.reach_state({"entry": ref + 250}, T)["state"] == "off-map"
    cards = [{"name": "on", "entry": ref + 150}, {"name": "off", "entry": ref + 250}]
    ok, pr = interpret.check_card_reach(cards, T)
    assert not ok and len(pr) == 1 and "off" in pr[0]
    assert cards[0]["reach"] == "watch" and cards[1]["reach"] == "off-map"
