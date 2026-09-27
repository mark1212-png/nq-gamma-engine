#!/usr/bin/env python3
"""
teddychart/server.py — the TeddyAI live chart service.

    set DATABENTO_API_KEY=...          (the key stays on this machine)
    python server.py                    # http://127.0.0.1:5138
    python server.py --sim              # synthetic tape, no key needed
    python server.py --probe            # what this key can stream, then exit

One process, standard library only (plus `databento` and `numpy`, which the
project already has). It serves the chart page, and over a WebSocket on the
same port it streams:

  * every NQ print, with the aggressor side, from the Databento Live gateway
    — replayed from the trading-day open on connect (Databento's intraday
    replay), so a chart opened at 09:40 has the whole session's footprint,
    delta and TPO structure, not just what arrived after the tab opened;
  * best bid/ask (mbp-1 by default), coalesced to at most 20 snapshots a second,
    which is what the DOM ladder and the liquidity heatmap are drawn from;
  * the prior sessions' 1-minute bars from the historical API (L0, no
    marginal cost), so the previous days' TPO profiles and candles exist;
  * the gamma map — flips, nodes, rails, reference — read from the SAME
    trinity.json the report is built from, and the setups read back out of
    trinity.html exactly the way the dashboard tracker reads them. Nothing
    here writes to either. If teddy.py is running, its setup states (in play,
    tp hit, stopped) are mirrored too.

WHY THE BROWSER AGGREGATES. The server sends prints, not bars. The chart builds
its own time, tick and range bars, footprints, CVD and TPO from the raw tape in
typed arrays, so switching from 1m to 500t is instant and never asks the server
for anything. The server's job is to be a thin, reliable pipe with a replay.

PRICES ARE FIXED-POINT on the wire from Databento (1 unit = 1e-9). Everything
leaving this file is a float in points. Forgetting the scale writes a plausible
number nine orders of magnitude off, so the conversion happens in exactly one
place: `px()`.

THE WIRE FORMAT (little-endian, see static/wire.js for the twin):
  0x01 TRADES  u8 type, u8 flags(1=replay), u16 pad, u32 n, f64 seq0,
               then n x { f64 ts_ms, f32 px, u32 sz, u8 side, u8[3] pad }
               seq0 is the day-sequence number of the first print, so a
               client can drop anything it already holds (a replay snapshot
               and the live fan-out can overlap by one flush)
               side: 1 = buy aggressor (lifted the ask), 2 = sell aggressor
               (hit the bid), 0 = unknown
  0x02 BOOK    u8 type, u8[7] pad, f64 ts_ms,
               10 x { f32 bid_px, u32 bid_sz }, 10 x { f32 ask_px, u32 ask_sz }
  text frames  JSON with a "t" field: hello, bars, levels, status, replay_done
"""

import os, sys, json, time, struct, socket, signal, base64, hashlib, argparse
import asyncio, threading, datetime, pathlib, traceback, collections, random, math
import urllib.request
from zoneinfo import ZoneInfo
try:
    from .flow import Flow
except ImportError:
    from flow import Flow

HERE = pathlib.Path(__file__).parent.resolve()
PROJECT = HERE.parent                       # ...\Claude Gamma Analysis
STATIC = HERE / "static"
CACHE = HERE / "cache"
LOGFILE = HERE / "server.log"

DATASET = "GLBX.MDP3"
# The contract comes from live_feed.py, which is the one place the roll is
# decided -- see the note there. This was a third hardcoded "NQ.c.0" until
# 2026-09-14, when the dashboard was pinned to the traded contract and the
# chart was NOT, because start_teddy.ps1 skips teddychart when it is already
# running: the page then drew NQU6 beside a sheet and a ladder built on NQZ6,
# 296 points apart, with nothing on screen to say which was which.
try:
    import sys as _sys
    if str(PROJECT) not in _sys.path:
        _sys.path.insert(0, str(PROJECT))
    from live_feed import SYMBOL, STYPE
except Exception:
    SYMBOL, STYPE = "NQZ6", "raw_symbol"
try:
    import stack_status                  # is the stack ticking, and is the map still today's?
except Exception:                        # the status bar just loses the token
    stack_status = None
TICK = 0.25
POINT_VALUE = 20.0
PT = ZoneInfo("America/Los_Angeles")
CT = ZoneInfo("America/Chicago")
UTC = datetime.timezone.utc

HOST, PORT = "127.0.0.1", 5138
TEDDY_URL = "http://127.0.0.1:5137"

TRADE_FLUSH_S = 0.040        # live prints are batched at 25 Hz
BOOK_FLUSH_S = 0.050         # book snapshots at 20 Hz max
STATUS_S = 1.0
LEVELS_POLL_S = 5.0
PRIOR_DAYS = 5               # sessions of 1-minute history for TPO/candles
REPLAY_CHUNK = 40_000        # prints per replay frame


# ------------------------------------------------------------------ util ---

def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:                  # cp1252 console
        print(line.encode("ascii", "replace").decode(), flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def px(v):
    """Fixed-point int64 (1e-9) -> points. The only place the scale lives."""
    return v * 1e-9


UNDEF_PRICE = 2**63 - 1


def trading_day_open(now=None):
    """
    Globex reopens at 17:00 Chicago. Everything from there to the next 16:00
    Chicago is one trading day, and it is the window the replay covers.
    Returned as an aware UTC datetime.
    """
    now = now or datetime.datetime.now(UTC)
    c = now.astimezone(CT)
    open_ = c.replace(hour=17, minute=0, second=0, microsecond=0)
    if c < open_:
        open_ -= datetime.timedelta(days=1)
    # Friday 17:00 and Saturday 17:00 open nothing: the most recent session
    # is Friday's, which opened Thursday 17:00. Sunday 17:00 opens Monday.
    while open_.weekday() in (4, 5):
        open_ -= datetime.timedelta(days=1)
    return open_.astimezone(UTC)


def trading_day_label(open_utc):
    """The trading day is named for the day it SETTLES (the next calendar day)."""
    return (open_utc.astimezone(CT) + datetime.timedelta(days=1)).date().isoformat()


SESSION_H = 23                  # 17:00 Chicago open to the 16:00 settle
SETTLE_GRACE_MIN = 30           # after the settle, before the historical API has all of it


def session_closed(day_open, now=None):
    """
    True once the trading day that opened at `day_open` has settled -- a
    weekend, a holiday, the 16:00-17:00 Chicago halt -- with SETTLE_GRACE_MIN on
    top, so that the historical API (whose ohlcv-1m end trails the clock by ten
    to fifteen minutes) holds the whole day by the time it is asked for.
    """
    now = now or datetime.datetime.now(UTC)
    return now >= day_open + datetime.timedelta(hours=SESSION_H, minutes=SETTLE_GRACE_MIN)


def ohlcv_avail_end(cl):
    """
    The last instant the historical API serves ohlcv-1m for, as an aware UTC
    datetime, or None. The per-schema end, not the dataset's top-level one: the
    two differ by ten to fifteen minutes and a request into the difference is a
    422, not an empty frame.
    """
    try:
        rng = cl.metadata.get_dataset_range(dataset=DATASET)
        sch = (rng.get("schema") or {}).get("ohlcv-1m") or {}
        end = str(sch.get("end") or rng.get("end") or "")[:19]
        return datetime.datetime.fromisoformat(end).replace(tzinfo=UTC) if end else None
    except Exception:
        return None


# ------------------------------------------------------- minimal websocket ---

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocket:
    """
    Just enough of RFC 6455 for a local, single-user chart: handshake,
    text/binary frames, ping/pong, close. No extensions, no fragmentation of
    our own frames (the client reassembles nothing; every message is one frame).
    """

    def __init__(self, reader, writer):
        self.r, self.w = reader, writer
        self.open = True
        self.lock = asyncio.Lock()

    async def send_text(self, s):
        await self._send(0x1, s.encode("utf-8"))

    async def send_bytes(self, b):
        await self._send(0x2, b)

    async def _send(self, op, payload):
        if not self.open:
            return
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | op, n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | op, 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | op, 127, n)
        async with self.lock:
            try:
                self.w.write(head + payload)
                await self.w.drain()
            except Exception:
                self.open = False

    async def recv(self):
        """Returns (opcode, payload) or None when the socket closes."""
        try:
            h = await self.r.readexactly(2)
            op, n = h[0] & 0x0F, h[1] & 0x7F
            masked = bool(h[1] & 0x80)
            if n == 126:
                n = struct.unpack("!H", await self.r.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", await self.r.readexactly(8))[0]
            mask = await self.r.readexactly(4) if masked else None
            data = await self.r.readexactly(n) if n else b""
            if mask:
                data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
            if op == 0x8:
                self.open = False
                return None
            if op == 0x9:                       # ping -> pong
                await self._send(0xA, data)
                return await self.recv()
            if op == 0xA:
                return await self.recv()
            return op, data
        except Exception:
            self.open = False
            return None

    async def close(self):
        if self.open:
            self.open = False
            try:
                self.w.write(struct.pack("!BB", 0x88, 0))
                await self.w.drain()
            except Exception:
                pass
        try:
            self.w.close()
        except Exception:
            pass


# ------------------------------------------------------------- tape store ---

class Tape:
    """
    The trading day's prints, in memory, in wire order. Held as a list of
    already-packed record chunks so a fresh client gets the replay as a
    handful of large binary frames with zero re-encoding.
    """
    REC = struct.Struct("<dfIB3x")            # 20 bytes

    def __init__(self):
        self.chunks = []                      # bytes of packed records
        self.flow = Flow()
        self.cur = bytearray()
        self.n = 0
        self.last = None                      # (ts_ms, px, sz, side)
        self.lock = threading.Lock()
        self.day_open = None

    def append(self, ts_ms, price, size, side):
        """Returns (seq, packed record); seq is the print's index in the day."""
        rec = self.REC.pack(ts_ms, price, size, side)
        with self.lock:
            self.flow.trade(ts_ms, price, size, side)
            seq = self.n
            self.cur += rec
            self.n += 1
            self.last = (ts_ms, price, size, side)
            if len(self.cur) >= REPLAY_CHUNK * self.REC.size:
                self.chunks.append(bytes(self.cur))
                self.cur = bytearray()
        return seq, rec

    def snapshot_frames(self):
        """Every print so far, framed for the wire, flagged as replay."""
        with self.lock:
            parts = list(self.chunks) + ([bytes(self.cur)] if self.cur else [])
        out, seq = [], 0
        for p in parts:
            n = len(p) // self.REC.size
            out.append(struct.pack("<BBHId", 0x01, 1, 0, n, seq) + p)
            seq += n
        return out

    def reset(self, day_open):
        with self.lock:
            self.flow.reset(day_open.timestamp() * 1000)
            self.chunks, self.cur, self.n, self.last = [], bytearray(), 0, None
            self.day_open = day_open


def frame_trades(items):
    """items: [(seq, packed record), ...] in order."""
    return struct.pack("<BBHId", 0x01, 0, 0, len(items), items[0][0]) + b"".join(r for _, r in items)


def frame_book(ts_ms, bids, asks):
    """bids/asks: lists of (px, sz), best first, padded to ten."""
    out = bytearray(struct.pack("<B7xd", 0x02, ts_ms))
    for i in range(10):
        p, s = bids[i] if i < len(bids) else (0.0, 0)
        out += struct.pack("<fI", p, s)
    for i in range(10):
        p, s = asks[i] if i < len(asks) else (0.0, 0)
        out += struct.pack("<fI", p, s)
    return bytes(out)


# ------------------------------------------------------------ gamma levels ---

class Levels:
    """
    The gamma map the chart draws, from the same files the report is built
    from. Read-only, by design: the report is INPUT to every screen, never
    output (HANDOFF). Re-read when trinity.json changes on disk.
    """

    def __init__(self):
        self.mtime = None
        self.doc = None
        self.track_at = 0.0
        self.track = None

    def _setups(self):
        html_path = PROJECT / "trinity.html"
        if not html_path.exists():
            return [], "no trinity.html"
        try:
            sys.path.insert(0, str(PROJECT))
            import setup_track                       # the dashboard's own parser
            setups = setup_track.parse_setups(html_path.read_text(encoding="utf-8"))
            return setups, ""
        except Exception as ex:
            return [], f"setups unreadable: {type(ex).__name__}: {ex}"

    def _teddy_track(self):
        """Mirror setup states from the dashboard if it happens to be up."""
        if time.time() - self.track_at < 15:
            return self.track
        self.track_at = time.time()
        try:
            with urllib.request.urlopen(TEDDY_URL + "/api/track", timeout=0.6) as r:
                self.track = json.loads(r.read().decode("utf-8"))
        except Exception:
            self.track = None
        return self.track

    def build(self):
        tj = PROJECT / "trinity.json"
        if not tj.exists():
            return dict(t="levels", ok=False, note="no trinity.json yet — generate a report")
        t = json.loads(tj.read_text(encoding="utf-8"))
        books = {}
        for name, p in (t.get("panels") or {}).items():
            g = p.get("gamma") or {}
            books[name] = dict(
                flips=[float(h) for h in (p.get("hvl") or [])],
                nodes=[[float(k), float(v)] for k, v in sorted(g.items(), key=lambda kv: float(kv[0]))],
                net_total=p.get("net_total"),
            )
        # LIVE LEVELS (2026-09-15, Mark: "lets do the chart repricing"). levels_live.py re-solves
        # the same book at the last print and the real clock every few seconds; when its file is
        # fresh and for this session, its panels replace the sheet's, so the walls and flips on
        # the chart move with the tape instead of sitting where the build left them. The sheet's
        # own numbers (rails, reference, setups) stay: only the gamma per strike and the flips
        # are repriced, which is exactly what the command center's live ladder does in the browser.
        live, live_asof, live_last, expired = False, None, None, []
        lj = PROJECT / "levels_live.json"
        try:
            if lj.exists() and time.time() - lj.stat().st_mtime < 180:
                L = json.loads(lj.read_text(encoding="utf-8"))
                if L.get("report_date") == t.get("report_date") and L.get("panels"):
                    for name, p in L["panels"].items():
                        g = p.get("gamma") or {}
                        if not g:
                            # The book EXPIRED out from under the sheet. Past its expiry instant
                            # (the 0DTE book at 20:00 UTC, 13:00 PT) model_T is 0, so levels_live
                            # writes the panel empty -- and this loop used to copy that emptiness
                            # over the plan's panel, which took the chart's gamma overlay down
                            # with it: chart.js reads L.books['0DTE'], found a book with no nodes
                            # and no flips, and drew nothing at all from 13:00 PT until the next
                            # build relabelled tomorrow's expiry as 0DTE. Keep the plan's panel
                            # and name the book expired instead. (2026-09-17)
                            expired.append(name)
                            continue
                        books[name] = dict(
                            flips=[float(h) for h in (p.get("hvl") or [])],
                            nodes=[[float(k), float(v)] for k, v in sorted(g.items(), key=lambda kv: float(kv[0]))],
                            net_total=p.get("net_total"),
                        )
                        live = True
                    if live:
                        live_asof, live_last = L.get("asof"), L.get("last")
        except Exception as ex:
            live, expired = False, []
        setups, warn = self._setups()
        track = self._teddy_track()
        if track and isinstance(track, dict):
            states = {s.get("id"): s for s in (track.get("setups") or [])}
            for s in setups:
                st = states.get(s["id"])
                if st:
                    s["state"] = st.get("state")
                    s["touches"] = st.get("touches")
        return dict(
            t="levels", ok=True,
            spot=t.get("spot"), settle=t.get("settle"), report_date=t.get("report_date"),
            hours=t.get("hours"), iv_atm=t.get("iv_atm"), em=t.get("em"),
            rail_hi=t.get("rail_hi"), rail_lo=t.get("rail_lo"),
            rail_scale=t.get("rail_scale"), rail_label=t.get("rail_label"),
            oi_vintage=t.get("oi_vintage"), ref_symbol=t.get("ref_symbol"),
            last_print=t.get("last_print"), asof=t.get("asof"),
            confluence={k: v for k, v in (t.get("confluence") or {}).items()},
            books=books, setups=setups, warn=warn,
            tracked=bool(track),
            live=live, live_asof=live_asof, live_last=live_last, expired=expired,
        )

    def poll(self):
        """Returns a fresh document when the file changed, else None."""
        tj = PROJECT / "trinity.json"
        m = tj.stat().st_mtime if tj.exists() else None
        th = PROJECT / "trinity.html"
        m2 = th.stat().st_mtime if th.exists() else None
        lj = PROJECT / "levels_live.json"
        m3 = lj.stat().st_mtime if lj.exists() else None
        key = (m, m2, m3)
        if key != self.mtime:
            self.mtime = key
            try:
                self.doc = self.build()
            except Exception as ex:
                self.doc = dict(t="levels", ok=False, note=f"{type(ex).__name__}: {ex}")
            return self.doc
        return None


# ------------------------------------------------------------ history bars ---

def prior_sessions(key, day_open, n=PRIOR_DAYS, include_current=False):
    """
    The previous n trading days as 1-minute bars [ts_ms, o, h, l, c, v], from
    the historical API, cached per trading day so a restart costs nothing.

    include_current puts the day that opened at `day_open` in as the newest
    session. That is for when it has already settled: on a weekend (or in the
    halt) the "current" trading day is Friday's, the live gateway's replay
    window is the last 24 hours and cannot reach it, and this walk used to
    start at the day before -- so nothing put Friday's candles on the chart
    from Friday's settle to Sunday's reopen, and after a Sunday restart not
    even then, because the history was loaded once and never again.

    Returns (docs, complete). complete is False when the newest day was fetched
    before the API held all of it; that day is served but not cached, and the
    caller asks again later.
    """
    import databento as db
    out = []
    cl = db.Historical(key)
    avail = ohlcv_avail_end(cl)
    complete = True
    got = 0
    d = day_open + datetime.timedelta(days=1) if include_current else day_open
    tries = 0
    while got < n and tries < n * 3:
        tries += 1
        d = d - datetime.timedelta(days=1)
        if d.astimezone(CT).weekday() in (4, 5):     # Fri/Sat 17:00 opens nothing
            continue
        start = d
        stop = d + datetime.timedelta(hours=SESSION_H)
        label = trading_day_label(start)
        f = CACHE / f"bars-{label}.json"
        if f.exists():
            rows = json.loads(f.read_text(encoding="utf-8"))
        else:
            try:
                df = cl.timeseries.get_range(dataset=DATASET, schema="ohlcv-1m",
                                             symbols=[SYMBOL], stype_in=STYPE,
                                             start=start.strftime("%Y-%m-%dT%H:%M:%S"),
                                             end=min(stop, avail or stop).strftime("%Y-%m-%dT%H:%M:%S")).to_df()
            except Exception as ex:
                log(f"  history {label}: {type(ex).__name__}: {ex}")
                continue
            rows = [[int(t.value // 1_000_000), float(r.open), float(r.high),
                     float(r.low), float(r.close), int(r.volume)] for t, r in df.iterrows()]
            if rows and (avail is None or stop <= avail):
                CACHE.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(rows), encoding="utf-8")
            elif rows:
                complete = False      # the API did not hold the whole day yet: serve it, do not cache it
        if rows:
            out.append(dict(t="bars", day=label, rows=rows))
            got += 1
    out.reverse()
    return out, complete


# ------------------------------------------------------------------ feeds ---

class Hub:
    """Fan-out: every connected chart gets the same frames."""

    def __init__(self, loop):
        self.loop = loop
        self.clients = set()
        self.status = dict(t="status", state="starting")

    def broadcast_bytes(self, b):
        for c in list(self.clients):
            asyncio.run_coroutine_threadsafe(c.send_bytes(b), self.loop)

    def broadcast_text(self, s):
        for c in list(self.clients):
            asyncio.run_coroutine_threadsafe(c.send_text(s), self.loop)


class DatabentoFeed(threading.Thread):
    """
    Two live sessions on one key: prints with replay-from-open, and the book
    live-only (a day of MBP-10 is gigabytes and the heatmap only needs what
    it can see). Callbacks do the minimum — the book callback keeps a
    reference to the newest record and nothing else, because MBP-10 arrives
    thousands of times a second and decoding each one in Python would fall
    behind inside a minute.
    """

    def __init__(self, hub, tape, key, book_on=True, book_schema="mbp-1"):
        super().__init__(daemon=True)
        self.hub, self.tape, self.key = hub, tape, key
        self.book_on = book_on
        self.book_schema = book_schema
        self.pending = collections.deque()
        self.book_rec = None
        self.book_n = 0
        self.book_error = None
        self.stop_flag = False
        self.contract = None
        self.replaying = True
        self.msgs = 0
        self.last_msg = 0.0

    # -- callbacks (databento thread) ---------------------------------------

    def on_trade(self, rec):
        m = getattr(rec, "stype_out_symbol", None)
        if m is not None:
            if self.contract and m != self.contract:
                log(f"  ROLL: {SYMBOL} {self.contract} -> {m}")
            self.contract = m
            return
        if getattr(rec, "action", None) != "T":
            return
        self.last_msg = time.time()
        self.msgs += 1
        side = rec.side
        code = 1 if side == "B" else 2 if side == "A" else 0
        ts_ms = rec.ts_event / 1e6
        self.pending.append(self.tape.append(ts_ms, px(rec.price), int(rec.size), code))

    def on_book(self, rec):
        if getattr(rec, "levels", None) is None:
            return
        self.book_rec = rec
        bids = [(px(v.bid_px), int(v.bid_sz)) for v in rec.levels if v.bid_px != UNDEF_PRICE and v.bid_sz]
        asks = [(px(v.ask_px), int(v.ask_sz)) for v in rec.levels if v.ask_px != UNDEF_PRICE and v.ask_sz]
        self.tape.flow.book(rec.ts_event / 1e6, bids, asks)
        self.book_n += 1
        self.last_msg = time.time()

    # -- thread body --------------------------------------------------------

    def watch_book(self, client):
        try:
            client.block_for_close()
        except Exception as ex:
            self.book_error = f'{type(ex).__name__}: {ex}'
            log(f'  depth failed: {self.book_error}')

    def run(self):
        import databento as db
        backoff = 5
        while not self.stop_flag:
            try:
                self.session(db)
                backoff = 5
            except Exception as ex:
                log(f"  feed disconnected: {type(ex).__name__}: {ex}")
                self.hub.status.update(state="reconnecting", error=str(ex), retry_in_s=backoff)
                self.hub.broadcast_text(json.dumps(self.hub.status))
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)

    def session(self, db):
        day_open = trading_day_open()
        now = datetime.datetime.now(UTC)
        self.tape.reset(day_open)
        self.book_rec = None
        self.book_error = None
        label = trading_day_label(day_open)
        # Intraday replay: the gateway resends everything since `start`, then
        # keeps going live. That is what gives a chart opened mid-session its
        # whole day. The gateway holds roughly the last 24 hours, so the start
        # is clamped to that (the dashboard's tick feed does the same); and once
        # the session has SETTLED -- a weekend, a holiday, the halt -- there is
        # nothing in that window to replay. The gateway does not refuse such a
        # subscription, it simply never sends a print, so the chart sat empty
        # from Friday's settle to Sunday's reopen while the log said "replay
        # from 2026-09-10 22:00". A settled day is live-only here, and its
        # candles come from the historical API instead (Server.load_history).
        closed = session_closed(day_open, now)
        start = None if closed else max(day_open, now - datetime.timedelta(hours=23))
        self.replaying = not closed
        self.hub.status.update(state="streaming" if closed else "replaying",
                               contract=self.contract, day=label, closed=closed)
        trades = db.Live(key=self.key)
        try:
            if start is None:
                trades.subscribe(dataset=DATASET, schema="trades", stype_in=STYPE, symbols=[SYMBOL])
            else:
                trades.subscribe(dataset=DATASET, schema="trades", stype_in=STYPE,
                                 symbols=[SYMBOL], start=start)
        except Exception as ex:
            log(f"  replay refused ({ex}); streaming live only")
            trades = db.Live(key=self.key)
            trades.subscribe(dataset=DATASET, schema="trades", stype_in=STYPE, symbols=[SYMBOL])
            start = None
        trades.add_callback(self.on_trade)
        trades.start()
        book = None
        if self.book_on:
            book = db.Live(key=self.key)
            try:
                book.subscribe(dataset=DATASET, schema=self.book_schema, stype_in=STYPE, symbols=[SYMBOL])
                book.add_callback(self.on_book)
                book.start()
                threading.Thread(target=self.watch_book, args=(book,), daemon=True).start()
            except Exception as ex:
                log(f"  {self.book_schema} refused ({type(ex).__name__}: {ex}); no book")
                self.book_error = f'{type(ex).__name__}: {ex}'
                book = None
        self.last_msg = time.time()
        if closed:
            since = (now - day_open).total_seconds() / 3600 - SESSION_H
            how = f"live only; the {label} session settled {since:.0f}h ago, its candles come from history"
        elif start is None:
            how = "live only"
        else:
            how = (f"replay from {start:%Y-%m-%d %H:%M} UTC"
                   + (f", clamped from the {day_open:%H:%M} open" if start != day_open else ""))
        log(f"  streaming {SYMBOL}: trades ({how})" + (f" + {self.book_schema}" if book else ""))
        try:
            while not self.stop_flag:
                time.sleep(1.0)
                quiet = time.time() - self.last_msg
                if quiet > 300:
                    raise RuntimeError(f"no messages for {quiet:.0f}s")
                # the trading day rolled under us: start over with a new replay window
                if datetime.datetime.now(UTC) - trading_day_open() < datetime.timedelta(seconds=90) \
                        and trading_day_open() != self.tape.day_open:
                    raise RuntimeError("trading day rolled")
        finally:
            for c in (trades, book):
                try:
                    if c: c.stop()
                except Exception:
                    pass

    # -- book decode (asyncio thread, at most 20 Hz) -------------------------

    def book_snapshot(self):
        rec = self.book_rec
        if rec is None:
            return None
        bids, asks = [], []
        for lv in rec.levels:
            if lv.bid_px != UNDEF_PRICE and lv.bid_sz:
                bids.append((px(lv.bid_px), int(lv.bid_sz)))
            if lv.ask_px != UNDEF_PRICE and lv.ask_sz:
                asks.append((px(lv.ask_px), int(lv.ask_sz)))
        return rec.ts_event / 1e6, bids, asks


class SimFeed(threading.Thread):
    """
    A synthetic NQ tape for testing without a key: a mean-reverting random
    walk with bursts, a ten-level book that leans with the flow, and a replay
    of the "day so far" so the chart's history path is exercised too.
    """

    def __init__(self, hub, tape, seed=7):
        super().__init__(daemon=True)
        self.hub, self.tape = hub, tape
        self.pending = collections.deque()
        self.stop_flag = False
        self.rng = random.Random(seed)
        self.p = 29600.0
        self.contract = "NQU6 (sim)"
        self.replaying = True
        self.msgs = 0
        self.last_msg = time.time()
        self.book_n = 0
        self._book = None

    def step(self, ts_ms, dt):
        r = self.rng
        drift = (29600 - self.p) * 0.0004 * dt
        self.p += drift + r.gauss(0, 0.9) * math.sqrt(max(dt, 0.02))
        self.p = round(self.p / TICK) * TICK
        n = 1 + int(r.expovariate(0.5))
        recs = []
        if r.random() < 0.004:                       # a burst: one side, one price, all at once
            side = 1 if r.random() < 0.5 else 2
            for _ in range(25 + r.randrange(40)):
                recs.append(self.tape.append(ts_ms + r.random() * 300, self.p + (TICK if side == 1 else 0),
                                             3 + int(r.expovariate(0.15)), side))
            self.msgs += len(recs)
        for _ in range(n):
            side = 1 if r.random() < 0.5 + (drift * 0.3) else 2
            sz = 1 + int(r.expovariate(0.35))
            if r.random() < 0.0025:
                sz += 40 + int(r.expovariate(0.02))
            price = self.p + (TICK if side == 1 else 0)
            recs.append(self.tape.append(ts_ms, price, sz, side))
        self.msgs += n
        return recs

    def run(self):
        day_open = trading_day_open()
        self.tape.reset(day_open)
        now = time.time() * 1000
        t = max(day_open.timestamp() * 1000, now - 6 * 3600 * 1000)
        # "the day so far", coarse: one step per 400 ms
        while t < now:
            self.step(t, 0.4)
            t += 400
        self.replaying = False
        self.hub.status.update(state="streaming", contract=self.contract,
                               day=trading_day_label(day_open))
        last = time.time()
        while not self.stop_flag:
            time.sleep(0.02 + self.rng.random() * 0.06)
            now = time.time()
            for item in self.step(now * 1000, now - last):
                self.pending.append(item)
            last = now
            self.last_msg = now
            self.book_n += 1

    def book_snapshot(self):
        r = self.rng
        bids = [(self.p - TICK * i, max(1, int(r.gauss(40 + i * 8, 15)))) for i in range(10)]
        asks = [(self.p + TICK * (i + 1), max(1, int(r.gauss(40 + i * 8, 15)))) for i in range(10)]
        if r.random() < 0.05:                       # a resting wall appears
            k = r.randrange(10)
            (bids if r.random() < 0.5 else asks)[k] = ((bids if k % 2 else asks)[k][0], 400 + r.randrange(600))
        return time.time() * 1000, bids, asks


class ReplayFeed(threading.Thread):
    """
    A real trading day, replayed at the pace of the clock.

    Pulls the day's prints (and optionally the book) from the historical API
    once, caches them under cache/, then plays them to the chart the way the
    live gateway would: everything before --from lands instantly as history,
    and from there prints are released as a virtual clock passes them, at
    --speed x real time. The chart cannot tell this from a live session,
    which is the point — it is how the screen gets judged before Monday.
    """

    def __init__(self, hub, tape, key, day, start_pt, end_pt, speed, book_schema):
        super().__init__(daemon=True)
        self.hub, self.tape, self.key = hub, tape, key
        self.day, self.start_pt, self.end_pt = day, start_pt, end_pt
        self.speed, self.book_schema = speed, book_schema
        self.pending = collections.deque()
        self.stop_flag = False
        self.contract = None
        self.replaying = True
        self.msgs = 0
        self.last_msg = time.time()
        self.book_n = 0
        self.vclock = None                # virtual "now", ms
        self._snap = None
        self.total = 0

    def now_ms(self):
        return self.vclock if self.vclock else time.time() * 1000

    # -- data --------------------------------------------------------------

    def load(self):
        import numpy as np
        d = datetime.date.fromisoformat(self.day)
        # trading day `d` opens 17:00 CT the calendar day before
        open_ct = datetime.datetime(d.year, d.month, d.day, 17, 0, tzinfo=CT) - datetime.timedelta(days=1)
        while open_ct.weekday() in (4, 5):          # a Sat/Sun date names Monday's day
            open_ct -= datetime.timedelta(days=1)
        self.day_open = open_ct.astimezone(UTC)
        close = self.day_open + datetime.timedelta(hours=23)
        CACHE.mkdir(parents=True, exist_ok=True)
        f = CACHE / f"trades-{self.day}.npz"
        if f.exists():
            z = np.load(f)
            self.ts, self.px, self.sz, self.side = z["ts"], z["px"], z["sz"], z["side"]
            self.contract = str(z["contract"]) if "contract" in z else None
            log(f"  replay: {len(self.ts):,} prints for {self.day} from cache")
        else:
            if not self.key:
                raise RuntimeError("DATABENTO_API_KEY is not set and this day is not cached yet")
            import databento as db
            cl = db.Historical(self.key)
            try:
                avail = str(cl.metadata.get_dataset_range(dataset=DATASET).get("end") or "")[:19]
                if avail and avail < close.strftime("%Y-%m-%dT%H:%M:%S"):
                    close = datetime.datetime.fromisoformat(avail).replace(tzinfo=UTC)
            except Exception:
                pass
            kw = dict(dataset=DATASET, symbols=[SYMBOL], stype_in=STYPE,
                      start=self.day_open.strftime("%Y-%m-%dT%H:%M:%S"),
                      end=close.strftime("%Y-%m-%dT%H:%M:%S"))
            try:
                cost = cl.metadata.get_cost(schema="trades", **kw)
                log(f"  replay: pulling trades for {self.day} (estimated cost ${cost:.2f})")
            except Exception:
                log(f"  replay: pulling trades for {self.day}")
            store = cl.timeseries.get_range(schema="trades", **kw)
            df = store.to_df()
            if df.empty:
                raise RuntimeError(f"no prints for {self.day} -- holiday, or not yet in the historical archive?")
            tcol = df["ts_event"] if "ts_event" in df.columns else df.index.to_series()
            self.ts = (tcol.values.astype("datetime64[ns]").astype("int64") / 1e6).astype("float64")
            self.px = df["price"].values.astype("float32")
            self.sz = df["size"].values.astype("uint32")
            sd = df["side"].astype(str).values
            self.side = np.where(sd == "B", 1, np.where(sd == "A", 2, 0)).astype("uint8")
            try:
                self.contract = str(df["symbol"].iloc[-1])
            except Exception:
                self.contract = None
            np.savez_compressed(f, ts=self.ts, px=self.px, sz=self.sz, side=self.side,
                                contract=self.contract or "")
            log(f"  replay: {len(self.ts):,} prints, cached to {f.name}")
        self.total = len(self.ts)
        self.book = None
        if self.book_schema and self.book_schema != "none":
            # The book must never take the prints down with it: a licensing
            # embargo on L2 is a reason to replay without depth, not to stop.
            try:
                self.load_book(close)
            except SystemExit:
                raise
            except Exception as ex:
                log(f"  replay: book unavailable ({type(ex).__name__}: {str(ex)[:160]}) -- replaying prints only")
                self.book = None

    def load_book(self, close):
        import numpy as np
        import databento as db
        f = CACHE / f"book-{self.book_schema}-{self.day}.npz"
        if f.exists():
            z = np.load(f)
            self.book = (z["ts"], z["px"], z["sz"])
            log(f"  replay: {len(self.book[0]):,} book snapshots from cache")
            return
        cl = db.Historical(self.key)
        kw = dict(dataset=DATASET, symbols=[SYMBOL], stype_in=STYPE,
                  start=self.day_open.strftime("%Y-%m-%dT%H:%M:%S"),
                  end=close.strftime("%Y-%m-%dT%H:%M:%S"))
        try:
            cost = cl.metadata.get_cost(schema=self.book_schema, **kw)
            size = cl.metadata.get_billable_size(schema=self.book_schema, **kw) / 1e6
            log(f"  replay: pulling {self.book_schema} for {self.day} ({size:,.0f} MB, estimated cost ${cost:.2f})")
        except Exception:
            log(f"  replay: pulling {self.book_schema} for {self.day}")
        # Fold to one snapshot per 100 ms as it streams, so a full day of
        # depth becomes a few hundred thousand rows rather than tens of millions.
        n_lv = 10 if self.book_schema == "mbp-10" else 1
        ts_l, px_l, sz_l = [], [], []
        last_bucket = None
        cur = None
        # Recent depth sits behind an embargo on some plans: the 422 names the
        # first allowed end time. Take it, and say how much of the day that is.
        try:
            stream = cl.timeseries.get_range(schema=self.book_schema, **kw)
            stream = list(stream)
        except Exception as ex:
            import re as _re
            m = _re.search(r"end time before (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", str(ex))
            if not m or m.group(1) <= kw["start"]:
                raise
            kw["end"] = m.group(1)
            log(f"  replay: {self.book_schema} is embargoed after {kw['end']} UTC on this plan -- "
                f"book will cover the day only up to then; prints are unaffected")
            stream = list(cl.timeseries.get_range(schema=self.book_schema, **kw))
        for rec in stream:
            lv = getattr(rec, "levels", None)
            if lv is None:
                continue
            b = rec.ts_event // 100_000_000
            if b != last_bucket and cur is not None:
                ts_l.append(last_bucket * 100.0); px_l.append(cur[0]); sz_l.append(cur[1])
            last_bucket = b
            p = [0.0] * 20; z = [0] * 20
            for i in range(min(n_lv, len(lv))):
                if lv[i].bid_px != UNDEF_PRICE and lv[i].bid_sz: p[i] = px(lv[i].bid_px); z[i] = int(lv[i].bid_sz)
                if lv[i].ask_px != UNDEF_PRICE and lv[i].ask_sz: p[10 + i] = px(lv[i].ask_px); z[10 + i] = int(lv[i].ask_sz)
            cur = (p, z)
        if cur is not None:
            ts_l.append(last_bucket * 100.0); px_l.append(cur[0]); sz_l.append(cur[1])
        self.book = (np.array(ts_l, "float64"), np.array(px_l, "float32"), np.array(sz_l, "uint32"))
        np.savez_compressed(f, ts=self.book[0], px=self.book[1], sz=self.book[2])
        log(f"  replay: {len(ts_l):,} book snapshots, cached to {f.name}")

    # -- playback ------------------------------------------------------------

    def pt_ms(self, hhmm):
        """'06:25' on the trading day's Pacific calendar date -> ms."""
        h, m = map(int, hhmm.split(":"))
        d = self.day_open.astimezone(PT).date()
        # RTH is on the day AFTER the 17:00 open in Pacific terms
        cal = d + datetime.timedelta(days=1)
        return datetime.datetime(cal.year, cal.month, cal.day, h, m, tzinfo=PT).timestamp() * 1000

    def run(self):
        try:
            self.load()
        except Exception as ex:
            log(f"  replay failed: {type(ex).__name__}: {ex}")
            self.hub.status.update(state="replay failed", error=str(ex))
            return
        self.tape.reset(self.day_open)
        t0 = self.pt_ms(self.start_pt)
        t1 = self.pt_ms(self.end_pt)
        import numpy as np
        i = int(np.searchsorted(self.ts, t0))
        # everything before --from is history. It goes into the tape (for
        # clients that connect later) AND into pending (for the ones already
        # waiting); the seq number on every frame makes the overlap harmless.
        for k in range(i):
            self.pending.append(self.tape.append(float(self.ts[k]), float(self.px[k]), int(self.sz[k]), int(self.side[k])))
        self.bi = int(np.searchsorted(self.book[0], t0)) if self.book else 0
        self.vclock = t0
        self.replaying = False
        self.hub.status.update(state="replay", contract=self.contract, day=self.day, replay=True,
                               speed=self.speed, replay_from=self.start_pt, replay_to=self.end_pt)
        self.hub.broadcast_text(json.dumps(dict(t="replay_done", n=self.tape.n, live=True)))
        log(f"  replay: {i:,} prints loaded as history; playing {self.start_pt}-{self.end_pt} PT at {self.speed}x")
        self.anchor_t, self.anchor_wall = t0, time.time()
        n = len(self.ts)
        while not self.stop_flag and i < n and self.vclock < t1:
            time.sleep(0.02)
            self.vclock = self.anchor_t + (time.time() - self.anchor_wall) * 1000 * self.speed
            while i < n and self.ts[i] <= self.vclock:
                self.pending.append(self.tape.append(float(self.ts[i]), float(self.px[i]), int(self.sz[i]), int(self.side[i])))
                i += 1; self.msgs += 1
            self.last_msg = time.time()
            if self.book:
                bts = self.book[0]
                while self.bi < len(bts) and bts[self.bi] <= self.vclock:
                    self.bi += 1
                if self.bi:
                    self._snap = self.bi - 1
                    self.book_n += 1
        self.hub.status.update(state="replay finished" if i >= n or self.vclock >= t1 else "stopped")
        log("  replay: finished")
        while not self.stop_flag:
            time.sleep(1)

    def set_speed(self, v):
        """Change pace without a jump: re-anchor the virtual clock at 'now'. 0 pauses."""
        v = max(0.0, min(200.0, float(v)))
        if self.vclock is not None:
            self.anchor_t, self.anchor_wall = self.vclock, time.time()
        self.speed = v
        self.hub.status.update(speed=v, state="paused" if v == 0 else "replay")
        self.hub.broadcast_text(json.dumps(self.hub.status))

    def book_snapshot(self):
        if not self.book or self._snap is None:
            return None
        k = self._snap
        p, z = self.book[1][k], self.book[2][k]
        bids = [(float(p[i]), int(z[i])) for i in range(10) if p[i]]
        asks = [(float(p[10 + i]), int(z[10 + i])) for i in range(10) if p[10 + i]]
        return float(self.book[0][k]), bids, asks


# ----------------------------------------------------------------- server ---

class Server:
    def __init__(self, feed, tape, key, sim):
        self.feed, self.tape, self.key, self.sim = feed, tape, key, sim
        self.levels = Levels()
        self.hub = None
        self.bars_cache = None
        self.history_open = None        # the trading day the history was loaded against
        self.history_include = False    # ...and whether that (settled) day was part of it
        self.history_complete = True    # False: the newest day was fetched before the API had it all

    def now_ms(self):
        f = getattr(self.feed, "now_ms", None)
        return f() if f else time.time() * 1000

    async def start(self):
        loop = asyncio.get_running_loop()
        self.hub = Hub(loop)
        self.feed.hub = self.hub
        self.feed.start()
        srv = await asyncio.start_server(self.handle, HOST, PORT)
        log(f"teddychart listening on http://{HOST}:{PORT}/  ({'SIM' if self.sim else 'Databento live'})")
        asyncio.create_task(self.pump_trades())
        asyncio.create_task(self.pump_book())
        asyncio.create_task(self.pump_status())
        asyncio.create_task(self.pump_levels())
        if self.key and not (self.sim and not isinstance(self.feed, ReplayFeed)):
            asyncio.create_task(self.load_history())
            if not isinstance(self.feed, ReplayFeed):
                asyncio.create_task(self.watch_history())
        async with srv:
            await srv.serve_forever()

    # -- pumps ---------------------------------------------------------------

    async def pump_trades(self):
        f = self.feed
        while True:
            await asyncio.sleep(TRADE_FLUSH_S)
            if not f.pending:
                continue
            recs = []
            while f.pending and len(recs) < REPLAY_CHUNK:
                recs.append(f.pending.popleft())
            if getattr(f, "replaying", False) and not self.sim:
                # replay burst: the gateway is ahead of "now" until it isn't
                last_ts = struct.unpack_from("<d", recs[-1][1], 0)[0]
                if time.time() * 1000 - last_ts < 3000:
                    f.replaying = False
                    self.hub.status.update(state="streaming")
                    self.hub.broadcast_text(json.dumps(dict(t="replay_done", n=self.tape.n)))
                    log(f"  replay caught up: {self.tape.n:,} prints")
            if self.hub.clients:
                self.hub.broadcast_bytes(frame_trades(recs))

    async def pump_book(self):
        f = self.feed
        seen = -1
        while True:
            await asyncio.sleep(BOOK_FLUSH_S)
            if not self.hub.clients or f.book_n == seen:
                continue
            seen = f.book_n
            try:
                snap = f.book_snapshot()
            except Exception:
                snap = None
            if snap:
                self.hub.broadcast_bytes(frame_book(*snap))

    def _stack(self):
        """
        The staleness token for the status bar.

        It rides the STATUS pump, not the levels pump, on purpose. Levels.poll()
        only speaks when trinity.json, trinity.html or levels_live.json change on
        disk -- and the whole point of 2026-09-16 is that NONE of them changed
        for twenty-two hours while the stack was down. A signal carried on the
        levels document would have gone silent in exactly the outage it exists
        to report. stack_status.state() caches for 5 s, so asking at 1 Hz costs
        one file read every fifth call.

        report_date comes from the document the chart actually drew, so "map N
        sessions behind" describes what is on screen rather than what is on disk.
        """
        if stack_status is None:
            return None
        try:
            doc = self.levels.doc or {}
            s = stack_status.state(report_date=doc.get("report_date") if doc.get("ok") else None)
            return {"state": s["state"], "line": s["line"], "ok": s["ok"]}
        except Exception:
            return None

    async def pump_status(self):
        f = self.feed
        last_n = 0
        while True:
            await asyncio.sleep(STATUS_S)
            st = self.hub.status
            st["stack"] = self._stack()
            st.update(t="status", contract=getattr(f, "contract", None),
                      msgs_s=f.msgs - last_n, prints=self.tape.n,
                      quiet_s=round(time.time() - f.last_msg, 1),
                      replaying=bool(getattr(f, "replaying", False)),
                      sim=self.sim and not isinstance(f, ReplayFeed), clients=len(self.hub.clients),
                      server_ms=int(self.now_ms()))
            snap = f.book_snapshot()
            st['depth'] = dict(schema=getattr(f, 'book_schema', 'synthetic'),
                               error=getattr(f, 'book_error', None),
                               state=('error' if getattr(f, 'book_error', None) else 'disabled' if not getattr(f, 'book_on', True) or getattr(f, 'book_schema', '') == 'none'
                                      else 'waiting' if snap is None else 'stale' if self.now_ms()-snap[0] > 30000 else 'receiving'),
                               last_ms=snap[0] if snap else None,
                               messages=getattr(f, 'book_n', 0))
            last_n = f.msgs
            if self.hub.clients:
                self.hub.broadcast_text(json.dumps(st))
                flow = self.tape.flow.snapshot(self.now_ms())
                flow.update(contract=getattr(f, 'contract', None), replaying=bool(getattr(f, 'replaying', False)),
                            mode='replay' if isinstance(f, ReplayFeed) else 'sim' if self.sim else 'live')
                self.hub.broadcast_text(json.dumps(flow))

    async def pump_levels(self):
        while True:
            doc = await asyncio.get_running_loop().run_in_executor(None, self.levels.poll)
            if doc is not None and self.hub.clients:
                self.hub.broadcast_text(json.dumps(doc))
            await asyncio.sleep(LEVELS_POLL_S)

    async def load_history(self):
        try:
            day_open = trading_day_open()
            replay = isinstance(self.feed, ReplayFeed)
            if replay:
                while getattr(self.feed, "day_open", None) is None and self.feed.is_alive():
                    await asyncio.sleep(0.5)
                day_open = getattr(self.feed, "day_open", None) or day_open
            # A settled "current" day (a weekend, a holiday, the halt) cannot
            # come off the live gateway's replay, so it is history -- the newest.
            include = (not replay) and session_closed(day_open)
            docs, complete = await asyncio.get_running_loop().run_in_executor(
                None, prior_sessions, self.key, day_open, PRIOR_DAYS, include)
            self.bars_cache = docs
            self.history_open, self.history_include, self.history_complete = day_open, include, complete
            log(f"  history: {len(docs)} session(s)"
                + (f", the settled {trading_day_label(day_open)} session the newest" if include else " before today")
                + ("" if complete else "; the newest is not all there yet, asking again later"))
            for c in list(self.hub.clients):
                for doc in docs:
                    await c.send_text(json.dumps(doc))
        except Exception as ex:
            log(f"  history failed: {type(ex).__name__}: {ex}")

    async def watch_history(self):
        """
        Reload the history when what it should hold changes: the trading day
        rolls (Sunday 17:00 Chicago turns Friday from "today" into a prior
        session; a weekday 17:00 does the same to the day just settled), the
        current day settles and becomes fetchable, or the newest day was
        fetched before the API had all of it. Loaded once at start and never
        again, the history on a server that came up on Sunday was still
        missing Friday on Monday morning.
        """
        while True:
            await asyncio.sleep(60)
            try:
                day_open = trading_day_open()
                if (day_open != self.history_open
                        or session_closed(day_open) != self.history_include
                        or not self.history_complete):
                    await self.load_history()
            except Exception as ex:
                log(f"  history watch: {type(ex).__name__}: {ex}")

    # -- HTTP + WS -----------------------------------------------------------

    async def handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except Exception:
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, path, _ = lines[0].split(" ", 2)
        except ValueError:
            writer.close()
            return
        hdr = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                hdr[k.strip().lower()] = v.strip()
        if hdr.get("upgrade", "").lower() == "websocket":
            await self.websocket(reader, writer, hdr)
            return
        await self.http(writer, method, path.split("?")[0])

    async def http(self, writer, method, path):
        if path == "/":
            path = "/index.html"
        if path == "/api/levels":
            body = json.dumps(self.levels.doc or self.levels.build()).encode("utf-8")
            ctype = "application/json"
        else:
            f = (STATIC / path.lstrip("/")).resolve()
            if not str(f).startswith(str(STATIC)) or not f.is_file():
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                await writer.drain(); writer.close(); return
            body = f.read_bytes()
            ctype = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
                     "css": "text/css; charset=utf-8", "json": "application/json",
                     "svg": "image/svg+xml", "png": "image/png"}.get(f.suffix[1:], "application/octet-stream")
        writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                      f"Cache-Control: no-store\r\nConnection: close\r\n\r\n").encode() + body)
        await writer.drain()
        writer.close()

    async def websocket(self, reader, writer, hdr):
        key = hdr.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
        await writer.drain()
        ws = WebSocket(reader, writer)
        f = self.feed
        try:
            await ws.send_text(json.dumps(dict(
                t="hello", symbol=SYMBOL, contract=getattr(f, "contract", None),
                tick=TICK, point_value=POINT_VALUE, tz="America/Los_Angeles",
                day_open_ms=int(self.tape.day_open.timestamp() * 1000) if self.tape.day_open else None,
                sim=self.sim, server_ms=int(self.now_ms()))))
            for doc in (self.bars_cache or []):
                await ws.send_text(json.dumps(doc))
            if self.levels.doc is None:
                self.levels.poll()
            await ws.send_text(json.dumps(self.levels.doc))
            # replay: everything so far, then join the live fan-out. Prints
            # that land between the snapshot and add() reach the client twice
            # (once here, once from the pump); the seq0 in every frame lets
            # the client drop the overlap exactly. Nothing is lost.
            for fr in self.tape.snapshot_frames():
                await ws.send_bytes(fr)
            await ws.send_text(json.dumps(dict(t="replay_done", n=self.tape.n,
                                               live=not getattr(f, "replaying", False))))
            snap = None
            try:
                snap = f.book_snapshot()
            except Exception:
                pass
            if snap:
                await ws.send_bytes(frame_book(*snap))
            self.hub.clients.add(ws)
            await ws.send_text(json.dumps(self.hub.status))
            while ws.open:
                msg = await ws.recv()
                if msg is None:
                    break
                op, data = msg
                if op == 0x1:
                    try:
                        req = json.loads(data.decode("utf-8"))
                    except Exception:
                        continue
                    if req.get("t") == "levels":
                        self.levels.mtime = None
                        doc = self.levels.poll()
                        await ws.send_text(json.dumps(doc or self.levels.doc))
                    elif req.get("t") == "speed" and hasattr(f, "set_speed"):
                        f.set_speed(req.get("v", 1))
                    elif req.get("t") == "ping":
                        await ws.send_text(json.dumps(dict(t="pong", c=req.get("c"),
                                                           server_ms=int(self.now_ms()))))
        except Exception as ex:
            log(f"  client error: {type(ex).__name__}: {ex}")
        finally:
            self.hub.clients.discard(ws)
            await ws.close()


# ------------------------------------------------------------------ probe ---

def probe(key):
    try:
        import databento as db
    except ImportError:
        print("databento is not installed:  pip install --user databento"); return 1
    if not key:
        print("DATABENTO_API_KEY is not set"); return 1
    print(f"teddychart probe  {DATASET} {SYMBOL}  databento {getattr(db, '__version__', '?')}")
    for sch in ("trades", "tbbo", "mbp-1", "mbp-10", "mbo"):
        c = db.Live(key=key)
        try:
            c.subscribe(dataset=DATASET, schema=sch, stype_in=STYPE, symbols=[SYMBOL])
            print(f"  {sch:8} YES")
        except Exception as ex:
            print(f"  {sch:8} no   {type(ex).__name__}: {str(ex)[:70]}")
        try: c.terminate()
        except Exception: pass
    c = db.Live(key=key)
    try:
        c.subscribe(dataset=DATASET, schema="trades", stype_in=STYPE, symbols=[SYMBOL],
                    start=trading_day_open())
        print(f"  replay   YES  (from {trading_day_open():%Y-%m-%d %H:%M} UTC)")
    except Exception as ex:
        print(f"  replay   no   {type(ex).__name__}: {str(ex)[:70]}")
    try: c.terminate()
    except Exception: pass
    return 0


# ------------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser(description="TeddyAI live chart service")
    ap.add_argument("--sim", action="store_true", help="synthetic tape, no key needed")
    ap.add_argument("--probe", action="store_true", help="check entitlements and exit")
    ap.add_argument("--no-book", action="store_true", help="skip the book (no DOM / heatmap)")
    ap.add_argument("--book-schema", default="mbp-1", choices=["mbp-10", "mbp-1"],
                    help="default mbp-1 is best bid/ask, included in Standard; mbp-10 requires live depth access")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--replay", metavar="YYYY-MM-DD",
                    help="replay a real trading day from the historical archive, paced to the clock")
    ap.add_argument("--from", dest="rfrom", default="06:20", help="replay start, PT clock (default 06:20)")
    ap.add_argument("--to", dest="rto", default="13:10", help="replay end, PT clock (default 13:10)")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier (default 1 = real time)")
    ap.add_argument("--replay-book", default="none", choices=["none", "mbp-1", "mbp-10"],
                    help="also replay the book (historical L2 may be billed on your plan; the log prints the estimate)")
    a = ap.parse_args()
    globals()["PORT"] = a.port
    key = os.environ.get("DATABENTO_API_KEY")
    if a.probe:
        return probe(key)
    tape = Tape()
    if a.replay:
        if not key and not (CACHE / f"trades-{a.replay}.npz").exists():
            print("DATABENTO_API_KEY is not set (the replay pulls the day from the historical API).")
            return 1
        feed = ReplayFeed(None, tape, key, a.replay, a.rfrom, a.rto, a.speed, a.replay_book)
        a.sim = True                         # no live pump semantics
    elif a.sim:
        feed = SimFeed(None, tape)
    else:
        if not key:
            print("DATABENTO_API_KEY is not set. Set it, or run with --sim.")
            return 1
        try:
            import databento  # noqa
        except ImportError:
            print("databento is not installed:  pip install --user databento")
            return 1
        feed = DatabentoFeed(None, tape, key, book_on=not a.no_book, book_schema=a.book_schema)
    srv = Server(feed, tape, key, a.sim)
    log(f"teddychart starting -- {('replay ' + a.replay) if a.replay else 'sim' if a.sim else DATASET + ' ' + SYMBOL}")
    try:
        asyncio.run(srv.start())
    except KeyboardInterrupt:
        pass
    finally:
        feed.stop_flag = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
