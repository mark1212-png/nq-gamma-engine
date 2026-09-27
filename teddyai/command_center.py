"""
TeddyAI Command Center — Flask blueprint.

Wired into teddy.py (bottom of the file, guarded like setup_track):
    import command_center as _cc; _cc.BARS_FN = cc_bars; app.register_blueprint(_cc.cc)
then open http://127.0.0.1:5137/cc

Responsibilities
  /cc                 serve command_center.html
  /cc/api/run   POST  start the pipeline in a background thread (idempotent while running);
                      body {commentary: bool} -- default FALSE: the API call is on request only
  /cc/api/commentary POST  regenerate the commentary alone (Anthropic API call), then rebuild
  /cc/api/state       step checklist + feed status (polled by the loading screen)
  /cc/api/report      normalized report for the page (trinity.json + setups read out of trinity.html)
  /cc/api/bars        recent bars (delegates to the existing /api/bars logic — see BARS_FN)
  /cc/api/ticks       Server-Sent Events: the print stream from cc_ticks.py (Databento Live trades)
  /cc/api/ladder      the sheet's own rendered ladder (ladder_frag.html) + its CSS, scoped
  /cc/api/chain       chain_live.json -- per-expiry, per-strike net OI + IV, for live re-pricing in the page
  state.chart         whether teddychart answers on :5138 -- the page embeds it in the chart row when it does

Conventions honoured (from the teddyai-narrative skill / HANDOFF.md):
  * every file read/write pins encoding='utf-8'
  * the report is READ OUT of the rendered sheet; nothing here rewrites build.py structure
  * commentary regeneration calls the Anthropic API with trinity.json only — lean prompt, no fluff
"""
from __future__ import annotations
import json, os, re, subprocess, threading, time, html, datetime
from pathlib import Path
from flask import Blueprint, jsonify, request, send_file, Response
import cc_ticks
try:
    import stack_status                # is the stack ticking, and is the map still today's?
except Exception:                      # the panel degrades to the old per-service rows
    stack_status = None

ROOT = Path(__file__).resolve().parent
cc = Blueprint("cc", __name__, url_prefix="/cc")

# ----------------------------------------------------------------------------------
# Adapters — the two places to edit when wiring into the real project
# ----------------------------------------------------------------------------------
BARS_FN = None          # teddy.py sets this module attribute to cc_bars(tf) -> [{t(ms),o,h,l,c,v}] asc
FEED_STALE_MIN = 30     # Databento historical trails the clock 10-25 min on a healthy day (autopull.FEED_LAG_NORMAL)
CHART_URL = os.environ.get("TEDDYCHART_URL", "http://127.0.0.1:5138")   # teddychart: the chart row when it is running
# Which model writes the commentary. Override without editing code:  setx CC_COMMENTARY_MODEL claude-opus-5
# IDs: claude-opus-5 ($5/$25 per MTok, ~5c a rewrite) · claude-sonnet-5 ($2/$10, ~2c) · claude-fable-5-1 ($10/$50, ~10c)
COMMENTARY_MODEL = os.environ.get("CC_COMMENTARY_MODEL", "claude-opus-5")   # Opus by Mark's choice 2026-09-05; Sonnet's structure reads were weaker
# Stops come from the gamma map, not from a points band (the 25-45 band of prompt round 1 put the 08-13 stop under a
# strike the tape had closed through 12 times that night). A card's stop sits BUFFER beyond the nearest MATERIAL
# strike in the adverse direction; material = |gamma| >= MATERIAL_PCT of that book's largest strike within one EM of
# price, on any book; flips are never anchors (a zero-crossing is not a wall); a strike the tape has already closed
# the tape has swung across DEMOTE_CROSSES times this session is demoted -- but if every candidate is demoted
# the nearest one anchors anyway and the card says so, because a trending session closes through everything behind it.
# The stop is wherever the structure is: there is no width cap (the 80-point one was a prop-firm number, removed 2026-09-16).
MATERIAL_PCT    = 0.25
STOP_BUFFER     = 10.0     # MINIMUM points beyond the anchor strike -- the buffer scales with the book, see STOP_BUFFER_EM
STOP_BUFFER_EM  = 0.15     # x the sheet's expected move for THIS plan's window (em x rail_scale): the IMPLIED move the ladder
                           # is priced on, not a realised range. 0 = fixed STOP_BUFFER. History: a fixed 10 put the stop 16
                           # points from the entry whenever the traded level was the anchor and was stopped 18 of 26 times
                           # over ten test sessions (notes/tests/2026-09-05-ten-sessions); 2026-09-05..07 the buffer was
                           # 1.0 x a measured intraday range (~30 pts Asia, ~65 RTH), which cut invalidations by a
                           # third. Mark's decision 2026-09-07: nothing ATR-derived anywhere -- it lags. On the same ten
                           # sessions 0.15 x EM x rail lands where the range buffer landed (Asia 24 vs 22 pts, RTH 48 vs 53)
                           # and scores the same (notes/tests/2026-09-07-stop-buffer-em); the two part ways only when
                           # implied and realised disagree, and there the range is the one that is late.
# DEMOTION, 2026-09-16. Was "3 close-throughs on 30-minute bars". Mark: "lets remove 30
# minute close logic, a lot can happen in 30 minutes including a massive stop run" -- and a
# 30-minute close is exactly what a stop run produces and then takes back. It was also
# session-blind: on 09-16 the 29,400 and 29,350 anchors were both demoted by three closes
# that all happened between 00:00 and 05:30Z, when price was 200 points lower. Counting only
# bars since the session open gave zero. So: crossings are counted from SWINGS, inside the
# CURRENT SESSION. A swing is a pivot the tape actually turned on; a wick through a level
# leaves none, which is the property a close does not have.
DEMOTE_CROSSES  = 3        # swing crossings this session at which a strike is a pivot, not a wall
# MAX_STRUCT_STOP was 80 points -- "a prop-firm number, the same at every scale" (HANDOFF).
# Removed 2026-09-16 (Mark: "we're trading based off of prop firm sizing and that doesnt even
# make sense and shouldnt be determined mechanically"). An account's risk limit is not a fact
# about the board, and letting it decide WHERE a stop goes put a fixed 80-point ceiling on every
# session at every scale -- which on a wide day deleted the card instead of widening the stop.
# The stop is now wherever the structure is. What is left is a preference, on the session's own
# scale: the wall behind the card's wall when it is within one drawn band, otherwise the card's
# own wall. How many contracts that risk is worth is a decision outside this engine.
STRUCT_STOP_BANDS = 1.0    # x the drawn band: how far the "wall behind" may sit before the stop falls back
CLUSTER_PTS     = 25.0     # material strikes this close together are one wall; the stop goes beyond the far edge
NEAR_WALL_PTS   = 40.0     # ADVISORY. 2026-09-07 backtest, 57 sessions / 81 closed trades: cards whose wall sat
STOP_BAND       = (45.0, 70.0)  # within 40 pts of the open ran 75.8% vs 64.2% pooled, and stops of 45-70 pts ran
                           # 72-75% vs 46.7% beyond 70. Both are UNCORRECTED subgroups (Bonferroni p = 0.18) on a
                           # pooled edge that is itself only p = 0.10. Nothing here rejects a card -- it annotates.
PIPELINE = [            # (step id, shell command or callable)
    # autopull --job auto is the whole pull-and-build for the session the clock says it is
    # (it runs trinity_db, gen, build and archive itself with the right arguments, or does
    # nothing outside a job window). The ladder and build steps after it are RE-RENDERS of
    # whatever trinity.json now holds -- gen.py and build.py take no arguments. trinity_db.py
    # must never be run bare here: it needs --chain and --report-date, and only autopull knows them.
    ("pull",    "python autopull.py --job auto"),
    ("verify",  "python verify_pull.py"),              # chain >= 750 KB, reference inside its bar, OI delta vs prior day
    ("ladder",  "python gen.py"),
    # The market-state layer. BETWEEN ladder and build: it reads the trinity.json
    # trinity_db wrote and writes the state.json build.py's optional panel reads,
    # so the sheet and the state come out of one press of the button. --soft
    # because it is optional -- a failure here must not cost a sheet.
    ("state",   "python engine.py --from-trinity --soft"),
    ("build",   "python build.py"),
    ("opra",    None),                                  # in-process (_run_opra): the equity cross-reference, skip-never-fail
    ("comment", None),                                  # handled in-process (regenerate_commentary)
]
OPRA_ROOTS = ("SPX", "SPXW", "QQQ", "SPY")

# ----------------------------------------------------------------------------------
# Step state
# ----------------------------------------------------------------------------------
STEP_LABELS = {"pull": "Pull chain", "verify": "Verify reference & OI", "ladder": "Compute ladder",
               "state": "Market state", "build": "Build sheet", "opra": "Cross-reference",
               "comment": "Update plan"}
_state = {"steps": [{"id": s, "label": STEP_LABELS[s], "state": "pending", "ms": 0} for s, _ in PIPELINE],
          "running": False, "feed": "off", "started": 0.0, "stale": False,
          "plan_check": None}     # {"at", "checked", "note", "rejected":[...]} -- the played-out check on the last Update Plan
_lock = threading.Lock()

def _sim_anchor():
    T = json.loads(_read(ROOT / "trinity.json"))
    return T.get("last_print") or T.get("spot")
TICKS = cc_ticks.TickFeed(log=lambda m: _log(m), sim_anchor=_sim_anchor)

def _set(sid, state, ms=0):
    with _lock:
        for s in _state["steps"]:
            if s["id"] == sid:
                s["state"], s["ms"] = state, int(ms)

def _run_pipeline(commentary: bool):
    with _lock:
        if _state["running"]:
            return
        _state["running"] = True
        for s in _state["steps"]:
            s["state"], s["ms"] = "pending", 0
    try:
        for sid, cmd in PIPELINE:
            t0 = time.time(); _set(sid, "run")
            try:
                if sid == "comment":
                    if commentary: regenerate_commentary()
                    else: _set(sid, "skip"); continue
                elif sid == "opra":
                    # The equity books are a cross-reference, not the plan: a missing chain file
                    # (the equity pull is its own scheduled task, outside this margin) or a failed
                    # compare is a SKIP with a log line, never a failed pipeline. 2026-09-06.
                    ok, why = _run_opra()
                    if not ok:
                        _log(f"[opra] skipped: {why}"); _set(sid, "skip"); continue
                elif cmd:
                    r = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
                    out, err = (r.stdout or ""), (r.stderr or "")
                    if r.returncode != 0:
                        raise RuntimeError((err[-800:] or out[-800:]).strip() or f"exit {r.returncode}")
                    if sid == "build":
                        # A stale narrative is a STATE, not a failure: the sheet already carries the red banner,
                        # and failing here would stop the pipeline before the commentary step -- exactly when a
                        # rewrite is wanted. Record it; the page shows "words are stale -- Rewrite".
                        with _lock: _state["stale"] = "STALE NARRATIVE BANNER SHOWN" in out
                        # and the sheet is new, so check its cards against the tape before the plan (the
                        # commentary step re-checks after it writes; this covers a rebuild with no rewrite)
                        if not commentary: _refresh_plan_check(force=True, why="build")
                _set(sid, "ok", (time.time() - t0) * 1000)
            except Exception as e:  # stop at first failure; the page shows which step
                _set(sid, "fail", (time.time() - t0) * 1000)
                import traceback
                where = "" if isinstance(e, RuntimeError) else " @ " + traceback.extract_tb(e.__traceback__)[-1].name
                _log(f"[{sid}] FAILED: {type(e).__name__ if not isinstance(e, RuntimeError) else ''} {e}{where}".replace("FAILED:  ", "FAILED: "))
                break
    finally:
        with _lock:
            _state["running"] = False

def _log(line):
    with open(ROOT / "notes" / "cc.log", "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")

def _settle_date():
    """The NQ chain's settle date the sheet was built from -- what the equity files are keyed by."""
    try:
        return json.loads(_read(ROOT / "trinity.json")).get("settle")
    except Exception:
        return None

def _opra_missing(date):
    return [r for r in OPRA_ROOTS if not (ROOT / "data" / "opra" / f"{date}-{r}-chain.json").exists()]

def _run_opra():
    """opra_compare.py --date <settle>: the five books in dollar gamma per 1%, plus each book's
    chain_live.json for the Strike Exposure overlay. (ok, why-not)."""
    date = _settle_date()
    if not date:
        return False, "no settle date in trinity.json"
    missing = _opra_missing(date)
    if missing:
        # The pull is autopull.py --job opra, its own 16:10 PT entry. It is not run
        # from here and never from --job auto: an OPRA fetch does not belong in front
        # of the boot checklist. 2026-09-08.
        return False, (f"no equity chain for {date}: {' '.join(missing)} "
                       f"-- the opra job pulls it after that session's settle "
                       f"(autopull.py --job opra --date {date} to run it now)")
    r = subprocess.run(f"python opra_compare.py --date {date}", shell=True, cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        return False, ((r.stderr or r.stdout)[-400:]).strip()
    ok = (ROOT / "data" / "opra" / f"{date}-compare.json").exists()
    return ok, "" if ok else "compare.json not written"

# ----------------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------------
@cc.get("/")
def page():
    TICKS.start()                                  # the feed lives as long as teddy.py does; first open starts it
    return send_file(ROOT / "command_center.html")

@cc.get("/api/ticks")
def ticks():
    """Server-Sent Events. Snapshot of the day's prints so far, then batches every 250 ms."""
    TICKS.start()
    return Response(TICKS.sse(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

@cc.get("/api/ladder")
def ladder():
    """The sheet's ladder, verbatim: gen.py's ladder_frag.html plus the ladder rules from build.py's
    HEAD, every selector prefixed with .sheet-ladder so they cannot collide with the page's own."""
    frag = _read(ROOT / "ladder_frag.html") if (ROOT / "ladder_frag.html").exists() else ""
    src = _read(ROOT / "build.py")
    m = re.search(r"/\* trinity ladder \*/(.*?)/\* generic \*/", src, re.S)
    css = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S) if m else ""
    def scope(rule):
        sel, _, body = rule.partition("{")
        return ", ".join(".sheet-ladder " + x.strip() for x in sel.split(",") if x.strip()) + "{" + body
    scoped = "".join(scope(r) + "}" for r in css.split("}") if r.strip() and "{" in r)
    # gen.py writes only the rows; the frame (.lad > .grid) and the header row live in build.py's BODY
    h = re.search(r'(<div class="hd">.*?</div>\s*</div>)\s*__LADDER__', src, re.S)
    head = h.group(1) if h else ""
    try: nexp = json.loads(_read(ROOT / "trinity.json")).get("expiries")
    except Exception: nexp = None
    head = head.replace("__NEXP__", f"{nexp:,}" if nexp else "—")
    html = f'<div class="lad"><div class="grid">{head}\n{frag}</div></div>' if frag else ""
    return jsonify(html=html, css=scoped)

@cc.get("/api/chain")
def chain():
    """chain_live.json -- the inputs behind the gamma map (trinity_db.py writes it beside trinity.json):
    per expiry the underlying's settle, the 20:00 UTC expiry and per strike net OI + smile IV. The page
    re-prices gamma at the last print and the real clock. {} until the next pipeline run writes one."""
    p = ROOT / "chain_live.json"
    if not p.exists():
        return jsonify({})
    return Response(_read(p), mimetype="application/json", headers={"Cache-Control": "no-store"})

@cc.get("/api/opra")
def opra():
    """data/opra/<settle>-compare.json -- the five books (SPX complex, SPY, QQQ, NQ) in $M of dealer
    gamma per 1%, the daily QQQ/NQ and SPY/SPX factors, the pair rows and the cross row. Written by
    opra_compare.py (the 'opra' pipeline step). {} until it exists. Numbers only; the file carries
    hypothesis_status UNTESTED until backtest_opra.py says otherwise."""
    date = _settle_date()
    p = ROOT / "data" / "opra" / f"{date}-compare.json" if date else None
    if not p or not p.exists():
        return jsonify({"missing": _opra_missing(date) if date else OPRA_ROOTS, "settle": date})
    return Response(_read(p), mimetype="application/json", headers={"Cache-Control": "no-store"})

@cc.get("/api/opra/chain")
def opra_chain():
    """data/opra/<settle>-<root>-chain_live.json -- an equity book in exactly chain_live.json's shape
    (per expiry F, expiry, per strike net OI + smile IV), so the page re-prices it with the same
    code it uses for NQ. ?root=QQQ (default), SPY, SPX, SPXW. {} until opra_compare.py writes it."""
    root = (request.args.get("root") or "QQQ").upper()
    if root not in OPRA_ROOTS + ("SPX+SPXW",):
        return jsonify({}), 400
    date = _settle_date()
    p = ROOT / "data" / "opra" / f"{date}-{root}-chain_live.json" if date else None
    if not p or not p.exists():
        return jsonify({})
    return Response(_read(p), mimetype="application/json", headers={"Cache-Control": "no-store"})

@cc.post("/api/ticks/speed")
def ticks_speed():
    """Replay only: change the tape's speed without losing the virtual clock. teddychart has its own speed
    buttons; the page's chip shows this one so the two can be kept matched."""
    body = request.get_json(silent=True) or {}
    try: sp = float(body.get("speed", 0))
    except (TypeError, ValueError): sp = 0
    if TICKS.mode != "replay" or not (0.1 <= sp <= 1000): return jsonify(ok=False), 400
    TICKS.set_speed(sp)
    return jsonify(ok=True, speed=sp)

REPLAY = os.environ.get("CC_REPLAY", "")          # set by replay_teddy.ps1: the sheet on disk is a past day's; leave it alone

@cc.post("/api/run")
def run():
    body = request.get_json(silent=True) or {}
    if REPLAY:
        with _lock:
            for st in _state["steps"]:
                st["state"], st["ms"] = "skip", 0
        return jsonify(ok=True, skipped="replay")
    threading.Thread(target=_run_pipeline, args=(bool(body.get("commentary", False)),), daemon=True).start()
    return jsonify(ok=True)

# ------------------------------------------------------------------------------
# Update Plan -- how the commentary gets rewritten (2026-09-08)
# ------------------------------------------------------------------------------
# It used to be one Anthropic API call from this process. Mark's decision: no Anthropic key in the stack.
# The button now RAISES A REQUEST -- notes/commentary_request.json -- and a Claude session with access to the
# folder answers it: reads trinity.json and the bar cache, rewrites the fenced blocks in build.py, renders,
# and writes trinity.html / ladder_frag.html / the sheet back. Two things answer a request, it is the same
# job either way, and neither needs a key:
#
#   * a SCHEDULED session polling in the pull windows -- the 06:00 build he is asleep for
#   * HIM, asking in a chat -- immediate, and the one with judgement in the loop
#
# The request file IS the protocol: raise it, answer it, delete it. A request nobody answers expires on its
# own (REQUEST_TTL_MIN) so a stale one does not sit on the button for a day. CC_COMMENTARY_MODE=api puts the
# old API path back -- kept unchanged so the two can be compared, not as the default.

COMMENTARY_MODE = os.environ.get("CC_COMMENTARY_MODE", "request")   # request | api
REQUEST_PATH    = ROOT / "notes" / "commentary_request.json"
REQUEST_TTL_MIN = 180        # an unanswered request older than this is stale, not pending

def _request_state():
    """The pending request, or None.

    A request is answered in ONE of two ways, and the difference matters:

      * deleted -- what runs on this machine can do (the dashboard's cancel, the CLI)
      * stamped with "answered_at" -- what a Claude session can do. It reaches this folder through the
        desktop bridge, which WRITES files but cannot delete them, so "delete the request when you are done"
        is a protocol it is unable to finish. It writes the same file back with answered_at/answered_by
        instead, and THIS process, which can delete, reaps it on the next poll.

    Either way the button clears and the sheet reloads. Expired = raised longer ago than the TTL and still
    unanswered, which is a request nobody picked up rather than one in flight."""
    try:
        r = json.loads(_read(REQUEST_PATH))
    except Exception:
        return None
    if r.get("answered_at"):
        _log(f"[comment] answered {r['answered_at']} by {r.get('answered_by', '?')}"
             + (f" -- {r['answered_note']}" if r.get("answered_note") else ""))
        try: REQUEST_PATH.unlink()
        except Exception: pass
        return None
    age = (time.time() * 1000 - int(r.get("at_ms") or 0)) / 60_000
    r["age_min"] = round(age, 1)
    r["expired"] = age > REQUEST_TTL_MIN
    return r


def raise_request(why="", cards=None, T=None):
    """Write the request. Everything an answering session needs in order to know WHICH plan is being asked
    about goes in here, so it never has to infer it from mtimes: the book, the moment the plan is priced to,
    the sheet as it stands, and the cards on it. Idempotent per plan -- clicking twice does not queue two."""
    if T is None:
        try: T = json.loads(_read(ROOT / "trinity.json"))
        except Exception: T = {}
    if cards is None:
        try: cards = _setups(_read(ROOT / "trinity.html"))
        except Exception: cards = []
    pid = _plan_id(T, cards)
    prev = _request_state()
    if prev and prev.get("plan_id") == pid and not prev.get("expired"):
        return prev, False
    req = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "at_ms": int(time.time() * 1000), "plan_id": pid,
           "why": why or "Update Plan", "report_date": T.get("report_date"), "settle": T.get("settle"),
           "asof": T.get("asof"), "oi_vintage": T.get("oi_vintage"), "hours": T.get("hours"),
           "rail_label": T.get("rail_label"), "reference": T.get("last_print") or T.get("spot"),
           "sheet": "trinity.html",
           "sheet_mtime": (ROOT / "trinity.html").stat().st_mtime if (ROOT / "trinity.html").exists() else None,
           "cards": [{"name": c.get("name"), "entry": c.get("entry"), "sl": c.get("sl"),
                      "tps": c.get("tps") or ([c.get("tp")] if c.get("tp") is not None else [])}
                     for c in cards]}
    try:
        REQUEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        REQUEST_PATH.write_text(json.dumps(req, indent=1), encoding="utf-8")
        _log(f"[comment] request raised for {req['report_date']} {req['rail_label']} "
             f"({req['hours']}h, book {req['settle']}) -- {req['why']}")
    except Exception as e:
        _log(f"[comment] could not raise the request: {e}")
        return None, False
    return req, True


def clear_request(why=""):
    """Answered. Called by whoever rewrote the prose -- including the CLI, so a session that did the job by
    hand leaves the button in exactly the state a scheduled one would."""
    r = _request_state()
    try:
        REQUEST_PATH.unlink()
    except Exception:
        return False
    _log(f"[comment] request cleared{(' -- ' + why) if why else ''}"
         + (f" (raised {r.get('at')}, {r.get('age_min')} min earlier)" if r else ""))
    return True


@cc.post("/api/commentary")
def commentary():
    """Update Plan. Default: raise a request for a Claude session to answer -- no API call, no key.
    CC_COMMENTARY_MODE=api restores the old in-process Anthropic call. Refused while the pipeline is running
    either way: rewriting prose over numbers that are still moving is how the two end up disagreeing."""
    with _lock:
        if _state["running"]:
            return jsonify(ok=False, error="pipeline running"), 409
    if COMMENTARY_MODE == "api":
        threading.Thread(target=_run_commentary, daemon=True).start()
        return jsonify(ok=True, mode="api")
    req, fresh = raise_request(why="Update Plan (dashboard)")
    if req is None:
        return jsonify(ok=False, error="could not write notes/commentary_request.json"), 500
    # read it back rather than returning the raw dict: the page renders age_min/expired, which only
    # _request_state() computes, and the button must not paint a different shape here than on the next poll
    return jsonify(ok=True, mode="request", fresh=fresh, request=_request_state() or req)


@cc.delete("/api/commentary")
def commentary_cancel():
    """Take a request back -- the button's own undo, for a click that was not meant."""
    return jsonify(ok=clear_request("cancelled from the dashboard"))

def _run_commentary():
    with _lock:
        _state["running"] = True
    t0 = time.time(); _set("comment", "run")
    try:
        regenerate_commentary(); _set("comment", "ok", (time.time() - t0) * 1000)
    except Exception as e:
        _set("comment", "fail", (time.time() - t0) * 1000); _log(f"[comment] FAILED: {e}")
    finally:
        with _lock:
            _state["running"] = False

@cc.get("/api/state")
def state():
    with _lock:
        return jsonify(steps=_state["steps"], feed=_state["feed"], running=_state["running"], stale=_state["stale"], plan_check=_state["plan_check"],
                       commentary={"mode": COMMENTARY_MODE, "request": _request_state()},
                       model=COMMENTARY_MODEL, key=bool(os.environ.get("ANTHROPIC_API_KEY")), job=_last_job(),
                       ticks=TICKS.start().status(), chart=_chart_up(), replay=REPLAY or None)

@cc.get("/api/ops")
def ops():
    """
    THE OPERATIONS LIST (Mark, 2026-09-15: "something on the ui to show whats scheduled whats
    tracking last pull etc ... in a nice list ... last updated as well so i can keep track if i
    have a stale chart"). Every service with its heartbeat and age, the job table with next
    and last runs, the last pull per job, the last build, the last gameplan, the last post, the
    ledger's newest lines. Everything is read from files the services already write; nothing
    here talks to a service, so a dead one shows as stale rather than hanging the page.
    """
    import socket, datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    def rd(p_):
        try: return json.loads(Path(p_).read_text(encoding="utf-8"))
        except Exception: return None
    def age_of(iso=None, path=None):
        try:
            if iso:
                t = _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
                if t.tzinfo is None: t = t.astimezone()
                return round((now - t.astimezone(_dt.timezone.utc)).total_seconds())
            if path and Path(path).exists():
                return round(time.time() - Path(path).stat().st_mtime)
        except Exception:
            return None
        return None
    def port_up(port):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3): return True
        except Exception: return False
    services = []
    services.append(dict(name="dashboard", ok=True, detail=f"teddy.py :5137 · pid {os.getpid()}", age=0))
    fs = rd(ROOT / "data" / "bars" / "feed_status.json") or {}
    a = age_of(fs.get("updated"))
    services.append(dict(name="live feed", ok=bool(fs) and fs.get("state") == "streaming" and a is not None and a < 120,
                         detail=f"{fs.get('state', 'no status')} · last bar {str(fs.get('last_bar_ts', ''))[11:16]}Z · {fs.get('last_px', '—')}", age=a))
    lg = rd(ROOT / "notes" / "tape" / "ledger-status.json") or {}
    a = age_of(lg.get("updated"))
    services.append(dict(name="tape ledger", ok=bool(lg) and a is not None and a < 90,
                         detail=(f"session {lg.get('session')} · {lg.get('events', 0)} events · tape to {lg.get('last_bar')}" if lg else "no heartbeat"), age=a))
    lv = rd(ROOT / "levels_live.json") or {}
    a = age_of(path=ROOT / "levels_live.json")
    z = ((lv.get("panels") or {}).get("0DTE") or {})
    services.append(dict(name="live levels", ok=bool(lv) and a is not None and a < 180,
                         detail=(f"priced @ {lv.get('last')} ({lv.get('last_source')}) · 0DTE flip {(z.get('hvl') or ['—'])[0]} · for {lv.get('report_date')}" if lv else "no file"), age=a))
    services.append(dict(name="chart", ok=port_up(5138), detail="teddychart :5138" if port_up(5138) else "not answering on :5138", age=None))
    sc = rd(ROOT / "notes" / "scheduler-status.json") or {}
    a = age_of(sc.get("updated"))
    nxt = sorted([j for j in (sc.get("jobs") or []) if j.get("next")], key=lambda j: j["next"])[:1]
    services.append(dict(name="scheduler", ok=bool(sc) and a is not None and a < 90,
                         detail=(f"{len(sc.get('jobs') or [])} jobs · next {nxt[0]['name']} at {nxt[0]['next'][11:16]}" if nxt else "no status"), age=a))
    pulls = []
    for job, v in (rd(ROOT / "notes" / "autopull-status.json") or {}).items():
        pulls.append(dict(job=job, ok=bool(v.get("ok")), at=v.get("at"), age=age_of(v.get("at")), detail=str(v.get("detail", ""))[:140]))
    pulls.sort(key=lambda x: x.get("at") or "", reverse=True)
    T = rd(ROOT / "trinity.json") or {}
    build = dict(age=age_of(path=ROOT / "trinity.json"), report_date=T.get("report_date"), label=T.get("rail_label"),
                 spot=T.get("spot"), last_print=T.get("last_print"), book=T.get("settle"), vintage=T.get("oi_vintage"),
                 stale=("have not been rewritten" in ((ROOT / "trinity.html").read_text(encoding="utf-8") if (ROOT / "trinity.html").exists() else "")))
    gp = rd(ROOT / "notes" / "gameplan-status.json") or {}
    steps = gp.get("steps") or {}
    gameplan = dict(job=gp.get("job"), started=gp.get("started"), age=age_of(gp.get("started")),
                    steps=[dict(step=k, ok=bool(v.get("ok")), detail=str(v.get("detail", ""))[:90]) for k, v in steps.items()])
    post = None
    try:
        lines = (ROOT / "notes" / "discord.log").read_text(encoding="utf-8").strip().splitlines()
        last = [l for l in lines if "[post]" in l][-1:]
        if last: post = last[0][:160]
    except Exception:
        pass
    ledger_tail = []
    try:
        sess = lg.get("session") or T.get("report_date")
        lines = [l for l in (ROOT / "notes" / "tape" / f"{sess}.md").read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
        ledger_tail = [l[:170] for l in lines[-4:]]
    except Exception:
        pass
    # THE TOP LINE (2026-09-17). The stack was stopped from 15:45 PT on the 16th to 13:23 PT on
    # the 17th and NOTHING said so: the scheduler row sat in this list between "live levels" and
    # "chart", one grey dot among seven, while the session traded a sheet built on a book two
    # sessions old. "Is the stack up" is not a service, it is the question the panel exists to
    # answer, so it goes above the list -- and it folds the services in, because a scheduler that
    # ticks while the feed is dead is the same silence from another direction.
    stack = None
    if stack_status is not None:
        try:
            stack = dict(stack_status.state(root=ROOT))
            quiet = [s["name"] for s in services if s.get("ok") is False]
            stack["services_down"] = quiet
            if quiet:
                stack["ok"] = False
                stack["line"] = " · ".join(filter(None, [stack.get("line"), ", ".join(quiet) + " not reporting"]))
        except Exception as ex:
            stack = dict(state="unknown", ok=False, why=f"{type(ex).__name__}: {ex}",
                         line=f"staleness check failed: {type(ex).__name__}")
    return jsonify(now=now.strftime("%Y-%m-%dT%H:%M:%SZ"), services=services, schedule=sc.get("jobs") or [], scheduler_updated=sc.get("updated"),
                   stack=stack, pulls=pulls[:8], build=build, gameplan=gameplan, post=post, ledger_tail=ledger_tail)

@cc.post("/api/stop")
def stop():
    """Stop the TeddyAI services from the page -- the Stop button, top right (Mark, 2026-09-08). It runs the
    project's own stop_teddy.ps1, which is what "Stop TeddyAI.bat" runs: teddy.py, live_feed.py, teddychart
    and their consoles, matched by what they run rather than by port.

    stop_teddy.ps1 kills THIS process, so the killer has to outlive the request. A detached PowerShell sleeps
    ~1.2 s and then runs the script, which leaves time for this response to reach the page -- otherwise the
    button always reports a failure it did not have. Nothing is force-killed here and no state is written: a
    pipeline mid-run dies where it stands, which is the same as closing the console window.

    The button is guarded on the page by click-to-arm, not here: a POST to this route is taken at its word."""
    ps = ROOT / "stop_teddy.ps1"
    if not ps.exists():
        return jsonify(ok=False, error=f"{ps.name} is not in the project folder"), 404
    try:
        if os.name == "nt":
            DETACHED = 0x00000008 | 0x00000200 | 0x08000000     # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
            subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                              f"Start-Sleep -Milliseconds 1200; & '{ps}'"],
                             cwd=str(ROOT), creationflags=DETACHED, close_fds=True)
        else:                                                    # dev box: nothing to orchestrate, just go
            subprocess.Popen(["sh", "-c", f"sleep 1.2; kill {os.getpid()}"], cwd=str(ROOT), close_fds=True)
        with _lock:
            running = bool(_state.get("running"))
        _log(f"[stop] requested from the page -- running {ps.name}" + (" (a pipeline run was in flight)" if running else ""))
        return jsonify(ok=True, running=running)
    except Exception as e:
        _log(f"[stop] failed: {e}")
        return jsonify(ok=False, error=str(e)), 500

_chart_cache = {"at": 0.0, "up": False}
def _chart_up():
    """Is teddychart answering on CHART_URL? Probed at most every 10 s. When it is, the page puts it in
    the chart row (footprint, large lots, TPO, VP, CVD, depth); the built-in canvas is the fallback."""
    now = time.time()
    if now - _chart_cache["at"] > 10:
        import urllib.request
        try:
            with urllib.request.urlopen(CHART_URL + "/api/levels", timeout=0.5) as r:
                _chart_cache["up"] = r.status == 200
        except Exception:
            _chart_cache["up"] = False
        _chart_cache["at"] = now
    return {"up": _chart_cache["up"], "url": CHART_URL}

def _last_job():
    """The most recent autopull job name and outcome, for the loading screen's subtitle."""
    try:
        st = json.loads(_read(ROOT / "notes" / "autopull-status.json"))
        job, rec = max(st.items(), key=lambda kv: kv[1].get("at", ""))
        return {"name": job, "ok": bool(rec.get("ok")), "at": rec.get("at", ""), "detail": rec.get("detail", "")}
    except Exception:
        return None

@cc.get("/api/bars")
def bars():
    tf = int(request.args.get("tf", 1))
    if BARS_FN is None:
        return jsonify([])
    b = BARS_FN(tf)
    with _lock:
        _state["feed"] = "live" if b and (time.time() * 1000 - b[-1]["t"]) < FEED_STALE_MIN * 60_000 else "stale"
    return jsonify(b)

@cc.get("/api/report")
def report():
    R = normalize()
    # a sheet newer than the last plan check (a hand-written commentary, an autopull build) gets checked here,
    # so the Gameplan tab's "before this plan" rows never depend on which path wrote the words
    _refresh_plan_check(cards=R.get("setups"), why="report")
    return jsonify(R)

# ----------------------------------------------------------------------------------
# Report normalization — trinity.json is every number; trinity.html carries the prose
# ----------------------------------------------------------------------------------
def _read(p): return Path(p).read_text(encoding="utf-8")

BOOKS = (("0dte", "0DTE"), ("near", "NEAR"), ("full", "FULL"))   # (page key, trinity.json panel)

def _num_at(m, k):
    return float(m.get(str(float(k)), 0) or 0)

def _ladder_keys(T, spot, em, last):
    """The same strike selection gen.py uses for the sheet, so the two ladders show the same rows."""
    P = T.get("panels") or {}
    g = [(P.get(b) or {}).get("gamma") or {} for _, b in BOOKS]
    vex = T.get("vex") or {}
    half = max(1.7 * em, 380); lo, hi = spot - half, spot + half
    if last:
        lo, hi = min(lo, last - 0.6 * half), max(hi, last + 0.6 * half)
    cand = {float(k) for m in (*g, vex) for k in m if lo <= float(k) <= hi}
    cand |= {float(k) for k in range(int(lo // 100 * 100), int(hi // 100 * 100) + 100, 100) if lo <= k <= hi}
    def weight(k):
        return max(abs(_num_at(g[0], k)) / 5.0, abs(_num_at(g[1], k)) / 12.0, abs(_num_at(g[2], k)) / 25.0,
                   abs(_num_at(vex, k)) / 8.0, 1.01 if k % 100 == 0 and abs(k - spot) <= 1.2 * em else 0.0)
    keys = sorted(k for k in cand if weight(k) >= 1.0)
    if len(keys) > 40: keys = sorted(sorted(keys, key=weight, reverse=True)[:40])
    return keys or sorted({float(round((spot + o) / 50) * 50) for o in range(-400, 401, 50)})

def normalize() -> dict:
    """trinity.json field names, as build.py / gen.py read them:
         spot (settle), last_print / last_at / last_vs_settle, em, rail_scale, rail_lo, rail_hi, rail_label,
         hours, settle, report_date, oi_vintage, expiries, vex_total, vex{strike:v},
         panels{0DTE|NEAR|FULL}{hvl:[flip], net_total, gamma{strike:v}}, confluence{strike:2|3}"""
    T = json.loads(_read(ROOT / "trinity.json"))
    H = _read(ROOT / "trinity.html") if (ROOT / "trinity.html").exists() else ""
    st = {}
    try: st = json.loads(_read(ROOT / "notes" / "autopull-status.json"))
    except Exception: pass

    P = T.get("panels") or {}
    spot = float(T.get("spot") or 0)
    last = T.get("last_print")
    last_at = T.get("last_at") or ""
    price = float(last) if last else spot
    em = float(T.get("em") or 0)
    scale = float(T.get("rail_scale") or 1.0)
    emr = em * scale                                   # the band actually drawn
    label = T.get("rail_label") or "1D"
    kind = ("Asia" if label.startswith(("Asia", "Rest of overnight")) else "RTH")   # page counts down to 20:00Z for RTH
    vintage = T.get("oi_vintage", "final")

    flips = {k: ((P.get(b) or {}).get("hvl") or [None])[0] for k, b in BOOKS}
    net = {k: (P.get(b) or {}).get("net_total") for k, b in BOOKS}
    gas = {}                                           # net gamma within the drawn band of PRICE (build.py _near)
    for k, b in BOOKS:
        g = (P.get(b) or {}).get("gamma") or {}
        gas[k] = round(sum(float(v) for s_, v in g.items() if abs(float(s_) - price) <= (emr or em)), 2) if price and (emr or em) else 0.0

    conf = T.get("confluence") or {}
    keys = _ladder_keys(T, spot, em, last)
    ladder = {}
    for k, b in BOOKS:
        g = (P.get(b) or {}).get("gamma") or {}
        rows = [[s_, _num_at(g, s_), "⬤⬤" if int(conf.get(str(float(s_)), 0) or 0) >= 3 else "⬤" if int(conf.get(str(float(s_)), 0) or 0) == 2 else ""]
                for s_ in keys]
        if flips[k] is not None:
            rows.append([round(float(flips[k]), 1), 0.0, "flip"])
        ladder[k] = sorted(rows, key=lambda r: -r[0])   # descending: the page marks spot on the first row <= ref

    src = (f"last print {last_at}" if last else f"settle {T.get('ref_symbol') or 'NQ'} {T.get('settle') or ''}")
    if vintage != "final": src += " · preliminary book"
    sets = _setups(H)                                  # parsed once: the window, entry quality and the page all read it
    from publication import identity, validated
    snapshot_id = identity(T)
    publication_ok = validated(ROOT, snapshot_id)
    try:
        win = _plan_window(T, sets)
        # each card carries its OWN start, so a rebuild that moved one level does not restart the others
        for c, g in zip(sets, _card_geo_by_card(sets)):
            cw = (win.get("cards") or {}).get(g) or {}
            if cw.get("live_from"):
                c["live_from"] = _iso(cw["live_from"])
                c["first_seen"] = _iso(cw["first_seen"])
    except Exception as e:                             # a window that cannot be computed must not take the report down
        _log(f"[plan-live] window failed: {e}")
        win = {}
    return {
        "session": {"date": T.get("report_date") or st.get("session"), "kind": kind, "hours": T.get("hours"),
                    "label": f"{T.get('report_date', '')} · {label}" + (" · preliminary book" if vintage != "final" else ""),
                    "settle": T.get("settle"), "expiries": T.get("expiries"), "oi_vintage": vintage,
                    "iv_atm": T.get("iv_atm"), "em": em, "asof": T.get("asof"),
                    # asof stays the PRICING instant. live_from is where card tracking starts -- see _plan_window.
                    "live_from": _iso(win["live_from"]) if win.get("live_from") else None,
                    "published_at": _iso(win["published_at"]) if win.get("published_at") else None,
                    "session_open": _iso(win["session_open"]) if win.get("session_open") else None,
                    "live_basis": win.get("basis")},
        "reference": {"price": price, "settle": spot, "source": src, "stale": "stale" in last_at,
                      "vs_settle": T.get("last_vs_settle")},
        "flips": {k: v for k, v in flips.items() if v is not None},   # an Asia build has no 0DTE panel
        "net": net,
        "gamma_at_spot": gas,
        "rails": {"lo": T.get("rail_lo"), "hi": T.get("rail_hi"), "scale": scale, "label": label, "em": emr},
        "vex": {"total": float(T.get("vex_total") or sum(float(v) for v in (T.get("vex") or {}).values())),
                "note": _text(H, r'class="note v"'),
                "by_strike": {k: v for k, v in (T.get("vex") or {}).items()},
                "by_expiry": T.get("by_expiry") or []},          # written by trinity_db.py from 2026-09-05 on
        "lead": _lead(H),
        "snapshot_id": snapshot_id,
        "publication": {"status": "passed" if publication_ok else "unverified"},
        "caveat": (_text(H, r'class="note w"') + ('' if publication_ok else ' Publication unverified: last saved commentary is shown. Generate a checked replacement before publication.')),
        "changes": _changes(H),                             # the sheet's "What changed" table, if the build drew one
        "reading": [{"t": "", "s": s_} for s_ in _items(H, "reading")],
        "setups": sets,
        "entry_quality": _entry_quality_safe(sets, T),
        "ladder": ladder,
        "history": _history(),
        "log": _tail(ROOT / "notes" / "autopull.log", 8),
    }

def _strip(s): return html.unescape(re.sub(r"<[^>]+>", " ", s)).split()
def _safe(s):
    """The sheet's prose with only its inline emphasis kept: <b>, <i>, and paragraph breaks (<br><br> -> <br>).
    Everything else -- classes, spans, the mono styling -- is dropped; the page restyles it. Whitespace is
    collapsed the way a browser would, so the words read as one paragraph, not as source lines."""
    s = re.sub(r"<br\s*/?>\s*<br\s*/?>", "\n\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"</?b(?:\s[^>]*)?>", lambda m: "</b>" if m.group(0).startswith("</") else "<b>", s, flags=re.I)
    s = re.sub(r"</?i(?:\s[^>]*)?>", lambda m: "</i>" if m.group(0).startswith("</") else "<i>", s, flags=re.I)
    s = re.sub(r"<(?!/?[bi]>)[^>]+>", " ", s)               # every other tag
    paras = [" ".join(p.split()) for p in s.split("\n\n")]
    return "<br><br>".join(p for p in paras if p)
def _lead(H):
    """The lead paragraph: the plain .note nearest the 'Reading the day' heading that is not the bullet list
    itself. Today's build puts it right above the heading; the 09-04 archived sheets put it just below, with a
    'what changed' note and a book note further up -- reading the first .note on the page picked those."""
    heading = H.find("Reading the day")
    cands = [(m.start(), m.group(1)) for m in re.finditer(r'<div class="note"[^>]*>(.*?)</div>', H, re.S) if "<ul" not in m.group(1) and "<li" not in m.group(1)]
    if not cands: return _text(H, r'class="note"')
    if heading < 0: return _safe(cands[0][1])
    return _safe(min(cands, key=lambda c: abs(c[0] - heading))[1])
def _text(H, cls_re):
    m = re.search(rf'<(div|p|section)[^>]*{cls_re}[^>]*>(.*?)</\1>', H, re.S)
    return _safe(m.group(2)) if m else ""
def _changes(H):
    """The sheet's 'What changed' block: {since, rows:[{k, a, b, d}]} or None when the build drew none.
    build.py renders it as .chg > .chgh (title + since) and .chr rows (.chk key, .chv from, .cha arrow, .chv to, .chd delta)."""
    m = re.search(r'<div class="chg">(.*?)</div>\s*<div style="height', H, re.S)
    if not m: return None
    blk = m.group(1)
    since = re.search(r'<span>(.*?)</span>', blk, re.S)
    rows = []
    for r in re.finditer(r'<div class="chr">(.*?)</div>\s*(?=<div class="chr">|$)', blk, re.S):
        cells = [" ".join(_strip(c)) for c in re.findall(r'<div class="ch[kvad][^"]*">(.*?)</div>', r.group(1), re.S)]
        if len(cells) >= 5: rows.append({"k": cells[0], "a": cells[1], "b": cells[3], "d": cells[4]})
    return {"since": " ".join(_strip(since.group(1))).lstrip("· ").strip() if since else "", "rows": rows} if rows else None
def _items(H, section_id):
    m = re.search(rf'id="{section_id}"[^>]*>(.*?)</(ul|section|div)>', H, re.S)
    return [_safe(li) for li in re.findall(r"<li[^>]*>(.*?)</li>", m.group(1), re.S)] if m else []
def _setups(H):
    """Read the cards back out of the rendered sheet. build.py's _cards() stamps every .pl card with
    data-side / data-entry / data-sl / data-tp (first target) / data-tps (all targets), so the levels
    are read exactly; the name, rank and why come from the markup inside the card."""
    out = []
    opens = [m for m in re.finditer(r'<div class="pl(?: pri)?"([^>]*)>', H)]
    for i, m in enumerate(opens):
        attrs = m.group(1)
        card = H[m.end(): opens[i + 1].start() if i + 1 < len(opens) else H.find('</section>', m.end())]
        nums = {k: _num(attrs, k) for k in ("entry", "tp", "sl")}
        tps = re.search(r'data-tps="([^"]+)"', attrs)
        tps_list = [float(x) for x in tps.group(1).split(",")] if tps else []
        if nums["entry"] is None:                      # a sheet built before the data-* attributes (archived days): read the level rows
            lv = {k: float(v.replace(",", "")) for k, v in re.findall(r'<div class="a">(Entry|Stop|TP\d)</div>\s*<div class="b2 mono">([\d,.]+)', card)}
            nums = {"entry": lv.get("Entry"), "sl": lv.get("Stop"), "tp": lv.get("TP1")}
            tps_list = [lv[k] for k in sorted(k for k in lv if k.startswith("TP"))]
        name = re.search(r"<h\d[^>]*>(.*?)</h\d>", card, re.S)
        rank = re.search(r'class="cv"[^>]*>(.*?)</div>', card, re.S)
        tag = re.search(r'class="tg"[^>]*>(.*?)</div>', card, re.S)
        sm = re.search(r'data-side="(long|short)"', attrs)
        if sm: side = sm.group(1)
        elif nums["entry"] is not None and (tps_list or nums["sl"] is not None):   # geometry, like setup_track: targets beyond the entry, stop behind it
            side = "long" if ((tps_list and tps_list[0] > nums["entry"]) or (nums["sl"] is not None and nums["sl"] < nums["entry"])) else "short"
        else: side = "short" if re.search(r"\b(fade|short|sell|below|losing)\b", card, re.I) else "long"
        why = re.search(r'class="wy"[^>]*>(.*?)</div>', card, re.S)
        out.append({"name": " ".join(_strip(name.group(1))) if name else "Setup", "side": side,
                    "rank": " ".join(_strip(rank.group(1))) if rank else "",
                    "tag": _safe(tag.group(1)) if tag else "",
                    "why": _safe(why.group(1)) if why else "",
                    "tps": tps_list, **nums})
    return out
def _num(card, key):
    m = re.search(rf'data-{key}="([\d.]+)"', card) or re.search(rf'{key}\D{{0,12}}([\d,]+\.?\d*)', card, re.I)
    return float(m.group(1).replace(",", "")) if m else None
def _history(n=12):
    """
    Rail containment, running %, one point per session -- did the session's last print close
    inside [rail_lo, rail_hi] of the plan built for it?

    archive/history.csv has no containment column (it records the rails, not the outcome), so
    the outcome is measured here against data/bars/<session>.json. Per session the LAST rth-kind
    row wins (the plan actually traded); sessions with no bar cache are skipped, not counted.
    Returns [] when nothing can be measured -- the widget says so instead of drawing a line.
    """
    import csv
    p = ROOT / "archive" / "history.csv"
    if not p.exists(): return []
    try: rows = list(csv.DictReader(p.open(encoding="utf-8")))
    except Exception: return []
    plan = {}
    for r in rows:
        if (r.get("kind") or "").startswith("rth") and r.get("rail_lo") and r.get("rail_hi"):
            plan[r["session"]] = r                            # later rows overwrite: last build wins
    hits, out = [], []
    for day in sorted(plan)[-n - 1:]:
        bf = ROOT / "data" / "bars" / f"{day}.json"
        if not bf.exists(): continue
        try:
            bars = json.loads(_read(bf))
            rth = [b for b in bars if "13:30" <= str(b["ts"])[11:16] < "20:00"]
            if not rth or str(rth[-1]["ts"])[11:16] < "19:50":
                continue        # session not complete in the cache (09-04 stalled at 13:39) -- no verdict, not a miss
            close = float(rth[-1]["c"]); r = plan[day]
            hits.append(float(r["rail_lo"]) <= close <= float(r["rail_hi"]))
        except Exception:
            continue
        out.append(round(100.0 * sum(hits) / len(hits)))
    return out[-n:]
def _tail(p, n):
    try: return "\n".join(_read(p).strip().splitlines()[-n:])
    except Exception: return ""

# ----------------------------------------------------------------------------------
# Commentary — lean, no disclaimers, written from trinity.json only
# ----------------------------------------------------------------------------------
PROMPT = """You are writing the TeddyAI commentary for an NQ gamma sheet. Input is trinity.json.
Write for a discretionary intraday trader who already knows the method. No disclaimers, no hedging boilerplate,
no restating the method. Every sentence must carry a number or a level.

Field guide: spot = the reference the rails are drawn on (the latest settle, or the live print on an intraday plan); ref_settle = the settle the book is priced on; last_print = where price is now, or null when the build had no
usable print -- then the sheet is drawn from spot and the tape summary opens with a line saying where price actually was;
panels.{0DTE,NEAR,FULL}.hvl[0] = that book's flip (call it "the 0DTE / near / full flip", never HVL);
panels.*.gamma = net dealer gamma per strike in futures per 1% move (call them "futures" or just the number, never "units");
panels.*.net_total = the column sum; rail_lo / rail_hi = the band drawn for THIS session, labelled rail_label
(say the label, e.g. "rest-of-overnight rails", not "O/N"); em = the FULL-DAY expected move, which is NOT the band
drawn -- only mention em when rail_scale < 1 and the distinction matters; hours = time to the front expiry;
oi_vintage = "preliminary" means the book will be revised, say so once; vex = vanna per vol point per strike.

Return ONLY JSON, no preamble, no fences:
{"lead": "<the only prose block that renders, so it carries the session: what changed since the last sheet and
          whether it was the book, the clock or the tape; the read; where the reference sits vs the flips and the
          rails; which strikes are heavy and which shelf is thin; the open-location branch; and what decides the
          session. Long is fine -- see the house method below for the order>",
 "setups": [{"name": "<short name, the level it leans on, e.g. 29,400 retest -- no letter prefix>",
             "tag": "<the trigger, then the entry zone, then the stop, then a sentence beginning VOID IF that
             names the swing that kills the card. e.g. Pullback into 29,600 that holds, entry 29,596 to 29,612,
             stop 29,578 one point under the 14:07 UTC swing. Risk 18 points. VOID IF a swing prints below
             29,579 and is not reclaimed>",
             "rank": "Primary|Secondary|Opportunistic", "side": "long|short",
             "entry": 0, "sl": 0, "zone_lo": 0, "zone_hi": 0, "tp": [0, 0, 0],
             "why": "<2-3 sentences of gamma structure: which strike absorbs, where the stop sits relative to
             structure, what each target is, and what argues against the card>"}],
 "vex_note": "<1-2 sentences on the vanna regime and where the mass sits, or empty if VEX barely moved>"}

How the setups are chosen, ranked, stopped and argued is THE HOUSE METHOD BELOW and is not repeated here. Read it.
It is the governing document; everything in this message is context for it.

ONE RULE OUTRANKS EVERYTHING ELSE IN IT, so it is also here: A CARD THAT IS IN PLAY AND NOT INVALIDATED IS CARRIED
FORWARD UNCHANGED -- same entry, same stop, same targets. The tape ledger below grades the previous sheet's cards;
read it FIRST. A rebuild is a new map, not a new opinion about an open trade. Never retire a live card, never
re-price it onto a nearer rung, and never manufacture a different setup in its place. Fill the remaining slots
around it. And a level the tape is holding keeps its card even where the candidate list grades it out -- say that
the tape is the reason when you do it.

The plan is for the REST of the session, written now. Gamma is standing open interest: a wall the tape already ran
overnight is not spent -- on 2026-09-04 the 29,600 wall was hit and rejected three separate times in one day. When
"what price has already done" and the level ledger are supplied, use them: say what each level did (held, broke,
untested), prefer levels that have already held, and write the card for the NEXT visit. Do not move every entry
beyond the range the tape has covered -- a session that stays inside its overnight range needs cards inside it.
An entry must be a level price has to travel to; never where price is now. At a rebuild inside the session, use the
tape ledger: a card whose level the tape has already triggered since the session opened is written for its NEXT visit
or withheld, and the lead says what the first minutes did to each level (2026-09-14: the 15:24 PT rebuild carried a
hold that had triggered in the reopen's first minute and a break that had triggered and been stopped in that same
minute, both as fresh cards).

Prefer -- do not force -- entries whose material strike sits within 40 points of the reference and whose structural
stop lands between 45 and 70 points ON A FULL-DAY PLAN -- on a partial session multiply both thresholds by rail_scale
(an Asia plan at 0.37x prefers a wall within 15 points and a stop of 17-26; the band was measured on RTH plans, so an
overnight stop of 22 is inside it, not tight). Measured over 57 sessions those cards ran 75.8% to their first target against
64.2% pooled, and stops beyond 70 points ran 46.7%. This is an UNCORRECTED subgroup on an edge that is itself only
p = 0.10, so it ranks cards you already like; it never justifies inventing a card at a far wall or skewing a stop to
fit the band. Where the structure only offers a distant wall, write that card and say so.
"""


def _card_rules() -> str:
    """CARD_RULES.md verbatim -- the pinned house method, sent with every commentary request.

    ONE FILE GOVERNS BOTH WRITERS. A scheduled gameplan run and a hand-written /build used to
    follow different rules: the prompt above and the build skill disagreed about whether a
    Primary exists when directional confidence is null, and nothing reconciled them (found
    2026-09-17). The rules travel in the PAYLOAD rather than as a path because cli_commentary
    forbids the CLI from reading files, and they are read per call rather than at import so an
    edit takes effect without restarting teddy.py.

    Missing or unreadable file: the prompt above stands on its own, exactly as before.
    """
    try:
        body = _read(ROOT / "CARD_RULES.md").strip()
    except Exception:
        return ""
    nl = chr(10)
    head = nl + nl + "===== HOUSE METHOD (CARD_RULES.md) =====" + nl
    head += "This governs. Where anything above conflicts with it, follow this." + nl + nl
    return head + body


def _state_doc():
    """The market-state document for the commentary payload, or None.

    Returns the LLM VIEW rather than the whole of state.json: a strict subset with
    no raw internals in it, so there is less for a model to recompute and quietly
    contradict. Never raises -- a missing or stale state.json means the commentary
    runs exactly as it did before this layer existed.
    """
    try:
        p = ROOT / "state.json"
        if not p.exists():
            return None
        doc = json.loads(_read(p))
        T = json.loads(_read(ROOT / "trinity.json"))
        src = doc.get("source") or {}
        # A state computed on a different book is worse than none: it would put
        # yesterday's structure into today's prose with today's confidence.
        if any(src.get(k) != T.get(k) for k in ('settle','report_date','asof')):
            _log(f"[comment] state.json is for {src.get('settle')}/{src.get('report_date')}, "
                 f"sheet is {T.get('settle')}/{T.get('report_date')} -- not sent")
            return None
        import engine
        return engine.for_llm(doc)
    except Exception as e:
        _log(f"[comment] state.json not usable: {type(e).__name__}: {e}")
        return None


STATE_GUIDE = """

A SECOND DOCUMENT is attached below trinity.json: `state.json`, the deterministic market-state
layer. Everything in it was COMPUTED. Use it for structure and ordering; never recompute any of it.

  levels[]          strikes already ranked and filtered, best first. `quality` is a ranking score
                    for ATTENTION, NOT a probability of holding -- see `caveats`. `topology` says
                    what formation the strike sits in (wall / shelf), `labels` adds cluster,
                    isolated_node or vacuum. Prefer these when choosing which levels to write about.
  topology[]        the shape of the book as intervals: lower, upper, sign, width, strength.
                    A `vacuum` is a stretch with nothing in it -- describe it as thin structure,
                    NEVER as a reason price will travel.
  market_state{}    six measured dimensions, each with its own confidence. `gamma.measures_agree`
                    false means the at-spot and chain-wide gamma disagree in SIGN: say so plainly,
                    it is a real and common state. `vol.percentile` ranks today against 502 past
                    sessions. `expiry.gross_share` and `net_share` are the 0DTE share on two
                    different cancellation conventions and routinely differ by 2x -- quote one and
                    name which.
  scenarios[]       conditional paths already generated: trigger, targets, invalidation. Quote a
                    trigger EXACTLY as written. These are ranked by how well SPECIFIED they are,
                    not by likelihood, so "base case" is a position in a ranking and not a forecast.
  confidence{}      six dimensions. `directional` is normally null, because gamma structure carries
                    no direction. If it is null, say the map has no directional view.
  caveats[]         measured results that constrain what may be claimed. Read them.

The setups you write are still yours, and the page still recomputes their stops from the gamma map.
But an entry or a target that is not a level in `levels`, a flip, a rail, or a scenario target will
be REJECTED by a checker, so build them from what is in the document.
"""


def _send_api(user_msg: str, _round: int = 0) -> str:
    """The Anthropic API call: one request, billed per rewrite, needs ANTHROPIC_API_KEY.

    Split out of regenerate_commentary so the validators below can be driven by a different
    sender without any of them changing -- see cli_commentary.send."""
    import urllib.request, urllib.error
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key: raise RuntimeError("ANTHROPIC_API_KEY not set")
    # Opus/Sonnet 5 think before they write and the thinking counts against max_tokens: Sonnet used ~1,500
    # tokens of it and Opus blew through 3,000 on the first try ("reply truncated at max_tokens"). Budget for
    # the thinking (it is what makes the structural reads better), keep it at medium effort, and give the
    # reply itself room. ~6-10k output tokens on Opus is 15-25 cents a rewrite.
    body = {"model": COMMENTARY_MODEL, "max_tokens": 16000,
            "thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"},
            "system": "You return exactly one JSON object and nothing else: no preamble, no markdown fences, no commentary after it.",
            "messages": [{"role": "user", "content": user_msg}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", method="POST",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        data=json.dumps(body).encode("utf-8"))
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:            # 4xx/5xx carry a JSON error body worth reading
        err = e.read().decode("utf-8", "replace")[:600]
        _save_reply(f"HTTP {e.code}\n{err}", tag=f"-r{_round}-http")
        raise RuntimeError(f"API HTTP {e.code}: {err}")
    data = json.loads(raw)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type", "text") == "text")
    _save_reply(f"model {data.get('model')}  stop {data.get('stop_reason')}  usage {data.get('usage')}\n\n{text}", tag=f"-r{_round}")
    if data.get("stop_reason") == "max_tokens":
        u = data.get("usage", {}); th = (u.get("output_tokens_details") or {}).get("thinking_tokens")
        raise RuntimeError(f"reply truncated at max_tokens ({u.get('output_tokens')} out, {th} thinking) -- raise max_tokens in command_center.py")
    return text


def regenerate_commentary(_retry: str = "", _round: int = 0, send=None):
    """Gets the plan written, writes the four prose blocks into build.py and updates NARRATIVE_FOR.
    Anchors both ends of each replaced block and asserts the span. The raw reply is always saved to
    notes/cc-last-reply.txt so a bad one can be read, not guessed at.

    `send` takes the built prompt and returns the model's raw reply. None means the Anthropic API
    (_send_api, billed per rewrite); cli_commentary.send drives the Claude Code CLI on a
    subscription instead. WHICHEVER wrote the words, they pass through the same card checker, the
    same structural-stop recompute, the same interpret.verify_commentary and the same _write_prose.
    The sender is the only thing that differs, so a plan is never less checked for having come the
    cheap way."""
    T = _read(ROOT / "trinity.json")
    tape = _tape_summary(json.loads(T))
    ledger = _tape_ledger_file(json.loads(T))
    if ledger:
        _log(f"[comment] tape ledger sent: {ledger.count(chr(10)) + 1} line(s) from "
             f"notes/tape/{json.loads(T).get('report_date')}.md")
    else:
        _log("[comment] no tape ledger for this session (tape_ledger.py not running, or an old file)")
    state = _state_doc()
    if not state:
        raise RuntimeError('Matching state.json is required before commentary generation')
    state_blob = ("\n\nstate.json (the deterministic market-state layer -- see the field guide above):\n"
                  + json.dumps(state, indent=1)) if state else ""
    if state:
        _log(f"[comment] state.json sent: {len(state.get('levels') or [])} ranked levels, "
             f"{len(state.get('scenarios') or [])} scenarios")
    user_msg = (PROMPT + _card_rules() + (STATE_GUIDE if state else "")
                + (("\n\nWhat price has already done (from the bar cache):\n" + tape) if tape else "")
                + (("\n\nTape ledger from the live monitor (events in the order they happened, UTC; the previous "
                    "sheet's cards graded as they went, its verdict on each card at each open, feed gaps, "
                    "rebuilds). A card that triggered, was stopped or was spent before the open is written for "
                    "its next visit or withheld -- say which:\n" + ledger) if ledger else "")
                + _swings_block(json.loads(T))
                + "\n\ntrinity.json:\n" + T + state_blob
                + (("\n\n" + _retry) if _retry else ""))
    gate = state.get('trade_gate') or {}
    # THE GATE'S NUMBERS, UP FRONT (2026-09-15). The prompt "preferred" entries within 40 points
    # while interpret.check_card_reach REJECTS anything beyond half the drawn band and
    # precommentary refuses a TP1 beyond the band -- rules the model only ever met in a
    # rejection message, and it failed three rounds three times in one evening writing cards
    # at 29,100-29,200 against a 49-point reach. Say the window before it writes.
    try:
        import interpret as _itp0
        _T0 = json.loads(T)
        _ref0 = float(_T0.get("last_print") or _T0.get("spot") or 0)
        _band0 = float(_T0.get("em") or 0) * float(_T0.get("rail_scale") or 1.0)
        _reach0 = getattr(_itp0, "REACH_EMS", 0.5) * _band0
        if _ref0 and _band0:
            user_msg += (f"\n\nREACH (hard rules, checked before anything else): the reference is {_ref0:,.2f} and "
                         f"the drawn band is {_band0:,.0f} points. Every entry MUST lie within {_reach0:,.0f} points "
                         f"of it, between {_ref0 - _reach0:,.0f} and {_ref0 + _reach0:,.0f}; a card outside that is "
                         f"removed. Every first target MUST lie within {_band0:,.0f} points of its entry, on a "
                         f"strike or a rail. Fewer cards inside the window beat four with one outside it.")
    except Exception:
        pass
    user_msg += ('\n\nCARD COUNT OVERRIDE: never manufacture four cards. Stand aside requires setups: []; '
                 'thin permits at most two and no more than scenarios_carried. Otherwise zero to four valid cards. '
                 'Explain why no trade is justified when returning zero. Current gate: ' + json.dumps(gate))
    if send is None:
        text = _send_api(user_msg, _round)
    else:
        text = send(user_msg)
        _save_reply(f"via {getattr(send, '__module__', '?')}.{getattr(send, '__qualname__', 'send')}\n\n{text}",
                    tag=f"-r{_round}")
    try:
        C = _extract_json(text)
    except RuntimeError as e:
        # A reply that will not parse is just another rejected reply, and the corrective round
        # already exists for exactly that. Until 2026-09-10 it aborted the whole run instead, so
        # one misplaced brace cost the plan: the CLI's round-1 answer closed a setup object
        # before its "why" key ( "tp":[...]},"why": ) and the 17:15 job would have posted nothing.
        # Cheap to ask again, and it costs a round rather than a session.
        if _round < 2:
            _log(f"[comment] reply did not parse ({e}) -- asking again (round {_round + 1})")
            return regenerate_commentary(
                _retry=f"Your previous reply was not valid JSON: {e}. Return ONE valid JSON object and "
                       f"nothing else -- check every brace and comma, and keep each setup's keys inside "
                       f"its own object.",
                _round=_round + 1, send=send)
        raise
    for k in ("lead", "setups"):
        if k not in C: raise RuntimeError(f"reply JSON has no '{k}' -- see notes/cc-last-reply.txt")
    # A card is named for the level it leans on, never lettered: Mark read A-D as grades
    # (2026-09-13). The prompt says so; this makes sure of it whatever the model returns,
    # so no scheduled regeneration can put the letters back on the sheet or in Discord.
    for c in C["setups"] if isinstance(C["setups"], list) else []:
        if isinstance(c, dict) and isinstance(c.get("name"), str):
            c["name"] = re.sub(r"^\s*[A-Da-d]\s*(?:[\u00b7\u2022:.)]|-{1,2})\s*", "", c["name"]).strip() or c["name"]
    # LAST ROUND: DROP WHAT IS BEYOND REACH, NOT THE PLAN (2026-09-15). Twice tonight the CLI
    # returned three cards inside reach and one 78 points out; the reach check sent the whole
    # reply back, and after three rounds the run died with nothing published, so the sheet sat
    # under a stale banner all evening. The reach rule stands (a card beyond 0.5 EM fills 22%
    # of the time); on the final round it removes the card rather than the plan.
    if _round >= 2 and isinstance(C.get("setups"), list) and len(C["setups"]) > 1:
        try:
            import interpret as _itp
            _T_r = json.loads(T)
            _keep, _dropped = [], []
            for c in C["setups"]:
                _ok1, _ = _itp.check_card_reach([c], _T_r)
                (_keep if _ok1 else _dropped).append(c)
            if _dropped and _keep:
                _log("[comment] final round: dropped " + ", ".join(str(c.get("name")) for c in _dropped)
                     + " -- beyond reach; the plan keeps the rest rather than fail")
                C["setups"] = _keep
        except Exception as e:
            _log(f"[comment] final-round reach drop unavailable: {type(e).__name__}: {e}")
    bad = _check_cards(C, state.get('trade_gate'))
    if not bad:
        T_ = json.loads(T)
        sbad, snotes = _structure_check(C["setups"], T_)
        if sbad and _round < 2: bad = "stops are structural, not a points band. " + sbad
        elif sbad:
            # last round: the map decides. Override the stops and let the checker re-test the R geometry.
            bars, _ = _day_bars(TICKS.status().get("clock_ms") if (REPLAY and TICKS.mode == "replay") else None)
            for c in C["setups"]:
                r = _struct_stop(c, T_, bars)
                if r["stop"] is not None: c["sl"] = r["stop"]
            _log(f"[comment] stops overridden by structure: {sbad}")
            bad = _check_cards(C, state.get('trade_gate'))
            if bad and _round < 3:
                # THE OVERRIDE IS OUR EDIT, NOT THE MODEL'S ERROR (2026-09-10). The line above
                # replaces the model's stop with the map's, which on a far wall widens the risk
                # -- 65 points to 95 on the 29,600 card -- and TP1 that cleared 1.2R against the
                # model's stop no longer clears it against ours. Throwing the reply away here
                # cost four consecutive plans, two of them unattended, while the other three
                # cards were fine. Hand back the stops we computed and let it move the targets.
                stops = "; ".join(f"{c.get('name')}: entry {c.get('entry')}, stop {c.get('sl')}"
                                  for c in C["setups"])
                _log(f"[comment] targets no longer clear 1.2R against the overridden stops ({bad})"
                     f" -- asking again (round {_round + 1})")
                return regenerate_commentary(
                    _retry=("The stops below were recomputed from the gamma map and are FINAL. Use them "
                            f"EXACTLY as given, and keep the entries too: {stops}. Against those stops your "
                            f"targets no longer clear the 1.2R minimum ({bad}). Return the same four cards "
                            "with those entries and stops, moving TP1 out until it is at least 1.2R and "
                            "keeping TP2 and TP3 beyond it in order."),
                    _round=_round + 1, send=send)
        else: _log("[comment] structural stops: " + "; ".join(snotes))
    if not bad and state:
        # THE SECOND GATE (2026-09-10). _check_cards tests the card's SHAPE and
        # _structure_check recomputes its STOP. Neither has ever checked whether the
        # entry and the TARGETS are levels that exist -- a card could name 29,555 as
        # T2 with no such strike on the board and nothing would catch it.
        #
        # interpret.verify() reads the whole reply back against state.json and also
        # catches an invented percentage, a reworded trigger, and a gamma sign
        # written as support or bullish. It runs only when a matching state.json was
        # sent, and its failures feed the corrective round that already exists.
        try:
            import interpret
            # Two checks with two different severities, both calibrated against a
            # sheet known to be correct. See interpret.verify_commentary.
            block, advise = interpret.verify_commentary(C, state, T_, tape)
            ok_l, lvl = interpret.check_card_levels(C.get("setups"), T_)
            block += lvl                      # a target that is not a level always blocks
            # ...and so does a card price cannot reach. 63% of every archived card
            # never filled; 39% of them sat beyond the one EM scenario.Config already
            # claims to enforce on trigger levels, and those filled at 22%. This sends
            # the reply back for a nearer level rather than dropping the card, so the
            # plan still carries four setups -- see interpret.check_card_reach.
            ok_r, rch = interpret.check_card_reach(C.get("setups"), T_)
            block += rch
            # STRUCTURE (2026-09-15). The same gate precommentary.py runs by hand before a
            # hand-written commentary -- the band against the clock, a live reference, the
            # absurdity floor, and the level-state policy (no continuation through a wall the
            # tape closed beyond and stayed) -- so a model-written plan is sent back for the
            # same reasons a hand-written one is refused. Mark: "before generating a commentary
            # make sure everything makes sense structurally... I dont want any ABSURD or
            # outlandish cards being outputted moving forward." Until this line the bot's path
            # checked shape, reach and "is it a level", and nothing else.
            try:
                import precommentary
                sb = precommentary.structure_problems(C.get("setups"), T_)
                if sb:
                    _log(f"[comment] structure check: {len(sb)} problem(s) -- " + "; ".join(s[:90] for s in sb[:3]))
                block += sb
            except Exception as e:
                _log(f"[comment] structure check unavailable: {type(e).__name__}: {e}")
            if advise:
                _log(f"[comment] narrative check: {len(advise)} advisory -- "
                     + "; ".join(a[:90] for a in advise[:3]))
            if block:
                _log(f"[comment] narrative check REJECTED: {len(block)} problem(s)")
                bad = "the narrative check rejected this reply. " + "; ".join(block[:6])
            else:
                _log("[comment] narrative check: clean")
        except Exception as e:
            raise RuntimeError(f'Required narrative checker failed: {type(e).__name__}: {e}') from e
    if not bad:
        # What each card's level did BEFORE the plan was written -- recorded and shown, never a reason to reject.
        # The 09-04 replay proved the rejection wrong: the four cards it threw out were the four that triggered in RTH.
        pre, checked, note = _pre_plan(C["setups"], T_)
        with _lock:
            _state["plan_check"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "checked": checked, "note": note, "cards": pre}
        _save_plan_check()
        _log(f"[comment] plan check: {note}" + (" -- " + "; ".join(f"{k}: {v['text']}" for k, v in pre.items() if v.get('state') not in (None, 'pending')) if pre else ""))
    if bad and _round < 2:
        # a corrective round: the same brief plus what was wrong with the reply
        _log(f"[comment] reply rejected ({bad}) -- asking again (round {_round + 1})")
        return regenerate_commentary(_retry=f"Your previous reply was rejected: {bad}. Return the corrected JSON object only, "
                                            f"with scenarios for the rest of the session.", _round=_round + 1, send=send)
    if bad: raise RuntimeError(f"reply rejected {_round + 1} times: {bad} -- see notes/cc-last-reply.txt")
    _write_prose(C, json.loads(T))


def _day_bars(now_ms=None, min_hours: float = 0):
    """1-min bars for the current trading day (Globex open -> now), [{t(ms),o,h,l,c}] asc. In a replay they
    are built from the replayed prints up to the virtual clock, otherwise from BARS_FN (teddy.py's bar cache).
    Returns (bars, newest_ms) -- the caller decides whether 'newest' is fresh enough to trust."""
    if REPLAY and TICKS.mode == "replay":
        st = TICKS.status(); clock = st.get("clock_ms")
        ts, pxs, *_ = TICKS.since(0)
        out = []
        for t, p in zip(ts, pxs):
            if clock and t > clock: break
            m = int(t // 60000 * 60000)
            if out and out[-1]["t"] == m:
                b = out[-1]; b["h"] = max(b["h"], p); b["l"] = min(b["l"], p); b["c"] = p
            else: out.append({"t": m, "o": p, "h": p, "l": p, "c": p})
        return out, (out[-1]["t"] if out else None)
    try: rows = BARS_FN(1) if BARS_FN else []
    except Exception: rows = []
    if not rows: return [], None
    import datetime as _dt
    now = _dt.datetime.fromtimestamp((now_ms or time.time() * 1000) / 1000, _dt.timezone.utc)
    open_dt = cc_ticks.trading_day_open(now)
    if min_hours and (now - open_dt).total_seconds() < min_hours * 3600:
        # an evening build: the new trading day is minutes old, so "today" is the session that just closed
        open_dt = cc_ticks.trading_day_open(open_dt - _dt.timedelta(hours=1))
    open_ms = open_dt.timestamp() * 1000
    day = [b for b in rows if b["t"] >= open_ms]
    return day, (rows[-1]["t"] if rows else None)


TEST_FRAC = 0.25   # a card is TESTED when price came within this fraction of its own risk of the entry and
                   # turned without touching it. 2026-09-07: the Sun-Mon tape put in its low at 29,530.50 against a
                   # 29,530 entry -- the most useful thing the weekend said about that card, and the old three
                   # states (pending / played / invalidated) had no way to say it.

def _fold_card(c, bars):
    """The page's tracker logic on bars: triggered when a bar spans the entry, then stop vs T1..T3 in bar order.
    Returns (state, at) with state in pending / tested / active / played / invalid and
    at = {entry, tp0.., sl: t(ms)} -- plus approach / approach_t / approach_pts for a tested card."""
    # cards arrive two ways: the commentary's JSON carries tp as a list; the sheet-parsed setups (normalize)
    # carry tp = T1 and tps = [T1, T2, T3], the same shape the page's tpsOf() reads
    raw = c.get("tps") or c.get("tp")
    tps = [float(x) for x in (raw if isinstance(raw, (list, tuple)) else [raw]) if x is not None]
    e, sl, side = float(c["entry"]), float(c["sl"]), c["side"]
    trig, best, at = False, -1, {}
    risk = abs(e - sl)
    near = None                      # (pts short of the entry, price, t) -- the closest a bar came without spanning it
    hit_tp = lambda b, t: (b["h"] >= t) if side == "long" else (b["l"] <= t)
    for b in bars:
        hit_sl = (b["l"] <= sl) if side == "long" else (b["h"] >= sl)
        if not trig:
            if b["l"] <= e <= b["h"]:
                trig = True; at["entry"] = b["t"]
                # A trigger bar that also spans the stop or a target cannot be
                # ordered at 1-minute resolution (setup_track flags the same
                # case). Record the entry, say it is ambiguous, grade from the
                # next bar on -- never resolve it in the flattering direction.
                if hit_sl or any(hit_tp(b, t) for t in tps):
                    at["ambiguous_entry"] = b["t"]
            else:
                # side-agnostic: the bar sat wholly above or wholly below the entry; how close did its edge come?
                d, px = ((e - b["h"], b["h"]) if e > b["h"] else (b["l"] - e, b["l"]))
                if near is None or d < near[0]: near = (d, px, b["t"])
            continue
        # Order inside a one-minute bar is unknowable. The old code recorded
        # the targets first and then the stop, so a bar that touched both was
        # graded "played" -- a bias that only ever turned losses into wins.
        # setup_track.py flags the same case as ambiguous; here the stop wins
        # the tie for any target the bar touches in the SAME minute, and the
        # ambiguity is recorded so the card can say so.
        new_tps = [j for j, t in enumerate(tps) if hit_tp(b, t) and f"tp{j}" not in at]
        if hit_sl:
            at["sl"] = b["t"]
            if new_tps:
                at["ambiguous"] = b["t"]          # stop and target(s) in one bar
            # "played" only when a target was banked on an EARLIER bar
            return ("played" if best >= 0 else "invalid"), at
        for j in new_tps:
            at[f"tp{j}"] = b["t"]; best = max(best, j)
        if best == len(tps) - 1: return "played", at
    if trig: return "active", at
    if near is not None and risk and near[0] <= TEST_FRAC * risk:
        at["approach_pts"], at["approach"], at["approach_t"] = near
        return "tested", at
    return "pending", at


def _bars_between(t0_ms, t1_ms):
    """1-min bars with t0 <= t <= t1, [{t(ms),o,h,l,c}] asc. Read from data/bars/<UTC-date>.json -- the
    dashboard's own per-day cache -- for every UTC day in the window, with BARS_FN folded in for the freshest
    day. cc_bars() only spans the previous and current UTC day, which is why the files are read directly: a
    Tuesday plan built on Friday's settle has three days of tape before its asof. De-duplicated by minute,
    later source wins."""
    import datetime as _dt
    by_t = {}
    d0 = _dt.datetime.fromtimestamp(t0_ms / 1000, _dt.timezone.utc).date()
    d1 = _dt.datetime.fromtimestamp(t1_ms / 1000, _dt.timezone.utc).date()
    d = d0
    while d <= d1:
        p = ROOT / "data" / "bars" / f"{d.isoformat()}.json"
        if p.exists():
            try:
                for r in json.loads(_read(p)):
                    t = _dt.datetime.fromisoformat(str(r["ts"]).replace(" ", "T").replace("Z", "+00:00"))
                    ms = int(t.timestamp() // 60 * 60) * 1000
                    by_t[ms] = {"t": ms, "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]), "c": float(r["c"])}
            except Exception as e:
                _log(f"[plan-check] {p.name}: {e}")
        d += _dt.timedelta(days=1)
    try:
        for b in (BARS_FN(1) if BARS_FN else []):
            by_t[int(b["t"])] = {"t": int(b["t"]), "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]), "c": float(b["c"])}
    except Exception:
        pass
    return [by_t[k] for k in sorted(by_t) if t0_ms <= k <= t1_ms]


def _globex_open_at(dt):
    """Is CME Globex trading equity-index futures at aware-UTC instant dt? Open 18:00 ET the day before a
    session, close 17:00 ET (settlement is 16:00 but the tape runs an hour past it); on a Globex-only holiday
    the 12:00 CT halt; nothing on a weekend or a full closure. Built on nqvol's calendar so the sheet, the
    pull and this check all disagree with the exchange in the same places, if they ever do."""
    import datetime as _dt, nqvol
    d = dt.date()
    for cand in (d, d + _dt.timedelta(days=1)):
        if not nqvol.unit_weight(cand):
            continue
        o = nqvol.open_utc(cand)
        c = nqvol.close_utc(cand) if nqvol.is_globex_holiday(cand) else nqvol.et(cand, 17)
        if o <= dt < c:
            return True
    return False


def _expected_newest_ms(now_ms):
    """The last minute the tape SHOULD hold at now_ms: now itself when the market is open, otherwise the
    last open minute before it. Walks back a minute at a time, at most four days (a long weekend plus a
    holiday), so a closed market is never mistaken for a dead feed -- the 09-07 premarket run at 13:26 PT
    flagged a 16:59 UTC print as '3.5 HOURS old, the cache is NOT advancing' on Labor Day."""
    import datetime as _dt
    t = _dt.datetime.fromtimestamp(now_ms / 1000, _dt.timezone.utc).replace(second=0, microsecond=0)
    for _ in range(4 * 24 * 60):
        if _globex_open_at(t):
            return int(t.timestamp() * 1000)
        t -= _dt.timedelta(minutes=1)
    return now_ms


# ----------------------------------------------------------------------------------
# Plan lifetime -- when a plan goes live, and how long it is the plan
# ----------------------------------------------------------------------------------
# trinity.json's "asof" is a PRICING instant: trinity_db.py prices the greeks and counts time to expiry from
# it (13:30 UTC for a premarket RTH plan). It was never meant to be the moment card tracking starts, but the
# page used it for both, so a plan written Monday evening for Tuesday's RTH threw away every print until
# 13:30 Tuesday. On 2026-09-07 that showed: card A triggered at 23:16 and hit T1 at 00:02 on the reopened
# Globex session, the server's own check said so in notes/plan_check.json, and every card on the screen still
# read "Not triggered". A plan is live from the moment it is published and stays the plan until the next OI
# book replaces it (Mark, 2026-09-07), so tracking runs on the plan's own trading day, not on its pricing
# instant.
#
#   live_from = max(Globex open of the session the plan is for, when the plan was first published)
#
#   * the Globex open is the CME day boundary -- 18:00 ET the evening before -- the same boundary that lets an
#     Asia entry run through London and premarket instead of being cut at a session line
#   * the publication floor is what keeps it honest: a card is never graded on tape that had already printed
#     when the words were written. A REBUILD of the same plan does not move the floor, so a re-stamp or a
#     commentary rewrite can never retroactively disqualify a trigger that was real when it happened.
#
# Nothing here writes to trinity.json or touches asof: the pull, the ladder and the sheet are unchanged.

PLAN_LIVE_PATH = ROOT / "notes" / "plan_live.json"
PLAN_LIVE_KEEP = 12      # plans remembered in that file; a day makes one or two (a rebuild that moves a level
                         # is a different plan and gets its own floor)

def _card_geo1(c):
    """One card as comparable geometry: name + entry + stop + targets. None when the card has no levels."""
    if not c or c.get("entry") is None or c.get("sl") is None:
        return None
    raw = c.get("tps") or ([c.get("tp")] if c.get("tp") is not None else [])
    tps = ",".join(f"{float(x):g}" for x in raw if x is not None)
    return f"{c.get('name', '?')}|{float(c['entry']):g}|{float(c['sl']):g}|{tps}"


def _card_geo_by_card(cards):
    """Geometry per card, positionally aligned with the list handed in."""
    return [_card_geo1(c) for c in (cards or [])]


def _card_geo(cards):
    """A card list as comparable geometry, order-independent."""
    return sorted(g for g in _card_geo_by_card(cards) if g)


def _plan_id(T, cards):
    """A plan's identity: the book it is priced on, the moment it is priced to, and the geometry of its
    cards. Two builds of the SAME plan (a re-stamp, a commentary rewrite that leaves the levels alone) share
    an id; a new book, a repriced intraday build or a moved level does not."""
    import hashlib
    key = "|".join([str(T.get("settle")), str(T.get("report_date")), str(T.get("asof"))] + _card_geo(cards))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _published_at(T, cards):
    """(ms, source) -- when this plan was first published. Recorded in notes/plan_live.json the first time the
    plan is seen and never moved afterwards.

    On FIRST sight the record is bootstrapped from file mtimes, earliest wins: trinity.html, and any
    sheets/<report_date>*teddyai.html carrying the same cards. A plan can have been on the screen for hours
    before this code existed (the 09-08 plan first published 22:26 UTC and was re-stamped at 00:10), and the
    current trinity.html mtime is the LAST build, not the first -- taking it alone would disqualify a trigger
    that came in between. From the first record on, the file is the authority and mtimes are not read again."""
    store = _plan_live_store()
    at, src = _published_at_in(T, cards, store, _published_candidates(T))
    _plan_live_save(store)
    return at, src


def _plan_live_store():
    try: s = json.loads(_read(PLAN_LIVE_PATH))
    except Exception: s = {}
    return {"plans": s.get("plans") or {}, "cards": s.get("cards") or {}, "_dirty": False}


def _plan_live_save(store):
    if not store.pop("_dirty", False):
        return
    store["plans"] = dict(sorted(store["plans"].items(),
                                 key=lambda kv: -int(kv[1].get("recorded_at") or 0))[:PLAN_LIVE_KEEP])
    store["cards"] = dict(sorted(store["cards"].items(),
                                 key=lambda kv: -int(kv[1].get("recorded_at") or 0))[:PLAN_LIVE_KEEP * 6])
    try:
        PLAN_LIVE_PATH.write_text(json.dumps(store, indent=1), encoding="utf-8")
    except Exception as e:
        _log(f"[plan-live] could not write {PLAN_LIVE_PATH.name}: {e}")


def _published_candidates(T):
    """Every rendered copy of this plan's day that can date it: [(mtime ms, filename, {card geometry})] ascending.
    trinity.html plus sheets/<report_date>*teddyai.html. archive/ is deliberately not read -- it is the record of
    what was built, and its copies are written later than the build they hold."""
    out = []
    try:
        p = ROOT / "trinity.html"
        out.append((int(p.stat().st_mtime * 1000), p.name, set(_card_geo(_setups(_read(p))))))
    except Exception:
        pass
    rd = str(T.get("report_date") or "")
    if rd:
        for p in sorted((ROOT / "sheets").glob(f"{rd}*teddyai.html")):
            try: out.append((int(p.stat().st_mtime * 1000), p.name, set(_card_geo(_setups(_read(p))))))
            except Exception: pass
    return sorted(out, key=lambda r: r[0])


def _published_at_in(T, cards, store, cands):
    pid, geo = _plan_id(T, cards), set(_card_geo(cards))
    rec = store["plans"].get(pid) or {}
    if rec.get("published_at"):
        return int(rec["published_at"]), str(rec.get("source") or "recorded")
    hit = next(((ms, nm) for ms, nm, g in cands if g == geo), None)
    at, src = hit if hit else (int(time.time() * 1000), "first seen")
    store["plans"][pid] = {"published_at": at, "source": src, "report_date": T.get("report_date"),
                           "settle": T.get("settle"), "asof": T.get("asof"), "cards": len(geo),
                           "recorded_at": int(time.time() * 1000)}
    store["_dirty"] = True
    _log(f"[plan-live] plan {pid} published {_iso(at)} (from {src}) -- "
         f"{T.get('report_date')} {len(geo)} cards")
    return at, src


# A card's own floor, not the plan's. Mark, 2026-09-08: a rebuild that moves ONE level should not wipe the
# tracked history of the three cards it left alone. A card is identified by its geometry within a book
# (report_date + settle + name|entry|sl|targets), so an untouched card keeps the moment it was first
# published and a moved one starts from the rebuild -- which is right, it is a level nobody had seen before.
def _card_first_seen(T, cards, store, cands):
    """{card geometry: (ms, source)} -- when each card was first published. Same bootstrap as the plan floor:
    the earliest rendered copy of the day that carries that exact card."""
    out, key0 = {}, f"{T.get('report_date')}|{T.get('settle')}|"
    for g in _card_geo(cards):
        rec = store["cards"].get(key0 + g) or {}
        if rec.get("first_seen"):
            out[g] = (int(rec["first_seen"]), str(rec.get("source") or "recorded"))
            continue
        hit = next(((ms, nm) for ms, nm, gs in cands if g in gs), None)
        at, src = hit if hit else (int(time.time() * 1000), "first seen")
        store["cards"][key0 + g] = {"first_seen": at, "source": src, "recorded_at": int(time.time() * 1000)}
        store["_dirty"] = True
        _log(f"[plan-live] card {g.split('|')[0]} first published {_iso(at)} (from {src})")
        out[g] = (at, src)
    return out


def _iso(ms):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _session_open_ms(T):
    """The Globex open of the session the plan is for: 18:00 ET the evening before report_date. Falls back to
    the trading day containing asof when report_date is missing, and to None when neither can be read."""
    import datetime as _dt
    try:
        import nqvol
        return int(nqvol.open_utc(_dt.date.fromisoformat(str(T.get("report_date"))[:10])).timestamp() * 1000)
    except Exception:
        pass
    try:
        asof = _dt.datetime.fromisoformat(str(T.get("asof")).replace("Z", "+00:00"))
        if asof.tzinfo is None: asof = asof.replace(tzinfo=_dt.timezone.utc)
        return int(cc_ticks.trading_day_open(asof).timestamp() * 1000)
    except Exception:
        return None


def _plan_window(T, cards):
    """The plan's tracking window, for the page and for the pre-plan check:
         published_at  when the words were written (never moves for a given plan)
         session_open  the Globex open of the session the plan is for
         live_from     max of the two -- where card tracking starts
         basis         which of the two set live_from, for the tooltip
         cards         {geometry: {live_from, first_seen}} -- each card's OWN floor, which is earlier than the
                       plan's whenever the card survived a rebuild that moved a different level
    Everything between the book's settle and live_from stays what it always was: information under the card,
    not a graded result."""
    store, cands = _plan_live_store(), _published_candidates(T)
    pub, src = _published_at_in(T, cards, store, cands)
    seen = _card_first_seen(T, cards, store, cands)
    _plan_live_save(store)
    op = _session_open_ms(T)
    if op is None:
        w = {"published_at": pub, "published_src": src, "session_open": None,
             "live_from": pub, "basis": "publication (no session open for this plan)"}
    else:
        w = {"published_at": pub, "published_src": src, "session_open": op, "live_from": max(op, pub),
             "basis": ("the Globex open of the session the plan is for" if op >= pub else
                       "when the plan was published -- later than the Globex open, so the tape before it "
                       "is not graded")}
    w["cards"] = {g: {"first_seen": at, "first_seen_src": s2, "live_from": max(op or 0, at)}
                  for g, (at, s2) in seen.items()}
    return w


def _pre_plan(cards, T=None):
    """What each card's level did between the BOOK'S SETTLE and the moment the plan describes -- information
    for the card ("before this plan: triggered 02:34, stopped 11:22, invalidated"; "tested, low 29,530.50,
    held"), never a rejection. Returns ({name|entry: {state, text, ...}}, checked, note).

    Window (2026-09-07): from the settle instant of the chain the plan is built on (T["settle"], 16:00 ET) to
    the plan's asof (T["asof"]) -- not "the current trading day". The old window lost the Sun-Mon Globex
    session the moment Tuesday's trading day opened, which is exactly the tape a Tuesday plan built on
    Friday's book needs to know about. A plan whose asof is still ahead of the clock is checked up to now.

    Staleness (2026-09-07): "stale" means bars are missing that the exchange calendar says should exist,
    not "older than 30 minutes". A holiday halt or a weekend is a closed market, not a dead feed.

    checked=False, with the note saying so, when the window has no bars or the newest bar trails what the
    calendar expects by more than FEED_STALE_MIN."""
    import datetime as _dt
    hhmm = lambda ms: _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).strftime("%H:%M")
    dhm  = lambda ms: _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).strftime("%a %H:%M")
    if REPLAY and TICKS.mode == "replay":
        # replay: the virtual clock's own day, as before -- the replayed prints are the only tape there is
        now_ms = TICKS.status().get("clock_ms")
        bars, newest = _day_bars(now_ms)
        if not bars:
            return {}, False, "no replayed prints yet -- the overnight was not checked"
        t0, t1 = bars[0]["t"], bars[-1]["t"]
    else:
        if T is None:
            try: T = json.loads(_read(ROOT / "trinity.json"))
            except Exception: T = {}
        now_ms = int(time.time() * 1000)
        try:
            import nqvol
            sd = _dt.date.fromisoformat(str(T.get("settle"))[:10])
            t0 = int(nqvol.close_utc(sd).timestamp() * 1000)
        except Exception:
            t0 = int(cc_ticks.trading_day_open(_dt.datetime.now(_dt.timezone.utc)).timestamp() * 1000)
        try:
            asof = _dt.datetime.fromisoformat(str(T.get("asof")).replace("Z", "+00:00"))
            if asof.tzinfo is None: asof = asof.replace(tzinfo=_dt.timezone.utc)
            t1 = min(int(asof.timestamp() * 1000), now_ms)
        except Exception:
            t1 = now_ms
        bars = _bars_between(t0, t1)
        if not bars:
            return {}, False, f"no bars between the settle ({dhm(t0)} UTC) and the plan -- the tape before it was not checked"
        newest = bars[-1]["t"]
        expected = _expected_newest_ms(t1)
        if expected - newest > FEED_STALE_MIN * 60_000:
            return {}, False, (f"bar cache stale (newest {dhm(newest)} UTC, the calendar expects tape to "
                               f"{dhm(expected)}) -- the tape before the plan was not checked")
    # 2026-09-14: when the session the plan is for has already opened inside the window, the fold that matters is
    # the one SINCE that open -- the reopen's first minutes -- and the tape before it is a note. The Sunday plan on
    # Thursday's book reported "triggered Thu 22:47 · stopped Fri 00:07 · invalidated" for the 29,100 card and
    # stopped there, while the reopen's first six minutes had triggered that card and paid T1 and T2.
    try: op = _session_open_ms(T)
    except Exception: op = None
    split = op is not None and bars[0]["t"] < op <= bars[-1]["t"]
    def describe(c, state, at):
        hits = " · ".join(f"T{j + 1} {hhmm(at[f'tp{j}'])}" for j in range(4) if f"tp{j}" in at)
        if state == "pending":
            return "untested"
        if state == "tested":
            return (f"tested {hhmm(at['approach_t'])} · {'low' if c['side'] == 'long' else 'high'} "
                    f"{at['approach']:,.2f} · held ({at['approach_pts']:g} pts from the entry)")
        return (f"triggered {hhmm(at['entry'])}" + (f" · {hits}" if hits else "")
                + (f" · stopped {hhmm(at['sl'])}" if "sl" in at else "")
                + {"played": " · played", "invalid": " · invalidated", "active": " · still open"}[state])
    out = {}
    for c in cards:
        if split:
            state, at = _fold_card(c, [b for b in bars if b["t"] >= op])
            text = "since the open: " + describe(c, state, at)
            pre_state, pre_at = _fold_card(c, [b for b in bars if b["t"] < op])
            if pre_state != "pending":
                text += " · before the open: " + describe(c, pre_state, pre_at)
        else:
            state, at = _fold_card(c, bars)
            text = describe(c, state, at)
        rec = {"state": state, "text": text}
        for k in ("approach", "approach_t", "approach_pts", "entry", "sl"):
            if k in at: rec[k] = at[k]
        out[f"{c.get('name', '?')}|{float(c['entry']):g}"] = rec
    return out, True, (f"levels checked against {len(bars)} bars, {dhm(bars[0]['t'])} to {dhm(bars[-1]['t'])} UTC, "
                       f"from the book's settle to the plan" + (" -- cards graded since the session's open" if split else ""))


def _refresh_plan_check(cards=None, T=None, force=False, why=""):
    """Run the pre-plan check whenever the sheet on disk is newer than the last check -- so a commentary
    written by hand into build.py (2026-09-07, the 9/8 sheet) is checked the same as one the Rewrite button
    wrote. Cheap: a few thousand bars and four folds. Keyed on trinity.html's mtime; force=True ignores it."""
    try:
        mt = (ROOT / "trinity.html").stat().st_mtime
    except Exception:
        return False
    pc = _state.get("plan_check") or {}
    if not force and pc.get("sheet_mtime") == mt:
        return False
    try:
        if cards is None:
            cards = normalize().get("setups") or []
        pre, checked, note = _pre_plan(cards, T)
    except Exception as e:
        _log(f"[plan-check] failed: {e}")
        return False
    with _lock:
        _state["plan_check"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "checked": checked, "note": note,
                                "cards": pre, "sheet_mtime": mt}
    stamped = _stamp_sheet(_state["plan_check"])
    if stamped:
        # the stamp rewrote trinity.html; key on the file as it is now, or the next report re-checks and
        # re-stamps forever
        try:
            with _lock: _state["plan_check"]["sheet_mtime"] = (ROOT / "trinity.html").stat().st_mtime
        except Exception: pass
    _save_plan_check()
    acted = "; ".join(f"{k}: {v['text']}" for k, v in pre.items() if v.get("state") not in (None, "pending"))
    _log(f"[plan-check{(' ' + why) if why else ''}] {note}" + (" -- " + acted if acted else "")
         + (f" -- stamped {', '.join(stamped)}" if stamped else ""))
    return True


STAMP_RE = re.compile(r'\s*<div class="pc"[^>]*>.*?</div>', re.S)

def _stamp_sheet(pc):
    """Write the pre-plan check INTO the built sheet, one line per card, so the report says what the tape did to
    a level before the plan -- not just the dashboard. Idempotent: an existing stamp is replaced. The line goes
    after the card's why-block; _setups() reads the why with a non-greedy match to its own </div>, so the
    stamp is invisible to the parser, and build.py is not touched (the stamp is inline-styled).

    Stamps trinity.html and the sheet in sheets/ that is the same build (same cards, written within ten
    minutes of it). The archive copy is left alone: it is the record of what was built, and the check is
    derived from it. When the check could not run (no bars, stale feed) the stamps are REMOVED, so a sheet
    never carries a line from an older check. Returns the names of the files it rewrote."""
    cards = pc.get("cards") or {}
    checked = bool(pc.get("checked"))
    at = str(pc.get("at") or "")[11:16]
    def line_for(name, entry):
        key = f"{name}|{float(entry):g}"
        v = cards.get(key)
        if not checked or not v: return None
        st = v.get("state")
        txt = html.escape(v.get("text") or "")
        tick = "&#10003; " if st == "tested" else ""
        col = "var(--ink3)" if st == "pending" else ("var(--neg)" if st == "invalid" else "var(--ink)")
        return (f'<div class="pc" data-plan-check="{at}" style="padding:8px 16px 10px;border-top:1px dashed var(--rule);'
                f'font-size:12px;color:{col}" title="what the tape did to this level between the book\'s settle and '
                f'the moment the plan describes (checked {at})">Before this plan · {tick}{txt}</div>')
    def stamp_text(H):
        H = STAMP_RE.sub("", H)
        if not checked: return H
        opens = [m for m in re.finditer(r'<div class="pl(?: pri)?"([^>]*)>', H)]
        out, pos = [], 0
        for i, m in enumerate(opens):
            end = opens[i + 1].start() if i + 1 < len(opens) else H.find("</section>", m.end())
            card = H[m.end():end]
            name = re.search(r"<h\d[^>]*>(.*?)</h\d>", card, re.S)
            entry = _num(m.group(1), "entry")
            wy = re.search(r'<div class="wy"[^>]*>.*?</div>', card, re.S)
            if not (name and entry is not None and wy):
                continue
            ln = line_for(" ".join(_strip(name.group(1))), entry)
            if not ln: continue
            cut = m.end() + wy.end()
            out.append(H[pos:cut]); out.append("\n    " + ln); pos = cut
        out.append(H[pos:])
        return "".join(out)
    done = []
    main = ROOT / "trinity.html"
    try:
        H = _read(main)
    except Exception:
        return done
    targets = [main]
    try:
        mt = main.stat().st_mtime
        sig = re.findall(r'<div class="pl(?: pri)?"([^>]*)>', H)
        for f in sorted((ROOT / "sheets").glob("*.html"), key=lambda f: f.stat().st_mtime, reverse=True):
            if abs(f.stat().st_mtime - mt) > 600: continue
            try:
                if re.findall(r'<div class="pl(?: pri)?"([^>]*)>', _read(f)) == sig: targets.append(f); break
            except Exception: pass
    except Exception:
        pass
    for f in targets:
        try:
            old = _read(f) if f != main else H
            new = stamp_text(old)
            if new != old:
                f.write_text(new, encoding="utf-8"); done.append(f.name)
        except Exception as e:
            _log(f"[plan-check] stamp failed for {f.name}: {e}")
    return done


def _save_plan_check():
    try: (ROOT / "notes" / "plan_check.json").write_text(json.dumps(_state["plan_check"]), encoding="utf-8")
    except Exception: pass
def _load_plan_check():
    try: _state["plan_check"] = json.loads(_read(ROOT / "notes" / "plan_check.json"))
    except Exception: pass
_load_plan_check()


def _tape_ledger_file(T: dict, max_lines: int = 80) -> str:
    """The live monitor's ledger for the plan's session, if one is being written (tape_ledger.py
    appends one line per event to notes/tape/<session>.md). Fresh means touched in the last day;
    an old file is some other night's tape and stays out of the prompt."""
    try:
        f = ROOT / "notes" / "tape" / f"{T.get('report_date')}.md"
        if not f.exists() or time.time() - f.stat().st_mtime > 26 * 3600:
            return ""
        lines = [l for l in f.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
        if not lines:
            return ""
        if len(lines) > max_lines:
            lines = [f"... {len(lines) - max_lines} earlier lines omitted"] + lines[-max_lines:]
        return "\n".join(lines)
    except Exception:
        return ""


def _level_ledger(bars, T: dict, price: float) -> str:
    """What the tape did at each structural level so far today -- touches, first touch, crossings (closes that
    changed side), and where price sits now. Levels: ladder strikes within 1.2 EM of price, the flips, the rails.
    This is what lets the model tell a wall that held three times from one that has never been tested."""
    import datetime as _dt
    if not bars: return ""
    P = T.get("panels") or {}
    em = float(T.get("em") or 300)
    lv = {}
    for b in ("0DTE", "NEAR", "FULL"):
        for k, v in ((P.get(b) or {}).get("gamma") or {}).items():
            k = float(k)
            if abs(k - price) <= 1.0 * em and abs(int(v)) >= 25: lv.setdefault(k, {})[b] = int(v)
    for b in ("0DTE", "NEAR", "FULL"):
        h = ((P.get(b) or {}).get("hvl") or [None])[0]
        if h is not None: lv.setdefault(round(float(h), 1), {})["flip"] = b
    for k in ("rail_lo", "rail_hi"):
        if T.get(k): lv.setdefault(round(float(T[k]), 1), {})["rail"] = k[-2:]
    hhmm = lambda ms: _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).strftime("%H:%M")
    if len(lv) > 24:                                   # keep the heaviest strikes; flips and rails always stay
        keep = sorted(lv, key=lambda k: max([abs(v) for b, v in lv[k].items() if b in ("0DTE", "NEAR", "FULL")] + [10**6 if ("flip" in lv[k] or "rail" in lv[k]) else 0]), reverse=True)[:24]
        lv = {k: lv[k] for k in keep}
    lines = []
    for k in sorted(lv, reverse=True):
        touches = [b for b in bars if b["l"] <= k <= b["h"]]
        cross = _closes_through(bars, k)                      # swing crossings this session, the same basis the stop rule demotes on
        side = "above" if bars[-1]["c"] > k else "below"
        tag = " ".join([f"{b} {v:+d}" for b, v in lv[k].items() if b in ("0DTE", "NEAR", "FULL")]
                       + ([f"{lv[k]['flip']} flip"] if "flip" in lv[k] else []) + ([f"rail {lv[k]['rail']}"] if "rail" in lv[k] else []))
        if touches:
            verdict = "held every time" if cross == 0 else f"{cross} swing crossing{'s' if cross != 1 else ''} this session" + (" -- a pivot, not a wall" if cross >= DEMOTE_CROSSES else "")
            lines.append(f"{k:,.0f} ({tag}): touched {len(touches)}x, first {hhmm(touches[0]['t'])}, last {hhmm(touches[-1]['t'])}, "
                         f"{verdict}; price now {side} it")
        else:
            lines.append(f"{k:,.0f} ({tag}): untested today; price {side} it")
    return "Level ledger so far today (UTC):\n" + "\n".join(lines)


def _material(T: dict, price: float, bars=None) -> dict:
    """{strike: {book: gamma}} -- the strikes that count as structure.

    A UNION, NOT ONE THRESHOLD (2026-09-16). It used to be a single test: >= MATERIAL_PCT of
    the book's largest |gamma| within an EM. On 09-16 that threshold was 25% of 29,600's 265
    = 66, and 29,600 is seven eighths September paper that expires the following morning. So
    a strike that will not exist in two days was deciding that 29,450 -- the put wall on the
    engine's OWN chart, tested three times that afternoon and held every time, with price
    above VWAP right on top of it -- was not structure. The sheet then carded longs at 29,575
    and 29,600, the ceiling of its own corridor, and refused the floor.

    A level is structure if ANY of these is true:
      * it clears MATERIAL_PCT of its book's largest within an EM          (the old test)
      * it clears MATERIAL_PCT of the 0DTE book's own largest              (the expiring book
        is small in absolute terms; its rungs are still the paper being hedged into the bell)
      * a rail or a flip sits on it
      * the tape turned there this session -- a confirmed swing within SWING_TAG points
    The gamma dict returned for a level that qualifies only on tape or geometry carries its
    real gamma, which may be small or zero. That is the honest number; being small is not the
    same as being absent."""
    P = T.get("panels") or {}; em = float(T.get("em") or 300); out = {}
    # 0DTE FIRST (Mark, 2026-09-16: "analysis should only be focused on 0dte"). Each book is
    # scored against ITS OWN largest, so the small absolute numbers on the expiring book are
    # not measured against months of paper. NEAR and FULL can still admit a level -- they are
    # context for what survives tomorrow -- but they never set the bar for the front book.
    for b in ("0DTE", "NEAR", "FULL"):
        g = {float(k): int(v) for k, v in ((P.get(b) or {}).get("gamma") or {}).items() if abs(float(k) - price) <= em}
        if not g: continue
        mx = max(abs(v) for v in g.values()) or 1
        for k, v in g.items():
            if abs(v) >= MATERIAL_PCT * mx: out.setdefault(k, {})[b] = v
    allg = {b: {float(k): int(v) for k, v in ((P.get(b) or {}).get("gamma") or {}).items()}
            for b in ("0DTE", "NEAR", "FULL")}

    def admit(k):
        k = float(k)
        if abs(k - price) > em or k in out:
            return
        out[k] = {b: allg[b][k] for b in allg if k in allg[b]}

    # the book that actually expires today, scored against its OWN largest
    z = {k: v for k, v in allg["0DTE"].items() if abs(k - price) <= em}
    if z:
        zmx = max(abs(v) for v in z.values()) or 1
        for k, v in z.items():
            if abs(v) >= MATERIAL_PCT * zmx: admit(k)
    # THE WALLS THE CHART DRAWS (Mark, 2026-09-16, of his own screen: "this is mapped just
    # like the engine"). teddychart marks the largest positive above the live price as the
    # call wall and the largest negative below it as the put wall -- same trinity.json, same
    # numbers. The screen refusing a level the engine has already labelled a wall is the sheet
    # disagreeing with its own chart, which is how 29,450 got drawn as the put wall and carded
    # nowhere while longs went on at 29,575 and 29,600, the ceiling of the same corridor.
    for b in ("0DTE", "NEAR", "FULL"):
        g = {k: v for k, v in allg[b].items() if abs(k - price) <= em}
        above = [(k, v) for k, v in g.items() if k > price and v > 0]
        below = [(k, v) for k, v in g.items() if k < price and v < 0]
        if above: admit(max(above, key=lambda t: t[1])[0])
        if below: admit(min(below, key=lambda t: t[1])[0])
    # rails and flips
    for key in ("rail_lo", "rail_hi"):
        if T.get(key) is not None: admit(T[key])
    for b in ("0DTE", "NEAR", "FULL"):
        for h in ((P.get(b) or {}).get("hvl") or []): admit(h)
    # and anywhere the tape actually turned this session
    if bars:
        try:
            import swings as _sw
            sb = _session_bars(bars)
            rows = [{"ts": datetime.datetime.fromtimestamp(x["t"] / 1000.0, datetime.timezone.utc).isoformat(),
                     "h": x.get("h", x["c"]), "l": x.get("l", x["c"]), "c": x["c"]} for x in sb]
            # A swing admits every strike within ONE NOISE UNIT of it, not the rounded one.
            # On 2026-09-16 the three tests of the 29,450 put wall bottomed at 29,430.25,
            # 29,430.75 and 29,437.00. Rounding 29,437 to the 25-grid gives 29,425 and loses
            # the wall the tape was actually turning under by 13 points.
            band = em * float(T.get("rail_scale") or 1.0)
            tol = max(STOP_BUFFER, STOP_BUFFER_EM * band) if STOP_BUFFER_EM else STOP_BUFFER
            known = sorted({k for b in allg for k in allg[b]})
            W = getattr(_sw, "WINDOW_INTRA", 15)
            for _, p, _i in _sw.lows(rows, W, W) + _sw.highs(rows, W, W):
                for k in known:
                    if abs(k - p) <= tol: admit(k)
        except Exception:
            pass
    return out


def _session_bars(bars):
    """Only the bars of the session in progress: since the 13:30Z open once it has happened,
    otherwise since the 22:00Z reopen. Last night's price regime does not get to disqualify
    today's levels (2026-09-16)."""
    if not bars:
        return bars
    last = datetime.datetime.fromtimestamp(bars[-1]["t"] / 1000.0, datetime.timezone.utc)
    open_ = last.replace(hour=13, minute=30, second=0, microsecond=0)
    if last < open_:
        open_ = (last - datetime.timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
    cut = int(open_.timestamp() * 1000)
    return [b for b in bars if b["t"] >= cut] or bars


def _closes_through(bars, level: float, minutes: int = None) -> int:
    """How many times the tape has SWUNG across `level` this session.

    Replaces the 30-minute close count (2026-09-16). Each swing high and swing low the tape
    has confirmed is a place price actually turned; walking them in time order and counting
    the sign changes against `level` says how often price has pivoted on both sides of it. A
    stop run that spikes through and comes straight back leaves no swing and no crossing,
    which is the whole point -- a 30-minute close is the signature of exactly that spike.

    The name is kept so nothing downstream has to change; `minutes` is ignored."""
    b = _session_bars(bars)
    if not b or len(b) < 90:
        return 0
    try:
        import swings as _sw
    except Exception:
        return 0
    rows = [{"ts": datetime.datetime.fromtimestamp(x["t"] / 1000.0, datetime.timezone.utc).isoformat(),
             "h": x.get("h", x["c"]), "l": x.get("l", x["c"]), "c": x["c"]} for x in b]
    W = getattr(_sw, "WINDOW_INTRA", 15)
    piv = sorted([(i, p) for _, p, i in _sw.lows(rows, W, W)] + [(i, p) for _, p, i in _sw.highs(rows, W, W)])
    side, n = None, 0
    for _, p in piv:
        s_ = p > level
        if side is not None and s_ != side: n += 1
        side = s_
    return n


def _struct_stop(card: dict, T: dict, bars) -> dict:
    """The stop the gamma map gives this card: BUFFER (a share of the sheet's expected move for this window) beyond the
    nearest material strike in the adverse direction that the tape has not swung across DEMOTE_CROSSES times this session. Returns {stop, anchor, anchor_gamma, dist,
    demoted:[...], ok} -- ok False only when there is no anchor at all on the adverse side."""
    e = float(card["entry"]); side = card["side"]
    price = float(T.get("last_print") or T.get("spot") or e)
    mat = _material(T, price, bars)
    em = float(T.get("em") or 0) * float(T.get("rail_scale") or 1.0)      # the expected move for this plan's window
    buf = max(STOP_BUFFER, STOP_BUFFER_EM * em) if STOP_BUFFER_EM and em else STOP_BUFFER
    buf = round(buf / 0.25) * 0.25
    cands = sorted((k for k in mat if (k < e if side == "long" else k > e)), key=lambda k: abs(k - e))
    own = round(e / 25.0) * 25.0                     # the strike the card leans on (entry is one point inside it)
    demoted = []

    def anchor(pool, allow_demoted=False):
        for k in pool:
            n = _closes_through(bars, k) if bars else 0
            if n >= DEMOTE_CROSSES:
                if (k, n) not in demoted: demoted.append((k, n))
                if not allow_demoted:
                    continue
            # a wall pair (29,210 / 29,220) is one wall: take the far edge of the strikes within CLUSTER_PTS of the
            # anchor. ONE hop, not a walk -- on a book with strikes every 25 points a repeated walk chained the whole
            # ladder and pushed the stop 100+ points away (found on the 08-16 Asia book).
            near_k = [m for m in mat if (m < k if side == "long" else m > k) and abs(m - k) <= CLUSTER_PTS]
            edge = (min(near_k) if side == "long" else max(near_k)) if near_k else k
            stop = edge - buf if side == "long" else edge + buf
            dist = abs(e - stop)
            return {"stop": stop, "anchor": k, "edge": edge, "anchor_gamma": mat[k], "dist": dist, "demoted": demoted,
                    "buffer": buf, "ok": True, "wide": dist > STRUCT_STOP_BANDS * em,
                    "behind": abs(k - own) > 0.01, "pivot": n >= DEMOTE_CROSSES}
        return None

    # THE WALL BEHIND THE WALL (Mark, 2026-09-15: "maybe we should move back to structural stops ...
    # because we are pretty much back at 250"). The 29,250 hold's stop sat 19.75 under its own strike,
    # a 29,226 wick took it at 01:57Z, and price was back over 29,250 six minutes later. A stop a
    # buffer past the card's own wall is a distance, not structure: the structure that has to fail
    # for the trade to be wrong is the next wall behind it. So the first choice is the nearest
    # material strike BEYOND the card's own strike on the adverse side, with the same buffer and
    # cluster hop -- 29,200 behind 29,250, stop 29,185.25 -- as long as that sits inside one drawn
    # band; when the wall behind is further than that (a 29,400 fade with nothing material until
    # 29,500) the stop goes behind the card's own wall as before. Nothing decides whether the card
    # EXISTS any more: the width and the R are printed on it and the trader decides (2026-09-16).
    r = anchor([k for k in cands if abs(k - own) > 0.01])
    if r is not None and not r["wide"]:
        return r
    r0 = anchor(cands)
    if r0 is not None:
        return r0
    # DEMOTION DEMOTES; IT DOES NOT DELETE (2026-09-16). On a trending session the tape closes
    # through everything behind it, so by the afternoon every candidate on the adverse side is a
    # pivot and this returned no stop at all -- which deleted every long below price for the whole
    # day. On 2026-09-16 that was the entire downside: 29,418, 29,416, 29,400, 29,350 and the lower
    # rail all came back "no stop can be anchored" while sitting in plain sight on the board. A stop
    # behind a pivot is weaker than one behind a wall and the card says so; it is not nothing.
    r1 = anchor(cands, allow_demoted=True)
    if r1 is not None:
        return r1
    return {"stop": None, "anchor": None, "edge": None, "anchor_gamma": None, "dist": None,
            "demoted": demoted, "ok": False, "behind": False, "pivot": False}


def _structure_check(cards, T) -> tuple[str, list]:
    """Compare every card's stop with the structural one. Returns (problem text or '', notes). A card whose stop
    is off by more than 5 points, or whose structure is too far away, produces a corrective instruction."""
    now_ms = TICKS.status().get("clock_ms") if (REPLAY and TICKS.mode == "replay") else None
    bars, _ = _day_bars(now_ms)
    probs, notes = [], []
    for c in cards:
        r = _struct_stop(c, T, bars)
        name = c.get("name", "?")
        if not r["ok"]:
            if r["anchor"] is None:
                probs.append(f"{name}: no material strike at all on the stop side of {c['entry']:g} -- there is nothing for a stop to sit behind; move the entry to a strike or replace the card")
            else:
                probs.append(f"{name}: the nearest material strike on the stop side is {r['anchor']:,.0f} ({', '.join(f'{b} {v:+d}' for b, v in r['anchor_gamma'].items())}), {r['dist']:.0f} points away -- too far; move the entry to the structure or replace the card")
            continue
        if abs(float(c["sl"]) - r["stop"]) > 5:
            dem = (" (" + ", ".join(f"{k:,.0f} closed through {n}x today" for k, n in r["demoted"]) + " do not count)") if r["demoted"] else ""
            wall = f"{r['anchor']:,.0f}" + (f"-{r['edge']:,.0f} cluster" if r['edge'] != r['anchor'] else "")
            probs.append(f"{name}: stop must be {r['stop']:g}, {r['buffer']:g} beyond {wall} ({', '.join(f'{b} {v:+d}' for b, v in r['anchor_gamma'].items())}){dem}; yours was {float(c['sl']):g}")
        notes.append(f"{name}: stop {r['stop']:g} beyond {r['anchor']:,.0f}" + (f"-{r['edge']:,.0f}" if r['edge'] != r['anchor'] else "")
                     + (" (the wall behind the card's own)" if r.get("behind") else " (the card's own wall; the next is out of range)"))
    return ("; ".join(probs), notes)


def _entry_quality_safe(cards, T) -> dict:
    """_entry_quality behind a guard: this runs inside normalize(), which serves the whole report.
    An advisory annotation must never be able to take that endpoint down."""
    try:
        try: bars, _ = _day_bars()
        except Exception: bars = []
        return _entry_quality(cards, T, bars)
    except Exception as e:
        _log(f"[entry_quality] skipped: {e}")
        return {}


def _entry_quality(cards, T, bars=None) -> dict:
    """ADVISORY ONLY -- never rejects. For each card: how far the entry sits from the material strike that
    anchors its stop, how far that wall sat from the reference, and whether the structural stop lands in the
    45-70 point band -- on a full-day plan; both thresholds scale by rail_scale on a partial session (2026-09-14:
    every Asia card was flagged against the RTH band). Both come from the 2026-09-07 backtest and are UNCORRECTED subgroup results
    (see NEAR_WALL_PTS). Keyed "name|entry" to match _pre_plan, so the page can line them up with the cards."""
    out = {}
    price = float(T.get("last_print") or T.get("spot") or 0)
    rs = float(T.get("rail_scale") or 1.0)          # the session's share of a full day's range
    lo, hi = STOP_BAND[0] * rs, STOP_BAND[1] * rs
    for c in cards or []:
        try:
            e = float(c.get("entry")); side = c.get("side")
        except (TypeError, ValueError):
            continue
        if not side:
            continue
        r = _struct_stop({"entry": e, "side": side}, T, bars or [])
        wall = r.get("anchor")
        D = r.get("dist")
        near = wall is not None and price and abs(wall - price) <= NEAR_WALL_PTS * rs
        band = D is not None and lo <= D <= hi
        if wall is None:
            txt = "no material strike anchors this entry"
        else:
            bits = [f"wall {wall:,.0f} ({abs(e - wall):.0f} pts from entry, {abs(wall - price):.0f} from the reference)"]
            bits.append(f"stop {D:.0f} pts" + (f" -- in the {lo:.0f}-{hi:.0f} band" if band else f" -- outside the {lo:.0f}-{hi:.0f} band")
                        + ("" if rs == 1.0 else f" (45-70 x {rs:.2f})"))
            txt = "; ".join(bits)
        out[f"{c.get('name','?')}|{e:g}"] = {
            "wall": wall, "wall_from_entry": (abs(e - wall) if wall is not None else None),
            "wall_from_price": (abs(wall - price) if wall is not None and price else None),
            "stop_dist": D, "near": bool(near), "in_band": bool(band),
            "flag": ("near-wall" if near and band else "near-wall, stop outside band" if near
                     else "wall far from price" if wall is not None else "unanchored"),
            "text": txt}
    return out


def _check_cards(C, gate=None) -> str:
    """The card contract the page and build.py rely on: four cards, three targets each, targets ordered away
    from the entry with the stop on the other side. Returns '' when the reply passes, else what failed."""
    cards = C.get("setups") or []
    if not isinstance(cards, list): return 'setups must be a list'
    verdict = (gate or {}).get('verdict')
    maximum = 0 if verdict == 'stand aside' else min(2, (gate or {}).get('scenarios_carried', 2)) if verdict == 'thin' else 4
    if not isinstance(cards, list) or len(cards) > maximum: return f'{len(cards)} setups, maximum {maximum} for this trade gate'
    # Rank. Primary is an ORDER -- the card to take first -- not a directional claim, and it was
    # unreachable from 2026-09-14 to 2026-09-16 because the rule listed all three gate verdicts
    # (including the passing one) and then required a directional confidence nothing supplies.
    # The gate is enforced here; the omission is repaired rather than costing a round.
    ranked = [c for c in cards if isinstance(c, dict)]
    primary = [c for c in ranked if (c.get("rank") or "").strip().lower() == "primary"]
    if primary and verdict in ("thin", "stand aside"):
        return (f'{len(primary)} card(s) ranked Primary, but the trade gate reads "{verdict}" -- no card is '
                f'Primary unless the verdict is "look". Rank them Secondary or Opportunistic.')
    if len(primary) > 1:
        return (f'{len(primary)} cards ranked Primary ({", ".join(c.get("name", "?") for c in primary)}). '
                f'Primary is an order, not a grade: exactly one card carries it.')
    if not primary and ranked and verdict == "look":
        ranked[0]["rank"] = "Primary"     # build.py's own default, applied before the page is drawn
    for c in cards:
        if not isinstance(c, dict): return 'each setup must be an object'
        name = c.get("name", "?")
        tp = c.get("tp");  tp = tp if isinstance(tp, list) else [tp] if tp is not None else []
        tp = [float(x) for x in tp if isinstance(x, (int, float))]
        c["tp"] = tp
        if len(tp) != 3: return f"card {name} has {len(tp)} targets, need exactly 3"
        try: e, sl = float(c["entry"]), float(c["sl"])
        except (KeyError, TypeError, ValueError): return f"card {name} is missing entry or sl"
        side = (c.get("side") or ("long" if tp[0] > e else "short")).lower(); c["side"] = side
        ok = (sl < e < tp[0] < tp[1] < tp[2]) if side == "long" else (sl > e > tp[0] > tp[1] > tp[2])
        if not ok: return f"card {name} ({side}) targets/stop are not ordered away from the entry"
        if abs(tp[0] - e) < 1.2 * abs(e - sl):
            # SAY THE ARITHMETIC, not the rule (2026-09-10). "TP1 is under 1.2R" restates the
            # instruction the model has just failed, and it failed it again on all three
            # corrective rounds, four plans running -- always the far-wall card, whose wide
            # structural stop is exactly what makes the ratio hard to see. Give it the number
            # to clear and the two ways to clear it.
            risk = abs(e - sl)
            need = e + 1.2 * risk if side == "long" else e - 1.2 * risk
            return (f"card {name} TP1 is under 1.2R: entry {e:,.2f}, stop {sl:,.2f}, so risk is "
                    f"{risk:,.2f} points and TP1 {tp[0]:,.2f} is only {abs(tp[0] - e) / risk:.2f}R. "
                    f"TP1 must be at or beyond {need:,.2f}, with TP2 and TP3 further out again. "
                    f"Either move the targets out to a level that far away, or move the ENTRY closer "
                    f"to its structure so the stop is tighter.")
    return ""

def _reference_note(T: dict, last_close: float) -> str:
    """One line reconciling the reference the sheet is DRAWN from with where the tape actually is.

    The 2026-09-04 06:13 premarket sheet is why this exists: the bar cache had stopped refreshing, autopull found
    no usable print, and the sheet anchored every distance on the 29,186.25 settle while the tape was at 29,544 --
    400 points away. Nothing in the commentary said so, because the tape summary was skipped entirely.
    """
    ref = T.get("last_print"); spot = T.get("spot")
    if not ref:
        gap = (last_close - float(spot)) if spot else 0.0
        return (f"NO LAST PRINT IN THIS BUILD. Every distance on the sheet is measured from the settle "
                f"{float(spot):,.2f}, but the tape's last close is {last_close:,.2f} -- {gap:+,.0f} away. Say this in the "
                f"lead, describe the levels from the book rather than from the reference, and do not write "
                f"'price sits N points above the flip' off a reference the tape has left behind.")
    if abs(last_close - float(ref)) > 25:
        return (f"THE REFERENCE IS BEHIND THE TAPE: the sheet is drawn from {float(ref):,.2f}, the tape's last close is "
                f"{last_close:,.2f} ({last_close - float(ref):+,.0f}). Distances on the page are measured from the reference; "
                f"say which one you are using.")
    return ""


def _swings_block(T: dict, n: int = 8) -> str:
    """The swing highs and lows of the session in progress, with times.

    WHY (2026-09-17). CARD_RULES.md says the stop is one point beyond the most recent swing
    beyond the entry -- and the payload never carried the swings. trinity, state, the tape
    summary and the ledger went out, and nothing said where the tape actually turned, so the one
    rule that decides every stop on the page was impossible to follow and every run reached for
    the fallback construction instead. On the 18:41 board a 29,700 short got a stop at 29,765.8,
    beyond the 29,750 strike, while the tape had swing highs at 29,708.00 and 29,728.50 sitting
    right above the entry: 36 points apart on the same trade, 1.32R against 1.75R.

    Cheap -- swings.py over the session's 1-minute bars -- and it is the raw material for the
    rule. Empty when there are no bars, which is the build that has no swings to quote anyway.
    """
    try:
        rows = BARS_FN(1) if BARS_FN else []
        if not rows:
            return ""
        import swings as _sw
        sb = _session_bars(rows)
        if not sb:
            return ""
        conv = [{"ts": datetime.datetime.fromtimestamp(x["t"] / 1000.0, datetime.timezone.utc).isoformat(),
                 "h": x.get("h", x["c"]), "l": x.get("l", x["c"]), "c": x["c"]} for x in sb]
        def fmt(v):
            # swings.highs/lows return TUPLES -- (iso timestamp, price, bar index). The first
            # version of this formatter handled dicts and scalars, float()ed the tuple, threw,
            # and the except below turned the whole block into an empty string (2026-09-17).
            if isinstance(v, (tuple, list)) and len(v) >= 2:
                return f"{float(v[1]):,.2f} at {str(v[0])[11:16]}Z"
            if isinstance(v, dict):
                px = v.get("px", v.get("p", v.get("price")))
                ts = str(v.get("ts", ""))[11:16]
                return f"{float(px):,.2f}" + (f" at {ts}Z" if ts else "")
            return f"{float(v):,.2f}"
        hi = ", ".join(fmt(v) for v in (_sw.highs(conv) or [])[-n:]) or "none"
        lo = ", ".join(fmt(v) for v in (_sw.lows(conv) or [])[-n:]) or "none"
        head = ("\n\nSWINGS THIS SESSION -- the tape's own turns, newest last. EVERY STOP ON THE PAGE IS "
                "BUILT FROM THIS LIST: one point beyond the most recent swing beyond the entry. Only if this "
                "list has nothing beyond a card's entry does the fallback construction apply, and a card using "
                "the fallback says so.\n")
        return head + f"  swing highs: {hi}\n  swing lows : {lo}"
    except Exception as ex:
        _log(f"[comment] swings block unavailable: {ex!r}")
        return ""



def _tape_summary(T: dict) -> str:
    """
    What the tape has done in the session the plan is being written for, so the commentary reads like the sheet
    it is on. Built from BARS_FN (teddy.py's 1-minute cache), or from the replayed prints in a replay.

    It keys off WHETHER THE SESSION HAS BARS, not off last_print. The old guard returned nothing whenever
    last_print was null -- which is precisely the build that needs the tape most, because last_print is null
    exactly when the price feed has died (2026-09-04 06:13). A premarket build with a live cache now gets the
    overnight, the path and the ledger like any other.
    """
    if REPLAY and TICKS.mode == "replay":
        return _tape_summary_replay(T)
    try:
        rows = BARS_FN(1) if BARS_FN else []
    except Exception:
        rows = []
    if not rows:
        return ""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    day = now.date().isoformat()
    def hhmm(ms): return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).strftime("%H:%M")
    today = [b for b in rows if _dt.datetime.fromtimestamp(b["t"] / 1000, _dt.timezone.utc).date().isoformat() == day]
    rth = [b for b in today if "13:30" <= hhmm(b["t"]) < "20:00"]
    on = [b for b in today if hhmm(b["t"]) < "13:30"]
    if not today:
        return ""
    def block(name, bs):
        if not bs: return ""
        hi = max(bs, key=lambda b: b["h"]); lo = min(bs, key=lambda b: b["l"])
        return (f"{name}: open {bs[0]['o']:,.2f} at {hhmm(bs[0]['t'])} UTC, high {hi['h']:,.2f} at {hhmm(hi['t'])}, "
                f"low {lo['l']:,.2f} at {hhmm(lo['t'])}, last {bs[-1]['c']:,.2f} at {hhmm(bs[-1]['t'])}, range {hi['h'] - lo['l']:,.0f} pts")
    session = _day_bars(min_hours=3)[0] or today          # the trading day from the 22:00 UTC Globex open
    last_close = float(today[-1]["c"])
    lines = [_reference_note(T, last_close),
             block("Overnight (00:00-13:30 UTC)", on), block("RTH so far (from 13:30 UTC)", rth),
             _path30(session), _level_ledger(session, T, last_close)]
    hrs = T.get("front_expiry_hours") or T.get("hours")     # hours to the front LISTED expiry (2026-09-05)
    if hrs: lines.append(f"Hours left to the front expiry ({T.get('front_root') or 'front book'}): {float(hrs):.2f}. "
                         + ("The rails on this sheet are scaled to the rest of the session; do not describe them as a full-day range."
                            if (T.get("rail_scale") or 1.0) < 1.0 else "The rails are the one-session expected move around the settle."))
    if T.get("no_0dte"): lines.append("There is NO 0DTE book on this report date (nothing expires); the 0DTE column is empty, not flat.")
    settle = T.get("spot")
    if settle: lines.append(f"Reference the rails are drawn on: {float(settle):,.2f}; the tape's last close is {last_close - float(settle):+,.0f} from it."
                            + (f" The book itself is priced on the {T.get('ref_symbol') or 'NQ'} settle {T['ref_settle']:,.2f}." if T.get("ref_settle") else ""))
    return "\n".join(l for l in lines if l)

def _path30(bars) -> str:
    """The day's path as the turns the tape actually made, so the model sees what was traded
    through and what merely touched -- high/low/last alone cannot tell a bounce from a break.

    This was a 30-minute OHLC table until 2026-09-16 ("I want 30 minute close gone
    completely"). The clock was never the point: what the model needs is where price turned
    and in what order, which is what a swing sequence is. The running extreme since the last
    confirmed turn is appended and marked unconfirmed, because a swing needs its window to
    close and the newest leg is always the one being asked about.
    """
    if not bars:
        return ""
    import datetime as _dt
    try:
        import swings as _sw
    except Exception:
        return ""
    sb = _session_bars(bars)
    rows = [{"ts": _dt.datetime.fromtimestamp(x["t"] / 1000.0, _dt.timezone.utc).isoformat(),
             "h": x.get("h", x["c"]), "l": x.get("l", x["c"]), "c": x["c"]} for x in sb]
    W = _sw.WINDOW_INTRA
    if len(rows) < 2 * W + 2:
        return ""
    piv = sorted([(i, p, "low") for _ts, p, i in _sw.lows(rows, W, W)]
                 + [(i, p, "high") for _ts, p, i in _sw.highs(rows, W, W)])
    out = ["{}  {:<4} {:,.2f}".format(rows[i]["ts"][11:16], kind, p) for i, p, kind in piv]
    if piv:
        tail = rows[piv[-1][0] + 1:]
        if tail:
            lo = min(tail, key=lambda r: r["l"])
            hi = max(tail, key=lambda r: r["h"])
            use_lo = abs(lo["l"] - piv[-1][1]) > abs(hi["h"] - piv[-1][1])
            ext = lo if use_lo else hi
            out.append("{}  {:<4} {:,.2f}  (forming, unconfirmed)".format(
                ext["ts"][11:16], "low" if use_lo else "high", ext["l"] if use_lo else ext["h"]))
    if not out:
        return ""
    head = ("Turns so far this session (UTC), oldest first -- a swing is a place price actually "
            "turned, and the last line is the leg still forming:")
    return head + chr(10) + chr(10).join(out)

def _tape_summary_replay(T: dict) -> str:
    """Same summary, from the replayed prints up to the virtual clock -- what the tape looked like at that moment."""
    import datetime as _dt
    st = TICKS.status(); clock = st.get("clock_ms")
    ts, pxs, *_ = TICKS.since(0)
    if not ts or not clock: return ""
    pts = [(t, p) for t, p in zip(ts, pxs) if t <= clock]
    if not pts: return ""
    day = _dt.datetime.fromtimestamp(clock / 1000, _dt.timezone.utc).date().isoformat()
    def hhmm(ms): return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).strftime("%H:%M")
    today = [(t, p) for t, p in pts if _dt.datetime.fromtimestamp(t / 1000, _dt.timezone.utc).date().isoformat() == day]
    on = [x for x in today if hhmm(x[0]) < "13:30"]; rth = [x for x in today if "13:30" <= hhmm(x[0]) < "20:00"]
    def block(name, xs):
        if not xs: return ""
        hi = max(xs, key=lambda x: x[1]); lo = min(xs, key=lambda x: x[1])
        return (f"{name}: open {xs[0][1]:,.2f} at {hhmm(xs[0][0])} UTC, high {hi[1]:,.2f} at {hhmm(hi[0])}, low {lo[1]:,.2f} at {hhmm(lo[0])}, "
                f"last {xs[-1][1]:,.2f} at {hhmm(xs[-1][0])}, range {hi[1] - lo[1]:,.0f} pts")
    bars30, _ = _day_bars(clock)
    lines = [f"The clock reads {hhmm(clock)} UTC.", _reference_note(T, float(pts[-1][1])),
             block("Overnight (00:00-13:30 UTC)", on), block("RTH so far (from 13:30 UTC)", rth), _path30(bars30),
             _level_ledger(bars30, T, float(pts[-1][1]))]
    hrs = T.get("front_expiry_hours") or T.get("hours")
    if hrs: lines.append(f"Hours left to the front expiry ({T.get('front_root') or 'front book'}): {float(hrs):.2f}. "
                         + ("The rails on this sheet are scaled to the rest of the session."
                            if (T.get("rail_scale") or 1.0) < 1.0 else "The rails are the one-session expected move around the settle."))
    if T.get("spot"): lines.append(f"Reference the rails are drawn on: {T['spot']:,.2f}; price is {pts[-1][1] - T['spot']:+,.0f} from it."
                                   + (f" The book itself is priced on the {T.get('ref_symbol') or 'NQ'} settle {T['ref_settle']:,.2f}." if T.get("ref_settle") else ""))
    return "\n".join(l for l in lines if l)

def _save_reply(s, tag=""):
    """cc-last-reply.txt is the newest; notes/replies/ keeps every round of every request, so a rejected
    round can be scored later (the 09-04 post-mortem could only recover the rejected round's entries from cc.log)."""
    try:
        (ROOT / "notes" / "cc-last-reply.txt").write_text(s, encoding="utf-8")
        d = ROOT / "notes" / "replies"; d.mkdir(exist_ok=True)
        (d / f"{time.strftime('%Y-%m-%d-%H%M%S')}{tag}.txt").write_text(s, encoding="utf-8")
    except Exception: pass

def _extract_json(text: str) -> dict:
    """The first balanced {...} in the reply, wherever it sits. Models sometimes lead with a sentence
    or wrap in fences despite instructions; an empty reply is reported as such, not as 'char 0'."""
    t = re.sub(r"```(?:json)?", "", text or "").strip()
    if not t: raise RuntimeError("empty reply from the model -- see notes/cc-last-reply.txt")
    i = t.find("{")
    if i < 0: raise RuntimeError("no JSON object in the reply -- see notes/cc-last-reply.txt")
    depth, in_str, esc = 0, False, False
    for j, ch in enumerate(t[i:], i):
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc: in_str = False
            if ch != "\\": esc = False
            continue
        if ch == '"': in_str = True
        elif ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try: return json.loads(t[i:j + 1])
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"reply JSON did not parse ({e.msg} at char {e.pos}) -- see notes/cc-last-reply.txt")
    raise RuntimeError("reply JSON is unterminated -- see notes/cc-last-reply.txt")

def _write_prose(C: dict, snapshot=None):
    """Publish structured prose only against the exact inputs used by its validators."""
    if snapshot is None:
        raise RuntimeError('Publication requires the original validated snapshot')
    if REPLAY:
        from publication import publish_replay
        return publish_replay(ROOT, C, snapshot)
    from publication import publish
    publish(ROOT, C, snapshot)



if __name__ == "__main__":
    # python command_center.py --plan-check      fold the current sheet's cards over the tape before the plan
    # and write notes/plan_check.json. teddy.py loads that file once at import, so a running dashboard shows
    # the result after its next restart (or the next /api/report, which re-checks a newer sheet on its own).
    import sys
    if "--plan-check" in sys.argv:
        ok = _refresh_plan_check(force=True, why="cli")
        pc = _state.get("plan_check") or {}
        print(("checked" if pc.get("checked") else "NOT checked") + " -- " + str(pc.get("note")))
        for k, v in (pc.get("cards") or {}).items():
            print(f"  {k.split('|')[0]:<20} {v.get('state', '?'):<8} {v.get('text', '')}")
        sys.exit(0 if ok else 1)
    if "--plan-window" in sys.argv:
        # python command_center.py --plan-window   what the tracking window IS and what each card would say
        # inside it, printed against the same fold the page runs. Reads only; the page is not changed yet.
        import datetime as _dt
        T = json.loads(_read(ROOT / "trinity.json"))
        cards = _setups(_read(ROOT / "trinity.html"))
        w = _plan_window(T, cards)
        hhmm = lambda ms: _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).strftime("%a %H:%M")
        print(f"plan            {T.get('report_date')}  ({T.get('rail_label')}, book settle {T.get('settle')},"
              f" OI {T.get('oi_vintage')})")
        print(f"asof (pricing)  {T.get('asof')}")
        print(f"session open    {_iso(w['session_open']) if w.get('session_open') else '--'}")
        print(f"published       {_iso(w['published_at'])}  (from {w['published_src']})")
        print(f"live_from       {_iso(w['live_from'])}  <- {w['basis']}")
        now_ms = int(time.time() * 1000)
        bars = _bars_between(w["live_from"], now_ms)
        print(f"tape            {len(bars)} bars, {hhmm(bars[0]['t'])} to {hhmm(bars[-1]['t'])} UTC"
              if bars else "tape            no bars in the window yet")
        for c in cards:
            state, at = _fold_card(c, bars)
            hits = " · ".join(f"T{j+1} {hhmm(at[f'tp{j}'])}" for j in range(4) if f"tp{j}" in at)
            det = (f"triggered {hhmm(at['entry'])}" + (f" · {hits}" if hits else "")
                   + (f" · stopped {hhmm(at['sl'])}" if "sl" in at else "")) if "entry" in at else (
                   f"closest {at['approach']:,.2f} ({at['approach_pts']:g} pts off)" if "approach" in at else "")
            pre = "  [before publication]" if at.get("entry") and at["entry"] < w["published_at"] else ""
            print(f"  {c.get('name','?'):<22} {state:<8} {det}{pre}")
        sys.exit(0)
    if "--request" in sys.argv or "--answered" in sys.argv or "--requests" in sys.argv:
        # The request file from a terminal, so option B leaves the same trail option A does:
        #   --request [why]   raise one          --answered [why]  clear one          --requests  show it
        import datetime as _dt
        if "--requests" in sys.argv:
            r = _request_state()
            if not r:
                print("no request pending"); sys.exit(1)
            print(f"pending  {r['at']}  ({r['age_min']:.0f} min"
                  + (", EXPIRED" if r["expired"] else "") + ")")
            print(f"  plan   {r['report_date']} {r['rail_label']}  {r['hours']}h  book {r['settle']} "
                  f"({r['oi_vintage']})  ref {r['reference']}")
            print(f"  why    {r['why']}")
            for c in r.get("cards") or []:
                print(f"    {c['name']:<24} {c['entry']} / {c['sl']} -> {c.get('tps')}")
            sys.exit(0)
        rest = [a for a in sys.argv[2:] if not a.startswith("--")]
        why = " ".join(rest) or ("raised from the CLI" if "--request" in sys.argv else "answered in a chat")
        if "--request" in sys.argv:
            req, fresh = raise_request(why=why)
            print(("raised" if fresh else "already pending") + f" -- {req['report_date']} {req['rail_label']}"
                  if req else "could not write the request")
            sys.exit(0 if req else 1)
        ok = clear_request(why)
        print("cleared" if ok else "no request to clear")
        sys.exit(0 if ok else 1)
    print("usage: python command_center.py [--plan-check | --plan-window | "
          "--request [why] | --answered [why] | --requests]")
