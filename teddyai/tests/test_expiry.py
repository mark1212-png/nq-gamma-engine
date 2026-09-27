"""
Unit tests for expiry.py.

The point of the module is that a dominance ratio depends on WHICH cancellation
you allow, so most of these build books whose cancellation behaviour is obvious
by construction and check that the two measures separate the way they should.
"""
import pytest

import expiry
from expiry import dominance, per_expiry, _bucket_of, DEFAULT_BUCKETS

from test_nodescore import book, one_book       # the same tiny fake book helper


# ---------------------------------------------------------------- buckets ---

def test_buckets_cover_every_nonnegative_dte():
    for d in range(0, 400):
        assert _bucket_of(d, DEFAULT_BUCKETS) is not None


def test_buckets_do_not_overlap():
    for d in range(0, 400):
        hits = [n for n, lo, hi in DEFAULT_BUCKETS if lo <= d <= hi]
        assert len(hits) == 1, f"dte {d} landed in {hits}"


def test_zero_and_one_dte_are_separate_buckets():
    assert _bucket_of(0, DEFAULT_BUCKETS) == "0DTE"
    assert _bucket_of(1, DEFAULT_BUCKETS) == "1DTE"


# ------------------------------------------------------------- per expiry ---

def test_per_expiry_reports_one_row_per_book():
    bs = [book({1000.0: (100, 0)}, dte=0), book({1000.0: (50, 0)}, dte=3)]
    rows = per_expiry(bs, 1000.0, 1000.0)
    assert len(rows) == 2
    assert [r["dte"] for r in rows] == [0, 3]


def test_per_expiry_rows_are_sorted_by_dte():
    bs = [book({1000.0: (10, 0)}, dte=7), book({1000.0: (10, 0)}, dte=1)]
    assert [r["dte"] for r in per_expiry(bs, 1000.0, 1000.0)] == [1, 7]


def test_gross_equals_abs_net_when_one_expiry_has_a_single_sign():
    b = one_book({990.0: (50, 0), 1000.0: (80, 0), 1010.0: (30, 0)}, dte=0)
    r = per_expiry(b, 1000.0, 1000.0)[0]
    assert r["gamma_gross"] == pytest.approx(abs(r["gamma_net"]), rel=1e-9)


def test_gross_exceeds_abs_net_when_an_expiry_offsets_internally():
    """The level-2 cancellation the module exists to expose: one expiry holding a
    call wall and a put wall nets to nearly nothing but plainly has structure."""
    b = one_book({990.0: (0, 100), 1010.0: (100, 0)}, dte=0)
    r = per_expiry(b, 1000.0, 1000.0)[0]
    assert abs(r["gamma_net"]) < 0.2 * r["gamma_gross"]
    assert r["gamma_gross"] > 0


# --------------------------------------------------------------- shares ---

def test_shares_sum_to_one():
    bs = [book({1000.0: (100, 0)}, dte=0), book({1000.0: (60, 0)}, dte=3),
          book({1000.0: (40, 0)}, dte=12)]
    d = dominance(bs, 1000.0, 1000.0)
    tot = sum(v["gamma_share"] for v in d["buckets"].values() if v["gamma_share"])
    assert tot == pytest.approx(1.0, abs=1e-3)


def test_gross_shares_sum_to_one():
    bs = [book({1000.0: (100, 0)}, dte=0), book({990.0: (0, 60)}, dte=3)]
    d = dominance(bs, 1000.0, 1000.0)
    tot = sum(v["gamma_gross_share"] for v in d["buckets"].values() if v["gamma_gross_share"])
    assert tot == pytest.approx(1.0, abs=1e-3)


def test_opposite_signed_expiries_cannot_cancel_in_the_share():
    """The level-3 protection Mark's brief asked for. Two equal and opposite
    expiries net to zero, but each carries half the board and the share says so."""
    bs = [book({1000.0: (100, 0)}, dte=0), book({1000.0: (0, 100)}, dte=3)]
    d = dominance(bs, 1000.0, 1000.0)
    assert d["totals"]["gamma_signed"] == pytest.approx(0.0, abs=1e-6)
    assert d["buckets"]["0DTE"]["gamma_share"] == pytest.approx(0.5, abs=1e-3)
    assert d["buckets"]["2-5"]["gamma_share"] == pytest.approx(0.5, abs=1e-3)


def test_signed_over_abs_detects_an_offsetting_board():
    bs = [book({1000.0: (100, 0)}, dte=0), book({1000.0: (0, 100)}, dte=3)]
    d = dominance(bs, 1000.0, 1000.0)
    assert d["totals"]["signed_over_abs"] == pytest.approx(0.0, abs=1e-6)


def test_signed_over_abs_is_one_when_every_expiry_agrees():
    bs = [book({1000.0: (100, 0)}, dte=0), book({1000.0: (60, 0)}, dte=3)]
    d = dominance(bs, 1000.0, 1000.0)
    assert d["totals"]["signed_over_abs"] == pytest.approx(1.0, abs=1e-6)


def test_abs_over_gross_detects_cancellation_inside_expiries():
    inner = [book({990.0: (0, 100), 1010.0: (100, 0)}, dte=0)]
    outer = [book({1000.0: (100, 0)}, dte=0)]
    assert dominance(inner, 1000.0, 1000.0)["totals"]["abs_over_gross"] < 0.2
    assert dominance(outer, 1000.0, 1000.0)["totals"]["abs_over_gross"] == pytest.approx(1.0, abs=1e-6)


def test_the_two_dominance_measures_can_disagree():
    """They disagree exactly when an expiry is internally offsetting, which is the
    reason both are reported rather than one being chosen."""
    bs = [book({990.0: (0, 100), 1010.0: (100, 0)}, dte=0),   # big structure, ~zero net
          book({1000.0: (60, 0)}, dte=3)]                      # small structure, all net
    d = dominance(bs, 1000.0, 1000.0)
    z = d["buckets"]["0DTE"]
    assert z["gamma_gross_share"] > z["gamma_share"]


# ------------------------------------------------------------ zero DTE ---

def test_zero_dte_headline_is_populated_when_a_book_expires_today():
    bs = [book({1000.0: (100, 0)}, dte=0), book({1000.0: (60, 0)}, dte=3)]
    z = dominance(bs, 1000.0, 1000.0)["zero_dte"]
    assert z["present"] is True
    assert z["roots"] == ["TEST"]
    assert 0.0 <= z["share"] <= 1.0


def test_no_zero_dte_says_empty_not_flat():
    """A board with nothing expiring today has an EMPTY 0DTE bucket. Reporting
    that as zero gamma would say dealers are neutral, which is a different and
    false claim -- the same distinction trinity_db draws with `no_0dte`."""
    bs = [book({1000.0: (100, 0)}, dte=2)]
    z = dominance(bs, 1000.0, 1000.0)["zero_dte"]
    assert z["present"] is False
    assert "EMPTY" in z["note"]


# --------------------------------------------------------- partition ---

def test_the_confluence_partition_is_reported_alongside():
    """trinity_db scores confluence on 0 / 1-9 / 10+. Those buckets are reported
    unchanged so the new convention sits beside the old rather than replacing it."""
    bs = [book({1000.0: (100, 0)}, dte=0), book({1000.0: (60, 0)}, dte=5),
          book({1000.0: (40, 0)}, dte=30)]
    p = dominance(bs, 1000.0, 1000.0)["partition"]
    assert set(p) == {"0DTE", "1-9", "10+"}
    assert p["1-9"]["expiries"] == 1
    assert p["10+"]["expiries"] == 1


# ------------------------------------------------------------ edge cases ---

def test_an_empty_book_list_is_handled():
    d = dominance([], 1000.0, 1000.0)
    assert d["by_expiry"] == []
    assert "empty book" in d["note"]


def test_a_book_with_no_exposure_does_not_divide_by_zero():
    d = dominance(one_book({1000.0: (50, 50)}, dte=0), 1000.0, 1000.0)
    assert d["totals"]["signed_over_abs"] is None or d["totals"]["gamma_gross"] == 0


def test_a_single_expiry_takes_the_whole_share():
    d = dominance(one_book({1000.0: (100, 0)}, dte=0), 1000.0, 1000.0)
    assert d["buckets"]["0DTE"]["gamma_share"] == pytest.approx(1.0)
    assert d["buckets"]["0DTE"]["gamma_gross_share"] == pytest.approx(1.0)


def test_open_interest_shares_are_reported_and_never_negative():
    bs = [book({1000.0: (100, 20)}, dte=0), book({1000.0: (10, 90)}, dte=4)]
    d = dominance(bs, 1000.0, 1000.0)
    tot = 0.0
    for v in d["buckets"].values():
        if v["oi_share"] is not None:
            assert v["oi_share"] >= 0.0
            tot += v["oi_share"]
    assert tot == pytest.approx(1.0, abs=1e-3)


def test_charm_is_documented_as_absent_rather_than_silently_missing():
    d = dominance(one_book({1000.0: (100, 0)}, dte=0), 1000.0, 1000.0)
    assert "charm" in d
    assert "worse than chance" in d["charm"]


def test_vanna_dominance_is_reported_separately_from_gamma():
    bs = [book({1000.0: (100, 0)}, dte=0), book({1100.0: (100, 0)}, dte=5)]
    d = dominance(bs, 1000.0, 1000.0)
    for v in d["buckets"].values():
        assert "vanna_share" in v and "vanna_gross_share" in v
