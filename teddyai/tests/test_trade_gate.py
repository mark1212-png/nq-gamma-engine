"""
The trade gate (scenario.book_gate, 2026-09-12): should a trade be looked for
in this book at all.

Mark: "sometimes when the book is thin we're still outputting an A, B, C and D
trade when they're all technically low quality because the book and the ladder
are bad." These tests hold that a thin or broken book is NAMED as such, that
the report withholds paths on a stand-aside book, that every check carries its
value and threshold, and that a good real book still reads `look`.
"""
import pytest

import scenario
from scenario import book_gate, generate, Config
from conftest import have_chain, chain_path


def _state(score=1.0, issues=None):
    return {"dimensions": {}, "data_quality": {"score": score,
                                               "issues": issues or ["no data-quality issue detected"],
                                               "penalties": []}}


def _topo(strength=0.5, n=3):
    feats = [{"kind": "wall", "strength": strength, "lower": 990.0 + 50 * i, "upper": 1010.0 + 50 * i,
              "labels": []} for i in range(n)]
    return {"features": feats, "n_strikes": 100, "window_truncated_by_coverage": False,
            "window": [500.0, 1500.0]}


def _node(price, q, dist_ems):
    return {"price": price, "quality": q, "sign": 1,
            "raw": {"gamma": 50.0, "oi": 500, "distance": dist_ems * 100, "distance_ems": dist_ems},
            "topology": {"kind": "wall", "labels": []}, "evidence": ["x"]}


def _nodes(*ns, dominance=0.2, gamma_scale=60.0):
    return {"nodes": list(ns), "n_candidates": len(ns), "dominance": {"price": 1000.0, "share": dominance},
            "scales": {"gamma": gamma_scale, "oi": 500.0}, "weights_validated": False, "window": [500.0, 1500.0]}


GOOD = _nodes(_node(1000.0, 0.8, 0.2), _node(1050.0, 0.6, 0.5), _node(950.0, 0.5, -0.5))


# ---------------------------------------------------------------- verdicts ---

def test_a_good_book_reads_look():
    g = book_gate(_state(), _topo(), GOOD, 980.0, 100.0)
    assert g["verdict"] == "look"
    assert g["reasons"] == []
    assert all(c["pass"] for c in g["checks"])


def test_no_usable_anchor_is_stand_aside():
    g = book_gate(_state(), _topo(), _nodes(_node(1000.0, 0.2, 0.2), _node(1300.0, 0.9, 3.0)), 980.0, 100.0)
    assert g["verdict"] == "stand aside"
    assert any("no node of quality" in r for r in g["reasons"])


def test_one_anchor_is_thin_not_stand_aside():
    g = book_gate(_state(), _topo(), _nodes(_node(1000.0, 0.8, 0.2)), 980.0, 100.0)
    assert g["verdict"] == "thin"
    assert g["anchors"] == [1000.0]


def test_bad_inputs_are_stand_aside_whatever_the_structure():
    g = book_gate(_state(0.5, ["the reference print is stale"]), _topo(), GOOD, 980.0, 100.0)
    assert g["verdict"] == "stand aside"
    assert any("stale" in r for r in g["reasons"])


def test_a_single_line_book_is_stand_aside():
    g = book_gate(_state(), _topo(), _nodes(*GOOD["nodes"], dominance=0.8), 980.0, 100.0)
    assert g["verdict"] == "stand aside"
    assert any("holds 80%" in r for r in g["reasons"])


def test_single_digit_material_gamma_is_stand_aside():
    """METHOD.md section 2: single-digit nodes carry real uncertainty."""
    g = book_gate(_state(), _topo(), _nodes(*GOOD["nodes"], gamma_scale=7.0), 980.0, 100.0)
    assert g["verdict"] == "stand aside"


def test_smallish_material_gamma_is_thin():
    g = book_gate(_state(), _topo(), _nodes(*GOOD["nodes"], gamma_scale=18.0), 980.0, 100.0)
    assert g["verdict"] == "thin"


def test_no_structure_at_all_is_stand_aside():
    g = book_gate(_state(), {"features": [], "n_strikes": 100}, GOOD, 980.0, 100.0)
    assert g["verdict"] == "stand aside"
    assert any("structural" in c["check"] for c in g["checks"] if not c["pass"])


# ------------------------------------------------------------- the record ---

def test_every_check_carries_its_value_and_threshold():
    g = book_gate(_state(), _topo(), GOOD, 980.0, 100.0)
    for c in g["checks"]:
        assert {"check", "value", "threshold", "pass", "severity", "why"} <= set(c)
        assert c["severity"] in ("hard", "soft")


def test_the_gate_says_it_is_unvalidated():
    g = book_gate(_state(), _topo(), GOOD, 980.0, 100.0)
    assert g["validated"] is False
    assert "PROVISIONAL" in g["note"]
    assert "Crucible" in g["note"]


def test_thresholds_are_config_not_constants():
    strict = Config(gate_quality=0.95)
    g = book_gate(_state(), _topo(), GOOD, 980.0, 100.0, cfg=strict)
    assert g["verdict"] == "stand aside"


# ------------------------------------------------------------ in generate ---

def test_generate_carries_the_gate_and_stamps_every_scenario():
    r = generate(_state(), _topo(), GOOD, 980.0, 100.0)
    assert r["gate"]["verdict"] == "look"
    assert r["report_carries"] == len(r["scenarios"])
    assert all(s["book_verdict"] == "look" for s in r["scenarios"])


def test_a_stand_aside_book_still_generates_but_carries_nothing():
    """Deterministic generation is kept; the REPORT is what withholds."""
    r = generate(_state(0.5, ["stale"]), _topo(), GOOD, 980.0, 100.0)
    assert r["gate"]["verdict"] == "stand aside"
    assert r["report_carries"] == 0
    assert all(s["book_verdict"] == "stand aside" for s in r["scenarios"])


def test_a_thin_book_carries_at_most_the_configured_few():
    r = generate(_state(), _topo(), _nodes(_node(1000.0, 0.8, 0.2), _node(1050.0, 0.3, 0.5)), 980.0, 100.0)
    assert r["gate"]["verdict"] == "thin"
    assert r["report_carries"] <= Config().thin_scenarios


def test_no_nodes_still_returns_a_gate():
    r = generate(_state(), _topo(), {"nodes": []}, 980.0, 100.0)
    assert r["scenarios"] == [] and r["gate"]["verdict"] == "stand aside"


# --------------------------------------------------------------- in engine ---

def test_the_engine_report_leads_with_the_gate_on_a_real_book():
    if not have_chain("2026-09-08"):
        pytest.skip("no cached chain")
    import engine
    d = engine.run(str(chain_path("2026-09-08")), "2026-09-09")
    tg = d["report"]["trade_gate"]
    assert tg["verdict"] in ("look", "thin", "stand aside")
    assert tg["scenarios_carried"] == len(d["report"]["scenarios"])
    assert tg["scenarios_generated"] == len(d["scenarios"]["scenarios"])
    v = engine.for_llm(d)
    assert v["trade_gate"] == tg
    assert any("stand aside" in m for m in v["llm_contract"]["must_not"])


def test_the_engine_withholds_scenarios_on_a_stand_aside_book(monkeypatch):
    if not have_chain("2026-09-08"):
        pytest.skip("no cached chain")
    import engine
    # force the gate shut by demanding an impossible anchor quality
    monkeypatch.setattr(scenario, "Config", lambda **kw: Config(**dict(kw, gate_quality=0.999)))
    d = engine.run(str(chain_path("2026-09-08")), "2026-09-09")
    assert d["report"]["trade_gate"]["verdict"] == "stand aside"
    assert d["report"]["scenarios"] == []
    assert len(d["scenarios"]["scenarios"]) > 0        # the document still has them
