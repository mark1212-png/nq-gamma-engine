#!/usr/bin/env python
"""
Basilisk game plan bot -- the unattended path from Databento to a Discord channel.

    python gameplan_bot.py --job premarket --dry-run     # everything, post nothing
    python gameplan_bot.py --job premarket               # the real thing
    python gameplan_bot.py --job auto                    # whatever the clock says
    python gameplan_bot.py --post-only                   # post the sheet already on disk

One run is one session's plan:

    pull       autopull.py --job <job>   CME chain + open interest from Databento, then
                                         trinity_db -> gen -> build -> archive, as always
    equities   the OPRA books for the same settle -> opra_compare.py, so the post can carry
               SPX / SPXW / SPY / QQQ beside NQ. NEVER fatal: a missing equity chain is a
               post without the cross-reference, not a lost plan
    verify     verify_pull.py -- chain size, reference in its bar, OI delta vs the prior day
    ladder     gen.py
    state      engine.py --from-trinity --soft
    build      build.py, and read back whether the prose still matches the numbers
    comment    the Anthropic call that writes the plan: lead, reading, four cards, VEX note.
               Runs when the sheet is stale, which after a fresh pull it always is
    publish    the rewritten sheet back into sheets/<date>...html and archive/
    post       discord_post.py -> the channel

WHAT STOPS A POST. The whole point of an unattended pipeline is that nobody reads the output
before the channel does, so every gate is on the way IN:

  * autopull did nothing (outside its window, no new book)  -> no post
  * verify_pull failed                                      -> no post
  * the commentary could not be rewritten                   -> no post, because the sheet would
                                                               carry fresh numbers under words
                                                               written for a different book
  * this exact plan already went out                        -> no post (discord_post's ledger)

Nothing here retries a paid Databento pull. A failed run is a failed run; the next scheduled
slot picks it up, and notes/gameplan.log says which step stopped and why.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, re, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NOTES = ROOT / "notes"
LOG = NOTES / "gameplan.log"
STATUS = NOTES / "gameplan-status.json"

# The autopull jobs a scheduled game plan is ever built from. The OPRA jobs are not here: an
# equity fetch is its own scheduled entry (it is minutes and hundreds of megabytes), and this
# pipeline only ever CONSUMES what it left behind. See --opra.
POST_JOBS = ("auto", "premarket", "intraday", "asia", "tomorrow", "holiday")

STEPS = ("pull", "equities", "verify", "ladder", "state", "build", "comment", "publish", "post")


def log(msg: str):
    NOTES.mkdir(exist_ok=True)
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def run(cmd: list[str], timeout=1800) -> tuple[int, str]:
    """A child python step. Output is captured, utf-8, and never raises -- the caller decides
    whether a non-zero return code stops the run."""
    try:
        r = subprocess.run([sys.executable] + cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except Exception as e:                                 # a step that cannot even start
        return 125, f"{type(e).__name__}: {e}"


def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def status(result: dict):
    NOTES.mkdir(exist_ok=True)
    STATUS.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")


# ----------------------------------------------------------------------------------
# Steps
# ----------------------------------------------------------------------------------
def step_pull(job: str, extra: list[str]) -> tuple[bool, str]:
    """autopull.py owns the pull and the first build. It never throws and always writes
    notes/autopull-status.json, so its verdict is read from there rather than from the exit
    code -- 'auto' outside a job window exits 0 having done nothing at all."""
    before = _read_json(NOTES / "autopull-status.json", {}) or {}
    t0 = time.time()
    rc, out = run(["autopull.py", "--job", job] + extra, timeout=2400)
    after = _read_json(NOTES / "autopull-status.json", {}) or {}

    tail = (out.strip().splitlines() or [""])[-1][:200]
    if rc != 0:
        return False, f"autopull exited {rc}: {tail}"

    # Which entry moved? --job auto dispatches to one of the named jobs, so the entry to read
    # is whichever one has a newer timestamp than it did before this run.
    moved = [k for k, v in after.items()
             if (v or {}).get("at") != ((before.get(k) or {}).get("at"))]
    if not moved:
        return False, f"autopull did nothing this run ({tail or 'outside a job window'})"

    bad = [k for k in moved if not (after[k] or {}).get("ok")]
    if bad:
        return False, "; ".join(f"{k}: {(after[k] or {}).get('detail', '')}"[:200] for k in bad)
    return True, "; ".join(f"{k}: {(after[k] or {}).get('detail', '')}"[:200] for k in moved) \
                 + f" ({time.time() - t0:.0f}s)"


def _settle() -> str | None:
    return (_read_json(ROOT / "trinity.json", {}) or {}).get("settle")


def step_equities(mode: str) -> tuple[bool, str]:
    """The equity cross-reference. Advisory by design: this returns True even when it does
    nothing, because SPX and QQQ are context beside the NQ plan and never the plan itself."""
    if mode == "none":
        return True, "skipped (--opra none)"
    settle = _settle()
    if not settle:
        return True, "no settle in trinity.json -- skipped"

    opra = ROOT / "data" / "opra"
    roots = ("SPX", "SPXW", "QQQ", "SPY")
    missing = [r for r in roots if not (opra / f"{settle}-{r}-chain.json").exists()]

    if mode == "pull" and missing:
        # The full fetch: hundreds of megabytes and twenty-odd minutes. Only on request --
        # a scheduled plan should be consuming the books its own 16:10 entry already pulled.
        job = "opra-live" if dt.datetime.now().hour < 17 else "opra"
        log(f"[equities] fetching the {settle} books ({' '.join(missing)}) with --job {job}")
        rc, out = run(["autopull.py", "--job", job], timeout=3600)
        if rc != 0:
            return True, f"the {job} pull failed ({rc}) -- posting without the cross-reference"
        missing = [r for r in roots if not (opra / f"{settle}-{r}-chain.json").exists()]

    if missing:
        return True, (f"no equity chain for {settle}: {' '.join(missing)} "
                      f"-- posting without the cross-reference")

    # EXISTS IS NOT CURRENT (2026-09-16). This was `if cmp_.exists(): return`, so a
    # compare written before its NQ chain landed -- or before the equity chains were
    # refetched for their open interest -- was posted to Discord all the next day.
    # autopull owns the test; import it rather than keeping a second opinion here.
    cmp_ = opra / f"{settle}-compare.json"
    try:
        import autopull
        why = autopull._opra_compare_needed(settle)
    except Exception as ex:                               # never let this step break the post
        why = None if cmp_.exists() else "no compare on disk"
        log(f"[equities] could not ask autopull why ({type(ex).__name__}); falling back to existence")
    if not why:
        return True, f"{cmp_.name} already current"
    if not cmp_.exists() and "waiting on an NQ chain" in str(why):
        return True, f"no compare for {settle} -- {why}; posting without the cross-reference"
    log(f"[equities] {cmp_.name}: {why} -- rebuilding")
    rc, out = run(["opra_compare.py", "--date", settle], timeout=900)
    if rc != 0 or not cmp_.exists():
        return True, f"opra_compare failed ({rc}) -- posting without the cross-reference"
    return True, f"wrote {cmp_.name} ({why})"


def step_verify() -> tuple[bool, str]:
    rc, out = run(["verify_pull.py"], timeout=300)
    tail = (out.strip().splitlines() or [""])[-1][:200]
    return rc == 0, tail or f"exit {rc}"


def step_simple(script: list[str]) -> tuple[bool, str]:
    rc, out = run(script, timeout=900)
    tail = (out.strip().splitlines() or [""])[-1][:200]
    return rc == 0, tail or f"exit {rc}"


def step_build() -> tuple[bool, str, bool]:
    """(ok, detail, stale). build.py prints [STALE NARRATIVE BANNER SHOWN] when the prose was
    written for a different snapshot -- the same signal the command center reads."""
    rc, out = run(["build.py"], timeout=600)
    stale = "STALE NARRATIVE BANNER SHOWN" in out
    tail = (out.strip().splitlines() or [""])[-1][:200]
    return rc == 0, tail or f"exit {rc}", stale


def step_comment(mode: str = "cli-then-api") -> tuple[bool, str]:
    """The interpretation: the plan gets written into build.py between the CC: markers, then
    re-rendered. Calls command_center.regenerate_commentary() directly rather than the /cc
    route, because the route's default mode raises a request for a human Claude session -- which
    is exactly what an unattended run cannot wait for.

    mode picks who writes it, and the ONLY difference between them is the sender. Every reply
    goes through the same card checker, stop recompute, level check and corrective rounds:

      cli          the Claude Code CLI, on your subscription. Nothing billed.
      api          api.anthropic.com, billed per rewrite (~15-25c on Opus).
      cli-then-api the CLI, falling back to the API if it is missing, logged out or rate
                   limited. Costs nothing on a normal day and still produces a plan on a bad
                   one -- but it CAN bill, so it is not the default.
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import command_center

    def _api():
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        t0 = time.time()
        command_center.regenerate_commentary()
        return f"{command_center.COMMENTARY_MODEL} via the API ({time.time() - t0:.0f}s, billed)"

    def _cli():
        import cli_commentary
        ok, why = cli_commentary.available()
        if not ok:
            raise RuntimeError(why)
        t0 = time.time()
        command_center.regenerate_commentary(send=cli_commentary.send)
        return f"the Claude Code CLI wrote the plan ({time.time() - t0:.0f}s, subscription)"

    try:
        if mode == "api":
            return True, _api()
        if mode == "cli":
            return True, _cli()
        try:                                      # cli-then-api
            return True, _cli()
        except Exception as e:
            log(f"[comment] the CLI could not write it ({type(e).__name__}: {e}) -- falling back to the API")
            return True, _api() + f"  [CLI failed: {e}]"
    except Exception as e:
        # regenerate_commentary already saved the raw reply; point at it rather than repeating it
        return False, f"{type(e).__name__}: {e} -- see notes/cc-last-reply.txt"


def _kind_for(sheet_name: str) -> str:
    """The archive label autopull would have used for this sheet, read back off its name."""
    m = re.search(r"-intraday-(\d{4})-", sheet_name)
    if m:
        return f"intraday-{m.group(1)}"
    for tag in ("asia", "holiday", "europe"):
        if f"-{tag}-" in sheet_name:
            return tag
    vintage = (_read_json(ROOT / "trinity.json", {}) or {}).get("oi_vintage", "final")
    return "rth" if vintage == "final" else "rthprelim"


def step_publish(since: float) -> tuple[bool, str]:
    """Put the rewritten sheet where autopull put the first one.

    autopull built and archived a sheet BEFORE the commentary existed; that copy carries the
    previous day's words. This overwrites it with the re-rendered trinity.html and re-archives,
    so sheets/ and archive/ hold the plan that actually went to the channel."""
    src = ROOT / "trinity.html"
    if not src.exists():
        return False, "no trinity.html to publish"
    sheets = ROOT / "sheets"
    sheets.mkdir(exist_ok=True)

    # Whatever autopull wrote during THIS run -- not the newest file in the folder, which on a
    # re-run of an old date would be some other session's sheet.
    fresh = [p for p in sheets.glob("*-teddyai.html") if p.stat().st_mtime >= since - 1]
    if fresh:
        dest = max(fresh, key=lambda p: p.stat().st_mtime)
    else:
        date = (_read_json(ROOT / "trinity.json", {}) or {}).get("report_date")
        if not date:
            return False, "no sheet written this run and no report_date to name one"
        dest = sheets / f"{date}-teddyai.html"

    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    session = (_read_json(ROOT / "trinity.json", {}) or {}).get("report_date") or dest.name[:10]
    rc, out = run(["archive.py", "--session", session, "--kind", _kind_for(dest.name),
                   "--sheet", str(dest)], timeout=300)
    note = "" if rc == 0 else f" (archive exited {rc})"
    return True, f"{dest.name}{note}"


def step_post(dry_run: bool, force: bool, attach: bool, mention, with_opra: bool,
              fmt: str = "image", parts=None) -> tuple[bool, str, bool]:
    """(step ok, detail, anything actually reached the channel)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import discord_post
    try:
        r = discord_post.post_gameplan(dry_run=dry_run, force=force, attach_sheet=attach,
                                       mention=mention, with_opra=with_opra, fmt=fmt, parts=parts)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", False
    if dry_run:
        return True, (f"dry run: {len(r['messages'])} messages"
                      + (f", {len(r['images'])} images" if r.get("images") else "")
                      + f", fingerprint {r['fingerprint']}"
                      + (" -- STALE, a real post would be refused" if r["stale"] else "")), False
    if not r["posted"]:
        # a duplicate is a correct outcome; a stale sheet at this point is a bug in the run above
        return r["reason"] == "duplicate", f"{r['reason']}: {r.get('detail', '')}".rstrip(": "), False
    return True, f"{r['messages']} messages -> Discord ({r['fingerprint']})", True


# ----------------------------------------------------------------------------------
# The run
# ----------------------------------------------------------------------------------
def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Pull, interpret and post the TeddyAI game plan")
    ap.add_argument("--job", default="auto", choices=POST_JOBS,
                    help="the autopull job to build from (default: auto, by the clock)")
    ap.add_argument("--date", default=None, help="run as if today were this date (YYYY-MM-DD)")
    ap.add_argument("--hours", type=float, default=None, help="override hours to expiry")
    ap.add_argument("--spot", type=float, default=None, help="override the reference")
    ap.add_argument("--rail-scale", type=float, default=None, help="override the band scale")
    ap.add_argument("--opra", choices=("use", "pull", "none"), default="use",
                    help="use = compare whatever equity chains are on disk (default); "
                         "pull = fetch them first (minutes, hundreds of MB); none = skip")
    ap.add_argument("--dry-run", action="store_true", help="run everything; render the post, send nothing")
    ap.add_argument("--post-only", action="store_true", help="skip the pipeline, post what is on disk")
    ap.add_argument("--no-post", action="store_true", help="run the pipeline, do not post at all")
    ap.add_argument("--no-commentary", action="store_true",
                    help="do not call the model (leaves the sheet stale, so nothing is posted)")
    ap.add_argument("--commentary", choices=("cli", "api", "cli-then-api"), default="cli",
                    help="who writes the plan. cli (default): the Claude Code CLI on your "
                         "subscription, nothing billed. api: api.anthropic.com, billed per "
                         "rewrite. cli-then-api: the CLI, falling back to the API when it is "
                         "missing, logged out or rate limited -- which CAN bill you.")
    ap.add_argument("--force", action="store_true", help="post even if this plan already went out")
    ap.add_argument("--attach-sheet", action="store_true", help="attach the dated HTML sheet")
    ap.add_argument("--mention", default=None, help="override DISCORD_MENTION")
    ap.add_argument("--format", choices=("image", "embeds", "both"), default="image",
                    help="image (default): the sheet's own sections as PNGs; embeds: Discord "
                         "embeds; both: the embeds followed by the pictures")
    ap.add_argument("--parts", default=None,
                    help="which sheet sections to render, comma-separated, or 'all' "
                         "(default: overview,gameplan,reading)")
    a = ap.parse_args()

    t_start = time.time()
    steps: dict[str, dict] = {}
    result = {"job": a.job, "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "steps": steps,
              "posted": False, "dry_run": a.dry_run}

    def record(name, ok, detail, t0):
        steps[name] = {"ok": ok, "detail": detail, "ms": round((time.time() - t0) * 1000)}
        log(f"[{name}] {'ok' if ok else 'FAILED'} -- {detail}")
        return ok

    def stop(why):
        result["stopped_at"] = why
        result["elapsed_s"] = round(time.time() - t_start, 1)
        status(result)
        log(f"run stopped at {why} -- nothing posted")
        return 2

    log(f"=== gameplan --job {a.job}"
        + (" --post-only" if a.post_only else "")
        + (" --dry-run" if a.dry_run else "") + " ===")

    if not a.post_only:
        extra = []
        for flag, val in (("--date", a.date), ("--hours", a.hours), ("--spot", a.spot),
                          ("--rail-scale", a.rail_scale)):
            if val is not None:
                extra += [flag, str(val)]

        t0 = time.time()
        ok, detail = step_pull(a.job, extra)
        if not record("pull", ok, detail, t0):
            return stop("pull")

        t0 = time.time()
        ok, detail = step_equities(a.opra)
        record("equities", ok, detail, t0)          # advisory: never stops the run

        t0 = time.time()
        ok, detail = step_verify()
        if not record("verify", ok, detail, t0):
            return stop("verify")

        for name, script in (("ladder", ["gen.py"]),
                             ("state", ["engine.py", "--from-trinity", "--soft"])):
            t0 = time.time()
            ok, detail = step_simple(script)
            if not record(name, ok, detail, t0) and name != "state":
                return stop(name)                   # the state layer is optional by design (--soft)

        t0 = time.time()
        ok, detail, stale = step_build()
        if not record("build", ok, detail + (" [stale narrative]" if stale else ""), t0):
            return stop("build")

        if stale and not a.no_commentary:
            t0 = time.time()
            ok, detail = step_comment(a.commentary)
            if not record("comment", ok, detail, t0):
                return stop("comment")
            t0 = time.time()
            ok, detail = step_publish(t_start)
            record("publish", ok, detail, t0)
        elif stale:
            record("comment", False, "skipped (--no-commentary); the sheet stays stale",
                   time.time())
        else:
            record("comment", True, "the prose already matches the numbers", time.time())

    if a.no_post:
        result["elapsed_s"] = round(time.time() - t_start, 1)
        status(result)
        log("done -- --no-post, nothing sent")
        return 0

    t0 = time.time()
    parts = None
    if a.parts:
        parts = "all" if a.parts == "all" else [p.strip() for p in a.parts.split(",") if p.strip()]
    ok, detail, posted = step_post(a.dry_run, a.force, a.attach_sheet, a.mention,
                                   a.opra != "none", a.format, parts)
    record("post", ok, detail, t0)
    result["posted"] = posted
    result["elapsed_s"] = round(time.time() - t_start, 1)
    status(result)
    log(f"done in {result['elapsed_s']}s")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
