"""
sheet_image.py -- splitting the sheet into standalone pages, and the browser lookup.

The split is the part that can silently rot: it depends on build.py's markup (one .wrap, one
<section class="sec"> per section, an <h1>/<h2> inside each). If build.py changes shape these
fail here rather than in a channel at 6:05 in the morning.

One test does render for real. It is skipped when no Chrome or Edge is installed, and it uses
the smallest section it can find, so it costs about a second.
"""
import re

import pytest

import sheet_image as S


SHEET = """<!doctype html><meta charset="utf-8">
<style>.wrap{max-width:1100px}.sec{margin:20px 0}</style>
<div class="wrap">
<div class="note w"><b>A caveat.</b></div>
<header class="mast"><h1>Basilisk <span>/ Sep 10</span></h1></header>
<div class="strip">tiles</div>
<section class="sec"><h2>Reading the day</h2><ul><li>a bullet</li></ul></section>
<section class="sec"><h2>Gameplan</h2><div class="pl">card A</div></section>
<section class="sec"><h2>Teddy Analysis</h2><div class="grid">ladder</div></section>
<section class="sec"><h2>Market state</h2><div>state</div></section>
</div>"""


# ----------------------------------------------------------------------------------
# Naming
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("label,want", [
    ("Reading the day", "reading"),
    ("Gameplan", "gameplan"),
    ("Teddy Analysis", "analysis"),
    ("Market state", "state"),
    ("Something New", "something-new"),
    ("", "part"),
])
def test_slug(label, want):
    assert S._slug(label) == want


# ----------------------------------------------------------------------------------
# The split
# ----------------------------------------------------------------------------------
def test_split_names_every_section():
    assert [n for n, _ in S.split(SHEET)] == \
           ["overview", "reading", "gameplan", "analysis", "state"]


def test_overview_is_everything_above_the_first_section():
    page = dict(S.split(SHEET))["overview"]
    assert "Basilisk" in page and "A caveat." in page and "strip" in page
    assert "Reading the day" not in page and "Gameplan" not in page


def test_each_part_carries_the_sheets_own_css_and_wrapper():
    """A fragment rendered without the sheet's <style> is unstyled text, not the sheet."""
    for name, page in S.split(SHEET):
        assert "<style>" in page and ".wrap{max-width:1100px}" in page
        assert '<div class="wrap">' in page
        assert page.count("<div") == page.count("</div>"), f"{name} has unbalanced divs"


def test_a_part_contains_only_its_own_section():
    page = dict(S.split(SHEET))["gameplan"]
    assert "card A" in page and "Gameplan" in page
    assert "a bullet" not in page and "ladder" not in page


def test_split_rejects_a_sheet_it_does_not_recognise():
    with pytest.raises(RuntimeError, match="no .wrap container"):
        S.split("<html><body>nothing familiar</body></html>")


def test_split_survives_a_section_with_no_heading():
    html = SHEET.replace("<h2>Market state</h2>", "")
    names = [n for n, _ in S.split(html)]
    assert names[-1] == "part4" and len(names) == 5


# ----------------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------------
def test_gameplan_renders_narrower_so_the_cards_go_two_across():
    """At the full width the card grid strands D alone beside an empty column."""
    assert S.PART_WIDTH["gameplan"] < S.WIDTH


def test_the_default_post_is_the_cards_alone():
    """One image, not three. The levels that were in the overview image are carried as text by
    discord_post.header_line on the same message."""
    assert S.DEFAULT_PARTS == ("gameplan",)


def test_browser_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "mybrowser.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("TEDDY_BROWSER", str(fake))
    assert S.browser() == str(fake)


def test_browser_ignores_an_override_that_does_not_exist(monkeypatch, tmp_path):
    """A stale TEDDY_BROWSER must fall through to the real search, not fail the post."""
    monkeypatch.setenv("TEDDY_BROWSER", str(tmp_path / "gone.exe"))
    try:
        found = S.browser()
    except RuntimeError:
        return                                  # no browser on this machine: also correct
    assert found != str(tmp_path / "gone.exe")


# ----------------------------------------------------------------------------------
# One real render
# ----------------------------------------------------------------------------------
def _have_browser():
    try:
        S.browser()
        return True
    except RuntimeError:
        return False


@pytest.mark.skipif(not _have_browser(), reason="no Chrome or Edge installed")
def test_a_real_render_produces_a_trimmed_png(tmp_path):
    from PIL import Image
    sheet = tmp_path / "sheet.html"
    sheet.write_text(SHEET, encoding="utf-8")

    files = S.render(sheet, tmp_path / "out", parts=["gameplan"], scale=1)
    assert len(files) == 1 and files[0].name == "gameplan.png"

    with Image.open(files[0]) as im:
        # trimmed to content plus the pad, so nowhere near the 2600px capture height
        assert im.height < 400, f"not trimmed: {im.size}"
        assert im.width > 100


@pytest.mark.skipif(not _have_browser(), reason="no Chrome or Edge installed")
def test_an_unknown_part_is_skipped_not_raised(tmp_path):
    sheet = tmp_path / "sheet.html"
    sheet.write_text(SHEET, encoding="utf-8")
    said = []
    files = S.render(sheet, tmp_path / "out", parts=["nosuchsection"], log=said.append)
    assert files == []
    assert any("no 'nosuchsection' section" in s for s in said)


def test_the_screenshot_path_is_absolute(monkeypatch, tmp_path):
    """A relative --screenshot is not resolved against the working directory: Chrome logs
    "cannot find the path specified" and still exits 0, so every part silently produced no
    file. Pinned because the failure looks like a browser problem, not a path problem."""
    seen = {}

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        (tmp_path / "x.png").write_bytes(b"png")
        return Done()

    monkeypatch.setattr(S.subprocess, "run", fake_run)
    monkeypatch.setattr(S, "browser", lambda: "chrome.exe")
    page = tmp_path / "p.html"
    page.write_text("<div class='wrap'></div>", encoding="utf-8")

    ok, why = S._shoot(page, tmp_path / "x.png", 1100, 2600, 2, tmp_path / "profile")
    assert ok and why == ""
    shot = next(a for a in seen["cmd"] if a.startswith("--screenshot="))
    from pathlib import Path as P
    assert P(shot.split("=", 1)[1]).is_absolute()


def test_a_browser_that_writes_nothing_reports_its_error(monkeypatch, tmp_path):
    class Failed:
        returncode = 0
        stdout = ""
        stderr = "[123:ERROR:headless_command_handler.cc:266] Failed to write file: (0x3)\n"

    monkeypatch.setattr(S.subprocess, "run", lambda cmd, **kw: Failed())
    monkeypatch.setattr(S, "browser", lambda: "chrome.exe")
    page = tmp_path / "p.html"
    page.write_text("<div></div>", encoding="utf-8")

    ok, why = S._shoot(page, tmp_path / "missing.png", 1100, 2600, 2, tmp_path / "profile")
    assert ok is False and "Failed to write file" in why


@pytest.mark.skipif(not _have_browser(), reason="no Chrome or Edge installed")
def test_parts_all_keeps_the_default_order_and_appends_the_rest(tmp_path):
    sheet = tmp_path / "sheet.html"
    sheet.write_text(SHEET, encoding="utf-8")
    files = S.render(sheet, tmp_path / "out", parts="all", scale=1)
    assert files[0].stem == "gameplan"                       # the default leads
    assert sorted(f.stem for f in files) ==            ["analysis", "gameplan", "overview", "reading", "state"]
