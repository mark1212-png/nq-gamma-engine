"""
One side per boundary, and the played-path filter (2026-09-12).

Measured on 98 sessions: with both sides emitted for every boundary, 309 of 584
paths were already beyond their trigger at the RTH open, and re-striking the
whole book at the open still left 332 of 586 -- the both-sides rule, not the
overnight gap, was the source. The side is now set by where price is, never by
the gamma sign; the gamma-sign tests in test_scenario.py still hold. A path the
live print has already crossed is stamped `played` and withheld from the
report, kept in the document. Config(one_side_per_boundary=False) and
run(filter_played=False) restore the old behaviour exactly.
"""
import pytest

import scenario
from scenario import generate, Config
from conftest import have_chain, chain_path


def _node(price, q=0.8, sign=1):
    return {"price": price, "quality": q, "sign": sign, "raw": {"gamma": 50.0, "oi": 500,
            "distance": price - 1000.0, "distance_ems": (price - 1000.0) / 100.0},
            "topology": {"kind": "wall", "labels": []}, "evidence": ["x"]}


def _nodes(*ns):
    return {"nodes": list(ns), "n_candidates": len(ns), "dominance": {"price": 1000.0, "share": 0.2},
            "scales": {"gamma": 60.0}, "weights_validated": False, "window": [500.0, 1500.0]}


def _state():
    return {"dimensions": {}, "data_quality": {"score": 1.0, "issues": ["no data-quality issue detected"], "penalties": []}}


def _topo(*feats):
    return {"features": list(feats), "n_strikes": 100, "window_truncated_by_coverage": False, "window": [500.0, 1500.0]}


def _wall(lo, hi):
    return {"kind": "wall", "strength": 0.4, "lower": lo, "upper": hi, "center": (lo + hi) / 2, "labels": [], "sign": 1}


def _transition(lo, hi):
    return {"kind": "transition", "strength": 0.4, "lower": lo, "upper": hi, "center": (lo + hi) / 2, "labels": [], "sign": 0}


NODES = _nodes(_node(1150.0), _node(1250.0), _node(850.0), _node(750.0))


def _paths(r):
    return [s for s in r["scenarios"] if s["kind"] != "pin"]


def test_a_boundary_above_price_is_only_accepted_upward():
    r = generate(_state(), _topo(_wall(1040.0, 1060.0)), NODES, spot=1000.0, em=100.0)
    assert {s["side"] for s in _paths(r)} == {"up"}
    assert all("price is below" in s["side_basis"] for s in _paths(r))


def test_a_boundary_below_price_is_only_accepted_downward():
    r = generate(_state(), _topo(_wall(940.0, 960.0)), NODES, spot=1000.0, em=100.0)
    assert {s["side"] for s in _paths(r)} == {"down"}


def test_a_boundary_at_price_keeps_both_sides():
    """A transition centred on spot: price is on neither side of it."""
    r = generate(_state(), _topo(_transition(995.0, 1005.0)), NODES, spot=1000.0, em=100.0)
    assert {s["side"] for s in _paths(r)} == {"up", "down"}


def test_the_old_generator_is_one_flag_away():
    r = generate(_state(), _topo(_wall(1040.0, 1060.0)), NODES, spot=1000.0, em=100.0,
                 one_side_per_boundary=False)
    assert {s["side"] for s in _paths(r)} == {"up", "down"}
    assert r["one_side_per_boundary"] is False


def test_the_result_states_the_basis():
    r = generate(_state(), _topo(_wall(1040.0, 1060.0)), NODES, spot=1000.0, em=100.0)
    assert r["one_side_per_boundary"] is True
    assert "never a gamma-sign one" in r["side_basis"]


def test_the_gamma_sign_still_does_not_select_the_side():
    t = _topo(_wall(1040.0, 1060.0), _wall(940.0, 960.0))
    pos = _nodes(_node(1150.0, sign=1), _node(1250.0, sign=1), _node(850.0, sign=1), _node(750.0, sign=1))
    neg = _nodes(_node(1150.0, sign=-1), _node(1250.0, sign=-1), _node(850.0, sign=-1), _node(750.0, sign=-1))
    a = generate(_state(), t, pos, spot=1000.0, em=100.0)
    b = generate(_state(), t, neg, spot=1000.0, em=100.0)
    assert [s["side"] for s in a["scenarios"]] == [s["side"] for s in b["scenarios"]]


# ------------------------------------------------------------ played filter ---

SETTLE, REPORT = "2026-09-08", "2026-09-09"


def test_no_live_print_marks_nothing_played():
    if not have_chain(SETTLE):
        pytest.skip("no cached chain")
    import engine
    d = engine.run(str(chain_path(SETTLE)), REPORT)
    assert d["scenarios"]["played_paths"] == 0
    assert d["report"]["trade_gate"]["live_print"] is None
    for s in d["scenarios"]["scenarios"]:
        if s["kind"] != "pin":
            assert s["played"]["is_played"] is False


def test_a_live_print_beyond_a_trigger_withholds_that_path_from_the_report_only():
    if not have_chain(SETTLE):
        pytest.skip("no cached chain")
    import engine
    base = engine.run(str(chain_path(SETTLE)), REPORT)
    ups = [s for s in base["scenarios"]["scenarios"] if s["kind"] != "pin" and s["side"] == "up"]
    if not ups:
        pytest.skip("no upward path on this book")
    L = min(s["trigger"]["level"] for s in ups)
    d = engine.run(str(chain_path(SETTLE)), REPORT, last=L + 5.0)
    played = [s for s in d["scenarios"]["scenarios"] if (s.get("played") or {}).get("is_played")]
    assert played and all(s["trigger"]["level"] <= L + 5.0 for s in played if s["side"] == "up")
    assert d["report"]["trade_gate"]["scenarios_played"] == len(played)
    carried = {s["rank"] for s in d["report"]["scenarios"]}
    assert not (carried & {s["rank"] for s in played})
    assert len(d["scenarios"]["scenarios"]) == len(base["scenarios"]["scenarios"])   # the document keeps them
    assert d["report"]["played_paths"][0]["note"].startswith("the live print")


def test_the_filter_can_be_switched_off():
    if not have_chain(SETTLE):
        pytest.skip("no cached chain")
    import engine
    base = engine.run(str(chain_path(SETTLE)), REPORT)
    ups = [s for s in base["scenarios"]["scenarios"] if s["kind"] != "pin" and s["side"] == "up"]
    if not ups:
        pytest.skip("no upward path on this book")
    L = min(s["trigger"]["level"] for s in ups)
    d = engine.run(str(chain_path(SETTLE)), REPORT, last=L + 5.0, filter_played=False)
    assert d["report"]["trade_gate"]["scenarios_played"] >= 1
    assert len(d["report"]["scenarios"]) == d["report"]["trade_gate"]["scenarios_carried"]
    assert any((s.get("book_verdict") is not None) for s in d["report"]["scenarios"])
    assert {s["rank"] for s in d["report"]["scenarios"]} & {
        s["rank"] for s in d["scenarios"]["scenarios"] if (s.get("played") or {}).get("is_played")}
