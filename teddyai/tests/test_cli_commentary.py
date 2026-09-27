"""
cli_commentary.py -- writing the plan on a subscription instead of the metered API.

No subprocess is ever started here and no network is touched: subprocess.run is stubbed in
every test that reaches it. What is under test is the seam -- that a reply from the CLI enters
the SAME validation chain the API path uses, that the API is not called when it should not be,
and that the two failures which look like bugs (a lapsed login, a hit rate limit) are named
rather than reported as a bare exit code.
"""
import json
import subprocess

import pytest

import cli_commentary as C


# ----------------------------------------------------------------------------------
# Finding the CLI
# ----------------------------------------------------------------------------------
def test_which_honours_the_env_override(monkeypatch, tmp_path):
    exe = tmp_path / "claude.cmd"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CLI", str(exe))
    assert C.which() == str(exe)


def test_which_searches_the_path(monkeypatch):
    monkeypatch.delenv("CLAUDE_CLI", raising=False)
    monkeypatch.setattr(C.shutil, "which", lambda n: r"C:\npm\claude.cmd" if n == "claude.cmd" else None)
    assert C.which() == r"C:\npm\claude.cmd"


def test_which_looks_for_the_windows_cmd_shim(monkeypatch):
    """npm installs it as claude.cmd on Windows; searching only for "claude" finds nothing."""
    assert "claude.cmd" in C.CANDIDATES


def test_available_explains_how_to_install_when_it_is_missing(monkeypatch):
    monkeypatch.setattr(C, "which", lambda: None)
    ok, why = C.available()
    assert ok is False
    assert "npm install -g @anthropic-ai/claude-code" in why and "claude login" in why


def test_available_reports_a_binary_that_will_not_run(monkeypatch):
    monkeypatch.setattr(C, "which", lambda: "claude")
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    ok, why = C.available()
    assert ok is False and "would not run" in why


def test_available_ok(monkeypatch):
    class R:
        returncode, stdout, stderr = 0, "1.2.3 (Claude Code)\n", ""
    monkeypatch.setattr(C, "which", lambda: "claude")
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **k: R())
    ok, why = C.available()
    assert ok is True and "1.2.3" in why


# ----------------------------------------------------------------------------------
# Getting the reply out of the CLI's envelope
# ----------------------------------------------------------------------------------
def test_text_from_the_documented_result_field():
    assert C._text_from(json.dumps({"result": '{"lead":"x"}'})) == '{"lead":"x"}'


def test_text_from_content_blocks():
    doc = {"content": [{"type": "text", "text": '{"lead":'}, {"type": "text", "text": '"x"}'}]}
    assert C._text_from(json.dumps(doc)) == '{"lead":"x"}'


def test_text_from_a_stream_of_events_takes_the_last():
    doc = [{"type": "system"}, {"result": "first"}, {"result": '{"lead":"last"}'}]
    assert C._text_from(json.dumps(doc)) == '{"lead":"last"}'


def test_text_from_plain_output_passes_through():
    """An older CLI, or --output-format text: hand it back and let _extract_json cope."""
    assert C._text_from('{"lead":"x"}') == '{"lead":"x"}'


def test_text_from_an_unknown_shape_degrades_rather_than_raising():
    """The envelope has moved between CLI versions. A shape change must not break the plan."""
    blob = json.dumps({"something": "unexpected"})
    assert C._text_from(blob) == blob


def test_text_from_surfaces_an_error_envelope():
    with pytest.raises(RuntimeError, match="the CLI returned an error"):
        C._text_from(json.dumps({"is_error": True, "error": "context too long"}))


def test_text_from_empty():
    assert C._text_from("") == "" and C._text_from(None) == ""


# ----------------------------------------------------------------------------------
# send()
# ----------------------------------------------------------------------------------
@pytest.fixture
def cli(monkeypatch):
    """A stubbed CLI. Returns the dict the test can inspect / set up."""
    box = {"returncode": 0, "stdout": json.dumps({"result": '{"lead":"ok"}'}), "stderr": ""}

    class R:
        pass

    def fake_run(cmd, **kw):
        box["cmd"] = cmd
        box["input"] = kw.get("input")
        box["cwd"] = kw.get("cwd")
        r = R()
        r.returncode, r.stdout, r.stderr = box["returncode"], box["stdout"], box["stderr"]
        return r

    monkeypatch.setattr(C, "which", lambda: "claude.cmd")
    monkeypatch.setattr(C.subprocess, "run", fake_run)
    return box


def test_send_returns_the_reply(cli):
    assert C.send("the prompt") == '{"lead":"ok"}'


def test_the_prompt_goes_on_stdin_not_the_command_line(cli):
    """Windows caps a command line at 32,767 characters and the payload is tens of kilobytes
    of JSON. Passing it as an argument works in testing and fails on a real sheet."""
    big = "x" * 60000
    C.send(big)
    assert cli["input"] == big
    assert not any(big in str(part) for part in cli["cmd"])


def test_send_asks_for_headless_json_and_pins_the_system_prompt(cli):
    C.send("p")
    assert "-p" in cli["cmd"]
    assert cli["cmd"][cli["cmd"].index("--output-format") + 1] == "json"
    sysmsg = cli["cmd"][cli["cmd"].index("--system-prompt") + 1]
    assert "exactly one JSON object" in sysmsg and "Do not use any tools" in sysmsg


def test_the_system_prompt_replaces_rather_than_appends(cli):
    """--append-system-prompt leaves Claude Code's whole coding-agent brief underneath the
    sheet's instructions, and the model failed the narrative check three rounds running."""
    C.send("p")
    assert "--system-prompt" in cli["cmd"]
    assert "--append-system-prompt" not in cli["cmd"]
    assert "--exclude-dynamic-system-prompt-sections" in cli["cmd"]


def test_send_denies_the_tools(cli):
    """The plan is written from the payload, not from whatever the loop decides to go and read."""
    C.send("p")
    denied = cli["cmd"][cli["cmd"].index("--disallowed-tools") + 1]
    for tool in ("Read", "Bash", "WebSearch", "Task"):
        assert tool in denied


def test_send_can_pin_a_model(cli, monkeypatch):
    monkeypatch.setenv("CC_CLI_MODEL", "claude-opus-5")
    C.send("p")
    assert cli["cmd"][cli["cmd"].index("--model") + 1] == "claude-opus-5"


def test_send_without_the_cli_says_how_to_install_it(monkeypatch):
    monkeypatch.setattr(C, "which", lambda: None)
    with pytest.raises(RuntimeError, match="npm install"):
        C.send("p")


def test_a_lapsed_login_is_named(cli):
    cli["returncode"], cli["stderr"] = 1, "Error: Not authenticated. Please run `claude login`."
    with pytest.raises(RuntimeError, match="not logged in"):
        C.send("p")


def test_a_rate_limit_is_named(cli):
    cli["returncode"], cli["stderr"] = 1, "usage limit reached, resets at 5pm"
    with pytest.raises(RuntimeError, match="subscription limit reached"):
        C.send("p")


def test_any_other_failure_carries_the_output(cli):
    cli["returncode"], cli["stderr"] = 2, "some other breakage"
    with pytest.raises(RuntimeError, match="exited 2.*some other breakage"):
        C.send("p")


def test_empty_stdout_is_a_failure_not_an_empty_plan(cli):
    cli["stdout"] = "  "
    with pytest.raises(RuntimeError, match="returned nothing"):
        C.send("p")


def test_a_timeout_says_which_knob_to_turn(monkeypatch):
    monkeypatch.setattr(C, "which", lambda: "claude")
    monkeypatch.setattr(C.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("claude", 300)))
    with pytest.raises(RuntimeError, match="CC_CLI_TIMEOUT"):
        C.send("p")


# ----------------------------------------------------------------------------------
# The seam: a CLI reply is validated exactly like an API reply
# ----------------------------------------------------------------------------------
class _Reached(Exception):
    """Carries the text that arrived at the validation chain."""
    def __init__(self, text):
        self.text = text


@pytest.fixture
def cc(monkeypatch):
    """command_center with the file reads and the reply log stubbed, and _extract_json
    replaced by a tripwire that reports what text reached it."""
    import command_center as cc_mod
    monkeypatch.setattr(cc_mod, "_read", lambda p: '{"settle": "2026-09-09", "report_date": "2026-09-10"}')
    monkeypatch.setattr(cc_mod, "_tape_summary", lambda T: "")
    monkeypatch.setattr(cc_mod, "_state_doc", lambda: {"trade_gate": {"verdict": "normal"}})
    import interpret
    monkeypatch.setattr(interpret, "verify_commentary", lambda *args: ([], []))
    monkeypatch.setattr(interpret, "check_card_levels", lambda *args: (True, []))
    monkeypatch.setattr(cc_mod, "_save_reply", lambda *a, **k: None)
    monkeypatch.setattr(cc_mod, "_log", lambda *a, **k: None)
    monkeypatch.setattr(cc_mod, "_extract_json",
                        lambda text: (_ for _ in ()).throw(_Reached(text)))
    return cc_mod


def test_a_cli_reply_reaches_the_validation_chain(cc):
    """The whole point: the sender changes, nothing else does."""
    with pytest.raises(_Reached) as e:
        cc.regenerate_commentary(send=lambda msg: "REPLY-FROM-CLI")
    assert e.value.text == "REPLY-FROM-CLI"


def test_a_send_callable_means_the_api_is_never_called(cc, monkeypatch):
    monkeypatch.setattr(cc, "_send_api",
                        lambda *a, **k: pytest.fail("the API must not be called in cli mode"))
    with pytest.raises(_Reached):
        cc.regenerate_commentary(send=lambda msg: "x")


def test_without_a_sender_it_still_uses_the_api(cc, monkeypatch):
    monkeypatch.setattr(cc, "_send_api", lambda msg, rnd=0: "REPLY-FROM-API")
    with pytest.raises(_Reached) as e:
        cc.regenerate_commentary()
    assert e.value.text == "REPLY-FROM-API"


def test_the_sender_receives_the_same_prompt_the_api_would_get(cc):
    """Not a different, weaker brief -- the identical payload: the house prompt and the sheet."""
    seen = {}

    def spy(msg):
        seen["msg"] = msg
        return "x"

    with pytest.raises(_Reached):
        cc.regenerate_commentary(send=spy)
    assert cc.PROMPT in seen["msg"]
    assert "trinity.json:" in seen["msg"] and '"settle": "2026-09-09"' in seen["msg"]


def test_cli_mode_needs_no_anthropic_key(cc, monkeypatch):
    """The key check lives in _send_api now, so a subscription run works without one."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(_Reached):
        cc.regenerate_commentary(send=lambda msg: "x")


def test_the_api_path_still_demands_its_key(cc, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY not set"):
        cc.regenerate_commentary()


# ----------------------------------------------------------------------------------
# A reply that will not parse is a rejected reply, not a lost session
# ----------------------------------------------------------------------------------
@pytest.fixture
def cc_raw(monkeypatch):
    """Like `cc`, but keeps the REAL _extract_json -- these tests are about parsing itself."""
    import command_center as cc_mod
    monkeypatch.setattr(cc_mod, "_read", lambda p: '{"settle": "2026-09-09", "report_date": "2026-09-10"}')
    monkeypatch.setattr(cc_mod, "_tape_summary", lambda T: "")
    monkeypatch.setattr(cc_mod, "_state_doc", lambda: {"trade_gate": {"verdict": "normal"}})
    import interpret
    monkeypatch.setattr(interpret, "verify_commentary", lambda *args: ([], []))
    monkeypatch.setattr(interpret, "check_card_levels", lambda *args: (True, []))
    monkeypatch.setattr(cc_mod, "_save_reply", lambda *a, **k: None)
    monkeypatch.setattr(cc_mod, "_log", lambda *a, **k: None)
    return cc_mod


def test_unparseable_json_asks_again_instead_of_aborting(cc_raw):
    """2026-09-10: the CLI closed a setup object one key early -- "tp":[...]},"why": -- and the
    whole run died on the JSONDecodeError, costing the plan. It should cost a round."""
    replies = iter(['{"lead": "broken",}', '{"parsed": true}'])
    seen = []

    def spy(msg):
        seen.append(msg)
        return next(replies)

    # the second reply parses, so the run gets past _extract_json and stops at the key check --
    # which is proof the retry happened AND that the retry's reply was parsed
    with pytest.raises(RuntimeError, match="has no 'lead'"):
        cc_raw.regenerate_commentary(send=spy)
    assert len(seen) == 2
    assert "not valid JSON" in seen[1] and "brace" in seen[1]


def test_the_real_malformed_reply_from_the_cli_is_retried(cc_raw):
    """The exact shape that broke it: a setup object closed before its "why" key."""
    bad = '{"lead":"x","reading":["y"],"setups":[{"name":"A","tp":[1,2,3]},"why":"z"}]}'
    seen = []

    def spy(msg):
        seen.append(msg)
        return bad if len(seen) == 1 else '{"parsed": true}'

    with pytest.raises(RuntimeError, match="has no 'lead'"):
        cc_raw.regenerate_commentary(send=spy)
    assert len(seen) == 2


def test_it_gives_up_after_the_corrective_rounds(cc_raw):
    """Three bad replies is a real failure -- surface it rather than looping forever."""
    seen = []

    def spy(msg):
        seen.append(msg)
        return '{"lead": "still broken",}'

    with pytest.raises(RuntimeError, match="did not parse"):
        cc_raw.regenerate_commentary(send=spy)
    assert len(seen) == 3                       # rounds 0, 1, 2 then give up


def test_a_parse_retry_keeps_the_same_sender(cc_raw, monkeypatch):
    """The retry must not silently fall back to the billed API path."""
    monkeypatch.setattr(cc_raw, "_send_api",
                        lambda *a, **k: pytest.fail("a retry must not switch to the API"))
    # both shapes _extract_json rejects -- a bad delimiter and a truncated object
    for reply in ('{"lead": "still broken",}', "not json at all {"):
        with pytest.raises(RuntimeError, match="did not parse|unterminated"):
            cc_raw.regenerate_commentary(send=lambda msg, r=reply: r)


# ----------------------------------------------------------------------------------
# The stop override is our edit, so the model gets a round to answer it
# ----------------------------------------------------------------------------------
OVERRIDDEN = 29685.0            # what _struct_stop returns for the far-wall card


@pytest.fixture
def far_wall(cc_raw, monkeypatch):
    """A reply whose cards are fine as written, and a map that widens one stop until they are
    not -- the exact 2026-09-10 shape: entry 29,590 / stop 29,630 (TP1 2.25R) becomes
    entry 29,590 / stop 29,685 (TP1 0.95R) the moment the map overrides it."""
    card = {"name": "D - 29,600 wall fade", "side": "short", "entry": 29590, "sl": 29630,
            "tp": [29500, 29450, 29400], "why": "w", "tag": "t", "rank": "Opportunistic"}
    reply = json.dumps({"lead": "l", "reading": ["r"], "setups": [card] * 4, "vex_note": ""})

    def check(C, gate=None):
        # passes as written; fails only once OUR override has widened the stop
        return ("card D - 29,600 wall fade TP1 is under 1.2R"
                if any(c.get("sl") == OVERRIDDEN for c in C["setups"]) else "")

    monkeypatch.setattr(cc_raw, "_check_cards", check)
    monkeypatch.setattr(cc_raw, "_structure_check", lambda s, T: ("far wall", []))
    monkeypatch.setattr(cc_raw, "_day_bars", lambda *a, **k: ([], None))
    monkeypatch.setattr(cc_raw, "_struct_stop", lambda c, T, bars: {"stop": OVERRIDDEN})
    return cc_raw, reply


def test_a_card_broken_by_our_own_stop_override_gets_another_round(far_wall):
    """2026-09-10: the last-round override widened a far-wall stop from 40 to 95 points, TP1
    fell under 1.2R, and the reply was binned with no round left -- four plans lost that way,
    two of them unattended, while the other three cards were fine."""
    cc_raw, reply = far_wall
    seen = []

    with pytest.raises(RuntimeError):
        cc_raw.regenerate_commentary(send=lambda m: seen.append(m) or reply)

    # rounds 0 and 1 are the "stops are structural" nudge; the override lands on round 2, and
    # the card it breaks buys a fourth round carrying the computed stops back to the model
    assert len(seen) == 4, f"expected a 4th round, got {len(seen)}"
    last = seen[-1]
    assert "recomputed from the gamma map and are FINAL" in last
    assert str(OVERRIDDEN) in last or "29685" in last
    assert "1.2R" in last and "TP2 and TP3 beyond it" in last


def test_the_extra_round_carries_the_entries_too(far_wall):
    """Handing back a stop without its entry invites the model to move the entry instead."""
    cc_raw, reply = far_wall
    seen = []
    with pytest.raises(RuntimeError):
        cc_raw.regenerate_commentary(send=lambda m: seen.append(m) or reply)
    assert "entry 29590" in seen[-1]


def test_the_extra_round_is_bounded(far_wall):
    """One extra round, then the failure is real. It must not loop."""
    cc_raw, reply = far_wall
    seen = []
    with pytest.raises(RuntimeError, match="rejected|under 1.2R"):
        cc_raw.regenerate_commentary(send=lambda m: seen.append(m) or reply)
    assert len(seen) == 4


def test_cards_that_survive_the_override_still_pass(cc_raw, monkeypatch):
    """The extra round must not fire when the override leaves the geometry intact."""
    card = {"name": "A", "side": "short", "entry": 29590, "sl": 29630,
            "tp": [29500, 29450, 29400], "why": "w", "tag": "t", "rank": "Primary"}
    reply = json.dumps({"lead": "l", "reading": ["r"], "setups": [card] * 4, "vex_note": ""})
    seen = []
    monkeypatch.setattr(cc_raw, "_check_cards", lambda C, gate=None: "")
    monkeypatch.setattr(cc_raw, "_structure_check", lambda s, T: ("", ["ok"]))
    monkeypatch.setattr(cc_raw, "_pre_plan", lambda s, T: ({}, 0, "none"))
    monkeypatch.setattr(cc_raw, "_save_plan_check", lambda: None)
    monkeypatch.setattr(cc_raw, "_write_prose", lambda C, snapshot: seen.append("wrote"))
    cc_raw.regenerate_commentary(send=lambda m: reply)
    assert seen == ["wrote"]


# ----------------------------------------------------------------------------------
# A rejection must carry the arithmetic, not restate the rule
# ----------------------------------------------------------------------------------
def _card(**kw):
    c = {"name": "D", "side": "short", "entry": 29590.0, "sl": 29685.0,
         "tp": [29500.0, 29450.0, 29400.0]}
    c.update(kw)
    return c


def test_the_1_2r_rejection_names_the_price_tp1_must_clear():
    """"TP1 is under 1.2R" is the rule the model just failed; repeating it failed four plans
    running. risk 95 -> TP1 must be at or beyond 29,590 - 114 = 29,476."""
    import command_center as cc
    msg = cc._check_cards({"setups": [_card()] * 4})
    assert "0.95R" in msg and "risk is 95.00" in msg
    assert "29,476.00" in msg
    assert "move the ENTRY closer" in msg


def test_the_1_2r_rejection_does_the_long_arithmetic_too():
    import command_center as cc
    long_ = _card(side="long", entry=29100.0, sl=29040.0, tp=[29150.0, 29200.0, 29250.0])
    msg = cc._check_cards({"setups": [long_] * 4})
    assert "29,172.00" in msg              # 29,100 + 1.2 * 60
    assert "0.83R" in msg


def test_a_card_that_clears_1_2r_is_not_rejected():
    import command_center as cc
    ok = _card(tp=[29450.0, 29400.0, 29350.0])          # 140 / 95 = 1.47R
    assert cc._check_cards({"setups": [ok] * 4}) == ""
