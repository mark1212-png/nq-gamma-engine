"""
Unit tests for dealer_flow.py -- the flow-based dealer-sign measurement.

Nothing here touches the network or a chain file. The fixtures are prints and
open-interest pairs small enough to reconcile by hand.
"""
import pytest

import dealer_flow
from dealer_flow import classify_prints, reconcile, apply, summary, ASSUMPTION


# -------------------------------------------------------------- classify ---

def test_a_side_field_is_taken_as_given():
    net, unk = classify_prints([{"sym": "A C1", "price": 1.0, "size": 5, "side": "B"},
                                {"sym": "A C1", "price": 1.0, "size": 2, "side": "A"}])
    assert net["A C1"] == 3 and unk == 0


def test_a_print_is_placed_against_the_quote():
    net, _ = classify_prints([{"sym": "A C1", "price": 1.10, "size": 4, "bid": 1.0, "ask": 1.10},
                              {"sym": "A C1", "price": 1.00, "size": 1, "bid": 1.0, "ask": 1.10}])
    assert net["A C1"] == 3


def test_an_inside_spread_print_takes_the_last_tick():
    """Lee-Ready: up-tick = lifted, down-tick = hit, unchanged inherits."""
    net, _ = classify_prints([{"sym": "A C1", "price": 1.00, "size": 1, "side": "B"},
                              {"sym": "A C1", "price": 1.05, "size": 3, "bid": 1.0, "ask": 1.10},   # up: buy
                              {"sym": "A C1", "price": 1.05, "size": 2, "bid": 1.0, "ask": 1.10},   # unchanged: inherits buy
                              {"sym": "A C1", "price": 1.02, "size": 4, "bid": 1.0, "ask": 1.10}])  # down: sell
    assert net["A C1"] == 1 + 3 + 2 - 4


def test_the_first_print_with_nothing_to_place_it_stays_unplaced():
    net, unk = classify_prints([{"sym": "A C1", "price": 1.0, "size": 9}])
    assert "A C1" not in net and unk == 1


# -------------------------------------------------------------- reconcile ---

def test_a_contract_first_seen_with_oi_starts_from_the_convention():
    pos = reconcile({"A C1": 100}, {"A C1": 100}, {}, None, "2026-09-11")
    assert pos["A C1"]["pos"] == 100 and pos["A C1"]["source"] == "convention-init"
    pos = reconcile({"A P1": 100}, {"A P1": 100}, {}, None, "2026-09-11")
    assert pos["A P1"]["pos"] == -100


def test_customer_buying_makes_dealers_shorter():
    pos = reconcile({"A C1": 0}, {"A C1": 40}, {"A C1": 40.0}, None, "2026-09-11")
    assert pos["A C1"]["pos"] == -40 and pos["A C1"]["flow_share"] == 1.0


def test_customer_selling_makes_dealers_longer():
    pos = reconcile({"A P1": 0}, {"A P1": 40}, {"A P1": -40.0}, None, "2026-09-11")
    assert pos["A P1"]["pos"] == 40


def test_the_flow_share_rule_assigns_only_what_the_flow_covers():
    """The one change from the source: 5 net contracts of flow against 30 new OI
    moves 5 the flow's way and 25 by convention, not all 30."""
    pos = reconcile({"A C1": 100}, {"A C1": 130}, {"A C1": 5.0}, None, "2026-09-11")
    assert pos["A C1"]["pos"] == 100 - 5 + 25
    assert pos["A C1"]["measured_contracts"] == 5
    assert pos["A C1"]["flow_share"] == pytest.approx(5 / 130, abs=1e-3)


def test_with_enough_flow_the_source_rule_is_recovered():
    pos = reconcile({"A C1": 100}, {"A C1": 130}, {"A C1": 900.0}, None, "2026-09-11")
    assert pos["A C1"]["pos"] == 100 - 30


def test_closing_flow_unwinds_the_right_side():
    """OI falls 20 with customers net buying: they bought to close shorts, so
    dealers were long those and are less long now."""
    inv = reconcile({"A C1": 100}, {"A C1": 100}, {}, None, "2026-09-10")
    pos = reconcile({"A C1": 100}, {"A C1": 80}, {"A C1": 20.0}, inv, "2026-09-11")
    assert pos["A C1"]["pos"] == 80


def test_a_session_is_never_applied_twice():
    a = reconcile({"A C1": 100}, {"A C1": 130}, {"A C1": 30.0}, None, "2026-09-11")
    b = reconcile({"A C1": 100}, {"A C1": 130}, {"A C1": 30.0}, a, "2026-09-11")
    assert b["A C1"]["pos"] == a["A C1"]["pos"]


def test_a_position_is_clamped_to_the_contracts_that_exist():
    pos = reconcile({"A C1": 0}, {"A C1": 10}, {"A C1": 10.0}, None, "2026-09-11")
    assert abs(pos["A C1"]["pos"]) <= 10


def test_every_record_states_the_assumption():
    pos = reconcile({"A C1": 1}, {"A C1": 1}, {}, None, "2026-09-11")
    assert pos["A C1"]["assumption"] == ASSUMPTION
    assert "aggressor = customer" in ASSUMPTION


# ------------------------------------------------------------------ apply ---

def _books():
    return [{"root": "A", "strikes": {"100.0": {"C": {"oi": 30}, "P": {"oi": 10}},
                                      "110.0": {"C": {"oi": 7}, "P": {"oi": 0}}}}]


def test_apply_stamps_the_signed_position_in_memory():
    books = _books()
    n, covered = apply(books, {"A C100": {"pos": -30, "oi": 30, "source": "flow", "measured_contracts": 30},
                               "A P100": {"pos": -10, "oi": 10, "source": "flow", "measured_contracts": 10}})
    assert n == 1 and covered == 40
    assert books[0]["strikes"]["100.0"]["pos"]["dealer"] == -40
    assert "pos" not in books[0]["strikes"]["110.0"]


def test_a_strike_with_one_leg_measured_keeps_the_convention_on_the_other():
    books = _books()
    apply(books, {"A C100": {"pos": -30, "oi": 30, "source": "flow", "measured_contracts": 30}})
    assert books[0]["strikes"]["100.0"]["pos"]["dealer"] == -30 - 10   # puts stay dealer-short


def test_the_stamp_feeds_the_exposure_engine():
    import trinity_db
    books = _books()
    apply(books, {"A C100": {"pos": -30, "oi": 30, "source": "flow", "measured_contracts": 30},
                  "A P100": {"pos": -10, "oi": 10, "source": "flow", "measured_contracts": 10}})
    assert trinity_db.net_position(books[0]["strikes"]["100.0"]) == -40
    assert trinity_db.net_position(books[0]["strikes"]["110.0"]) == 7


# ---------------------------------------------------------------- summary ---

def test_the_summary_counts_signs_opposite_to_the_convention():
    pos = {"A C1": {"pos": -5, "oi": 5, "source": "flow", "measured_contracts": 5},
           "A P1": {"pos": -5, "oi": 5, "source": "flow", "measured_contracts": 5}}
    s = summary(pos)
    assert s["sign_opposite_to_convention"] == 1 and s["share_opposite"] == 0.5
    assert s["oi_share_measured"] == 1.0


def test_the_selftest_passes():
    dealer_flow.selftest()
