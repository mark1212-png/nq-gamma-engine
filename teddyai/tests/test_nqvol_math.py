"""
Unit tests for nqvol.py -- the project's ONE implementation of time, Black-76,
the smile and the IV solve.

Small, hand-checkable inputs only. No cached chain is read here: if these fail,
the failure is in the mathematics, not in the data.

The properties tested are the ones the rest of the engine silently relies on:
  * put-call parity, so the C and P legs of a strike agree
  * gamma is identical for the call and the put at a strike -- METHOD.md section 2
    verifies the engine against CME's own Gamma(1 Pct) column using exactly this
  * the vol solve inverts the pricer
  * business time is zero across a weekend and one unit across a session
  * the smile extends its wing slope instead of going flat, which is what stopped
    put-side vol (and therefore downside gamma, where the stops sit) being
    understated by construction
"""
import math
import datetime

import pytest

import nqvol
from nqvol import bs76_price, implied_vol, greeks, Smile


def _delta_call(F, K, sig, T):
    d1 = (math.log(F / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
    return 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))


# --------------------------------------------------------------- Black-76 ---

def test_put_call_parity():
    """C - P = D * (F - K). With r = 0 the discount factor is 1."""
    F, K, sig, T = 29000.0, 29100.0, 0.15, 0.02
    c = bs76_price(F, K, sig, T, True)
    p = bs76_price(F, K, sig, T, False)
    assert c - p == pytest.approx(F - K, abs=1e-9)


def test_parity_with_nonzero_rate():
    F, K, sig, T, r = 650.0, 640.0, 0.20, 0.10, 0.045
    c = bs76_price(F, K, sig, T, True, r)
    p = bs76_price(F, K, sig, T, False, r)
    assert c - p == pytest.approx(math.exp(-r * T) * (F - K), abs=1e-9)


def test_expiry_is_intrinsic():
    assert bs76_price(100.0, 90.0, 0.2, 0.0, True) == pytest.approx(10.0)
    assert bs76_price(100.0, 110.0, 0.2, 0.0, True) == pytest.approx(0.0)
    assert bs76_price(100.0, 110.0, 0.2, 0.0, False) == pytest.approx(10.0)


def test_zero_vol_is_intrinsic():
    assert bs76_price(100.0, 90.0, 0.0, 1.0, True) == pytest.approx(10.0)
    assert bs76_price(100.0, 90.0, 0.0, 1.0, False) == pytest.approx(0.0)


def test_price_is_monotone_in_vol():
    F, K, T = 29000.0, 29000.0, 0.02
    px = [bs76_price(F, K, s, T, True) for s in (0.05, 0.10, 0.20, 0.40)]
    assert px == sorted(px)


def test_bad_inputs_raise():
    for args in [(-1.0, 100.0, 0.2, 1.0, True),       # F <= 0
                 (100.0, 0.0, 0.2, 1.0, True),        # K <= 0
                 (100.0, 100.0, -0.1, 1.0, True),     # sigma < 0
                 (float("nan"), 100.0, 0.2, 1.0, True)]:
        with pytest.raises(ValueError):
            bs76_price(*args)


# ------------------------------------------------------------- vol solve ---

def test_implied_vol_round_trips():
    F, K, T = 29000.0, 29250.0, 0.03
    for want in (0.08, 0.1234, 0.25, 0.60):
        px = bs76_price(F, K, want, T, True)
        assert implied_vol(px, F, K, T, True) == pytest.approx(want, abs=1e-5)


def test_implied_vol_round_trips_on_puts():
    F, K, T = 29000.0, 28500.0, 0.03
    for want in (0.09, 0.1777, 0.42):
        px = bs76_price(F, K, want, T, False)
        assert implied_vol(px, F, K, T, False) == pytest.approx(want, abs=1e-5)


def test_implied_vol_refuses_intrinsic_or_below():
    F, K, T = 29000.0, 28000.0, 0.02
    assert implied_vol(1000.0, F, K, T, True) is None       # exactly intrinsic
    assert implied_vol(999.0, F, K, T, True) is None        # below intrinsic
    assert implied_vol(50.0, F, K, 0.0, True) is None       # expired


def test_implied_vol_refuses_above_the_search_ceiling():
    """A price richer than sigma = 5.0 can produce is not inverted; it is
    reported as unsolvable rather than pinned silently to the ceiling."""
    F, K, T = 29000.0, 29000.0, 0.02
    too_rich = bs76_price(F, K, 5.0, T, True) * 1.01
    assert implied_vol(too_rich, F, K, T, True) is None


# ---------------------------------------------------------------- greeks ---

def test_gamma_is_the_same_for_call_and_put():
    """The single most load-bearing identity in the engine: strike_metrics uses
    ONE gamma for a strike's net call-minus-put open interest. METHOD.md section 2
    verifies this against CME's own Gamma(1 Pct) column using exactly this."""
    F, K, sig, T = 29000.0, 29300.0, 0.14, 0.02
    g, _ = greeks(F, K, sig, T)
    h = 1.0
    num_c = (bs76_price(F + h, K, sig, T, True) - 2 * bs76_price(F, K, sig, T, True)
             + bs76_price(F - h, K, sig, T, True)) / (h * h)
    num_p = (bs76_price(F + h, K, sig, T, False) - 2 * bs76_price(F, K, sig, T, False)
             + bs76_price(F - h, K, sig, T, False)) / (h * h)
    assert num_c == pytest.approx(num_p, rel=1e-6)
    assert g == pytest.approx(num_c, rel=1e-4)


def test_gamma_units_are_per_point_of_forward():
    """greeks() returns dDelta/dF per contract, in units of delta per POINT."""
    F, K, sig, T = 29000.0, 29000.0, 0.14, 0.02
    g, _ = greeks(F, K, sig, T)
    h = 0.1
    num = (_delta_call(F + h, K, sig, T) - _delta_call(F - h, K, sig, T)) / (2 * h)
    assert num == pytest.approx(g, rel=1e-5)


def test_one_percent_conversion_is_first_order_only():
    """strike_metrics reports gamma * 0.01 * F: delta change per 1% move, which
    is the QuikStrike Gamma(1 Pct) convention. That is a FIRST-ORDER figure. At
    the money, where gamma peaks, the true delta change over a full 1% move is
    ~5% smaller than the linear estimate, because gamma falls away on both sides.

    This is not a defect -- it is what the exchange's own column means, and
    METHOD.md section 2 verifies the engine against it. It is recorded here so
    nobody later "fixes" the linear conversion into a repriced one and silently
    changes every number on the sheet.
    """
    F, K, sig, T = 29000.0, 29000.0, 0.14, 0.02
    g, _ = greeks(F, K, sig, T)
    linear = g * 0.01 * F
    true_move = _delta_call(F * 1.01, K, sig, T) - _delta_call(F, K, sig, T)
    assert true_move < linear                              # gamma decays away from ATM
    assert true_move == pytest.approx(linear, rel=0.08)    # but the same order


def test_gamma_peaks_near_the_money():
    F, sig, T = 29000.0, 0.14, 0.02
    g_atm, _ = greeks(F, F, sig, T)
    for K in (F * 0.97, F * 0.99, F * 1.01, F * 1.03):
        g, _ = greeks(F, K, sig, T)
        assert g < g_atm


def test_gamma_and_vanna_are_zero_at_or_past_expiry():
    assert greeks(29000.0, 29000.0, 0.14, 0.0) == (0.0, 0.0)
    assert greeks(29000.0, 29000.0, 0.0, 0.02) == (0.0, 0.0)


def test_vanna_matches_a_numerical_ddelta_dvol():
    """vanna = dDelta/dVol per 1.00 of vol. strike_metrics scales by 0.01 to get
    per VOL POINT, which is the unit the sheet's VEX column is in."""
    F, K, sig, T = 29000.0, 29400.0, 0.14, 0.02
    _, va = greeks(F, K, sig, T)
    h = 1e-5
    num = (_delta_call(F, K, sig + h, T) - _delta_call(F, K, sig - h, T)) / (2 * h)
    assert va == pytest.approx(num, rel=1e-4)


def test_vanna_sign_flips_across_the_money():
    """vanna = -phi(d1) * d2 / sigma with d1 built from log(F/K).

    A strike BELOW the forward has log(F/K) > 0, so d2 > 0 and vanna is
    NEGATIVE. A strike ABOVE the forward has vanna POSITIVE: raising vol lifts
    an out-of-the-money call's delta. Measured at F = 29,000, sigma = 0.14,
    T = 0.02: -0.250 at 0.95F and +0.347 at 1.05F.

    A sign slip here would invert the whole VEX column, so the direction is
    pinned rather than assumed.
    """
    F, sig, T = 29000.0, 0.14, 0.02
    _, below = greeks(F, F * 0.95, sig, T)
    _, above = greeks(F, F * 1.05, sig, T)
    assert above > 0 > below


# ----------------------------------------------------------- business time ---

def test_a_plain_weekend_carries_no_time():
    """11 Sep 2026 is a Friday and 14 Sep an ordinary Monday, so business time is
    flat across the gap. Note the fixture choice: the 4 Sep / 8 Sep weekend does
    NOT work, because Labor Day sits inside it and carries 0.40 units."""
    fri = nqvol.close_utc(datetime.date(2026, 9, 11))
    mon = nqvol.open_utc(datetime.date(2026, 9, 14))
    assert nqvol.tau(mon) == pytest.approx(nqvol.tau(fri), abs=1e-12)


def test_a_weekend_containing_a_globex_holiday_carries_the_holiday_weight():
    """The counterpart of the test above, and the reason it needed a careful
    fixture. Labor Day 2026 is 19 hours of NQ tape, not a closure, so the clock
    advances 0.40 units across the long weekend rather than standing still."""
    fri = nqvol.close_utc(datetime.date(2026, 9, 4))
    mon = nqvol.open_utc(datetime.date(2026, 9, 8))
    assert nqvol.tau(mon) - nqvol.tau(fri) == pytest.approx(nqvol.GLOBEX_HOLIDAY_WEIGHT, abs=1e-9)


def test_one_session_is_one_unit():
    a = nqvol.close_utc(datetime.date(2026, 9, 2))
    b = nqvol.close_utc(datetime.date(2026, 9, 3))
    assert nqvol.tau(b) - nqvol.tau(a) == pytest.approx(1.0, abs=1e-9)


def test_years_between_is_sessions_over_252():
    a = nqvol.close_utc(datetime.date(2026, 9, 2))
    b = nqvol.close_utc(datetime.date(2026, 9, 3))
    assert nqvol.years_between(a, b) == pytest.approx(1.0 / 252.0, abs=1e-12)


def test_years_between_is_never_negative():
    a = nqvol.close_utc(datetime.date(2026, 9, 3))
    b = nqvol.close_utc(datetime.date(2026, 9, 2))
    assert nqvol.years_between(a, b) == 0.0


def test_rth_holds_more_than_half_the_session_variance():
    """The reason the calendar was replaced. At the RTH open the 0DTE still has
    ~55% of its last day's variance ahead of it, not the ~27% a calendar clock
    implies."""
    d = datetime.date(2026, 9, 3)
    at_open = nqvol.session_frac(nqvol.rth_open_utc(d), d)
    assert 0.42 < at_open < 0.48                 # 0.064 + 0.189 + 0.201 = 0.454
    assert 1.0 - at_open > 0.50


def test_a_full_closure_is_not_a_session():
    assert not nqvol.is_session(datetime.date(2026, 12, 25))
    assert nqvol.unit_weight(datetime.date(2026, 12, 25)) == 0.0


def test_a_globex_only_holiday_carries_partial_time():
    d = datetime.date(2026, 9, 7)                # Labor Day 2026
    assert not nqvol.is_session(d)
    assert nqvol.is_globex_holiday(d)
    assert nqvol.unit_weight(d) == pytest.approx(nqvol.GLOBEX_HOLIDAY_WEIGHT)


def test_expected_move_is_consistent_with_the_vol_it_uses():
    S, iv = 29000.0, 0.15
    assert nqvol.expected_move(S, iv, 1.0) == pytest.approx(S * iv / math.sqrt(252.0))
    assert nqvol.expected_move(S, iv, 4.0) == pytest.approx(2 * nqvol.expected_move(S, iv, 1.0))


def test_tau_needs_an_aware_datetime():
    with pytest.raises(ValueError):
        nqvol.tau(datetime.datetime(2026, 9, 3, 12, 0))


# ----------------------------------------------------------------- smile ---

def test_smile_interpolates_linearly_between_knots():
    s = Smile([(-0.02, 0.20), (0.0, 0.16), (0.02, 0.14)], 0.17)
    assert s(0.0) == pytest.approx(0.16)
    assert s(-0.01) == pytest.approx(0.18)
    assert s(0.01) == pytest.approx(0.15)


def test_smile_averages_duplicate_strikes():
    """A call and a put at one strike are one point. The old code fed both in and
    the interpolator silently took the lower."""
    s = Smile([(0.0, 0.10), (0.0, 0.20), (0.05, 0.15)], 0.17)
    assert s(0.0) == pytest.approx(0.15)


def test_smile_extends_the_wing_slope_rather_than_going_flat():
    """Flat wings understated put-side vol by construction, because the premium
    floor drops exactly the strikes that carry the skew."""
    pts = [(-0.04, 0.26), (-0.03, 0.24), (-0.02, 0.22), (-0.01, 0.20), (0.0, 0.18)]
    s = Smile(pts, 0.18)
    assert s(-0.06) > s(-0.04)


def test_smile_wings_are_capped_around_the_atm_level():
    pts = [(-0.04, 0.26), (-0.03, 0.24), (-0.02, 0.22), (-0.01, 0.20), (0.0, 0.18)]
    s = Smile(pts, 0.18)
    far = s(-10.0)
    assert 0.5 * s.atm <= far <= 2.0 * s.atm


def test_empty_smile_falls_back():
    s = Smile([], 0.19)
    assert s(0.0) == 0.19
    assert s(-0.5) == 0.19


def test_smile_survives_a_single_knot():
    s = Smile([(0.0, 0.16)], 0.19)
    assert s(0.0) == pytest.approx(0.16)
    assert s(-0.3) == pytest.approx(0.16)
    assert s(0.3) == pytest.approx(0.16)


def test_smile_knots_round_trip():
    s = Smile([(-0.02, 0.20), (0.0, 0.16), (0.02, 0.14)], 0.17)
    r = Smile.from_knots(s.knots(), 0.17)
    for m in (-0.03, -0.01, 0.0, 0.01, 0.03):
        assert r(m) == pytest.approx(s(m), abs=1e-6)


# ------------------------------------------------------------------ charm ---

def test_charm_matches_a_numerical_ddelta_dt():
    """charm = -dDelta/dT = phi(d1) * d2 / (2T): the delta change per YEAR of
    time passing. Checked against a central difference in T."""
    F, K, sig, T = 29000.0, 29400.0, 0.14, 0.02
    h = 1e-6
    num = -(_delta_call(F, K, sig, T + h) - _delta_call(F, K, sig, T - h)) / (2 * h)
    assert nqvol.charm(F, K, sig, T) == pytest.approx(num, rel=1e-4)


def test_charm_sign_follows_moneyness():
    """An out-of-the-money call's delta decays toward zero (charm < 0); an
    in-the-money call's climbs toward one (charm > 0)."""
    F, sig, T = 29000.0, 0.14, 0.02
    assert nqvol.charm(F, F * 1.02, sig, T) < 0
    assert nqvol.charm(F, F * 0.98, sig, T) > 0


def test_charm_is_zero_at_or_past_expiry():
    assert nqvol.charm(29000.0, 29000.0, 0.14, 0.0) == 0.0
    assert nqvol.charm(29000.0, 29000.0, 0.0, 0.02) == 0.0


def test_put_charm_equals_call_charm_without_carry():
    """With no carry a put's delta is the call's minus one, so their time
    derivatives agree and one charm serves a strike's net open interest -- the
    same identity strike_metrics relies on for gamma."""
    F, K, sig, T = 29000.0, 28700.0, 0.14, 0.02
    h = 1e-6

    def put_delta(t):
        return _delta_call(F, K, sig, t) - 1.0

    num_put = -(put_delta(T + h) - put_delta(T - h)) / (2 * h)
    assert nqvol.charm(F, K, sig, T) == pytest.approx(num_put, rel=1e-4)


def test_trinity_db_refuses_a_reference_the_chain_cannot_price(tmp_path):
    """
    The silent fallback this roll week was about: --ref NQZ6 on a chain that settles
    only NQU6 used to price every level on NQU6's forward and still label the file
    NQZ6. It must refuse instead.
    """
    import json, pytest, trinity_db
    chain = {"date": "2026-09-15", "iv_basis": "sessions.2",
             "futures_settle": {"NQU6": 28955.0, "NQU6-NQZ6": 291.75},
             "chain": {"2026-09-18|QN3U6": {"29000": {
                 "C": {"oi": 10, "iv": 0.2, "underlying": "NQU6", "px": 100.0},
                 "P": {"oi": 10, "iv": 0.2, "underlying": "NQU6", "px": 100.0}}}}}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(chain), encoding="utf-8")
    with pytest.raises(SystemExit) as ex:
        trinity_db.load(str(p), "2026-09-16", ref_symbol="NQZ6")
    msg = str(ex.value)
    assert "NQZ6" in msg and "NQU6" in msg
    assert "NQU6-NQZ6" not in msg          # spreads are not offered as references
