"""
Integration tests for engine.py.

These are the only tests in the suite that run the real stack on a real cached
chain, because composition is the thing being tested and a fake book would not
exercise it. Everything they assert is a CONTRACT -- the shape of the document,
the units it declares, what the LLM subset is allowed to contain -- rather than a
number, so they do not duplicate the baseline regression.
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


# ------------------------------------------------------------ composition ---

def test_the_document_carries_every_layer(doc):
    for k in ("market_state", "topology", "nodes", "expiry", "scenarios",
              "confidence", "reference", "flips", "net", "report", "units"):
        assert k in doc, f"{k} missing from the document"


def test_the_document_is_json_serialisable(doc):
    """It is written to disk and handed to an API, so anything unserialisable is
    a defect that would only appear in production."""
    json.dumps(doc)


def test_units_are_declared_for_every_quantity_that_has_one(doc):
    u = doc["units"]
    assert "per 1% move" in u["gamma"]
    assert "per vol point" in u["vanna"]
    assert "decimal" in u["iv"]
    assert "sessions" in u["time"]


def test_the_engine_does_not_touch_trinity_json(root, doc):
    """The sheet, the tracker and the archive all read trinity.json. This layer
    is additive and must leave it alone."""
    p = root / "trinity.json"
    if not p.exists():
        pytest.skip("no trinity.json in the working tree")
    before = p.read_bytes()
    engine.run(str(chain_path(SETTLE)), REPORT)
    assert p.read_bytes() == before


def test_the_reference_block_matches_the_exposure_engine(doc):
    import trinity_db
    out, _, _ = trinity_db.analyse(str(chain_path(SETTLE)), REPORT, hours=6.5, quiet=True)
    assert doc["reference"]["spot"] == out["spot"]
    assert doc["reference"]["em"] == out["em"]
    assert doc["flips"]["NEAR"] == (out["panels"]["NEAR"]["hvl"] or [None])[0]


# ---------------------------------------------------------------- report ---

def test_the_report_is_ranked_and_filtered(doc):
    """The brief: do not flood the trader with thirty important levels."""
    levels = doc["report"]["levels"]
    assert len(levels) <= engine.TOP_NODES
    q = [l["quality"] for l in levels]
    assert q == sorted(q, reverse=True)


def test_the_full_node_list_survives_in_the_document(doc):
    """Filtering is for the report view; anything downstream can still see all
    of them."""
    assert len(doc["nodes"]["nodes"]) >= len(doc["report"]["levels"])


def test_every_reported_level_explains_itself(doc):
    for l in doc["report"]["levels"]:
        assert l["why"], f"{l['price']} has no explanation"


def test_the_report_carries_the_measured_caveats(doc):
    joined = " ".join(doc["report"]["caveats"])
    assert "p=0.46" in joined
    # the 500-session null, measured 2026-09-10, must ride on every report
    assert "NODES DO NOT PREDICT REACTIONS" in joined
    assert "56.8%" in joined and "57.0%" in joined
    assert "not a probability" in joined or "No confidence figure here is a probability" in joined


def test_the_report_state_block_carries_confidences(doc):
    for name, d in doc["report"]["state"].items():
        if d:
            assert "confidence" in d, f"{name} reported without a confidence"


# ------------------------------------------------------------ LLM subset ---

def test_the_llm_view_is_a_strict_subset(doc):
    v = engine.for_llm(doc)
    assert "topology" in v and "scenarios" in v and "levels" in v
    # the raw internals are NOT handed over
    assert "nodes" not in v            # the scored list with all components
    assert "market_state" in v         # only the reduced report view
    assert "dimensions" not in v.get("market_state", {})


def test_the_llm_view_carries_its_own_contract(doc):
    v = engine.for_llm(doc)
    c = v["llm_contract"]
    assert c["may"] and c["must_not"]
    joined = " ".join(c["must_not"])
    assert "invent a level" in joined
    assert "invent a probability" in joined
    assert "recalculate any Greek" in joined
    assert "convert a gamma sign into a directional call" in joined


def test_the_contract_travels_with_the_payload_not_the_prompt(doc):
    """A rule that lives only in a prompt drifts away from the data it governs."""
    v = engine.for_llm(doc)
    assert "llm_contract" in v
    assert "why" in v["llm_contract"]


def test_the_llm_view_is_json_serialisable(doc):
    json.dumps(engine.for_llm(doc))


def test_the_llm_view_contains_no_field_an_llm_could_be_asked_to_fill(doc):
    """Nothing in the payload should look like a blank waiting for a number."""
    v = engine.for_llm(doc)
    blob = json.dumps(v)
    for bad in ('"thesis"', '"narrative"', '"commentary"', '"forecast"', '"prediction"'):
        assert bad not in blob


def test_the_llm_view_topology_is_only_meaningful_formations(doc):
    v = engine.for_llm(doc)
    for f in v["topology"]:
        assert f["kind"] in ("wall", "shelf", "pocket") or "vacuum" in f.get("labels", [])


def test_data_quality_reaches_the_llm_view(doc):
    v = engine.for_llm(doc)
    assert "score" in v["data_quality"]
    assert "issues" in v["data_quality"]


# ------------------------------------------------------------ behaviour ---

def test_the_engine_is_deterministic(doc):
    again = engine.run(str(chain_path(SETTLE)), REPORT)
    assert again["reference"] == doc["reference"]
    assert [l["price"] for l in again["report"]["levels"]] == \
           [l["price"] for l in doc["report"]["levels"]]
    assert [s["trigger"] for s in again["report"]["scenarios"]] == \
           [s["trigger"] for s in doc["report"]["scenarios"]]


def test_directional_confidence_is_absent_without_cross_asset_input(doc):
    assert doc["report"]["confidence"]["directional"]["value"] is None


def test_a_panel_with_no_book_falls_back_and_says_so():
    """The 0DTE panel is EMPTY, not flat, on a date with no expiry. Falling back
    silently would let the report describe the wrong book."""
    if not have_chain(SETTLE):
        pytest.skip("no cached chain")
    d = engine.run(str(chain_path(SETTLE)), REPORT, panel="0DTE")
    assert "panel_was_empty" in d["source"]


def test_the_scenarios_in_the_report_keep_their_invalidations(doc):
    for s in doc["report"]["scenarios"]:
        if s["side"] is not None:
            assert "trigger" in s and "targets" in s and "invalidation" in s


# ------------------------------------------------- --from-trinity inheritance ---

def test_from_trinity_inherits_the_hours_and_the_reference(root, tmp_path, monkeypatch):
    """Found live on 2026-09-10, mid-session.

    `--from-trinity` took the chain and the report date from trinity.json and then
    used its own defaults for everything else. On an INTRADAY sheet that built the
    PREMARKET view: 6.5 hours against the sheet's 3.96, the settle at 29,448.75
    against the sheet's 29,216 reference, and a NEAR book reading +1,269 beside a
    sheet showing -857. The panel would have sat under the ladder contradicting it.
    """
    import subprocess
    import sys
    p = root / "trinity.json"
    if not p.exists():
        pytest.skip("no trinity.json in the working tree")
    T = json.loads(p.read_text(encoding="utf-8"))
    if not T.get("hours") or not T.get("spot"):
        pytest.skip("trinity.json carries no hours/spot to inherit")

    out = tmp_path / "state.json"
    r = subprocess.run([sys.executable, "engine.py", "--from-trinity", "--out", str(out)],
                       cwd=root, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    d = json.loads(out.read_text(encoding="utf-8"))

    assert d["source"]["hours"] == pytest.approx(float(T["hours"]), abs=0.01), \
        "the state document did not inherit the sheet's hours"
    assert d["reference"]["spot"] == pytest.approx(float(T["spot"]), abs=0.01), \
        "the state document did not inherit the sheet's reference"
    # and therefore the derived figures agree with the sheet
    for k in ("0DTE", "NEAR", "FULL"):
        want = ((T["panels"].get(k) or {}).get("hvl") or [None])[0]
        if want is not None:
            assert d["flips"][k] == pytest.approx(want, abs=0.05), f"{k} flip disagrees with the sheet"
        assert d["net"][k] == (T["panels"].get(k) or {}).get("net_total"), \
            f"{k} net gamma disagrees with the sheet"


def test_an_explicit_hours_still_overrides_trinity(root, tmp_path):
    import subprocess
    import sys
    if not (root / "trinity.json").exists():
        pytest.skip("no trinity.json")
    out = tmp_path / "state.json"
    r = subprocess.run([sys.executable, "engine.py", "--from-trinity", "--hours", "2.0",
                        "--out", str(out)], cwd=root, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["source"]["hours"] == pytest.approx(2.0, abs=0.01)
