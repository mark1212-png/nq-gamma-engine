"""
discord_post.py -- rendering, limits and the gates that decide whether a plan goes out.

Nothing here touches the network. The transport is exercised only through target(), which is
pure environment resolution; send() is not called, because a test that posts to Discord is a
test that posts to Discord.

The fixtures are the SHAPES normalize() and opra_compare.py produce, cut down to what the
renderer reads. When either of those changes shape, these fail -- which is the point.
"""
import json
from pathlib import Path

import pytest

import discord_post as D


# ----------------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------------
@pytest.fixture
def report():
    """A normalize() report with one long card and one short, and no staleness."""
    return {
        "session": {"date": "2026-09-10", "kind": "RTH", "hours": 3.0953, "settle": "2026-09-09",
                    "expiries": 28, "oi_vintage": "final", "iv_atm": 0.15635,
                    "asof": "2026-09-10T16:54+00:00"},
        "reference": {"price": 29236.5, "settle": 29448.75, "source": "last print 16:54 UTC",
                      "stale": False, "vs_settle": -212.25},
        "flips": {"0dte": 29444.9, "near": 29333.9, "full": 29296.5},
        "net": {"0dte": -658, "near": -737, "full": -492},
        "gamma_at_spot": {"0dte": -406.0, "near": -628.0, "full": -516.0},
        "rails": {"lo": 29147.09, "hi": 29325.91, "scale": 0.3105, "label": "Rest of session",
                  "em": 89.41},
        "vex": {"total": 870.04, "note": "Net VEX <b>+870</b> per vol point."},
        "lead": "The reference is <b>29,236.50</b> and it sits below all three flips.",
        "caveat": "",
        "reading": [{"t": "", "s": "<b>29,100</b> is the heaviest strike within reach."},
                    {"t": "", "s": "Vol is ordinary; the weight is one day out."}],
        "setups": [
            {"name": "A · Upper-rail fade", "side": "short", "rank": "Primary",
             "tag": "rejection at the 29,320 node", "why": "The band's upper edge at <b>29,319</b>.",
             "tps": [29250.0, 29200.0, 29150.0], "entry": 29315.0, "tp": 29250.0, "sl": 29345.5},
            {"name": "B · Low retest", "side": "long", "rank": "Secondary",
             "tag": "a return to 29,100 that holds", "why": "<b>29,100</b> is the heaviest strike.",
             "tps": [29200.0, 29250.0, 29300.0], "entry": 29120.0, "tp": 29200.0, "sl": 29084.5},
        ],
        "ladder": {}, "history": [], "log": "",
    }


@pytest.fixture
def compare():
    """opra_compare.py output, cut to the fields opra_embed reads."""
    return {
        "date": "2026-09-09", "report_date": "2026-09-10",
        "unit": "USD millions of dealer gamma per 1% move",
        "sign_convention": "dealers long calls / short puts, net = C - P open interest (as NQ)",
        "books": {
            "SPX complex (SPX+SPXW)": {"within_1pct_musd": -24924.2, "ref": 7640.775,
                                       "panels": {"0DTE": {"flip": 7657.3}}, "stale": False},
            "QQQ": {"within_1pct_musd": 281.0, "ref": 716.29,
                    "panels": {"0DTE": {"flip": 716.4}}, "stale": True},
            "NQ": {"within_1pct_musd": 588.0, "ref": 29448.75,
                   "panels": {"0DTE": {"flip": 29430.6}}, "stale": False},
        },
        # present in the real file, and deliberately NOT rendered -- see opra_cross's docstring
        "cross": {"label": "SPX complex (SPX+SPXW+SPY)"},
        "hypothesis_status": "UNTESTED -- step 5; no prose may use the cross row until then",
    }


# ----------------------------------------------------------------------------------
# Prose conversion
# ----------------------------------------------------------------------------------
def test_md_converts_emphasis_and_drops_everything_else():
    assert D.md('<span class="x">a <b>29,100</b> and <i>b</i></span>') == "a **29,100** and *b*"


def test_md_unescapes_entities():
    assert D.md("29,100 &rarr; 29,200 &amp; back") == "29,100 → 29,200 & back"


def test_md_escapes_markdown_in_the_text_but_not_its_own_markers():
    """A stray underscore or asterisk in the prose must not italicise the rest of the message."""
    out = D.md("net_total went <b>*up*</b>")
    assert out == r"net\_total went **\*up\***"


def test_md_keeps_paragraph_breaks():
    assert D.md("one<br><br>two") == "one\n\ntwo"
    assert D.md("one<br>two") == "one two"


def test_md_empty():
    assert D.md("") == "" and D.md(None) == ""


def test_clip_trims_on_a_word_boundary():
    out = D.clip("the quick brown fox jumps", 15)
    assert len(out) <= 15 and out.endswith("…") and not out.startswith("the quick brown f")


def test_clip_leaves_short_strings_alone():
    assert D.clip("short", 100) == "short"


@pytest.mark.parametrize("v,want", [(29100.0, "29,100"), (29345.5, "29,345.5"),
                                    (29345.25, "29,345.25"), (None, D.DASH)])
def test_px(v, want):
    assert D.px(v) == want


def test_signed():
    assert D.signed(-658) == "-658" and D.signed(870) == "+870" and D.signed(None) == D.DASH


# ----------------------------------------------------------------------------------
# Embeds
# ----------------------------------------------------------------------------------
def test_header_carries_every_number_the_plan_is_built_on(report):
    e = D.header_embed(report)
    blob = json.dumps(e)
    for n in ("29,236.5", "29,444.9", "29,333.9", "29,296.5", "29,147.09", "29,325.91",
              "-658", "-737", "-492", "2026-09-09", "28 expiries"):
        assert n in blob, f"{n} missing from the header embed"


def test_header_colour_follows_the_sign_of_gamma_at_spot(report):
    assert D.header_embed(report)["color"] == D.C_NEG
    report["gamma_at_spot"] = {"0dte": 406.0, "near": 628.0, "full": 516.0}
    assert D.header_embed(report)["color"] == D.C_POS


def test_header_timestamp_gets_seconds(report):
    assert D.header_embed(report)["timestamp"] == "2026-09-10T16:54:00+00:00"


def test_header_survives_an_asia_build_with_no_0dte_panel(report):
    report["flips"].pop("0dte")
    report["gamma_at_spot"].pop("0dte")
    e = D.header_embed(report)
    assert "0DTE" not in e["fields"][1]["value"] and "near" in e["fields"][1]["value"]


def test_setup_card_colour_and_r_multiples(report):
    short, long_ = (D.setup_embed(c) for c in report["setups"])
    assert short["color"] == D.C_SHORT and long_["color"] == D.C_LONG
    tps = short["fields"][2]["value"]
    assert "T1 **29,250**" in tps and "(2.1R)" in tps      # 65 / 30.5
    assert "T3 **29,150**" in tps and "(5.4R)" in tps      # 165 / 30.5
    assert "risk 30.5" in short["fields"][1]["value"]


def test_setup_card_separates_the_trigger_from_the_reasoning(report):
    d = D.setup_embed(report["setups"][0])["description"]
    assert d.startswith("*rejection at the 29,320 node*") and "\n\n" in d


def test_setup_card_without_a_stop_does_not_divide_by_zero(report):
    c = dict(report["setups"][0], sl=None)
    e = D.setup_embed(c)
    assert "R)" not in e["fields"][2]["value"] and D.DASH in e["fields"][1]["value"]


def test_reading_embed_bullets_every_item(report):
    d = D.reading_embed(report)["description"]
    assert d.count("• ") == 2 and "**29,100**" in d


def test_reading_embed_is_none_when_the_sheet_has_no_reading(report):
    report["reading"] = []
    assert D.reading_embed(report) is None


def test_caveat_embed_only_when_there_is_a_caveat(report):
    assert D.caveat_embed(report) is None
    report["caveat"] = "<b>The numbers are fresh; the words are not.</b>"
    assert "**" in D.caveat_embed(report)["description"]


def test_opra_embed_reads_the_books_and_flags_stale_open_interest(compare):
    e = D.opra_embed(compare)
    assert "-24,924" in e["description"] and "7,657.30" in e["description"]
    assert "stale open interest: QQQ" in e["footer"]["text"]


def test_opra_embed_never_renders_the_untested_cross_row(compare):
    """compare.json's cross row carries hypothesis_status = UNTESTED and is not for prose."""
    e = D.opra_embed(compare)
    assert "SPX+SPXW+SPY" not in json.dumps(e)


def test_opra_embed_is_none_without_books():
    assert D.opra_embed({"books": {}}) is None and D.opra_embed({}) is None


# ----------------------------------------------------------------------------------
# Message assembly and Discord's limits
# ----------------------------------------------------------------------------------
def test_messages_split_header_cards_and_equities(report, compare):
    msgs = D.build_messages(report, cmp_=compare)
    assert len(msgs) == 3
    assert msgs[0]["embeds"][0]["title"].startswith("NQ game plan")
    assert len(msgs[1]["embeds"]) == 2                       # one embed per card
    assert msgs[2]["embeds"][0]["title"].startswith("Equity cross-reference")


def test_messages_without_equities(report):
    assert len(D.build_messages(report)) == 2


def test_every_message_is_inside_discord_limits(report, compare):
    for m in D.build_messages(report, mention="<@&1>", cmp_=compare):
        assert len(m["content"]) <= D.LIM_CONTENT
        assert len(m["embeds"]) <= D.LIM_EMBEDS
        assert sum(D._embed_len(e) for e in m["embeds"]) <= D.LIM_MESSAGE
        for e in m["embeds"]:
            assert len(e.get("title", "")) <= D.LIM_TITLE
            assert len(e.get("description", "")) <= D.LIM_DESC
            for f in e.get("fields", []):
                assert len(f["name"]) <= D.LIM_NAME and len(f["value"]) <= D.LIM_FIELD


def test_a_very_long_sheet_is_trimmed_to_fit(report):
    report["lead"] = "x " * 3000
    report["reading"] = [{"t": "", "s": "y " * 3000}] * 4
    for m in D.build_messages(report):
        assert sum(D._embed_len(e) for e in m["embeds"]) <= D.LIM_MESSAGE


def test_fit_trims_the_tail_not_the_header():
    head = {"title": "head", "description": "h" * 3000, "fields": []}
    tail = {"title": "tail", "description": "t" * 4000, "fields": []}
    out = D._fit([head, tail])
    assert out[0]["description"] == "h" * 3000                 # untouched
    assert len(out[1]["description"]) < 4000


def test_mention_is_only_on_the_first_message_and_only_parses_roles(report):
    msgs = D.build_messages(report, mention="<@&123>")
    assert msgs[0]["content"] == "<@&123>" and msgs[0]["allowed_mentions"]["parse"] == ["roles"]
    assert msgs[1]["content"] == "" and msgs[1]["allowed_mentions"]["parse"] == []


def test_no_mention_means_no_pings(report):
    for m in D.build_messages(report):
        assert m["allowed_mentions"]["parse"] == []


# ----------------------------------------------------------------------------------
# The gates
# ----------------------------------------------------------------------------------
def test_is_stale_reads_build_pys_drift_banner(report, monkeypatch):
    import publication
    monkeypatch.setattr(publication, "validated", lambda *a: True)
    assert D.is_stale(report) is False
    report["caveat"] = ("<b>The numbers are fresh; the words are not.</b> ... were written for "
                        "the 2026-09-09 snapshot and have not been rewritten.")
    assert D.is_stale(report) is True


def test_fingerprint_is_stable_and_moves_with_the_plan(report):
    a = D.fingerprint(report)
    assert a == D.fingerprint(json.loads(json.dumps(report)))   # same plan, same hash
    report["setups"][0]["entry"] = 29316.0
    assert D.fingerprint(report) != a                           # a moved entry is a new post


def test_fingerprint_moves_when_the_words_change(report):
    a = D.fingerprint(report)
    report["lead"] = "Something else entirely."
    assert D.fingerprint(report) != a


# ----------------------------------------------------------------------------------
# Transport configuration (no network)
# ----------------------------------------------------------------------------------
def test_target_prefers_the_webhook(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "9")
    kind, url, headers = D.target()
    assert kind == "webhook" and url.endswith("?wait=true") and headers == {}


def test_target_falls_back_to_the_bot(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "9")
    kind, url, headers = D.target()
    assert kind == "bot" and url.endswith("/channels/9/messages")
    assert headers["Authorization"] == "Bot tok"


def test_target_rejects_a_url_that_is_not_a_discord_webhook(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.com/hook")
    with pytest.raises(RuntimeError, match="not a Discord webhook"):
        D.target()


def test_target_with_nothing_configured_says_so(monkeypatch):
    for k in ("DISCORD_WEBHOOK_URL", "DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="no Discord target"):
        D.target()


def test_multipart_carries_the_payload_and_the_file(tmp_path):
    f = tmp_path / "2026-09-10-teddyai.html"
    f.write_text("<html>sheet</html>", encoding="utf-8")
    body, ctype = D._multipart({"content": "x", "embeds": []}, [f])
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="payload_json"' in body and b'filename="2026-09-10-teddyai.html"' in body
    assert b"<html>sheet</html>" in body
    assert b'"attachments": [{"id": 0' in body


# ----------------------------------------------------------------------------------
# post_gameplan's decisions, with the report and the ledger faked
# ----------------------------------------------------------------------------------
@pytest.fixture
def wired(monkeypatch, report, tmp_path):
    """post_gameplan with a fixed report, a temporary ledger and send() disabled.

    render_images is stubbed to return nothing, so the default image format falls back to the
    embed rendering. A unit test must never start a browser: it would be slow, it would depend
    on Chrome being installed, and it would test the screenshot rather than the decision."""
    sent = []
    import publication
    monkeypatch.setattr(publication, "validated", lambda *a: True)
    monkeypatch.setattr(D, "load_report", lambda: report)
    monkeypatch.setattr(D, "opra_cross", lambda R: None)
    monkeypatch.setattr(D, "render_images", lambda R, parts=None, log=None: [])
    monkeypatch.setattr(D, "NOTES", tmp_path)
    monkeypatch.setattr(D, "LEDGER", tmp_path / "discord-posted.json")
    monkeypatch.setattr(D, "PREVIEW", tmp_path / "discord-preview.json")
    monkeypatch.setattr(D, "LOG", tmp_path / "discord.log")
    monkeypatch.setattr(D, "send", lambda p, files=None: sent.append(p) or {"id": str(len(sent))})
    monkeypatch.setattr(D.time, "sleep", lambda s: None)
    monkeypatch.delenv("DISCORD_MENTION", raising=False)
    return sent


def test_a_stale_sheet_is_never_posted(wired, report):
    report["caveat"] = "<b>The numbers are fresh; the words are not.</b> not been rewritten."
    r = D.post_gameplan()
    assert r["posted"] is False and r["reason"] == "stale" and wired == []


def test_a_stale_sheet_still_renders_in_a_dry_run(wired, report):
    report["caveat"] = "<b>The numbers are fresh; the words are not.</b> not been rewritten."
    r = D.post_gameplan(dry_run=True)
    assert r["stale"] is True and r["messages"] and wired == []


def test_allow_stale_overrides_the_gate(wired, report):
    report["caveat"] = "<b>The numbers are fresh; the words are not.</b> not been rewritten."
    assert D.post_gameplan(allow_stale=True)["posted"] is True and len(wired) == 2


def test_the_same_plan_is_not_posted_twice(wired):
    assert D.post_gameplan()["posted"] is True
    assert len(wired) == 2
    r = D.post_gameplan()
    assert r["posted"] is False and r["reason"] == "duplicate" and len(wired) == 2


def test_force_reposts(wired):
    D.post_gameplan()
    assert D.post_gameplan(force=True)["posted"] is True and len(wired) == 4


def test_a_changed_plan_posts_again(wired, report):
    D.post_gameplan()
    report["setups"][0]["entry"] = 29316.0
    assert D.post_gameplan()["posted"] is True and len(wired) == 4


def test_a_half_sent_post_resumes_rather_than_duplicating(wired, monkeypatch, report):
    """The second message fails; the retry sends only the second message."""
    calls = {"n": 0}

    def flaky(p, files=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("Discord HTTP 500")
        wired.append(p)
        return {"id": str(calls["n"])}

    monkeypatch.setattr(D, "send", flaky)
    with pytest.raises(RuntimeError):
        D.post_gameplan()
    assert len(wired) == 1                      # only the header went out

    monkeypatch.setattr(D, "send", lambda p, files=None: wired.append(p) or {"id": "x"})
    assert D.post_gameplan()["posted"] is True
    assert len(wired) == 2                      # the cards, and NOT the header again


def test_dry_run_writes_a_preview_and_sends_nothing(wired, tmp_path):
    r = D.post_gameplan(dry_run=True)
    assert r["posted"] is False and r["reason"] == "dry-run" and wired == []
    assert json.loads((tmp_path / "discord-preview.json").read_text(encoding="utf-8"))


def test_render_text_is_readable(report):
    out = D.render_text(D.build_messages(report))
    assert "=== message 1" in out and "NQ game plan" in out and "Entry (short)" in out


# ----------------------------------------------------------------------------------
# Image format -- the sheet's own sections, one per message
# ----------------------------------------------------------------------------------
@pytest.fixture
def images(tmp_path):
    out = []
    for name in ("overview", "gameplan", "reading"):
        p = tmp_path / f"{name}.png"
        p.write_bytes(bytes([137, 80, 78, 71, 13, 10, 26, 10]) + name.encode())
        out.append(p)
    return out


def test_header_line_carries_the_levels_as_text(report):
    """An image is not searchable and does not show in a notification preview."""
    line = D.header_line(report)
    assert "2026-09-10" in line and "Rest of session" in line
    assert "29,236.5" in line and "29,445" in line and "29,147–29,326" in line
    assert "2026-09-09 final" in line and len(line) <= D.LIM_CONTENT


def test_header_line_rounds_flips_and_rails_to_whole_points(report):
    """The sheet's stat strip prints 29,334, not 29,333.9 -- a summary line must not imply
    a precision the flip does not have."""
    line = D.header_line(report)
    assert "29,333.9" not in line and "29,325.91" not in line


def test_image_messages_are_one_picture_each(report, images):
    msgs = D.build_messages(report, images=images)
    assert len(msgs) == 3
    assert [m["_files"] for m in msgs] == [[images[0]], [images[1]], [images[2]]]
    assert msgs[0]["content"].startswith("**NQ game plan")
    assert all(m["embeds"] == [] for m in msgs)
    assert msgs[1]["content"] == "" and msgs[2]["content"] == ""


def test_image_messages_keep_the_opra_embed(report, images, compare):
    msgs = D.build_messages(report, images=images, cmp_=compare)
    assert len(msgs) == 4
    assert msgs[3]["embeds"][0]["title"].startswith("Equity cross-reference")
    assert "_files" not in msgs[3]


def test_image_mention_only_pings_on_the_first_message(report, images):
    msgs = D.build_messages(report, mention="<@&123>", images=images)
    assert msgs[0]["content"].startswith("<@&123> ")
    assert msgs[0]["allowed_mentions"]["parse"] == ["roles"]
    assert all(m["allowed_mentions"]["parse"] == [] for m in msgs[1:])


def test_image_post_sends_each_picture(monkeypatch, wired, report, images):
    monkeypatch.setattr(D, "render_images", lambda R, parts=None, log=None: images)
    files_seen = []
    monkeypatch.setattr(D, "send",
                        lambda p, files=None: files_seen.append(files) or {"id": "1"})
    r = D.post_gameplan(fmt="image")
    assert r["posted"] is True and r["messages"] == 3
    assert files_seen == [[images[0]], [images[1]], [images[2]]]


def test_a_failed_render_falls_back_to_embeds_rather_than_not_posting(wired, report):
    """render_images returns [] on any failure. The plan still goes out, plainer."""
    r = D.post_gameplan(fmt="image")
    assert r["posted"] is True and r["messages"] == 2      # the embed rendering


def test_render_images_never_raises(monkeypatch, tmp_path):
    """A broken renderer is a log line and an empty list, never an exception."""
    monkeypatch.setattr(D, "LOG", tmp_path / "discord.log")
    monkeypatch.setattr(D, "NOTES", tmp_path)
    import sys
    monkeypatch.setitem(sys.modules, "sheet_image", None)   # import raises
    assert D.render_images({"session": {}}) == []


def test_format_embeds_never_renders(monkeypatch, wired, report):
    monkeypatch.setattr(D, "render_images",
                        lambda R, parts=None, log=None: pytest.fail("embeds must not render"))
    assert D.post_gameplan(fmt="embeds")["posted"] is True


def test_format_both_puts_the_embeds_first(monkeypatch, wired, report, images):
    monkeypatch.setattr(D, "render_images", lambda R, parts=None, log=None: images)
    r = D.post_gameplan(fmt="both")
    assert r["posted"] is True and r["messages"] == 5     # 2 embed messages + 3 pictures


def test_the_sheet_attachment_rides_on_the_last_message(monkeypatch, wired, report, images, tmp_path):
    sheet = tmp_path / "2026-09-10-teddyai.html"
    sheet.write_text("<html>sheet</html>", encoding="utf-8")
    monkeypatch.setattr(D, "render_images", lambda R, parts=None, log=None: images)
    monkeypatch.setattr(D, "sheet_path", lambda R: sheet)
    files_seen = []
    monkeypatch.setattr(D, "send",
                        lambda p, files=None: files_seen.append(files) or {"id": "1"})
    D.post_gameplan(fmt="image", attach_sheet=True)
    assert files_seen[-1] == [images[2], sheet]
    assert files_seen[0] == [images[0]]


def test_dry_run_reports_the_images(monkeypatch, wired, images):
    monkeypatch.setattr(D, "render_images", lambda R, parts=None, log=None: images)
    r = D.post_gameplan(fmt="image", dry_run=True)
    assert [Path(p).name for p in r["images"]] == ["overview.png", "gameplan.png", "reading.png"]


def test_render_text_lists_attachments(report, images):
    out = D.render_text(D.build_messages(report, images=images))
    assert "[attachment] overview.png" in out and "[attachment] gameplan.png" in out
