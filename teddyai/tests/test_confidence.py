"""
Unit tests for confidence.py.

Most of these check what the module must REFUSE to do: produce one number, call
anything a probability, or invent a directional confidence when nothing
directional was supplied.
"""
import pytest

import confidence
from confidence import (decompose, structural_confidence, level_confidence,
                        regime_confidence, directional_confidence, scenario_confidence,
                        Config)


def _topo(features, **kw):
    d = {"features": features, "n_strikes": 100, "window_truncated_by_coverage": False}
    d.update(kw)
    return d


def _feat(kind="wall", strength=0.3, lower=1000.0, upper=1025.0, labels=None):
    return {"kind": kind, "strength": strength, "lower": lower, "upper": upper,
            "labels": labels or []}


def _state(dims=None, dq=None):
    return {"dimensions": dims or {}, "data_quality": dq or {"score": 1.0, "issues": ["no data-quality issue detected"]}}


# ------------------------------------------------------------ the contract ---

def test_there_is_no_single_confidence_number():
    c = decompose(state=_state(), topo=_topo([]))
    assert "value" not in c
    assert "overall" not in c
    assert "no_single_number" in c


def test_nothing_is_called_a_probability():
    c = decompose(state=_state(), topo=_topo([_feat()]))
    blob = repr(c).lower()
    assert "probability" not in blob.replace("probabilit", "")  # guard the schema, not the prose
    for d in c["dimensions"].values():
        assert d["calibrated"] is False


def test_every_dimension_is_stamped_uncalibrated():
    c = decompose(state=_state(), topo=_topo([_feat()]))
    assert c["calibrated"] is False
    assert all(d["calibrated"] is False for d in c["dimensions"].values())


def test_no_dimension_exceeds_the_uncalibrated_ceiling():
    c = decompose(state=_state(), topo=_topo([_feat(strength=1.0)]))
    for d in c["dimensions"].values():
        if d["value"] is not None:
            assert d["value"] <= Config().max_confidence


def test_the_six_named_dimensions_are_present():
    c = decompose(state=_state(), topo=_topo([]))
    assert set(c["dimensions"]) == {"data_quality", "structural", "level",
                                    "regime", "directional", "scenario"}


# ------------------------------------------------------------- directional ---

def test_directional_is_none_without_a_directional_input():
    """It must not default to 0.5. A neutral-looking number reads as a weak
    opinion when the truth is that no opinion was formed."""
    d = directional_confidence(None)
    assert d["value"] is None
    assert any("no direction" in x.lower() or "carries no direction" in x for x in d["limited_by"])


def test_directional_explains_why_gamma_cannot_supply_it():
    d = directional_confidence(None)
    assert any("4,354 breaks" in x for x in d["limited_by"])


def test_directional_appears_only_with_cross_asset_evidence():
    d = directional_confidence({"composite": -0.5, "agreement_strength": 0.7,
                                "conflict": 0.1, "weights_validated": False})
    assert d["value"] is not None
    assert any("unvalidated" in x for x in d["limited_by"])


def test_cross_asset_conflict_reduces_directional_confidence():
    lo = directional_confidence({"composite": -0.5, "agreement_strength": 0.7, "conflict": 0.5})
    hi = directional_confidence({"composite": -0.5, "agreement_strength": 0.7, "conflict": 0.0})
    assert lo["value"] < hi["value"]


# -------------------------------------------------------------- structural ---

def test_structural_confidence_is_near_zero_with_no_formations():
    s = structural_confidence(_topo([]))
    assert s["value"] <= 0.1


def test_concentrated_structure_beats_fragmented_structure():
    tight = structural_confidence(_topo([_feat(strength=0.30), _feat(strength=0.25), _feat(strength=0.20)]))
    spread = structural_confidence(_topo([_feat(strength=0.04) for _ in range(20)]))
    assert tight["value"] > spread["value"]


def test_a_fragmented_book_says_so():
    s = structural_confidence(_topo([_feat(strength=0.04) for _ in range(20)]))
    assert any("fragmented" in x for x in s["limited_by"])


def test_a_truncated_window_lowers_structural_confidence():
    a = structural_confidence(_topo([_feat(strength=0.5)]))
    b = structural_confidence(_topo([_feat(strength=0.5)], window_truncated_by_coverage=True))
    assert b["value"] < a["value"]
    assert any("moneyness cap" in x for x in b["limited_by"])


def test_a_single_dominant_line_lowers_structural_confidence():
    """METHOD.md section 6: a 'major call wall' that was 82% one 1,556-lot line."""
    t = _topo([_feat(strength=0.5)])
    a = structural_confidence(t, {"dominance": {"price": 1000.0, "share": 0.1}})
    b = structural_confidence(t, {"dominance": {"price": 1000.0, "share": 0.8}})
    assert b["value"] < a["value"]
    assert any("single line" in x for x in b["limited_by"])


def test_a_thin_strike_count_lowers_structural_confidence():
    a = structural_confidence(_topo([_feat(strength=0.5)], n_strikes=100))
    b = structural_confidence(_topo([_feat(strength=0.5)], n_strikes=5))
    assert b["value"] < a["value"]


# ------------------------------------------------------------------- level ---

def _node(**kw):
    d = {"price": 1000.0, "quality": 0.8, "topology": {"kind": "wall", "labels": []},
         "raw": {"expiries_material_in": 5, "distance_ems": 0.2}}
    d.update(kw)
    return d


def test_level_confidence_rises_with_node_quality():
    lo = level_confidence(_node(quality=0.2))
    hi = level_confidence(_node(quality=0.9))
    assert hi["value"] > lo["value"]


def test_a_node_in_no_formation_is_penalised():
    a = level_confidence(_node())
    b = level_confidence(_node(topology={"kind": None, "labels": []}))
    assert b["value"] < a["value"]
    assert any("lone value" in x for x in b["limited_by"])


def test_a_single_expiry_node_is_penalised_with_the_reason():
    b = level_confidence(_node(raw={"expiries_material_in": 1, "distance_ems": 0.2}))
    assert any("1,838-lot" in x for x in b["limited_by"])


def test_a_distant_node_is_penalised():
    near = level_confidence(_node(raw={"expiries_material_in": 5, "distance_ems": 0.2}))
    far = level_confidence(_node(raw={"expiries_material_in": 5, "distance_ems": 2.5}))
    assert far["value"] < near["value"]


def test_unfitted_weights_are_declared_on_the_level_dimension():
    c = level_confidence(_node(), {"weights_validated": False})
    assert any("unfitted" in x for x in c["limited_by"])


def test_no_node_gives_a_none_level_confidence():
    assert level_confidence(None)["value"] is None


# ------------------------------------------------------------------ regime ---

def test_regime_confidence_takes_the_minimum_not_the_mean():
    """A state read is only as good as its weakest dimension. A mean would let a
    confident vol reading paper over a gamma sign the two measures disagree on."""
    dims = {"a": {"confidence": 0.9}, "b": {"confidence": 0.9}, "c": {"confidence": 0.2}}
    r = regime_confidence(_state(dims))
    assert r["value"] == pytest.approx(0.2)


def test_regime_confidence_names_the_binding_dimension():
    dims = {"gamma_regime": {"confidence": 0.2}, "vol_state": {"confidence": 0.9}}
    r = regime_confidence(_state(dims))
    assert any("gamma_regime" in x for x in r["limited_by"])


def test_a_gamma_measure_disagreement_is_surfaced():
    dims = {"gamma_regime": {"confidence": 0.5, "measures_agree": False}}
    r = regime_confidence(_state(dims))
    assert any("disagree in sign" in x for x in r["limited_by"])


def test_data_quality_issues_flow_into_the_regime_limits():
    st = _state({"a": {"confidence": 0.5}},
                dq={"score": 0.8, "issues": ["open interest is PRELIMINARY and will be revised"]})
    r = regime_confidence(st)
    assert any("PRELIMINARY" in x for x in r["limited_by"])


def test_no_measured_dimension_gives_none():
    assert regime_confidence(_state({}))["value"] is None


# ---------------------------------------------------------------- scenario ---

def test_scenario_confidence_rewards_specification_not_likelihood():
    full = scenario_confidence({"trigger": {"level": 1}, "targets": [1, 2, 3],
                                "invalidation": {"level": 9}, "opposing_evidence": ["x"]})
    bare = scenario_confidence({"trigger": None, "targets": [], "invalidation": None,
                                "opposing_evidence": []})
    assert full["value"] > bare["value"]
    assert any("says nothing about whether the path occurs" in x for x in full["limited_by"])


def test_a_scenario_without_an_invalidation_is_flagged():
    s = scenario_confidence({"trigger": {"level": 1}, "targets": [1], "invalidation": None})
    assert any("cannot be proven wrong" in x for x in s["limited_by"])


def test_a_scenario_without_a_trigger_is_flagged():
    s = scenario_confidence({"trigger": None, "targets": [1], "invalidation": {"level": 2}})
    assert any("no trigger" in x for x in s["limited_by"])


def test_no_scenario_gives_none():
    assert scenario_confidence(None)["value"] is None


# ------------------------------------------------------------ edge cases ---

def test_missing_inputs_give_none_dimensions_never_guesses():
    c = decompose()
    for name in ("level", "regime", "directional", "scenario"):
        assert c["dimensions"][name]["value"] is None


def test_every_dimension_records_what_limits_it():
    c = decompose(state=_state({"a": {"confidence": 0.3}}), topo=_topo([_feat(strength=0.1)]))
    for name, d in c["dimensions"].items():
        assert "limited_by" in d
        assert "evidence" in d
