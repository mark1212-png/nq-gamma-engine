"""
Unit tests for crossasset.py.

Two things dominate: that the scalar is never allowed to present itself as a
direction forecast, and that five instruments on two underlyings are not weighted
as five independent votes.
"""
import math

import pytest

import crossasset
from crossasset import (Config, agreement, state_score, dollar_gamma,
                        complex_of, COMPLEXES, MULTIPLIER)


def _inst(gamma_at_spot=0.0, gamma_gross=1000.0, reference=1000.0,
          flip_distance_ems=None, skew_spread=None, stale=False):
    return {"gamma_at_spot": gamma_at_spot, "gamma_gross": gamma_gross,
            "reference": reference, "flip_distance_ems": flip_distance_ems,
            "skew_spread": skew_spread, "stale": stale}


# ----------------------------------------------------------------- units ---

def test_dollar_gamma_uses_the_right_multiplier_per_root():
    """20 for NQ, 100 for the equity roots. opra_compare.py's convention."""
    assert dollar_gamma(1.0, 29000.0, "NQ") == pytest.approx(29000.0 * 20)
    assert dollar_gamma(1.0, 7700.0, "SPX") == pytest.approx(7700.0 * 100)
    assert dollar_gamma(1.0, 770.0, "SPY") == pytest.approx(770.0 * 100)


def test_contract_gamma_is_not_comparable_but_dollar_gamma_is():
    """One contract of NQ delta per 1% and one of SPY are wildly different
    amounts of money. That is why nothing is weighted in contract terms."""
    nq = dollar_gamma(1.0, 29000.0, "NQ")
    spy = dollar_gamma(1.0, 770.0, "SPY")
    assert nq / spy > 7


def test_every_known_root_maps_to_a_complex():
    for roots in COMPLEXES.values():
        for r in roots:
            assert complex_of(r) is not None


def test_an_unknown_root_maps_to_no_complex():
    assert complex_of("RUT") is None


# ------------------------------------------------------------ state score ---

def test_state_score_is_bounded():
    cfg = Config()
    for g in (-1e9, -100.0, 0.0, 100.0, 1e9):
        s, _ = state_score(g, 1000.0, 5.0, 0.5, cfg)
        assert -1.0 <= s <= 1.0


def test_long_gamma_above_the_flip_with_calls_bid_scores_positive():
    s, _ = state_score(900.0, 1000.0, 1.0, -0.03, Config())
    assert s > 0.5


def test_short_gamma_below_the_flip_with_puts_bid_scores_negative():
    s, _ = state_score(-900.0, 1000.0, -1.0, 0.03, Config())
    assert s < -0.5


def test_the_score_is_antisymmetric():
    """Mirroring every input must mirror the score, so neither sign is favoured."""
    cfg = Config()
    a, _ = state_score(600.0, 1000.0, 0.7, -0.02, cfg)
    b, _ = state_score(-600.0, 1000.0, -0.7, 0.02, cfg)
    assert a == pytest.approx(-b, abs=1e-6)


def test_puts_bid_pushes_toward_the_amplify_end():
    cfg = Config()
    puts, _ = state_score(0.0, 1000.0, 0.0, 0.04, cfg)
    calls, _ = state_score(0.0, 1000.0, 0.0, -0.04, cfg)
    assert puts < 0 < calls


def test_a_far_flip_saturates_rather_than_dominating():
    cfg = Config()
    near, _ = state_score(0.0, 1000.0, 2.0, None, cfg)
    far, _ = state_score(0.0, 1000.0, 50.0, None, cfg)
    assert far > near
    assert far <= 1.0
    assert abs(far - near) < 0.2


def test_missing_components_are_recorded_as_none_not_zero():
    s, comps = state_score(500.0, 1000.0, None, None, Config())
    assert comps["flip"] is None and comps["skew"] is None
    assert comps["gamma"] is not None
    assert s is not None


def test_no_usable_component_gives_no_score():
    s, comps = state_score(0.0, 0.0, None, None, Config())
    assert s is None


# --------------------------------------------------------------- weighting ---

def test_five_instruments_are_not_five_equal_votes():
    """Weighting five equally counts the SPX complex three times. The composite
    must be an average of two complexes, not of five books."""
    inst = {r: _inst(gamma_at_spot=800.0, gamma_gross=1000.0) for r in ("SPX", "SPXW", "SPY")}
    inst["NQ"] = _inst(gamma_at_spot=-800.0, gamma_gross=1000.0)
    inst["QQQ"] = _inst(gamma_at_spot=-800.0, gamma_gross=1000.0)
    a = agreement(inst)
    # three positive books and two negative ones, but one complex each way
    assert a["composite"] == pytest.approx(0.0, abs=1e-6)


def test_within_a_complex_the_bigger_book_carries_more_weight():
    inst = {"SPX": _inst(gamma_at_spot=1000.0, gamma_gross=1000.0, reference=7700.0),
            "SPY": _inst(gamma_at_spot=-1000.0, gamma_gross=10.0, reference=770.0)}
    a = agreement(inst)
    w = a["complexes"]["SPX"]["size_weights"]
    assert w["SPX"] > w["SPY"]
    assert a["complexes"]["SPX"]["state_score"] > 0


def test_size_weights_within_a_complex_sum_to_one():
    inst = {"SPX": _inst(gamma_at_spot=500.0, gamma_gross=1000.0, reference=7700.0),
            "SPXW": _inst(gamma_at_spot=300.0, gamma_gross=800.0, reference=7700.0),
            "SPY": _inst(gamma_at_spot=100.0, gamma_gross=400.0, reference=770.0)}
    a = agreement(inst)
    assert sum(a["complexes"]["SPX"]["size_weights"].values()) == pytest.approx(1.0, abs=1e-3)


def test_complex_weights_are_configurable():
    inst = {"NQ": _inst(gamma_at_spot=1000.0, gamma_gross=1000.0),
            "SPX": _inst(gamma_at_spot=-1000.0, gamma_gross=1000.0)}
    even = agreement(inst)
    tilt = agreement(inst, complex_weights={"NDX": 3.0, "SPX": 1.0})
    assert tilt["composite"] > even["composite"]


def test_the_weighting_rationale_is_stated_in_the_output():
    """The rationale changed on 2026-09-10 when it was measured. It used to rest
    on QQQ and NQ being one underlying; the correlation matrix says that pair is
    one of the WEAKER ones. It now rests on the correlation structure itself,
    which is what actually supports not weighting five instruments equally."""
    a = agreement({"NQ": _inst(gamma_at_spot=100.0)})
    assert "count one factor five times" in a["weights_note"]
    assert "0.59 to 0.91 correlated" in a["weights_note"]
    assert a["weights_validated"] is False


# ------------------------------------------------------------- agreement ---

def test_agreement_is_high_when_the_complexes_match():
    inst = {"NQ": _inst(gamma_at_spot=800.0, gamma_gross=1000.0),
            "SPX": _inst(gamma_at_spot=800.0, gamma_gross=1000.0)}
    a = agreement(inst)
    assert a["agreement_strength"] > 0.9
    assert a["conflict"] < 0.1
    assert a["complexes_same_sign"] is True


def test_agreement_is_low_when_the_complexes_oppose():
    inst = {"NQ": _inst(gamma_at_spot=1000.0, gamma_gross=1000.0),
            "SPX": _inst(gamma_at_spot=-1000.0, gamma_gross=1000.0)}
    a = agreement(inst)
    assert a["agreement_strength"] < 0.2
    assert a["complexes_same_sign"] is False


def test_agreement_and_conflict_sum_to_one():
    inst = {"NQ": _inst(gamma_at_spot=300.0), "SPX": _inst(gamma_at_spot=-600.0)}
    a = agreement(inst)
    assert a["agreement_strength"] + a["conflict"] == pytest.approx(1.0, abs=1e-9)


def test_a_single_complex_yields_no_agreement_measure():
    """Agreement between one thing and itself is not a measurement."""
    a = agreement({"NQ": _inst(gamma_at_spot=500.0), "QQQ": _inst(gamma_at_spot=400.0)})
    assert a["agreement_strength"] is None
    assert a["n_complexes"] == 1


def test_a_wide_internal_spread_is_flagged_as_a_data_problem():
    """Two books on ONE underlying disagreeing says something is wrong with one
    of them, not something about the market."""
    inst = {"SPX": _inst(gamma_at_spot=1000.0, gamma_gross=1000.0, reference=7700.0),
            "SPY": _inst(gamma_at_spot=-1000.0, gamma_gross=1000.0, reference=770.0)}
    a = agreement(inst)
    c = a["complexes"]["SPX"]
    assert c["internal_warning"] is True
    assert "data problem" in c["internal_note"]


# ------------------------------------------------- what it must not claim ---

def test_the_output_declares_that_it_does_not_predict_direction():
    a = agreement({"NQ": _inst(gamma_at_spot=1000.0), "SPX": _inst(gamma_at_spot=1000.0)})
    assert a["predicts_direction"] is False
    assert "not a price forecast" in a["direction_note"]


def test_the_direction_note_cites_the_measurements_behind_the_refusal():
    a = agreement({"NQ": _inst(gamma_at_spot=1000.0)})
    n = a["direction_note"]
    assert "p=0.46" in n
    assert "0.00 with direction" in n or "correlates 0.00" in n


def test_no_field_is_named_bullish_or_bearish():
    a = agreement({"NQ": _inst(gamma_at_spot=1000.0), "SPX": _inst(gamma_at_spot=-500.0)})
    blob = repr(a)
    for f in ('"bullish"', '"bearish"', "'bullish'", "'bearish'"):
        assert f not in blob


def test_the_scale_explains_both_ends_in_positioning_terms():
    a = agreement({"NQ": _inst(gamma_at_spot=1000.0)})
    assert "DAMPEN" in a["scale"]["+1"]
    assert "AMPLIFY" in a["scale"]["-1"]


# ------------------------------------------------------------ edge cases ---

def test_no_instruments_is_handled():
    a = agreement({})
    assert a["composite"] is None
    assert "no instruments" in a["note"]


def test_an_unknown_root_does_not_break_the_aggregation():
    a = agreement({"RUT": _inst(gamma_at_spot=500.0), "NQ": _inst(gamma_at_spot=500.0)})
    assert a["instruments"]["RUT"]["complex"] is None
    assert "NDX" in a["complexes"]


def test_an_instrument_with_no_exposure_is_excluded_not_scored_zero():
    a = agreement({"NQ": _inst(gamma_at_spot=0.0, gamma_gross=0.0),
                   "SPX": _inst(gamma_at_spot=500.0, gamma_gross=1000.0)})
    assert a["instruments"]["NQ"]["state_score"] is None
    assert "NDX" not in a["complexes"]


def test_a_zero_reference_price_does_not_divide_by_zero():
    a = agreement({"NQ": _inst(gamma_at_spot=500.0, gamma_gross=1000.0, reference=0.0)})
    assert a["instruments"]["NQ"]["size_usd_per_pct"] == 0.0
    assert a["complexes"]["NDX"]["state_score"] is not None


def test_staleness_is_carried_through():
    a = agreement({"NQ": _inst(gamma_at_spot=500.0, stale=True)})
    assert a["instruments"]["NQ"]["stale"] is True


# ------------------------------------- the measurement, carried in the code ---

def test_the_measured_null_travels_with_every_result():
    """502 sessions, Sep 2024 to Sep 2026: the composite carries no measurable
    information about the next session. That finding must ride with the score, or
    a reader treats a composite of -0.48 as a view. Full write-up in
    Claude outputs/RESULTS-crossasset-500-sessions.md."""
    a = agreement({"NQ": _inst(gamma_at_spot=500.0), "SPX": _inst(gamma_at_spot=-300.0)})
    m = a["measured"]
    assert m["sessions"] == 502
    assert m["rho_composite_vs_next_return"] == pytest.approx(-0.052)
    assert m["one_standard_error_on_rho"] == pytest.approx(0.045)
    assert "NO measurable information" in m["verdict"]
    assert "DO NOT FIT THE WEIGHTS" in m["verdict"]


def test_every_measured_rho_is_inside_two_standard_errors():
    """The claim in the verdict has to be arithmetically true of the numbers
    stored beside it."""
    m = agreement({"NQ": _inst(gamma_at_spot=500.0)})["measured"]
    se = m["one_standard_error_on_rho"]
    for k in ("rho_composite_vs_next_return", "rho_composite_vs_next_range",
              "rho_conflict_vs_next_range"):
        assert abs(m[k]) < 2 * se, f"{k} is no longer inside two standard errors"


def test_the_grouping_caveat_is_recorded_not_buried():
    """The correlation structure partly falsifies the rationale the grouping was
    built on. That belongs in the output, not only in a results file."""
    m = agreement({"NQ": _inst(gamma_at_spot=500.0)})["measured"]
    c = m["caveat_to_the_grouping"]
    assert "only weakly supported" in c
    assert "0.746" in c            # NQ-QQQ, the weak within-complex pair
    assert m["correlation_within_complex_median"] > m["correlation_across_complex_median"]


def test_the_weights_note_no_longer_claims_a_tautology():
    a = agreement({"NQ": _inst(gamma_at_spot=500.0)})
    assert "near-tautological" not in a["weights_note"]
    assert "0.59 to 0.91 correlated" in a["weights_note"]
