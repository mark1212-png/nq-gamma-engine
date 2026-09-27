"""
mm_footprint.py — order-BEHAVIOUR classifier for Databento MBO (L3) on GLBX.MDP3.

STATUS: UNTESTED HEURISTIC. Additive module; nothing in the NQ pipeline imports it.
Thresholds below are starting points, not findings. Nothing here goes into the
sheet prose until it has been replayed against real days and written up in HANDOFF.md.

What it does
------------
CME market data is anonymous — there is no firm, trader or market-maker identity in
any GLBX.MDP3 message, so this module does NOT identify participants. It sorts order
flow by how orders BEHAVE, using the full order lifecycle that MBO exposes:

  flicker   gone inside `fast_life_s`, shown below `big_size` — the ambient one-lot layer.
            On NQ this is ~88% of all passive volume, every minute, balanced bid/ask.
            It is the liquidity-provider footprint, and precisely because it is always
            there it discriminates nothing. It is measured so it can be subtracted.
  mid       between the two lifetime cutoffs, small
  resting   alive past `slow_life_s`, or refreshed (iceberg), or carried from the snapshot
  big       shown at `big_size` or more, whatever its life. big_pulled is the subset
            shown big, never filled and gone inside `fast_life_s` — liquidity that was
            displayed and withdrawn before anyone could trade against it

Cutoffs are PERCENTILES OF REAL NQ, not guesses: `mm_calib.py` derives them from
calibration sessions and writes `mm_calib.json` beside this file; the tracker loads
that file when present. The numbers in DEFAULTS are the 2026-09-04 06:25-07:25 PT
readings (p90 life, p99 life, smallest size above p99) and are overridden by the json.

plus two flow measures that need no lifetime:

  aggressive  T records (side = aggressor side), split buy / sell
  iceberg     an order whose displayed size is refreshed after being filled to zero
              (CME sends the refresh as a Modify / re-Add on the same order_id)

Everything is aggregated per price level (for the DOM ladder / bookmap) and over a
rolling time window (for the CVD / delta pane), so the existing panes can show the
tail buckets and the aggressor split without a new panel.

GLBX.MDP3 normalisation facts this relies on (Databento venue docs)
------------------------------------------------------------------
* A T record is the aggressor execution; side is the aggressor side (N for implieds).
* Each T is followed by F records for the passive orders it hit. F records DO NOT
  modify the book — the book change arrives as a separate M (partial) or C (full)
  on the same order_id, normally within the same event (same ts_event).
  So: F attributes filled volume to the order; the following C is a fill-out, not a
  cancel, when cumulative fills >= displayed size.
* order_id is venue-assigned and per-order. It links one order's lifecycle only.
* Snapshot records (flag F_SNAPSHOT) carry orders that persisted from earlier
  sessions — their add time is unknown, they are treated as resting.
* R (clear) wipes the book.
* A repriced order is attributed to the price it left the book at (its last price).

Usage
-----
    from mm_footprint import FootprintTracker
    trk = FootprintTracker()                # defaults are for NQ ticks (0.25)
    for rec in mbo_records:                 # databento MboMsg, dict or itertuples row
        trk.on_record(rec)
    trk.window_summary(now_ns)              # rolling-window split
    trk.level_table()                       # per-price aggregates

    python mm_footprint.py --selftest                       # synthetic replay
    python mm_footprint.py file.mbo.dbn.zst [--window 60]   # replay a DBN file
"""
from __future__ import annotations

import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

# ---- DBN constants ---------------------------------------------------------
PRICE_SCALE = 1e-9
F_LAST, F_TOB, F_SNAPSHOT, F_MBP, F_BAD_TS_RECV, F_MAYBE_BAD_BOOK = 128, 64, 32, 16, 8, 4
NS = 1_000_000_000

# ---- cutoffs: overridden by mm_calib.json when present (see mm_calib.py) ----
DEFAULTS = dict(
    fast_life_s=1.0,          # p90 lifetime, NQ 2026-09-04 06:25-07:25 PT
    slow_life_s=60.0,         # p99 lifetime, same slice
    big_size=2,               # smallest shown size above p99 (p99 = 1 lot)
    iceberg_refill_gap_s=0.5, # refresh must follow the fill-out within this
    window_s=60.0,            # rolling window for window_summary()
    tick=0.25,                # NQ tick, for level bucketing
)
CALIB_KEYS = ("fast_life_s", "slow_life_s", "big_size")
CALIB_PATH = __import__("pathlib").Path(__file__).with_name("mm_calib.json")


def load_calib(path=CALIB_PATH) -> Dict[str, Any]:
    """Cutoffs from mm_calib.json (only CALIB_KEYS are taken); {} if absent."""
    try:
        import json
        d = json.loads(path.read_text())
        return {k: d[k] for k in CALIB_KEYS if k in d}
    except (OSError, ValueError):
        return {}


def _get(rec: Any, name: str, default=None):
    """Field access for databento MboMsg, dict, pandas itertuples row."""
    if isinstance(rec, dict):
        return rec.get(name, default)
    return getattr(rec, name, default)


def _char(v) -> str:
    # databento python exposes action/side as str; raw ints possible from other sources
    if isinstance(v, (bytes, bytearray)):
        return v.decode()
    if isinstance(v, int):
        return chr(v)
    return str(v)


@dataclass
class Order:
    order_id: int
    side: str
    price: int              # raw int64 (1e-9 units)
    size: int               # current displayed size
    ts_add: Optional[int]   # None for snapshot orders
    ts_last: int
    n_modify: int = 0
    n_reprice: int = 0
    filled: int = 0         # cumulative filled qty
    max_size: int = 0
    refills: int = 0
    snapshot: bool = False
    ts_zero: Optional[int] = None   # when displayed size was filled to zero
    refill_qty: int = 0     # total size shown via iceberg refreshes
    pending: bool = False   # filled out; waiting to see if an iceberg refresh follows


KINDS = ("flicker", "mid", "resting", "big")


@dataclass
class Level:
    flicker: int = 0
    mid: int = 0
    resting: int = 0
    big: int = 0
    big_pulled: int = 0       # subset of big: never filled, gone inside fast_life_s
    passive_filled: int = 0
    aggr_buy: int = 0
    aggr_sell: int = 0
    iceberg_refills: int = 0
    iceberg_vol: int = 0

    def as_dict(self) -> Dict[str, int]:
        return dict(self.__dict__)


@dataclass
class Event:
    ts: int
    kind: str          # one of KINDS | 'aggr' | 'iceberg'
    side: str
    price: int
    size: int
    extra: Dict[str, Any] = field(default_factory=dict)


class FootprintTracker:
    def __init__(self, instrument_id: Optional[int] = None, use_calib: bool = True, **kw):
        bad = set(kw) - set(DEFAULTS)
        if bad:
            raise ValueError(f"unknown thresholds: {sorted(bad)}")
        self.p = {**DEFAULTS, **(load_calib() if use_calib else {}), **kw}
        self.instrument_id = instrument_id
        self.orders: Dict[int, Order] = {}
        self.levels: Dict[int, Level] = defaultdict(Level)
        self.events: Deque[Event] = deque()
        self.n_records = 0
        self.last_ts = 0
        self.first_ts: Optional[int] = None      # start of what this tracker has seen
        self._fast = int(self.p["fast_life_s"] * NS)
        self._slow = int(self.p["slow_life_s"] * NS)
        self._big = int(self.p["big_size"])
        self._ice_gap = int(self.p["iceberg_refill_gap_s"] * NS)
        self._win = int(self.p["window_s"] * NS)

    # ------------------------------------------------------------------ input
    def on_record(self, rec: Any) -> List[Event]:
        if self.instrument_id is not None and _get(rec, "instrument_id") != self.instrument_id:
            return []
        action = _char(_get(rec, "action"))
        side = _char(_get(rec, "side", "N"))
        ts = int(_get(rec, "ts_event"))
        price = int(_get(rec, "price", 0))
        size = int(_get(rec, "size", 0))
        oid = int(_get(rec, "order_id", 0))
        flags = int(_get(rec, "flags", 0) or 0)
        self.n_records += 1
        self.last_ts = ts
        if self.first_ts is None:
            self.first_ts = ts
        out: List[Event] = []

        if action == "R":
            self.orders.clear()
        elif action == "A":
            out += self._add(oid, side, price, size, ts, bool(flags & F_SNAPSHOT))
        elif action == "M":
            out += self._modify(oid, side, price, size, ts)
        elif action == "C":
            out += self._cancel(oid, ts)
        elif action == "F":
            self._fill(oid, size, ts)
        elif action == "T":
            if side in ("A", "B") and size > 0:
                lv = self.levels[price]
                if side == "B":
                    lv.aggr_buy += size
                else:
                    lv.aggr_sell += size
                out.append(Event(ts, "aggr", side, price, size))
        # 'N' carries no book change
        out += self._sweep_pending(ts)
        for ev in out:
            self.events.append(ev)
        self._prune(ts)
        return out

    # ------------------------------------------------------------- lifecycle
    def _add(self, oid, side, price, size, ts, snapshot) -> List[Event]:
        o = self.orders.get(oid)
        if o is not None and o.ts_zero is not None and o.price == price \
                and ts - o.ts_zero <= self._ice_gap:
            # re-Add on a filled-out order id: iceberg refresh
            return self._refill(o, size, ts)
        if o is not None:
            # duplicate id without a prior fill-out: close the old one, start fresh
            self._close(o, ts, cancelled=True)
        self.orders[oid] = Order(oid, side, price, size,
                                 None if snapshot else ts, ts,
                                 max_size=size, snapshot=snapshot)
        return []

    def _modify(self, oid, side, price, size, ts) -> List[Event]:
        o = self.orders.get(oid)
        if o is None:
            # modify of an order we never saw (pre-snapshot join): adopt it as snapshot-like
            self.orders[oid] = Order(oid, side, price, size, None, ts, max_size=size, snapshot=True)
            return []
        if o.ts_zero is not None and price == o.price and size > 0 and ts - o.ts_zero <= self._ice_gap:
            return self._refill(o, size, ts)
        o.n_modify += 1
        if price != o.price:
            o.n_reprice += 1
            o.price = price
        o.size = size
        o.max_size = max(o.max_size, size)
        o.ts_last = ts
        return []

    def _fill(self, oid, qty, ts) -> None:
        o = self.orders.get(oid)
        if o is None:
            return
        o.filled += qty
        o.ts_last = ts
        self.levels[o.price].passive_filled += qty
        # displayed size exhausted -> candidate for iceberg refresh
        if o.filled >= o.max_size + o.refill_qty:
            o.ts_zero = ts

    def _refill(self, o: Order, size, ts) -> List[Event]:
        o.refills += 1
        o.refill_qty += size
        o.size = size
        o.ts_zero = None
        o.pending = False
        o.ts_last = ts
        lv = self.levels[o.price]
        lv.iceberg_refills += 1
        lv.iceberg_vol += size
        return [Event(ts, "iceberg", o.side, o.price, size, {"order_id": o.order_id, "refills": o.refills})]

    def _cancel(self, oid, ts) -> List[Event]:
        o = self.orders.get(oid)
        if o is None:
            return []
        filled_out = o.filled > 0 and o.filled >= (o.max_size + o.refill_qty) * 0.999
        if filled_out:
            # CME removes a filled-out order with C; an iceberg refresh would follow on
            # the same order_id within the gap. Hold it until the gap passes.
            o.pending = True
            o.ts_zero = ts if o.ts_zero is None else o.ts_zero
            return []
        return self._close(o, ts, cancelled=True)

    def _sweep_pending(self, now: int) -> List[Event]:
        out: List[Event] = []
        for o in [o for o in self.orders.values() if o.pending and now - o.ts_zero > self._ice_gap]:
            out += self._close(o, o.ts_zero, cancelled=False)
        return out

    def flush(self, now: Optional[int] = None) -> List[Event]:
        """Close every pending (filled-out) order; call at end of a replay."""
        now = self.last_ts if now is None else now
        out: List[Event] = []
        for o in [o for o in self.orders.values() if o.pending]:
            out += self._close(o, o.ts_zero, cancelled=False)
        self.events.extend(out)
        return out

    def _close(self, o: Order, ts, cancelled: bool) -> List[Event]:
        self.orders.pop(o.order_id, None)
        # volume attributed = what was displayed in total (max shown + refills)
        vol = o.max_size + o.refill_qty
        life = None if o.ts_add is None else ts - o.ts_add
        kind = self._classify(o, life, cancelled)
        lv = self.levels[o.price]
        setattr(lv, kind, getattr(lv, kind) + vol)
        pulled = kind == "big" and cancelled and o.filled == 0 and life is not None and life <= self._fast
        if pulled:
            lv.big_pulled += vol
        return [Event(ts, kind, o.side, o.price, vol, {"pulled": pulled,
            "order_id": o.order_id, "life_s": None if life is None else life / NS,
            "filled": o.filled, "n_modify": o.n_modify, "n_reprice": o.n_reprice,
            "cancelled": cancelled, "refills": o.refills})]

    def _classify(self, o: Order, life: Optional[int], cancelled: bool) -> str:
        if o.max_size >= self._big:
            return "big"                         # size first: rare on NQ, whatever its life
        if o.refills > 0 or life is None:        # iceberg, or snapshot carry-over
            return "resting"
        if life >= self._slow:
            return "resting"
        if life <= self._fast:
            return "flicker"
        return "mid"

    # ----------------------------------------------------------------- output
    def _prune(self, now: int) -> None:
        cutoff = now - self._win
        while self.events and self.events[0].ts < cutoff:
            self.events.popleft()

    def window_summary(self, now: Optional[int] = None) -> Dict[str, Any]:
        """Rolling-window split. Volumes are contracts. Shares are of PASSIVE volume."""
        now = self.last_ts if now is None else now
        self._prune(now)
        vol = defaultdict(int)
        side_vol = {k: {"A": 0, "B": 0} for k in KINDS}
        for ev in self.events:
            if ev.kind == "aggr":
                vol["aggr_buy" if ev.side == "B" else "aggr_sell"] += ev.size
            elif ev.kind == "iceberg":
                vol["iceberg_refills"] += 1
                vol["iceberg_vol"] += ev.size
            else:
                vol[ev.kind] += ev.size
                if ev.extra.get("pulled"):
                    vol["big_pulled"] += ev.size
                if ev.side in ("A", "B"):
                    side_vol[ev.kind][ev.side] += ev.size
        passive = sum(vol[k] for k in KINDS)
        aggr = vol["aggr_buy"] + vol["aggr_sell"]

        def bal(k):  # bid minus ask, as a share of that bucket: +1 all bids, -1 all asks
            b, a = side_vol[k]["B"], side_vol[k]["A"]
            return (b - a) / (b + a) if (b + a) else 0.0

        out = {
            "window_s": self.p["window_s"],
            "passive_vol": passive,
            "aggr_buy": vol["aggr_buy"],
            "aggr_sell": vol["aggr_sell"],
            "aggr_delta": vol["aggr_buy"] - vol["aggr_sell"],
            "aggr_vs_passive": aggr / passive if passive else 0.0,
            "iceberg_refills": vol["iceberg_refills"],
            "iceberg_vol": vol["iceberg_vol"],
            "big_pulled": vol["big_pulled"],
            "resting_orders": sum(1 for o in self.orders.values() if not o.pending),
        }
        for k in KINDS:
            out[k] = vol[k]
            out[k + "_share"] = vol[k] / passive if passive else 0.0
            out[k + "_bal"] = bal(k)
        return out

    def level_table(self) -> List[Dict[str, Any]]:
        """Per-price aggregates since start (or last reset), highest price first."""
        rows = []
        for px in sorted(self.levels, reverse=True):
            d = self.levels[px].as_dict()
            passive = sum(d[k] for k in KINDS)
            d.update(price=px * PRICE_SCALE, passive=passive,
                     tail=d["resting"] + d["big"])      # what is left once flicker is subtracted
            rows.append(d)
        return rows

    def reset_levels(self) -> None:
        self.levels.clear()

    def standing(self, now: Optional[int] = None, with_levels: bool = False) -> Dict[str, Any]:
        """The STANDING book by age and size, at `now` — a state, not a flow.

        Every alive order (not pending a refill) is aged from its add; an order whose
        add predates what the tracker has seen is aged from the tracker's first record
        (a lower bound, so it only becomes `tail` once that much time has elapsed).
          tail  age >= slow_life_s        old resting interest still on the book
          mid   fast_life_s < age < slow
          big   shown size >= big_size    whatever its age
        Balances are (bid - ask) / (bid + ask) in contracts: +1 all bids, -1 all asks.
        with_levels=True also returns {price: [tail_bid, tail_ask, big_bid, big_ask]}.
        Cost is one pass over the alive orders (a few thousand on NQ) — call it per
        window or per second, not per record.
        """
        now = self.last_ts if now is None else now
        c = {k: 0 for k in ("tail_bid", "tail_ask", "mid_bid", "mid_ask", "big_bid", "big_ask",
                            "n_alive", "n_tail")}
        lv: Dict[int, List[int]] = defaultdict(lambda: [0, 0, 0, 0]) if with_levels else None
        for o in self.orders.values():
            if o.pending or o.side not in ("A", "B"):
                continue
            c["n_alive"] += 1
            born = o.ts_add if o.ts_add is not None else self.first_ts
            age = now - born if born is not None else 0
            sfx = "_bid" if o.side == "B" else "_ask"
            sz = o.size
            if sz >= self._big:
                c["big" + sfx] += sz
                if lv is not None:
                    lv[o.price][2 if o.side == "B" else 3] += sz
            if age >= self._slow:
                c["tail" + sfx] += sz
                c["n_tail"] += 1
                if lv is not None:
                    lv[o.price][0 if o.side == "B" else 1] += sz
            elif age > self._fast:
                c["mid" + sfx] += sz

        def bal(k):
            b, a = c[k + "_bid"], c[k + "_ask"]
            return (b - a) / (b + a) if (b + a) else 0.0

        out = dict(c, tail_bal=bal("tail"), mid_bal=bal("mid"), big_bal=bal("big"))
        if with_levels:
            out["levels"] = {px * PRICE_SCALE: v for px, v in lv.items()}
        return out


# =========================================================================
# Synthetic replay — checks the classifier recovers what was generated.
# =========================================================================
def _synthetic_stream(seed: int = 7):
    """Emit DBN-shaped dicts in ts order: quoters, sticky resting orders, one
    iceberg, and aggressors sweeping the book. Returns (records, truth)."""
    import random
    rnd = random.Random(seed)
    tick = int(0.25 / PRICE_SCALE)
    mid = int(20000.0 / PRICE_SCALE)
    t = 1_700_000_000 * NS
    recs: List[dict] = []
    oid = [1000]
    truth = dict(flicker=0, mid=0, resting=0, big=0, big_pulled=0, aggr_buy=0, aggr_sell=0, iceberg_refills=0)
    book: Dict[int, dict] = {}

    def emit(action, side, price, size, order_id, ts, flags=0):
        recs.append(dict(ts_event=ts, action=action, side=side, price=price,
                         size=size, order_id=order_id, flags=flags, instrument_id=1))

    def new_id():
        oid[0] += 1
        return oid[0]

    # sticky resting orders 3-6 ticks off mid, live 2 minutes
    for k in range(6):
        side = "B" if k % 2 == 0 else "A"
        px = mid - (3 + k) * tick if side == "B" else mid + (3 + k) * tick
        o = new_id(); sz = rnd.randint(8, 15)
        emit("A", side, px, sz, o, t + k * NS); truth["big"] += sz
        book[o] = dict(side=side, px=px, sz=sz, end=t + 120 * NS + k * NS)

    # pulled large orders: 40 lots, cancelled after 0.5 s unfilled
    for k in range(3):
        o = new_id(); px = mid - 4 * tick
        emit("A", "B", px, 40, o, t + 10 * NS + k * NS); truth["big"] += 40; truth["big_pulled"] += 40
        emit("C", "B", px, 40, o, t + 10 * NS + k * NS + int(0.5 * NS))

    # iceberg: 5 shown, 50 total, sits at mid+1 on the ask
    ice = new_id(); ice_px = mid + tick
    emit("A", "A", ice_px, 5, ice, t + 2 * NS); ice_left = 45

    # quoters: 1-3 lots at mid+-1..2, repriced or pulled within 0.2-1.5 s, both sides
    ts = t + 3 * NS
    for k in range(400):
        side = "B" if k % 2 == 0 else "A"
        px = mid - tick * rnd.randint(1, 2) if side == "B" else mid + tick * rnd.randint(1, 2)
        o = new_id(); sz = rnd.randint(1, 3)
        emit("A", side, px, sz, o, ts)
        life = int(rnd.uniform(0.2, 1.5) * NS)
        truth["big" if sz >= 2 else ("flicker" if life <= NS else "mid")] += sz
        if sz >= 2 and life <= NS:
            truth["big_pulled"] += sz                       # big, unfilled, gone inside fast_life_s
        if rnd.random() < 0.5:
            emit("M", side, px + (tick if side == "A" else -tick), sz, o, ts + life // 2)
        emit("C", side, px, sz, o, ts + life)
        ts += int(rnd.uniform(0.05, 0.3) * NS)

    # aggressors: buyers lift the iceberg 9 times (5 each), sellers hit a sticky bid once
    ts = t + 5 * NS
    for k in range(9):
        emit("T", "B", ice_px, 5, 0, ts); truth["aggr_buy"] += 5
        emit("F", "A", ice_px, 5, ice, ts)
        emit("C", "A", ice_px, 5, ice, ts)                 # fill-out of displayed size
        ice_left -= 5
        emit("A", "A", ice_px, 5, ice, ts + int(0.05 * NS))  # refresh (re-Add, same id)
        truth["iceberg_refills"] += 1
        ts += 8 * NS
    # last slice gets lifted and NOT refreshed: order gone
    emit("T", "B", ice_px, 5, 0, ts); truth["aggr_buy"] += 5
    emit("F", "A", ice_px, 5, ice, ts); emit("C", "A", ice_px, 5, ice, ts)
    truth["big"] += 50                                      # iceberg shows 5 at a time: big; total shown = 50

    # three one-lot orders that sit 90 s: resting
    for k in range(3):
        o = new_id(); px = mid - 8 * tick
        emit("A", "B", px, 1, o, t + 30 * NS + k * NS); truth["resting"] += 1
        emit("C", "B", px, 1, o, t + 120 * NS + k * NS)

    # a seller hits the first sticky bid for 4, order later cancels with the rest
    first = min(book); fb = book[first]
    emit("T", "A", fb["px"], 4, 0, t + 20 * NS); truth["aggr_sell"] += 4
    emit("F", "B", fb["px"], 4, first, t + 20 * NS)
    emit("M", "B", fb["px"], fb["sz"] - 4, first, t + 20 * NS)

    for o, b in book.items():
        emit("C", b["side"], b["px"], b["sz"], o, b["end"])

    recs.sort(key=lambda r: r["ts_event"])
    return recs, truth


def selftest() -> int:
    recs, truth = _synthetic_stream()
    trk = FootprintTracker(window_s=10_000, use_calib=False)   # defaults, not the json
    for r in recs:
        trk.on_record(r)
    trk.flush()
    s = trk.window_summary()
    got = dict(flicker=s["flicker"], mid=s["mid"], resting=s["resting"], big=s["big"],
               big_pulled=s["big_pulled"], aggr_buy=s["aggr_buy"], aggr_sell=s["aggr_sell"],
               iceberg_refills=s["iceberg_refills"])
    ok = True
    print(f"records replayed: {trk.n_records}")
    for k in truth:
        flag = "ok " if got[k] == truth[k] else "BAD"
        ok &= got[k] == truth[k]
        print(f"  {flag} {k:16s} truth={truth[k]:6d} got={got[k]:6d}")
    print(f"  flicker_bal = {s['flicker_bal']:+.2f}  (expect ~0, quoters were symmetric)")
    print(f"  resting orders left on book = {s['resting_orders']} (expect 0)")
    ok &= s["resting_orders"] == 0
    print("\nlevel table (top rows):")
    for row in trk.level_table()[:8]:
        print("  {price:9.2f}  flicker={flicker:4d} mid={mid:4d} resting={resting:4d} big={big:4d} "
              "(pulled {big_pulled:3d})  aggrB={aggr_buy:3d} aggrS={aggr_sell:3d} ice={iceberg_refills}".format(**row))
    print("\nSELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def replay_dbn(path: str, window_s: float, instrument_id: Optional[int]) -> None:
    import databento as db  # local only; the API key never leaves Mark's machine and no fetch happens here
    store = db.DBNStore.from_file(path)
    trk = FootprintTracker(instrument_id=instrument_id, window_s=window_s)
    next_print = None
    step = int(window_s * NS)
    for rec in store:
        trk.on_record(rec)
        if next_print is None:
            next_print = trk.last_ts + step
        if trk.last_ts >= next_print:
            s = trk.window_summary()
            print(f"{trk.last_ts}  passive={s['passive_vol']:6d} flicker={s['flicker_share']:.2f} "
                  f"mid={s['mid_share']:.2f} resting={s['resting_share']:.2f} big={s['big']:5d} "
                  f"aggrB={s['aggr_buy']:5d} aggrS={s['aggr_sell']:5d} ice={s['iceberg_refills']:3d}")
            next_print += step
    trk.flush()
    print(f"\n{trk.n_records} records; {len(trk.levels)} price levels touched")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "--selftest":
        sys.exit(selftest())
    window = 60.0
    inst = None
    if "--window" in args:
        window = float(args[args.index("--window") + 1])
    if "--instrument" in args:
        inst = int(args[args.index("--instrument") + 1])
    replay_dbn(args[0], window, inst)
