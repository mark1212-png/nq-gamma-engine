#!/usr/bin/env python3
"""
live_feed.py — TeddyAI live bar feeder.

    set DATABENTO_API_KEY=...
    python live_feed.py --probe      # once, before you trust it
    python live_feed.py              # run it for real
    python live_feed.py --backfill   # close the holes in the last five days' files, then exit

Streams NQ from Databento Live and keeps `data/bars/<utc-day>.json` current, in
exactly the shape `teddy._last_print`, `teddy.api_bars` and `autopull.last_print`
already read. Nothing downstream needs to know this exists: it writes the same
file the historical pull writes, only continuously.

WHY THIS EXISTS. `autopull --job intraday` anchors the plan on the last print,
warns above 10 minutes stale, and tells you to "start the dashboard so /api/bars
can pull forward" -- so the scheduled intraday job has been quietly depending on
teddy.py being open on your desk. HANDOFF calls a stale reference the one failure
mode that does not degrade gracefully. This removes that dependency: the bars
stay current whether or not anything is open.

WHAT IT DOES NOT DO. It does not make the gamma ladder live. The ladder is built
from open interest, which publishes once a day; a live feed cannot change it. It
makes SPOT live, and therefore the rails and the HVL solve that are derived from
spot and time to expiry. That is the whole of it.

THE FILE IS SHARED. teddy.py reads the same JSON while this writes it, so every
write goes through a temp file and os.replace -- a reader either sees the old
file whole or the new file whole, never a half-written one.

TIMESTAMPS ARE THE START OF THE BAR, UTC, and serialised space-separated
("2026-09-04 13:30:00+00:00") because that is what `str(pandas.Timestamp)` gives
and what the existing caches contain. Do not "fix" it to an ISO T: teddy's
incremental fetch already puts the T back on the way out, and changing it here
would silently break the resume.

PRICES ARE FIXED-POINT. Databento sends int64 where 1 unit = 1e-9. A raw 'open'
of 29402000000000 is 29,402.00. Forgetting the scale does not raise -- it writes
a plausible-looking number that is off by nine orders of magnitude.
"""

import os, sys, json, time, signal, argparse, datetime, pathlib, traceback

ROOT = pathlib.Path(__file__).parent.resolve()
BARSDIR = ROOT / "data" / "bars"
STATUS = BARSDIR / "feed_status.json"
LOGFILE = ROOT / "notes" / "live_feed.log"

DATASET = "GLBX.MDP3"
# PINNED TO THE TRADED CONTRACT FOR THE ROLL WINDOW -- 2026-09-14.
#
# This was NQ.c.0 / "continuous": the CALENDAR-roll front month, which stays on
# the expiring contract until it expires. Measured today, four sessions before
# NQU6 expires on 09-18:
#
#   NQZ6 held 54.6% of RTH volume against NQU6's 45.4% -- the roll has already
#   happened in the tape, and Mark's platform moved him with it -- while BOTH
#   NQ.c.0 and NQ.v.0 still resolved to NQU6 (identical bars to the tick). So
#   the volume-roll symbol is not a fix here; it lags the crossover.
#
# The cost of leaving it was a chart and a reference 296 points away from the
# contract actually being traded, which is how a whole day of sheets got built
# on an axis that could not be filled.
#
# RAW SYMBOL, so revisit at every quarterly roll: teddy.py's api_bars warns that
# a raw symbol silently serves a stale series once it stops being front, and
# that is a real trap -- it is simply the smaller one this week. After 09-18,
# NQ.c.0 points at NQZ6 by itself and this can go back to continuous.
SYMBOL = "NQZ6"
STYPE = "raw_symbol"

# ohlcv-1m is one message a minute and is what the cache stores. If the gateway
# will not serve it live we fall back to ohlcv-1s and fold seconds into minutes
# here -- 60x the messages, still trivial next to a trades feed.
#
# HOW FAST THE DASHBOARD CAN POSSIBLY BE IS DECIDED HERE, not in the browser.
# On ohlcv-1m a bar arrives once a minute, so the last price is up to 60s old no
# matter how often the page re-reads it. Run with `--schema ohlcv-1s` and the
# forming minute is rewritten every second instead: same file, same format,
# same 1-minute rows -- the last row simply keeps updating until the minute
# closes. That is the setting to reach for when "make it refresh faster" means
# the price, rather than the page.
SCHEMA_PREFS = ["ohlcv-1m", "ohlcv-1s"]

STALE_RECONNECT_S = 300    # no messages for this long -> tear down and redial
# The cache is what every reader sees, so this is the real end-to-end latency
# floor on top of the schema. Two seconds costs one ~100 KB write per second in
# the worst case, which is nothing locally, and halves the lag on ohlcv-1s.
FLUSH_EVERY_S = 2
MAX_BACKOFF_S = 300
HEAL_EVERY_S = 600         # re-ask the historical API for the whole day; fills holes a crash left


# ------------------------------------------------------------------- util ---

def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        LOGFILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOGFILE, "a", encoding="utf-8") as fh:   # utf-8, always: the
            fh.write(line + "\n")                          # log carries arrows
    except Exception:
        pass


def replace(tmp, final, tries=8):
    """
    os.replace with a short retry, and never an exception.

    On Windows the rename fails with WinError 5 (access denied) for as long as
    another process has the target open for reading -- teddy.py stats and reads
    feed_status.json and the day file every few seconds, autopull reads them
    too -- so a write loses this race several times an hour. Both files are
    rewritten every couple of seconds anyway, so a lost write can simply wait
    for the next one. It used to raise: on 2026-09-10 21:44 PT it dropped the
    stream, and on 2026-09-13 22:24Z it hit inside the reconnect handler and
    took the whole feeder down, twenty minutes into the Sunday reopen.
    """
    for i in range(tries):
        try:
            os.replace(tmp, final)
            return True
        except PermissionError:
            time.sleep(0.05 * (i + 1))
        except Exception:
            break
    try:
        tmp.unlink()
    except Exception:
        pass
    return False


def px(v):
    """Fixed-point int64 (1 unit = 1e-9) -> a price. NQ ticks at 0.25."""
    return round(v * 1e-9, 4)


def ts_of(ns):
    """Event nanoseconds -> the cache's timestamp string, truncated to seconds."""
    dt = datetime.datetime.fromtimestamp(ns / 1e9, datetime.timezone.utc)
    return dt.replace(microsecond=0).isoformat(sep=" ")


def utc_day(ns=None):
    if ns is None:
        return datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    return datetime.datetime.fromtimestamp(ns / 1e9, datetime.timezone.utc).date().isoformat()


def minute_floor(ts):
    """'2026-09-04 13:30:41+00:00' -> '2026-09-04 13:30:00+00:00'."""
    return ts[:17] + "00" + ts[19:]


# -------------------------------------------------------------- bar store ---

class BarBook:
    """
    One UTC day of 1-minute bars, merged into whatever is already on disk.

    Merging rather than replacing is the point: the feeder is usually started
    into a day that already has bars in it -- from the historical backfill, or
    from the feeder's own earlier run -- and clobbering those would punch a hole
    in exactly the file the intraday job anchors on.
    """

    def __init__(self, day=None):
        self.day = None
        self.rows = []
        self.pos = {}
        self.dirty = False
        if day:
            self.load(day)

    def load(self, day):
        self.day, self.rows, self.pos, self.dirty = day, [], {}, False
        f = BARSDIR / f"{day}.json"
        if f.exists():
            try:
                self.rows = json.loads(f.read_text(encoding="utf-8"))
            except Exception as ex:
                log(f"  cache for {day} unreadable ({ex}); starting from empty")
                self.rows = []
        self.pos = {r["ts"]: i for i, r in enumerate(self.rows)}
        return len(self.rows)

    def upsert(self, ts, o, h, l, c, v=0):
        """A forming minute is written repeatedly; the last write for it wins."""
        i = self.pos.get(ts)
        row = dict(ts=ts, o=o, h=h, l=l, c=c, v=v)
        if i is None:
            self.rows.append(row)
            self.pos[ts] = len(self.rows) - 1
        else:
            if self.rows[i] == row:
                return False
            self.rows[i] = row
        self.dirty = True
        return True

    def merge(self, rows):
        n = 0
        for r in rows:
            if self.upsert(r["ts"], r["o"], r["h"], r["l"], r["c"], r.get("v", 0)):
                n += 1
        return n

    def last(self):
        return self.rows[-1] if self.rows else None

    def flush(self):
        if not self.dirty or self.day is None:
            return False
        BARSDIR.mkdir(parents=True, exist_ok=True)
        # Sort on the way out. Records can arrive slightly out of order across a
        # reconnect, and every reader downstream takes rows[-1] as "latest".
        self.rows.sort(key=lambda r: r["ts"])
        self.pos = {r["ts"]: i for i, r in enumerate(self.rows)}
        final = BARSDIR / f"{self.day}.json"
        tmp = final.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.rows), encoding="utf-8")
        if not replace(tmp, final):     # atomic: teddy never sees a torn file
            return False                # still dirty: the next flush tries again
        self.dirty = False
        return True


class MinuteAgg:
    """Folds ohlcv-1s into the minute that contains it. Unused on ohlcv-1m."""

    def __init__(self):
        self.ts = None
        self.o = self.h = self.l = self.c = None

    def add(self, ts, o, h, l, c):
        m = minute_floor(ts)
        if m != self.ts:
            self.ts, self.o, self.h, self.l, self.c = m, o, h, l, c
        else:
            self.h = max(self.h, h)
            self.l = min(self.l, l)
            self.c = c
        return dict(ts=self.ts, o=self.o, h=self.h, l=self.l, c=self.c)


# ----------------------------------------------------------------- status ---

def write_status(**kw):
    """
    A heartbeat teddy.py and autopull can stat without importing anything.

    Written every few seconds; `age` at read time is what tells a reader whether
    the feed is actually alive, so consumers must look at the mtime or at
    `updated`, never merely at the file existing.
    """
    BARSDIR.mkdir(parents=True, exist_ok=True)
    doc = dict(pid=os.getpid(),
               updated=datetime.datetime.now(datetime.timezone.utc)
                       .isoformat(timespec="seconds"))
    doc.update(kw)
    try:
        tmp = STATUS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        replace(tmp, STATUS)
    except Exception:
        pass            # the heartbeat is best-effort; the next one is two seconds away


def clear_status(reason):
    try:
        write_status(state="stopped", detail=reason, last_px=None)
    except Exception:
        pass


# ------------------------------------------------------------- historical ---

def historical():
    import databento as db
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        raise RuntimeError("DATABENTO_API_KEY not set")
    return db.Historical(key)


def avail_end(cl, schema="ohlcv-1m"):
    """
    The last instant the historical API will serve for `schema`, as
    'YYYY-MM-DDTHH:MM:SS', or None if the metadata call fails.

    THE SCHEMA'S END, NOT THE DATASET'S. get_dataset_range() reports a top-level
    `end` that belongs to its newest schema (trades: 21:50Z on the Sunday this
    was found) and a per-schema map under "schema"; ohlcv-1m trails the top
    level by ten to fifteen minutes, and a request past ITS end is not an empty
    frame but a 422 data_schema_not_fully_available. Clamping to the top-level
    end is why every catch-up from 2026-09-10 on failed with exactly that.
    """
    try:
        rng = cl.metadata.get_dataset_range(dataset=DATASET)
        sch = (rng.get("schema") or {}).get(schema) or {}
        end = str(sch.get("end") or rng.get("end") or "")[:19]
        return end or None
    except Exception:
        return None


def get_bars(cl, start, end):
    """
    ohlcv-1m rows for [start, end) in the cache's shape.

    One retry when the API says the range runs past what it holds: the refusal
    names the end it will serve, so ask again up to there rather than lose the
    whole catch-up to the last ten minutes of it.
    """
    import re
    try:
        df = cl.timeseries.get_range(dataset=DATASET, schema="ohlcv-1m",
                                     symbols=[SYMBOL], stype_in=STYPE,
                                     start=start, end=end).to_df()
    except Exception as ex:
        m = re.search(r"and (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", str(ex))
        if "not_fully_available" not in str(ex) or not m or m.group(1) <= start:
            raise
        log(f"  range clamped to {m.group(1)} (the API's ohlcv-1m end)")
        df = cl.timeseries.get_range(dataset=DATASET, schema="ohlcv-1m",
                                     symbols=[SYMBOL], stype_in=STYPE,
                                     start=start, end=m.group(1)).to_df()
    # VOLUME (2026-09-16). ohlcv-1m carries it and the cache was throwing it away, so
    # VWAP and a volume profile -- both on Mark's chart, both load-bearing in his scenarios
    # -- were not computable anywhere downstream. Same schema, same L0 row, no extra cost.
    return [dict(ts=str(t), o=float(r.open), h=float(r.high), l=float(r.low),
                 c=float(r.close), v=int(getattr(r, "volume", 0) or 0))
            for t, r in df.iterrows()]


def catch_up(book, day, cl=None, avail=None):
    """
    Fill the day up to now from the historical API before streaming.

    Run at every connect, not just the first, so a reconnect closes its own gap
    instead of leaving a hole nobody notices until a plan anchors beside it.
    L0 historical carries no marginal cost, so this is free to do liberally.
    `cl` and `avail` are passed in when the caller already has them
    (backfill_gaps runs this for several days in a row) and made here otherwise.
    """
    cl = cl or historical()
    nxt = (datetime.date.fromisoformat(day) + datetime.timedelta(days=1)).isoformat()
    end = f"{nxt}T00:00:00"
    avail = avail or avail_end(cl)
    if avail and avail < end:
        end = avail

    # THE WHOLE DAY, not "since the last bar". A feeder that died at 22:24Z and
    # came back at 22:49Z (2026-09-13) had its last bar at 22:29Z from the
    # previous catch-up; "since the last bar" would have left 22:30-22:48Z a
    # hole for good, because the live stream only ever appends and every later
    # catch-up would start from a bar past the hole. A day is at most 1,440
    # rows on a free endpoint, and the merge is by minute, so ask for all of it.
    # (The T is not cosmetic: Databento rejects the space-separated form with
    # 400 data_invalid_datetime_string.)
    start = f"{day}T00:00:00"
    if start >= end:
        return 0
    n = book.merge(get_bars(cl, start, end))
    book.flush()
    return n


# UTC days this process has verified to the close. A settled day does not
# change, so it is asked about once, not at every redial: through a weekend the
# halt-reconnect cycle would otherwise repeat the same requests every five
# minutes.
_COMPLETE = set()


def expected_close(day):
    """
    The last minute a UTC day's file should hold, 'HH:MM', or None for a day no
    session touches (the backfill only uses the None; the dashboard uses the time).

    Mon-Thu and Sun: a session opens at 22:00Z that evening, so the file runs
    to 23:59. Fri: the session settles at 21:00Z and nothing opens after it, so
    20:59. Sat: nothing. Holidays and early closes are not modelled -- the
    fetch simply returns nothing more and the day is marked complete.
    """
    wd = datetime.date.fromisoformat(day).weekday()
    return "23:59" if wd in (0, 1, 2, 3, 6) else "20:59" if wd == 4 else None


def backfill_gaps(cl, avail, today, days_back=5):
    """
    Close the holes in the PREVIOUS UTC days' files before streaming today's.

    catch_up() fills the day being streamed and nothing else, so a feeder that
    died mid-session left that day short for good: 2026-09-11.json stopped at
    15:51Z when the process went down at 08:51 PT, and all weekend every reader
    downstream -- the settle job's "close print", the plan check ("bar cache
    stale ... the calendar expects tape to Fri 20:59"), the command center's
    chart -- had Friday's morning where Friday's close should have been. Walks
    back `days_back` UTC days and asks for each WHOLE day once per process --
    the whole day, because a hole in the middle of a file (a crash, a restart
    an hour later) looks complete from its last bar. A day the exchange was
    shut returns nothing and is marked complete like any other.
    """
    filled = []
    for i in range(1, days_back + 1):
        d = today - datetime.timedelta(days=i)
        day = d.isoformat()
        if day in _COMPLETE or expected_close(day) is None:
            continue
        book = BarBook(day)
        try:
            n = catch_up(book, day, cl, avail)
        except Exception as ex:
            log(f"  backfill {day}: {type(ex).__name__}: {ex}")
            continue
        _COMPLETE.add(day)
        if n:
            filled.append(f"{day} +{n} bar(s), now to {book.last()['ts'][11:16]}Z")
    if filled:
        log("  backfill: " + "; ".join(filled))


# ----------------------------------------------------------------- stream ---

class Feed:
    def __init__(self, schema=None):
        self.schema = schema
        self.book = BarBook()
        self.agg = MinuteAgg()
        self.contract = None
        self.last_msg = 0.0
        self.bars_seen = 0
        self.stop_flag = False

    # -- record handling ----------------------------------------------------

    def on_record(self, rec):
        self.last_msg = time.time()

        # Symbol mappings arrive at session start and again on a roll. The roll
        # is worth shouting about: NQ.c.0 silently becoming the next contract
        # mid-session is a change of instrument under a plan built on the old
        # one, and nothing else in the stack would notice.
        mapped = getattr(rec, "stype_out_symbol", None)
        if mapped is not None:
            if self.contract and mapped != self.contract:
                log(f"  ROLL: {SYMBOL} remapped {self.contract} -> {mapped}")
            else:
                log(f"  {SYMBOL} maps to {mapped}")
            self.contract = mapped
            return

        o = getattr(rec, "open", None)
        if o is None:                       # status, error, anything not a bar
            return
        ts = ts_of(rec.ts_event)
        day = utc_day(rec.ts_event)
        if day != self.book.day:
            if self.book.day is not None:
                self.book.flush()
                log(f"  UTC day rolled {self.book.day} -> {day}")
            self.book.load(day)
            self.agg = MinuteAgg()

        bar = dict(ts=ts, o=px(rec.open), h=px(rec.high),
                   l=px(rec.low), c=px(rec.close), v=int(getattr(rec, "volume", 0) or 0))
        if self.schema == "ohlcv-1s":
            bar = self.agg.add(bar["ts"], bar["o"], bar["h"], bar["l"], bar["c"])
        else:
            bar["ts"] = minute_floor(bar["ts"])
        self.book.upsert(bar["ts"], bar["o"], bar["h"], bar["l"], bar["c"], bar.get("v", 0))
        self.bars_seen += 1

    # -- one connection -----------------------------------------------------

    def connect(self, db, key):
        """
        Open a session, negotiating the schema on the first connection.

        The gateway is the authority on what this key may stream, so the schema
        is chosen by trying rather than by assuming: ohlcv-1m first because it
        is one message a minute, ohlcv-1s second because folding it locally is
        cheap. Once one works it is remembered for later reconnects.
        """
        tries = [self.schema] if self.schema else SCHEMA_PREFS
        last_err = None
        for sch in tries:
            client = db.Live(key=key)
            try:
                client.subscribe(dataset=DATASET, schema=sch,
                                 stype_in=STYPE, symbols=[SYMBOL])
            except Exception as ex:
                last_err = ex
                log(f"  {sch}: refused ({type(ex).__name__}: {ex})")
                try: client.terminate()
                except Exception: pass
                continue
            if self.schema != sch:
                log(f"  schema: {sch}"
                    + ("  (folding seconds into minutes here)"
                       if sch == "ohlcv-1s" else ""))
            self.schema = sch
            return client
        raise RuntimeError(f"no usable schema; last error: {last_err}")

    def run_once(self):
        import databento as db
        key = os.environ.get("DATABENTO_API_KEY")
        if not key:
            raise RuntimeError("DATABENTO_API_KEY not set")

        day = utc_day()
        self.book.load(day)
        write_status(state="connecting", schema=self.schema, symbol=SYMBOL,
                     contract=self.contract, bars_today=len(self.book.rows))
        cl = None
        try:
            cl = historical()
            avail = avail_end(cl)
            try:
                backfill_gaps(cl, avail, datetime.date.fromisoformat(day))
            except Exception as ex:
                log(f"  backfill failed ({type(ex).__name__}: {ex})")
            n = catch_up(self.book, day, cl, avail)
            log(f"  catch-up: {n} bar(s) from history; {len(self.book.rows)} today")
        except Exception as ex:
            log(f"  catch-up failed ({type(ex).__name__}: {ex}) -- streaming anyway")

        if cl is None:
            try: cl = historical()
            except Exception: cl = None
        client = self.connect(db, key)
        client.add_callback(self.on_record)
        client.start()
        self.last_msg = time.time()
        log(f"  streaming {SYMBOL} {self.schema}")

        healed = time.time()
        try:
            while not self.stop_flag:
                time.sleep(FLUSH_EVERY_S)
                self.book.flush()
                # Heal the day every HEAL_EVERY_S: a crash leaves a hole that
                # the catch-up at reconnect can only fill up to the API's
                # 10-15 minute lag, and on a healthy stream the next reconnect
                # may be hours away. Free, idempotent (merged by minute).
                if time.time() - healed > HEAL_EVERY_S:
                    healed = time.time()
                    try:
                        n = catch_up(self.book, self.book.day, cl, avail_end(cl))
                        if n:
                            log(f"  heal: {n} bar(s) filled from history")
                    except Exception as ex:
                        log(f"  heal failed ({type(ex).__name__}: {ex})")
                last = self.book.last()
                quiet = time.time() - self.last_msg
                write_status(state="streaming", schema=self.schema, symbol=SYMBOL,
                             contract=self.contract,
                             last_bar_ts=last["ts"] if last else None,
                             last_px=last["c"] if last else None,
                             bars_today=len(self.book.rows),
                             quiet_s=round(quiet, 1))
                if quiet > STALE_RECONNECT_S:
                    # Silence is ambiguous -- a dead socket and the daily halt
                    # look identical from here -- so redial rather than guess.
                    # Backoff upstream keeps an overnight halt from thrashing.
                    raise RuntimeError(f"no messages for {quiet:.0f}s")
        finally:
            self.book.flush()
            try: client.stop()
            except Exception: pass

    def run_forever(self):
        backoff = 5
        while not self.stop_flag:
            try:
                self.run_once()
                backoff = 5
            except KeyboardInterrupt:
                raise
            except Exception as ex:
                log(f"  disconnected: {type(ex).__name__}: {ex}")
                write_status(state="reconnecting", schema=self.schema,
                             symbol=SYMBOL, contract=self.contract,
                             error=f"{type(ex).__name__}: {ex}",
                             retry_in_s=backoff)
                if self.stop_flag:
                    break
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)


# ------------------------------------------------------------------ probe ---

def probe(seconds=90):
    """
    One-shot check, before trusting any of the above.

    Answers the three things the docs would not settle: whether this key can
    stream GLBX.MDP3 live at all, which OHLCV schema the gateway actually
    serves, and what NQ.c.0 is currently mapped to. Writes nothing.
    """
    print("TeddyAI live feed probe")
    print("=" * 60)
    key = os.environ.get("DATABENTO_API_KEY")
    print(f"  DATABENTO_API_KEY : {'found' if key else 'NOT SET -- stop here'}")
    if not key:
        return 1
    try:
        import databento as db
    except ImportError:
        print("  databento         : NOT INSTALLED  ->  pip install --user databento")
        return 1
    print(f"  databento         : {getattr(db, '__version__', 'unknown')}")
    print(f"  dataset / symbol  : {DATASET}  {SYMBOL}  (stype_in={STYPE})")

    # What this key may actually stream, asked rather than assumed. Standard is
    # documented as L0-L1, which would mean bars, trades, tbbo and top-of-book
    # yes; MBP-10 (L2) and MBO (L3) no. Entitlements vary, so probe them.
    print("\n  capability check -- what this key can stream live")
    LEVELS = [("ohlcv-1m", "L0", "1 bar/min -- what the feeder needs"),
              ("ohlcv-1s", "L0", "1 bar/sec -- fallback"),
              ("trades",   "L1", "every print, with aggressor side"),
              ("tbbo",     "L1", "trades + top of book at the trade -> order flow"),
              ("mbp-1",    "L1", "top of book, quote by quote"),
              ("mbp-10",   "L2", "10 levels of depth"),
              ("mbo",      "L3", "full order book by order")]
    entitled = []
    for sch, lvl, what in LEVELS:
        c = db.Live(key=key)
        try:
            c.subscribe(dataset=DATASET, schema=sch, stype_in=STYPE, symbols=[SYMBOL])
            print(f"    {lvl}  {sch:10} YES   {what}")
            entitled.append(sch)
        except Exception as ex:
            print(f"    {lvl}  {sch:10} no    {type(ex).__name__}: {str(ex)[:60]}")
        try: c.terminate()
        except Exception: pass

    print("\n  bar schema for the feeder")
    ok = None
    for sch in SCHEMA_PREFS:
        c = db.Live(key=key)
        try:
            c.subscribe(dataset=DATASET, schema=sch, stype_in=STYPE, symbols=[SYMBOL])
            print(f"    {sch:10} accepted")
            if ok is None:
                ok = (sch, c)
                continue
        except Exception as ex:
            print(f"    {sch:10} REFUSED  {type(ex).__name__}: {ex}")
        try: c.terminate()
        except Exception: pass
    if ok is None:
        print("\n  No schema was accepted. Either the live entitlement is not on this\n"
              "  key, or GLBX.MDP3 live is not enabled for it.")
        return 1

    sch, client = ok
    print(f"\n  listening on {sch} for up to {seconds}s "
          f"(quiet is normal outside a session)\n")
    seen = {"bars": 0, "map": None, "first": None, "last": None}

    def on_rec(rec):
        m = getattr(rec, "stype_out_symbol", None)
        if m is not None:
            seen["map"] = m
            print(f"    mapping   {SYMBOL} -> {m}  (instrument_id "
                  f"{getattr(rec, 'instrument_id', '?')})")
            return
        if getattr(rec, "open", None) is None:
            return
        seen["bars"] += 1
        row = dict(ts=ts_of(rec.ts_event), o=px(rec.open), h=px(rec.high),
                   l=px(rec.low), c=px(rec.close))
        seen["last"] = row
        if seen["first"] is None:
            seen["first"] = row
        if seen["bars"] <= 5:
            print(f"    bar       {row['ts']}  O {row['o']}  H {row['h']}  "
                  f"L {row['l']}  C {row['c']}")

    client.add_callback(on_rec)
    client.start()
    try:
        t0 = time.time()
        while time.time() - t0 < seconds and seen["bars"] < 5:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try: client.stop()
        except Exception: pass

    print("\n" + "=" * 60)
    flow = [s for s in ("tbbo", "trades", "mbp-1") if s in entitled]
    depth = [s for s in ("mbp-10", "mbo") if s in entitled]
    print(f"  order flow        : {', '.join(flow) if flow else 'NOT AVAILABLE on this key'}")
    print(f"  depth / book      : {', '.join(depth) if depth else 'not on this key (L2/L3 needs Plus or above)'}")
    print(f"  schema that works : {sch}")
    print(f"  NQ.c.0 maps to    : {seen['map'] or 'no mapping message seen'}")
    print(f"  bars received     : {seen['bars']}")
    if seen["last"]:
        p = seen["last"]["c"]
        print(f"  last close        : {p:,.2f}")
        sane = 1_000 < p < 200_000
        print(f"  price scale       : {'looks right' if sane else 'WRONG -- check the 1e-9 scaling'}")
    else:
        print("  No bars arrived. If the market is open, that is a real problem;\n"
              "  if it is the overnight halt or a weekend, it is expected.")
    print("\n  If that all looks right:  python live_feed.py")
    return 0


# ------------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser(description="TeddyAI live bar feeder")
    ap.add_argument("--probe", action="store_true",
                    help="check entitlement, schema and symbol mapping, then exit")
    ap.add_argument("--seconds", type=int, default=90,
                    help="how long the probe listens (default 90)")
    ap.add_argument("--schema", default=None,
                    help="force a schema instead of negotiating")
    ap.add_argument("--run-for", type=int, default=0,
                    help="stop after N seconds; 0 = run until killed")
    ap.add_argument("--backfill", action="store_true",
                    help="close the holes in the last five days' bar files, then exit "
                         "(today's file belongs to the running feeder and is left alone)")
    a = ap.parse_args()

    if a.probe:
        return probe(a.seconds)
    if a.backfill:
        cl = historical()
        today = datetime.datetime.now(datetime.timezone.utc).date()
        backfill_gaps(cl, avail_end(cl), today)
        for i in range(1, 6):
            day = (today - datetime.timedelta(days=i)).isoformat()
            last = BarBook(day).last()
            print(f"  {day}: " + (f"{last['ts'][11:16]}Z" if last else "no bars"))
        return 0

    feed = Feed(schema=a.schema)

    def bye(*_):
        feed.stop_flag = True
    for s in (signal.SIGINT, signal.SIGTERM):
        try: signal.signal(s, bye)
        except Exception: pass

    log(f"live_feed starting -- {DATASET} {SYMBOL}")
    if a.run_for:
        import threading
        threading.Timer(a.run_for, bye).start()
    try:
        feed.run_forever()
    except KeyboardInterrupt:
        pass
    except Exception:
        log("fatal:\n" + traceback.format_exc())
        clear_status("crashed")
        return 1
    feed.book.flush()
    clear_status("stopped cleanly")
    log("live_feed stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
