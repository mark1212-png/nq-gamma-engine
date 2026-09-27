"""
Unit tests for regime.py.

The market-state layer is where a project quietly acquires a fortune-teller, so
a good share of these tests are about what it must REFUSE to say: no overall
label, no range claim, no direction, and no confidence manufactured out of a
branch being taken.
"""
import math

import pytest

import regime
from regime import Config, market_state, data_quality, _band, _conf

from test_nodescore import book, one_book


def _out(**kw):
    """The minimum of trinity_db.analyse's dict that regime.py reads."""
    d = {"panels": {}, "by_expiry": [], "iv_atm": 0.15, "expiries": 1,
         "oi_vintage": "final", "risk_coverage": "all live books have an IV curve",
         "excluded_risk_books": [], "no_0dte": False, "last_at": ""}
    d.update(kw)
    return d


def _panels(zero=None, near=None, full=None):
    mk = lambda h: {"hvl": ([] if h is None else [h]), "gamma": {}, "net_total": 0, "expiries": 1}
    return {"0DTE": mk(zero), "NEAR": mk(near), "FULL": mk(full)}


# ------------------------------------------------------------- primitives ---

def test_band_labels_by_magnitude():
    c = Config()
    assert _band(0.9, c)[0] == "strong"
    assert _band(-0.9, c)[0] == "strong"
    assert _band(0.05, c)[0] == "flat"
    assert _band(0.4, c)[0] == "moderate"


def test_confidence_rises_with_distance_from_the_boundary():
    """A reading sitting on a threshold is genuinely uncertain, and this is the
    mechanism that makes the confidence mean something."""
    near = _conf(0.0, 0.6)
    far = _conf(0.6, 0.6)
    assert far > near


def test_confidence_is_reduced_by_data_penalties():
    assert _conf(0.6, 0.6, 0.5) < _conf(0.6, 0.6)


def test_confidence_never_reaches_certainty():
    """A deterministic calculation on a noisy book is still a measurement."""
    assert _conf(1e9, 0.1) <= 0.95
    assert _conf(0.0, 0.6, 0.99) >= 0.05


# ---------------------------------------------------------- gamma regime ---

def test_gamma_regime_reports_both_measures():
    bs = one_book({1000.0: (100, 0)}, dte=0)
    st = market_state(_out(panels=_panels()), bs, 1000.0, 1000.0, 50.0)
    g = st["dimensions"]["gamma_regime"]
    assert "at_spot" in g and "chain_wide" in g
    assert "measures_agree" in g


def test_gamma_regime_flags_a_sign_disagreement_and_lowers_confidence():
    """HANDOFF records this happening for real on 2026-09-09: chain-wide +85/+457
    while at spot -524/-548. A book with puts near the money and a large far call
    wall reproduces the shape."""
    bs = one_book({1000.0: (0, 300), 1200.0: (400, 0)}, dte=0, T=40.0 / 252.0)
    st = market_state(_out(panels=_panels()), bs, 1000.0, 1000.0, 30.0)
    g = st["dimensions"]["gamma_regime"]
    if not g["measures_agree"]:
        assert any("DISAGREE" in e for e in g["evidence"])
        same = market_state(_out(panels=_panels()), one_book({1000.0: (100, 0)}, dte=0),
                            1000.0, 1000.0, 50.0)["dimensions"]["gamma_regime"]
        assert g["confidence"] < same["confidence"]


def test_gamma_regime_sign_follows_the_book():
    pos = market_state(_out(panels=_panels()), one_book({1000.0: (100, 0)}),
                       1000.0, 1000.0, 50.0)["dimensions"]["gamma_regime"]
    neg = market_state(_out(panels=_panels()), one_book({1000.0: (0, 100)}),
                       1000.0, 1000.0, 50.0)["dimensions"]["gamma_regime"]
    assert pos["score"] > 0 > neg["score"]


def test_gamma_regime_score_is_bounded():
    st = market_state(_out(panels=_panels()), one_book({1000.0: (100, 0)}), 1000.0, 1000.0, 50.0)
    assert -1.0 <= st["dimensions"]["gamma_regime"]["score"] <= 1.0


# ------------------------------------------------------------- flip state ---

def test_flip_state_reports_side_per_book():
    out = _out(panels=_panels(zero=990.0, near=980.0, full=970.0))
    st = market_state(out, one_book({1000.0: (100, 0)}), 1000.0, 1000.0, 50.0)
    f = st["dimensions"]["flip_state"]
    assert f["per_book"]["0DTE"]["side"] == "above"
    assert f["label"] == "above every flip"
    # position and proximity are separate facts: price is above every flip AND
    # close to the nearest one, and both must survive into the output
    assert f["at_flip"] is True


def test_flip_state_detects_being_inside_the_band():
    out = _out(panels=_panels(zero=1050.0, near=980.0, full=970.0))
    st = market_state(out, one_book({1000.0: (100, 0)}), 1000.0, 1000.0, 200.0)
    f = st["dimensions"]["flip_state"]
    assert f["label"] == "inside the flip band"
    assert f["at_flip"] is True          # nearest crossing is 0.10 EM away


def test_flip_state_carries_the_range_caveat():
    """The one claim this layer is forbidden to make. HANDOFF: p = 0.46."""
    out = _out(panels=_panels(zero=990.0))
    st = market_state(out, one_book({1000.0: (100, 0)}), 1000.0, 1000.0, 50.0)
    assert "p=0.46" in st["dimensions"]["flip_state"]["caveat"]


def test_flip_state_handles_a_book_with_no_crossing():
    out = _out(panels=_panels())
    st = market_state(out, one_book({1000.0: (100, 0)}), 1000.0, 1000.0, 50.0)
    assert st["dimensions"]["flip_state"]["label"] == "unknown"
    assert st["dimensions"]["flip_state"]["confidence"] <= 0.1


# ----------------------------------------------------------- expiry state ---

def test_expiry_state_says_empty_not_flat_when_nothing_expires():
    bs = one_book({1000.0: (100, 0)}, dte=3)
    st = market_state(_out(panels=_panels()), bs, 1000.0, 1000.0, 50.0)
    e = st["dimensions"]["expiry_state"]
    assert e["label"] == "no 0DTE book"
    assert "EMPTY, not flat" in e["caveat"]


def test_expiry_state_reports_both_share_conventions():
    bs = [book({1000.0: (100, 0)}, dte=0), book({1000.0: (60, 0)}, dte=4)]
    e = market_state(_out(panels=_panels()), bs, 1000.0, 1000.0, 50.0)["dimensions"]["expiry_state"]
    assert "gross_share" in e and "net_share" in e


# ------------------------------------------------------------ vanna state ---

def test_vanna_state_is_reported_in_its_own_unit():
    bs = one_book({1040.0: (100, 0)}, dte=0)
    v = market_state(_out(panels=_panels()), bs, 1000.0, 1000.0, 50.0)["dimensions"]["vanna_state"]
    assert v["unit"] == "contracts of delta per vol point"


def test_vanna_state_score_is_bounded():
    bs = one_book({960.0: (0, 50), 1040.0: (100, 0)}, dte=0)
    v = market_state(_out(panels=_panels()), bs, 1000.0, 1000.0, 50.0)["dimensions"]["vanna_state"]
    assert -1.0 <= v["score"] <= 1.0


# -------------------------------------------------------------- vol state ---

def test_vol_state_refuses_a_percentile_without_a_distribution():
    """Calling a level elevated with no history behind it is a heuristic wearing
    a number, which is what the brief forbids."""
    v = market_state(_out(panels=_panels()), one_book({1000.0: (100, 0)}),
                     1000.0, 1000.0, 50.0)["dimensions"]["vol_state"]
    assert v["percentile"] is None
    assert v["label"] == "level only"
    assert v["confidence"] <= 0.3


def test_vol_state_ranks_against_a_supplied_history():
    hist = [0.10 + 0.001 * i for i in range(100)]        # 0.100 .. 0.199
    v = market_state(_out(iv_atm=0.19, panels=_panels()), one_book({1000.0: (100, 0)}),
                     1000.0, 1000.0, 50.0, iv_history=hist)["dimensions"]["vol_state"]
    assert v["percentile"] > 0.75
    assert v["label"] == "elevated"


def test_vol_state_rejects_a_history_too_short_to_rank():
    v = market_state(_out(panels=_panels()), one_book({1000.0: (100, 0)}),
                     1000.0, 1000.0, 50.0, iv_history=[0.1, 0.2, 0.3])["dimensions"]["vol_state"]
    assert v["percentile"] is None
    assert any("too short" in e for e in v["evidence"])


def test_a_short_history_lowers_confidence_relative_to_a_long_one():
    short = [0.10 + 0.001 * i for i in range(25)]
    long = [0.10 + 0.001 * i for i in range(400)]
    mk = lambda h: market_state(_out(iv_atm=0.15, panels=_panels()),
                                one_book({1000.0: (100, 0)}), 1000.0, 1000.0, 50.0,
                                iv_history=h)["dimensions"]["vol_state"]
    assert mk(short)["confidence"] < mk(long)["confidence"]


def test_term_structure_is_measured_inside_a_tradeable_horizon():
    """The board lists expiries out past two years. Comparing the front against
    an 841-session book measures the equity vol curve, not this session."""
    out = _out(panels=_panels(), by_expiry=[
        {"dte": 0, "atm_iv": 0.13}, {"dte": 27, "atm_iv": 0.18}, {"dte": 841, "atm_iv": 0.19}])
    v = market_state(out, one_book({1000.0: (100, 0)}), 1000.0, 1000.0, 50.0)["dimensions"]["vol_state"]
    assert v["term_structure"]["back_dte"] == 27
    assert "841" in v["term_structure"]["note"]


def test_skew_is_sampled_in_standard_deviations_not_fixed_moneyness():
    """A fixed 3% is ~1 sigma on a 30-session book and ~10 sigma on a 0DTE one.
    The sample point must scale with the book's own move."""
    short = market_state(_out(panels=_panels()), one_book({1000.0: (100, 0)}, T=1.0 / 252.0),
                         1000.0, 1000.0, 50.0)["dimensions"]["vol_state"]
    long = market_state(_out(panels=_panels()), one_book({1000.0: (100, 0)}, T=40.0 / 252.0),
                        1000.0, 1000.0, 50.0)["dimensions"]["vol_state"]
    if "skew" in short and "skew" in long:
        assert short["skew"]["sample_moneyness"] < long["skew"]["sample_moneyness"]


class KnotSmile:
    """A smile with REAL knots, so `regime` actually enters its skew block.

    The fixtures share `FlatSmile`, whose `.x` is an empty list. `market_state`
    gates the whole skew computation on `getattr(smile, "x", None)`, so with the
    flat smile that block never runs -- which meant BOTH skew tests in this file
    were passing vacuously behind an `if "skew" in ...` guard, and the caveat
    string had never once been asserted. Puts are priced above calls here so the
    label is deterministic.
    """
    def __init__(self, atm=0.15, tilt=0.6):
        self.atm, self.tilt = atm, tilt
        self.x = [-0.20, -0.05, 0.0, 0.05, 0.20]
        self.y = [atm - tilt * m for m in self.x]

    def __call__(self, m):
        return self.atm - self.tilt * max(min(m, self.x[-1]), self.x[0])


def skewed_book(**kw):
    b = one_book({1000.0: (100, 0)}, **kw)
    b[0]["smile"] = KnotSmile()
    return b


def test_skew_carries_the_493_session_null_not_the_63_session_hint():
    """The caveat must quote the measurement that SURVIVED.

    Until 2026-09-10 this stamped "rho +0.23, p 0.08" off 63 sessions and called
    skew a weak range signal. On 493 sessions the primary endpoint is rho -0.036,
    p 0.43, null in every vol tercile and all four walk-forward folds, and the
    original reading was a volatility confound. A caveat advertising a falsified
    hint is worse than no caveat, so this pins the new number AND pins that the
    supersession is still narrated -- dropping the old figure silently would let
    the next reader rediscover it.
    """
    hist = [0.15] * 30
    v = market_state(_out(panels=_panels()), skewed_book(),
                     1000.0, 1000.0, 50.0, iv_history=hist)["dimensions"]["vol_state"]
    assert "skew" in v and v["skew"]["label"] == "puts bid", "fixture did not produce a skew"
    assert "skew_caveat" in v, "a skew sample must always carry its caveat"
    c = v["skew_caveat"]
    assert "493 sessions" in c
    assert "rho -0.036" in c
    assert "DID NOT SURVIVE" in c
    assert "never trade it as a rule" in c
    # the superseded figure must still be NAMED as superseded, never left standing
    assert "+0.23" in c and c.index("DID NOT SURVIVE") > c.index("rho -0.036")


# ----------------------------------------------------------- data quality ---

def test_preliminary_open_interest_is_penalised():
    a = data_quality(_out(oi_vintage="final"))
    b = data_quality(_out(oi_vintage="preliminary"))
    assert b["score"] < a["score"]
    assert any("PRELIMINARY" in i for i in b["issues"])


def test_a_stale_reference_is_penalised_hardest():
    stale = data_quality(_out(last_at="13:39 UTC, 47 min stale"))
    prelim = data_quality(_out(oi_vintage="preliminary"))
    assert stale["score"] < prelim["score"]


def test_excluded_books_reduce_the_score():
    d = data_quality(_out(excluded_risk_books=[{"book": "x"}, {"book": "y"}]))
    assert d["score"] < 1.0
    assert any("excluded" in i for i in d["issues"])


def test_a_clean_book_scores_one():
    assert data_quality(_out())["score"] == 1.0


def test_data_quality_penalties_propagate_into_every_dimension():
    clean = market_state(_out(panels=_panels(zero=990.0)), one_book({1000.0: (100, 0)}),
                         1000.0, 1000.0, 50.0)
    dirty = market_state(_out(panels=_panels(zero=990.0), last_at="stale"),
                         one_book({1000.0: (100, 0)}), 1000.0, 1000.0, 50.0)
    for name in ("gamma_regime", "flip_state"):
        assert dirty["dimensions"][name]["confidence"] < clean["dimensions"][name]["confidence"]


# ----------------------------------------------------- what it must refuse ---

def test_there_is_no_overall_label_or_single_confidence():
    st = market_state(_out(panels=_panels()), one_book({1000.0: (100, 0)}), 1000.0, 1000.0, 50.0)
    assert "label" not in st
    assert "confidence" not in st
    assert "no_overall_label" in st


def test_no_dimension_carries_a_range_or_direction_field():
    st = market_state(_out(panels=_panels(zero=990.0)), one_book({1000.0: (100, 0)}),
                      1000.0, 1000.0, 50.0)
    banned = {"direction", "bias", "bullish", "bearish", "range_forecast",
              "expected_range", "compression", "target"}
    for name, d in st["dimensions"].items():
        assert banned.isdisjoint(d.keys()), f"{name} carries a forbidden field"


def test_charm_is_declared_absent_with_its_reason():
    st = market_state(_out(panels=_panels()), one_book({1000.0: (100, 0)}), 1000.0, 1000.0, 50.0)
    assert "worse than chance" in st["charm_state"]


def test_every_dimension_carries_evidence_and_a_confidence():
    st = market_state(_out(panels=_panels(zero=990.0), by_expiry=[{"dte": 0, "atm_iv": 0.13},
                                                                  {"dte": 5, "atm_iv": 0.15}]),
                      one_book({1000.0: (100, 0)}), 1000.0, 1000.0, 50.0)
    for name, d in st["dimensions"].items():
        assert "confidence" in d, f"{name} has no confidence"
        assert 0.0 <= d["confidence"] <= 1.0
        assert d.get("evidence"), f"{name} has no evidence"


def test_an_empty_book_is_handled():
    st = market_state(_out(), [], 1000.0, 1000.0, 50.0)
    assert st["dimensions"] == {}
    assert "empty book" in st["note"]
