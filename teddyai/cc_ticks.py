"""
cc_ticks.py -- the command center's print feed.

One Databento Live subscription (GLBX.MDP3, NQ.c.0, schema `trades`, intraday replay
from the Globex open) running in a background thread inside teddy.py, fanned out to the
browser over Server-Sent Events at /cc/api/ticks. This is what makes the built-in chart's
forming bar and the setup tracker move per print rather than per minute.

It is INPUT only. Nothing here writes the bar cache (live_feed.py owns that) or touches the
sheet. The prints are held in memory for the current trading day and are gone on restart;
the replay refills them on reconnect.

Modes
  real    DATABENTO_API_KEY set                -> Databento Live
  sim     CC_TICKS=sim (or no key)             -> a synthetic tape around the reference,
                                                  so the page can be exercised without a key
  replay  CC_TICKS=replay:2026-09-04:06:20:5   -> a recorded day (teddychart/cache/trades-<day>.npz,
                                                  written by teddychart's own --replay) released on
                                                  a virtual clock: everything before 06:20 PT lands at
                                                  once as history, then prints at 5x real time.
                                                  Fields: day[:from HH:MM PT][:speed]
Set CC_TICKS=off to disable the feed entirely.

Prices from Databento are fixed-point int64, 1 unit = 1e-9; the scale is applied in exactly
one place, px(). Side: 'B' = buy aggressor, 'A' = sell aggressor, 'N' = unknown.

ES (2026-09-06). The same subscription also carries ES.c.0, for one purpose: the Gamma Ladders
panel re-prices the SPX and SPY books off a live S&P print (SPX = ES x the day's basis factor,
SPY = SPX x the day's SPY/SPX ratio -- the convention the QQQ ghost already uses off NQ). Only
the LAST ES print is kept, in status()['es'] = {contract, last, last_ms}; ES prints never enter
the NQ deques, the bar builder or the setup tracker. Records are routed by instrument_id from
the gateway's SymbolMappingMsg (one per subscribed symbol, sent before any data). CC_ES=off
drops ES from the subscription and the feed is byte-for-byte what it was before. A replay takes
ES from teddychart/cache/es-<day>.npz (es_replay_pull.py: 1-second closes; es-1m-<day>.npz
for 1-minute) released on the virtual clock, src = '1s' | '1m'; without that file es is null and the page says the columns hold settle.
"""
from __future__ import annotations
import os, json, time, threading, datetime, random, collections
from zoneinfo import ZoneInfo

DATASET, STYPE = "GLBX.MDP3", "continuous"
# ONE INSTRUMENT ACROSS THE STACK (2026-09-16). This fed NQ.c.0, the continuous front month
# by expiry, which in roll week is still the September contract while the bars, the sheets
# and the chart are on NQZ6 (live_feed.SYMBOL, the reference every plan is built on). The
# result on the 09-16 open: the quote band read 29,174 while the tape printed 29,466, and the
# setup tracker filled a 29,150 hold at "29,151" -- a September print 292 points under the
# December level the card was written on. The NQ leg now follows live_feed's symbol and
# symbology exactly; ES stays continuous, on its own subscription, for the cross-reference.
try:
    from live_feed import SYMBOL as SYMBOL, STYPE as NQ_STYPE
except Exception:
    SYMBOL, NQ_STYPE = "NQZ6", "raw_symbol"
ES_SYMBOL = "ES.c.0"
UTC, CT = datetime.timezone.utc, ZoneInfo("America/Chicago")
MAX_PRINTS = 400_000            # a heavy NQ day is ~250k trades; ring buffer beyond that
SNAP_PRINTS = 40_000           # what a NEW subscriber is sent up front -- see sse()


def px(v):
    """Fixed-point int64 (1e-9) -> points. The only place the scale lives."""
    return round(v * 1e-9, 2)


def trading_day_open(now=None):
    """Globex reopens 17:00 Chicago; Fri/Sat 17:00 open nothing. Aware UTC datetime."""
    now = now or datetime.datetime.now(UTC)
    c = now.astimezone(CT)
    o = c.replace(hour=17, minute=0, second=0, microsecond=0)
    if c < o:
        o -= datetime.timedelta(days=1)
    while o.weekday() in (4, 5):
        o -= datetime.timedelta(days=1)
    return o.astimezone(UTC)


class TickFeed:
    def __init__(self, log=print, mode=None, key=None, sim_anchor=None):
        self.log = log
        self.key = key or os.environ.get("DATABENTO_API_KEY")
        env = (mode or os.environ.get("CC_TICKS") or "").lower()
        self.replay = None
        if env.startswith("replay"):
            parts = env.split(":")                       # replay:2026-09-04:06:20:5  (from-time is HH:MM, two parts)
            day = parts[1] if len(parts) > 1 else None
            frm = ":".join(parts[2:4]) if len(parts) > 3 else "06:20"
            speed = float(parts[4]) if len(parts) > 4 else 1.0
            self.replay = {"day": day, "from": frm, "speed": speed}
            self.mode = "replay"
        else:
            self.mode = "off" if env == "off" else "sim" if (env == "sim" or not self.key) else "real"
        self.sim_anchor = sim_anchor                  # callable -> reference price for the simulator
        self.lock = threading.Lock()
        self.ts = collections.deque(maxlen=MAX_PRINTS)      # ms
        self.px = collections.deque(maxlen=MAX_PRINTS)
        self.sz = collections.deque(maxlen=MAX_PRINTS)
        self.side = collections.deque(maxlen=MAX_PRINTS)    # 1 buy, 2 sell, 0 n/a
        self.seq = 0                                   # total prints ever appended (for client cursors)
        self.state = {"state": "off", "mode": self.mode, "n": 0, "last": None, "last_ms": None,
                      "contract": None, "day_open_ms": None, "replaying": False, "error": None,
                      "clock_ms": None, "replay": self.replay,      # clock_ms: the virtual "now" during a replay
                      "es": None}                                    # {contract, last, last_ms} -- the last ES print, or null
        self.stop_flag = False
        self.thread = None
        self.live_only = False        # set when the gateway refuses the replay start; cleared on a new trading day
        self.es_on = (os.environ.get("CC_ES") or "").lower() != "off"
        self.nq_ids, self.es_ids = set(), set()   # instrument_id -> which symbol, from the mapping messages
        self._unknown_logged = False

    # -- lifecycle ------------------------------------------------------------------
    def start(self):
        if self.mode == "off" or self.thread:
            return self
        self.thread = threading.Thread(target=self._run, name="cc-ticks", daemon=True)
        self.thread.start()
        return self

    def _run(self):
        backoff = 5
        while not self.stop_flag:
            try:
                if self.mode == "sim":
                    self._sim()
                elif self.mode == "replay":
                    self._replay(); return               # a replay ends when the day does
                else:
                    self._live()
                    if self.stop_flag:
                        break
                    continue                            # a clean return = retry now (replay -> live-only)
                backoff = 5
            except Exception as ex:
                self.state.update(state="reconnecting", error=f"{type(ex).__name__}: {ex}")
                self.log(f"[ticks] disconnected: {type(ex).__name__}: {ex} -- retry in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)

    # -- appending ------------------------------------------------------------------
    def _append(self, ts_ms, price, size, side):
        with self.lock:
            self.ts.append(ts_ms); self.px.append(price); self.sz.append(size); self.side.append(side)
            self.seq += 1
            self.state["n"] = self.seq
            self.state["last"] = price
            self.state["last_ms"] = ts_ms

    def _reset_day(self, day_open):
        with self.lock:
            self.ts.clear(); self.px.clear(); self.sz.clear(); self.side.clear()
            self.seq = 0
            self.state.update(n=0, last=None, last_ms=None, day_open_ms=int(day_open.timestamp() * 1000))

    # -- Databento Live -------------------------------------------------------------
    def _live(self):
        import databento as db
        day_open = trading_day_open()
        if self.state.get("day_open_ms") != int(day_open.timestamp() * 1000):
            self.live_only = False                    # a new trading day: try the replay again
        self._reset_day(day_open)
        self.state.update(state="replaying", replaying=True, error=None)
        client = db.Live(key=self.key)
        # Intraday replay: the gateway resends the session since `start`, then goes live -- a page
        # opened at 09:40 gets the whole day for the tracker. The gateway only accepts a start
        # inside roughly the last 24 hours, so the start is clamped to that; and when it still
        # refuses (the error surfaces on the first read, not on subscribe -- which is why the
        # earlier try/except around subscribe never caught it) the next attempt is live-only.
        now = datetime.datetime.now(UTC)
        start = max(day_open, now - datetime.timedelta(hours=23, minutes=30))
        symbols = [SYMBOL] + ([ES_SYMBOL] if self.es_on else [])
        names = " + ".join(symbols)
        self.nq_ids, self.es_ids = set(), set()
        # two subscriptions when ES is on: NQ by the bars' symbology (a raw NQZ6 while the roll is
        # on), ES by the continuous front -- one stype_in per subscribe call
        legs = [(NQ_STYPE, [SYMBOL])] + ([(STYPE, [ES_SYMBOL])] if self.es_on else [])
        if self.live_only:
            for st, syms in legs:
                client.subscribe(dataset=DATASET, schema="trades", stype_in=st, symbols=syms)
            self.state.update(state="live", replaying=False)
            self.log(f"[ticks] streaming {names} trades, live only (replay window closed for this session)")
        else:
            for st, syms in legs:
                client.subscribe(dataset=DATASET, schema="trades", stype_in=st, symbols=syms, start=start)
            self.log(f"[ticks] streaming {names} trades (replay from {start:%Y-%m-%d %H:%M} UTC"
                     + (f", clamped from the {day_open:%H:%M} open" if start != day_open else "") + ")")
        last_msg = time.time()
        try:
            it = iter(client)
            rec = next(it)
        except StopIteration:
            raise RuntimeError("stream ended before the first record")
        except Exception as ex:
            if "start" in str(ex).lower() and not self.live_only:
                self.live_only = True
                self.log(f"[ticks] replay refused ({ex}); switching to live only")
                return                                  # _run loops straight back in, no backoff
            raise
        for rec in self._chain(rec, it):
            if self.stop_flag:
                break
            m = getattr(rec, "stype_out_symbol", None)
            if m is not None:                          # SymbolMappingMsg: which contract NQ.c.0 (and ES.c.0) is today
                iid = getattr(rec, "instrument_id", None)
                if self._is_es(rec, m):
                    self.es_ids.add(iid)
                    es = self.state.get("es") or {}
                    if es.get("contract") and m != es["contract"]:
                        self.log(f"[ticks] ES ROLL {es['contract']} -> {m}")
                    self.state["es"] = {"contract": m, "last": es.get("last"), "last_ms": es.get("last_ms")}
                    continue
                self.nq_ids.add(iid)
                if self.state["contract"] and m != self.state["contract"]:
                    self.log(f"[ticks] ROLL {self.state['contract']} -> {m}")
                self.state["contract"] = m
                continue
            if getattr(rec, "action", None) != "T":
                continue
            last_msg = time.time()
            ts_ms = rec.ts_event / 1e6
            if self.es_on:
                iid = getattr(rec, "instrument_id", None)
                if iid in self.es_ids:                 # ES: keep the last print only, never in the NQ deques
                    es = self.state.get("es") or {}
                    self.state["es"] = {"contract": es.get("contract"), "last": px(rec.price), "last_ms": ts_ms}
                    continue
                if iid not in self.nq_ids and self.es_ids:   # a print for an instrument neither mapping named: drop, say so once
                    if not self._unknown_logged:
                        self._unknown_logged = True
                        self.log(f"[ticks] print for unmapped instrument_id {iid} dropped (NQ {sorted(self.nq_ids)}, ES {sorted(self.es_ids)})")
                    continue
            side = 1 if rec.side == "B" else 2 if rec.side == "A" else 0
            self._append(ts_ms, px(rec.price), int(rec.size), side)
            # the replay is over when a print is within a few seconds of the wall clock
            if self.state["replaying"] and time.time() * 1000 - ts_ms < 5000:
                self.state.update(state="live", replaying=False)
                self.log(f"[ticks] replay done, {self.seq:,} prints; live")
        raise RuntimeError("stream ended")

    @staticmethod
    def _chain(first, it):
        yield first
        yield from it

    def _is_es(self, rec, out_symbol) -> bool:
        """Which symbol a SymbolMappingMsg belongs to: the input symbol when the record carries it,
        else the contract root (ESU6 / NQU6). With ES off nothing is ES, whatever the record says."""
        if not self.es_on:
            return False
        sin = getattr(rec, "stype_in_symbol", None) or ""
        if sin:
            return sin == ES_SYMBOL
        return str(out_symbol).startswith("ES")

    # -- simulator ------------------------------------------------------------------
    def _sim(self):
        day_open = trading_day_open()
        self._reset_day(day_open)
        anchor = None
        try: anchor = float(self.sim_anchor()) if self.sim_anchor else None
        except Exception: anchor = None
        p = anchor or 29600.0
        self.state.update(state="replaying", replaying=True, contract="SIM", error=None)
        # a plausible day so far: one print every ~2 s from the open to now, then live at ~3/s
        now_ms = time.time() * 1000
        t = max(day_open.timestamp() * 1000, now_ms - 6 * 3600e3)
        rng = random.Random(7)
        while t < now_ms:
            p = round((p + rng.gauss(0, 0.9)) * 4) / 4
            self._append(t, p, rng.choice([1, 1, 1, 2, 2, 3, 5, 8]), rng.choice([1, 2]))
            t += rng.uniform(800, 3200)
        self.state.update(state="live", replaying=False)
        self.log(f"[ticks] SIM tape: {self.seq:,} prints seeded around {p:,.2f}; live")
        es = 7750.0                                    # a synthetic ES beside the synthetic NQ -- no relation between them is claimed
        while not self.stop_flag:
            time.sleep(rng.uniform(0.15, 0.6))
            p = round((p + rng.gauss(0, 0.6)) * 4) / 4
            self._append(time.time() * 1000, p, rng.choice([1, 1, 2, 3, 5]), rng.choice([1, 2]))
            if self.es_on:
                es = round((es + rng.gauss(0, 0.15)) * 4) / 4
                self.state["es"] = {"contract": "SIM", "last": es, "last_ms": time.time() * 1000}

    # -- recorded day (teddychart's trades cache) ------------------------------------
    def _replay(self):
        import numpy as np
        r = self.replay
        root = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(root, "teddychart", "cache", f"trades-{r['day']}.npz")
        if not os.path.exists(path):
            self.state.update(state="reconnecting", error=f"no {os.path.relpath(path, root)} -- run teddychart once with "
                              f"--replay {r['day']} to pull and cache that day's trades")
            self.log(f"[ticks] replay: {self.state['error']}")
            return
        z = np.load(path)
        ts, pxs, szs, sides = z["ts"], z["px"], z["sz"], z["side"]
        # ES 1-minute closes beside the NQ tape (es_replay_pull.py); without the file the SPX/SPY columns hold settle
        es_ts, es_px, es_contract, es_res, ei = None, None, "", "1m", 0
        cands = [os.path.join(root, "teddychart", "cache", f"es-{r['day']}.npz"),        # 1-second closes (preferred)
                 os.path.join(root, "teddychart", "cache", f"es-1m-{r['day']}.npz")]     # 1-minute closes
        es_path = next((p for p in cands if os.path.exists(p)), None) if self.es_on else None
        if es_path:
            ze = np.load(es_path)
            es_ts, es_px, es_contract = ze["ts"], ze["close"], str(ze["contract"])
            es_res = str(ze["res"]) if "res" in ze.files else "1m"
        elif self.es_on:
            self.log(f"[ticks] replay: no {os.path.relpath(cands[0], root)} -- SPX/SPY columns stay at settle; "
                     f"python es_replay_pull.py --day {r['day']} --fetch makes it")

        def es_to(virtual_ms):
            nonlocal ei
            if es_ts is None:
                return
            moved = False
            while ei < len(es_ts) and es_ts[ei] <= virtual_ms:
                ei += 1; moved = True
            if moved:
                self.state["es"] = {"contract": es_contract, "last": float(es_px[ei - 1]), "last_ms": float(es_ts[ei - 1]), "src": es_res}
        # from-time is Pacific on the replayed day
        h, m = (int(x) for x in r["from"].split(":"))
        day = datetime.date.fromisoformat(r["day"])
        pt = ZoneInfo("America/Los_Angeles")
        start_ms = datetime.datetime(day.year, day.month, day.day, h, m, tzinfo=pt).timestamp() * 1000
        day_open_ms = float(ts[0]) if len(ts) else start_ms
        with self.lock:
            self.ts.clear(); self.px.clear(); self.sz.clear(); self.side.clear(); self.seq = 0
            self.state.update(n=0, last=None, last_ms=None, day_open_ms=int(day_open_ms), contract=f"REPLAY {r['day']}",
                              state="replaying", replaying=True, error=None, clock_ms=int(start_ms))
        i = 0
        n = len(ts)
        while i < n and ts[i] < start_ms:                # history: everything before the from-time, at once
            self._append(float(ts[i]), float(pxs[i]), int(szs[i]), int(sides[i])); i += 1
        es_to(start_ms)
        self.log(f"[ticks] REPLAY {r['day']}: {i:,} prints loaded to {r['from']} PT; releasing {n - i:,} at {r['speed']:g}x"
                 + (f"; ES {es_contract} {es_res} closes, {len(es_ts):,} bars" if es_ts is not None else ""))
        self.state.update(state="live", replaying=False)
        wall0 = time.time() * 1000
        speed = r["speed"]
        while i < n and not self.stop_flag:
            if r["speed"] != speed:                      # set_speed(): re-anchor so the virtual clock is continuous
                start_ms = start_ms + (time.time() * 1000 - wall0) * speed
                wall0 = time.time() * 1000; speed = r["speed"]
            virtual = start_ms + (time.time() * 1000 - wall0) * speed
            self.state["clock_ms"] = int(virtual)
            while i < n and ts[i] <= virtual:
                self._append(float(ts[i]), float(pxs[i]), int(szs[i]), int(sides[i])); i += 1
            es_to(virtual)
            time.sleep(0.05)
        self.state.update(state="live", error=None)
        self.log(f"[ticks] REPLAY {r['day']}: done, {self.seq:,} prints")
        while not self.stop_flag:                        # hold the day on screen
            time.sleep(1)

    def set_speed(self, speed: float):
        """Replay only. The loop re-anchors on the next tick; the state's replay.speed is what the chip shows."""
        if self.replay: self.replay["speed"] = float(speed)

    # -- reading --------------------------------------------------------------------
    def since(self, cursor):
        """Prints appended after `cursor` (a seq value), as parallel lists, plus the new cursor."""
        with self.lock:
            n = len(self.ts)
            first_seq = self.seq - n                    # seq of ts[0]
            start = max(0, cursor - first_seq)
            if start >= n:
                return [], [], [], [], self.seq
            ts = list(self.ts)[start:]; pxs = list(self.px)[start:]; szs = list(self.sz)[start:]; sd = list(self.side)[start:]
            return ts, pxs, szs, sd, self.seq

    def status(self):
        s = dict(self.state)
        s["age_s"] = round(time.time() - s["last_ms"] / 1000, 1) if s.get("last_ms") else None
        return s

    def sse(self, flush_ms=250):
        """A generator for a text/event-stream response: a snapshot, then batches.

        THE SNAPSHOT IS THE TAIL OF THE RING BUFFER, NOT ALL OF IT. This opened at cursor 0,
        which sent every print the buffer held. On 2026-09-17 that was 328,483 prints -- 8.3 MB
        in one `data:` line, re-sent on every reconnect, because `retry: 3000` makes a failed
        connection repeat it every three seconds. The page JSON.parses that single string and
        then folds all of it against every card in one synchronous loop (TAPE.add in
        command_center.html), so the tab locks up and the tape reads as dead while the feed
        behind it is a second fresh. The buffer was that large because the thread had restarted
        mid-session and replayed from the PREVIOUS session's 22:00 UTC reopen.

        40,000 prints is about 1 MB and roughly the last hour of a busy tape. Anything older
        than the tape covers already comes from the bars API -- chartBars() splices hist in
        below the tape's first print -- so a shorter snapshot costs resolution on old bars,
        not correctness. (2026-09-17)
        """
        cursor = max(0, self.seq - SNAP_PRINTS)
        yield "retry: 3000\n\n"
        while True:
            ts, pxs, szs, sd, cursor = self.since(cursor)
            if ts:
                # compact: [ [ts_ms...], [px...], [sz...], [side...] ] -- ~30 bytes a print as text
                yield "event: prints\ndata: " + json.dumps([[round(t) for t in ts], pxs, szs, sd], separators=(",", ":")) + "\n\n"
            yield "event: status\ndata: " + json.dumps(self.status(), separators=(",", ":")) + "\n\n"
            time.sleep(flush_ms / 1000)
