"""
Unit tests for topology.py.

Every fixture here is small enough to check by hand. The point of the module is
to describe shape, so the tests build a shape whose answer is known and assert
the module finds it -- not to snapshot a big JSON blob, which would pass while
being wrong.

The edge cases are the list Mark asked for: zero exposure, a single strike,
duplicate strikes, sparse grids, negative gamma, mixed sign, a zero denominator,
an extreme outlier, and a spot outside the modelled strike range.
"""
import math

import pytest

import topology
from topology import Config, bin_exposure, strike_topology, spot_profile


def _kinds(t, kind):
    return [f for f in t["features"] if f["kind"] == kind]


def _labelled(t, label):
    return [f for f in t["features"] if label in f["labels"]]


# ------------------------------------------------------------- binning ---

def test_binning_preserves_total_mass():
    """The whole reason exposure is summed rather than interpolated."""
    g = {100.0: 3.0, 110.0: -2.0, 125.0: 7.5, 180.0: 1.25}
    _, vals, _ = bin_exposure(g, 100.0, 200.0, 25.0)
    assert sum(vals) == pytest.approx(3.0 - 2.0 + 7.5 + 1.25)


def test_binning_groups_by_price_not_by_index():
    g = {100.0: 1.0, 110.0: 1.0, 120.0: 1.0, 130.0: 1.0}
    centers, vals, cnts = bin_exposure(g, 100.0, 150.0, 25.0)
    assert vals[0] == pytest.approx(3.0)     # 100, 110, 120
    assert vals[1] == pytest.approx(1.0)     # 130
    assert cnts == [3, 1]


def test_binning_counts_strikes_so_evidence_is_distinguishable():
    """One strike of 60 and six strikes of 10 both mass 60. They are different
    kinds of evidence and the caller has to be able to tell them apart."""
    a = {500.0: 60.0}
    b = {500.0 + 3 * i: 10.0 for i in range(6)}
    _, va, ca = bin_exposure(a, 490.0, 540.0, 25.0)
    _, vb, cb = bin_exposure(b, 490.0, 540.0, 25.0)
    assert sum(va) == pytest.approx(sum(vb))
    assert sum(ca) == 1 and sum(cb) == 6


def test_binning_excludes_strikes_outside_the_window():
    g = {50.0: 99.0, 150.0: 1.0}
    _, vals, cnts = bin_exposure(g, 100.0, 200.0, 25.0)
    assert sum(vals) == pytest.approx(1.0)
    assert sum(cnts) == 1


def test_binning_rejects_a_nonpositive_width():
    with pytest.raises(ValueError):
        bin_exposure({1.0: 1.0}, 0.0, 10.0, 0.0)


def test_duplicate_strikes_cannot_occur_but_colocated_ones_add():
    """A dict cannot carry a duplicate key, so the engine's own maps are unique.
    Two strikes inside one bin must ADD rather than one overwriting the other."""
    g = {29500.0: 10.0, 29505.0: 15.0, 29510.0: -5.0}
    _, vals, cnts = bin_exposure(g, 29500.0, 29525.0, 25.0)
    assert vals[0] == pytest.approx(20.0)
    assert cnts[0] == 3


# ------------------------------------------------------ wall and shelf ---

def _wall_book():
    """One tall spike at 1,000 against a flat low background. The scale is driven
    by the spike, so the spike is the wall and nothing else is material.

    Note 1,000 is itself one of the background strikes, so the assignment
    REPLACES that 1.0 rather than adding to it: the bin holds 100.0, not 101.0.
    """
    g = {800.0 + 25 * i: 1.0 for i in range(17)}      # 800..1200 background
    g[1000.0] = 100.0
    return g


def test_a_tall_spike_is_a_wall():
    t = strike_topology(_wall_book(), spot=1000.0, em=100.0,
                        cfg=Config(bin_width=25.0, window_min=250.0))
    walls = _kinds(t, "wall")
    assert len(walls) == 1
    w = walls[0]
    assert w["lower"] <= 1000.0 <= w["upper"]
    assert w["sign"] == 1
    assert w["peak"] == pytest.approx(100.0)
    assert w["n_strikes"] == 1


def test_a_broad_moderate_run_is_a_shelf_not_a_wall():
    """Same total mass as a wall, spread over eight bins. Shape, not size, is
    what separates the two."""
    g = {}
    for i in range(8):
        g[1000.0 + 25 * i] = 12.0
    g[1400.0] = 40.0                                   # sets the scale so 12s are mid-band
    t = strike_topology(g, spot=1100.0, em=200.0,
                        cfg=Config(bin_width=25.0, window_min=450.0))
    shelves = _kinds(t, "shelf")
    assert shelves, "a broad even run should be reported as a shelf"
    s = max(shelves, key=lambda f: f["n_bins"])
    assert s["n_bins"] >= 6
    assert s["concentration"] < 0.35                   # no single bin dominates


def test_a_multi_bin_wall_is_labelled_a_cluster():
    g = {1000.0: 50.0, 1025.0: 45.0, 1050.0: 48.0}
    g.update({700.0 + 25 * i: 0.5 for i in range(10)})
    t = strike_topology(g, spot=1000.0, em=200.0,
                        cfg=Config(bin_width=25.0, window_min=400.0))
    clusters = _labelled(t, "cluster")
    assert clusters, "three adjacent heavy strikes are one wall, labelled a cluster"
    assert clusters[0]["n_bins"] >= 2


def test_an_isolated_wall_is_labelled():
    g = {1000.0: 40.0}                                 # the only structure at all
    g.update({1400.0 + 25 * i: 40.0 for i in range(3)})
    t = strike_topology(g, spot=1000.0, em=400.0,
                        cfg=Config(bin_width=25.0, window_min=600.0, isolated_gap=50.0))
    iso = _labelled(t, "isolated_node")
    assert any(abs(f["center"] - 1012.5) < 30 for f in iso)


def test_two_adjacent_heavy_strikes_are_not_isolated():
    g = {1000.0: 40.0, 1025.0: 38.0}
    t = strike_topology(g, spot=1000.0, em=200.0,
                        cfg=Config(bin_width=25.0, window_min=400.0, isolated_gap=50.0))
    walls = _kinds(t, "wall")
    assert walls
    # they merge into ONE run, so there is no second structure to be isolated from
    assert len(walls) == 1


# ---------------------------------------------------- pocket and vacuum ---

def test_a_gap_between_two_walls_is_a_pocket():
    g = {1000.0: 50.0}
    g.update({k: 0.0 for k in (1025.0, 1050.0, 1075.0)})
    g[1100.0] = 50.0
    t = strike_topology(g, spot=1050.0, em=100.0,
                        cfg=Config(bin_width=25.0, window_min=150.0))
    pockets = _kinds(t, "pocket")
    assert pockets
    # take the BOUNDED pocket, not the widest: the runs of empty bins running off
    # each end of the window are also pockets, and they are wider. They carry
    # edge=True precisely so a caller can tell the two apart.
    inner = [p for p in pockets if not p["edge"]]
    assert inner, "the gap between the two walls should be a bounded pocket"
    p = inner[0]
    assert p["sign"] == 0
    assert p["lower"] >= 1000.0 and p["upper"] <= 1110.0


def test_a_wide_pocket_is_labelled_a_vacuum():
    g = {1000.0: 50.0, 1500.0: 50.0}
    t = strike_topology(g, spot=1250.0, em=300.0,
                        cfg=Config(bin_width=25.0, window_min=300.0, vacuum_width=100.0))
    vac = _labelled(t, "vacuum")
    assert vac, "a 400-point empty stretch between two walls is a vacuum"
    assert vac[0]["width"] >= 100.0


def test_a_narrow_pocket_is_not_a_vacuum():
    g = {1000.0: 50.0, 1050.0: 50.0}
    t = strike_topology(g, spot=1025.0, em=200.0,
                        cfg=Config(bin_width=25.0, window_min=200.0, vacuum_width=100.0))
    for f in _kinds(t, "pocket"):
        if f["width"] < 100.0:
            assert "vacuum" not in f["labels"]


def test_a_pocket_open_at_the_window_edge_says_so():
    """The trap this guards: the analysis window's edge is not the book's edge.
    A quiet stretch running off the end must not read as a confirmed void."""
    g = {1000.0: 50.0}
    t = strike_topology(g, spot=1000.0, em=300.0,
                        cfg=Config(bin_width=25.0, window_min=400.0))
    edge_pockets = [f for f in _kinds(t, "pocket") if f["edge"]]
    assert edge_pockets
    assert any("edge of what was analysed" in e for f in edge_pockets for e in f["evidence"])


def test_coverage_bound_truncates_the_window():
    """trinity_db drops any strike beyond the moneyness cap. Passing that cap in
    as coverage stops the truncation being reported as an empty book."""
    g = {1000.0: 50.0, 1100.0: 50.0}
    t = strike_topology(g, spot=1050.0, em=500.0,
                        cfg=Config(bin_width=25.0, window_min=1000.0),
                        coverage=(980.0, 1120.0))
    assert t["window"][0] >= 980.0
    assert t["window"][1] <= 1120.0


# ------------------------------------------------------------ transition ---

def test_a_sign_change_between_walls_is_a_transition():
    g = {1000.0: 50.0, 1200.0: -50.0}
    t = strike_topology(g, spot=1100.0, em=200.0,
                        cfg=Config(bin_width=25.0, window_min=250.0))
    tr = _kinds(t, "transition")
    assert len(tr) == 1
    assert 1000.0 <= tr[0]["center"] <= 1200.0


def test_same_signed_walls_produce_no_transition():
    g = {1000.0: 50.0, 1200.0: 50.0}
    t = strike_topology(g, spot=1100.0, em=200.0,
                        cfg=Config(bin_width=25.0, window_min=250.0))
    assert _kinds(t, "transition") == []


def test_a_transition_says_it_is_not_the_flip():
    """A sign change in the strike map is a different object from the gamma flip,
    which is a spot-axis crossing. Conflating them is the axis error the module
    exists to prevent."""
    g = {1000.0: 50.0, 1200.0: -50.0}
    t = strike_topology(g, spot=1100.0, em=200.0,
                        cfg=Config(bin_width=25.0, window_min=250.0))
    ev = " ".join(_kinds(t, "transition")[0]["evidence"])
    assert "not the gamma flip" in ev


# ------------------------------------------------------------ normalising ---

def test_thresholds_scale_with_the_book_not_with_absolute_size():
    """A quiet book and a violent one with the same SHAPE must produce the same
    topology. This is what 'normalised relative to the distribution' has to mean."""
    shape = {1000.0: 50.0, 1025.0: 10.0, 1200.0: -50.0}
    small = strike_topology(shape, spot=1100.0, em=200.0,
                            cfg=Config(bin_width=25.0, window_min=250.0))
    big = strike_topology({k: v * 1000 for k, v in shape.items()}, spot=1100.0, em=200.0,
                          cfg=Config(bin_width=25.0, window_min=250.0))
    assert [f["kind"] for f in small["features"]] == [f["kind"] for f in big["features"]]
    for a, b in zip(small["features"], big["features"]):
        assert a["strength"] == pytest.approx(b["strength"], abs=1e-9)


def test_sign_is_mirrored_for_a_negated_book():
    """Negative gamma must be described identically with the signs flipped, so
    the layer cannot be accidentally biased toward one side of the book."""
    shape = {1000.0: 50.0, 1025.0: 12.0, 1200.0: -50.0}
    a = strike_topology(shape, spot=1100.0, em=200.0,
                        cfg=Config(bin_width=25.0, window_min=250.0))
    b = strike_topology({k: -v for k, v in shape.items()}, spot=1100.0, em=200.0,
                        cfg=Config(bin_width=25.0, window_min=250.0))
    assert [f["kind"] for f in a["features"]] == [f["kind"] for f in b["features"]]
    for x, y in zip(a["features"], b["features"]):
        assert x["sign"] == -y["sign"]
        assert x["mass"] == pytest.approx(-y["mass"])


def test_strength_is_a_share_and_never_exceeds_one():
    g = {1000.0 + 25 * i: (i % 5) - 2 for i in range(20)}
    t = strike_topology(g, spot=1250.0, em=300.0, cfg=Config(bin_width=25.0))
    for f in t["features"]:
        assert 0.0 <= f["strength"] <= 1.0


def test_concentration_is_one_for_a_single_bin_wall():
    g = {1000.0: 50.0}
    t = strike_topology(g, spot=1000.0, em=200.0,
                        cfg=Config(bin_width=25.0, window_min=200.0))
    w = _kinds(t, "wall")[0]
    assert w["concentration"] == pytest.approx(1.0)


def test_config_overrides_apply():
    g = {1000.0: 50.0, 1500.0: 50.0}
    strict = strike_topology(g, spot=1250.0, em=300.0,
                             cfg=Config(bin_width=25.0, window_min=300.0), vacuum_width=1e9)
    assert _labelled(strict, "vacuum") == []


# ------------------------------------------------------------ edge cases ---

def test_an_empty_map_returns_no_features_and_says_why():
    t = strike_topology({}, spot=1000.0, em=100.0)
    assert t["features"] == []
    assert t["scale"] == 0.0
    assert "no exposure" in t["note"]


def test_an_all_zero_map_does_not_divide_by_zero():
    g = {1000.0 + 25 * i: 0.0 for i in range(10)}
    t = strike_topology(g, spot=1100.0, em=100.0)
    assert t["features"] == []
    assert t["scale"] == 0.0


def test_a_single_strike_is_handled():
    t = strike_topology({1000.0: 42.0}, spot=1000.0, em=100.0,
                        cfg=Config(bin_width=25.0, window_min=100.0))
    walls = _kinds(t, "wall")
    assert len(walls) == 1
    assert walls[0]["n_strikes"] == 1
    assert walls[0]["mass"] == pytest.approx(42.0)


def test_spot_far_outside_the_strike_range_yields_an_empty_window():
    """A modelled book that does not reach price is a data problem, and the
    honest answer is 'nothing here', not a fabricated structure."""
    g = {1000.0: 50.0, 1025.0: 40.0}
    t = strike_topology(g, spot=50000.0, em=100.0, cfg=Config(bin_width=25.0))
    assert t["features"] == []
    assert t["scale"] == 0.0


def test_a_sparse_grid_still_bins():
    g = {1000.0: 20.0, 2000.0: 20.0, 3000.0: 20.0}
    t = strike_topology(g, spot=2000.0, em=1500.0, cfg=Config(bin_width=25.0))
    assert t["n_strikes"] == 3
    assert _kinds(t, "wall")


def test_an_extreme_outlier_does_not_erase_the_rest_of_the_book():
    """p95 rather than the max is used for the scale so one absurd print cannot
    push every real strike under the material threshold."""
    g = {1000.0 + 25 * i: 10.0 for i in range(20)}
    g[1250.0] = 1e6
    t = strike_topology(g, spot=1250.0, em=300.0, cfg=Config(bin_width=25.0))
    assert len(t["features"]) > 1, "the outlier must not flatten every other feature"


def test_mixed_sign_exposure_reports_both_sides():
    g = {1000.0: 40.0, 1100.0: -40.0, 1200.0: 40.0}
    t = strike_topology(g, spot=1100.0, em=200.0,
                        cfg=Config(bin_width=25.0, window_min=250.0))
    signs = {f["sign"] for f in t["features"] if f["kind"] in ("wall", "shelf")}
    assert signs == {1, -1}
    assert len(_kinds(t, "transition")) == 2


def test_no_feature_carries_a_directional_field():
    """The schema must make it impossible to read a bias out of structure. If a
    field like 'bullish' or 'support' ever appears, this fails."""
    g = {1000.0: 50.0, 1200.0: -50.0}
    t = strike_topology(g, spot=1100.0, em=200.0, cfg=Config(bin_width=25.0))
    banned = {"direction", "bias", "bullish", "bearish", "support", "resistance", "signal"}
    for f in t["features"]:
        assert banned.isdisjoint(f.keys())


# ------------------------------------------------------------ spot axis ---

def _analytic(a, b, c, F0):
    """A fake strike_metrics whose total is c*(x)^2 + b*x + a for shift x.

    Central differences are EXACT on a quadratic, so the derivative assertions
    below are equalities, not tolerances against a numerical guess.
    """
    def sm(books, S_shift=0.0, on_axis=True):
        x = float(S_shift)
        return {0.0: a + b * x + c * x * x}, {}
    return sm


def test_spot_profile_gradient_is_exact_on_a_linear_curve():
    sm = _analytic(0.0, 3.0, 0.0, 1000.0)
    p = spot_profile([], F_ref=1000.0, spot=1000.0, strike_metrics=sm,
                     cfg=Config(profile_step=5.0, profile_span=100.0))
    assert p["at_spot"]["G"] == pytest.approx(0.0)
    assert p["at_spot"]["gradient"] == pytest.approx(3.0)
    assert p["at_spot"]["curvature"] == pytest.approx(0.0, abs=1e-9)


def test_spot_profile_curvature_is_exact_on_a_quadratic():
    sm = _analytic(10.0, 2.0, 0.5, 1000.0)
    p = spot_profile([], F_ref=1000.0, spot=1000.0, strike_metrics=sm,
                     cfg=Config(profile_step=5.0, profile_span=100.0))
    assert p["at_spot"]["G"] == pytest.approx(10.0)
    assert p["at_spot"]["gradient"] == pytest.approx(2.0)
    assert p["at_spot"]["curvature"] == pytest.approx(1.0)      # d2/dx2 of 0.5x^2


def test_spot_profile_finds_the_zero_crossing():
    sm = _analytic(-60.0, 3.0, 0.0, 1000.0)                     # zero at x = +20
    p = spot_profile([], F_ref=1000.0, spot=1000.0, strike_metrics=sm,
                     cfg=Config(profile_step=5.0, profile_span=100.0))
    assert p["crossings"]
    assert p["crossings"][0] == pytest.approx(1020.0, abs=1e-6)


def test_spot_profile_orders_crossings_nearest_first():
    """Everything downstream reads the first crossing as 'the flip'."""
    def sm(books, S_shift=0.0, on_axis=True):
        x = float(S_shift)
        return {0.0: (x - 10.0) * (x + 60.0) / 100.0}, {}
    p = spot_profile([], F_ref=1000.0, spot=1000.0, strike_metrics=sm,
                     cfg=Config(profile_step=5.0, profile_span=200.0))
    assert len(p["crossings"]) == 2
    assert abs(p["crossings"][0] - 1000.0) <= abs(p["crossings"][1] - 1000.0)


def test_spot_profile_detects_monotonicity():
    sm = _analytic(0.0, 3.0, 0.0, 1000.0)
    p = spot_profile([], F_ref=1000.0, spot=1000.0, strike_metrics=sm,
                     cfg=Config(profile_step=5.0, profile_span=100.0))
    assert p["monotone"] is True


def test_a_flat_book_has_no_crossing_and_no_gradient():
    sm = _analytic(0.0, 0.0, 0.0, 1000.0)
    p = spot_profile([], F_ref=1000.0, spot=1000.0, strike_metrics=sm,
                     cfg=Config(profile_step=5.0, profile_span=50.0))
    assert p["at_spot"]["gradient"] == pytest.approx(0.0)
    assert p["scale"] == 0.0


def test_spot_profile_carries_its_units():
    sm = _analytic(0.0, 3.0, 0.0, 1000.0)
    p = spot_profile([], F_ref=1000.0, spot=1000.0, strike_metrics=sm,
                     cfg=Config(profile_step=5.0, profile_span=50.0))
    assert "per point of underlying" in p["gradient_unit"]
    assert "per point squared" in p["curvature_unit"]


def test_spot_profile_refuses_to_imply_a_range():
    sm = _analytic(0.0, 3.0, 0.0, 1000.0)
    p = spot_profile([], F_ref=1000.0, spot=1000.0, strike_metrics=sm,
                     cfg=Config(profile_step=5.0, profile_span=50.0))
    assert "p=0.46" in p["note"]
