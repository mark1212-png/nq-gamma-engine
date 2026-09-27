#!/usr/bin/env python3
"""
teddy.py — TeddyAI local dashboard.

    pip install --user flask
    python teddy.py            ->  http://127.0.0.1:5137

Runs entirely on this machine. The Databento API key stays in your environment
and never leaves it.

Data comes from Databento GLBX.MDP3. The statistics, definition and ohlcv
schemas are all L0, and the Standard subscription includes live plus 16+ years
of L0 history -- so pulls carry no per-call charge and the old estimate/approve
gate has been removed. The only thing that runs unprompted is the 1-minute bar
cache: it is topped up on load and every 45s while the tab is visible inside a
session, because the plan anchors on the last print. Everything else is a click.

There is no price chart. It was removed deliberately -- the numbers in the sheet
and in Session vs plan say which levels were touched and whether they held,
which is what the map is read for; the canvas only showed roughly where price
went, and cost ~450 lines to pan and zoom.

The QuikStrike PDF route (nqgamma.py -> trinity_pdf.py) was removed on
2026-09-13: Databento has been the only source since the 2026-09-05 audit.
quikstrike_oi.py still reads the OI PDF for the Sunday-open plan.

Adding a module later: append to MODULES and give it a route. The registry
drives the home page, so a new entry appears as a card automatically.
"""

import os, re, json, subprocess, sys, datetime, pathlib
import session_scale
from flask import Flask, request, jsonify, Response, send_from_directory

# The tracker reads setups back out of the RENDERED sheet, so the dashboard and
# the command line agree by construction: one parser, one state machine, no pair
# of them to drift apart. Optional on purpose -- a missing setup_track.py costs
# the tracking panel, not the dashboard.
try:
    import setup_track
    _TRACK_ERR = None
except Exception as _ex:                                   # noqa: BLE001
    setup_track, _TRACK_ERR = None, f"{type(_ex).__name__}: {_ex}"

ROOT = pathlib.Path(__file__).parent.resolve()
DATA, SHEETS = ROOT / "data", ROOT / "sheets"
CHARTS, REVIEWS, NOTES = DATA / "charts", DATA / "reviews", ROOT / "notes"
JOURNAL = NOTES / "journal.md"
PORT = 5137

# ONE source of truth for which contract the bars are. live_feed.py streams it
# and this file backfills it; when they disagreed the cache silently held two
# different contracts' bars in one day file. Imported rather than duplicated so
# the quarterly roll is a one-line change in live_feed.py and nowhere else.
try:
    from live_feed import SYMBOL as BAR_SYMBOL, STYPE as BAR_STYPE
except Exception:                       # live_feed imports databento; the
    BAR_SYMBOL, BAR_STYPE = "NQZ6", "raw_symbol"   # dashboard must start anyway

# Sessions, and how far price can travel by the end of each one.
#
# The expected move S x IV / sqrt(252) is ONE FULL TRADING DAY, and the rails are
# drawn around the SETTLE. So the scale for a session is not that session's own
# share of variance -- it is the CUMULATIVE share from the settle through the end
# of that session, because the band has to span everything that has happened
# since the anchor. Measured from 67 sessions of NQ 1-minute bars, Jun-Sep 2026:
#
#     segment            share      cumulative from settle    rail scale
#     post-close 20-24     6.4%
#     Asia       00-07    18.9%              25.3%              0.503x
#     Europe     07-13:30 20.1%              45.4%              0.674x
#     US RTH     13:30-20 54.6%             100.0%              1.000x
#
# RTH therefore comes out at exactly 1.00x, which preserves the full-day rail the
# backtest actually validated. Asia at 0.50x is the correction that matters: the
# full-day band is twice as wide as an overnight session warrants, so it contains
# nearly everything and tells you nothing.
#
# `start`/`end` are UTC hours. The front option expires 20:00 UTC (13:00 PT).
SESSIONS = {
    "asia":   dict(label="Asia",    start=0.0,  end=7.0,  var=0.253),
    "europe": dict(label="Europe",  start=7.0,  end=13.5, var=0.454),
    "rth":    dict(label="US RTH",  start=13.5, end=20.0, var=1.000),
    "full":   dict(label="Full day", start=0.0, end=20.0, var=1.000),
}
EXPIRY_UTC = 20.0


def session_view(sid, report_date, now=None):
    """
    Hours to expiry and rail scale for one session.

    Hours are measured from the moment the plan DESCRIBES, not from when it is
    generated: the session's own start, or now if we are already inside it. That
    is why RTH comes out at 6.5 -- 13:30 UTC to 20:00 UTC -- which is the number
    the premarket workflow has always used.
    """
    sd = SESSIONS.get(sid) or SESSIONS["rth"]
    now = now or datetime.datetime.now(datetime.timezone.utc)
    today = now.date().isoformat()
    ref, live = sd["start"], False
    if report_date == today:
        now_h = now.hour + now.minute / 60.0
        if sd["start"] <= now_h < sd["end"]:
            ref, live = now_h, True          # mid-session: time actually left
        elif now_h >= sd["end"]:
            ref = sd["end"]                  # session already done
    hours = max(EXPIRY_UTC - ref, 0.25)

    # Shortening TTE mid-session but leaving the rails at the whole-session
    # scale is the wrong half of the fix, and it errs one way every time: RTH
    # variance is front-loaded, so an hour into the session only ~47% of the
    # day's movement is still ahead even though 85% of the clock is. The
    # measured remaining-displacement curve lives in session_scale.py. It only
    # covers RTH -- there is no intra-Asia profile -- so the overnight sessions
    # keep their cumulative-variance scale.
    scale, label = round(sd["var"] ** 0.5, 3), sd["label"]
    if live:
        s = session_scale.scale_for_hours(hours)
        if s is not None:
            scale, label = round(s, 3), "Rest of session"
    return dict(id=sid, label=label, hours=round(hours, 2), live=live,
                rail_scale=scale, var_share=sd["var"],
                window=f"{int(sd['start']):02d}:{int(sd['start']%1*60):02d}-"
                       f"{int(sd['end']):02d}:{int(sd['end']%1*60):02d} UTC")

def _last_print(day=None):
    """
    The most recent cached bar close, or None.

    Reads the newest bar across the current UTC day and the one before, because
    the cache files are UTC days and the local date stops matching at 17:00 PT.
    Whatever the last /api/bars call left on disk. The dashboard no longer draws
    a chart, so nothing refreshes this as a side effect of looking at it -- the
    console keeps the cache warm in the background instead, and Generate
    refreshes it once more before anchoring a plan to it.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    days = ([day] if day else
            [now.date().isoformat(),
             (now.date() - datetime.timedelta(days=1)).isoformat()])
    best = None
    for d in days:
        bf = DATA / "bars" / f"{d}.json"
        if not bf.exists():
            continue
        try:
            rows = json.loads(bf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if rows and (best is None or rows[-1]["ts"] > best[0]):
            best = (rows[-1]["ts"], rows[-1]["c"])
    return best[1] if best else None


def feed_status(max_age_s=90):
    """
    Is live_feed.py alive, and what does it say?

    Returns None when there is no feeder, or when its heartbeat has gone cold.
    Age is what matters, never the file merely existing -- a status file left
    behind by a process that died an hour ago would otherwise read as "live",
    which is the exact failure this is meant to catch.
    """
    f = DATA / "bars" / "feed_status.json"
    if not f.exists():
        return None
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
        age = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.datetime.fromisoformat(doc["updated"])).total_seconds()
    except Exception:
        return None
    if age > max_age_s or doc.get("state") not in ("streaming", "connecting"):
        return None
    doc["age_s"] = round(age, 1)
    return doc


app = Flask(__name__)

MODULES = [
    dict(id="premarket", name="Premarket Report", status="live",
         blurb="Teddy Analysis across the 0DTE, near and full books, VEX regime "
               "read, and the gameplan with TP/SL.",
         href="/premarket"),
    dict(id="review", name="Chart Review", status="live",
         blurb="Drop in a live chart. It is saved with a snapshot of the plan that "
               "was current, ready for Claude to re-rank the setups against what "
               "price has actually done.", href="/review"),
    dict(id="journal", name="Discussion", status="live",
         blurb="A running thread on results and decisions. You write here, Claude "
               "reads and replies into the same file.", href="/journal"),
    dict(id="vexterm", name="VEX Term Structure", status="planned",
         blurb="Vanna exposure by expiry over time — regime shifts before they "
               "show up in price.", href="#"),
    dict(id="cc", name="Command Center", status="live",
         blurb="Opens on the pipeline: pull, verify, ladder, sheet, commentary. "
               "Then the live chart with the levels on it, the game plan with "
               "played / in progress / invalidated, and search across the sheet.",
         href="/cc"),
]


# ------------------------------------------------------------------- shell ---

def run(cmd, timeout=600):
    """Run a helper script, capture everything, never raise."""
    try:
        # Same cp1252 trap in the other direction: without an explicit encoding
        # a child that prints any non-ASCII character raises UnicodeDecodeError
        # HERE, turning a working script into an opaque dashboard failure.
        p = subprocess.run([sys.executable] + cmd, cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, "timed out"
    except Exception as ex:
        return 1, f"{type(ex).__name__}: {ex}"


# ------------------------------------------------------------------ routes ---

@app.get("/")
def home():
    return Response(page(CONSOLE), mimetype="text/html")


@app.get("/premarket")
@app.get("/review")
@app.get("/journal")
def _legacy():
    return Response('<meta http-equiv="refresh" content="0;url=/">', mimetype="text/html")


@app.get("/api/state")
def state():
    # newest by modification time, not by filename -- an older file whose name
    # happens to sort later was silently winning and serving a stale report.
    # newest by modification time, not by filename -- an older file whose name
    # sorts later was silently winning and serving a stale report. Reports that
    # are not gameplans (the backtest) are pushed to the end so they never take
    # over the panel just by being the most recently written file.
    def _rank(q):
        return (0 if not q.name.startswith("backtest") else 1, -q.stat().st_mtime)
    sheets = ([p.name for p in sorted(SHEETS.glob("*.html"), key=_rank)]
              if SHEETS.exists() else [])
    # Front-expiry cutoff is 13:00 PT = 20:00 UTC. Hours remaining is what the
    # gamma map is actually sensitive to, far more than IV, so it should never
    # be a number typed from memory.
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now.replace(hour=20, minute=0, second=0, microsecond=0)
    hrs = round(max((cutoff - now).total_seconds() / 3600.0, 0.25), 2)

    # Latest settlement whose OPEN INTEREST has actually published. OI for
    # session D lands ~12:00 UTC on D+1, so walk back until that has passed --
    # otherwise the default date looks fine and returns an empty book.
    d = datetime.date.today()
    latest = None
    for _ in range(10):
        d -= datetime.timedelta(days=1)
        if d.weekday() >= 5:
            continue
        pub = datetime.datetime.combine(d + datetime.timedelta(days=1),
                                        datetime.time(12, 0), datetime.timezone.utc)
        while pub.weekday() >= 5:
            pub += datetime.timedelta(days=1)
        if now >= pub:
            latest = d.isoformat(); break

    last_px = _last_print()
    rd = request.args.get("report_date") or datetime.date.today().isoformat()
    sess = {k: session_view(k, rd, now) for k in SESSIONS}
    live = next((k for k, v in SESSIONS.items()
                 if v["start"] <= (now.hour + now.minute/60.0) < v["end"]), "full")
    return jsonify(sheets=sheets[:12],
                   sessions=sess, session_now=live,
                   key=bool(os.environ.get("DATABENTO_API_KEY")),
                   today=datetime.date.today().isoformat(),
                   hours_now=hrs, hours_at_open=6.5,
                   latest_settle=latest, last_price=last_px)


@app.post("/api/run")
def run_report():
    """
    Build a report from Databento. The QuikStrike PDF source was removed on
    2026-09-13; a request that still asks for it gets a clear refusal.
    """
    b = request.json or {}
    date, source = b.get("date", ""), b.get("source", "databento")
    hours = b.get("hours")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return jsonify(ok=False, log="bad date"), 400
    if source != "databento":
        return jsonify(ok=False, log=f"source {source!r} is gone -- Databento only"), 400

    log = []
    if True:
        # A pull takes ~90s and the chain for a past settle never changes, so
        # re-rendering a report should not re-fetch it. Pass refetch:true to
        # force one (e.g. if a partial file was written).
        cached = DATA / f"{date}-chain.json"
        if cached.exists() and cached.stat().st_size > 5000 and not b.get("refetch"):
            log.append(f"  using cached {cached.name} "
                       f"({cached.stat().st_size/1e6:.2f} MB) -- send refetch:true to re-pull")
        else:
            rc, out = run(["databento_pull.py", "--fetch", "--date", date,
                           "--window", "final", "--out", "data"])
            log.append(out)
            if rc != 0:
                return jsonify(ok=False, log="\n".join(log))

    chain = DATA / f"{date}-chain.json"
    if chain.exists():
        rd = b.get("report_date") or (datetime.date.fromisoformat(date)
                                      + datetime.timedelta(days=2)).isoformat()
        cmd = ["trinity_db.py", "--chain", str(chain), "--report-date", rd]
        # THE REFERENCE CONTRACT, not just the price. --spot below is a print off
        # data/bars, which live_feed writes under BAR_SYMBOL; without --ref
        # trinity_db references the FRONT BOOK's underlying, so in roll week the
        # dashboard's own Generate produced one sheet with price on NQZ6 and every
        # flip, rail and level on NQU6 -- 292 points apart on 2026-09-16, with
        # nothing on the page to say so. autopull has passed --ref since 09-14
        # (REF_ARGS) and engine.py inherits it from trinity.json; this button was
        # the last path that did not. Skipped for a continuous symbol, which is
        # not a contract the chain settles.
        if BAR_SYMBOL and not BAR_SYMBOL.startswith("NQ."):
            cmd += ["--ref", BAR_SYMBOL]
        if hours: cmd += ["--hours", str(hours)]

        # The session selector has to reach the SHEET, not just the page. It once
        # reached only the chart overlay, so a sheet built from the dashboard
        # always carried full-day rails no matter which session was picked.
        sv = session_view(b.get("session") or "full", rd)
        if sv["rail_scale"] != 1.0 or sv["live"]:
            cmd += ["--rail-scale", f"{sv['rail_scale']:.4f}",
                    "--rail-label", sv["label"]]
        # Mid-session the question is where price can go from HERE, so the plan
        # anchors on the last print rather than a settle that is hours old.
        if sv["live"] and not b.get("spot"):
            px = _last_print()
            if px:
                cmd += ["--spot", f"{px:.2f}"]
                log.append(f"  mid-session: anchored on the last print {px:,.2f}, "
                           f"{sv['hours']}h to expiry, rails x{sv['rail_scale']}")
        elif b.get("spot"):
            cmd += ["--spot", f"{float(b['spot']):.2f}"]
        rc, out = run(cmd)
    else:
        return jsonify(ok=False, log=f"{chain.name} was not written -- see the pull log above"), 500
    log.append(out)

    # The analytics step writes trinity.json. The PAGE is a separate render --
    # gen.py builds the ladder from that json, build.py builds the sheet around
    # it. Skipping these two was why a fresh pull left the old reference price
    # on screen: the data moved and the HTML was simply copied forward.
    if rc == 0:
        for step in (["gen.py"], ["build.py"]):
            rc, out = run(step)
            log.append(out)
            if rc != 0:
                break

    sheet = None
    if rc == 0:
        SHEETS.mkdir(exist_ok=True)
        src = ROOT / "trinity.html"
        if src.exists():
            sheet = f"{date}-teddyai.html"
            (SHEETS / sheet).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return jsonify(ok=rc == 0, log="\n".join(log), sheet=sheet)


# ------------------------------------------------------- chart review ---

@app.post("/api/review")
def review_post():
    """
    Saves the pasted chart next to a snapshot of the plan that was live when it
    was taken. Nothing here calls Claude -- the local app has no route to it --
    so the output is a handoff the folder carries across.
    """
    CHARTS.mkdir(parents=True, exist_ok=True); REVIEWS.mkdir(parents=True, exist_ok=True)
    f = request.files.get("chart")
    note = (request.form.get("note") or "").strip()
    if not f or not f.filename:
        return jsonify(ok=False, log="no image"), 400
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    ext = os.path.splitext(f.filename)[1].lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        return jsonify(ok=False, log=f"unsupported image type {ext}"), 400
    img = CHARTS / f"{stamp}{ext}"
    f.save(img)

    plan = None
    tj = ROOT / "trinity.json"
    if tj.exists():
        try:
            t = json.loads(tj.read_text())
            plan = {k: t.get(k) for k in ("spot", "hours", "panels", "vex")}
            for pk in (plan.get("panels") or {}):
                plan["panels"][pk].pop("gamma", None)      # keep the snapshot small
        except Exception:
            pass
    rec = dict(id=stamp, chart=img.name, note=note,
               created=datetime.datetime.now().isoformat(timespec="seconds"),
               plan_snapshot=plan, verdict=None)
    (REVIEWS / f"{stamp}.json").write_text(json.dumps(rec, indent=1))
    return jsonify(ok=True, id=stamp, chart=img.name,
                   handoff=f"review the chart in data/charts/{img.name} against the "
                           f"plan and re-rank the setups")


@app.get("/api/reviews")
def reviews_list():
    if not REVIEWS.exists():
        return jsonify(reviews=[])
    out = []
    for p in sorted(REVIEWS.glob("*.json"), reverse=True)[:20]:
        try:
            out.append(json.loads(p.read_text()))
        except Exception:
            pass
    return jsonify(reviews=out)


@app.get("/charts/<path:name>")
def chart(name):
    return send_from_directory(CHARTS, name)


# ---------------------------------------------------------- discussion ---

@app.get("/api/journal")
def journal_get():
    NOTES.mkdir(exist_ok=True)
    return jsonify(text=JOURNAL.read_text(encoding="utf-8") if JOURNAL.exists() else "")


@app.post("/api/journal")
def journal_post():
    NOTES.mkdir(exist_ok=True)
    body = (request.json or {}).get("text", "").strip()
    if not body:
        return jsonify(ok=False), 400
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n\n## Mark — {ts}\n\n{body}\n"
    with open(JOURNAL, "a", encoding="utf-8") as fh:
        fh.write(entry)
    return jsonify(ok=True)


# ---------------------------------------------------------------- bars ---
#
# The console has no chart any more, but the bar cache is still load-bearing:
# mid-session reports anchor on the last print, and /api/analyze scores the
# session against the plan from it. The page refreshes it headlessly.

@app.get("/api/bars")
def api_bars():
    """
    1-minute bars for one session, cached per day under data/bars/.

    NQ.c.0 is the continuous front month -- a raw symbol silently returns a
    stale series once the contract you named stops being the front one.

    Today's cache goes stale by the minute, so it is refreshed INCREMENTALLY
    (only bars after the last one held) and only ever replaced on success: a
    failed refresh must not cost you the bars you already have. Past sessions
    are finished, so their cache is final.
    """
    day = request.args.get("date", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return jsonify(ok=False, log="bad date"), 400
    have, err, fs, today = _bars_cached(day)
    if not have and err:
        return jsonify(ok=False, log=err), 400

    # ?since=<epoch> -- send back only what the browser does not already hold.
    # A tab that was hidden for an hour needs the 60 bars it missed, not a
    # re-transmission of the whole session, and certainly not a 30-day backfill.
    try: since = int(request.args.get("since", "0"))
    except ValueError: since = 0

    out = []
    for r in have:
        try: t = datetime.datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
        except Exception: continue
        ts = int(t.timestamp())
        if ts < since:            # strictly older: the bar AT `since` is resent,
            continue              # because the last one held is usually unfinished
        # `ts` is the cache's own timestamp string, passed through untouched.
        # The tracker stamps its events with exactly this, so the chart can look
        # an event's bar up by identity instead of re-deriving a key from the
        # epoch and hoping the two agree about rounding and timezone.
        out.append(dict(time=ts, ts=r["ts"], open=r["o"], high=r["h"],
                        low=r["l"], close=r["c"]))
    return jsonify(ok=True, bars=out, day=day, live=(day == today), stale_error=err,
                   partial=bool(since), total=len(have),
                   feed=fs,
                   updated=datetime.datetime.now().strftime("%H:%M:%S"))


# Past-day bar files: those verified to the close this process (never asked
# again), and those that answered with nothing, with when (asked again after
# BARS_EMPTY_RETRY_S, not on every call from the command center).
_BARS_FINAL, _BARS_EMPTY = set(), {}
BARS_EMPTY_RETRY_S = 600


def _expected_close(day):
    """
    The last minute a UTC day's bar file should hold, 'HH:MM', or None when no
    session touches the day. Mon-Thu and Sun: a session opens at 22:00Z that
    evening, so 23:59. Fri: settles at 21:00Z and nothing opens after, so 20:59.
    Sat: none. Holidays and early closes are not modelled; the fetch simply
    returns nothing more.
    """
    wd = datetime.date.fromisoformat(day).weekday()
    return "23:59" if wd in (0, 1, 2, 3, 6) else "20:59" if wd == 4 else None


def _bars_cached(day):
    """
    The cache-and-refresh half of /api/bars, shared with the command center.

    Returns (rows, stale_error, feed_status, today_utc). Rows are the on-disk
    shape -- {ts, o, h, l, c} -- refreshed incrementally when `day` is the live
    UTC day and nobody else (live_feed.py) owns the file. It was the body of
    api_bars until the command center needed the same bars without a request
    context; the route above is now a thin re-shaper over it.
    """
    BARSDIR = DATA / "bars"; BARSDIR.mkdir(parents=True, exist_ok=True)
    cache = BARSDIR / f"{day}.json"
    # THE CACHE FILES ARE UTC CALENDAR DAYS, so "is this the live one" has to be
    # asked in UTC. Using the LOCAL date meant that from 17:00 PT onward -- when
    # the tape has rolled into the next UTC day -- the live file was judged a
    # finished past session, whose cache is final and never refreshed. It then
    # answered instantly, with whatever it had, and reported no error: the
    # 2026-09-04 file sat at 120 bars from 19:00 PT while price moved 100 points.
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    STALE_S = 30

    have = []
    if cache.exists():
        try: have = json.loads(cache.read_text())
        except Exception: have = []
    if not have:                                   # salvage from a wider range file
        for p_ in BARSDIR.glob("nq-front-*.json"):
            try: rows = [r for r in json.loads(p_.read_text()) if r["ts"].startswith(day)]
            except Exception: continue
            if rows:
                have = rows; cache.write_text(json.dumps(rows)); break

    stale = (day == today and (not have or
             datetime.datetime.now().timestamp() - cache.stat().st_mtime > STALE_S
             if cache.exists() else True))
    # If live_feed.py owns this file, leave it alone. Two writers racing on one
    # cache is how you get a historical pull overwriting bars the stream has
    # already advanced past -- and the feeder is the fresher of the two by
    # definition, so the poll simply steps aside while it is alive.
    fs = feed_status()
    if fs and have:
        stale = False
    # A PAST day is final only if it reaches its close. A file that stops short
    # is a hole, not a finished session: live_feed.py died at 08:51 PT on
    # 2026-09-11 and Friday's file sat at 15:51Z all weekend, so every "Friday
    # close" downstream was Friday's morning. Filled once per process (a settled
    # day does not change); the merge is last-write-wins, the same rule the
    # feeder's own backfill of that day applies, so the two cannot disagree.
    # A Saturday has no session and is never asked for.
    short = False
    if day < today and day not in _BARS_FINAL:
        want = _expected_close(day)
        if want is None:
            _BARS_FINAL.add(day)
        elif have:
            short = have[-1]["ts"][11:16] < want
        elif datetime.datetime.now().timestamp() - _BARS_EMPTY.get(day, 0) < BARS_EMPTY_RETRY_S:
            return have, None, fs, today
    err = None
    if not have or stale or short:
        try:
            rows = _fetch_bars(day, have)
            if rows:
                have = rows
                cache.write_text(json.dumps(rows))
            if day < today:
                if have:
                    _BARS_FINAL.add(day)
                else:
                    _BARS_EMPTY[day] = datetime.datetime.now().timestamp()
        except Exception as ex:
            err = f"{type(ex).__name__}: {ex}"
    return have, err, fs, today


def cc_bars(tf=1):
    """
    Bars for the command center: [{t(ms), o, h, l, c, v}] ascending, aggregated
    to `tf`-minute candles, spanning the last two UTC days that have bars -- the
    previous and current day on a weekday -- so the chart is continuous across
    the 17:00 PT roll. Same cache and the same
    refresh rules as /api/bars (via _bars_cached), so the two never disagree
    about what the last print is. The 1-minute cache carries no volume; v is 0.
    """
    tf = max(1, int(tf or 1))
    now = datetime.datetime.now(datetime.timezone.utc).date()
    # The current UTC day and the one before it -- except that a weekend has
    # bars in neither, and on a Sunday afternoon the chart drew nothing at all.
    # So the window walks back, six days at most, until it holds two days that
    # have bars: Friday is "yesterday" all weekend, which is what is meant.
    rows, with_bars = [], 0
    for i in range(6):
        d = (now - datetime.timedelta(days=i)).isoformat()
        try:
            have, _err, _fs, _today = _bars_cached(d)
        except Exception:
            have = []
        rows = (have or []) + rows
        with_bars += bool(have)
        if with_bars >= 2 and i >= 1:
            break
    out, cur = [], None
    for r in rows:
        try:
            t = datetime.datetime.fromisoformat(str(r["ts"]).replace(" ", "T").replace("Z", "+00:00"))
        except Exception:
            continue
        ms = int(t.timestamp()) // (tf * 60) * (tf * 60) * 1000
        if cur is None or ms != cur["t"]:
            if cur is not None:
                out.append(cur)
            cur = dict(t=ms, o=float(r["o"]), h=float(r["h"]), l=float(r["l"]), c=float(r["c"]), v=0)
        else:
            cur["h"] = max(cur["h"], float(r["h"]))
            cur["l"] = min(cur["l"], float(r["l"]))
            cur["c"] = float(r["c"])
    if cur is not None:
        out.append(cur)
    out.sort(key=lambda b: b["t"])
    return out


def _client():
    import databento as db
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        raise RuntimeError("DATABENTO_API_KEY not set")
    return db.Historical(key)


def _clamp_end(cl, end, schema="ohlcv-1m"):
    """
    Never request past the end of the dataset -- that is a 422, not empty data.

    THE SCHEMA'S END, NOT THE DATASET'S: get_dataset_range() reports a top-level
    `end` that belongs to its newest schema (trades) and a per-schema map under
    "schema"; ohlcv-1m trails the top level by ten to fifteen minutes (21:40Z
    against 21:50Z on 2026-09-13), and a request into that gap is refused with
    data_schema_not_fully_available. Clamping to the top-level end is why the
    weekend refresh of the live day failed every 30 seconds.
    """
    try:
        rng = cl.metadata.get_dataset_range(dataset="GLBX.MDP3")
        sch = (rng.get("schema") or {}).get(schema) or {}
        avail = str(sch.get("end") or rng.get("end") or "")[:19]
        return avail if avail and avail < end else end
    except Exception:
        return end


def _fetch_bars(day, have):
    cl = _client()
    nxt = (datetime.date.fromisoformat(day) + datetime.timedelta(days=1)).isoformat()
    end = _clamp_end(cl, f"{nxt}T00:00:00")
    # str(pandas.Timestamp) is "2026-09-03 10:19:00+00:00" -- SPACE separated.
    # Databento's parser wants ISO 8601 with a T, and rejects the space with
    # 400 data_invalid_datetime_string. This is why the chart froze at the first
    # incremental refresh of the day and then silently stopped advancing: the
    # initial backfill takes the f"{day}T00:00:00" branch and works, every
    # refresh after it takes this branch and 400s.
    start = (have[-1]["ts"][:19].replace(" ", "T") if have
             else f"{day}T00:00:00")
    if start >= end:
        return have
    df = cl.timeseries.get_range(dataset="GLBX.MDP3", schema="ohlcv-1m",
                                 symbols=[BAR_SYMBOL], stype_in=BAR_STYPE,
                                 start=start, end=end).to_df()
    fresh = [dict(ts=str(t), o=float(r.open), h=float(r.high),
                  l=float(r.low), c=float(r.close)) for t, r in df.iterrows()]
    if not have:
        return fresh
    # LAST WRITE WINS, as live_feed.BarBook.upsert does. A forming minute is
    # written repeatedly and the last write is the complete bar; the old
    # first-wins merge kept a partial bar forever if the feeder died mid-minute
    # (a 4-point bar where the truth was 23), and session high/low, node
    # touches and the setup tracker all read that file.
    by_ts = {r["ts"]: r for r in have}
    for r in fresh:
        by_ts[r["ts"]] = r
    return [by_ts[k] for k in sorted(by_ts)]


@app.post("/api/backfill")
def api_backfill():
    """
    One call for N days of history, split into per-day caches. Far cheaper on
    request count than fetching each session separately, and it makes scrolling
    back through past sessions instant.
    """
    b = request.json or {}
    days = max(1, min(int(b.get("days", 30)), 120))
    BARSDIR = DATA / "bars"; BARSDIR.mkdir(parents=True, exist_ok=True)
    end_d = datetime.date.today()
    start_d = end_d - datetime.timedelta(days=days)
    if b.get("only_if_missing"):
        # Runs on every page load, so do nothing when the window is already
        # cached -- otherwise opening the dashboard re-pulls a month each time.
        want = [start_d + datetime.timedelta(days=i) for i in range(days)]
        want = [d for d in want if d.weekday() < 5 and d < end_d]
        missing = [d for d in want if not (BARSDIR / f"{d.isoformat()}.json").exists()]
        if len(missing) <= max(1, len(want) // 5):
            return jsonify(ok=True, skipped=True, cached=len(want) - len(missing),
                           days=0, bars=0)
    try:
        cl = _client()
        end = _clamp_end(cl, f"{(end_d + datetime.timedelta(days=1)).isoformat()}T00:00:00")
        df = cl.timeseries.get_range(dataset="GLBX.MDP3", schema="ohlcv-1m",
                                     symbols=[BAR_SYMBOL], stype_in=BAR_STYPE,
                                     start=f"{start_d.isoformat()}T00:00:00", end=end).to_df()
    except Exception as ex:
        return jsonify(ok=False, log=f"{type(ex).__name__}: {ex}"), 400
    byday = {}
    for t, r in df.iterrows():
        ts = str(t)
        byday.setdefault(ts[:10], []).append(dict(ts=ts, o=float(r.open), h=float(r.high),
                                                  l=float(r.low), c=float(r.close)))
    for d, rows in byday.items():
        (BARSDIR / f"{d}.json").write_text(json.dumps(rows))
    return jsonify(ok=True, days=len(byday), bars=len(df),
                   first=min(byday) if byday else None, last=max(byday) if byday else None)


@app.get("/api/days")
def api_days():
    BARSDIR = DATA / "bars"
    if not BARSDIR.exists(): return jsonify(days=[])
    ds = sorted(p.stem for p in BARSDIR.glob("*.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem))
    return jsonify(days=ds)


@app.post("/api/analyze")
def api_analyze():
    """
    Score the session so far against the live plan.

    This replaced a paste-a-screenshot flow. With the bars in hand a picture of
    the chart is the weaker input: the numbers say exactly which levels were
    touched, whether they held, and how long price spent each side of each flip,
    where an image only shows roughly where price went. Nothing in the console
    sends an image now that the canvas is gone, but the branch stays so old
    records and the Chart Review flow still line up.
    """
    b = request.json or {}
    day = b.get("date", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return jsonify(ok=False, log="bad date"), 400
    cache = DATA / "bars" / f"{day}.json"
    tj = ROOT / "trinity.json"
    if not cache.exists():
        return jsonify(ok=False, log="no bars cached for that date yet — "
                                     "give the background refresh a moment, or "
                                     "generate a report"), 400
    if not tj.exists():
        return jsonify(ok=False, log="no report generated yet"), 400
    rows, t = json.loads(cache.read_text()), json.loads(tj.read_text())

    bars = []
    for r in rows:
        ts = r["ts"]
        hh, mm = int(ts[11:13]), int(ts[14:16])
        if (hh, mm) >= (13, 30) and (hh, mm) <= (20, 0):
            bars.append(r)
    if len(bars) < 5:
        return jsonify(ok=False, log="not enough RTH bars yet"), 400

    o, last = bars[0]["o"], bars[-1]["c"]
    hi, lo = max(x["h"] for x in bars), min(x["l"] for x in bars)
    spot = t.get("spot") or o
    atm = None
    em = None
    for p_ in (t.get("panels") or {}).values():
        pass
    em = t.get("em") or (spot * 0.1696 / (252 ** 0.5))     # fall back to the sheet's convention

    flips = []
    for name, p_ in (t.get("panels") or {}).items():
        for h in (p_.get("hvl") or []):
            above = sum(1 for x in bars if x["c"] > h)
            flips.append(dict(name=name, price=h, above_min=above,
                              below_min=len(bars) - above,
                              side_now="above" if last > h else "below"))

    g = ((t.get("panels") or {}).get("0DTE") or {}).get("gamma") or {}
    tol, need = max(6.0, em * 0.02), em * 0.25
    nodes = []
    for k, v in g.items():
        if abs(v) < 6: continue
        K = float(k)
        idx = next((i for i, x in enumerate(bars) if x["l"] - tol <= K <= x["h"] + tol), None)
        rec = dict(price=K, gamma=v, touched=idx is not None)
        if idx is not None:
            fwd = bars[idx:]
            up = next((i for i, x in enumerate(fwd) if x["h"] >= K + need), 10**9)
            dn = next((i for i, x in enumerate(fwd) if x["l"] <= K - need), 10**9)
            from_below = bars[max(idx - 1, 0)]["c"] < K
            if up == dn == 10**9:
                rec["outcome"] = "unresolved"
            else:
                went_up = up < dn
                rec["outcome"] = ("held" if (from_below and not went_up) or
                                  (not from_below and went_up) else "broke")
            rec["touch_time"] = bars[idx]["ts"][11:16]
            rec["approached_from"] = "below" if from_below else "above"
        nodes.append(rec)
    nodes.sort(key=lambda r: -abs(r["gamma"]))

    out = dict(date=day, created=datetime.datetime.now().isoformat(timespec="seconds"),
               session=dict(open=o, high=hi, low=lo, last=last, range=round(hi - lo, 2),
                            bars=len(bars), first=bars[0]["ts"][11:16], latest=bars[-1]["ts"][11:16]),
               plan=dict(reference=spot, em=round(em, 1),
                         rail_hi=round(spot + em, 1), rail_lo=round(spot - em, 1),
                         hi_vs_rail=round(hi - (spot + em), 1),
                         lo_vs_rail=round(lo - (spot - em), 1),
                         range_over_em=round((hi - lo) / em, 2) if em else None),
               flips=flips, nodes=nodes)

    ANALYSIS = DATA / "analysis"; ANALYSIS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    img = b.get("image")
    if img and img.startswith("data:image/png;base64,"):
        import base64
        CHARTS.mkdir(parents=True, exist_ok=True)
        (CHARTS / f"{stamp}.png").write_bytes(base64.b64decode(img.split(",", 1)[1]))
        out["chart"] = f"{stamp}.png"
    (ANALYSIS / f"{stamp}.json").write_text(json.dumps(out, indent=1))
    out["id"] = stamp
    return jsonify(ok=True, analysis=out)


@app.get("/api/track")
def api_track():
    """
    Where each setup in the live report stands, plus the log of how it got there.

    The setups are parsed out of trinity.html -- the RENDERED report. Nothing
    here writes back to it; the sheet is input. If the markup ever changes,
    setup_track raises and this returns the error rather than an empty board,
    because a tidy empty scoreboard beside a report showing four live setups is
    the one output that would actually mislead.
    """
    if setup_track is None:
        return jsonify(ok=False, log=f"setup_track unavailable: {_TRACK_ERR}"), 500

    day = request.args.get("date") or ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        try:
            day = json.loads((ROOT / "trinity.json").read_text())["report_date"]
        except Exception:
            day = datetime.date.today().isoformat()

    sheet = ROOT / "trinity.html"
    if not sheet.exists():
        return jsonify(ok=False, log="no trinity.html yet — generate a report"), 400
    try:
        setups, reading = setup_track.read_sheet(sheet)   # memoised on mtime
    except Exception as ex:
        return jsonify(ok=False, log=f"could not read the setups: {ex}"), 500

    # Grade on RTH when there is an RTH session to grade. Premarket -- which is
    # when this panel is most often looked at -- has none yet, and an empty board
    # at 06:00 would say nothing about an overnight tape that has already run
    # through half the levels. So fall back to the whole UTC day and say which
    # window was actually walked.
    full = request.args.get("full_day") == "1"
    bars = setup_track.load_bars(day, rth_only=not full)
    if len(bars) < 5 and not full:
        bars, full = setup_track.load_bars(day, rth_only=False), True
    rows = [setup_track.track(s, bars) for s in setups]

    # ---- the event log ----------------------------------------------------
    # One line per thing that actually happened, newest first. Built from the
    # first-touch stamps the tracker already records, so the log and the board
    # can never disagree about when something occurred.
    KIND = {"entry": ("trigger", "triggered at"),
            "stop":  ("stop",    "stopped at"),
            "tp1":   ("tp",      "TP1"), "tp2": ("tp", "TP2"), "tp3": ("tp", "TP3")}
    events = []
    for r in rows:
        for k, ts in (r["touches"] or {}).items():
            if not isinstance(ts, str) or k not in KIND:
                continue
            kind, label = KIND[k]
            lvl = (r["entry"] if k == "entry" else r["stop"] if k == "stop"
                   else r["tps"][int(k[-1]) - 1])
            events.append(dict(ts=ts, at=ts[11:16], kind=kind, setup=r["id"],
                               price=lvl,
                               text=(f"{r['id']} · " if r['id'] != r['name'] else "") + f"{r['name']} — {label} {lvl:,.0f}"))
        for k, ts in (r["touches"] or {}).items():
            if isinstance(ts, str) and k.startswith("missed_tp"):
                n = k[-1]
                events.append(dict(ts=ts, at=ts[11:16], kind="missed", setup=r["id"],
                                   price=r["tps"][int(n) - 1],
                                   text=f"{r['id']} · TP{n} {r['tps'][int(n)-1]:,.0f} "
                                        f"reached with no fill"))
    events.sort(key=lambda e: e["ts"], reverse=True)

    t = {}
    try:
        t = json.loads((ROOT / "trinity.json").read_text(encoding="utf-8"))
    except Exception:
        pass

    return jsonify(ok=True, date=day, bars=len(bars),
                   window=[bars[0]["ts"][11:16], bars[-1]["ts"][11:16]] if bars else None,
                   last_px=bars[-1]["c"] if bars else None,
                   fingerprint=setup_track.fingerprint(setups),
                   reading=reading,
                   asof=t.get("asof"), settle=t.get("settle"),
                   oi_vintage=t.get("oi_vintage"),
                   setups=rows, events=events[:60], feed=feed_status())


@app.get("/api/analyses")
def api_analyses():
    d = DATA / "analysis"
    if not d.exists(): return jsonify(items=[])
    items = []
    for p_ in sorted(d.glob("*.json"), reverse=True)[:10]:
        try: items.append(json.loads(p_.read_text()))
        except Exception: pass
    return jsonify(items=items)


@app.get("/api/levels")
def api_levels():
    """
    Current plan levels, straight from trinity.json.

    Drawn on a canvas until the chart came out; the console now reads only the
    session-scaled band, ATM IV and hours off it for the context line. The full
    level list is still returned -- it is the cheapest way for anything else to
    ask what the live plan says.
    """
    tj = ROOT / "trinity.json"
    if not tj.exists():
        return jsonify(ok=False, levels=[], log="no report generated yet")
    t = json.loads(tj.read_text())
    lv = []
    for name, p_ in (t.get("panels") or {}).items():
        for h in (p_.get("hvl") or []):
            lv.append(dict(price=h, label=f"{name} flip", kind="flip"))
    g = ((t.get("panels") or {}).get("0DTE") or {}).get("gamma") or {}
    for k, v in g.items():
        if abs(v) >= 8:
            lv.append(dict(price=float(k), label=f"{'+' if v>0 else ''}{v}",
                           kind="pos" if v > 0 else "neg", weight=abs(v)))
    spot = t.get("spot")
    if spot:
        lv.append(dict(price=spot, label="reference", kind="spot"))

    # Rails are rescaled to the SELECTED SESSION. trinity.json always stores the
    # full-day expected move; the band you draw depends on how much of a day you
    # are actually trading. Asia is 0.43x, not 1.0x.
    sid = request.args.get("session") or "full"
    sv = session_view(sid, request.args.get("report_date")
                      or datetime.date.today().isoformat())
    em_full = t.get("em")
    em_s = (em_full * sv["rail_scale"]) if em_full else None
    if spot and em_s:
        tag = "1D" if sid == "full" else sv["label"]
        lv.append(dict(price=spot + em_s, label=f"{tag} max", kind="rail"))
        lv.append(dict(price=spot - em_s, label=f"{tag} min", kind="rail"))
    return jsonify(ok=True, levels=lv, spot=spot,
                   iv=t.get("iv_atm"), em=em_s, em_full=em_full,
                   hours=t.get("hours"), session=sv)


# The sheet is built to stand alone at full browser width: .wrap centres a
# 1400px column. Inside the dashboard's right-hand panel that reads as a band of
# dead margin either side, and the type is sized for a full page rather than a
# panel. Rather than fork the sheet, ?embed=1 appends an override -- so the file
# on disk and the "open full" view are untouched.
EMBED_CSS = """
<style id="embed-fit">
.wrap{max-width:none!important;padding:14px 16px 36px!important;gap:18px!important}
body{font-size:13.5px}
.mast{padding-bottom:11px}
.mast h1{font-size:26px!important}
.mast .sub{font-size:12.5px;max-width:none}
.stamp{font-size:10.5px}
.strip{grid-template-columns:repeat(auto-fit,minmax(122px,1fr))}
.st{padding:10px 11px}
.st .v{font-size:17px}
.st .k{font-size:9.5px}
.st .n{font-size:10.5px}
.sech h2{font-size:15px}
.note{font-size:12px;padding:11px 13px}
.plans{grid-template-columns:repeat(auto-fit,minmax(268px,1fr));gap:13px}
.pl h3{font-size:14px}
.lad{font-size:12px}
.foot{font-size:11px}
</style>
"""


LADDER_FIT = """
<style>
body{margin:0;background:var(--panel,#FBFBFA)}
.sec{margin:0}
/* The dashboard already puts "Teddy Analysis" on the card header above this
   frame; the section carries its own, and two in a row reads like a mistake.
   Keep the hint line, which is the part that actually explains the columns. */
.sech h2{display:none}
.sech{padding:9px 16px 0;margin:0;border:0}
.sech .hint{font-size:10.5px;line-height:1.5}
.lad{padding:10px 16px 18px;font-size:11.5px}
</style>
"""


@app.get("/ladder")
def ladder():
    """
    The Teddy Analysis ladder, lifted whole out of the rendered sheet.

    Same rule as the setups and the day's reading: read the report, never rebuild
    it. Reconstructing the ladder's wrapper and header row here would have meant
    a second copy to keep in step with gen.py, and it would have gone stale the
    first time a column changed.

    It arrives in an iframe rather than inline because the fragment's class names
    are single letters -- .r, .k, .b, .v, .p, .n, .z -- which would collide with
    half the dashboard's stylesheet. The iframe carries the SHEET's own <style>,
    so it looks exactly like it does in the report, and the CSS variables come
    with it.
    """
    src = ROOT / "trinity.html"
    if not src.exists():
        return Response('<body style="font:13px system-ui;color:#777;padding:18px">'
                        'No report yet.</body>', mimetype="text/html")
    doc = src.read_text(encoding="utf-8")

    i = doc.find("Teddy Analysis")
    if i < 0:
        return Response('<body style="font:13px system-ui;color:#777;padding:18px">'
                        'No Teddy Analysis section in this sheet.</body>',
                        mimetype="text/html")
    start = doc.rfind("<section", 0, i)
    end = doc.find("</section>", i)
    if start < 0 or end < 0:
        return Response('<body style="font:13px system-ui;color:#777;padding:18px">'
                        'Could not isolate the ladder.</body>', mimetype="text/html")
    section = doc[start:end + len("</section>")]

    style = re.search(r"<style>(.*?)</style>", doc, re.S)
    css = style.group(1) if style else ""
    return Response(
        f'<!doctype html><meta charset="utf-8">'
        f'<style>{css}</style>{LADDER_FIT}<body>{section}</body>',
        mimetype="text/html")


@app.get("/sheets/<path:name>")
def sheet(name):
    if request.args.get("embed") and name.endswith(".html"):
        p = SHEETS / name
        if p.exists():
            return Response(p.read_text(encoding="utf-8") + EMBED_CSS,
                            mimetype="text/html")
    return send_from_directory(SHEETS, name)


# --------------------------------------------------------------------- ui ---

CSS = """
:root{--ground:#F2F3F1;--panel:#FBFBFA;--panel2:#E9EBE7;--ink:#14181A;--ink2:#4A5350;
--ink3:#7C8783;--rule:#D2D6D0;--rule2:#BFC5BE;--brass:#9A6E1F;--brassx:#EFE2C6;
--neg:#A8412C;--pos:#1F6E62;--posx:#CFE3DE;--warn:#8A6A12;--vio:#5B4B8A}
@media(prefers-color-scheme:dark){:root{--ground:#101413;--panel:#171C1B;--panel2:#1F2624;
--ink:#E9EDEA;--ink2:#A8B2AE;--ink3:#77837E;--rule:#2A322F;--rule2:#3A443F;--brass:#D6A64A;
--brassx:#3A3018;--neg:#E07A5F;--pos:#5FBFAC;--posx:#123029;--warn:#D6B04A;--vio:#A395D6}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font:15px/1.55 "IBM Plex Sans",
-apple-system,Segoe UI,sans-serif;-webkit-font-smoothing:antialiased}
.mono{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
a{color:var(--brass)}
.top{border-bottom:2px solid var(--ink);background:var(--panel)}
.topin{max-width:1500px;margin:0 auto;padding:14px 18px;display:flex;align-items:center;gap:14px}
.brand{font-family:Archivo,sans-serif;font-weight:800;font-size:24px;letter-spacing:-.02em}
.brand span{color:var(--brass)}
.wrap{max-width:1080px;margin:0 auto;padding:28px 22px 70px;display:flex;flex-direction:column;gap:26px}
h2{font-family:Archivo,sans-serif;font-size:17px;font-weight:700;margin:0 0 2px}
.sech{border-bottom:1px solid var(--rule2);padding-bottom:7px;margin-bottom:14px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:3px;padding:16px 17px;
display:flex;flex-direction:column;gap:8px;text-decoration:none;color:inherit}
.card.live{border-color:var(--pos)}
.card.planned{opacity:.62}
.card h3{font-family:Archivo,sans-serif;font-size:15.5px;margin:0;font-weight:700}
.card p{margin:0;font-size:13px;color:var(--ink2)}
.tag{align-self:flex-start;font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;
font-weight:700;padding:2px 8px;border-radius:2px;border:1px solid}
.tag.live{color:var(--pos);background:var(--posx);border-color:var(--pos)}
.tag.planned{color:var(--ink3);border-color:var(--rule2)}
.panel{background:var(--panel);border:1px solid var(--rule);border-radius:3px;padding:18px}
.row{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end}
label{display:block;font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;
color:var(--ink3);font-weight:600;margin-bottom:5px}
input,select{background:var(--ground);color:var(--ink);border:1px solid var(--rule2);
border-radius:2px;padding:8px 10px;font:14px "IBM Plex Mono",monospace}
button{font:600 13px/1 "IBM Plex Sans",sans-serif;padding:11px 16px;border-radius:2px;
border:1px solid var(--ink);background:var(--ink);color:var(--ground);cursor:pointer}
button.ghost{background:none;color:var(--ink);border-color:var(--rule2)}
button.go{background:var(--pos);border-color:var(--pos);color:#fff}
button:disabled{opacity:.4;cursor:not-allowed}
.note{font-size:13px;color:var(--ink2);background:var(--panel);border:1px solid var(--rule);
border-left:3px solid var(--brass);border-radius:2px;padding:13px 16px}
.note.w{border-left-color:var(--neg)}
.note b{color:var(--ink)}
pre{background:var(--panel2);border:1px solid var(--rule);border-radius:2px;padding:12px;
font:12px/1.5 "IBM Plex Mono",monospace;max-height:340px;overflow:auto;white-space:pre-wrap}
.quote{display:flex;align-items:baseline;gap:10px;padding:12px 14px;border:1px solid var(--brass);
background:var(--brassx);border-radius:2px}
.quote b{font-size:24px;font-family:"IBM Plex Mono",monospace}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);text-align:left;
padding:8px 10px;border-bottom:1px solid var(--rule2)}
td{padding:7px 10px;border-bottom:1px solid var(--rule)}
td.r,th.r{text-align:right}
.muted{color:var(--ink3);font-size:12.5px}
/* Tools left and narrow, the report right and wide -- the Teddy Analysis
   ladder needs ~900px, so the report has to own the wide column. */
.app{display:grid;grid-template-columns:minmax(460px,680px) minmax(0,1fr);
align-items:start;gap:0}
.left{padding:18px 16px 60px;display:flex;flex-direction:column;gap:14px;min-width:0}
.right{position:sticky;top:0;height:100vh;border-left:1px solid var(--rule);
background:var(--panel);display:flex;flex-direction:column;min-width:0}
.rhd{padding:12px 16px;border-bottom:1px solid var(--rule);background:var(--panel2);
display:flex;align-items:baseline;gap:12px}
.rhd h2{font-size:13.5px;font-weight:700;font-family:Archivo,sans-serif}
.rhd a{margin-left:auto;font-size:11.5px}
.thread{max-height:280px;overflow:auto;padding:13px 15px;white-space:pre-wrap;font-size:13px;
line-height:1.6;color:var(--ink2)}
.compose{border-top:1px solid var(--rule);padding:12px 15px;background:var(--panel2)}
.compose textarea{width:100%;background:var(--ground);color:var(--ink);border:1px solid var(--rule2);
border-radius:2px;padding:9px;font:13.5px/1.5 inherit;resize:vertical}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:3px;overflow:hidden}
.card .hd{padding:11px 15px;border-bottom:1px solid var(--rule);background:var(--panel2);
display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.card .hd h2{white-space:nowrap}
.card .hd h2{font-size:13.5px;font-weight:700;font-family:Archivo,sans-serif}
.card .hd .muted,.card .hd a{margin-left:auto;font-size:11.5px}
.card .body{padding:15px}
.card pre{margin:0;border:0;border-radius:0;max-height:260px}
iframe{flex:1;width:100%;border:0;background:var(--ground)}
.drop{border:1.5px dashed var(--rule2);border-radius:3px;padding:26px 16px;text-align:center;
color:var(--ink3);font-size:13px;transition:border-color .12s,background .12s}
.drop.over{border-color:var(--brass);background:var(--brassx)}
.drop img{max-width:100%;max-height:280px;border:1px solid var(--rule);border-radius:2px}
.link{color:var(--brass);cursor:pointer;text-decoration:underline}
.quote{display:flex;align-items:baseline;gap:10px;padding:10px 13px;border:1px solid var(--brass);
background:var(--brassx);border-radius:2px}.quote b{font-size:20px;font-family:"IBM Plex Mono",monospace}
.warn{padding:10px 13px;border:1px solid var(--neg);background:var(--negx);border-radius:2px;
color:var(--neg);font-size:13px}
.rev{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-bottom:1px solid var(--rule)}
.rev:last-child{border-bottom:0}
.rev img{width:190px;border:1px solid var(--rule);border-radius:2px;display:block}
.rev>div{flex:1;min-width:0;font-size:12.5px}
.vd{margin-top:7px;padding:8px 10px;border-left:2px solid var(--brass);background:var(--panel2);
border-radius:2px;font-size:12.5px}
@media(max-width:1100px){.app{grid-template-columns:1fr}
.right{position:static;height:auto;border-left:0;border-top:1px solid var(--rule)}
iframe{height:640px;flex:none}}

/* ---------- the brief: the day's reading + gameplan, above the chart ------- */
.brief{background:var(--panel);border-bottom:1px solid var(--rule)}
.bhd,.chd,.thd,.thd2{padding:10px 16px;border-bottom:1px solid var(--rule);
background:var(--panel2);display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.bhd h2,.chd h2,.thd h2,.thd2 h2{font-size:13px;font-weight:700;font-family:Archivo,sans-serif}
.bhd .muted,.chd .muted{margin-left:auto}
.bhd button,.chd button{padding:5px 10px;font-size:11.5px}
.bbody{padding:14px 16px;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);
gap:18px;align-items:start}
.rd{margin:0;padding-left:17px;font-size:12.5px;line-height:1.62;color:var(--ink2)}
.rd li{margin-bottom:8px}
.rd b{color:var(--ink)}
.gp{display:grid;grid-template-columns:repeat(auto-fit,minmax(216px,1fr));gap:10px;
align-content:start}
.gc{border:1px solid var(--rule);border-radius:3px;padding:9px 11px;background:var(--ground)}
.gc.pri{border-color:var(--brass)}
.gc h4{margin:0 0 5px;font:700 12.5px Archivo,sans-serif;display:flex;gap:7px;align-items:baseline}
.gc h4 .sid{color:var(--brass)}
.gc .tg{font-size:11.5px;color:var(--ink3);margin-bottom:7px;line-height:1.45}
.gc .rows{display:grid;grid-template-columns:auto 1fr;gap:1px 9px;font-size:11.5px}
.gc .rows .k{color:var(--ink3)}
.gc .rows .v{text-align:right;font-family:"IBM Plex Mono",monospace}
@media(max-width:1180px){.bbody{grid-template-columns:1fr}}

/* ---------- chart band ---------------------------------------------------- */
.band{display:grid;grid-template-columns:minmax(0,1fr) 372px;
border-bottom:2px solid var(--ink);background:var(--panel)}
.chartwrap{display:flex;flex-direction:column;min-width:0}
.cbody{padding:0}
#cwrap{position:relative;height:430px}
#px{position:absolute;inset:0;cursor:crosshair}
#cread{padding:5px 15px 0;font-size:11.5px;height:19px}
.live{font:700 9.5px/1 "IBM Plex Sans",sans-serif;letter-spacing:.09em;padding:3px 7px;
border-radius:2px;background:var(--pos);color:#fff}
.toggles{display:flex;gap:14px;flex-wrap:wrap;padding:9px 15px;
border-top:1px solid var(--rule);font-size:11.5px;color:var(--ink3)}
.toggles label{display:flex;align-items:center;gap:5px;cursor:pointer;
text-transform:none;letter-spacing:0;font-weight:400;margin:0;font-size:11.5px}
.toggles input{margin:0;accent-color:var(--brass)}

/* ---------- tracking panel ------------------------------------------------ */
.track{border-left:1px solid var(--rule);display:flex;flex-direction:column;min-width:0;
background:var(--panel)}
.thd .muted,.thd2 .muted{margin-left:auto}
.thd2{border-top:1px solid var(--rule)}
.board{padding:4px 0}
.sr{display:grid;grid-template-columns:16px 1fr auto;gap:8px;align-items:baseline;
padding:7px 14px;border-bottom:1px solid var(--rule);font-size:12px}
.sr:last-child{border-bottom:0}
.sr .sid{font:700 12px Archivo,sans-serif;color:var(--brass)}
.sr .nm{min-width:0}
.sr .nm .n{font-weight:600}
.sr .nm .d{color:var(--ink3);font-size:11px;line-height:1.4;margin-top:2px}
.st{font:700 9.5px/1 "IBM Plex Sans",sans-serif;letter-spacing:.07em;text-transform:uppercase;
padding:3px 6px;border-radius:2px;border:1px solid;white-space:nowrap}
.st.wait{color:var(--ink3);border-color:var(--rule2)}
.st.play{color:var(--brass);border-color:var(--brass);background:var(--brassx)}
.st.win{color:var(--pos);border-color:var(--pos);background:var(--posx)}
.st.loss{color:var(--neg);border-color:var(--neg)}
.st.miss{color:var(--vio);border-color:var(--vio)}
.st.gone{color:var(--ink3);border-color:var(--rule2);opacity:.7}
.elog{flex:1;overflow:auto;max-height:270px;padding:4px 0;font-size:11.5px}
.ev{display:grid;grid-template-columns:44px 8px 1fr;gap:7px;align-items:baseline;
padding:5px 14px;border-bottom:1px solid var(--rule)}
.ev:last-child{border-bottom:0}
.ev .t{font-family:"IBM Plex Mono",monospace;color:var(--ink3);font-size:11px}
.ev .dot{width:7px;height:7px;border-radius:50%;align-self:center}
.ev.trigger .dot{background:var(--brass)} .ev.tp .dot{background:var(--pos)}
.ev.stop .dot{background:var(--neg)}     .ev.missed .dot{background:var(--vio)}
.ev b{color:var(--ink)}
@media(max-width:1180px){.band{grid-template-columns:1fr}
.track{border-left:0;border-top:1px solid var(--rule)}
.elog{max-height:220px}}

/* ---------- ladder ------------------------------------------------------- */
.ladbox{background:var(--panel);border-bottom:2px solid var(--ink)}
.ladbox iframe{width:100%;height:520px;border:0;background:var(--panel);display:block}
.ladbox .muted{margin-left:auto}
.ladbox button{padding:5px 10px;font-size:11.5px}

"""

def page(body):
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TeddyAI</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@700;800&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style></head><body>
<div class="top"><div class="topin">
  <div class="brand">Teddy<span>AI</span></div>
</div></div>
<script>
async function state(){{const r=await fetch('/api/state');return await r.json();}}
</script>
{body}
</body></html>"""


CONSOLE = """
<!-- The day's reading and the gameplan, mirrored from the rendered sheet so the
     words stay authored in exactly one place. Collapsible, because on a second
     look you want the chart at the top of the screen, not the prose. -->
<section class="brief" id="brief">
  <div class="bhd">
    <h2>Reading the day</h2>
    <span class="muted" id="briefstamp"></span>
    <button class="ghost" id="brieftoggle">hide</button>
  </div>
  <div class="bbody" id="bbody">
    <ul class="rd" id="reading"></ul>
    <div class="gp" id="gameplan"></div>
  </div>
</section>

<section class="band">
  <div class="chartwrap">
    <div class="chd">
      <h2>Price &amp; plan</h2>
      <span class="muted mono" id="chartstat">—</span>
      <span class="live" id="livebadge" hidden>LIVE</span>
      <button class="ghost" id="chartreset"
              title="wheel = time · shift-wheel or wheel over the price axis = price · drag = pan · drag on the axis = stretch price · double-click = reset">Reset</button>
      <button class="ghost" id="chartreload">Reload</button>
    </div>
    <div class="cbody">
      <div id="cwrap"><canvas id="px"></canvas></div>
      <div id="cread" class="muted mono"></div>
      <div class="toggles" id="toggles">
        <label><input type="checkbox" id="t_profile"> gamma profile</label>
        <label><input type="checkbox" id="t_nodes" checked> gamma nodes</label>
        <label><input type="checkbox" id="t_flips" checked> flips</label>
        <label><input type="checkbox" id="t_rails" checked> rails</label>
        <label><input type="checkbox" id="t_setups" checked> setups</label>
        <label><input type="checkbox" id="t_events" checked> events</label>
      </div>
    </div>
  </div>

  <aside class="track">
    <div class="thd"><h2>Setups</h2><span class="muted mono" id="trackstamp"></span></div>
    <div class="board" id="board"><div class="muted" style="padding:12px 14px">loading…</div></div>
    <div class="thd2"><h2>Log</h2><span class="muted" id="logcount"></span></div>
    <div class="elog" id="elog"></div>
  </aside>
</section>

<!-- The ladder, lifted out of the sheet and framed so its single-letter class
     names cannot reach the dashboard's stylesheet. -->
<section class="ladbox" id="ladbox">
  <div class="bhd"><h2>Teddy Analysis</h2>
    <span class="muted" id="ladstamp">gamma ladder, from the current sheet</span>
    <button class="ghost" id="ladtoggle">hide</button></div>
  <iframe id="ladframe" title="Teddy Analysis ladder"></iframe>
</section>

<div class="app">

  <main class="left">
<section class="card">
      <div class="hd"><h2>Premarket Report</h2>
        <span class="muted" id="srcnote">Databento GLBX.MDP3</span></div>
      <div class="body">
        <div class="row">
          <div><label>Settlement date</label><input id="date" type="date"></div>
          <div><label>Report for</label><input id="rdate" type="date"></div>
          <div><label>Session</label>
            <select id="sess"
                    title="which session you are trading — sets time to expiry and scales the rails">
              <option value="rth">US RTH</option>
              <option value="asia">Asia</option>
              <option value="europe">Europe</option>
              <option value="full">Full day</option>
            </select></div>
          <div><label>Hours to expiry</label><input id="hrs" size="6" placeholder="6.5"></div>
        </div>
        <div class="row" style="margin-top:14px;align-items:center">
          <button class="go" id="go">Generate report</button>
          <span class="muted" id="lastpx">—</span>
          <span class="muted" id="msg"></span>
        </div>
        <div class="muted mono" id="ctx"
             style="margin-top:12px;font-size:11.5px;line-height:1.7"></div>
      </div>
    </section>
<section class="card" id="acard">
      <div class="hd"><h2>Session vs plan</h2>
        <span class="muted" id="astamp"></span>
        <button class="go" id="analyze" style="padding:6px 11px;font-size:12px"
                title="score the session so far against the live plan">Analyze</button></div>
      <div class="body" id="abody"><div class="muted">Not run yet — Analyze scores
        the session so far against the live plan, from the cached 1-minute bars.</div></div>
    </section>
<section class="card">
      <div class="hd"><h2>Discussion</h2>
        <span class="muted">writes to notes/journal.md</span></div>
      <div class="body" style="padding:0">
<div class="thread" id="thread">loading…</div>
    <div class="compose">
      <textarea id="t" rows="3" placeholder="Notes, questions, what you decided…"></textarea>
      <div class="row" style="margin-top:8px">
        <button class="go" id="add">Add</button>
        <span class="muted" id="m"></span>
      </div>

    </div>
  
      </div>
    </section>
<section class="card">
      <div class="hd"><h2>Earlier analyses</h2></div>
      <div class="body"><div id="list" class="muted">none yet</div></div>
    </section>
<section class="card">
      <div class="hd"><h2>Output</h2></div>
      <pre id="log">idle — nothing has run.</pre>
    </section>
  </main>

  <aside class="right">
    <div class="rhd"><h2>Latest report</h2>
      <a id="openfull" target="_blank" class="muted">open full</a></div>
    <iframe id="frame"></iframe>
    <div id="noreport" class="muted" style="padding:24px 20px">
      No report yet — generate one on the left.</div>
  </aside>

</div>
<script>
const $=i=>document.getElementById(i);


/* =========================================================================
   PRICE & PLAN — candles, the plan levels, and the setups drawn on top.

   Hand-drawn on a canvas rather than pulled from a library: the CDN is blocked
   on this network, so a hosted chart library is a dependency that silently
   vanishes. It also lets the gamma profile share the price axis instead of
   being layered over a foreign one, which is the whole point of the panel.

   EVERYTHING IS ANCHORED TO PRICE, NOT TO PIXELS. Every level, every gamma
   node, every setup line goes through Y(price), which is recomputed from the
   current view on each draw. So zooming and panning move the tape and the
   structure together and they cannot drift apart -- there is no cached pixel
   position anywhere in here.

   THE TIME AND PRICE AXES ARE INDEPENDENT. Zooming time must not restretch
   price, and price zoom must not be dragged around by one far-off strike:
     wheel                     time
     shift-wheel, or wheel over the price axis   price
     drag                      pan
     drag on the price axis    stretch price
     double-click              reset both
   ========================================================================= */
let BARS=[], LEVELS=[], TRACK=null, IDX={}, hover=null, drag=null;
let LASTL=null, LASTBAR=null, BARN=0, STALE_ERR=null;

/* view is a window over BAR INDICES and may run past either end -- that is what
   puts empty space to the right of the last print instead of jamming it against
   the price axis. pz/pc are the price zoom and centre, kept separate from the
   time window so the two axes never pull on each other. */
let view={i0:0,i1:0}, pz=1, pc=null, atEnd=true;
const RPAD=90;                       // slots you may scroll into on either side
const PAD={l:10,r:78,t:12,b:24}, PROFW=104;

function css(v){return getComputedStyle(document.documentElement).getPropertyValue(v).trim();}
function todayISO(){ return new Date().toLocaleDateString('en-CA'); }
function sessionId(){ return $('sess').value || 'rth'; }
function levelsURL(){
  return '/api/levels?session=' + encodeURIComponent(sessionId())
       + '&report_date=' + encodeURIComponent($('rdate').value || '');
}
const ON = id => $(id) && $(id).checked;
const fmt0 = n => Number(n).toLocaleString(undefined,{maximumFractionDigits:0});

function stateClass(s){
  if(s==='in play') return 'play';
  if(s && s.startsWith('tp')) return 'win';
  if(s==='stopped') return 'loss';
  if(s==='missed') return 'miss';
  if(s==='expired') return 'gone';
  return 'wait';
}
function stateColour(s){
  const k=stateClass(s);
  return k==='play' ? css('--brass') : k==='win' ? css('--pos')
       : k==='loss' ? css('--neg')   : k==='miss' ? css('--vio') : css('--ink3');
}
function fitView(){
  view={i0:0, i1: BARS.length + Math.max(12, Math.round(BARS.length*0.12))};
  pz=1; pc=null; atEnd=true;
}

/* ---------------------------------------------------------------- drawing */
function draw(){
  const cv=$('px'), wrap=$('cwrap'); if(!cv||!wrap) return;
  const dpr=window.devicePixelRatio||1, W=wrap.clientWidth, H=wrap.clientHeight;
  if(!W||!H) return;
  cv.width=W*dpr; cv.height=H*dpr; cv.style.width=W+'px'; cv.style.height=H+'px';
  const x=cv.getContext('2d'); x.setTransform(dpr,0,0,dpr,0,0);
  x.fillStyle=css('--panel'); x.fillRect(0,0,W,H);
  if(!BARS.length){ x.fillStyle=css('--ink3'); x.font='12px system-ui';
    x.fillText('no bars for this session yet',16,28); return; }
  if(view.i1<=view.i0) fitView();

  const n=Math.max(2, view.i1-view.i0), pw=W-PAD.l-PAD.r, ph=H-PAD.t-PAD.b;
  const vis=BARS.slice(Math.max(0,view.i0), Math.min(BARS.length, view.i1));
  const off=Math.max(0,view.i0);
  if(!vis.length){ x.fillStyle=css('--ink3'); x.font='12px system-ui';
    x.fillText('scrolled off the session — double-click to reset',16,28); return; }

  /* Price fits the VISIBLE bars, so zooming in on a quiet stretch actually
     magnifies it instead of keeping the whole session's range. */
  let blo=Math.min(...vis.map(b=>b.low)), bhi=Math.max(...vis.map(b=>b.high));

  /* THE BARS ARE THE SUBJECT. A level may widen the axis only while it stays
     context -- admit it if it does not blow the range past 3x what price
     actually did. Without this one far-off strike squashes a trend day's tape
     into a flat line, and a trend day is when you most need to read it.
     Anything rejected still gets an edge marker, so an off-screen level is a
     known absence rather than a silent one. */
  const barSpan=Math.max(bhi-blo,1), MAXEXP=3.0, off_=[];
  const widen=v=>{
    const nlo=Math.min(blo,v), nhi=Math.max(bhi,v);
    if((nhi-nlo)<=barSpan*MAXEXP){ blo=nlo; bhi=nhi; }
  };
  LEVELS.filter(v=>v.kind==='rail'||v.kind==='spot').forEach(v=>widen(v.price));
  if(TRACK && ON('t_setups')) TRACK.setups.forEach(s=>{ widen(s.entry); widen(s.stop); });

  const mid=(pc===null)?(blo+bhi)/2:pc;
  const half=(((bhi-blo)/2)||10)*1.08*pz;
  const lo=mid-half, hi=mid+half;
  const Y=p=>PAD.t+(hi-p)/(hi-lo)*ph;
  const X=i=>PAD.l+(i+0.5)*(pw/n);          // i is an index within the view
  const Xa=abs=>X(abs-view.i0);             // absolute bar index -> x

  // RTH shading: the tracker grades this window, so the chart says where it is.
  const rth=[];
  vis.forEach((b,k)=>{ const m=+b.ts.slice(11,13)*60 + +b.ts.slice(14,16);
    if(m>=810 && m<=1200) rth.push(off+k); });
  if(rth.length && rth.length<vis.length){
    x.fillStyle=css('--panel2'); x.globalAlpha=.55;
    x.fillRect(Xa(rth[0])-1,PAD.t,Xa(rth[rth.length-1])-Xa(rth[0])+2,ph); x.globalAlpha=1;
  }

  // grid + price axis
  x.strokeStyle=css('--rule'); x.lineWidth=1; x.font='10px "IBM Plex Mono",monospace';
  x.fillStyle=css('--ink3'); x.textBaseline='middle';
  const dec=Math.pow(10,Math.floor(Math.log10(Math.max((hi-lo)/5,1e-6))));
  const step=dec*([1,2,5,10].find(m=>(hi-lo)/(dec*m)<=6)||1);
  for(let p=Math.ceil(lo/step)*step;p<hi;p+=step){
    const y=Y(p); x.beginPath(); x.moveTo(PAD.l,y); x.lineTo(W-PAD.r,y); x.stroke();
    x.textAlign='left'; x.fillText(fmt0(p), W-PAD.r+6, y);
  }
  // time axis
  x.textAlign='center'; x.textBaseline='top';
  const nlab=Math.max(3,Math.min(8,Math.floor(pw/100)));
  for(let k=0;k<vis.length;k+=Math.max(1,Math.floor(vis.length/nlab))){
    x.fillText(vis[k].ts.slice(11,16),
      Math.min(W-PAD.r-16,Math.max(PAD.l+16,Xa(off+k))), H-PAD.b+5);
  }

  /* ---- gamma, profile or ticks. Both go through Y(price), so they stay on
     their strikes at every zoom level. ---------------------------------- */
  const nodes=LEVELS.filter(v=>v.kind==='pos'||v.kind==='neg');
  if(nodes.length && (ON('t_profile')||ON('t_nodes'))){
    const mx=Math.max(...nodes.map(v=>v.weight||0))||1, R=W-PAD.r-2;
    x.textBaseline='middle';
    nodes.forEach(v=>{
      const y=Y(v.price); if(y<PAD.t||y>H-PAD.b) return;
      x.fillStyle=v.kind==='pos'?css('--pos'):css('--neg');
      if(ON('t_profile')){
        const w=Math.max(3,(v.weight/mx)*PROFW);
        x.globalAlpha=.30; x.fillRect(R-w,y-2.5,w,5);
        if(v.weight>=mx*0.45){ x.globalAlpha=.85; x.fillStyle=css('--ink3');
          x.textAlign='right'; x.fillText(v.label,R-w-5,y); }
      }else{
        const w=Math.max(4,(v.weight/mx)*16);
        x.globalAlpha=Math.min(.85,.20+(v.weight/mx)*.65);
        x.fillRect(R-w,y-1.5,w,3);
      }
    });
    x.globalAlpha=1;
  }

  /* ---- plan levels ---------------------------------------------------- */
  const used=[];
  LEVELS.filter(v=>(v.kind==='flip'&&ON('t_flips'))||(v.kind==='rail'&&ON('t_rails'))
                 ||v.kind==='spot')
        .sort((a,b)=>a.price-b.price).forEach(v=>{
    const y=Y(v.price);
    if(y<PAD.t||y>H-PAD.b){ off_.push({v,above:y<PAD.t}); return; }
    const col=v.kind==='flip'?css('--warn'):v.kind==='rail'?css('--ink3'):css('--brass');
    x.strokeStyle=col; x.setLineDash(v.kind==='flip'?[5,4]:v.kind==='rail'?[1,3]:[]);
    x.lineWidth=1; x.beginPath(); x.moveTo(PAD.l,y); x.lineTo(W-PAD.r,y); x.stroke();
    x.setLineDash([]);
    let ly=y-3; while(used.some(u=>Math.abs(u-ly)<11)) ly-=11;
    used.push(ly);
    x.font='10px "IBM Plex Mono",monospace';
    x.fillStyle=col; x.textAlign='left'; x.textBaseline='bottom';
    x.fillText(v.label+'  '+fmt0(v.price),PAD.l+4,ly);
  });

  /* ---- setup levels. Dormant setups fade; only live ones are labelled,
     because four setups times five levels is twenty labels in one margin. */
  if(TRACK && ON('t_setups')){
    const usedR=[], rx = ON('t_profile') ? W-PAD.r-PROFW-10 : W-PAD.r-5;
    const rows=[];
    TRACK.setups.forEach(s=>{
      const live=['in play','tp1 hit','tp2 hit','tp3 hit','stopped','missed'].includes(s.state);
      const col=stateColour(s.state), a=live?.95:.30;
      rows.push({p:s.entry,dash:[],txt:`${s.id} entry`,col,a,live});
      rows.push({p:s.stop,dash:[4,3],txt:`${s.id} stop`,col,a,live});
      s.tps.forEach((tp,i)=>rows.push({p:tp,dash:[2,4],txt:`${s.id} TP${i+1}`,col,a,live}));
    });
    rows.sort((u,v)=>(u.live?1:0)-(v.live?1:0));
    rows.forEach(r=>{
      const y=Y(r.p); if(y<PAD.t||y>H-PAD.b) return;
      x.globalAlpha=r.a; x.strokeStyle=r.col; x.setLineDash(r.dash); x.lineWidth=r.live?1.4:1;
      x.beginPath(); x.moveTo(PAD.l,y); x.lineTo(W-PAD.r,y); x.stroke(); x.setLineDash([]);
      x.globalAlpha=1;
    });
    rows.filter(r=>r.live).sort((u,v)=>v.p-u.p).forEach(r=>{
      const y=Y(r.p); if(y<PAD.t||y>H-PAD.b) return;
      let ly=y, guard=0;
      while(usedR.some(u=>Math.abs(u-ly)<10.5) && guard++<40) ly+=10.5;
      if(ly<=PAD.t||ly>=H-PAD.b) return;
      usedR.push(ly);
      x.font='600 9.5px "IBM Plex Mono",monospace';
      x.fillStyle=r.col; x.textAlign='right'; x.textBaseline='middle';
      x.fillText(r.txt,rx,ly);
    });
  }

  /* ---- candles --------------------------------------------------------
     Body width follows the zoom. Below about two pixels a body is a line and
     the wick is the whole bar, which is the honest way to draw a session you
     have zoomed out of rather than pretending to a precision the pixels do
     not have. */
  const cw=Math.max(1,(pw/n)*0.66);
  vis.forEach((b,k)=>{
    const i=off+k, up=b.close>=b.open, col=up?css('--pos'):css('--neg');
    const xi=Xa(i);
    x.strokeStyle=col; x.fillStyle=col;
    x.beginPath(); x.moveTo(xi,Y(b.high)); x.lineTo(xi,Y(b.low)); x.stroke();
    const y0=Y(Math.max(b.open,b.close)), y1=Y(Math.min(b.open,b.close));
    if(cw<2) { x.beginPath(); x.moveTo(xi,y0); x.lineTo(xi,Math.max(y1,y0+1)); x.stroke(); }
    else x.fillRect(xi-cw/2, y0, cw, Math.max(1,y1-y0));
  });

  /* ---- event markers, on the bar and at the level it happened at ------- */
  if(TRACK && ON('t_events')){
    (TRACK.events||[]).forEach(e=>{
      const i=IDX[e.ts]; if(i===undefined || i<view.i0 || i>view.i1) return;
      const y=Y(e.price); if(y<PAD.t||y>H-PAD.b) return;
      const col=e.kind==='tp'?css('--pos'):e.kind==='stop'?css('--neg')
               :e.kind==='missed'?css('--vio'):css('--brass');
      x.beginPath(); x.arc(Xa(i),y,e.kind==='trigger'?4.5:3.5,0,7);
      x.fillStyle=col; x.fill();
      x.strokeStyle=css('--panel'); x.lineWidth=1.5; x.stroke();
    });
  }

  /* ---- last price ------------------------------------------------------ */
  const lp=BARS[BARS.length-1].close, ly2=Y(lp);
  if(ly2>PAD.t&&ly2<H-PAD.b){
    x.strokeStyle=css('--ink'); x.globalAlpha=.5; x.setLineDash([2,3]); x.lineWidth=1;
    x.beginPath(); x.moveTo(PAD.l,ly2); x.lineTo(W-PAD.r,ly2); x.stroke();
    x.setLineDash([]); x.globalAlpha=1;
    const lab=lp.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
    x.font='10px "IBM Plex Mono",monospace';
    const tw=x.measureText(lab).width;
    x.fillStyle=css('--ink'); x.fillRect(W-PAD.r+2,ly2-8,tw+8,16);
    x.fillStyle=css('--panel'); x.textAlign='left'; x.textBaseline='middle';
    x.fillText(lab,W-PAD.r+6,ly2);
  }

  /* ---- levels the axis could not reach --------------------------------- */
  if(off_.length){
    x.font='9px "IBM Plex Mono",monospace'; x.globalAlpha=.8;
    const seen=[];
    off_.sort((p,q)=>Math.abs(p.v.price-(p.above?hi:lo))-Math.abs(q.v.price-(q.above?hi:lo)))
        .forEach(o=>{
      const side=o.above?'above':'below';
      if(seen.filter(t=>t===side).length>=2) return;
      seen.push(side);
      const yy=o.above?PAD.t+9+seen.filter(t=>t==='above').length*12
                      :H-PAD.b-4-seen.filter(t=>t==='below').length*12;
      x.fillStyle=css('--ink3'); x.textAlign='right'; x.textBaseline='middle';
      x.fillText(`${o.above?'▲':'▼'} ${o.v.label} ${fmt0(o.v.price)} `
                +`${Math.round(Math.abs(o.v.price-(o.above?hi:lo)))} pts ${side}`,W-PAD.r-4,yy);
    });
    x.globalAlpha=1;
  }

  /* ---- crosshair ------------------------------------------------------- */
  const habs = hover===null ? null : view.i0+hover;
  if(habs!==null && habs>=0 && habs<BARS.length){
    const b=BARS[habs];
    x.strokeStyle=css('--ink3'); x.setLineDash([3,3]); x.lineWidth=1;
    x.beginPath(); x.moveTo(Xa(habs),PAD.t); x.lineTo(Xa(habs),H-PAD.b); x.stroke();
    x.setLineDash([]);
    const d=b.close-b.open;
    $('cread').textContent=`${b.ts.slice(11,16)}Z   O ${b.open.toFixed(2)}   `
      +`H ${b.high.toFixed(2)}   L ${b.low.toFixed(2)}   C ${b.close.toFixed(2)}`
      +`   ${d>=0?'+':''}${d.toFixed(2)}`;
  }
}

function bindChart(){
  const cv=$('px');

  cv.onwheel=e=>{
    e.preventDefault();
    const r=cv.getBoundingClientRect(), overAxis=(e.clientX-r.left) > r.width-PAD.r;
    if(e.shiftKey || overAxis){                    // vertical: price only
      pz=Math.max(0.08, Math.min(6, pz*(e.deltaY>0?1.12:0.89)));
    }else{                                         // horizontal: time only
      const m=Math.round((view.i1-view.i0)*(e.deltaY>0?1.15:0.87));
      const c=(view.i0+view.i1)/2;
      const halfw=Math.max(10, Math.min(BARS.length+RPAD, m))/2;
      view.i0=Math.max(-RPAD, Math.round(c-halfw));
      view.i1=Math.min(BARS.length+RPAD, Math.round(c+halfw));
      atEnd = view.i1 >= BARS.length;
    }
    draw();
  };

  cv.ondblclick=()=>{ fitView(); draw(); };

  cv.onmousedown=e=>{
    const r=cv.getBoundingClientRect();
    // Dragging ON the price axis stretches price, the way a trading chart does.
    const onAxis=(e.clientX-r.left) > r.width-PAD.r;
    drag={x:e.clientX,y:e.clientY,i0:view.i0,i1:view.i1,pc:pc,pz:pz,
          mode: onAxis?'scale' : (e.shiftKey?'pan-price':'pan-time')};
  };
  window.addEventListener('mouseup',()=>{ drag=null; });

  cv.onmousemove=e=>{
    const r=cv.getBoundingClientRect(), pw=r.width-PAD.l-PAD.r;
    if(drag && drag.mode==='scale'){
      const dy=(e.clientY-drag.y)/Math.max(60,r.height-PAD.t-PAD.b);
      pz=Math.max(0.05, Math.min(8, drag.pz*(1+dy*2.2)));
    }else if(drag && drag.mode==='pan-price'){
      const vis=BARS.slice(Math.max(0,view.i0),Math.min(BARS.length,view.i1));
      if(vis.length){
        const blo=Math.min(...vis.map(b=>b.low)), bhi=Math.max(...vis.map(b=>b.high));
        const span=(((bhi-blo)/2)||10)*1.08*pz*2, ph=r.height-PAD.t-PAD.b;
        pc=(drag.pc===null?(blo+bhi)/2:drag.pc)+((e.clientY-drag.y)/ph)*span;
      }
    }else if(drag && drag.mode==='pan-time'){
      const n=view.i1-view.i0;
      const sh=Math.round((drag.x-e.clientX)/(pw/n));
      let i0=drag.i0+sh, i1=drag.i1+sh;
      const LOW=-RPAD, HIGH=BARS.length+RPAD;
      if(i0<LOW){ i1-=i0-LOW; i0=LOW; }
      if(i1>HIGH){ i0-=i1-HIGH; i1=HIGH; }
      view.i0=i0; view.i1=i1;
      atEnd = view.i1 >= BARS.length;
    }
    hover=Math.floor((e.clientX-r.left-PAD.l)/(pw/Math.max(view.i1-view.i0,1)));
    draw();
  };

  cv.onmouseleave=()=>{ hover=null; $('cread').textContent=''; draw(); };
  cv.addEventListener('mousemove',e=>{
    const r=cv.getBoundingClientRect();
    cv.style.cursor=(e.clientX-r.left) > r.width-PAD.r ? 'ns-resize'
                   : (drag?'grabbing':'crosshair'); });

  new ResizeObserver(draw).observe($('cwrap'));
  ['t_profile','t_nodes','t_flips','t_rails','t_setups','t_events'].forEach(id=>{
    const el=$(id); if(!el) return;
    try{ const v=localStorage.getItem('teddy.'+id); if(v!==null) el.checked = v==='1'; }catch(e){}
    el.addEventListener('change',()=>{
      try{ localStorage.setItem('teddy.'+id, el.checked?'1':'0'); }catch(e){}
      draw(); });
  });
  const rs=$('chartreset'); if(rs) rs.onclick=()=>{ fitView(); draw(); };
}

/* ---------------------------------------------------------------- panels */
function renderBrief(){
  if(!TRACK) return;
  $('reading').innerHTML=(TRACK.reading||[]).map(r=>`<li>${r}</li>`).join('')
    || '<li class="muted">no "Reading the day" section in this sheet</li>';
  $('briefstamp').textContent=[TRACK.date && ('for '+TRACK.date),
      TRACK.settle && ('book '+TRACK.settle),
      TRACK.oi_vintage==='preliminary' ? 'preliminary OI' : ''].filter(Boolean).join('  ·  ');
  $('gameplan').innerHTML=TRACK.setups.map(s=>`
    <div class="gc${s.priority==='primary'?' pri':''}">
      <h4><span class="sid">${s.id}</span><span>${s.name}</span>
        <span class="st ${stateClass(s.state)}" style="margin-left:auto">${s.state}</span></h4>
      <div class="tg">${s.trigger||''}</div>
      <div class="rows">
        <div class="k">Entry</div><div class="v">${fmt0(s.entry)}</div>
        <div class="k">Stop</div><div class="v">${fmt0(s.stop)} · ${fmt0(s.risk)} pts</div>
        ${s.tps.map((t,i)=>`<div class="k">TP${i+1}</div><div class="v">${fmt0(t)}</div>`).join('')}
      </div>
    </div>`).join('');
}

function renderBoard(){
  if(!TRACK) return;
  $('board').innerHTML=TRACK.setups.map(s=>`
    <div class="sr">
      <div class="sid">${s.id}</div>
      <div class="nm"><div class="n">${s.name}</div>
        <div class="d">${s.detail||''}${s.mfe!==null&&s.mfe!==undefined
          ? `<br>ran ${s.mfe>0?'+':''}${fmt0(s.mfe)} for / ${fmt0(s.mae)} against`
            +(s.r_mfe?` · ${s.r_mfe}R best`:'') : ''}</div></div>
      <div class="st ${stateClass(s.state)}">${s.state}</div>
    </div>`).join('');
  const w=TRACK.window;
  $('trackstamp').textContent=(w?`${w[0]}–${w[1]} UTC · `:'')
    +`${TRACK.bars} bars · ${TRACK.fingerprint}`;
}

function renderLog(){
  if(!TRACK) return;
  const ev=TRACK.events||[];
  $('logcount').textContent=ev.length?`${ev.length} events`:'';
  $('elog').innerHTML=ev.length ? ev.map(e=>`
    <div class="ev ${e.kind}"><div class="t">${e.at}</div><div class="dot"></div>
      <div>${e.text}</div></div>`).join('')
    : '<div class="muted" style="padding:10px 14px">nothing has happened against the plan yet</div>';
}

/* ------------------------------------------------------------------ load */
/* Incremental by default. A full session is ~1,400 bars, and re-sending all of
   them several times a minute to redraw the last one is the kind of waste that
   caps how fast the refresh can go. `since` asks only for what we do not hold;
   the server resends the bar AT `since` because the last one held is usually
   still forming, so it is replaced rather than appended. */
let CHART_DAY=null;
async function loadChart(force){
  const day=(TRACK && TRACK.date) || $('rdate').value || todayISO();
  const fresh = force || day!==CHART_DAY || !BARS.length;
  try{
    const url='/api/bars?date='+day+(fresh?'':'&since='+LASTBAR);
    const b=await (await fetch(url)).json();
    if(b.ok){
      if(fresh){
        BARS=b.bars||[];
        IDX={}; BARS.forEach((r,i)=>{ IDX[r.ts]=i; });
        fitView();
      }else{
        const add=b.bars||[];
        if(add.length && BARS.length && add[0].time===BARS[BARS.length-1].time){
          BARS[BARS.length-1]=add.shift();          // the forming minute, updated
        }
        const grew=add.length;
        for(const r of add){ IDX[r.ts]=BARS.length; BARS.push(r); }
        /* Follow the tape only if you were already at the right-hand edge. Panning
           back to look at the open and being yanked forward a minute later is the
           single most irritating thing a live chart can do. */
        if(grew && atEnd){ view.i0+=grew; view.i1+=grew; }
      }
      CHART_DAY=day;
      BARN=b.total||BARS.length; STALE_ERR=b.stale_error||null;
      if(BARS.length) LASTBAR=BARS[BARS.length-1].time;
      const f=b.feed;
      $('livebadge').hidden=!f;
      if(f) $('livebadge').textContent='LIVE '+(f.schema||'');
      $('chartstat').innerHTML=`${BARN.toLocaleString()} × 1m`
        + (BARS.length?`  ·  ${BARS[0].ts.slice(11,16)}–${BARS[BARS.length-1].ts.slice(11,16)} UTC`:'')
        + (f?`  ·  feed ${f.age_s}s ago`:'')
        + (STALE_ERR?`  ·  <b style="color:var(--neg)">NOT UPDATING — ${String(STALE_ERR).slice(0,70)}</b>`:'');
    }else{ $('chartstat').textContent=b.log||'no bars'; BARS=[]; }
  }catch(e){ $('chartstat').textContent=String(e.message||e); }
  draw();
}

async function loadLevels(){
  try{ LASTL=await (await fetch(levelsURL())).json(); LEVELS=LASTL.levels||[]; }
  catch(e){ LEVELS=[]; }
  drawContext(); draw();
}

async function loadTrack(){
  try{
    const d=await (await fetch('/api/track')).json();
    if(!d.ok){ $('board').innerHTML=`<div class="muted" style="padding:12px 14px">${d.log||'tracking unavailable'}</div>`;
      TRACK=null; return; }
    TRACK=d; renderBrief(); renderBoard(); renderLog();
  }catch(e){
    $('board').innerHTML=`<div class="muted" style="padding:12px 14px">${e.message||e}</div>`;
  }
}

async function refreshAll(){ await loadTrack(); await loadChart(true); await loadLevels(); }
$('chartreload').onclick=refreshAll;

/* Collapsible sections. The brief and the ladder are reference: open when you
   sit down, in the way once you know the day. */
function collapser(boxId,btnId,bodySel,key){
  const btn=$(btnId), body=$(bodySel);
  if(!btn||!body) return;
  const set=v=>{ body.hidden=!v; btn.textContent=v?'hide':'show';
    try{ localStorage.setItem(key,v?'1':'0'); }catch(e){} };
  let init=true;
  try{ const v=localStorage.getItem(key); if(v!==null) init = v==='1'; }catch(e){}
  set(init);
  btn.onclick=()=>set(body.hidden);
}
collapser('brief','brieftoggle','bbody','teddy.brief');
collapser('ladbox','ladtoggle','ladframe','teddy.ladder');

function loadLadder(){ $('ladframe').src='/ladder?t='+Date.now(); }

bindChart();

/* The context line under Generate — the numbers the old chart header carried. */
function drawContext(){
  const L=LASTL||{}, sv=L.session||{}, bits=[];
  if(BARN) bits.push(BARN.toLocaleString()+' × 1m bars cached');
  if(L.iv) bits.push(`ATM IV <b style="color:var(--ink2)">${(L.iv*100).toFixed(2)}%</b>`);
  if(L.em){
    const tag=sv.id&&sv.id!=='full'?sv.label+' ':'';
    bits.push(`${tag}band ±${fmt0(L.em)}`
      +(sv.rail_scale&&sv.rail_scale<1
        ? ` <span style="color:var(--ink3)">(${sv.rail_scale}× the ${fmt0(L.em_full||0)} full-day EM)</span>`:''));
  }
  if(sv.hours) bits.push(`${sv.hours}h to expiry`);
  else if(L.hours) bits.push(`${L.hours}h to expiry`);
  if(STALE_ERR) bits.push(`<b style="color:var(--neg)">bars NOT updating — ${String(STALE_ERR).slice(0,80)}</b>`);
  if($('ctx')) $('ctx').innerHTML=bits.length?bits.join('  ·  '):'generate a report for the plan context';
}

/* Generate and Analyze both top the cache up first; this is that entry point.
   Without a chart being watched, nothing else refreshes it as a side effect. */
async function refreshBars(){ await loadChart(true); }

/* Tick while the tab is visible and the session has not expired. The feeder,
   when it is running, is the thing actually keeping the cache current -- this
   only re-reads it. */
function inSession(){
  const d=new Date(); return (d.getUTCHours()+d.getUTCMinutes()/60) < 20.0;
}
/* Two cadences, because the two things cost different amounts.

   BARS are incremental and tiny -- a handful of new rows -- so they can run
   fast. TRACKING re-walks every bar against every setup and is only interesting
   when a level is actually crossed, which cannot happen more than once per bar,
   so it runs slower. Splitting them is what lets the price update every few
   seconds without re-running the state machine at the same rate.

   Both are gated on the tab being visible. That is deliberate and it is not what
   keeps the data flowing: live_feed.py is a separate process writing the cache
   whether or not this page -- or the browser -- is open. The page is a reader.
   Browsers throttle background timers to about once a minute anyway, so the
   visibility handler is what actually closes the gap: on coming back it catches
   up immediately, however long you were away. */
const FAST_MS = 5000, SLOW_MS = 20000;
let slowDue = 0;

async function tick(){
  if(document.visibilityState!=='visible' || !inSession()) return;
  await loadChart(false);                       // cheap: only what is new
  if(Date.now() >= slowDue){
    slowDue = Date.now() + SLOW_MS;
    await loadTrack();                          // parse is memoised server-side
    draw();
  }
}
setInterval(tick, FAST_MS);
document.addEventListener('visibilitychange',()=>{
  if(document.visibilityState==='visible'){ slowDue=0; tick(); } });
window.addEventListener('focus',()=>{ slowDue=0; tick(); });

/* ---------- report ---------- */
/* ?embed=1 serves the sheet with a fit-to-panel override; the cache-buster is
   what makes a regenerated report actually appear -- the URL is otherwise
   identical and the iframe just redraws the copy it already has. */
function showSheet(name){
  $('frame').src = '/sheets/'+name+'?embed=1&t='+Date.now();
  $('openfull').href = '/sheets/'+name;
}
$('go').onclick=async()=>{
  await refreshState();                 // never generate against a stale clock
  // and never against a stale tape: mid-session the plan anchors on the last
  // cached print, so top the cache up first rather than trusting the last tick.
  await refreshBars();
  $('msg').textContent='running…'; $('go').disabled=true;
  const d=await (await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({date:$('date').value, report_date:$('rdate').value||null,
      source:'databento', session:sessionId(), hours:$('hrs').value||null})})).json();
  $('log').textContent=d.log||'no output';
  if(d.ok&&d.sheet){ $('noreport').style.display='none';
    showSheet(d.sheet);
    $('msg').textContent='done'; } else { $('msg').textContent=d.ok?'done':'failed — see output'; }
  $('go').disabled=false;
  // A rebuild rewrites trinity.html, so the setups, the reading, the ladder and
  // the band all move together. Reload the lot rather than any one of them.
  refreshAll(); loadLadder();
};

/* ---------- analyse the session against the plan ---------- */
function fmt(n,d=0){return Number(n).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});}
function renderAnalysis(a){
  $('acard').style.display='block';
  $('astamp').textContent=a.created.replace('T',' ')+'  ·  '+a.session.first+'–'+a.session.latest;
  const s=a.session, p=a.plan;
  const railHi = s.high>p.rail_hi, railLo = s.low<p.rail_lo;
  const flips=a.flips.map(f=>`<tr><td>${f.name}</td><td class="mono">${fmt(f.price)}</td>
      <td>${f.side_now}</td><td class="mono">${f.above_min}m / ${f.below_min}m</td></tr>`).join('');
  const nodes=a.nodes.slice(0,14).map(n=>`<tr><td class="mono">${fmt(n.price)}</td>
      <td class="mono" style="color:${n.gamma>0?css('--pos'):css('--neg')}">${n.gamma>0?'+':''}${n.gamma}</td>
      <td>${n.touched?(n.touch_time+' from '+n.approached_from):'—'}</td>
      <td><b style="color:${n.outcome==='held'?css('--pos'):n.outcome==='broke'?css('--neg'):css('--ink3')}">
        ${n.outcome||(n.touched?'':'untouched')}</b></td></tr>`).join('');
  $('abody').innerHTML=`
    <div class="row" style="gap:22px;margin-bottom:14px">
      <div><label>Range</label><div class="mono" style="font-size:18px">${fmt(s.range)} pts</div>
        <div class="muted">${p.range_over_em}× the expected move</div></div>
      <div><label>Rails</label><div class="mono" style="font-size:18px">
        ${railHi||railLo?'BROKEN':'held'}</div>
        <div class="muted">hi ${fmt(p.hi_vs_rail,1)} · lo ${fmt(p.lo_vs_rail,1)} vs rail</div></div>
      <div><label>Last</label><div class="mono" style="font-size:18px">${fmt(s.last,2)}</div>
        <div class="muted">open ${fmt(s.open,2)}</div></div>
    </div>
    <table style="width:100%;font-size:12.5px;border-collapse:collapse">
      <tr><th style="text-align:left">Flip</th><th style="text-align:left">Level</th>
          <th style="text-align:left">Now</th><th style="text-align:left">Time above / below</th></tr>
      ${flips}
      <tr><td colspan="4" style="padding-top:10px"></td></tr>
      <tr><th style="text-align:left">Node</th><th style="text-align:left">Γ</th>
          <th style="text-align:left">First touch</th><th style="text-align:left">Outcome</th></tr>
      ${nodes}
    </table>
    <div class="muted" style="margin-top:12px">Saved to <span class="mono">data/analysis/</span>.
      Say <b>"analyse the latest session"</b> in chat and Claude re-ranks the setups from this.</div>`;
}
$('analyze').onclick=async()=>{
  // Analyze reads the SERVER's bar cache for the date, so top it up first --
  // without the chart there is no other reason for it to be current.
  $('analyze').disabled=true; $('astamp').textContent='analysing…';
  await refreshBars();
  const d=await (await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({date:$('rdate').value||$('date').value})})).json();
  $('analyze').disabled=false;
  if(!d.ok){ $('astamp').textContent=d.log||'failed'; return; }
  renderAnalysis(d.analysis); loadAnalyses();
};
async function loadAnalyses(){
  const d=await (await fetch('/api/analyses')).json();
  $('list').innerHTML=d.items.length?d.items.map(a=>`
    <div style="padding:8px 0;border-bottom:1px solid var(--rule);font-size:12.5px">
      <span class="mono muted">${a.created.replace('T',' ')}</span> ·
      range <b class="mono">${fmt(a.session.range)}</b> (${a.plan.range_over_em}× EM) ·
      ${a.nodes.filter(n=>n.outcome==='held').length} held /
      ${a.nodes.filter(n=>n.outcome==='broke').length} broke
      ${a.chart?` · <a href="/charts/${a.chart}" target="_blank">chart</a>`:''}
    </div>`).join(''):'none yet';
}

/* ---------- journal ---------- */
async function loadJournal(){
  const d=await (await fetch('/api/journal')).json();
  const el=$('thread'); el.textContent=d.text.trim()||'nothing yet.'; el.scrollTop=el.scrollHeight;
}
$('add').onclick=async()=>{
  const t=$('t').value.trim(); if(!t)return;
  $('add').disabled=true; $('m').textContent='saving…';
  await fetch('/api/journal',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:t})});
  $('add').disabled=false; $('m').textContent=''; $('t').value=''; loadJournal();
};
$('t').addEventListener('keydown',e=>{ if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)) $('add').click(); });

function autoHours(st){
  // Time to expiry is measured from the moment the plan DESCRIBES -- the start
  // of the session being traded, or now if we are already inside it -- not from
  // when the button is pressed. Generating a Friday-open plan on Thursday night
  // and entering 17h would move the near-book flip ~40 points.
  const sv = (st.sessions||{})[sessionId()];
  if(!sv){ $('hrs').value = st.hours_at_open; return; }
  $('hrs').value = sv.hours;
  $('hrs').title = `${sv.label} (${sv.window}) — ${sv.hours}h to the 20:00 UTC expiry`
                 + `; rails ×${sv.rail_scale}`;
}
async function refreshState(){
  ST = await state();
  autoHours(ST);
  $('lastpx').innerHTML = ST.last_price
    ? `last <b class="mono" style="color:var(--brass)">${Number(ST.last_price).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}</b>`
    : '';
  return ST;
}
let ST=null;
$('rdate').addEventListener('change',()=>{ if(ST) autoHours(ST); refreshLevels(); });
/* The session selector drives three things: the hours written into the form,
   the rail scale the SHEET is built with (sent on /api/run), and the band shown
   in the context line. `touched` is what stops the 60s state refresh from
   overwriting a session you picked by hand. */
$('sess').addEventListener('change', ()=>{
  $('sess').dataset.touched=1; if(ST) autoHours(ST); refreshLevels(); });
setInterval(refreshState, 60000);       // keep the clock honest all session
refreshState().then(s=>{ $('date').value = s.latest_settle || s.today;
  $('rdate').value=s.today;
  if(s.session_now && !$('sess').dataset.touched) $('sess').value=s.session_now;
  autoHours(s);
  // Tracking first: it settles which session the chart is drawn for, so the
  // chart and the board can never end up describing different days.
  refreshAll(); loadLadder();
  if(s.sheets&&s.sheets.length){ $('noreport').style.display='none';
    showSheet(s.sheets[0]); } });
loadAnalyses(); loadJournal();
</script>
"""


# ---------------------------------------------------------- command center ---
#
# command_center.py is a blueprint under /cc. It owns nothing here: the bars
# come from cc_bars (the same cache /api/bars serves), the report is read out
# of trinity.json and the rendered trinity.html, and the pipeline it runs is
# the same scripts the scheduler runs. Optional on purpose, like setup_track --
# a missing or broken command_center.py costs the /cc page, not the dashboard.
try:
    import command_center as _cc
    _cc.BARS_FN = cc_bars
    app.register_blueprint(_cc.cc)
    _CC_ERR = None
except Exception as _ex:                                   # noqa: BLE001
    _cc, _CC_ERR = None, f"{type(_ex).__name__}: {_ex}"


if __name__ == "__main__":
    for d in (DATA, SHEETS, CHARTS, REVIEWS, NOTES): d.mkdir(parents=True, exist_ok=True)
    if _CC_ERR:
        print(f"  command center: NOT LOADED -- {_CC_ERR}")
    print(f"\n  TeddyAI  ->  http://127.0.0.1:{PORT}")
    print(f"  Databento key: {'found' if os.environ.get('DATABENTO_API_KEY') else 'NOT SET (pulls will fail)'}\n")
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
