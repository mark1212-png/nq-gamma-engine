"""
Unit tests for nodescore.py.

The scorer needs a book to run, so these build the smallest object that
trinity_db.strike_metrics will accept -- a fake book with a flat smile and a
handful of strikes -- rather than reading a cached chain. That keeps every
assertion checkable by hand and keeps the suite fast.

What is tested: normalisation, sign handling, distance, the explicit exclusions,
component storage, and the edge cases Mark listed.
"""
import math

import pytest

import nodescore
from nodescore import Config, Weights, score_nodes, oi_by_strike, persistence_by_strike


class FlatSmile:
    """A smile that returns one vol everywhere, so gamma depends only on the
    strike's distance from the forward and nothing is hidden in the vol curve."""
    def __init__(self, iv=0.15):
        self.iv = iv
        self.x = []
        self.y = []
        self.atm = iv

    def __call__(self, m):
        return self.iv


def book(oi_map, F=1000.0, T=20.0 / 252.0, dte=0, root="TEST", basis=0.0, grid=25.0):
    """oi_map: {strike: (call_oi, put_oi)}.

    T defaults to 20 sessions, not one. On a ONE-session book with sigma 0.15 a
    strike 4% out carries 0.02% of the at-the-money gamma, so every fixture
    strike away from the forward would be filtered out before it was scored and
    the test would be measuring the gamma decay rather than the scorer. At 20
    sessions a 5% strike still carries 53% of the at-the-money gamma, which is
    the regime the scorer is actually used in.
    """
    strikes = {}
    for k, (c, p) in oi_map.items():
        rec = {}
        if c:
            rec["C"] = {"oi": c}
        if p:
            rec["P"] = {"oi": p}
        strikes[str(float(k))] = rec
    return {"root": root, "day": "2026-09-09", "dte": dte, "F": F, "T": T,
            "und": "TEST", "basis": basis, "smile": FlatSmile(), "atm": 0.15,
            "strikes": strikes, "grid": grid, "cap": 0.25}


def one_book(oi_map, **kw):
    return [book(oi_map, **kw)]


# --------------------------------------------------------------- plumbing ---

def test_oi_by_strike_sums_calls_and_puts():
    b = one_book({1000.0: (10, 5), 1025.0: (0, 7)})
    assert oi_by_strike(b) == {1000.0: 15, 1025.0: 7}


def test_oi_by_strike_shifts_a_foreign_book_onto_the_reference_axis():
    """A book on another underlying must land where its HEDGE does, or the open
    interest and the gamma would key on different prices."""
    b = one_book({1100.0: (10, 0)}, basis=100.0)
    assert oi_by_strike(b) == {1000.0: 10}


def test_oi_by_strike_skips_empty_strikes():
    b = one_book({1000.0: (0, 0), 1025.0: (3, 0)})
    assert oi_by_strike(b) == {1025.0: 3}


def test_persistence_counts_books_not_strikes():
    bs = [book({1000.0: (50, 0), 1200.0: (1, 0)}, dte=0),
          book({1000.0: (50, 0)}, dte=1)]
    per, n = persistence_by_strike(bs, 1000.0, 1000.0)
    assert n == 2
    assert per[1000.0] == pytest.approx(1.0)


# ------------------------------------------------------------- normalising ---

def test_every_component_and_the_score_are_bounded():
    b = one_book({980.0: (0, 40), 1000.0: (120, 0), 1025.0: (30, 0), 1100.0: (5, 0)})
    r = score_nodes(b, spot=1000.0, F_ref=1000.0, em=50.0)
    assert r["nodes"]
    for n in r["nodes"]:
        assert 0.0 <= n["quality"] <= 1.0
        for name, val in n["components"].items():
            assert 0.0 <= val <= 1.0, f"{name} out of range: {val}"


def test_score_is_the_weighted_mean_of_its_components():
    """The score must be reconstructable from what is stored beside it, or the
    engine cannot explain a rank."""
    b = one_book({980.0: (0, 40), 1000.0: (120, 0), 1025.0: (30, 0)})
    w = Weights(gamma=2.0, oi=1.0, proximity=1.0, persistence=1.0, structure=1.0)
    r = score_nodes(b, spot=1000.0, F_ref=1000.0, em=50.0, weights=w)
    wd = w.as_dict()
    for n in r["nodes"]:
        want = sum(n["components"][k] * wd[k] for k in n["components"]) / w.total()
        assert n["quality"] == pytest.approx(want, abs=5e-4)


def test_scores_are_invariant_to_a_uniform_rescaling_of_the_book():
    """Ten times the open interest at every strike is the same SHAPE, so the
    ranking must not move. This is what normalising against the book has to mean."""
    small = one_book({980.0: (0, 40), 1000.0: (120, 0), 1025.0: (30, 0)})
    big = one_book({980.0: (0, 400), 1000.0: (1200, 0), 1025.0: (300, 0)})
    a = score_nodes(small, spot=1000.0, F_ref=1000.0, em=50.0)
    b = score_nodes(big, spot=1000.0, F_ref=1000.0, em=50.0)
    assert [n["price"] for n in a["nodes"]] == [n["price"] for n in b["nodes"]]
    for x, y in zip(a["nodes"], b["nodes"]):
        assert x["quality"] == pytest.approx(y["quality"], abs=1e-9)


def test_an_extreme_outlier_does_not_flatten_the_other_nodes():
    """One absurd line must not push every genuine strike under the threshold.

    METHOD.md section 6 records the real version: 29,500 read as a major call
    wall on +142 net gamma of which almost all was ONE 1,838-lot line.

    THE ROBUSTNESS IS ASYMPTOTIC IN THE NUMBER OF STRIKES and the fixture
    reflects that honestly. p95 can only discard an outlier once the top 5% of
    the sample contains it, which needs about 21 points; below that any high
    quantile interpolates into the outlier and there is no threshold rule that
    can tell "one huge line" from "a book with one strike in it". On real books
    the quantile runs over every exposed strike in the window -- 115 on the
    2026-09-08 book -- so the top six are discarded and the margin is wide.
    """
    oi = {950.0 + 5 * i: (40 + (i % 5), 0) for i in range(25)}
    oi[1030.0] = (1000000, 0)
    r = score_nodes(one_book(oi), spot=1000.0, F_ref=1000.0, em=80.0)
    ordinary = [n for n in r["nodes"] if n["price"] != 1030.0]
    assert ordinary, "the outlier flattened every other node"
    assert max(n["components"]["gamma"] for n in ordinary) > 0.5


def test_dominance_is_reported_so_a_single_line_is_visible():
    """When one strike carries most of the window's exposure the caller is told,
    because that is the situation METHOD.md warns produces a phantom wall."""
    oi = {950.0 + 5 * i: (40, 0) for i in range(25)}
    oi[1030.0] = (1000000, 0)
    r = score_nodes(one_book(oi), spot=1000.0, F_ref=1000.0, em=80.0)
    assert r["dominance"]["share"] > 0.9
    assert r["dominance"]["price"] == 1030.0

    even = {950.0 + 5 * i: (40, 0) for i in range(25)}
    r2 = score_nodes(one_book(even), spot=1000.0, F_ref=1000.0, em=80.0)
    assert r2["dominance"]["share"] < 0.3


# ------------------------------------------------------------------- sign ---

def test_sign_is_reported_but_never_scored():
    """A big positive node and a big negative node of equal size must rank
    equally. Turning 'positive gamma' into 'support' needs evidence this layer
    does not have."""
    pos = one_book({1000.0: (100, 0)})
    neg = one_book({1000.0: (0, 100)})
    a = score_nodes(pos, spot=1000.0, F_ref=1000.0, em=50.0)["nodes"][0]
    b = score_nodes(neg, spot=1000.0, F_ref=1000.0, em=50.0)["nodes"][0]
    assert a["sign"] == 1 and b["sign"] == -1
    assert a["quality"] == pytest.approx(b["quality"])


def test_no_node_carries_a_directional_field():
    b = one_book({980.0: (0, 40), 1000.0: (120, 0)})
    r = score_nodes(b, spot=1000.0, F_ref=1000.0, em=50.0)
    banned = {"direction", "bias", "bullish", "bearish", "support", "resistance", "signal"}
    for n in r["nodes"]:
        assert banned.isdisjoint(n.keys())
        assert banned.isdisjoint(n["components"].keys())


# -------------------------------------------------------------- proximity ---

def test_proximity_falls_with_distance():
    b = one_book({1000.0: (100, 0), 1020.0: (100, 0), 1040.0: (100, 0)})
    r = score_nodes(b, spot=1000.0, F_ref=1000.0, em=100.0)
    by = {n["price"]: n["components"]["proximity"] for n in r["nodes"]}
    assert by[1000.0] > by[1020.0] > by[1040.0]


def test_proximity_is_one_at_the_reference():
    b = one_book({1000.0: (100, 0)})
    n = score_nodes(b, spot=1000.0, F_ref=1000.0, em=50.0)["nodes"][0]
    assert n["components"]["proximity"] == pytest.approx(1.0)


def test_distance_is_reported_in_points_and_expected_moves():
    b = one_book({1040.0: (100, 0), 1000.0: (100, 0)})
    r = score_nodes(b, spot=1000.0, F_ref=1000.0, em=20.0)
    n = [x for x in r["nodes"] if x["price"] == 1040.0][0]
    assert n["raw"]["distance"] == pytest.approx(40.0)
    assert n["raw"]["distance_ems"] == pytest.approx(2.0)


# ------------------------------------------------------------- exclusions ---

def test_vanna_is_a_component_and_still_reported_raw():
    """By decision on 2026-09-12. Scored as a magnitude in [0, 1]; the raw signed
    value stays in `raw`, so the VEX reading is unchanged."""
    b = one_book({980.0: (0, 40), 1000.0: (120, 0), 1020.0: (30, 0)})
    r = score_nodes(b, spot=1000.0, F_ref=1000.0, em=50.0)
    for n in r["nodes"]:
        assert "vanna" in n["raw"]
        assert 0.0 <= n["components"]["vanna"] <= 1.0
    assert "vanna" not in r["excluded"]
    assert "vanna" in r["added_by_decision"]


def test_charm_is_a_component_and_reported_raw_per_session():
    b = one_book({980.0: (0, 40), 1000.0: (120, 0), 1020.0: (30, 0)})
    r = score_nodes(b, spot=1000.0, F_ref=1000.0, em=50.0)
    assert "charm" not in r["excluded"]
    assert "charm" in r["added_by_decision"]
    assert r["scales"]["charm"] > 0
    for n in r["nodes"]:
        assert "charm" in n["raw"]
        assert 0.0 <= n["components"]["charm"] <= 1.0


def test_cluster_is_still_excluded():
    b = one_book({1000.0: (100, 0)})
    r = score_nodes(b, spot=1000.0, F_ref=1000.0, em=50.0)
    assert "cluster" in r["excluded"]
    for n in r["nodes"]:
        assert "cluster" not in n["components"]


def test_zero_weights_on_vanna_and_charm_restore_the_five_component_score():
    """The way back is one line, and it must be exact."""
    b = one_book({980.0: (0, 40), 1000.0: (120, 0), 1025.0: (30, 0)})
    r = score_nodes(b, spot=1000.0, F_ref=1000.0, em=50.0,
                    weights=Weights(vanna=0.0, charm=0.0))
    five = ("gamma", "oi", "proximity", "persistence", "structure")
    for n in r["nodes"]:
        want = sum(n["components"][k] for k in five) / 5.0
        assert n["quality"] == pytest.approx(want, abs=5e-4)


def test_the_measurement_says_which_score_it_measured():
    b = one_book({1000.0: (100, 0)})
    r = score_nodes(b, spot=1000.0, F_ref=1000.0, em=50.0)
    assert "five-component" in r["measured"]["scored_with"]
    assert "unmeasured" in r["measured"]["scored_with"]


def test_strike_charm_keys_match_strike_metrics_and_signs_follow_moneyness():
    """Long out-of-the-money calls lose delta as time passes (charm < 0); long
    in-the-money calls gain it (charm > 0). Keyed on the same axis as gamma."""
    import trinity_db
    b = one_book({980.0: (100, 0), 1000.0: (100, 0), 1020.0: (100, 0)})
    g, _ = trinity_db.strike_metrics(b)
    c = trinity_db.strike_charm(b)
    assert set(c) == set(g)
    assert c[1020.0] < 0 < c[980.0]


def test_the_weights_declare_themselves_unvalidated():
    b = one_book({1000.0: (100, 0)})
    r = score_nodes(b, spot=1000.0, F_ref=1000.0, em=50.0)
    assert r["weights_validated"] is False
    assert "not a probability of holding" in r["score_note"]
    assert "fitting to noise" in r["weights_note"]


def test_default_weights_are_equal():
    w = Weights()
    assert len(set(w.as_dict().values())) == 1


# --------------------------------------------------------------- evidence ---

def test_every_node_explains_itself():
    b = one_book({980.0: (0, 40), 1000.0: (120, 0)})
    r = score_nodes(b, spot=1000.0, F_ref=1000.0, em=50.0)
    for n in r["nodes"]:
        assert n["evidence"]
        assert any("net gamma" in e for e in n["evidence"])
        assert any("strongest component" in e for e in n["evidence"])


def test_components_are_stored_alongside_the_score():
    b = one_book({1000.0: (100, 0)})
    n = score_nodes(b, spot=1000.0, F_ref=1000.0, em=50.0)["nodes"][0]
    assert set(n["components"]) == {"gamma", "oi", "proximity", "persistence", "structure",
                                    "vanna", "charm"}


# ------------------------------------------------------------ ordering ---

def test_nodes_are_returned_best_first():
    b = one_book({980.0: (30, 0), 1000.0: (200, 0), 1020.0: (60, 0)})
    r = score_nodes(b, spot=1000.0, F_ref=1000.0, em=100.0)
    q = [n["quality"] for n in r["nodes"]]
    assert q == sorted(q, reverse=True)


def test_top_n_truncates_after_ranking():
    b = one_book({980.0: (120, 0), 990.0: (140, 0), 1000.0: (200, 0), 1010.0: (160, 0)})
    full = score_nodes(b, spot=1000.0, F_ref=1000.0, em=100.0)
    cut = score_nodes(b, spot=1000.0, F_ref=1000.0, em=100.0, top_n=2)
    assert len(cut["nodes"]) == 2
    assert [n["price"] for n in cut["nodes"]] == [n["price"] for n in full["nodes"]][:2]


# ------------------------------------------------------------ edge cases ---

def test_an_empty_book_list_is_handled():
    r = score_nodes([], spot=1000.0, F_ref=1000.0, em=50.0)
    assert r["nodes"] == []
    assert "empty book" in r["note"]


def test_a_book_with_no_open_interest_is_handled():
    r = score_nodes(one_book({1000.0: (0, 0)}), spot=1000.0, F_ref=1000.0, em=50.0)
    assert r["nodes"] == []


def test_a_single_strike_scores():
    r = score_nodes(one_book({1000.0: (100, 0)}), spot=1000.0, F_ref=1000.0, em=50.0)
    assert len(r["nodes"]) == 1
    assert r["nodes"][0]["price"] == 1000.0


def test_a_perfectly_offsetting_strike_is_not_a_node():
    """Equal call and put open interest nets to zero gamma. It carries open
    interest but no exposure, and must not be ranked as structure."""
    r = score_nodes(one_book({1000.0: (50, 50)}), spot=1000.0, F_ref=1000.0, em=50.0)
    assert r["nodes"] == []


def test_spot_far_outside_the_book_yields_no_candidates():
    r = score_nodes(one_book({1000.0: (100, 0)}), spot=50000.0, F_ref=50000.0, em=50.0)
    assert r["nodes"] == []


def test_a_zero_expected_move_does_not_divide_by_zero():
    r = score_nodes(one_book({1000.0: (100, 0)}), spot=1000.0, F_ref=1000.0, em=0.0)
    for n in r["nodes"]:
        assert n["components"]["proximity"] == 0.0
        assert n["raw"]["distance_ems"] is None


def test_mixed_sign_nodes_are_both_ranked():
    b = one_book({980.0: (0, 100), 1020.0: (100, 0)})
    r = score_nodes(b, spot=1000.0, F_ref=1000.0, em=100.0)
    assert {n["sign"] for n in r["nodes"]} == {1, -1}


def test_config_material_threshold_filters_candidates():
    b = one_book({1000.0: (100, 0), 1010.0: (2, 0)})
    loose = score_nodes(b, spot=1000.0, F_ref=1000.0, em=100.0, material=0.001)
    tight = score_nodes(b, spot=1000.0, F_ref=1000.0, em=100.0, material=0.9)
    assert len(loose["nodes"]) >= len(tight["nodes"])
    assert len(tight["nodes"]) == 1


# ------------------------------------- the measurement, carried in the code ---

def test_the_measured_null_result_travels_with_every_score():
    """500 sessions, 4,556 touches: this score does NOT rank reaction probability.
    That finding must ride along with the score, or a future reader will treat a
    0.88 as a hold rate. Full write-up in
    Claude outputs/RESULTS-nodes-500-sessions.md."""
    r = score_nodes(one_book({1000.0: (100, 0)}), spot=1000.0, F_ref=1000.0, em=50.0)
    m = r["measured"]
    assert m["sessions"] == 500
    assert m["node_reaction_rate"] == pytest.approx(0.568)
    assert m["placebo_reaction_rate"] == pytest.approx(0.570)
    assert "NO EDGE" in m["verdict"]
    assert "not where price turns" in m["verdict"]


def test_the_quality_buckets_are_recorded_as_non_monotone():
    r = score_nodes(one_book({1000.0: (100, 0)}), spot=1000.0, F_ref=1000.0, em=50.0)
    b = r["measured"]["by_quality_bucket"]
    # q1_low beats q2 and q3: a score that ranked reactions would not do this
    assert b["q1_low"] > b["q2"] > b["q3"]
    assert not (b["q1_low"] < b["q2"] < b["q3"] < b["q4_high"])


def test_the_score_note_no_longer_claims_an_edge():
    r = score_nodes(one_book({1000.0: (100, 0)}), spot=1000.0, F_ref=1000.0, em=50.0)
    assert "does NOT" in r["score_note"]
    assert "ATTENTION" in r["score_note"]
