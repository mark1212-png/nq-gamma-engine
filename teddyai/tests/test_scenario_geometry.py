"""
The scenario geometry fix (scenario.py, 2026-09-12).

Measured on 98 sessions before it existed: the median first target sat 25 points
beyond the trigger and the median invalidation 24 points the other way, against
a median 30-minute RTH bar of 90 points -- so the acceptance bar that IS the
trigger ran through T1 in 89 of 110 triggered paths. A target now has to sit at
least `min_target_travel_ems` beyond the trigger and the invalidation at least
`min_invalidation_ems` the other way, and the geometry is stamped on every path.
"""
import pytest

from scenario import generate, Config, _nodes_beyond, _invalidation


def _node(price, q=0.8):
    return {"price": price, "quality": q, "sign": 1, "raw": {"gamma": 50.0, "oi": 500,
            "distance": price - 1000.0, "distance_ems": (price - 1000.0) / 100.0},
            "topology": {"kind": "wall", "labels": []}, "evidence": ["x"]}


def _state():
    return {"dimensions": {}, "data_quality": {"score": 1.0, "issues": ["no data-quality issue detected"], "penalties": []}}


def _topo():
    return {"features": [{"kind": "wall", "strength": 0.5, "lower": 990.0, "upper": 1010.0, "labels": []}],
            "n_strikes": 100, "window_truncated_by_coverage": False, "window": [500.0, 1500.0]}


def test_a_target_inside_the_trigger_bars_travel_is_not_offered():
    """EM 100: a node 20 points beyond the trigger is 0.20 EM, under the 0.35 floor."""
    nodes = [_node(1020.0), _node(1060.0), _node(1120.0)]
    t = _nodes_beyond(nodes, 1000.0, "up", 980.0, 100.0, Config())
    assert [n["price"] for n in t] == [1060.0, 1120.0]


def test_the_floor_is_config_not_a_constant():
    nodes = [_node(1020.0), _node(1060.0)]
    t = _nodes_beyond(nodes, 1000.0, "up", 980.0, 100.0, Config(min_target_travel_ems=0.1))
    assert t[0]["price"] == 1020.0


def test_the_invalidation_skips_the_adjacent_strike():
    """The node 10 points below the trigger is inside one bar's noise; the next one down is the stop."""
    nodes = [_node(990.0), _node(960.0), _node(1100.0)]
    inv = _invalidation(nodes, 1000.0, "up", Config(), em=100.0)
    assert inv["price"] == 960.0


def test_no_qualifying_invalidation_is_none_not_the_near_strike():
    nodes = [_node(990.0), _node(1100.0)]
    assert _invalidation(nodes, 1000.0, "up", Config(), em=100.0) is None


def test_without_an_expected_move_the_old_rule_holds():
    nodes = [_node(990.0), _node(960.0)]
    assert _invalidation(nodes, 1000.0, "up", Config(), em=None)["price"] == 990.0


def test_every_generated_path_respects_both_floors_and_stamps_its_geometry():
    nodes = [_node(880.0), _node(940.0), _node(990.0, 0.5), _node(1010.0, 0.5), _node(1060.0), _node(1120.0)]
    r = generate(_state(), _topo(), {"nodes": nodes, "n_candidates": 6,
                                     "dominance": {"price": 1000.0, "share": 0.2},
                                     "scales": {"gamma": 60.0}, "weights_validated": False}, 1000.0, 100.0)
    cfg = Config()
    paths = [s for s in r["scenarios"] if s["kind"] != "pin"]
    assert paths
    for s in paths:
        g = s["geometry"]
        assert g["t1_travel_ems"] >= cfg.min_target_travel_ems
        if s["invalidation"]:
            assert g["risk_ems"] >= cfg.min_invalidation_ems
            assert g["R"] == pytest.approx(g["t1_travel"] / g["risk"], rel=1e-3)
        assert g["min_target_travel_ems"] == cfg.min_target_travel_ems
        assert "acceptance close" in g["note"]
