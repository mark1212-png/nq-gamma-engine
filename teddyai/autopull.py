#!/usr/bin/env python3
"""
autopull.py -- the unattended half of the TeddyAI workflow.

Runs on THIS machine, on Windows Task Scheduler, because the Databento API key
lives in this environment and never leaves it. Claude cannot run these pulls;
it picks up the files afterwards.

    python autopull.py --job intraday     # any time 06:30-13:00 PT, real TTE
    python autopull.py --job close        # 14:10 PT  Globex closed, day complete
    python autopull.py --job settle       # 16:00 PT  official settlement bulletin
    python autopull.py --job asia         # 17:15 PT  Asia plan on tonight's session
    python autopull.py --job prelim       # 22:15 PT  tonight preliminary OI book
    python autopull.py --job tomorrow     # 22:20 PT  TOMORROW's RTH plan, tonight
    python autopull.py --job oi-probe     #           when does preliminary OI land?
    python autopull.py --job premarket    # 06:00 PT  final OI -> build the sheet
    python autopull.py --job opra         # late slot equity chains for today's settle
    python autopull.py --job opra-live    # 03:30-17:00 PT  the PREVIOUS settle's equity
                                          #           books, open interest off the OPRA
                                          #           live gateway. The launcher starts
                                          #           this one; --job auto never does
    python autopull.py --job auto         #           pick by the clock

Every build is also copied into archive/<session>/ with the trinity.json it came
from, and a row is appended to archive/history.csv -- so day-to-day drift in the
flips, IV and net gamma is a spreadsheet rather than a folder of HTML.

Every job is safe to run twice, logs to notes/autopull.log, and writes
notes/autopull-status.json so the dashboard and Claude can both see what
happened without reading a log file.

LICENSING: these pulls go to Databento, a licensed CME redistributor, which
permits automated retrieval. CME's and QuikStrike's own sites do NOT -- the
QuikStrike PDF path stays manual on purpose. Do not automate it.

COST: statistics, definition and ohlcv are all L0 schemas and the Standard
subscription includes them, so a scheduled pull carries no marginal charge.
"""

import os, sys, re, json, subprocess, datetime, argparse, pathlib, urllib.request, time

# Windows consoles are cp1252. run() captures subprocess output with
# errors="replace", which MANUFACTURES U+FFFD for any byte that was not valid
# UTF-8 -- and printing that to a cp1252 stream raises UnicodeEncodeError and
# kills the job. This killed every `--job asia` from 2026-09-04 onward, always
# at the line right after gen.py: log() writes the file with encoding="utf-8"
# and would have been fine, but it PRINTS first and never reached the write.
# Fixed at the stream so every job is covered, not just the one that tripped
# it. 2026-09-08.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass       # not a TextIOWrapper (redirected, embedded); log() still guards

import nqvol                      # calendar (holidays, half-days, closes) -- 2026-09-05
UTC = datetime.timezone.utc

ROOT = pathlib.Path(__file__).parent.resolve()
DATA, NOTES = ROOT / "data", ROOT / "notes"
LOG, STATUS = NOTES / "autopull.log", NOTES / "autopull-status.json"
DASH = "http://127.0.0.1:5137"
NOTES.mkdir(exist_ok=True)

# THE CONTRACT THE BARS ARE, which after 2026-09-14 is not the front option
# book's underlying. live_feed.py is pinned to the traded contract through the
# quarterly roll (see the note there), so `--spot` is an NQZ6 price while
# trinity_db, left to itself, would reference the front book's NQU6 and produce
# one file with the price on one axis and every flip on the other.
#
# That is exactly the defect found in engine.py --from-trinity earlier the same
# day, reappearing one level up: fixing the inheritance was not enough while
# nothing here PASSED a reference in the first place. The 21:04 Asia build put
# a 29,453.75 NQZ6 print beside NQU6 flips at 29,396 / 29,340 / 29,294 and so
# read "above every flip" when the truth was below all three.
#
# The futures settle is looked up under the same symbol for the same reason --
# comparing an NQU6 settle against an NQZ6 print reported a +304.00 "unusually
# wide" divergence that was just the calendar basis.
try:
    from live_feed import SYMBOL as BAR_SYMBOL
except Exception:
    BAR_SYMBOL = "NQZ6"
REF_ARGS = ["--ref", BAR_SYMBOL] if BAR_SYMBOL and not BAR_SYMBOL.startswith("NQ.") else []

# The equity cross-reference. These roots must match command_center.OPRA_ROOTS --
# its 'opra' pipeline step runs opra_compare.py and SKIPS when a chain is absent,
# which is what this job exists to prevent.
OPRA_ROOTS = ("SPX", "SPXW", "QQQ", "SPY")
# The ceiling the free estimate must come in under before anything is downloaded.
# opra_pull.py --fetch applies the same gate itself (--max-mb, default 200); this
# is the scheduled copy of it, so a refusal is recorded in the status file rather
# than only in a log nobody reads.
#
# IT IS A PER-ROOT CEILING NOW (2026-09-10), because it never fitted a whole day.
# Measured on the complete 2026-09-08 session, all four roots together estimate
# 321.8 MB -- SPX and SPXW about 82 between them, QQQ 111.3, SPY 128.7 -- so a
# correctly timed four-root request was always going to be refused by a 200 MB
# gate. The single run that ever fired estimated 70.9 MB only because it ran at
# 14:20, before the day was complete in the dataset, and it failed for a
# different reason. job_opra fetches one root at a time, so the ceiling stays
# meaningful, the largest real request has ~35% headroom under it, and one bad
# root no longer costs the other three.
OPRA_MAX_MB = 200.0


def log(msg):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp}  {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # the stream refused a character. Never let that end the job -- the file
        # write below is the record that matters.
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def status(job, ok, detail):
    """One small file the dashboard and Claude can both read."""
    try:
        cur = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    except Exception:
        cur = {}
    cur[job] = dict(ok=ok, detail=detail,
                    at=datetime.datetime.now().isoformat(timespec="seconds"))
    STATUS.write_text(json.dumps(cur, indent=1), encoding="utf-8")


# THE STRUCTURAL GATE (2026-09-15). After build.py, precommentary.py checks the BUILD --
# the band against the clock, the reference against the newest bar -- and a non-zero
# exit stops the job before the sheet is written or archived, so a band describing a
# session other than the one on the clock never leaves the machine. It is deliberately
# run WITHOUT --cards here: the scheduled jobs build with the previous plan's prose by
# design and rely on a later rewrite, so the card checks belong where the cards are
# written (command_center's narrative checker), not on the stale ones.
GATE = ["precommentary.py"]

# THE RANKED PLAN (2026-09-16, Mark: "make sure your proposed gameplan is now pushed into
# the dashboard updating the cards"). Runs BEFORE build.py so the sheet can read it while
# it renders: precommentary writes notes/plan_cards.json -- the A trade and one alternate
# with the entry zone, the swing invalidation, three targets, the gate and the rank all
# computed. build.py checks the reference on that file and falls back to the CARDS block
# in its own source when it does not match, so a failure here is the old behaviour, not a
# stale plan on the page.
CARDS_STEP = ["precommentary.py", "--plan"]


def run(args, timeout=900):
    """Never raise. A scheduled task that throws is a scheduled task you stop trusting."""
    try:
        p = subprocess.run([sys.executable] + args, cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as ex:
        return 1, f"{type(ex).__name__}: {ex}"


def prev_business_day(d):
    """Previous CME session -- weekends AND holidays (nqvol.HOLIDAYS). The old
    weekday-only version handed the Tuesday-after-Labor-Day job a Monday that
    never traded, and built a 2026-09-07 sheet for a closed market."""
    return nqvol.prev_session(d)


def next_business_day(d):
    return nqvol.next_session(d)


def rth_hours(session):
    """Hours from the RTH open to the close for that session: 6.5, or 3.5 on a half-day."""
    return (nqvol.close_utc(session) - nqvol.rth_open_utc(session)).total_seconds() / 3600.0


def verify(tag, gate):
    """
    Run verify_pull.py against the trinity.json just written. It was never
    called from any job before 2026-09-05, so its three checks ran only when
    someone remembered to. `gate` makes a FAIL stop the build; otherwise it is
    logged and the build goes on (the reference check is warn-only by design).
    """
    rc, out = run(["verify_pull.py"])
    for line in out.strip().splitlines()[-4:]:
        log(f"{tag}: verify -- {line[:150]}")
    if rc != 0 and gate:
        log(f"{tag}: verify_pull FAILED -- not building on this book")
        return False
    return True


def chain_ok(path):
    """A real chain is ~1 MB. Anything tiny is a failed pull wearing a filename."""
    return path.exists() and path.stat().st_size > 500_000


def latest_chain(today, tag, fetch=True):
    """
    The newest COMPLETE option book at or before `today`, as (settle_date, path).

    "The previous business day" is not the same as "the previous SESSION".
    prev_business_day skips weekends but knows nothing about market holidays, so
    the Tuesday after Labor Day would ask CME for a Monday that never traded and
    get an empty book. Rather than carry a holiday calendar that rots, walk back
    until a settle actually yields a chain -- the same self-correcting pattern
    the open-interest window uses.

    SIDEWAYS BEFORE BACKWARD. When the FINAL book for a date has not published
    yet, stepping back a session is the expensive move: it costs a whole vintage
    of open interest, and that is not cosmetic -- measured 2026-09-03, changing
    only the book moved all three flips ~90 points and inverted the sign of net
    gamma in every one of them. CME's PRELIMINARY book for that same date is a
    session fresher than anything walking backward can reach, so try it before
    giving up on the date. It is stamped preliminary on the sheet (see
    chain_vintage) because it does get revised.

    On 2026-09-04 the absence of this cost a morning: the final for 09-03 had
    not published, so premarket walked back to the 09-02 book and built the
    session on a two-session-old vintage while the preliminary 09-03 file sat
    unread in data/prelim/.
    """
    cand = prev_business_day(today)
    for _ in range(3):
        c = DATA / f"{cand}-chain.json"
        if chain_ok(c):
            log(f"{tag}: using {c.name} ({c.stat().st_size/1e6:.2f} MB)")
            return cand, c
        tail = ""
        if fetch:
            rc, out = run(["databento_pull.py", "--fetch", "--date", cand.isoformat(),
                           "--window", "final", "--out", "data"])
            tail = out.strip().splitlines()[-1][:160] if out.strip() else ""
            if rc == 0 and chain_ok(c):
                log(f"{tag}: pulled {c.name} ({c.stat().st_size/1e6:.2f} MB)")
                return cand, c
        p = DATA / "prelim" / f"{cand}-chain.json"
        if chain_ok(p):
            log(f"{tag}: no FINAL book for {cand} yet -- using the PRELIMINARY "
                f"one for the same date ({p.stat().st_size/1e6:.2f} MB) rather "
                f"than stepping back a whole session. {tail}")
            return cand, p
        prev = prev_business_day(cand)
        log(f"{tag}: {cand} yielded no usable chain (holiday?) -- "
            f"stepping back to {prev}. {tail}")
        cand = prev
    return None, None


def chain_vintage(path):
    """
    'preliminary' when the book came out of data/prelim/, else 'final'.

    The sheet stamps this next to the settle date. Getting it wrong is worse
    than not showing it: a preliminary book labelled final reads as
    authoritative when it is still going to be revised.
    """
    return "preliminary" if path and pathlib.Path(path).parent.name == "prelim" else "final"


def _bars_for(day):
    """
    Bars for one UTC calendar day, dashboard first, then the cache on disk.

    /api/bars returns the bars it ALREADY HAD plus a stale_error when the
    incremental fetch fails, so a failed refresh looks exactly like a successful
    one that had nothing to add. Ignoring that field is how a plan came to be
    stamped with a four-hour-old "last print" and no warning anywhere.
    """
    # SAY WHICH SOURCE ANSWERED, always. Silence used to mean any of three
    # different things -- dashboard down, dashboard up but the fetch returned
    # nothing new, dashboard up and the fetch errored -- which need three
    # different fixes and looked identical in the log.
    rows = None
    if _dashboard_up():
        try:
            u = f"{DASH}/api/bars?date={day}"
            d = json.loads(urllib.request.urlopen(u, timeout=180).read())
            rows = d.get("bars") or []
            err = d.get("stale_error")
            newest = (rows[-1].get("ts") or rows[-1].get("time")) if rows else None
            if err:
                log(f"bars: {day} DASHBOARD UP but DID NOT REFRESH -- {str(err)[:200]}")
            else:
                log(f"bars: {day} from the dashboard -- {len(rows):,} bars, "
                    f"newest {newest}, no error reported")
        except Exception as ex:
            log(f"bars: {day} dashboard request FAILED -- {type(ex).__name__}: {ex}")
            rows = None
    else:
        log(f"bars: {day} -- teddy.py is NOT RUNNING on 127.0.0.1:5137, so the cache "
            f"cannot refresh; falling back to whatever is on disk")
    if not rows:
        f = DATA / "bars" / f"{day}.json"
        if f.exists():
            try:
                rows = json.loads(f.read_text(encoding="utf-8"))
                log(f"bars: {day} from disk -- {len(rows):,} bars")
            except Exception as ex:
                log(f"bars: could not read {f.name} -- {ex}")
        else:
            log(f"bars: {day} -- no cache file either")
    return rows or []


def _bar_time_and_close(r):
    """
    The two sources do not speak the same dialect. The cache on disk stores
    {ts: "2026-09-03 20:59:00+00:00", o/h/l/c}; /api/bars re-shapes it for the
    charting library into {time: <unix seconds>, open/high/low/close}. Reading
    the disk shape from the API response is a KeyError, and it only fires when
    the dashboard is RUNNING -- the configuration actually wanted.

    THIRD DIALECT, 2026-09-04: /api/bars now returns rows carrying BOTH -- ts
    AND time, close AND the rest -- so branching on "is ts present" took the
    disk path and then died on r["c"], which the API row does not have. The log
    read `unrecognised bar shape ['close','high','low','open','time','ts'] --
    'c'` and every job lost its reference; `intraday` refuses to build at all
    without one, so a running dashboard made the mid-session plan impossible.
    Dispatch per FIELD on the one actually being read, and accept any
    combination, rather than inferring the source and assuming the rest.
    """
    px = r.get("c", r.get("close"))
    if px is None:
        raise KeyError("no close field (looked for 'c' and 'close')")
    if "ts" in r:
        t = datetime.datetime.fromisoformat(str(r["ts"]).replace(" ", "T"))
    elif "time" in r:
        t = datetime.datetime.fromtimestamp(int(r["time"]), datetime.timezone.utc)
    else:
        raise KeyError("no timestamp field (looked for 'ts' and 'time')")
    return t, float(px)


def last_print(day=None):
    """
    The most recent traded price, as (price, utc_hhmm, age_minutes).

    THE BAR FILES ARE UTC CALENDAR DAYS; the jobs think in local PT. Those agree
    only until 17:00 PT. After that the live tape has rolled into the next UTC
    day, and asking for today's PT date returns the 16:59 PT bar -- which is how
    a plan built at 22:33 came to be anchored on 29,504.00 while the tape was
    29,611.50, a hundred and seven points away, with the rails centred there.

    So the default is not a date at all: take the newest bar across the current
    UTC day and the one before it, and let the timestamp decide. Pass `day` only
    to ask about a specific past session.
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    if day is not None:
        days = [str(day)]
    else:
        days = [now_utc.date().isoformat(),
                (now_utc.date() - datetime.timedelta(days=1)).isoformat()]

    best = None
    for d in days:
        for r in reversed(_bars_for(d)):
            try:
                t, px = _bar_time_and_close(r)
            except Exception as ex:
                log(f"bars: unrecognised bar shape {sorted(r)[:6]} -- {ex}")
                break
            if best is None or t > best[0]:
                best = (t, px)
            break                       # only the last bar of each day matters
    if best is None:
        return None, None, None
    t, px = best
    return px, f"{t:%H:%M}", (now_utc - t).total_seconds() / 60.0


# Databento's HISTORICAL feed is not a live tape. Its dataset end trails the
# clock by a variable margin -- measured 0 minutes at 05:09 UTC and 18 minutes
# at 13:58 UTC on 2026-09-04 -- and _clamp_end refuses to request past it. So a
# freshly refreshed cache is routinely ten to twenty-five minutes behind, and a
# threshold under that fires on every healthy run.
#
# Crying wolf is not a safe default here. A warning that appears on every build
# stops being read, and then the one that matters -- the four-hour-stale cache
# on 2026-09-03 -- goes past unnoticed too. So there are three bands, and only
# the third claims something is broken.
# The market-state layer (2026-09-10). Runs BETWEEN gen.py and build.py: it needs
# the trinity.json that trinity_db just wrote, and build.py's optional panel reads
# the state.json it writes, so both come out of one build.
#
# --soft because this step is OPTIONAL and the render loop below fails a whole job
# on any non-zero exit. A sheet without state.json is the sheet this project had
# before the layer existed; a missed premarket build is not recoverable.
STATE_STEP = ["engine.py", "--from-trinity", "--soft"]

FEED_LAG_NORMAL = 30.0     # at or under this, the feed is simply behind live
FEED_LAG_EDGE   = 60.0     # above this, the cache really is not advancing


def _price_age(tag, last, hhmm, age, warn_after=FEED_LAG_NORMAL):
    """
    Log the print's age and return the label the sheet will carry.

    A "last price" is only useful if it is actually last. Four hours old and
    unlabelled it is worse than showing nothing, because the whole point of
    adding it was to stop the sheet reasoning from the wrong price.
    """
    if last is None:
        log(f"{tag}: NO last print available -- the sheet will show the settle only")
        return ""
    if age is None:
        log(f"{tag}: last print {last:,.2f} at {hhmm} UTC (age unknown)")
        return f"{hhmm} UTC"
    log(f"{tag}: last print {last:,.2f} at {hhmm} UTC, {age:.0f} min old")
    if age <= warn_after:
        return f"{hhmm} UTC"
    if age <= FEED_LAG_EDGE:
        log(f"{tag}: the print is {age:.0f} min old -- at the edge of Databento's "
            f"normal historical lag, not necessarily a fault. Check your own tape "
            f"before leaning on any distance measured from it.")
        return f"{hhmm} UTC · {age:.0f}min behind"
    log(f"{tag}: WARNING -- the print is {age/60:.1f} HOURS old, well past the "
        f"feed's normal lag. The cache is NOT advancing. Confirm teddy.py is "
        f"running, look for a 'DID NOT REFRESH' line above, and re-run once it "
        f"moves. The sheet will label it stale.")
    return (f"{hhmm} UTC · {age/60:.1f}h stale" if age >= 90
            else f"{hhmm} UTC · {age:.0f}min stale")


# ------------------------------------------------------------------- jobs ---

def job_close(today):
    """
    14:05 PT. The cash close is 13:00 PT and the Globex day ends 14:00 PT, so by
    now the session is complete. Open interest is NOT out (that is a clearing
    figure, tomorrow morning) -- so this job only refreshes bars, which is what
    the session-vs-plan review needs.
    """
    if not _dashboard_up():
        log("close: dashboard not running, skipping bar refresh")
        return status("close", False, "dashboard down")
    try:
        u = f"{DASH}/api/bars?date={today}"
        d = json.loads(urllib.request.urlopen(u, timeout=180).read())
        n, err = len(d.get("bars") or []), d.get("stale_error")
        log(f"close: {n:,} bars for {today}" + (f"  STALE: {err[:90]}" if err else ""))
        status("close", not err, f"{n} bars" + (f"; {err[:120]}" if err else ""))
    except Exception as ex:
        log(f"close: bar refresh failed -- {type(ex).__name__}: {ex}")
        status("close", False, str(ex)[:200])


def job_settle(today):
    """
    16:00 PT = 23:00 UTC, the end of the settlement dissemination window.

    Settlement is STRUCK at 13:00 PT (VWAP of 12:59:30-13:00) and disseminated
    over the following three hours, so this is the first moment the official
    figure is reliably final. Open interest is skipped -- it will not clear
    until tomorrow morning.

    Why bother, when tomorrow's chain pull carries the settle anyway: it does
    not arrive until ~06:00 PT, which is after the Asia and overnight plans have
    already been built. Capturing it now gives those plans a verified anchor,
    and gives tomorrow's pull something to be checked against.

    It also measures a gap that caught us out: CME's official settle and the
    final Globex print are DIFFERENT numbers. QuikStrike reports the latter as
    "Future". On 2026-09-03 the official settle sat outside the 19:59 UTC bar
    entirely while the 20:59 print matched to the cent.
    """
    rc, out = run(["databento_pull.py", "--fetch", "--date", today.isoformat(),
                   "--window", "final", "--out", "data", "--settle-only"])
    path = DATA / f"{today}-settle.json"
    if rc != 0 or not path.exists():
        tail = out.strip().splitlines()[-1][:160] if out.strip() else ""
        log(f"settle: FAILED -- {tail}")
        return status("settle", False, f"pull failed: {tail}")

    try:
        official = json.loads(path.read_text(encoding="utf-8"))["futures_settle"].get(BAR_SYMBOL)
    except Exception as ex:
        log(f"settle: wrote {path.name} but could not read {BAR_SYMBOL} -- {ex}")
        return status("settle", False, f"unreadable: {ex}")

    detail = (f"{BAR_SYMBOL} official settle {official:,.2f}" if official
              else f"no {BAR_SYMBOL} settle")
    log(f"settle: {detail}")

    # Compare against the day's final print, which is what the Asia job anchors to.
    bars = DATA / "bars" / f"{today}.json"
    if official and bars.exists():
        try:
            rows = json.loads(bars.read_text(encoding="utf-8"))
            # The LAST bar of the UTC day at 23:00 UTC is an hour into the NEW
            # session (Globex reopens 22:00). Compare against the last bar at or
            # before the close instead -- the print the settle was struck from.
            cut = nqvol.close_utc(today)
            at_close = [r for r in rows
                        if datetime.datetime.fromisoformat(str(r["ts"]).replace(" ", "T")) <= cut]
            if at_close:
                last = at_close[-1]["c"]
                gap = last - official
                detail += f"; close print {last:,.2f} ({gap:+.2f})"
                log(f"settle: close print {last:,.2f} ({str(at_close[-1]['ts'])[11:16]} UTC), "
                    f"{gap:+.2f} vs official settle"
                    + ("  <-- unusually wide, check before trusting either"
                       if abs(gap) > 25 else ""))
        except Exception:
            pass
    status("settle", True, detail)


def job_asia(today):
    """
    17:15 PT = 00:15 UTC, fifteen minutes after Tokyo opens.

    Today's SETTLEMENT is published by now (struck 13:00 PT, disseminated
    13:00-16:00 PT) but today's OPEN INTEREST is not -- that is a clearing
    figure and lands tomorrow morning. So the Asia plan is a deliberate hybrid:
    the most recent COMPLETE book, repriced against TODAY's closing future.

    THE REFERENCE IS THE LAST PRINT, not the settle, from half an hour after the
    settlement instant onward -- which is every run this job will ever make on
    its 17:15 schedule. This docstring used to claim the opposite ("the official
    settle when the 16:00 job has captured it, the print only as a fallback")
    and was simply wrong about its own code; corrected 2026-09-08 after it sent
    a session looking for a difference between building at 15:00 and at 16:30.
    There is none: the settle is not the reference at either time.

    The rule, and why, is in the comment on the selection block below. Short
    version: the rails are drawn AROUND the reference, so the reference has to
    be where price is, and hours after the settle it is not -- 20.75 points off
    on 2026-09-03, 63.50 on 2026-09-04, against bands of 90 to 180. The settle
    still anchors the BOOK: every expiry's greeks come from its own settled
    future and the vol solved from it, none of which the reference touches. The
    settle only becomes the reference inside the first half hour, or when there
    is no print at all. Always say on the sheet which one was used.

    What must NOT happen is anchoring to the settle inside the older BOOK, which
    is a full session behind and would centre every rail hundreds of points off.

    HOURS AND RAILS COME FROM THE CLOCK, not from constants. This used to be a
    flat 20h and 0.503x however late it ran, so a rebuild at 21:42 produced a
    plan that described 17:00 -- 20 hours to an expiry that was 15 hours away,
    and a band covering an overnight that was most of the way over. Both are now
    measured from the moment the job actually runs.
    """
    session = next_business_day(today)          # tonight's Asia belongs to D+1

    # TONIGHT's preliminary book when the prelim job has landed one, otherwise
    # the newest complete final book. The preliminary is a whole session fresher
    # and the difference sits almost entirely in the front expiry -- +21% on
    # 2026-09-03 -- which is the book this plan leans on hardest.
    oi_label = "final"
    p = DATA / "prelim" / f"{today}-chain.json"
    if chain_ok(p):
        oi_label, settle, chain = "preliminary", today, p
        log(f"asia: using TONIGHT's preliminary book ({p.stat().st_size/1e6:.2f} MB)"
            f" -- subject to revision when final lands ~05:00 PT")
    else:
        settle, chain = latest_chain(today, "asia", fetch=False)
        # latest_chain can now hand back a PRELIMINARY book for an earlier date
        # when that date's final has not published, so the label has to come
        # from the file it actually returned. Assuming "final" here would stamp
        # a revisable book as authoritative -- worse than not stamping at all.
        oi_label = chain_vintage(chain)
    if chain is None:
        log("asia: no complete chain in the last 3 business days")
        return status("asia", False, "no complete chain")

    import session_scale
    now = datetime.datetime.now(UTC)
    # The settlement instant today and the close of the session being planned,
    # both from the calendar (16:00 ET; 13:00 on a half-day) -- not a hardcoded
    # 13:00 PT, and not clamped. The old min(hours, 20) silently priced a
    # Friday-evening book for Monday at 20h instead of ~68h (gamma x1.84).
    settle_at = nqvol.close_utc(today)
    expiry_at = nqvol.close_utc(session)
    hours = OPTS.get("hours")
    if hours is None:
        hours = (expiry_at - now).total_seconds() / 3600.0
    if hours <= 0:
        log(f"asia: {hours:.2f}h to the {session} close -- the session this plan would "
            f"describe is over (a --date backfill?). Pass --hours to force it.")
        return status("asia", False, "hours to expiry <= 0")
    since = (now - settle_at).total_seconds() / 3600.0
    if since < 0 and OPTS.get("rail_scale") is None:
        log(f"asia: it is {(-since):.1f}h BEFORE today's settle -- there is no overnight "
            f"to plan yet. Run after 13:00 PT, or pass --rail-scale.")
        return status("asia", False, "before the settle")
    # LONDON (2026-09-15). This branch used to clamp to the floor and log "this is
    # a Europe plan, not an Asia one" -- correctly diagnosing the problem and then
    # drawing the band anyway. session_scale.scale_london now measures that window
    # (236 sessions, fitted on the same yardstick as the other two curves), so a
    # plan built for or inside 07:00-13:30 UTC gets rails that describe it.
    #
    # Asked for explicitly with --job london BEFORE 07:00 UTC, the plan is built
    # FORWARD to the London open the way the asia job builds tonight's Asia plan
    # in the afternoon: anchored at 07:00 with the full 0.346 band, not at the
    # dying tail of Asia. Inside the window it is built from now.
    want_london = OPTS.get("london")
    utc_now = now.astimezone(datetime.timezone.utc)
    hour_utc = utc_now.hour + utc_now.minute / 60.0
    scale = OPTS.get("rail_scale")
    if scale is None and want_london:
        at = max(hour_utc, session_scale.LONDON_START_UTC)
        scale = session_scale.scale_london(at)
        if scale is None:                       # past the NY open: the RTH curve owns it
            scale = session_scale.scale_for_hours(max(session_scale.EXPIRY_UTC - hour_utc, 0.01))
            log("london: the NY open has passed -- this is an intraday plan; use --job intraday")
        elif hour_utc < session_scale.LONDON_START_UTC:
            log(f"london: {session_scale.LONDON_START_UTC - hour_utc:.1f}h before the 07:00 UTC "
                f"London open -- building the plan FORWARD to it, rails {scale:.3f}x")
    if scale is None:
        scale = session_scale.scale_overnight(since)
    if scale is None:                           # past the 07:00 UTC Asia close
        scale = session_scale.scale_london(hour_utc) or session_scale.FLOOR
        log("asia: the Asia session is over -- this is a London plan; "
            f"rails {scale:.3f}x from the measured Europe curve")
        want_london = True
    # At the scheduled 17:15 run the band still spans essentially the whole Asia
    # session; several hours later it is only the tail, and the label has to say
    # so or the sheet looks like the morning's plan with mysteriously tight rails.
    label = ("London" if want_london else
             "Asia" if since <= 4.5 else "Rest of overnight")
    log(f"asia: {hours:.2f}h to the {session} expiry, {since:.1f}h since the settle "
        f"-> rails {scale:.3f}x, '{label}'")

    settle_px, ref, ref_src = None, None, None
    settle_file = DATA / f"{today}-settle.json"
    if settle_file.exists():
        try:
            settle_px = json.loads(settle_file.read_text(encoding="utf-8")
                                   )["futures_settle"].get(BAR_SYMBOL)
        except Exception as ex:
            log(f"asia: could not read {settle_file.name} -- {ex}")

    # The rails are drawn around the reference, so the reference has to be where
    # price IS. Hours after the settle it is not: on 2026-09-03 the settle and
    # the evening print were 20.75 points apart against a band of ~90, a quarter
    # of the rail. The settle still anchors the BOOK -- the greeks use each
    # expiry's own settled future and the vol solved from it, neither of which
    # this touches -- so taking the live print costs nothing and centres the band
    # correctly. Say which one the sheet used.
    if OPTS.get("spot") is not None:
        ref, ref_src = OPTS["spot"], "given on the command line"
    else:
        px, hhmm, age = last_print()
        if px is not None and (settle_px is None or since > 0.5):
            ref, ref_src = px, f"last print {hhmm} UTC"
            if settle_px:
                log(f"asia: settle {settle_px:,.2f}, last print {px:,.2f} "
                    f"({px - settle_px:+.2f}) -- anchoring on the print")
        elif settle_px:
            ref, ref_src = settle_px, "official settle"
    if ref is None:
        log("asia: no reference for today -- run the settle job (or close) first")
        return status("asia", False, "no reference price")
    log(f"asia: reference {ref:,.2f} ({ref_src})")

    cmd = ["trinity_db.py", "--chain", str(chain), "--report-date", session.isoformat(),
           "--hours", f"{hours:.4f}", "--spot", f"{ref:.2f}",
           "--rail-scale", f"{scale:.4f}", "--rail-label", label,
           "--oi-vintage", oi_label] + REF_ARGS
    for step in (cmd, ["gen.py"], STATE_STEP, CARDS_STEP, ["build.py"], GATE):
        rc, out = run(step)
        if rc != 0:
            log(f"asia: {step[0]} FAILED -- {out.strip().splitlines()[-1][:160]}")
            return status("asia", False, f"{step[0]} failed")
        log(f"asia: {step[0]} ok -- {out.strip().splitlines()[-1][:140]}")
        if step[0] == "trinity_db.py":
            verify("asia", gate=False)

    sheets = ROOT / "sheets"; sheets.mkdir(exist_ok=True)
    # The PRIMARY overnight plan keeps the stable name -- there is one of those a
    # night and you want to open it without hunting. A later rebuild gets its own
    # timestamped file, the way intraday sheets do.
    #
    # The archive already did this; the sheet did not, so the comment here used to
    # claim "two rows rather than one clobbering the other" while the HTML clobbered
    # regardless. That matters from 2026-09-14, when a second evening build became
    # routine: preliminary open interest lands ~19:00 PT, so the 17:15 plan runs on
    # the last complete book and a ~19:45 rebuild runs on the session's own. Those
    # are two different maps -- on 09-14 structural confidence went 0.55 to 0.85
    # between them -- and keeping only the later one throws away the plan that was
    # actually live for the first half of the overnight.
    kind = "asia" if label == "Asia" else f"asia-{datetime.datetime.now():%H%M}"
    dest = sheets / (f"{session}-asia-teddyai.html" if kind == "asia"
                     else f"{session}-{kind}-teddyai.html")
    src = ROOT / "trinity.html"
    if src.exists():
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    rc, out = run(["archive.py", "--session", session.isoformat(), "--kind", kind,
                   "--sheet", str(dest)])
    log(f"asia: archive {'ok' if rc == 0 else 'FAILED'} -- "
        f"{out.strip().splitlines()[-1][:120] if out.strip() else ''}")
    log(f"asia: wrote {dest.name} -- book {settle}, reference {ref:,.2f}, "
        f"{hours:.2f}h, rails {scale:.3f}x, COMMENTARY NOT REWRITTEN")
    status("asia", True, f"{dest.name}; book {settle}, ref {ref:.2f} ({ref_src}), "
                         f"session {session}, {hours:.2f}h, rails {scale:.3f}x")


def job_intraday(today):
    """
    Any time inside 06:30-13:00 PT. A plan for the REST OF TODAY, not for a
    session that has not started.

    Two things change against the premarket build, and both matter:

      * TIME TO EXPIRY is the hours actually left to the 20:00 UTC expiry.
        This is where TTE bites hardest -- the 0DTE book is the one whose gamma
        is most sensitive to it, and we have measured the 0DTE flip moving about
        100 points across the plausible TTE range. A 6.5h front book at 11:00 AM
        is not a small error.
      * THE REFERENCE is the last traded price, not the settle, because the
        question is where price can go from HERE.

    and consequently the rails shrink -- by the measured variance profile in
    session_scale.py, NOT in proportion to the clock. See that file; the short
    version is that RTH variance is front-loaded enough that proportional time
    draws the rails about a third too wide by mid-morning.

    The BOOK is still yesterday's -- open interest is a clearing figure and
    today's does not exist yet. Same limitation the premarket sheet has, and it
    grows through the day for the front expiry.
    """
    import session_scale
    now = datetime.datetime.now()
    v = session_scale.intraday(now)
    hours = OPTS.get("hours")
    scale = OPTS.get("rail_scale")
    if v is None and hours is None:
        log(f"intraday: {now:%H:%M} PT is outside 06:30-13:00 -- nothing to "
            f"describe. Use premarket or asia, or pass --hours to force it.")
        return status("intraday", False, "outside the RTH session")
    if v is not None:
        hours = hours if hours is not None else v[0]
        scale = scale if scale is not None else v[1]
        log(f"intraday: {session_scale.describe(now)}")
    else:
        # Forced with --hours from outside the session (a rebuild after the
        # bell, a what-if). Still take the rail from the measured profile --
        # getting TTE right and the rails wrong is the wrong half of the fix.
        scale = scale if scale is not None else session_scale.scale_for_hours(hours)
        if scale is None:
            log(f"intraday: --hours {hours} reaches back past the 06:30 open, "
                f"which is an Asia or Europe plan. Pass --rail-scale, or use "
                f"--job asia.")
            return status("intraday", False, "hours predate the RTH open")
        log(f"intraday: forced -- {hours}h to expiry, rails {scale:.3f}x")

    settle, chain = latest_chain(today, "intraday")
    if chain is None:
        log("intraday: no usable chain in the last 3 business days")
        return status("intraday", False, "no usable chain")

    ref = OPTS.get("spot")
    if ref is None:
        ref, hhmm, age = last_print()
        if ref is None:
            log("intraday: no bars for today -- cannot anchor to the current price")
            return status("intraday", False, "no reference price")
        note = f"last print {ref:,.2f} at {hhmm} UTC"
        if age is not None:
            note += f", {age:.0f} min old"
            # Same three bands as _price_age. The old 10-minute threshold sat
            # UNDER Databento's normal lag, so it fired on every healthy
            # mid-session run and told the user to start a dashboard that was
            # already running.
            if age > FEED_LAG_EDGE:
                log(f"intraday: WARNING -- the reference is {age/60:.1f} HOURS "
                    f"old, past the feed's normal lag. The cache is NOT "
                    f"advancing; confirm teddy.py is running and re-run.")
            elif age > FEED_LAG_NORMAL:
                log(f"intraday: the reference is {age:.0f} min old -- at the edge "
                    f"of the feed's normal lag. Check your own tape before "
                    f"leaning on distances measured from it.")
        log(f"intraday: {note}")
    else:
        log(f"intraday: reference {ref:,.2f} (given on the command line)")

    if hours <= 0:
        log(f"intraday: {hours:.2f}h to expiry -- nothing left to plan")
        return status("intraday", False, "hours to expiry <= 0")
    for step in ([ "trinity_db.py", "--chain", str(chain),
                   "--report-date", today.isoformat(),
                   "--hours", f"{hours:.4f}", "--spot", f"{ref:.2f}",
                   "--rail-scale", f"{scale:.4f}",
                   "--oi-vintage", chain_vintage(chain), *REF_ARGS,
                   "--rail-label", "Rest of session"],
                 ["gen.py"], STATE_STEP, CARDS_STEP, ["build.py"], GATE):
        rc, out = run(step)
        if rc != 0:
            log(f"intraday: {step[0]} FAILED -- {out.strip().splitlines()[-1][:160]}")
            return status("intraday", False, f"{step[0]} failed")
        log(f"intraday: {step[0]} ok -- {out.strip().splitlines()[-1][:140]}")
        if step[0] == "trinity_db.py":
            verify("intraday", gate=False)

    # Timestamped, unlike the other sheets. There can be a dozen of these in a
    # day and the whole point is to compare them, so they must not clobber one
    # another or the morning's RTH sheet.
    stamp = now.strftime("%H%M")
    sheets = ROOT / "sheets"; sheets.mkdir(exist_ok=True)
    dest = sheets / f"{today}-intraday-{stamp}-teddyai.html"
    src = ROOT / "trinity.html"
    if src.exists():
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    rc, out = run(["archive.py", "--session", today.isoformat(),
                   "--kind", f"intraday-{stamp}", "--sheet", str(dest)])
    log(f"intraday: archive {'ok' if rc == 0 else 'FAILED'} -- "
        f"{out.strip().splitlines()[-1][:120] if out.strip() else ''}")
    log(f"intraday: wrote {dest.name} -- book {settle}, reference {ref:,.2f}, "
        f"{hours:.2f}h, rails {scale:.3f}x, COMMENTARY NOT REWRITTEN")
    status("intraday", True, f"{dest.name}; book {settle}, ref {ref:.2f}, "
                             f"{hours:.2f}h to expiry, rails {scale:.3f}x")


def job_prelim(today):
    """
    ~19:30 PT. CME publishes PRELIMINARY open interest in the evening, in the
    00:00-06:00 UTC window of the next day.

    WHEN, exactly -- measured 2026-09-14 by reading the ts_recv of the stat_type
    9 records instead of guessing at windows:

        09-03 Thu 18:34 PT   09-10 Thu 18:42   09-14 Mon 18:58
        09-08 Tue 19:11      09-09 Wed 19:16

    Five weekdays inside a 42-minute band, each one burst of ~800-1,700 records
    in under two seconds. This job ran at 22:15 for months -- three hours after
    the book was already sitting there -- while the Asia plan was built at 17:15,
    nearly two hours BEFORE it. So the plan never once used the session's own
    open interest, and the ordering, not the data, was the reason.

    Why nobody caught it: job_oi_probe is exactly the instrument for this and it
    last ran 2026-09-11, a FRIDAY, when the OI is not broadcast until the Sunday
    reopen. It found nothing in all four windows and concluded "no usable
    preliminary OI in the evening", which is true of Fridays and false of every
    other night. That verdict then justified the schedule it came from.

    Measured 2026-09-03: it is a real new vintage, not the previous book re-sent --

        the 09-03 expiry had gone to zero (it expired at that close; the 09-02
        book still carried 10,237 on it), the 09-04 expiry had grown 20,823 ->
        25,148 (+21%), and 22 of 26 shared expiries had moved.

    That +21% is on the expiry that is the front book overnight and the 0DTE
    book in the morning -- the one place the project has always been weakest,
    because open interest was a session behind and the front expiry accumulates
    right into the bell.

    It is PRELIMINARY and gets revised, so it lives in data/prelim/ under the
    same filename rather than in data/. Nothing that expects a final book can
    pick it up by accident, and the morning build still pulls the real one.
    """
    probe = DATA / "prelim"
    probe.mkdir(exist_ok=True)
    # The preliminary window is 00:00-06:00 UTC on the next CALENDAR day (--oi-day
    # is calendar days). On a FRIDAY that window is Saturday, and CME's feed is
    # dark from Friday 16:00 CT to Sunday 17:00 CT, so nothing is in it: Friday's
    # OI is not broadcast until the reopen. Try the reopen evening and the night
    # after it as well, and if Databento still has nothing, use the QuikStrike
    # export if one has been dropped in data/ (quikstrike_oi.py).
    import nqvol
    attempts = [(1, "0-6")]
    nxt = today + datetime.timedelta(days=1)
    while not nqvol.unit_weight(nxt):
        nxt += datetime.timedelta(days=1)
    if nxt != today + datetime.timedelta(days=1):
        k = (nxt - today).days - 1                 # the reopen evening (Sunday for a Friday)
        attempts += [(k, "20-24"), (k + 1, "0-6")]
        log(f"prelim: {today} is followed by a closure -- the feed reopens {nxt - datetime.timedelta(days=1)} "
            f"evening; trying the Saturday window, then the reopen")
    path = probe / f"{today}-chain.json"
    tail = ""
    for day, hours in attempts:
        rc, out = run(["databento_pull.py", "--fetch", "--date", today.isoformat(),
                       "--window", "final", "--out", str(probe),
                       "--oi-day", str(day), "--oi-hours", hours])
        tail = out.strip().splitlines()[-1][:160] if out.strip() else ""
        if rc == 0 and chain_ok(path):
            log(f"prelim: open interest found in the day+{day} {hours}h UTC window")
            break
        log(f"prelim: day+{day} {hours}h UTC -- {tail}")
    if not chain_ok(path):
        pdf = DATA / f"{today}-oi.pdf"
        stl = DATA / f"{today}-settle.json"
        if pdf.exists() and stl.exists():
            log(f"prelim: Databento has no OI for {today} yet; merging the QuikStrike export {pdf.name} "
                f"with {stl.name} instead (final OI, manual export)")
            rc, out = run(["quikstrike_oi.py", "--date", today.isoformat()])
            for line in out.strip().splitlines()[-4:]:
                log(f"prelim: {line[:150]}")
            final = DATA / f"{today}-chain.json"
            if rc == 0 and chain_ok(final):
                return status("prelim", True, f"data/{final.name} from QuikStrike (final OI)")
            return status("prelim", False, "quikstrike merge failed")
        log(f"prelim: no preliminary book yet for {today} -- {tail}"
            + (f". Drop QuikStrike's OI Matrix (Greek: None) in data/{today}-oi.pdf to build without it."
               if not pdf.exists() else ""))
        return status("prelim", False, f"not published yet: {tail}")

    # Prove it is a NEW vintage rather than a re-send, every time. A count alone
    # cannot tell the two apart, and the whole value of this job is the freshness.
    verdict = "unverified"
    prev, prev_chain = latest_chain(today, "prelim", fetch=False)
    try:
        def by_exp(p):
            ch = json.loads(pathlib.Path(p).read_text(encoding="utf-8"))["chain"]
            out_ = {}
            for key, strikes in ch.items():
                day = key.split("|")[0]
                out_[day] = out_.get(day, 0) + sum(
                    (r.get(cp) or {}).get("oi", 0) or 0
                    for r in strikes.values() for cp in ("C", "P"))
            return out_
        new = by_exp(path)
        if prev_chain:
            old = by_exp(prev_chain)
            shared = set(new) & set(old)
            moved = sum(1 for d in shared if new[d] != old[d])
            expired_out = old.get(today.isoformat(), 0) > 0 and not new.get(today.isoformat())
            verdict = (f"{moved}/{len(shared)} expiries moved vs the {prev} book"
                       + ("; today's expiry has cleared to zero" if expired_out else ""))
            if moved < len(shared) // 2:
                log(f"prelim: WARNING -- only {moved} of {len(shared)} expiries "
                    f"differ from the {prev} book. This may be a re-send, not a "
                    f"new vintage. Do not build on it without checking.")
                return status("prelim", False, f"looks like a re-send: {verdict}")
    except Exception as ex:
        log(f"prelim: could not compare against the previous book -- {ex}")

    log(f"prelim: {path.name} ({path.stat().st_size/1e6:.2f} MB) -- {verdict}")
    status("prelim", True, f"data/prelim/{path.name}; {verdict}")


def job_tomorrow(today):
    """
    ~22:15 PT, right after `prelim`. TOMORROW's RTH plan, built tonight.

    This is what preliminary open interest actually buys. Until it existed the
    evening could only produce an overnight plan, because the book for tomorrow
    did not yet exist -- and Mark does not trade the overnight, so that plan was
    really just an early look wearing the wrong name. Now the real thing can be
    built a night early.

    Identical in every convention to the 06:00 premarket job -- 6.5h from the
    06:30 open, rails 1.000x, anchored on today's settle, sheet named for the
    session it is FOR. The only difference is the VINTAGE of the book, which the
    sheet stamps, and which the morning run then revises.
    """
    session = next_business_day(today)
    chain = DATA / "prelim" / f"{today}-chain.json"
    final = DATA / f"{today}-chain.json"
    if chain_ok(final):
        chain = final                               # a Sunday build on Friday's FINAL (QuikStrike or reopen)
    elif not chain_ok(chain):
        log(f"tomorrow: no preliminary book for {today} -- run --job prelim first "
            f"(it does not publish until ~22:00 PT)")
        return status("tomorrow", False, "no preliminary book")
    # `prelim` proves the evening file is a new vintage and writes ok:false when
    # it looks like a re-send. That verdict was computed and then ignored here,
    # so a two-session-old book could be stamped "(preliminary)" -- which on the
    # sheet means FRESHER than final. Honour it.
    try:
        st = json.loads(STATUS.read_text(encoding="utf-8")).get("prelim") or {}
        if st.get("at", "")[:10] == today.isoformat() and not st.get("ok") \
                and "re-send" in str(st.get("detail", "")):
            log(f"tomorrow: prelim flagged tonight's file as a re-send -- {st['detail'][:120]}. "
                f"Not building on it.")
            return status("tomorrow", False, "preliminary book looks like a re-send")
    except Exception:
        pass

    hours = OPTS.get("hours") or rth_hours(session)
    log(f"tomorrow: building the {session} RTH session on tonight's preliminary "
        f"book, {hours}h from the open")
    # The rails stay on the settle; the sheet also has to say where price IS.
    last, hhmm, age = last_print()
    last_at = _price_age("tomorrow", last, hhmm, age)
    for step in (["trinity_db.py", "--chain", str(chain),
                  "--report-date", session.isoformat(), "--hours", f"{hours:g}",
                  "--oi-vintage", chain_vintage(chain)] + REF_ARGS
                 + (["--last", f"{last:.2f}", "--last-at", last_at] if last else []),
                 ["gen.py"], STATE_STEP, CARDS_STEP, ["build.py"], GATE):
        rc, out = run(step)
        if rc != 0:
            log(f"tomorrow: {step[0]} FAILED -- {out.strip().splitlines()[-1][:160]}")
            return status("tomorrow", False, f"{step[0]} failed")
        log(f"tomorrow: {step[0]} ok -- {out.strip().splitlines()[-1][:140]}")
        if step[0] == "trinity_db.py" and not verify("tomorrow", gate=True):
            return status("tomorrow", False, "verify_pull failed")

    sheets = ROOT / "sheets"; sheets.mkdir(exist_ok=True)
    dest = sheets / f"{session}-teddyai.html"          # the same file the 06:00
    src = ROOT / "trinity.html"                        # run will revise
    if src.exists():
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    rc, out = run(["archive.py", "--session", session.isoformat(),
                   "--kind", "rth" if chain_vintage(chain) == "final" else "rthprelim", "--sheet", str(dest)])
    log(f"tomorrow: archive {'ok' if rc == 0 else 'FAILED'} -- "
        f"{out.strip().splitlines()[-1][:120] if out.strip() else ''}")
    log(f"tomorrow: wrote {dest.name} -- PRELIMINARY book, COMMENTARY NOT REWRITTEN")
    status("tomorrow", True, f"{dest.name}; preliminary book for {session}")


def _oi_landed(today):
    """
    Has tonight's preliminary open interest reached the feed? Reads the ts_recv of the
    stat_type 9 records in the statistics schema for the day+1 00:00Z window -- the same
    measurement that put the landing band at 18:34-19:16 PT -- and returns the first one as
    a UTC datetime, or None. Costs $0.00 (the estimate is checked before the pull) and a few
    megabytes; it is there or it is not, one burst in under a second.
    """
    import databento as db
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        log("prelim-poll: DATABENTO_API_KEY not set"); return None
    cl = db.Historical(key)
    try:
        rng = cl.metadata.get_dataset_range(dataset="GLBX.MDP3")
        end = str(((rng.get("schema") or {}).get("statistics") or {}).get("end") or rng.get("end"))[:19]
    except Exception as ex:
        log(f"prelim-poll: dataset range unavailable -- {type(ex).__name__}: {ex}"); return None
    start = f"{today + datetime.timedelta(days=1)}T00:00:00"
    if end <= start:
        return None
    kw = dict(dataset="GLBX.MDP3", schema="statistics", symbols=["NQ.OPT"], stype_in="parent", start=start, end=end)
    try:
        cost = cl.metadata.get_cost(**kw)
        if cost > 0:
            log(f"prelim-poll: the probe would cost ${cost:.2f} -- not pulling"); return None
        df = cl.timeseries.get_range(**kw).to_df()
    except Exception as ex:
        log(f"prelim-poll: probe failed -- {type(ex).__name__}: {str(ex)[:120]}"); return None
    oi = df[df["stat_type"] == 9] if len(df) else df
    if not len(oi):
        return None
    return oi.index.min().to_pydatetime()


def job_prelim_poll(today):
    """
    18:30 PT onward: wait for tonight's preliminary open interest, then pull it and rebuild
    the overnight plan on it (Mark, 2026-09-15: "Yes lets do a poll").

    Why a poll and not a time: the burst reached the feed at 18:34, 18:42, 18:52, 18:58,
    19:11 and 19:16 PT on the six weekdays measured, one burst each. CME says the report
    is available at 6:00 p.m. CT; the broadcast the plan reads comes two to three hours
    after that, and a fixed pull at 19:30 waits up to an hour for a book that is already
    there, while 22:15 waited three. On 2026-09-15 the 17:15 Asia plan faded a 29,300 wall
    that the 18:52 book had already replaced with a +40 ceiling at 29,350, which is where
    the night's rally stopped -- nobody pulled that book until 20:17.

    Ticks every --poll-every minutes (10) until --poll-until (21:00 PT). On landing: the
    prelim job (the real fetch), then `gameplan_bot.py --job asia --commentary cli` -- the
    overnight plan rewritten on the session's own book and posted -- unless --no-gameplan,
    in which case only autopull's asia job runs. Tomorrow's RTH plan stays with the 22:20
    task, three hours later on a fresher print. Refuses on a Friday, like the probe: that
    book is not broadcast until the Sunday reopen.
    """
    if today.weekday() == 4:
        log("prelim-poll: Friday -- open interest is not broadcast until the Sunday reopen; nothing to wait for")
        return status("prelim-poll", False, "Friday: not broadcast until Sunday")
    every = float(OPTS.get("poll_every") or 10)
    until = float(OPTS.get("poll_until") or 21.0)
    if OPTS.get("poll_dry"):
        t = _oi_landed(today)
        msg = (f"landed {t:%H:%M:%S} UTC ({t.astimezone().strftime('%H:%M:%S %Z')})" if t else "not on the feed yet")
        log(f"prelim-poll (dry): {msg}")
        return status("prelim-poll", bool(t), "dry probe: " + msg)
    landed = None
    while True:
        now = datetime.datetime.now()
        h = now.hour + now.minute / 60.0
        if h >= until:
            log(f"prelim-poll: {now:%H:%M} PT and no book -- giving up; the 22:15 prelim task is the fallback")
            return status("prelim-poll", False, f"no book by {now:%H:%M} PT")
        landed = _oi_landed(today)
        if landed:
            log(f"prelim-poll: open interest landed {landed:%H:%M:%S} UTC ({landed.astimezone().strftime('%H:%M:%S %Z')}); pulling")
            break
        log(f"prelim-poll: {now:%H:%M} PT -- not on the feed yet; next check in {every:g} min")
        time.sleep(every * 60)
    job_prelim(today)
    st = {}
    try:
        st = json.loads(STATUS.read_text(encoding="utf-8")).get("prelim") or {}
    except Exception:
        pass
    if not st.get("ok"):
        return status("prelim-poll", False, f"book landed {landed:%H:%M}Z but the prelim job failed: {str(st.get('detail'))[:100]}")
    if OPTS.get("no_gameplan"):
        job_asia(today)
        return status("prelim-poll", True, f"book landed {landed:%H:%M}Z; asia rebuilt (no gameplan)")
    rc, out = run(["gameplan_bot.py", "--job", "asia", "--commentary", "cli"], timeout=2400)
    tail = (out.strip().splitlines() or [""])[-1][:140]
    log(f"prelim-poll: gameplan asia {'ok' if rc == 0 else 'FAILED'} -- {tail}")
    return status("prelim-poll", rc == 0, f"book landed {landed:%H:%M}Z; overnight plan {'rewritten and posted' if rc == 0 else 'NOT posted: ' + tail}")


def job_holiday(today):
    """
    A plan for a GLOBEX-ONLY HOLIDAY session -- Labor Day, MLK, Presidents,
    Memorial, Juneteenth, July 4th (observed), Thanksgiving. The equity indexes
    trade from the Sunday/eve 17:00 CT reopen to a 12:00 CT halt: no cash
    session, no settlement, no expiry. Mark trades it, so it gets a sheet.

        python autopull.py --job holiday --date 2026-09-07

    Book: the newest complete chain, which on a Monday holiday is FRIDAY's --
    either the Databento final (Sunday reopen at the earliest) or the
    QuikStrike merge (quikstrike_oi.py, any time Saturday). Reference: Friday's
    official settle, or the last print once the reopen is trading. Time: the
    plan is described from the reopen (trinity_db handles a holiday report
    date); the front expiry is the next session's. Rails: the one-session move
    scaled to the holiday's variance weight with the usual mean-reversion
    shrink, sqrt(0.40) x 0.85 = 0.537x.
    """
    import nqvol
    if not nqvol.is_globex_holiday(today):
        log(f"holiday: {today} is not a Globex-only holiday -- nothing to build")
        return status("holiday", False, "not a holiday session")
    settle, chain = latest_chain(today, "holiday", fetch=False)
    if chain is None:
        log("holiday: no complete chain in the last 3 sessions")
        return status("holiday", False, "no complete chain")
    oi_label = chain_vintage(chain)
    try:
        src = json.loads(pathlib.Path(chain).read_text(encoding="utf-8")).get("oi_source")
        if src:
            log(f"holiday: open interest from {src}")
    except Exception:
        pass

    reopen, halt = nqvol.open_utc(today), nqvol.close_utc(today)
    now = datetime.datetime.now(UTC)
    scale = OPTS.get("rail_scale") or round(0.85 * nqvol.GLOBEX_HOLIDAY_WEIGHT ** 0.5, 3)
    label = f"{reopen:%a}–{halt:%a} Globex"

    settle_px = None
    settle_file = DATA / f"{settle}-settle.json"
    for p in (settle_file, pathlib.Path(chain)):
        try:
            settle_px = json.loads(p.read_text(encoding="utf-8"))["futures_settle"].get(BAR_SYMBOL)
            if settle_px:
                break
        except Exception:
            pass
    ref, ref_src, last, last_at = settle_px, f"{settle} official settle", None, ""
    if OPTS.get("spot") is not None:
        ref, ref_src = OPTS["spot"], "given on the command line"
    elif now > reopen:
        px, hhmm, age = last_print()
        last_at = _price_age("holiday", px, hhmm, age)
        if px is not None:
            last = px
    if ref is None:
        log(f"holiday: no reference price (no settle file, no {BAR_SYMBOL} in the chain)")
        return status("holiday", False, "no reference price")
    log(f"holiday: {today} session {reopen:%a %H:%M}Z -> {halt:%a %H:%M}Z, book {settle} ({oi_label}), "
        f"reference {ref:,.2f} ({ref_src}), rails {scale:.3f}x '{label}'")

    cmd = ["trinity_db.py", "--chain", str(chain), "--report-date", today.isoformat(),
           "--spot", f"{ref:.2f}", "--rail-scale", f"{scale:.4f}", "--rail-label", label,
           "--oi-vintage", oi_label] + REF_ARGS + (["--last", f"{last:.2f}", "--last-at", last_at] if last else [])
    for step in (cmd, ["gen.py"], STATE_STEP, CARDS_STEP, ["build.py"], GATE):
        rc, out = run(step)
        if rc != 0:
            log(f"holiday: {step[0]} FAILED -- {out.strip().splitlines()[-1][:160]}")
            return status("holiday", False, f"{step[0]} failed")
        log(f"holiday: {step[0]} ok -- {out.strip().splitlines()[-1][:140]}")
        if step[0] == "trinity_db.py":
            verify("holiday", gate=False)

    sheets = ROOT / "sheets"; sheets.mkdir(exist_ok=True)
    dest = sheets / f"{today}-holiday-teddyai.html"
    src_ = ROOT / "trinity.html"
    if src_.exists():
        dest.write_text(src_.read_text(encoding="utf-8"), encoding="utf-8")
    rc, out = run(["archive.py", "--session", today.isoformat(), "--kind", "holiday", "--sheet", str(dest)])
    log(f"holiday: archive {'ok' if rc == 0 else 'FAILED'} -- "
        f"{out.strip().splitlines()[-1][:120] if out.strip() else ''}")
    log(f"holiday: wrote {dest.name} -- book {settle}, reference {ref:,.2f}, rails {scale:.3f}x, "
        f"COMMENTARY NOT REWRITTEN")
    status("holiday", True, f"{dest.name}; book {settle} ({oi_label}), ref {ref:.2f} ({ref_src}), rails {scale:.3f}x")


def job_oi_probe(today):
    """
    23:00 PT = 06:00 UTC next day. Open question: does CME publish PRELIMINARY
    open interest in the evening, early enough to build tomorrow's plan tonight?

    Nothing in the project answers that, so this MEASURES it rather than assuming.
    It writes to a scratch directory so a partial answer can never be mistaken for
    the real chain, and reports the record count for two candidate windows.

    DO NOT RUN THIS ON A FRIDAY. Friday's open interest is not broadcast until
    the Sunday reopen, so every window here is empty and the job concludes
    "no usable preliminary OI in the evening" -- which is true of Fridays and
    false of every other night. That is not hypothetical: the 2026-09-11 run was
    a Friday, its verdict was taken at face value, and it kept the prelim job at
    22:15 and the Asia plan on a stale book for days. Answered properly on
    2026-09-14: weekday OI lands 18:34-19:16 PT (see job_prelim).
    """
    if today.weekday() == 4:
        log("oi-probe: REFUSING -- 2026-09-11's run made exactly this mistake. "
            "Friday's OI is not broadcast until the Sunday reopen, so every window "
            "below would read empty and the verdict would be wrong for every other "
            "night of the week. Run this Monday to Thursday.")
        return status("oi-probe", False, "refused: Friday is not measurable")
    probe = DATA / "probe"
    probe.mkdir(exist_ok=True)
    found = {}
    for label, day, hours in (("same-day evening", 0, "22-24"),
                              ("overnight", 1, "0-6"),
                              ("early morning", 1, "6-12"),
                              ("known-good morning", 1, "12-18")):
        rc, out = run(["databento_pull.py", "--fetch", "--date", str(today),
                       "--window", "final", "--out", str(probe),
                       "--oi-day", str(day), "--oi-hours", hours])
        n = 0
        for line in out.splitlines():
            if "open interest:" in line:
                try:
                    n = int(line.split("open interest:")[1].split()[0].replace(",", ""))
                except Exception:
                    pass
        found[label] = n
        log(f"oi-probe [{label}] day+{day} {hours}h UTC -> {n:,} OI records (rc={rc})")

    best = max(found.values()) if found else 0
    verdict = ("preliminary OI IS available in the evening -- the 23:00 job can "
               "build tomorrow's plan tonight" if best >= 1000 else
               "no usable preliminary OI in the evening -- keep the 06:00 build")
    log(f"oi-probe verdict: {verdict}")
    status("oi-probe", best >= 1000, f"{found}; {verdict}")


def job_premarket(today):
    """
    06:00 PT. Final open interest for the previous session published ~05:00 PT,
    so this is the real premarket build: pull the chain, then run the analysis
    and render so a numbers-fresh sheet exists before the 06:30 open.

    The COMMENTARY is not written here -- it is hand-written prose and the sheet
    will carry a staleness banner until Claude rewrites it. That is deliberate.
    """
    settle, chain = latest_chain(today, "premarket")
    if chain is None:
        log("premarket: no usable settle found in the last 3 business days")
        return status("premarket", False, "no usable settle in 3 business days")
    log(f"premarket: building the {today} session from the {settle} settle")

    # If last night built this plan on the preliminary book, say what the
    # overnight revision did to it. A revision that moves nothing is worth
    # knowing; one that moves a flip 40 points is worth knowing a lot more.
    pre = DATA / "prelim" / f"{settle}-chain.json"
    if chain_ok(pre):
        try:
            def tot(p_):
                ch = json.loads(pathlib.Path(p_).read_text(encoding="utf-8"))["chain"]
                return sum((r.get(cp) or {}).get("oi", 0) or 0
                           for st in ch.values() for r in st.values() for cp in ("C", "P"))
            a, b = tot(pre), tot(chain)
            log(f"premarket: OI revision {a:,} (preliminary) -> {b:,} (final), "
                f"{b - a:+,} ({(b - a) / a * 100:+.2f}%)")
        except Exception as ex:
            log(f"premarket: could not compare against the preliminary book -- {ex}")

    hours = OPTS.get("hours") or rth_hours(today)
    if nqvol.is_half_day(today):
        log(f"premarket: {today} is a half-day -- RTH is {hours:g}h, close "
            f"{nqvol.close_utc(today):%H:%M} UTC")
    last, hhmm, age = last_print()
    last_at = _price_age("premarket", last, hhmm, age)
    for step in (["trinity_db.py", "--chain", str(chain),
                  "--report-date", today.isoformat(), "--hours", f"{hours:g}",
                  "--oi-vintage", chain_vintage(chain)] + REF_ARGS
                 + (["--last", f"{last:.2f}", "--last-at", last_at] if last else []),
                 ["gen.py"], STATE_STEP, CARDS_STEP, ["build.py"], GATE):
        rc, out = run(step)
        if rc != 0:
            log(f"premarket: {step[0]} FAILED -- {out.strip().splitlines()[-1][:160]}")
            return status("premarket", False, f"{step[0]} failed")
        log(f"premarket: {step[0]} ok -- {out.strip().splitlines()[-1][:140]}")
        if step[0] == "trinity_db.py" and not verify("premarket", gate=True):
            return status("premarket", False, "verify_pull failed")

    sheets = ROOT / "sheets"
    sheets.mkdir(exist_ok=True)
    dest = sheets / f"{today}-teddyai.html"
    src = ROOT / "trinity.html"
    if src.exists():
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        log(f"premarket: wrote {dest.name} -- NUMBERS FRESH, COMMENTARY NOT REWRITTEN")
        # The archive label follows the book's vintage, as build.py's KIND does;
        # a premarket that fell back to a preliminary book is 'rthprelim'.
        rc, out = run(["archive.py", "--session", today.isoformat(),
                       "--kind", "rth" if chain_vintage(chain) == "final" else "rthprelim",
                       "--sheet", str(dest)])
        log(f"premarket: archive {'ok' if rc == 0 else 'FAILED'} -- "
            f"{out.strip().splitlines()[-1][:120] if out.strip() else ''}")
    status("premarket", True, f"{dest.name} built for the {today} session")


def sunday(today):
    """
    `--job auto` on a Sunday: the plan for the week's first session on FRIDAY's
    book, with the least manual work the feed allows.

    Friday's OI is final at CME by Saturday morning but is only BROADCAST when
    the feed reopens Sunday 17:00 CT (15:00 PT), so:

      before 15:00 PT   nothing can be pulled. If QuikStrike's OI Matrix has
                        been dropped in data/<friday>-oi.pdf, merge it and
                        build now; otherwise say what to do.
      after 15:00 PT    run `prelim` for Friday -- it tries the Saturday
                        window, then the reopen, then the PDF -- and build:
                        `holiday` if Monday is a Globex-only holiday, else
                        `tomorrow` (Monday's RTH sheet) on Friday's book.
    """
    friday = nqvol.prev_session(today)
    monday = today + datetime.timedelta(days=1)
    h = datetime.datetime.now().hour + datetime.datetime.now().minute / 60.0
    pdf = DATA / f"{friday}-oi.pdf"
    log(f"auto: Sunday -- building the week's first session on the {friday} book")
    if h < 15.0 and not pdf.exists() and not chain_ok(DATA / f"{friday}-chain.json"):
        log(f"auto: before the 15:00 PT reopen Databento cannot have Friday's open interest. "
            f"Drop QuikStrike's OI Matrix (Greek: None) in data/{friday}-oi.pdf to build now, "
            f"or run again after 15:00.")
        return status("auto", False, "Sunday before the reopen; no OI source yet")
    if not chain_ok(DATA / f"{friday}-chain.json"):
        log(f"--- prelim for {friday} ---")
        job_prelim(friday)
    if not chain_ok(DATA / f"{friday}-chain.json") and not chain_ok(DATA / "prelim" / f"{friday}-chain.json"):
        log("auto: no Friday book from Databento or QuikStrike -- not building on a stale one; "
            "the Tuesday/Monday 06:00 premarket picks up the final")
        return status("auto", False, "no Friday book")
    if nqvol.is_globex_holiday(monday):
        log(f"--- holiday for {monday} ---")
        return job_holiday(monday)
    log(f"--- tomorrow for {friday} (builds {nqvol.next_session(friday)}) ---")
    return job_tomorrow(friday)


def _dashboard_up():
    try:
        urllib.request.urlopen(f"{DASH}/api/state", timeout=5).read()
        return True
    except Exception:
        return False


def _opra_missing(date_str):
    """Roots with no chain on disk for this settle -- the same test command_center
    makes before it runs the compare."""
    return [r for r in OPRA_ROOTS
            if not (DATA / "opra" / f"{date_str}-{r}-chain.json").exists()]


def _opra_nq_chain(date_str):
    """The NQ chain opra_compare.py would build on: the final, else the preliminary,
    either one only when it is a real chain (>500 KB -- a settle-only file is not)."""
    for cand in (DATA / f"{date_str}-chain.json", DATA / "prelim" / f"{date_str}-chain.json"):
        if cand.exists() and cand.stat().st_size > 500_000:
            return cand
    return None


def _opra_compare_needed(date_str):
    """
    Why the compare for this settle has to be (re)written, or None when it is current.

    FOUR CHAINS ON DISK IS NOT A COMPARE WRITTEN (2026-09-16). The equity chains
    land at 16:10 PT; the NQ chain they are compared against lands hours later
    (prelim at 22:15, the final the next morning). On 2026-09-15 the 16:10 run
    pulled all four roots and failed the compare for want of an NQ chain, and every
    later run -- 22:22, 00:00 -- saw the chains and said "already complete", so the
    Cross-Reference panel stood empty all night on a settle whose books were on
    disk. The compare is also redone when the NQ chain is newer than it (a compare
    built on the preliminary, once the final exists).
    """
    cmp_ = DATA / "opra" / f"{date_str}-compare.json"
    nq = _opra_nq_chain(date_str)
    if nq is None:
        # Nothing to build on: opra_compare would refuse for want of an NQ chain.
        # This is NOT "current" -- saying so is how the 16:10 run came to report
        # success with no compare on disk. The caller prints this reason.
        return "waiting on an NQ chain for this settle (prelim 22:15 PT, final next morning)"
    if not cmp_.exists():
        return "no compare on disk"
    newer = [f.name for f in [nq] + [DATA / "opra" / f"{date_str}-{r}-chain.json" for r in OPRA_ROOTS]
             if f.exists() and f.stat().st_mtime > cmp_.stat().st_mtime]
    if newer:
        # an equity chain refetched by opra-live for its OI, or the final NQ chain
        # replacing the preliminary, after the compare was written
        return f"newer than the compare: {' '.join(newer)}"
    return None


def _opra_compare(job, date_str, pulled):
    """The compare, so the page has it without waiting for a page open. Returns the
    status. Needs the whole complex; callers check _opra_missing first."""
    import time
    t0 = time.time()
    rc, out = run(["opra_compare.py", "--date", date_str], timeout=900)
    cmp_ = DATA / "opra" / f"{date_str}-compare.json"
    made = cmp_.exists() and cmp_.stat().st_mtime >= t0 - 2     # THIS run's file, not an old one
    if rc != 0 or not made:
        tail = (out.strip().splitlines() or [""])[-1][:160]
        log(f"{job}: {pulled}; the compare failed -- {tail}")
        return status(job, False, f"{date_str} {pulled}; compare failed: {tail}")
    detail = f"{date_str}: {pulled}; compare written"
    log(f"{job}: {detail}")
    return status(job, True, detail)


def job_opra(today):
    """
    The equity books for TODAY's settle, so tomorrow's sheet opens with a
    cross-reference instead of four empty columns.

    MOVED TO THE LATE SLOT, 2026-09-10, and into --job auto's late branch.

    It used to be its own 16:10 entry, deliberately outside auto because a paid
    fetch does not belong behind a button. Three things turned out to be wrong
    with that:

      * IT IS NOT PAID. opra_pull's own estimate reports every request at
        $0.0000 and says so in as many words: "the account has priced every
        request at $0.0000 so far; the gate that matters is --max-mb". The
        objection that kept it out of auto was to a cost that does not exist.

      * 16:10 IS TOO EARLY FOR THE OPEN-INTEREST LEG. For date D the OI window
        is the D+1 UTC day, which opens at 17:00 PT ON D. The one run that ever
        fired went at 14:20 and asked for a window that did not exist yet; SPX
        and SPXW scraped through and QQQ and SPY came back with "no contracts
        with open interest". Both roots have 500-odd good pulls behind them, so
        the roots were never the problem, the clock was. The late slot is the
        same reasoning that puts job_prelim at 22:15 for CME's own preliminary
        OI, confirmed present at 22:06 PT.

      * RUNNING IT REPEATEDLY IS FREE ANYWAY. _opra_missing() short-circuits the
        whole job when the four chains are already on disk, so every auto run
        after the first is a directory listing.

    It sits AFTER prelim and tomorrow in the late branch on purpose: those two
    build the plan Mark actually trades, and a fetch that can run for an hour
    must not stand in front of them.

    NOTHING IS FETCHED BLIND. The free estimate (metadata.get_billable_size and
    get_cost) runs first, its size and price go into the log and the status file,
    and the fetch is abandoned above OPRA_MAX_MB. opra_pull.py --fetch re-checks
    the same gate itself; both are deliberate.
    """
    date_str = today.isoformat()
    missing = _opra_missing(date_str)
    if not missing:
        why = _opra_compare_needed(date_str)
        if not why:
            log(f"opra: {date_str} already complete ({' '.join(OPRA_ROOTS)}); compare current")
            return status("opra", True, f"{date_str} already on disk; compare current")
        if _opra_nq_chain(date_str) is None:
            # the chains are here, the NQ book is not: nothing to run, and it is not
            # a failure of this job -- prelim at 22:15 PT is what unblocks it
            log(f"opra: {date_str} chains on disk but no compare -- {why}")
            return status("opra", True, f"{date_str} chains on disk; NO COMPARE YET -- {why}")
        log(f"opra: {date_str} chains already on disk; {why} -- running the compare")
        return _opra_compare("opra", date_str, "chains already on disk")

    # ---- ONE ROOT AT A TIME ------------------------------------------------------
    # Four roots in one request estimates 321.8 MB on a complete session, which no
    # sane per-request ceiling admits. Per root the largest is SPY at 128.7 MB.
    # Splitting also means a root that fails -- QQQ did on 2026-09-09 -- no longer
    # takes the other three down with it, which is what turned one bad evening into
    # four empty columns on the next day's sheet.
    got, failed, total_mb, total_usd = [], [], 0.0, 0.0
    for r in missing:
        # 1. free: size and price this root's requests
        rc, out = run(["opra_pull.py", "--estimate", "--date", date_str, "--roots", r],
                      timeout=600)
        if rc != 0:
            tail = (out.strip().splitlines() or [""])[-1][:120]
            log(f"opra: {r} estimate FAILED -- {tail}")
            failed.append(f"{r} (estimate)")
            continue
        m = re.search(r"total\s+([\d.]+)\s*MB\s+\$([\d.]+)", out)
        if not m:
            log(f"opra: {r} estimate printed no total -- not fetching")
            failed.append(f"{r} (no total)")
            continue
        mb, usd = float(m.group(1)), float(m.group(2))
        if mb > OPRA_MAX_MB:
            log(f"opra: {r} {mb:,.1f} MB is over the {OPRA_MAX_MB:,.0f} MB ceiling -- NOT fetched")
            failed.append(f"{r} ({mb:,.0f} MB over ceiling)")
            continue
        log(f"opra: {r} estimate {mb:,.1f} MB  ${usd:.4f}")

        # 2. fetch this root
        rc, out = run(["opra_pull.py", "--fetch", "--date", date_str, "--roots", r,
                       "--max-mb", str(OPRA_MAX_MB)], timeout=3600)
        if rc != 0 or r in _opra_missing(date_str):
            tail = (out.strip().splitlines() or [""])[-1][:120]
            log(f"opra: {r} fetch FAILED -- {tail}")
            failed.append(f"{r}: {tail}")
            continue
        got.append(r)
        total_mb += mb
        total_usd += usd
        log(f"opra: {r} ok ({mb:,.1f} MB)")

    if not got:
        return status("opra", False,
                      f"nothing pulled for {date_str}; failed {'; '.join(failed)[:180]}")
    still = _opra_missing(date_str)

    # ---- 3. compare, so the sheet has it without waiting for a page open --------
    # The compare needs the whole complex, so it only runs on a complete set. A
    # partial day still records what landed, so tomorrow's run picks up the rest
    # instead of starting over.
    if still:
        detail = (f"{date_str}: pulled {' '.join(got)} ({total_mb:,.1f} MB); "
                  f"still missing {' '.join(still)}; no compare on a partial set")
        log(f"opra: {detail}")
        return status("opra", False, detail)

    return _opra_compare("opra", date_str, f"{' '.join(got)} pulled ({total_mb:,.1f} MB, ${total_usd:.4f})")


OPRA_LIVE_WAVE_UTC = 10.5      # OPRA disseminates open interest once, at 10:30 UTC


def _opra_needs_oi(date_str):
    """
    Roots whose chain for this settle does not yet carry open interest AS OF
    that settle's close -- the ones a live run would actually improve. A chain
    that is not on disk counts, so this fills gaps as well as refreshing
    vintages.

    THE TEST IS THE VINTAGE, NOT THE LEG. opra_pull stamps `oi_vintage` with
    the date the OI is measured at, and it takes one of three shapes: "OI as
    of D close, live replay ..." (the gateway), "OI as of D close, from the
    D+1 window" (the historical leg, once the D+1 day is complete), and
    "STALE: OI as of D-1 close ...". Only the third is worth another fetch.
    Measured 2026-09-10: 2026-09-09's QQQ chain already carried the 09-09
    vintage from the historical D+1 window, and a test for the words "live
    replay" instead would have re-pulled 128 MB to change a label.

    This is also what makes job_opra_live idempotent inside one morning: the
    launcher can be double-clicked, and the second run is four small JSON
    reads instead of another 320 MB.
    """
    want = []
    for r in OPRA_ROOTS:
        f = DATA / "opra" / f"{date_str}-{r}-chain.json"
        if not f.exists():
            want.append(r)
            continue
        try:
            vintage = json.loads(f.read_text(encoding="utf-8")).get("oi_vintage", "")
        except Exception:
            want.append(r)          # unreadable is not "good enough to keep"
            continue
        if f"OI as of {date_str} close" not in vintage or vintage.startswith("STALE"):
            want.append(r)
    return want


def job_opra_live(today):
    """
    The equity books for the PREVIOUS settle with the open-interest leg taken
    from the LIVE gateway -- the morning counterpart to job_opra, and what
    start_teddy.ps1 launches beside live_feed.py (2026-09-10, Mark's call:
    "run it at the same time teddy.py loads the nq data feed").

    WHY IT IS A SEPARATE JOB AND NOT job_opra WITH A FLAG. job_opra pulls
    TODAY's settle and belongs in the late slot: at 04:00 PT today's quote
    window does not exist yet -- the historical endpoint serves complete days
    only -- which is exactly the 14:20 failure recorded in job_opra's
    docstring. This job pulls the last COMPLETE session's quotes and pairs
    them with the open interest that only became readable this morning. Same
    roots, different date, and the two never want the same hour.

    WHAT IT BUYS. --live-oi is the difference between the cross-reference
    opening with OI as of the previous close and OI as of the settle the book
    is priced on -- the STALE stamp on the Cross-Reference panel. Only the OI
    leg comes from the gateway; the price legs stay historical, so this is a
    FULL fetch (~320 MB across the four roots, 20+ minutes with the American
    pass on QQQ/SPY). That is why the launcher gives it its own console and
    never waits on it.

    THE WINDOW IS NARROW AND REAL. live_oi() replays from 00:00Z on the OI day
    -- today -- and the gateway refuses a start in the future or older than 24
    hours. The wave lands at 10:30Z. So the job is valid from 10:30Z (03:30 PT)
    until 00:00Z rolls (17:00 PT). Outside that it declines instead of burning
    a 320 MB fetch on a replay that returns nothing and then quietly stamps
    the same STALE it was run to remove.

    NOTHING IS FETCHED BLIND, exactly as in job_opra: the free estimate runs
    first per root, its size goes to the log and the status file, and anything
    over OPRA_MAX_MB is abandoned. One root at a time, so a root that fails
    does not take the other three with it.
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    if now_utc.hour + now_utc.minute / 60.0 < OPRA_LIVE_WAVE_UTC:
        detail = (f"{now_utc:%H:%M}Z is before the 10:30Z open-interest wave -- "
                  f"nothing to replay yet, not fetching")
        log(f"opra-live: {detail}")
        return status("opra-live", False, detail)

    date_str = nqvol.prev_session(today).isoformat()
    want = _opra_needs_oi(date_str)
    if not want:
        why = _opra_compare_needed(date_str)
        if why and _opra_nq_chain(date_str) is not None:
            log(f"opra-live: {date_str} OI already current; {why} -- running the compare")
            return _opra_compare("opra-live", date_str, "OI already current")
        if why:
            log(f"opra-live: {date_str} OI already current but {why}")
            return status("opra-live", True, f"{date_str} OI current; NO COMPARE YET -- {why}")
        log(f"opra-live: {date_str} already carries its own settle's OI ({' '.join(OPRA_ROOTS)})")
        return status("opra-live", True, f"{date_str} OI already current for all four roots")

    log(f"opra-live: {date_str} settle, live OI replay from {today} 00:00Z; roots {' '.join(want)}")
    got, failed, total_mb = [], [], 0.0
    for r in want:
        # 1. free: size and price this root's requests. --live-oi drops the OI
        #    leg from the request list, so the estimate is smaller than job_opra's.
        rc, out = run(["opra_pull.py", "--estimate", "--date", date_str,
                       "--roots", r, "--live-oi"], timeout=600)
        if rc != 0:
            tail = (out.strip().splitlines() or [""])[-1][:120]
            log(f"opra-live: {r} estimate FAILED -- {tail}")
            failed.append(f"{r} (estimate)")
            continue
        m = re.search(r"total\s+([\d.]+)\s*MB\s+\$([\d.]+)", out)
        if not m:
            log(f"opra-live: {r} estimate printed no total -- not fetching")
            failed.append(f"{r} (no total)")
            continue
        mb = float(m.group(1))
        if mb > OPRA_MAX_MB:
            log(f"opra-live: {r} {mb:,.1f} MB is over the {OPRA_MAX_MB:,.0f} MB ceiling -- NOT fetched")
            failed.append(f"{r} ({mb:,.0f} MB over ceiling)")
            continue
        log(f"opra-live: {r} estimate {mb:,.1f} MB")

        # 2. fetch, OI from the gateway. opra_pull falls back to the historical
        #    window and then to STALE on its own; the vintage it wrote is the
        #    record of which leg it got, and _opra_needs_oi reads it back.
        rc, out = run(["opra_pull.py", "--fetch", "--date", date_str, "--roots", r,
                       "--max-mb", str(OPRA_MAX_MB), "--live-oi"], timeout=3600)
        if rc != 0:
            tail = (out.strip().splitlines() or [""])[-1][:120]
            log(f"opra-live: {r} fetch FAILED -- {tail}")
            failed.append(f"{r}: {tail}")
            continue
        current = r not in _opra_needs_oi(date_str)
        got.append(r if current else f"{r} (STALE OI)")
        total_mb += mb
        log(f"opra-live: {r} ok ({mb:,.1f} MB)"
            + ("" if current else " -- the replay was refused or empty and the historical "
                                  "window is not complete yet; OI is still one session STALE"))

    if not got:
        detail = f"nothing pulled for {date_str}; failed {'; '.join(failed)[:180]}"
        log(f"opra-live: {detail}")
        return status("opra-live", False, detail)

    # ---- 3. the compare, so the page has it without waiting for a page open ----
    # Same rule as job_opra: the compare needs the whole complex, so it only runs
    # on a complete set. A partial morning still records what landed.
    missing = _opra_missing(date_str)
    if missing:
        detail = (f"{date_str}: pulled {', '.join(got)} ({total_mb:,.1f} MB); "
                  f"still missing {' '.join(missing)}; no compare on a partial set")
        log(f"opra-live: {detail}")
        return status("opra-live", False, detail)

    return _opra_compare("opra-live", date_str, f"{', '.join(got)} ({total_mb:,.1f} MB)")


def choose(now=None):
    """
    Which session is the useful one to build RIGHT NOW.

    The jobs each assume a session -- premarket is always 6.5h RTH, asia is
    always 20h overnight -- so running the wrong one produces a perfectly
    computed plan for a session that has already finished. This picks by the
    clock instead, and returns the prerequisite alongside it where one exists.

    Local machine time is Pacific, which is what the CME day is reasoned in
    throughout this project.
    """
    now = now or datetime.datetime.now()
    h = now.hour + now.minute / 60.0
    if h < 5.0:
        return [], ("before 05:00 PT: the previous session's open interest has not "
                    "published and tonight's Asia plan was built at 17:15. Nothing to do.")
    if h < 6.5:
        return ["premarket"], "morning: building today's RTH plan on the previous settle"
    if h < 13.0:
        return ["intraday"], ("inside the RTH session: building a plan for the time "
                              "that is actually left, anchored on the current price")
    if h < 16.0:
        return ["close"], ("the session is done but settlement is still being "
                           "disseminated until 16:00 -- refreshing bars only")
    # PRELIMINARY OI LANDS AROUND 19:00 PT, NOT 22:15. Measured 2026-09-14 by
    # reading the ts_recv of the stat_type 9 records themselves rather than
    # guessing at windows: 09-03 18:34, 09-10 18:42, 09-14 18:58, 09-08 19:11,
    # 09-09 19:16 PT -- five weekdays inside a 42-minute band, each arriving as
    # one burst of ~800-1,700 records in under two seconds.
    #
    # So an evening run after ~19:30 can build on the CURRENT session's book
    # instead of one that is a session or more behind. Before that it cannot,
    # and pulling would only cache an empty window.
    if h < 19.5:
        return ["settle", "asia"], ("early evening: capturing the official settle, then "
                                    "building tonight's Asia plan -- preliminary open "
                                    "interest has not landed yet, so this uses the last "
                                    "complete book")
    if h < 22.0:
        return ["settle", "prelim", "asia"], (
            "evening: preliminary open interest is out by now -- pulling tonight's book "
            "FIRST so the Asia plan is built on the session it describes rather than on "
            "the previous one")
    return ["prelim", "tomorrow", "opra"], (
        "late: CME's preliminary open interest is out -- pulling tonight's book "
        "and building TOMORROW's RTH plan on it, a night early, then the equity "
        "books. The overnight session is nearly over by now and is not traded; "
        "--job asia still builds it by hand if you want the tail.")


def job_london(today):
    """Tonight's London plan: the same book and the same reference as the Asia build,
    with the rails the 07:00-13:30 UTC window actually earns. See job_asia."""
    OPTS["london"] = True
    return job_asia(today)


JOBS = {"close": job_close, "settle": job_settle, "asia": job_asia, "london": job_london,
        "oi-probe": job_oi_probe, "premarket": job_premarket,
        "intraday": job_intraday, "prelim": job_prelim,
        "tomorrow": job_tomorrow, "holiday": job_holiday,
        "prelim-poll": job_prelim_poll,   # 18:30 PT: wait for tonight's book, pull it, rebuild the overnight on it
        "opra": job_opra,         # in --job auto's LATE branch since 2026-09-10; see job_opra
        "opra-live": job_opra_live}   # started by the launcher, not by auto; see job_opra_live

# Manual overrides, set once in main() and read by the jobs that honour them.
# The scheduled runs never set these -- a scheduled job that needs an argument
# is a job whose defaults are wrong.
OPTS = {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True, choices=list(JOBS) + ["auto"])
    ap.add_argument("--date", default=None, help="override 'today' (YYYY-MM-DD)")
    ap.add_argument("--poll-every", type=float, default=None, help="prelim-poll: minutes between checks (10)")
    ap.add_argument("--poll-until", type=float, default=None, help="prelim-poll: give up at this PT hour (21.0)")
    ap.add_argument("--poll-dry", action="store_true", help="prelim-poll: probe once, report, exit")
    ap.add_argument("--no-gameplan", action="store_true", help="prelim-poll: rebuild the Asia sheet without the CLI commentary and the post")
    ap.add_argument("--hours", type=float, default=None,
                    help="force time to expiry in hours (intraday, premarket)")
    ap.add_argument("--spot", type=float, default=None,
                    help="force the reference price (intraday)")
    ap.add_argument("--rail-scale", type=float, default=None,
                    help="force the rail scale; the measured profile is used otherwise")
    a = ap.parse_args()
    OPTS.update({k: v for k, v in
                 dict(hours=a.hours, spot=a.spot, rail_scale=a.rail_scale).items()
                 if v is not None})
    OPTS.update({"poll_every": a.poll_every, "poll_until": a.poll_until, "poll_dry": a.poll_dry, "no_gameplan": a.no_gameplan})
    today = datetime.date.fromisoformat(a.date) if a.date else datetime.date.today()
    if today.weekday() == 6 and a.job == "auto":
        return sunday(today)
    if not nqvol.is_session(today) and a.job not in ("oi-probe", "holiday"):
        log(f"{a.job}: {today} is not a CME session (weekend or holiday), nothing to do"
            + (" -- use --job holiday for a Globex-only holiday session" if nqvol.is_globex_holiday(today) else ""))
        return
    jobs = [a.job]
    if a.job == "auto":
        jobs, why = choose()
        log(f"auto: {why}")
        if not jobs:
            return
        log(f"auto: running {' then '.join(jobs)}")

    for name in jobs:
        log(f"--- {name} for {today} ---")
        try:
            JOBS[name](today)
        except Exception as ex:
            log(f"{name}: UNCAUGHT {type(ex).__name__}: {ex}")
            status(name, False, f"uncaught {type(ex).__name__}")


if __name__ == "__main__":
    main()
