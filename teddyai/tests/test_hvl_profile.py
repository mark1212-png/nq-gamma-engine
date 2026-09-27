"""
Unit tests for the refined flip sweep and the gamma profile (trinity_db, 2026-09-12).

The refinement must return THE SAME crossings the full 1,001-point sweep did --
not close, the same floats -- because the regression wall compares flips
exactly. So the synthetic tests compare every=1 against the default on books
whose answer is inspectable, and the one chain-backed test does the same on a
real cached book across all three panels.
"""
import math

import pytest

import nqvol
import trinity_db
from trinity_db import (hvl, hvl_profile, profile_read, net_position, strike_metrics,
                        strike_charm, _sweep_points)
from conftest import have_chain, chain_path


def _smile():
    return nqvol.Smile([(-0.10, 0.22), (0.0, 0.18), (0.10, 0.16)], 0.18)


def _book(strikes, F=20000.0, T=5 / 252, root="X", basis=0.0):
    """strikes: {K: (call_oi, put_oi)}"""
    return {"root": root, "F": F, "T": T, "basis": basis, "smile": _smile(),
            "grid": 25.0, "cap": 0.25,
            "strikes": {f"{k:.1f}": {"C": {"oi": c}, "P": {"oi": p}} for k, (c, p) in strikes.items()}}


def _one_crossing_books():
    # calls above, puts below: dealer gamma positive above, negative below -> one flip near 20,000
    st = {}
    for i in range(-12, 13):
        K = 20000.0 + 25 * i
        st[K] = (60 if K > 20000 else 5, 60 if K < 20000 else 5)
    return [_book(st)]


# ----------------------------------------------------------- equivalence ---

def test_the_sweep_grid_is_walked_exactly_as_before():
    """Accumulated, so 0.1 steps land where the old loop landed, not on lo + i*step."""
    xs = _sweep_points(-40, 40, 0.1)
    x, walked = -40.0, []
    while x <= 40:
        walked.append(x)
        x += 0.1
    assert xs == walked


def test_refined_sweep_returns_the_full_sweep_crossings_exactly():
    books = _one_crossing_books()
    full, _ = hvl_profile(books, 20000.0, -500, 500, 1.0, every=1)
    fast, _ = hvl_profile(books, 20000.0, -500, 500, 1.0)
    assert full == fast
    assert len(full) == 1 and abs(full[0] - 20000.0) < 60


def test_hvl_is_the_refined_sweep():
    books = _one_crossing_books()
    assert hvl(books, 20000.0) == hvl_profile(books, 20000.0)[0]


def test_the_refinement_needs_far_fewer_evaluations():
    books = _one_crossing_books()
    _, full = hvl_profile(books, 20000.0, every=1)
    _, fast = hvl_profile(books, 20000.0)
    assert full["evaluations"] == 1001
    assert fast["evaluations"] < 1001 / 4


def test_two_crossings_are_both_found():
    """A book heavy in puts inside +/-100 and in calls beyond +/-300, one session
    out so the kernels are ~230 points wide: negative at spot, positive on both
    wings, two flips (measured at 19,658 and 20,347 when this was written)."""
    st = {}
    for i in range(-28, 29):
        K = 20000.0 + 25 * i
        d = abs(K - 20000)
        st[K] = (5, 400) if d <= 100 else ((100, 5) if d >= 300 else (5, 5))
    books = [_book(st, T=1 / 252)]
    full, _ = hvl_profile(books, 20000.0, every=1)
    fast, prof = hvl_profile(books, 20000.0)
    assert full == fast
    assert len(full) == 2
    mid = prof["g"][prof["x"].index(0.0)]
    assert prof["g"][0] > 0 > mid and prof["g"][-1] > 0


def test_an_empty_sweep_range_returns_nothing():
    c, p = hvl_profile(_one_crossing_books(), 20000.0, 10, 0, 1.0)
    assert c == [] and p["x"] == []


@pytest.mark.parametrize("settle,report", [("2026-09-08", "2026-09-09")])
def test_on_a_real_book_every_panel_matches_the_full_sweep(settle, report):
    if not have_chain(settle):
        pytest.skip("no cached chain")
    raw, books, S, asof, F_ref, sym = trinity_db.load(str(chain_path(settle)), report)
    P = trinity_db.panel_cols(books)
    for name, bs in P.items():
        if not bs:
            continue
        full, _ = hvl_profile(bs, F_ref, *trinity_db.HVL_SWEEP, every=1)
        fast, _ = hvl_profile(bs, F_ref, *trinity_db.HVL_SWEEP)
        assert full == fast, f"{name}: {full} != {fast}"


# -------------------------------------------------------------- profile ---

def test_the_profile_is_a_regular_grid_in_the_engine_units():
    _, prof = hvl_profile(_one_crossing_books(), 20000.0)
    assert prof["step"] == 5.0
    assert prof["x"][0] == -500.0 and prof["x"][-1] == 500.0
    assert len(prof["x"]) == len(prof["g"]) == 201
    assert "contracts of delta per 1% move" in prof["units"]


def test_the_profile_crosses_zero_where_the_flip_is():
    books = _one_crossing_books()
    cross, prof = hvl_profile(books, 20000.0)
    x = cross[0] - 20000.0
    below = [g for xx, g in zip(prof["x"], prof["g"]) if xx < x - 5]
    above = [g for xx, g in zip(prof["x"], prof["g"]) if xx > x + 5]
    assert below and above
    assert below[-1] < 0 < above[0]


def test_profile_read_interpolates_and_reports_the_slope():
    prof = {"x": [0.0, 10.0, 20.0], "g": [-10.0, 0.0, 10.0]}
    r = profile_read(prof, 5.0)
    assert r["g"] == pytest.approx(-5.0)
    assert r["slope"] == pytest.approx(1.0)
    assert "per point" in r["slope_units"]


def test_profile_read_is_none_outside_the_swept_range():
    prof = {"x": [0.0, 10.0], "g": [1.0, 2.0]}
    assert profile_read(prof, -1.0)["g"] is None
    assert profile_read(prof, 11.0)["slope"] is None


def test_the_read_at_spot_rides_in_the_panel_block():
    if not have_chain("2026-09-08"):
        pytest.skip("no cached chain")
    out, _, _ = trinity_db.analyse(str(chain_path("2026-09-08")), "2026-09-09", hours=6.5, quiet=True)
    for p in ("NEAR", "FULL"):
        blk = out["panels"][p]
        assert blk["profile"]["x"]
        assert blk["at_spot"]["slope"] is not None


# --------------------------------------------------------- dealer sign ---

def test_the_convention_is_plus_calls_minus_puts():
    assert net_position({"C": {"oi": 30}, "P": {"oi": 10}}) == 20
    assert net_position({"C": {"oi": 30}, "P": {"oi": 10}}, dealer_long_calls=False) == -20


def test_a_stamped_flow_position_overrides_the_convention():
    rec = {"C": {"oi": 30}, "P": {"oi": 10}, "pos": {"dealer": -25.0, "source": ["flow"]}}
    assert net_position(rec) == -25.0
    assert net_position(rec, dealer_long_calls=False) == 25.0


def test_the_opposite_book_negates_every_strike_exactly():
    books = _one_crossing_books()
    g, v = strike_metrics(books)
    gi, vi = strike_metrics(books, dealer_long_calls=False)
    assert set(g) == set(gi)
    for k in g:
        assert gi[k] == pytest.approx(-g[k])
        assert vi[k] == pytest.approx(-v[k])
    c, ci = strike_charm(books), strike_charm(books, dealer_long_calls=False)
    for k in c:
        assert ci[k] == pytest.approx(-c[k])


def test_the_flip_does_not_move_under_the_opposite_book():
    books = _one_crossing_books()
    assert hvl(books, 20000.0) == hvl(books, 20000.0, dealer_long_calls=False)


def test_a_stamped_position_changes_the_exposure_and_a_loader_walk_survives_it():
    books = _one_crossing_books()
    g0, _ = strike_metrics(books)
    rec = books[0]["strikes"]["20100.0"]
    rec["pos"] = {"dealer": -55.0, "source": ["flow"], "flow_share": 1.0}
    g1, _ = strike_metrics(books)
    assert g1[20100.0] == pytest.approx(-g0[20100.0])            # +55 -> -55
    # the pattern every loader uses on a record's values must not break on the stamp
    assert sum((c.get("oi", 0) or 0) for c in rec.values()) == 65
