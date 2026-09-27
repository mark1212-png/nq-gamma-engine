"""
Tests for state_panel.py.

The panel is called from `build.py`, which runs unattended inside `autopull` four
times a day. Its single most important property is that it CANNOT RAISE, so most
of these feed it something broken and check it returns an empty string instead of
an exception.
"""
import json

import pytest

import state_panel


DOC = {
    "source": {"settle": "2026-09-08", "report_date": "2026-09-09", "panel": "NEAR"},
    "report": {
        "state": {
            "gamma": {"label": "positive", "strength": "strong", "confidence": 0.52,
                      "measures_agree": True},
            "concentration": {"label": "high", "score": 0.54, "confidence": 0.77},
            "flip": {"label": "above every flip", "at_flip": True, "confidence": 0.70},
            "expiry": {"label": "moderate", "gross_share": 0.15, "confidence": 0.69},
            "vanna": {"label": "positive", "score": 0.99, "confidence": 0.79},
            "vol": {"label": "subdued", "atm_iv": 0.1306, "percentile": 0.23, "confidence": 0.54},
        },
        "levels": [
            {"price": 29600.0, "quality": 0.88, "sign": 1, "gamma": 254.5, "oi": 1809,
             "distance": 61.0, "topology": "wall", "labels": ["cluster"], "why": ["x"]},
        ],
        "scenarios": [
            {"rank": 1, "label": "base case", "side": "up",
             "trigger": "a 30-minute close above 29,515",
             "targets": [29600.0, 29650.0], "invalidation": "a 30-minute close below 29,450",
             "risks": [], "opposing": []},
        ],
        "confidence": {"structural": {"value": 0.85, "limited_by": []},
                       "directional": {"value": None, "limited_by": ["no directional input"]}},
        "caveats": [],
    },
}


def _write(tmp_path, obj):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


# --------------------------------------------------------------- fail-safe ---

def test_a_missing_file_returns_empty_and_does_not_raise():
    assert state_panel.render("no-such-file.json") == ""


def test_malformed_json_returns_empty(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json at all", encoding="utf-8")
    assert state_panel.render(p) == ""


def test_a_document_with_no_report_returns_empty(tmp_path):
    assert state_panel.render(_write(tmp_path, {"source": {}})) == ""


def test_an_empty_document_returns_empty(tmp_path):
    assert state_panel.render(_write(tmp_path, {})) == ""


def test_a_document_missing_every_optional_key_still_renders(tmp_path):
    """A partial document must degrade, not explode."""
    out = state_panel.render(_write(tmp_path, {"source": {}, "report": {"state": {}}}))
    assert isinstance(out, str)


def test_a_null_where_a_number_is_expected_does_not_raise(tmp_path):
    doc = json.loads(json.dumps(DOC))
    doc["report"]["levels"][0]["price"] = None
    doc["report"]["levels"][0]["oi"] = None
    assert isinstance(state_panel.render(_write(tmp_path, doc)), str)


def test_a_wrongly_typed_field_does_not_raise(tmp_path):
    doc = json.loads(json.dumps(DOC))
    doc["report"]["levels"] = "not a list"
    assert isinstance(state_panel.render(_write(tmp_path, doc)), str)


# ------------------------------------------------------------------ content ---

def test_the_state_strip_is_rendered(tmp_path):
    h = state_panel.render(_write(tmp_path, DOC))
    assert "Market state" in h
    assert "Dealer gamma" in h
    assert "Volatility" in h


def test_levels_and_scenarios_are_rendered(tmp_path):
    h = state_panel.render(_write(tmp_path, DOC))
    assert "Ranked nodes" in h
    assert "29,600" in h
    assert "Scenarios" in h
    assert "30-minute close above 29,515" in h


def test_the_invalidation_is_shown_beside_its_scenario(tmp_path):
    h = state_panel.render(_write(tmp_path, DOC))
    assert "invalidation" in h
    assert "29,450" in h


def test_confidence_is_shown_and_declared_not_a_probability(tmp_path):
    h = state_panel.render(_write(tmp_path, DOC))
    assert "Confidence" in h
    assert "None of these is a probability" in h


def test_the_range_caveat_travels_with_the_panel(tmp_path):
    h = state_panel.render(_write(tmp_path, DOC))
    assert "p=0.46" in h


def test_a_gamma_measure_disagreement_is_surfaced(tmp_path):
    doc = json.loads(json.dumps(DOC))
    doc["report"]["state"]["gamma"]["measures_agree"] = False
    h = state_panel.render(_write(tmp_path, doc))
    assert "DISAGREE" in h


def test_the_panel_stamps_the_book_it_was_computed_on(tmp_path):
    h = state_panel.render(_write(tmp_path, DOC))
    assert "2026-09-08" in h
    assert "2026-09-09" in h


# ---------------------------------------------------------------- staleness ---

def test_a_state_from_a_different_book_is_labelled(tmp_path):
    """Showing yesterday's structure beside today's ladder is the same class of
    failure build.staleness() exists to catch for the prose."""
    h = state_panel.render(_write(tmp_path, DOC), sheet_settle="2026-09-09")
    assert "different book" in h
    assert 'class="note w"' in h


def test_a_state_for_a_different_session_is_labelled(tmp_path):
    h = state_panel.render(_write(tmp_path, DOC), sheet_report="2026-09-11")
    assert "different session" in h


def test_a_matching_book_raises_no_warning(tmp_path):
    h = state_panel.render(_write(tmp_path, DOC),
                           sheet_settle="2026-09-08", sheet_report="2026-09-09")
    assert "different book" not in h
    assert "different session" not in h


# ------------------------------------------------------------------ safety ---

def test_text_from_the_document_is_escaped(tmp_path):
    """The document is machine-written, but it feeds straight into a page."""
    doc = json.loads(json.dumps(DOC))
    doc["report"]["state"]["gamma"]["label"] = "<script>alert(1)</script>"
    h = state_panel.render(_write(tmp_path, doc))
    assert "<script>" not in h
    assert "&lt;script&gt;" in h


def test_the_panel_is_bounded_in_size(tmp_path):
    """The sheet already carries a full ladder; this is a shortlist."""
    doc = json.loads(json.dumps(DOC))
    doc["report"]["levels"] = [dict(DOC["report"]["levels"][0], price=29000.0 + i)
                               for i in range(40)]
    h = state_panel.render(_write(tmp_path, doc))
    assert h.count("<tr>") <= state_panel.MAX_LEVELS + 1      # +1 for the header row


def test_scenarios_are_capped(tmp_path):
    doc = json.loads(json.dumps(DOC))
    doc["report"]["scenarios"] = [dict(DOC["report"]["scenarios"][0], rank=i) for i in range(10)]
    h = state_panel.render(_write(tmp_path, doc))
    assert h.count("<li>") <= state_panel.MAX_SCENARIOS


# ------------------------------------------------------- the build.py hook ---

def test_build_py_consumes_the_token_and_asserts_it(root):
    """build.py asserts every placeholder was substituted. The panel token must
    be in that list, or an unrendered __STATEPANEL__ could ship on the sheet."""
    src = (root / "build.py").read_text(encoding="utf-8")
    assert "__STATEPANEL__" in src
    assert "'__CAVEAT__', '__STATEPANEL__'" in src or '"__STATEPANEL__"' in src


def test_the_build_hook_is_wrapped_so_it_cannot_break_a_sheet(root):
    src = (root / "build.py").read_text(encoding="utf-8")
    i = src.find("import state_panel")
    assert i > 0, "build.py does not import state_panel"
    window = src[max(0, i - 200):i + 300]
    assert "try:" in window and "except Exception" in window
