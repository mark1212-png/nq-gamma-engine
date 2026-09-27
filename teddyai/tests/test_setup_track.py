"""setup_track: the window a sheet is graded over is the sheet's own.

Until 2026-09-15 `load_bars` took `rth_only` and every caller but `--full-day`
passed True, so an Asia plan was graded against RTH bars -- a window that starts
seven hours after the one it was written for and shares no minute with it.
"""
import sys, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import setup_track as ST


def sheet(label, title="Basilisk 2026-09-14"):
    return (f"<title>{title}</title>"
            f'<div class="st"><div class="k">{label} range</div><div class="v mono">\u00b1139</div></div>')


def test_asia_sheet_grades_the_overnight():
    assert ST.parse_window(sheet("Asia")) == ("asia", (0, 0), (13, 30))
    assert ST.parse_window(sheet("Rest of overnight")) == ("asia", (0, 0), (13, 30))


def test_rth_sheet_grades_rth():
    assert ST.parse_window(sheet("1D")) == ("rth", (13, 30), (20, 0))
    assert ST.parse_window("") == ("rth", (13, 30), (20, 0))


def test_intraday_starts_at_the_build_clock_from_the_title():
    w = ST.parse_window(sheet("Rest of session", "Basilisk 2026-09-14 Rest of session 0844"))
    assert w == ("intraday", (8, 44), (20, 0))


def test_intraday_without_a_stamped_clock_grades_the_whole_session():
    assert ST.parse_window(sheet("Rest of session")) == ("intraday", (13, 30), (20, 0))


def test_the_label_is_read_from_its_own_element_not_from_prose():
    """An RTH sheet whose lead talks about the Asia band is still an RTH sheet."""
    doc = sheet("1D") + "<p>the Asia band was 28,899 to 29,178; Asia MAX 29,178</p>"
    assert ST.parse_window(doc)[0] == "rth"


def test_the_build_clock_is_converted_through_the_zone_not_a_fixed_offset():
    assert ST._pt_to_utc("2026-09-14", 8, 44) == (15, 44)     # PDT
    assert ST._pt_to_utc("2026-11-14", 8, 44) == (16, 44)     # PST


def test_a_premarket_rebuild_still_grades_from_the_open(tmp_path, monkeypatch):
    """07:06 PT is 14:06 UTC, inside RTH; 05:00 PT would be 12:00 and is clipped to 13:30."""
    import json
    d = tmp_path / "bars"
    d.mkdir()
    rows = [{"ts": f"2026-09-14 {h:02d}:{m:02d}:00+00:00", "o": 1, "h": 2, "l": 0, "c": 1}
            for h in range(0, 21) for m in (0, 30)]
    (d / "2026-09-14.json").write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(ST, "DATA", tmp_path)
    early = ST.load_bars("2026-09-14", ("intraday", (5, 0), (20, 0)))
    assert early[0]["ts"][11:16] == "13:30"
    late = ST.load_bars("2026-09-14", ("intraday", (8, 44), (20, 0)))
    assert late[0]["ts"][11:16] == "16:00"        # first bar at or after 15:44


def test_asia_and_rth_windows_do_not_overlap(tmp_path, monkeypatch):
    import json
    d = tmp_path / "bars"
    d.mkdir()
    rows = [{"ts": f"2026-09-14 {h:02d}:00:00+00:00", "o": 1, "h": 2, "l": 0, "c": 1} for h in range(24)]
    (d / "2026-09-14.json").write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(ST, "DATA", tmp_path)
    a = {r["ts"] for r in ST.load_bars("2026-09-14", ("asia", (0, 0), (13, 30)))}
    r = {r["ts"] for r in ST.load_bars("2026-09-14", ("rth", (13, 30), (20, 0)))}
    assert a and r and not (a & r)
