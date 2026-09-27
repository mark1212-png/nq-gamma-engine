"""
gameplan_bot.py -- the decisions that stand between a Databento pull and a public channel.

No child process is ever started here: run() is monkeypatched in every test that reaches it.
What is under test is the judgement, not the subprocess plumbing -- did autopull actually do
anything, did it succeed, which sheet does the rewritten prose belong in, and does a failed
step stop the run before anything is posted.
"""
import json

import pytest

import gameplan_bot as G


@pytest.fixture
def notes(tmp_path, monkeypatch):
    """Redirect every log and status write into tmp.

    EVERY test that can reach G.log() must take this. Three of the step_comment tests did not,
    and wrote "falling back to the API" lines straight into the user's real notes/gameplan.log
    -- which then read as scheduled runs that had failed. A test suite must not be able to
    forge evidence in the log the operator diagnoses from."""
    monkeypatch.setattr(G, "NOTES", tmp_path)
    monkeypatch.setattr(G, "LOG", tmp_path / "gameplan.log")
    monkeypatch.setattr(G, "STATUS", tmp_path / "gameplan-status.json")
    return tmp_path


def write_status(notes, doc):
    (notes / "autopull-status.json").write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture
def no_cli(monkeypatch):
    """Make the Claude Code CLI look uninstalled.

    step_comment's default mode is cli-then-api, and cli_commentary.available() asks the REAL
    binary. On 2026-09-10 the three step_comment tests below found a real, logged-in CLI, and
    the first of them ran a live commentary: three model rounds, and the reply was written into
    the operator's real build.py and trinity.html (the sheet on disk had already gone to
    Discord). A test must not be able to spend quota or rewrite the plan. Every test that
    reaches step_comment without stubbing cli_commentary itself takes this."""
    import sys, types
    fake = types.ModuleType("cli_commentary")
    fake.available = lambda: (False, "stubbed out in tests")
    fake.send = lambda msg: pytest.fail("the test reached the real CLI")
    monkeypatch.setitem(sys.modules, "cli_commentary", fake)
    return fake


# ----------------------------------------------------------------------------------
# step_pull -- autopull's exit code is not its verdict
# ----------------------------------------------------------------------------------
def test_pull_that_moved_nothing_is_not_a_successful_pull(notes, monkeypatch):
    """--job auto outside a job window exits 0 having done nothing. Posting on that would put
    yesterday's plan in the channel with today's date on it."""
    doc = {"premarket": {"ok": True, "detail": "built", "at": "2026-09-10T06:00:39"}}
    write_status(notes, doc)
    monkeypatch.setattr(G, "run", lambda cmd, timeout=0: (0, "nothing to do at 03:14 PT"))

    ok, detail = G.step_pull("auto", [])
    assert ok is False and "did nothing" in detail


def test_pull_reads_the_entry_that_moved(notes, monkeypatch):
    before = {"premarket": {"ok": True, "detail": "old", "at": "2026-09-09T06:00:39"}}
    write_status(notes, before)

    def fake_run(cmd, timeout=0):
        write_status(notes, {"premarket": {"ok": True, "detail": "2026-09-10-teddyai.html built",
                                           "at": "2026-09-10T06:00:39"}})
        return 0, "premarket: ok"

    monkeypatch.setattr(G, "run", fake_run)
    ok, detail = G.step_pull("auto", [])
    assert ok is True and "2026-09-10-teddyai.html" in detail


def test_pull_fails_when_the_entry_that_moved_says_it_failed(notes, monkeypatch):
    write_status(notes, {})

    def fake_run(cmd, timeout=0):
        write_status(notes, {"premarket": {"ok": False, "detail": "verify_pull failed",
                                           "at": "2026-09-10T06:00:39"}})
        return 0, "premarket: FAILED"

    monkeypatch.setattr(G, "run", fake_run)
    ok, detail = G.step_pull("auto", [])
    assert ok is False and "verify_pull failed" in detail


def test_pull_fails_on_a_non_zero_exit(notes, monkeypatch):
    write_status(notes, {})
    monkeypatch.setattr(G, "run", lambda cmd, timeout=0: (1, "Traceback ...\nModuleNotFoundError"))
    ok, detail = G.step_pull("premarket", [])
    assert ok is False and "exited 1" in detail


def test_pull_passes_the_overrides_through(notes, monkeypatch):
    seen = {}
    write_status(notes, {})

    def fake_run(cmd, timeout=0):
        seen["cmd"] = cmd
        write_status(notes, {"intraday": {"ok": True, "detail": "d", "at": "t"}})
        return 0, ""

    monkeypatch.setattr(G, "run", fake_run)
    G.step_pull("intraday", ["--hours", "2.5", "--spot", "29480"])
    assert seen["cmd"] == ["autopull.py", "--job", "intraday", "--hours", "2.5", "--spot", "29480"]


# ----------------------------------------------------------------------------------
# step_equities -- context, never the plan
# ----------------------------------------------------------------------------------
@pytest.fixture
def project(tmp_path, monkeypatch, notes):
    monkeypatch.setattr(G, "ROOT", tmp_path)
    (tmp_path / "data" / "opra").mkdir(parents=True)
    (tmp_path / "sheets").mkdir()
    (tmp_path / "trinity.json").write_text(
        json.dumps({"settle": "2026-09-09", "report_date": "2026-09-10", "oi_vintage": "final"}),
        encoding="utf-8")
    return tmp_path


def _chains(project, roots=("SPX", "SPXW", "QQQ", "SPY")):
    for r in roots:
        (project / "data" / "opra" / f"2026-09-09-{r}-chain.json").write_text("{}", encoding="utf-8")


def test_equities_missing_chains_is_never_fatal(project, monkeypatch):
    monkeypatch.setattr(G, "run", lambda *a, **k: pytest.fail("must not fetch on --opra use"))
    ok, detail = G.step_equities("use")
    assert ok is True and "no equity chain" in detail


def test_equities_runs_the_compare_when_the_chains_are_there(project, monkeypatch):
    _chains(project)
    seen = {}

    def fake_run(cmd, timeout=0):
        seen["cmd"] = cmd
        (project / "data" / "opra" / "2026-09-09-compare.json").write_text("{}", encoding="utf-8")
        return 0, ""

    monkeypatch.setattr(G, "run", fake_run)
    ok, detail = G.step_equities("use")
    assert ok is True and seen["cmd"] == ["opra_compare.py", "--date", "2026-09-09"]
    assert "wrote 2026-09-09-compare.json" in detail


def test_equities_skips_a_compare_that_is_already_current(project, monkeypatch):
    _chains(project)
    (project / "data" / "opra" / "2026-09-09-compare.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(G, "run", lambda *a, **k: pytest.fail("nothing to recompute"))
    ok, detail = G.step_equities("use")
    assert ok is True and "already current" in detail


def test_equities_failure_still_lets_the_plan_go_out(project, monkeypatch):
    _chains(project)
    monkeypatch.setattr(G, "run", lambda cmd, timeout=0: (1, "boom"))
    ok, detail = G.step_equities("use")
    assert ok is True and "posting without the cross-reference" in detail


def test_equities_none_does_nothing(project, monkeypatch):
    monkeypatch.setattr(G, "run", lambda *a, **k: pytest.fail("--opra none must not run anything"))
    assert G.step_equities("none") == (True, "skipped (--opra none)")


# ----------------------------------------------------------------------------------
# The archive label, read back off the sheet autopull wrote
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("name,want", [
    ("2026-09-10-teddyai.html", "rth"),
    ("2026-09-10-asia-teddyai.html", "asia"),
    ("2026-09-10-intraday-0954-teddyai.html", "intraday-0954"),
    ("2026-09-07-holiday-teddyai.html", "holiday"),
])
def test_kind_for(project, name, want):
    assert G._kind_for(name) == want


def test_kind_for_a_preliminary_book(project):
    (project / "trinity.json").write_text(
        json.dumps({"settle": "2026-09-09", "report_date": "2026-09-10",
                    "oi_vintage": "preliminary"}), encoding="utf-8")
    assert G._kind_for("2026-09-10-teddyai.html") == "rthprelim"


# ----------------------------------------------------------------------------------
# step_publish -- the rewritten sheet replaces the one autopull archived
# ----------------------------------------------------------------------------------
def test_publish_overwrites_the_sheet_written_this_run(project, monkeypatch):
    import time
    dest = project / "sheets" / "2026-09-10-teddyai.html"
    dest.write_text("<html>the words autopull archived</html>", encoding="utf-8")
    (project / "trinity.html").write_text("<html>the rewritten plan</html>", encoding="utf-8")

    seen = {}
    monkeypatch.setattr(G, "run", lambda cmd, timeout=0: (seen.update(cmd=cmd), (0, ""))[1])
    ok, detail = G.step_publish(since=time.time() - 60)

    assert ok is True and detail == "2026-09-10-teddyai.html"
    assert dest.read_text(encoding="utf-8") == "<html>the rewritten plan</html>"
    assert seen["cmd"][:5] == ["archive.py", "--session", "2026-09-10", "--kind", "rth"]


def test_publish_ignores_a_sheet_from_an_earlier_run(project, monkeypatch):
    """Re-running an old date must not overwrite whatever sheet happens to be newest on disk."""
    import time
    old = project / "sheets" / "2026-09-04-teddyai.html"
    old.write_text("<html>a different session</html>", encoding="utf-8")
    (project / "trinity.html").write_text("<html>today</html>", encoding="utf-8")

    monkeypatch.setattr(G, "run", lambda cmd, timeout=0: (0, ""))
    ok, detail = G.step_publish(since=time.time() + 60)      # nothing was written "this run"

    assert ok is True and detail == "2026-09-10-teddyai.html"   # named from report_date instead
    assert old.read_text(encoding="utf-8") == "<html>a different session</html>"


def test_publish_without_a_rendered_sheet_fails(project, monkeypatch):
    import time
    monkeypatch.setattr(G, "run", lambda *a, **k: pytest.fail("nothing to archive"))
    ok, detail = G.step_publish(since=time.time())
    assert ok is False and "no trinity.html" in detail


def test_publish_reports_a_failed_archive_without_losing_the_sheet(project, monkeypatch):
    import time
    (project / "trinity.html").write_text("<html>plan</html>", encoding="utf-8")
    monkeypatch.setattr(G, "run", lambda cmd, timeout=0: (1, "archive blew up"))
    ok, detail = G.step_publish(since=time.time())
    assert ok is True and "archive exited 1" in detail
    assert (project / "sheets" / "2026-09-10-teddyai.html").exists()


# ----------------------------------------------------------------------------------
# step_comment -- the gate that matters most
# ----------------------------------------------------------------------------------
def test_comment_without_a_key_fails_rather_than_posting_stale_words(monkeypatch, notes, no_cli):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ok, detail = G.step_comment()
    assert ok is False and "ANTHROPIC_API_KEY" in detail


def test_comment_surfaces_the_model_failure_and_points_at_the_reply(monkeypatch, notes, no_cli):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    class FakeCC:
        COMMENTARY_MODEL = "claude-opus-5"

        @staticmethod
        def regenerate_commentary():
            raise RuntimeError("reply rejected 3 times: stops are structural")

    monkeypatch.setitem(__import__("sys").modules, "command_center", FakeCC)
    ok, detail = G.step_comment()
    assert ok is False and "cc-last-reply.txt" in detail


def test_comment_ok_names_the_model(monkeypatch, notes, no_cli):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    class FakeCC:
        COMMENTARY_MODEL = "claude-opus-5"

        @staticmethod
        def regenerate_commentary():
            return None

    monkeypatch.setitem(__import__("sys").modules, "command_center", FakeCC)
    ok, detail = G.step_comment()
    assert ok is True and "claude-opus-5" in detail


# ----------------------------------------------------------------------------------
# run() -- the one piece of subprocess plumbing worth pinning
# ----------------------------------------------------------------------------------
def test_run_captures_output_and_never_raises():
    rc, out = G.run(["-c", "import sys; print('hello'); sys.exit(3)"])
    assert rc == 3 and "hello" in out


def test_run_that_cannot_start_returns_a_code_not_an_exception():
    rc, out = G.run(["-c", "raise SystemExit(0)"], timeout=0.001)
    assert rc in (124, 125) and out


# ----------------------------------------------------------------------------------
# step_comment -- who writes the plan, and who gets billed for it
# ----------------------------------------------------------------------------------
@pytest.fixture
def senders(monkeypatch):
    """command_center and cli_commentary stubbed. Records which sender ran."""
    import sys
    ran = []

    class FakeCC:
        COMMENTARY_MODEL = "claude-opus-5"

        @staticmethod
        def regenerate_commentary(send=None):
            ran.append("cli" if send is not None else "api")

    class FakeCLI:
        ok, why = True, "1.2.3"

        @staticmethod
        def available():
            return FakeCLI.ok, FakeCLI.why

        @staticmethod
        def send(msg):
            return "x"

    monkeypatch.setitem(sys.modules, "command_center", FakeCC)
    monkeypatch.setitem(sys.modules, "cli_commentary", FakeCLI)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    return ran, FakeCLI


def test_cli_mode_uses_the_subscription_and_never_the_api(senders, notes):
    ran, _ = senders
    ok, detail = G.step_comment("cli")
    assert ok and ran == ["cli"]
    assert "subscription" in detail and "billed" not in detail


def test_api_mode_uses_the_api(senders, notes):
    ran, _ = senders
    ok, detail = G.step_comment("api")
    assert ok and ran == ["api"] and "billed" in detail


def test_cli_mode_does_not_silently_fall_back_and_bill_you(senders, notes):
    """--commentary cli means cli. A missing CLI is a failed run, not a surprise invoice."""
    ran, FakeCLI = senders
    FakeCLI.ok, FakeCLI.why = False, "the Claude Code CLI is not installed."
    ok, detail = G.step_comment("cli")
    assert ok is False and ran == [] and "not installed" in detail


def test_cli_then_api_falls_back_when_the_cli_cannot_write_it(senders, notes):
    ran, FakeCLI = senders
    FakeCLI.ok, FakeCLI.why = False, "subscription limit reached"
    ok, detail = G.step_comment("cli-then-api")
    assert ok and ran == ["api"]
    assert "CLI failed" in detail and "subscription limit reached" in detail


def test_cli_then_api_prefers_the_cli_when_it_works(senders, notes):
    ran, _ = senders
    ok, detail = G.step_comment("cli-then-api")
    assert ok and ran == ["cli"]


def test_cli_then_api_reports_the_second_failure_when_both_fail(senders, notes, monkeypatch):
    ran, FakeCLI = senders
    FakeCLI.ok, FakeCLI.why = False, "not installed"
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ok, detail = G.step_comment("cli-then-api")
    assert ok is False and "ANTHROPIC_API_KEY" in detail
