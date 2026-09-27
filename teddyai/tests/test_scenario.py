"""
Unit tests for scenario.py.

The generator's job is to turn a map into conditional paths WITHOUT acquiring an
opinion about direction on the way. A large share of these tests exist to prove
the up and down paths come out of the same code with the sign mirrored, and that
no scenario is emitted or suppressed because the gamma sign looked directional.
"""
import pytest

import scenario
from scenario import Config, generate, acceptance_rule


def _node(price, quality=0.6, sign=1, kind="wall"):
    return {"price": price, "quality": quality, "sign": sign,
            "components": {}, "topology": {"kind": kind, "strength": 0.3, "labels": []},
            "raw": {"gamma": 100.0 * sign, "vanna": 1.0, "oi": 500,
                    "distance": 0.0, "distance_ems": 0.0, "expiries_material_in": 3}}


def _nodes(*ns):
    return {"nodes": list(ns), "n_candidates": len(ns), "weights_validated": False,
            "dominance": {"price": ns[0]["price"] if ns else 0, "share": 0.1}}


def _topo(*features):
    return {"features": list(features), "n_strikes": 100,
            "window_truncated_by_coverage": False}


def _feat(kind, lower, upper, strength=0.3, labels=None):
    return {"kind": kind, "lower": lower, "upper": upper, "center": (lower + upper) / 2,
            "strength": strength, "labels": labels or [], "sign": 1}


def _state(**kw):
    d = {"dimensions": {"gamma_regime": {"label": "positive", "strength": "moderate",
                                         "confidence": 0.6, "measures_agree": True},
                        "flip_state": {"label": "above every flip", "at_flip": False,
                                       "at_flip_threshold_ems": 0.25, "confidence": 0.6},
                        "concentration": {"label": "moderate", "score": 0.3, "confidence": 0.6},
                        "expiry_state": {"label": "moderate", "confidence": 0.6}},
         "data_quality": {"score": 1.0, "issues": ["no data-quality issue detected"]}}
    d["dimensions"].update(kw.pop("dimensions", {}))
    d.update(kw)
    return d


# ------------------------------------------------------------- the trigger ---

def test_acceptance_is_structural_not_a_touch():
    r = acceptance_rule(1000.0, "up", Config())
    assert "acceptance" in r and "above" in r
    assert "1,000" in r


def test_the_trigger_carries_no_clock():
    """2026-09-16, Mark: "I want 30 minute close gone completely." Acceptance is a
    swing beyond the level with no reclaim -- precommentary.level_state's "broken"
    -- so no bar size appears in the sentence or in Config."""
    r = acceptance_rule(1000.0, "up", Config())
    assert "minute" not in r and "close" not in r
    assert not hasattr(Config(), "accept_bar_min")


def test_the_trigger_direction_word_mirrors_the_side():
    assert "above" in acceptance_rule(1000.0, "up", Config())
    assert "below" in acceptance_rule(1000.0, "down", Config())


# ------------------------------------------------------- direction symmetry ---

def test_both_directions_are_generated_from_a_symmetric_book():
    """The core guarantee: structure does not become direction."""
    nodes = _nodes(_node(1100.0), _node(900.0), _node(1200.0), _node(800.0))
    topo = _topo(_feat("transition", 995.0, 1005.0))
    r = generate(_state(), topo, nodes, spot=1000.0, em=100.0)
    sides = {s["side"] for s in r["scenarios"] if s["kind"] != "pin"}
    assert sides == {"up", "down"}


def test_a_mirrored_book_produces_mirrored_scenarios():
    """Reflecting every price about spot must reflect the scenarios, not change
    which side gets a base case."""
    up = _nodes(_node(1100.0), _node(1200.0), _node(900.0))
    dn = _nodes(_node(900.0), _node(800.0), _node(1100.0))
    t = _topo(_feat("transition", 995.0, 1005.0))
    a = generate(_state(), t, up, spot=1000.0, em=100.0)
    b = generate(_state(), t, dn, spot=1000.0, em=100.0)
    assert len(a["scenarios"]) == len(b["scenarios"])


def test_the_gamma_sign_does_not_select_a_side():
    """A book of positive nodes and a book of negative nodes at the same prices
    must generate the same paths."""
    t = _topo(_feat("transition", 995.0, 1005.0))
    pos = _nodes(_node(1100.0, sign=1), _node(900.0, sign=1))
    neg = _nodes(_node(1100.0, sign=-1), _node(900.0, sign=-1))
    a = generate(_state(), t, pos, spot=1000.0, em=100.0)
    b = generate(_state(), t, neg, spot=1000.0, em=100.0)
    assert [s["side"] for s in a["scenarios"]] == [s["side"] for s in b["scenarios"]]
    assert [[x["price"] for x in s["targets"]] for s in a["scenarios"]] == \
           [[x["price"] for x in s["targets"]] for s in b["scenarios"]]


def test_no_scenario_carries_a_directional_verdict_field():
    nodes = _nodes(_node(1100.0), _node(900.0))
    r = generate(_state(), _topo(_feat("transition", 995.0, 1005.0)), nodes, 1000.0, 100.0)
    banned = {"bias", "direction_forecast", "bullish", "bearish", "probability", "expected_move_direction"}
    for s in r["scenarios"]:
        assert banned.isdisjoint(s.keys())


def test_directional_confidence_is_none_without_cross_asset_input():
    nodes = _nodes(_node(1100.0), _node(900.0))
    r = generate(_state(), _topo(_feat("transition", 995.0, 1005.0)), nodes, 1000.0, 100.0)
    for s in r["scenarios"]:
        assert s["confidence"]["directional"]["value"] is None


# ------------------------------------------------------------------ shape ---

def test_a_scenario_has_trigger_targets_and_invalidation():
    nodes = _nodes(_node(1100.0), _node(1200.0), _node(900.0))
    r = generate(_state(), _topo(_feat("transition", 995.0, 1005.0)), nodes, 1000.0, 100.0)
    s = [x for x in r["scenarios"] if x["side"] == "up"][0]
    assert s["trigger"]["level"] == pytest.approx(1000.0)
    assert s["targets"]
    assert s["invalidation"]["level"] == 900.0


def test_targets_lie_beyond_the_trigger_in_the_scenarios_own_direction():
    nodes = _nodes(_node(1100.0), _node(1200.0), _node(900.0), _node(800.0))
    r = generate(_state(), _topo(_feat("transition", 995.0, 1005.0)), nodes, 1000.0, 100.0)
    for s in r["scenarios"]:
        if s["kind"] == "pin":
            continue
        lvl = s["trigger"]["level"]
        for t in s["targets"]:
            assert (t["price"] > lvl) if s["side"] == "up" else (t["price"] < lvl)


def test_the_invalidation_sits_on_the_other_side_of_the_trigger():
    nodes = _nodes(_node(1100.0), _node(900.0))
    r = generate(_state(), _topo(_feat("transition", 995.0, 1005.0)), nodes, 1000.0, 100.0)
    for s in r["scenarios"]:
        if s["kind"] == "pin" or not s["invalidation"]:
            continue
        lvl = s["trigger"]["level"]
        iv = s["invalidation"]["level"]
        assert (iv < lvl) if s["side"] == "up" else (iv > lvl)


def test_no_more_than_max_targets_are_offered():
    nodes = _nodes(*[_node(1000.0 + 50 * i) for i in range(1, 8)])
    r = generate(_state(), _topo(_feat("transition", 995.0, 1005.0)), nodes, 1000.0, 200.0)
    for s in r["scenarios"]:
        assert len(s["targets"]) <= Config().max_targets


def test_every_scenario_records_opposing_evidence():
    nodes = _nodes(_node(1100.0, quality=0.9), _node(900.0))
    r = generate(_state(), _topo(_feat("transition", 995.0, 1005.0)), nodes, 1000.0, 100.0)
    assert any(s["opposing_evidence"] for s in r["scenarios"])


def test_a_path_with_no_structural_invalidation_records_it_as_a_risk():
    nodes = _nodes(_node(1100.0), _node(1200.0))          # nothing below
    r = generate(_state(), _topo(_feat("transition", 995.0, 1005.0)), nodes, 1000.0, 100.0)
    ups = [s for s in r["scenarios"] if s["side"] == "up"]
    assert ups
    assert any("no structural invalidation" in x for x in ups[0]["risks"])


# ------------------------------------------------------------- dedupe/rank ---

def test_paths_with_the_same_targets_are_deduplicated_to_the_nearest_trigger():
    """Every formation edge is a candidate boundary, so two triggers a few points
    apart routinely reach the same targets. That is one path, not two."""
    nodes = _nodes(_node(1200.0), _node(1300.0), _node(800.0))
    topo = _topo(_feat("wall", 1005.0, 1015.0), _feat("wall", 1020.0, 1030.0))
    r = generate(_state(), topo, nodes, spot=1000.0, em=200.0)
    ups = [s for s in r["scenarios"] if s["side"] == "up"]
    keys = [tuple(t["price"] for t in s["targets"]) for s in ups]
    assert len(keys) == len(set(keys)), "two scenarios share a target sequence"
    assert r["generated"] >= r["distinct_paths"]


def test_the_returned_count_is_capped():
    nodes = _nodes(*[_node(1000.0 + 40 * i) for i in range(1, 8)],
                   *[_node(1000.0 - 40 * i) for i in range(1, 8)])
    topo = _topo(*[_feat("wall", 1000.0 + 20 * i, 1010.0 + 20 * i) for i in range(-4, 5)])
    r = generate(_state(), topo, nodes, spot=1000.0, em=400.0, max_scenarios=3)
    assert r["returned"] <= 3
    assert len(r["scenarios"]) <= 3


def test_ranking_is_by_specification_and_says_so():
    nodes = _nodes(_node(1100.0), _node(900.0))
    r = generate(_state(), _topo(_feat("transition", 995.0, 1005.0)), nodes, 1000.0, 100.0)
    assert "SPECIFICATION" in r["ranking_basis"]
    assert "not a forecast" in r["ranking_basis"]


def test_the_tie_break_is_structural_not_directional():
    nodes = _nodes(_node(1100.0), _node(900.0))
    r = generate(_state(), _topo(_feat("transition", 995.0, 1005.0)), nodes, 1000.0, 100.0)
    assert "never by direction" in r["tie_break"]


def test_labels_are_positions_in_a_ranking():
    nodes = _nodes(_node(1100.0), _node(1200.0), _node(900.0), _node(800.0))
    r = generate(_state(), _topo(_feat("transition", 995.0, 1005.0)), nodes, 1000.0, 100.0)
    if r["scenarios"]:
        assert r["scenarios"][0]["label"] in ("base case", "no-trade / pin case")
        assert r["scenarios"][0]["rank"] == 1


# ------------------------------------------------------------------- pin ---

def test_a_pin_case_appears_when_exposure_is_concentrated_at_price():
    st = _state(dimensions={"concentration": {"label": "high", "score": 0.7, "confidence": 0.7}})
    nodes = _nodes(_node(1000.0, quality=0.9))
    r = generate(st, _topo(_feat("wall", 995.0, 1005.0, strength=0.7)), nodes, 1000.0, 100.0)
    pins = [s for s in r["scenarios"] if s["kind"] == "pin"]
    assert pins


def test_the_pin_case_refuses_to_claim_compression():
    st = _state(dimensions={"concentration": {"label": "high", "score": 0.7, "confidence": 0.7}})
    nodes = _nodes(_node(1000.0, quality=0.9))
    r = generate(st, _topo(_feat("wall", 995.0, 1005.0, strength=0.7)), nodes, 1000.0, 100.0)
    pin = [s for s in r["scenarios"] if s["kind"] == "pin"][0]
    joined = " ".join(pin["risks"])
    assert "NOT VALIDATED" in joined
    assert "p=0.46" in joined


def test_no_pin_when_structure_is_far_from_price():
    st = _state(dimensions={"concentration": {"label": "high", "score": 0.7, "confidence": 0.7}})
    nodes = _nodes(_node(1500.0, quality=0.9))
    r = generate(st, _topo(_feat("wall", 1495.0, 1505.0, strength=0.7)), nodes, 1000.0, 100.0)
    assert not [s for s in r["scenarios"] if s["kind"] == "pin"]


def test_no_pin_when_exposure_is_spread():
    st = _state(dimensions={"concentration": {"label": "low", "score": 0.1, "confidence": 0.7}})
    nodes = _nodes(_node(1000.0, quality=0.9))
    r = generate(st, _topo(_feat("wall", 995.0, 1005.0, strength=0.1)), nodes, 1000.0, 100.0)
    assert not [s for s in r["scenarios"] if s["kind"] == "pin"]


# ------------------------------------------------------------ edge cases ---

def test_no_nodes_produces_no_scenarios():
    r = generate(_state(), _topo(), {"nodes": []}, 1000.0, 100.0)
    assert r["scenarios"] == []
    assert "nothing to build a path from" in r["note"]


def test_no_topology_features_produces_no_continuation_scenarios():
    nodes = _nodes(_node(1100.0), _node(900.0))
    r = generate(_state(), _topo(), nodes, 1000.0, 100.0)
    assert not [s for s in r["scenarios"] if s["kind"] == "continuation"]


def test_low_quality_nodes_do_not_anchor_targets():
    nodes = _nodes(_node(1100.0, quality=0.05), _node(900.0, quality=0.05))
    r = generate(_state(), _topo(_feat("transition", 995.0, 1005.0)), nodes, 1000.0, 100.0)
    for s in r["scenarios"]:
        for t in s["targets"]:
            assert t["quality"] >= Config().min_quality


def test_targets_beyond_reach_are_not_offered():
    nodes = _nodes(_node(9000.0), _node(1100.0))
    r = generate(_state(), _topo(_feat("transition", 995.0, 1005.0)), nodes, 1000.0, 100.0)
    for s in r["scenarios"]:
        for t in s["targets"]:
            assert abs(t["distance_ems"]) <= Config().reach_ems


def test_a_zero_expected_move_does_not_crash():
    nodes = _nodes(_node(1100.0), _node(900.0))
    r = generate(_state(), _topo(_feat("transition", 995.0, 1005.0)), nodes, 1000.0, 0.0)
    assert isinstance(r["scenarios"], list)


def test_generation_is_deterministic():
    nodes = _nodes(_node(1100.0), _node(1200.0), _node(900.0), _node(800.0))
    topo = _topo(_feat("transition", 995.0, 1005.0), _feat("wall", 1090.0, 1110.0))
    a = generate(_state(), topo, nodes, 1000.0, 100.0)
    b = generate(_state(), topo, nodes, 1000.0, 100.0)
    assert [(s["side"], s["trigger"]["level"] if s["trigger"] else None,
             tuple(t["price"] for t in s["targets"])) for s in a["scenarios"]] == \
           [(s["side"], s["trigger"]["level"] if s["trigger"] else None,
             tuple(t["price"] for t in s["targets"])) for s in b["scenarios"]]
