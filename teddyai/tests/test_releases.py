"""
Unit tests for releases.py. No network: every test injects a calendar.
"""
import datetime

import pytest

import releases
from releases import for_report, events_for, summary_line, GATE_MINUTES


def _cal(events):
    return {"source": "test", "fetched": "2026-09-14T10:00:00+00:00", "events": events}


def _ev(title, when, country="USD", impact="High", **kw):
    d = {"title": title, "country": country, "impact": impact, "date": when}
    d.update(kw)
    return d


# 2026-09-14 is a Monday in EDT (UTC-4): RTH open 13:30Z, close 20:00Z, reopen Sun 22:00Z
CAL = _cal([
    _ev("CPI m/m", "2026-09-14T08:30:00-04:00", forecast="0.3%", previous="0.2%"),
    _ev("Unemployment Claims", "2026-09-14T10:00:00-04:00"),
    _ev("FOMC Statement", "2026-09-14T14:00:00-04:00"),
    _ev("GDP", "2026-09-14T05:00:00-04:00", country="EUR"),
    _ev("Speech", "2026-09-14T11:00:00-04:00", impact="Medium"),
    _ev("Retail Sales", "2026-09-15T08:30:00-04:00"),
])


def test_only_high_impact_usd_inside_the_report_unit():
    evs = events_for(CAL, "2026-09-14")
    assert [e["title"] for e in evs] == ["CPI m/m", "Unemployment Claims", "FOMC Statement"]


def test_events_are_placed_in_the_unit_windows():
    evs = {e["title"]: e for e in events_for(CAL, "2026-09-14")}
    assert evs["CPI m/m"]["window"] == "globex"
    assert evs["CPI m/m"]["minutes_from_rth_open"] == -60
    assert evs["Unemployment Claims"]["window"] == "rth"
    assert evs["FOMC Statement"]["window"] == "rth"


def test_eastern_clock_is_rendered_on_the_project_dst_rule():
    evs = {e["title"]: e for e in events_for(CAL, "2026-09-14")}
    assert evs["CPI m/m"]["time_et"] == "08:30"
    assert evs["FOMC Statement"]["time_et"] == "14:00"


def test_in_plan_marks_releases_after_the_plan_asof():
    evs = {e["title"]: e for e in events_for(CAL, "2026-09-14", asof="2026-09-14T13:30:00+00:00")}
    assert evs["CPI m/m"]["in_plan"] is False
    assert evs["Unemployment Claims"]["in_plan"] is True


def test_forecast_is_renamed_so_the_llm_view_stays_clean():
    evs = events_for(CAL, "2026-09-14")
    assert evs[0]["consensus"] == "0.3%"
    assert "forecast" not in evs[0]


def test_the_document_carries_the_gate_rule_and_says_it_is_unmeasured():
    r = for_report("2026-09-14", asof="2026-09-14T13:30:00+00:00", calendar=CAL)
    assert r["n_in_plan"] == 2
    assert str(GATE_MINUTES) in r["gate_rule"]
    assert "unmeasured" in r["measured"]
    assert "release print" in r["summary"]


def test_a_clean_day_says_no_red_folder_rather_than_nothing():
    r = for_report("2026-09-16", calendar=CAL)
    assert r["events"] == []
    assert "No red folder" in r["summary"]


def test_a_calendar_error_is_carried_not_raised():
    r = for_report("2026-09-14", calendar={"error": "boom", "events": []})
    assert r["error"] == "boom"
    assert "could not be read" in r["summary"]


def test_a_broken_calendar_never_raises():
    r = for_report("2026-09-14", calendar={"events": [{"date": "garbage", "impact": "High", "country": "USD"}]})
    assert r["events"] == []


def test_no_fetch_and_no_cache_is_an_error_document(tmp_path, monkeypatch):
    monkeypatch.setattr(releases, "CACHE_DIR", tmp_path)
    r = for_report("2026-09-14", allow_fetch=False)
    assert r.get("error")
    assert r["events"] == []


def test_the_cache_is_read_when_fetching_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(releases, "CACHE_DIR", tmp_path)
    now = datetime.datetime(2026, 9, 14, 12, 0, tzinfo=datetime.timezone.utc)
    releases._cache_path(now).write_text(__import__("json").dumps(CAL), encoding="utf-8")
    r = for_report("2026-09-14", allow_fetch=False, now=now)
    assert r["from_cache"] is True
    assert [e["title"] for e in r["events"]] == ["CPI m/m", "Unemployment Claims", "FOMC Statement"]
    assert r["age_hours"] == pytest.approx(2.0)
