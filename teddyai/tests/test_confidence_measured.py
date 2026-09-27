"""
The measurement reaches the level dimension (confidence.py, 2026-09-12).

A null result is not a calibration, so `calibrated` stays False -- but it is a
ceiling on what the level dimension may claim, and these tests hold that the
ceiling applies, is stated, and does not fire when no measurement was supplied.
"""
import pytest

from confidence import level_confidence, decompose, Config


def _node(**kw):
    d = {"price": 1000.0, "quality": 0.8, "topology": {"kind": "wall", "labels": []},
         "raw": {"expiries_material_in": 5, "distance_ems": 0.2}}
    d.update(kw)
    return d


NULL = {"weights_validated": False,
        "measured": {"sessions": 500, "node_touches_resolved": 4556, "edge_pp": -0.2, "edge_z": -0.17,
                     "scored_with": "the five-component score; the seven-component score is unmeasured"}}


def test_a_measured_null_scales_the_level_value_down():
    a = level_confidence(_node())
    b = level_confidence(_node(), NULL)
    assert b["value"] == pytest.approx(a["value"] * Config().measured_null_scale, abs=1e-3)


def test_the_measurement_is_stated_in_the_limits_with_its_numbers():
    b = level_confidence(_node(), NULL)
    lim = " ".join(b["limited_by"])
    assert "MEASURED, not assumed" in lim
    assert "500 sessions" in lim and "4,556" in lim and "z=-0.17" in lim
    assert "anchored in the book, not whether it holds" in lim


def test_a_changed_score_is_declared_unmeasured():
    b = level_confidence(_node(), NULL)
    assert any("itself unmeasured" in x for x in b["limited_by"])


def test_no_measurement_means_no_scaling():
    a = level_confidence(_node())
    b = level_confidence(_node(), {"weights_validated": False})
    assert a["value"] == b["value"]


def test_a_significant_edge_would_not_be_scaled():
    edge = {"measured": {"sessions": 500, "node_touches_resolved": 4556, "edge_pp": 4.0, "edge_z": 3.1}}
    a = level_confidence(_node())
    b = level_confidence(_node(), edge)
    assert a["value"] == b["value"]


def test_quality_still_ranks_within_the_ceiling():
    lo = level_confidence(_node(quality=0.2), NULL)
    hi = level_confidence(_node(quality=0.9), NULL)
    assert hi["value"] > lo["value"]


def test_calibrated_stays_false_because_a_null_is_not_a_calibration():
    c = decompose(nodes=NULL, node=_node())
    assert c["calibrated"] is False
    assert c["dimensions"]["level"]["calibrated"] is False


def test_the_calibration_note_carries_the_superseding_figures():
    c = decompose()
    assert "500 sessions" in c["calibration_note"]
    assert "superseding" in c["calibration_note"]
    assert "64-74%" in c["calibration_note"]
    assert "level" in c["measured_inputs"]
