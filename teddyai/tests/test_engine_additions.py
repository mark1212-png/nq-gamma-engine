"""
Contract tests for what engine.py gained on 2026-09-12: the sign-convention
sensitivity block, the gamma profile, the rail sensitivity, the panel fallback,
the flip crossings and the releases document. Same shape as test_engine.py:
one real cached chain, assertions on structure and invariants, not numbers.
"""
import json

import pytest

import engine
from conftest import have_chain, chain_path

SETTLE = "2026-09-08"
REPORT = "2026-09-09"


@pytest.fixture(scope="module")
def doc():
    if not have_chain(SETTLE):
        pytest.skip(f"no cached chain for {SETTLE}")
    return engine.run(str(chain_path(SETTLE)), REPORT)


# ------------------------------------------------------- sign convention ---

def test_the_sign_convention_is_named_as_an_assumption(doc):
    sc = doc["sign_convention"]
    assert "assumption" in sc["note"].lower() or "METHOD.md" in sc["assumed"]
    assert sc["source"] == "convention"


def test_the_opposite_book_is_measured_not_asserted(doc):
    ui = doc["sign_convention"]["under_inversion"]
    assert ui is not None and "error" not in doc["sign_convention"]
    assert ui["node_ranking_identical"] is True
    assert ui["formations_identical"] is True
    assert ui["node_ranking"] == ui["node_ranking_inverted"]


def test_the_gamma_regime_label_inverts_with_the_book(doc):
    g = doc["sign_convention"]["under_inversion"]["gamma_regime"]
    a, i = g["assumed"], g["inverted"]
    if a in ("positive", "negative"):
        assert i == ("negative" if a == "positive" else "positive")
    if g["score_assumed"] is not None and g["score_inverted"] is not None:
        assert g["score_inverted"] == pytest.approx(-g["score_assumed"], abs=1e-6)


def test_the_flip_is_invariant_under_inversion_in_practice(doc):
    """The block says so by construction; check it on the book."""
    import trinity_db
    raw, books, S, asof, F_ref, sym = trinity_db.load(str(chain_path(SETTLE)), REPORT)
    P = trinity_db.panel_cols(books)
    a = trinity_db.hvl(P["NEAR"], F_ref)
    b = trinity_db.hvl(P["NEAR"], F_ref, dealer_long_calls=False)
    assert a == b


def test_the_report_and_llm_view_carry_the_sensitivity(doc):
    r = doc["report"]["sign_convention"]
    assert r["invariant_under_inversion"]["node_ranking_identical"] is True
    v = engine.for_llm(doc)
    assert v["sign_convention"]["gamma_regime_if_inverted"] in ("positive", "negative", "flat", None)
    assert any("sign convention" in c.lower() for c in v["caveats"])


# --------------------------------------------------------- gamma profile ---

def test_the_profile_rides_in_the_document_for_every_non_empty_panel(doc):
    for k, v in doc["gamma_profile"].items():
        if doc["net"][k] is None:
            continue
        assert v["curve"]["x"] and v["at_spot"]["slope"] is not None, k


def test_the_report_profile_is_the_read_at_spot_only(doc):
    gp = doc["report"]["gamma_profile"]
    assert gp
    for k, v in gp.items():
        assert set(v) == {"gamma_at_spot", "slope_at_spot"}
    assert engine.for_llm(doc)["gamma_profile"] == gp


def test_the_units_declare_the_profile(doc):
    assert "slope" in doc["units"]["gamma_profile"]


# ------------------------------------------------------ rail sensitivity ---

def test_every_reported_level_carries_its_rail_call_twice(doc):
    for l in doc["report"]["levels"]:
        assert set(l) >= {"inside_rail", "inside_rail_wide", "rail_call_fragile"}
        assert l["rail_call_fragile"] == (l["inside_rail"] != l["inside_rail_wide"])
        if l["inside_rail"]:
            assert l["inside_rail_wide"]           # a wider band never excludes what the band held


def test_the_fragile_list_is_exactly_the_flagged_levels(doc):
    rs = doc["report"]["rail_sensitivity"]
    assert rs["factor"] == engine.RAIL_SENSITIVITY
    assert rs["fragile_levels"] == [l["price"] for l in doc["report"]["levels"] if l["rail_call_fragile"]]


# --------------------------------------------------------- panel fallback ---

def test_an_empty_panel_falls_back_one_panel_out_and_says_which():
    if not have_chain(SETTLE):
        pytest.skip("no cached chain")
    d = engine.run(str(chain_path(SETTLE)), REPORT, panel="0DTE")
    assert "panel_was_empty" in d["source"] and "panel_used" in d["source"]
    if d["source"]["panel_was_empty"]:
        assert d["source"]["panel_used"] in ("NEAR", "FULL")
        assert d["source"]["panel"] == "0DTE"


def test_a_panel_that_exists_is_used_as_requested(doc):
    assert doc["source"]["panel_used"] == doc["source"]["panel"] == "NEAR"
    assert doc["source"]["panel_was_empty"] is False


# -------------------------------------------------------- flip crossings ---

def test_crossing_counts_reach_the_report_and_the_llm_view(doc):
    assert set(doc["flip_crossings"]) == {"0DTE", "NEAR", "FULL"}
    fl = doc["report"]["state"]["flip"]
    if fl.get("crossings"):
        assert fl["fragmented"] == any(c > 1 for c in fl["crossings"].values())
    assert engine.for_llm(doc)["flip_crossings"] == doc["flip_crossings"]


# --------------------------------------------------------------- releases ---

def test_a_library_call_never_touches_the_network(doc):
    assert doc["releases"] is None
    assert doc["report"]["releases"] is None


def test_an_injected_release_document_is_carried_and_summarised():
    if not have_chain(SETTLE):
        pytest.skip("no cached chain")
    import releases
    cal = {"source": "test", "fetched": "2026-09-09T10:00:00+00:00",
           "events": [{"title": "CPI m/m", "country": "USD", "impact": "High",
                       "date": "2026-09-09T08:30:00-04:00", "forecast": "0.3%"}]}
    rel = releases.for_report(REPORT, asof="2026-09-09T13:30:00+00:00", calendar=cal)
    d = engine.run(str(chain_path(SETTLE)), REPORT, releases=rel)
    rr = d["report"]["releases"]
    assert rr["events"][0]["title"] == "CPI m/m"
    assert any("release print" in c for c in d["report"]["caveats"])
    v = engine.for_llm(d)
    assert v["releases"]["gate_rule"]
    assert '"forecast"' not in json.dumps(v)


# ------------------------------------------------------------- measured ---

def test_the_null_rides_with_the_levels_in_the_llm_view(doc):
    v = engine.for_llm(doc)
    assert "nodes" not in v
    m = v["levels_measured"]
    assert m["sessions"] == 500 and m["edge_z"] == -0.17


def test_the_level_confidence_names_the_measurement(doc):
    lim = " ".join(doc["confidence"]["dimensions"]["level"]["limited_by"])
    assert "MEASURED, not assumed" in lim
    assert doc["confidence"]["dimensions"]["level"]["calibrated"] is False


def test_the_original_contract_tests_still_hold(doc):
    for k in ("market_state", "topology", "nodes", "expiry", "scenarios",
              "confidence", "reference", "flips", "net", "report", "units"):
        assert k in doc
    json.dumps(doc)
    json.dumps(engine.for_llm(doc))
