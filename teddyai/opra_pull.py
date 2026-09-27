#!/usr/bin/env python3
"""
opra_pull.py -- the OPRA equity-options ingest: SPX / SPXW / QQQ / SPY chains
in the shape trinity_db.py already consumes, as a CROSS-REFERENCE against the
NQ book. It is additive: nothing in databento_pull.py, nqvol.py or trinity_db.py
changes, and it writes under data/opra/ so nothing that globs data/<date>-chain.json
(autopull, verify_pull, backtest --pull) can pick an equity chain up by accident.

    python opra_pull.py --estimate --date 2026-09-03 --roots SPX     (free)
    python opra_pull.py --fetch    --date 2026-09-03 --roots SPX
    python opra_pull.py --selftest                                   (no network, no key)

RUN --estimate FIRST. metadata.get_cost / get_billable_size are free. This
account prices every request at $0.0000 (flat rate), so the gate here is SIZE:
--fetch refuses to run past --max-mb (default 200) rather than fetching blind.

WHAT IS DIFFERENT FROM THE NQ PULL, and why (each of these was measured on
2026-09-06, opra_probe.py .. opra_probe5.py; do not re-probe):

  * THERE IS NO SETTLEMENT PRICE. stat_type 3 does not exist on OPRA. The
    price anchor is the end-of-day NBBO built from stat 7 (lowest offer) and
    stat 8 (highest bid), which arrive PER EXCHANGE: bid = max over exchanges,
    offer = min over exchanges, anchor = the mid. That is a weaker anchor than
    CME's settlement and every file says so in `price_basis`. stat 11 (close)
    is 0 on illiquid contracts and is recorded, never used.
  * INT64_MAX (9223372036854775807) IS DATABENTO'S UNDEFINED SENTINEL. It sits
    in the price field of every open-interest record and the quantity field of
    every price record. Filtered by value on the way in; the writer asserts
    that nothing within a factor of 1e8 of it survives into the chain.
  * OPEN INTEREST ARRIVES ONCE PER LISTING EXCHANGE, all records carrying the
    same consolidated figure: ~18 per contract on SPY/QQQ (multi-listed), ONE
    on SPX (Cboe-exclusive -- measured 2026-09-03: 9,126 records for 9,126
    contracts). Summing inflates by the number of exchanges that list the
    strike. Deduped per instrument by taking the SET of quantities; the set
    must have one member, and every instrument where it does not is counted
    and reported (latest ts_event wins there).
  * EVERY PRICE STAT ARRIVES TWICE ON SPX, at 20h and 21h UTC (measured
    2026-09-03). SPX trades to 16:15 ET and then a curb session to 17:00, so
    the second record is a different time. Quotes are bucketed by ts_event
    minute and the anchor is the bucket nearest the 16:00 cash close; the
    other bucket's mid is kept as `alt_mid` and its distance reported, never
    blended into the NBBO.
  * THE FORWARD COMES FROM PUT-CALL PARITY per expiry, from the tight ATM
    pairs -- not from an index level. C - P = D (F - K): the slope gives the
    discount factor D, the intercept over the slope gives F. Dividends and
    rates come for free. The IV solve runs on mid / D so it is consistent with
    nqvol's undiscounted Black-76.
  * SETTLEMENT STYLE IS A RULE ON THE ROOT, WITH A CHECK. SPX is AM-settled:
    it expires at the 09:30 ET open on the third Friday, so its tau differs
    materially from the SPXW weekly beside it. SPXW, SPY and QQQ are PM. The
    rule is coded, not inferred from the date; an SPX expiration that is not a
    third Friday (or the Thursday before a Friday holiday) is EXCLUDED and
    reported, not guessed at.
  * TWO LEGS, like NQ, because the two numbers have different vintages: the
    quotes are D's, the open interest OPRA carries on D is OCC's figure as of
    D-1's close. The OI leg therefore reads the NEXT session's window. The
    first fetch pulls stat 9 from both days and prints the publication clock
    so the choice is measured rather than assumed; `oi_vintage` records it.
  * --live-oi (2026-09-06): OPRA publishes OI once, at 10:30 UTC on D+1 --
    no evening preliminary -- but the historical endpoint serves complete
    days only, so a same-morning pull cannot see it. The live gateway can:
    a replay of the statistics schema from D+1 00:00Z carries the wave, and
    the stream's SymbolMappingMsg records map instrument_id to the OSI
    symbol. Refused, empty or outside the 24h window -> the historical leg,
    then the STALE fallback. The plan covers OPRA live (probed 2026-09-06).
  * QQQ and SPY are AMERICAN (decision 2026-09-06, step 3). amer.py prices
    the American and the European option on one CRR tree; the difference --
    the early-exercise premium -- comes off the mid, and the anchor stored is
    (mid - EEP)/D, so nqvol's unchanged Black-76 solve returns an American-
    consistent vol. The parity forward is refitted on EEP-corrected mids
    (American parity is an inequality). Rate, yield per expiry, spot proxy and
    per-contract EEP are stamped; the report prints the vol shift by
    moneyness. --european-only skips all of that and stamps the bias.
  * BOTH SIDES ARE SOLVED at every strike, like NQ (Mark's call, 2026-09-06).
    The report prints the call-vs-put IV gap at strikes where both solve, so
    whether ITM mids are parity-consistent is visible in the data.

Time is nqvol's business-time clock, unchanged. For the 16:00 ET solve the
intra-session profile enters only through the AM-settled monthly (T to the
open = the overnight share, 0.454 sessions); a PM expiry N sessions out is N
units whatever the profile. equity_clock.py measures the overnight/RTH split
on the cash ETFs; the file records which clock it used.

Written 2026-09-06 (build step 1). See HANDOFF.md "OPRA cross-reference".
"""

import os, sys, json, math, argparse, datetime
from math import log, sqrt, exp
from pathlib import Path

import nqvol      # the one implementation of time, vol and Greeks

DATASET = "OPRA.PILLAR"
INT64_MAX = 9223372036854775807
INT32_MAX = 2147483647
MULTIPLIER = 100                 # contract multiplier, all four equity roots
PRICE_SCALE = 1e-9               # Databento fixed-point

# Root -> how it settles and how it exercises. `settle` is the rule the expiry
# instant is built from; `exercise` is what gates the fetch until step 3.
# `scale` is what trinity_db.py / nqvol.py read from the chain file instead of
# their NQ constants (2026-09-06): the strike grid the basis-shifted mass is
# bucketed to, the flip sweep in points either side of the reference, the
# log-moneyness cap, and the premium floor in points below which an inversion
# is noise. SPX/SPXW mirror NQ's proportions at SPX's level (5 points is 0.06%,
# +-400 is 5%); SPY/QQQ are placeholders until step 3 makes the call.
ROOTS = {
    "SPX":  dict(parent="SPX.OPT",  underlying="SPX", exercise="european", settle="AM",
                 scale=dict(grid=5.0, hvl_sweep=[-400, 400, 1.0], moneyness_cap=0.25, min_premium=1.0)),
    "SPXW": dict(parent="SPXW.OPT", underlying="SPX", exercise="european", settle="PM",
                 scale=dict(grid=5.0, hvl_sweep=[-400, 400, 1.0], moneyness_cap=0.25, min_premium=1.0)),
    "QQQ":  dict(parent="QQQ.OPT",  underlying="QQQ", exercise="american", settle="PM",
                 scale=dict(grid=1.0, hvl_sweep=[-40, 40, 0.1], moneyness_cap=0.25, min_premium=0.10)),
    "SPY":  dict(parent="SPY.OPT",  underlying="SPY", exercise="american", settle="PM",
                 scale=dict(grid=1.0, hvl_sweep=[-40, 40, 0.1], moneyness_cap=0.25, min_premium=0.10)),
}
STAT_NAMES = {1: "open", 2: "indic open", 3: "settlement", 4: "low", 5: "high",
              6: "volume", 7: "lowest offer", 8: "highest bid", 9: "open interest",
              10: "fixing", 11: "close", 12: "net change", 13: "vwap"}
STAT_OFFER, STAT_BID, STAT_OI, STAT_CLOSE, STAT_VOLUME = 7, 8, 9, 11, 6

PRICE_BASIS = ("OPRA end-of-day NBBO mid: bid = max(stat 8) and offer = min(stat 7) "
               "across exchanges; OPRA carries no settlement price (stat 3). Weaker "
               "anchor than CME settlement.")
WIDE_SPREAD = 0.10               # ask-bid over mid beyond this the quote is stamped 'wide'
WAVE_GAP = 10                    # minutes; quote records closer than this are one publication wave
                                 # (measured 2026-09-03: SPX 20:23 | 21:30; QQQ/SPY 20:40 | 21:10 | 21:30-21:41)
MIN_PARITY_PAIRS = 3


# --------------------------------------------------------------- helpers ---

def client():
    try:
        import databento as db
    except ImportError:
        sys.exit("pip install --user databento")
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        sys.exit("set DATABENTO_API_KEY first")
    return db.Historical(key)


def _day_window(d):
    return (f"{d}T00:00:00", f"{d + datetime.timedelta(days=1)}T00:00:00")


def _hours_window(d, hours):
    h0, h1 = (int(x) for x in hours.split("-"))
    if h1 >= 24:
        return (f"{d}T{h0:02d}:00:00", f"{d + datetime.timedelta(days=1)}T00:00:00")
    return (f"{d}T{h0:02d}:00:00", f"{d}T{h1:02d}:00:00")


_AVAIL_END = []


def _avail_end(c):
    """Last instant OPRA.PILLAR holds. Definitions are midnight-anchored and
    the final day is incomplete, so any window past this is refused outright."""
    if not _AVAIL_END:
        try:
            _AVAIL_END.append(str(c.metadata.get_dataset_range(dataset=DATASET)
                                  .get("end") or "")[:19].replace(" ", "T"))
        except Exception:
            _AVAIL_END.append("")
    return _AVAIL_END[0]


def _clamp(c, start, end, label, what):
    a = _avail_end(c)
    if a and end > a:
        if start >= a:
            raise SystemExit(f"  [{label}] {what} window {start} -> {end} is entirely past the "
                             f"end of the dataset ({a}). Nothing to fetch yet.")
        print(f"  [{label}] {what} end clamped {end} -> {a} (dataset ends there)")
        return a, True
    return end, False


def _to_df(store, price_type="fixed"):
    """
    store.to_df() across databento-python versions. Fixed-point prices keep the
    sentinel EXACT (a scaled float turns it into 9.22e9, and one client version
    turns it into NaN); the parsers below accept all three forms anyway.
    map_symbols is off: on OPRA `parent` resolves to instrument_id only and a
    symbology join is neither needed nor guaranteed.
    """
    attempts = (dict(price_type=price_type, pretty_ts=False, map_symbols=False),
                dict(pretty_px=(price_type != "fixed"), pretty_ts=False, map_symbols=False),
                dict(pretty_ts=False), {})
    last = None
    for kw in attempts:
        try:
            return store.to_df(**kw)
        except TypeError as ex:
            last = ex
    raise last


def _price(v):
    """Fixed-point or already-scaled -> float points; None for the sentinel in
    any of its forms (raw INT64_MAX, scaled 9.22e9, NaN)."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x):
        return None
    if x >= 9.0e18 or x == INT64_MAX:
        return None
    if float(v).is_integer() and abs(x) >= 1e6:        # raw fixed-point
        x *= PRICE_SCALE
    if abs(x) >= 1e8:                                   # scaled sentinel (9.22e9)
        return None
    return x


def _qty(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or x >= INT32_MAX:                 # INT32_MAX (DBN v1) or INT64_MAX
        return None
    return int(x)


def _ts_minute(v):
    """Minute of the UTC day of a ts_event (ns int, Timestamp or string); None if unknown."""
    try:
        if hasattr(v, "hour"):
            return int(v.hour) * 60 + int(v.minute)
        x = int(v)
        return (x // 60_000_000_000) % 1440
    except Exception:
        try:
            t = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return t.hour * 60 + t.minute
        except Exception:
            return None


def _hhmm(m):
    return f"{m // 60:02d}:{m % 60:02d}" if m is not None else "--:--"


def parse_osi(sym):
    """'SPX   260918C06500000' -> (root, date, 'C'|'P', strike). None if not OSI."""
    s = str(sym)
    if len(s) < 21:
        return None
    try:
        root = s[:6].strip()
        d = datetime.date(2000 + int(s[6:8]), int(s[8:10]), int(s[10:12]))
        cp = s[12]
        k = int(s[13:21]) / 1000.0
    except ValueError:
        return None
    if cp not in "CP" or not root:
        return None
    return root, d, cp, k


# ------------------------------------------------------- settlement style ---

def third_friday(y, m):
    d = datetime.date(y, m, 15)
    return d + datetime.timedelta(days=(4 - d.weekday()) % 7)


def settlement_instant(root, exp_date):
    """
    (aware UTC expiry instant, style, note). The rule is on the ROOT:
      SPX   AM-settled, 09:30 ET on the third Friday (Thursday when Friday is a
            holiday). Anything else under the SPX root is refused with a note.
      SPXW / SPY / QQQ   PM-settled at the cash close (16:00 ET, 13:00 on a
            half-day) via nqvol.close_utc. SPY/QQQ options actually trade to
            16:15 ET; the quarter hour is thin and the exercise value is the
            16:00 close, so 16:00 is used and said here.
    """
    style = ROOTS[root]["settle"]
    if style == "AM":
        tf = third_friday(exp_date.year, exp_date.month)
        if exp_date == tf and nqvol.is_session(exp_date):
            return nqvol.rth_open_utc(exp_date), "AM", None
        if (exp_date == tf - datetime.timedelta(days=1) and not nqvol.is_session(tf)
                and nqvol.is_session(exp_date)):
            return nqvol.rth_open_utc(exp_date), "AM", "third Friday is a holiday; Thursday"
        return None, "AM", (f"{exp_date} is not a third Friday -- not an AM-settled SPX "
                            f"expiry; excluded rather than guessed")
    if not nqvol.is_session(exp_date):
        return nqvol.close_utc(exp_date), "PM", f"{exp_date} is not a session"
    return nqvol.close_utc(exp_date), "PM", None


# ------------------------------------------------------------- parsing ---

def parse_defs(defs, root):
    """
    instrument_id -> dict(symbol, root, cp, strike, exp_date, exp_raw). Strike
    and expiry come from the OSI symbol, which is unambiguous, and are checked
    against the definition's own fields; the raw expiration timestamp is kept.
    """
    out, strays, unparsed, mism = {}, {}, 0, 0
    if defs is None or not len(defs):
        return out, dict(strays=strays, unparsed=0, mismatch=0)
    cols = set(defs.columns)
    iid_col = "instrument_id" if "instrument_id" in cols else None
    for i, row in enumerate(defs.itertuples(index=(iid_col is None))):
        iid = int(getattr(row, "instrument_id")) if iid_col else int(row.Index)
        sym = str(getattr(row, "raw_symbol", "") or "")
        osi = parse_osi(sym)
        if osi is None:
            unparsed += 1
            continue
        r, d, cp, k = osi
        if r != root:
            strays[r] = strays.get(r, 0) + 1
            continue
        exp_raw = getattr(row, "expiration", None)
        ks = getattr(row, "strike_price", None)
        if ks is not None:
            kd = _price(ks)
            if kd is not None and abs(kd - k) > 0.0005:
                mism += 1
        out[iid] = dict(symbol=sym, root=r, cp=cp, strike=k, exp_date=d,
                        exp_raw=str(exp_raw) if exp_raw is not None else None)
    return out, dict(strays=strays, unparsed=unparsed, mismatch=mism)


class StatRow(tuple):
    """Six legacy fields plus timestamps/actions needed for OI corrections."""
    def __new__(cls, fields, ts_event=None, ts_ref=None, update_action=1):
        obj = super().__new__(cls, fields)
        obj.ts_event = _timestamp_ns(ts_event)
        obj.ts_ref = _timestamp_ns(ts_ref)
        obj.update_action = int(update_action or 1)
        return obj


def _timestamp_ns(value):
    if value is None:
        return None
    try:
        if isinstance(value, str):
            dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=nqvol.UTC)
            return int(dt.timestamp() * 1e9)
        if hasattr(value, "value"):
            value = value.value
        elif isinstance(value, datetime.datetime):
            return int(value.timestamp() * 1e9)
        n = int(value)
        return n if 0 < n < INT64_MAX else None
    except (ValueError, TypeError, OverflowError):
        return None


def parse_stats(stats):
    """
    Normalise a statistics frame to rows of (instrument_id, publisher_id,
    stat_type, price|None, quantity|None, hour). Counts the sentinels removed.
    """
    rows, sent_p, sent_q = [], 0, 0
    if stats is None or not len(stats):
        return rows, dict(price_sentinels=0, qty_sentinels=0, records=0)
    cols = set(stats.columns)
    iid_col = "instrument_id" in cols
    for row in stats.itertuples(index=not iid_col):
        iid = int(row.instrument_id) if iid_col else int(row.Index)
        st = int(getattr(row, "stat_type"))
        p = _price(getattr(row, "price", None))
        q = _qty(getattr(row, "quantity", None))
        # the sentinel sits in the PRICE of open-interest records and the
        # QUANTITY of price records; count every undefined field either way
        if p is None:
            sent_p += 1
        if q is None:
            sent_q += 1
        rows.append(StatRow((iid, int(getattr(row, "publisher_id", 0) or 0), st, p, q,
                     _ts_minute(getattr(row, "ts_event", None))),
                     ts_event=getattr(row, "ts_event", None), ts_ref=getattr(row, "ts_ref", None),
                     update_action=getattr(row, "update_action", 1)))
    return rows, dict(price_sentinels=sent_p, qty_sentinels=sent_q, records=len(rows))


def hour_histogram(rows):
    """{stat_type: {utc_minute: n}} -- the publication clock, printed on every fetch."""
    h = {}
    for _, _, st, _, _, mn in rows:
        if mn is None:
            continue
        h.setdefault(st, {})
        h[st][mn] = h[st].get(mn, 0) + 1
    return h


def nbbo(rows, anchor_minute=None):
    """
    instrument_id -> dict(bid, ask, n_bid, n_ask, ts, close, volume, alt).

    Quotes are grouped by their ts_event MINUTE first. Within a bucket the bid
    is the HIGHEST bid across exchanges and the ask the LOWEST offer (zeros and
    sentinels excluded). The anchor is the bucket nearest the cash close --
    at or after it when there is one. Measured 2026-09-03: SPX sends every
    price stat twice, at 20h and 21h UTC, and SPX has a curb session 16:15-
    17:00 ET, so the second record is a different time; blending the two
    buckets into one max/min would pair a 16:00 bid with a 17:00 offer. The
    other buckets are kept as `alt` [(minute, mid)] so the report can say how
    far the curb quote sits from the close quote.
    """
    bk = {}
    for iid, pub, st, p, qty, mn in rows:
        if st not in (STAT_BID, STAT_OFFER, STAT_CLOSE, STAT_VOLUME):
            continue
        b = bk.setdefault(iid, {}).setdefault(mn, dict(bid=None, ask=None, n_bid=0, n_ask=0,
                                                       close=None, volume=None))
        if st == STAT_BID and p is not None and p > 0:
            b["n_bid"] += 1
            b["bid"] = p if b["bid"] is None else max(b["bid"], p)
        elif st == STAT_OFFER and p is not None and p > 0:
            b["n_ask"] += 1
            b["ask"] = p if b["ask"] is None else min(b["ask"], p)
        elif st == STAT_CLOSE and p is not None:
            b["close"] = p
        elif st == STAT_VOLUME and qty is not None:
            b["volume"] = qty if b["volume"] is None else max(b["volume"], qty)

    def _rank(mn):
        if mn is None:
            return (2, 0)
        if anchor_minute is None:
            return (0, mn)
        d = mn - anchor_minute
        return (0, d) if d >= 0 else (1, -d)          # at/after the close first, nearest first

    def _merge(into, b):
        if b["bid"] is not None and b["bid"] > 0:
            into["bid"] = b["bid"] if into["bid"] is None else max(into["bid"], b["bid"])
        if b["ask"] is not None and b["ask"] > 0:
            into["ask"] = b["ask"] if into["ask"] is None else min(into["ask"], b["ask"])
        into["n_bid"] += b["n_bid"]; into["n_ask"] += b["n_ask"]
        if into["close"] is None:
            into["close"] = b["close"]
        if b["volume"] is not None:
            into["volume"] = b["volume"] if into["volume"] is None else max(into["volume"], b["volume"])

    out = {}
    for iid, buckets in bk.items():
        # minutes into publication WAVES: a multi-listed root's 16+ exchanges
        # publish their summaries across a spread of minutes, and a bid at
        # 20:23 with its offer at 20:24 must not read as two one-sided quotes.
        # Minutes within WAVE_GAP of the previous one join its wave; the 21:30
        # re-broadcast (67 minutes later on SPX) stays its own wave.
        waves = []
        for mn in sorted(m for m in buckets if m is not None):
            if waves and mn - waves[-1][-1] <= WAVE_GAP:
                waves[-1].append(mn)
            else:
                waves.append([mn])
        if None in buckets:
            waves.append([None])
        merged = {}
        for w in waves:
            m0 = w[0]
            acc = dict(bid=None, ask=None, n_bid=0, n_ask=0, close=None, volume=None)
            for mn in w:
                _merge(acc, buckets[mn])
            merged[m0] = acc
        # the anchor is the wave nearest the close THAT CARRIES A TWO-SIDED
        # QUOTE. Measured 2026-09-03 on QQQ/SPY: the record nearest the close
        # (20:40, one per contract) is a consolidated placeholder with OHLC and
        # volume and no bid or offer; the per-exchange closing quotes arrive
        # in the 21:10 wave, re-broadcast at 21:30. Ranking by time alone
        # anchored 12,467 of 12,534 SPY contracts on the empty record.
        two_sided = lambda b: b["bid"] is not None and b["ask"] is not None
        order = sorted(merged, key=lambda mn: (0 if two_sided(merged[mn]) else 1, _rank(mn)))
        a = dict(merged[order[0]])
        a["ts"] = order[0]
        a["alt"] = []
        for mn in order[1:]:
            b = merged[mn]
            if b["bid"] is not None and b["ask"] is not None and b["bid"] <= b["ask"]:
                a["alt"].append((mn, 0.5 * (b["bid"] + b["ask"])))
            if a["close"] is None and b["close"] is not None:
                a["close"] = b["close"]
            if a["volume"] is None and b["volume"] is not None:
                a["volume"] = b["volume"]
        out[iid] = a
    return out


def open_interest(rows):
    """Latest published applicable observation per instrument, never a sum or maximum.

    A later legitimate zero or downward correction must replace the older value.
    Callers must restrict rows to their information cutoff. Full event timestamps
    are retained by parse_stats/live_oi; legacy six-tuples require chronological
    input and have only minute resolution. Conflicting values remain diagnostic.
    """
    oi, order = {}, {}
    for seq, row in enumerate(rows):
        iid, pub, st, p, qty, minute = row
        if st != STAT_OI:
            continue
        action = getattr(row, "update_action", 1)
        if action != 2 and (qty is None or qty < 0):
            continue
        event = getattr(row, "ts_event", None)
        rank = (event if event is not None else (minute if minute is not None else -1), seq)
        r = oi.setdefault(iid, dict(oi=None, n_records=0, values=set(), zeros=0))
        r["n_records"] += 1
        if qty == 0:
            r["zeros"] += 1
        elif qty is not None and qty > 0:
            r["values"].add(qty)
        if iid not in order or rank >= order[iid]:
            order[iid] = rank
            r["oi"] = None if action == 2 else qty
    return {iid: r for iid, r in oi.items() if r["oi"] is not None}


# --------------------------------------------------------------- parity ---

RATE_BAND = (-0.02, 0.12)        # implied continuous rate from the parity slope must sit here
UNRELIABLE_RMS = 0.001           # residual RMS over F beyond which the expiry gets no vols ...
UNRELIABLE_SPREADS = 1.5         # ... unless the pairs are quoted wider than that (LEAPS): 1.5x the median spread


def parity_forward(pairs, t_years, D_fixed=None):
    """
    pairs: [(K, C_mid, P_mid, C_spread, P_spread, tight)] for one expiry, both
    sides quoted and uncrossed; `tight` marks quality 'ok' (not 'wide').
    t_years: CALENDAR years to expiry, for the rate check only.
    D_fixed: hold the discount factor and fit F alone -- for AMERICAN roots,
    whose put-call parity is an inequality: the puts' early-exercise premium
    grows with strike and tilts the C - P line, so the slope is not a rate
    (the synthetic SPY read -13%/yr). The rate comes from the European sibling
    instead and D = exp(-r T) is imposed.
    Returns dict(F, D, n, rms, rate, note, unreliable).

    C - P = D*(F - K). A weighted least-squares line of (C - P) on K over the
    pairs nearest the money: slope = -D, intercept = D*F. Weights are 1 over
    the combined spread so a one-tick ATM pair outweighs a wide wing pair.
    Tight pairs are used when there are enough of them; wide ones only fill in.

    The guard on D is in IMPLIED-RATE terms, -ln(D)/T in RATE_BAND. The first
    real run (2026-09-03) had it as a band on D itself, (0.85, 1.01), which
    threw out the 2030 and 2031 LEAPS -- D = 0.81 and 0.77 over 4-5 years is
    an ordinary ~5% rate -- and forced D = 1 on them, printing residuals of 43
    and 938 points that were the fallback's misfit, not the quotes'. With too
    few pairs or a rate outside the band, D = 1 and the spread-weighted mean
    of K + C - P is used and said. Whatever the path, a residual RMS above
    UNRELIABLE_RMS * F marks the forward unreliable: the expiry keeps its OI
    and gets no vols, because a vol solved against a wrong forward is a wrong
    vol at every strike.
    """
    if not pairs:
        return dict(F=None, D=None, n=0, rms=None, rate=None, note="no quoted pairs", unreliable=True)
    tight = [t for t in pairs if t[5]]
    pool = tight if len(tight) >= 4 else pairs
    f0 = min(pool, key=lambda t: abs(t[1] - t[2]))[0]
    sel = []
    for band in (0.02, 0.04, 0.08, 1.0):
        sel = [t for t in pool if abs(t[0] - f0) <= band * f0]
        if len(sel) >= max(MIN_PARITY_PAIRS + 1, 4):
            break
    tick = 0.05

    def _finish(F, D, res, note):
        rms = sqrt(sum(r * r for r in res) / len(res))
        # THE GATE USES THE FIT'S OWN WEIGHTS (2026-09-18). The line is fitted
        # weighted by 1/(combined spread) -- both here and in _fallback -- and
        # was then judged by an UNWEIGHTED residual. So a fit that correctly
        # distrusted a handful of wide quotes was failed for not predicting
        # the very quotes it had been told to distrust.
        #
        # Found on SPY@2026-09-18 (5.26M contracts of OI dropped): the four
        # deepest ITM calls were quoted ~1.50 wide against 1-cent puts and
        # carried the whole residual (+1.88, +1.86, +1.28, +1.05) while every
        # strike from 756 up sat flat at -0.20. Unweighted rms 0.7955 against a
        # 0.76 limit; weighted 0.4329. They entered the fit because WIDE_SPREAD
        # screens spread OVER MID -- $1.51 on a $16.85 deep-ITM call is 9.0%,
        # under the 10% bar -- a relative test guarding an absolute quantity.
        #
        # Diffed over the whole archive before shipping: 63,394 expiry-days,
        # all four roots. 184 books gain vols, 110 lose them -- the gate gets
        # MORE discriminating in both directions, not merely looser: it stops
        # punishing wide quotes and starts catching misfits on tight ones. The
        # obvious alternative (drop pairs too wide to inform the limit) was
        # measured and is worse, +179/-244, because dropping pairs lowers the
        # median spread and so lowers the limit faster than it improves the fit.
        #
        # `rms` keeps its meaning -- the unweighted number every stored chain
        # already carries -- and `rms_w` is the one the gate reads.
        wts = [1.0 / (t[3] + t[4] + tick) for t in sel]
        rms_w = sqrt(sum(wi * r * r for wi, r in zip(wts, res)) / sum(wts))
        rate = (-log(D) / t_years) if (D and t_years > 1e-6) else None
        # unreliable when the residual exceeds BOTH 0.1% of F and 1.5x the
        # median combined spread of the pairs used: a LEAPS quoted $1 wide
        # cannot fit better than ~$1, and refusing it left those books on
        # trinity_db's 20% fallback vol (QQQ/SPY 2027-28, first real run)
        spreads = sorted(t[3] + t[4] for t in sel)
        med_spread = spreads[len(spreads) // 2] if spreads else 0.0
        limit = max(UNRELIABLE_RMS * abs(F), UNRELIABLE_SPREADS * med_spread)
        unrel = rms_w > limit
        if unrel:
            note = ((note + "; " if note else "")
                    + f"UNRELIABLE: weighted rms {rms_w:.2f} > {limit:.2f} (unweighted {rms:.2f})")
        return dict(F=round(F, 4), D=round(D, 6), n=len(sel), rms=round(rms, 4),
                    rms_w=round(rms_w, 4),
                    rate=round(rate, 5) if rate is not None else None, note=note, unreliable=unrel)

    def _fallback(note, D=1.0):
        w = [1.0 / (t[3] + t[4] + tick) for t in sel]
        F = sum(wi * (t[0] + (t[1] - t[2]) / D) for wi, t in zip(w, sel)) / sum(w)
        res = [t[1] - t[2] - D * (F - t[0]) for t in sel]
        return _finish(F, D, res, note)

    if D_fixed is not None:
        return _fallback(None, D_fixed)
    if len(sel) < MIN_PARITY_PAIRS:
        return _fallback(f"only {len(sel)} pairs; D fixed at 1")
    w = [1.0 / (t[3] + t[4] + tick) for t in sel]
    x = [t[0] for t in sel]
    y = [t[1] - t[2] for t in sel]
    sw = sum(w)
    mx = sum(wi * xi for wi, xi in zip(w, x)) / sw
    my = sum(wi * yi for wi, yi in zip(w, y)) / sw
    sxx = sum(wi * (xi - mx) ** 2 for wi, xi in zip(w, x))
    if sxx <= 0:
        return _fallback("degenerate strikes; D fixed at 1")
    b = sum(wi * (xi - mx) * (yi - my) for wi, xi, yi in zip(w, x, y)) / sxx
    a = my - b * mx
    D = -b
    if D <= 0:
        return _fallback(f"parity slope gave D={D:.4f}; D fixed at 1")
    if t_years > 0.01:
        rate = -log(D) / t_years
        if not (RATE_BAND[0] <= rate <= RATE_BAND[1]):
            return _fallback(f"parity slope implies r={rate * 100:.1f}%/yr (D={D:.4f}); D fixed at 1")
    elif not (0.99 <= D <= 1.005):
        return _fallback(f"parity slope gave D={D:.4f} on a {t_years * 365:.0f}-day expiry; D fixed at 1")
    F = a / D
    res = [yi - (a + b * xi) for xi, yi in zip(x, y)]
    return _finish(F, D, res, None)


# ---------------------------------------------------------- the chain ---

def _parity_pairs(ed, q_by_sym, qual, adjust=None):
    """[(K, C_mid, P_mid, C_spread, P_spread, tight)] for one expiry from the
    quoted, uncrossed contracts. `adjust` maps symbol -> amount to subtract
    from the mid (the early-exercise premium, on the refit)."""
    by_k = {}
    for sym, (dfn, q) in q_by_sym.items():
        if dfn["exp_date"] != ed or qual.get(sym) not in ("ok", "wide"):
            continue
        by_k.setdefault(dfn["strike"], {})[dfn["cp"]] = dict(q, sym=sym)
    pairs = []
    for k, v in by_k.items():
        if "C" not in v or "P" not in v:
            continue
        c_, p_ = v["C"], v["P"]
        cm = 0.5 * (c_["bid"] + c_["ask"]) - (adjust or {}).get(c_["sym"], 0.0)
        pm = 0.5 * (p_["bid"] + p_["ask"]) - (adjust or {}).get(p_["sym"], 0.0)
        tight = (qual.get(c_["sym"]) == "ok" and qual.get(p_["sym"]) == "ok")
        pairs.append((k, cm, pm, c_["ask"] - c_["bid"], p_["ask"] - p_["bid"], tight))
    return pairs


RATE_FALLBACK = 0.04
Q_BAND = (-0.02, 0.06)
REFIT_SHIFT = 2e-4               # refit the American parity forward when EEP moves F by more than this


def american_pass(exp_info, q_by_sym, qual, d0, close, rate_hint=None):
    """
    For an American root: strip the early-exercise premium from every quoted
    contract so nqvol's European solve returns an American-consistent vol.

    Rate: the median parity-implied rate over reliable expiries 20-400 calendar
    days out (the front's slope is fragile; the far LEAPS are wide). Spot proxy:
    the shortest reliable forward at least one day out (within 0.02% of spot).
    Yield per expiry: q = r - ln(F/S0)/T, clamped to Q_BAND. The tree runs in
    nqvol's BUSINESS time so its diffusion matches the business-time vol; the
    rate and yield are rescaled by T_cal/T_bus so the total carry and discount
    over the horizon stay calendar-correct.

    American put-call parity is an inequality, so the forward from raw mids is
    biased by the puts' premium: after the first pass the pairs are refitted on
    EEP-corrected mids and, where F moved by more than REFIT_SHIFT, the pass is
    run once more at the new forward. Fills exp_info[ed]["anchors"]:
    sym -> (anchor, eep, iv_eur, iv_amer, T_bus). Returns the stamp dict.
    """
    import amer
    live = {ed: i for ed, i in exp_info.items() if i["fwd"]["F"] is not None}
    stamp = dict(model=f"CRR {amer.N_STEPS}-step, American minus European on one tree; anchor = (mid - EEP)/D",
                 yields={}, refits={}, contracts=0, clamped_yields=0, rate_path=[])
    if not live:
        stamp["note"] = "no forwards; American pass skipped"
        return stamp
    t_cal_of = {ed: max((ed - d0).days, 0) / 365.25 for ed in live}

    def _forwards(r, adjust_by_ed):
        """F per expiry with D = exp(-r t) imposed, from (EEP-corrected) mids."""
        for ed, info in live.items():
            pairs = _parity_pairs(ed, q_by_sym, qual, adjust_by_ed.get(ed))
            f = parity_forward(pairs, t_cal_of[ed], D_fixed=math.exp(-r * t_cal_of[ed]))
            f["rate"] = round(r, 5)
            if f["F"] is not None:
                info["fwd"] = f

    def _pass(r):
        """EEP anchors for every quoted contract at the current forwards."""
        near = sorted((ed for ed in live if (ed - d0).days >= 1 and not live[ed]["fwd"]["unreliable"]))
        S0 = live[near[0]]["fwd"]["F"] if near else None
        stamp["spot_proxy"], stamp["spot_proxy_expiry"] = S0, near[0].isoformat() if near else None
        stamp["yields"], stamp["clamped_yields"], stamp["contracts"] = {}, 0, 0
        for ed, info in live.items():
            info["anchors"] = {}
            F, D = info["fwd"]["F"], info["fwd"]["D"]
            if S0 is None or info["fwd"]["unreliable"]:
                continue
            t_cal = t_cal_of[ed]
            T = nqvol.years_between(close, info["instant"])
            if T <= 0:
                continue
            q = r - math.log(F / S0) / t_cal if t_cal > 0 else 0.0
            if not (Q_BAND[0] <= q <= Q_BAND[1]):
                stamp["clamped_yields"] += 1
                q = min(max(q, Q_BAND[0]), Q_BAND[1])
            stamp["yields"][ed.isoformat()] = round(q, 5)
            scale = t_cal / T                      # business -> calendar for carry and discount
            r_, q_ = r * scale, q * scale
            for sym, (dfn, qd) in q_by_sym.items():
                if dfn["exp_date"] != ed or qual.get(sym) not in ("ok", "wide"):
                    continue
                mid = 0.5 * (qd["bid"] + qd["ask"])
                res = amer.american_anchor(mid, F, D, dfn["strike"], T, dfn["cp"] == "C", r_, q_)
                info["anchors"][sym] = res + (T,)
            stamp["contracts"] += len(info["anchors"])

    def _slope_rate(adjust_by_ed):
        """median parity-slope rate over 20-400d expiries, on corrected mids --
        only meaningful once the EEP has been taken off."""
        rs = []
        for ed in live:
            if 20 <= (ed - d0).days <= 400:
                f = parity_forward(_parity_pairs(ed, q_by_sym, qual, adjust_by_ed.get(ed)), t_cal_of[ed])
                if f["rate"] is not None and not f["unreliable"]:
                    rs.append(f["rate"])
        rs.sort()
        return rs[len(rs) // 2] if rs else None

    # the rate: European sibling if we have one, else iterate from the fallback
    if rate_hint is not None:
        r, src, rounds = float(rate_hint), "European sibling (SPX/SPXW parity, same date) or --rate", 1
    else:
        r, src, rounds = RATE_FALLBACK, f"iterated from {RATE_FALLBACK * 100:.0f}% on EEP-corrected slopes", 3
    adjust = {}
    for k in range(rounds):
        _forwards(r, adjust)
        _pass(r)
        adjust = {ed: {sym: v[1] for sym, v in info["anchors"].items() if v[1]} for ed, info in live.items()}
        stamp["rate_path"].append(round(r, 5))
        if rate_hint is None:
            r_new = _slope_rate(adjust)
            if r_new is None or abs(r_new - r) < 1e-4:
                break
            r = r_new
    # final forwards on the corrected mids at the settled rate, then the anchors once more
    before = {ed: info["fwd"]["F"] for ed, info in live.items()}
    _forwards(r, adjust)
    _pass(r)
    for ed, info in live.items():
        if before[ed] and info["fwd"]["F"]:
            shift = info["fwd"]["F"] / before[ed] - 1.0
            stamp["refits"][ed.isoformat()] = dict(F0=before[ed], F1=info["fwd"]["F"], shift=round(shift, 6))
    stamp["rate"], stamp["rate_source"] = round(r, 5), src
    return stamp


def build_chain(date_str, root, defs_price, price_rows, defs_oi, oi_rows, oi_label, european_only=False,
                rate_hint=None):
    """
    Pure: no network. Joins the price leg (D's definitions + stat 7/8/11/6) and
    the OI leg (the OI day's definitions + stat 9) on the OSI symbol, builds
    the parity forward per expiry, solves both sides through nqvol.solve_chain
    and returns (doc, diag). `doc` is the chain file in the NQ shape.
    """
    meta = ROOTS[root]
    d0 = datetime.date.fromisoformat(date_str)
    close = nqvol.close_utc(d0)
    quotes = nbbo(price_rows, anchor_minute=close.hour * 60 + close.minute)
    oi = open_interest(oi_rows)
    # symbol-keyed views of both legs (instrument_id is only trusted within a day)
    q_by_sym, oi_by_sym = {}, {}
    for iid, q in quotes.items():
        dfn = defs_price.get(iid)
        if dfn:
            q_by_sym[dfn["symbol"]] = (dfn, q)
    for iid, o in oi.items():
        dfn = defs_oi.get(iid)
        if dfn:
            oi_by_sym[dfn["symbol"]] = (dfn, o)

    # --- quality per quoted contract -----------------------------------------
    qual = {}
    for sym, (dfn, q) in q_by_sym.items():
        b, a = q["bid"], q["ask"]
        if b is None and a is None:
            qual[sym] = "unquoted"
        elif b is None or a is None:
            qual[sym] = "one-sided"
        elif b > a:
            qual[sym] = "crossed"
        elif b == a:
            qual[sym] = "locked"
        elif (a - b) / (0.5 * (a + b)) > WIDE_SPREAD:
            qual[sym] = "wide"
        else:
            qual[sym] = "ok"

    # --- expiries: settlement rule, then the parity forward -------------------
    expiries = {}
    for sym, (dfn, q) in q_by_sym.items():
        expiries.setdefault(dfn["exp_date"], {})
    for sym, (dfn, o) in oi_by_sym.items():
        expiries.setdefault(dfn["exp_date"], {})
    exp_info, excluded = {}, {}
    for ed in sorted(expiries):
        inst, style, note = settlement_instant(root, ed)
        if inst is None:
            excluded[ed.isoformat()] = note
            continue
        pairs = _parity_pairs(ed, q_by_sym, qual)
        fwd = parity_forward(pairs, max((ed - d0).days, 0) / 365.25)
        # keyed by ROOT, not underlying: SPX (AM) and SPXW (PM) both expire on a
        # third Friday and must not share a forward when the files are merged
        exp_info[ed] = dict(instant=inst, style=style, style_note=note, fwd=fwd,
                            und=f"{root}@{ed.isoformat()}", anchors={})

    # --- American roots: the early-exercise premium comes off the mid --------
    amer_stamp = None
    if meta["exercise"] == "american" and not european_only:
        amer_stamp = american_pass(exp_info, q_by_sym, qual, d0, close, rate_hint)

    # --- the chain: contracts with open interest, priced where quoted ----------
    chain, fut, forwards = {}, {}, {}
    skipped = dict(no_def=0, zero_oi=0, excluded_expiry=0, no_forward=0)
    for ed, info in exp_info.items():
        if info["fwd"]["F"] is None:
            continue
        fut[info["und"]] = info["fwd"]["F"]
        forwards[info["und"]] = dict(expiry=ed.isoformat(), F=info["fwd"]["F"], D=info["fwd"]["D"],
                                     rate=info["fwd"]["rate"],
                                     pairs=info["fwd"]["n"], resid_rms=info["fwd"]["rms"],
                                     note=info["fwd"]["note"], unreliable=info["fwd"]["unreliable"],
                                     settlement=info["style"],
                                     settlement_note=info["style_note"],
                                     expiry_utc=info["instant"].isoformat())
    for sym, (dfn, o) in oi_by_sym.items():
        ed = dfn["exp_date"]
        if ed.isoformat() in excluded:
            skipped["excluded_expiry"] += 1
            continue
        info = exp_info.get(ed)
        if info is None or info["fwd"]["F"] is None:
            skipped["no_forward"] += 1
            continue
        n = o["oi"]
        if not n or n <= 0:
            skipped["zero_oi"] += 1
            continue
        key = f"{ed.isoformat()}|{root}"
        rec = chain.setdefault(key, {}).setdefault(f"{dfn['strike']:g}", {})
        qd = q_by_sym.get(sym, (None, None))[1]
        c = {"oi": int(n), "settle": None, "underlying": info["und"],
             "expiration": info["instant"].isoformat(), "root": root, "symbol": sym,
             "quality": qual.get(sym, "unquoted"), "oi_records": o["n_records"]}
        if len(o["values"]) > 1:
            c["oi_note"] = f"exchanges disagree: {sorted(o['values'])}"
        if o.get("zeros"):
            c["oi_zero_records"] = o["zeros"]
        if qd is not None:
            c["bid"], c["ask"] = qd["bid"], qd["ask"]
            c["quote_utc"] = _hhmm(qd["ts"])
            if qd.get("alt"):
                c["alt_mid"] = [[_hhmm(mn), round(m, 4)] for mn, m in qd["alt"]]
            if qd["close"] is not None:
                c["close"] = qd["close"]
            if qd["volume"] is not None:
                c["volume"] = qd["volume"]
            if c["quality"] in ("ok", "wide"):
                mid = 0.5 * (qd["bid"] + qd["ask"])
                c["mid"] = round(mid, 4)
                if info["fwd"]["unreliable"]:
                    c["iv_note"] = "forward unreliable"          # OI kept, no vol
                elif amer_stamp is not None:
                    # American: the European-equivalent anchor, EEP stripped
                    res = info["anchors"].get(sym)
                    if res is None:
                        c["iv_note"] = "american: not priced"
                    else:
                        anchor, e, iv_eur, iv_am, used, T = res
                        c["eep"] = round(e, 4)
                        if iv_eur is not None:
                            c["iv_eur"] = round(iv_eur, 6)
                        if anchor is None:
                            c["iv_note"] = ("american: worth intrinsic after EEP" if iv_eur is not None
                                            else "below premium floor")
                        else:
                            c["settle"] = round(anchor, 6)
                else:
                    # the anchor nqvol solves on: the undiscounted (forward-space) mid
                    c["settle"] = round(mid / info["fwd"]["D"], 6)
        rec[dfn["cp"]] = c

    doc = {"date": date_str, "source": DATASET, "root": root, "underlying": meta["underlying"],
           "exercise": meta["exercise"], "multiplier": MULTIPLIER, "settle_only": False,
           "price_basis": PRICE_BASIS, "oi_vintage": oi_label,
           "clock": "nqvol business time (NQ-measured variance shares); see equity_clock.py",
           "iv_basis": nqvol.IV_BASIS,
           "iv_ref": nqvol.close_utc(d0).isoformat(timespec="minutes"),
           **meta["scale"],
           "american": amer_stamp if amer_stamp is not None else (
               {"model": "NONE -- priced as European by request (--european-only); ITM-put vols biased high"}
               if meta["exercise"] == "american" else None),
           "futures_settle": fut, "forwards": forwards,
           "excluded_expiries": excluded, "chain": chain}
    graded, settled = nqvol.solve_chain(doc, settle_date=date_str, force=True) or (0, 0)

    # the writer's promise: no sentinel, in any form, anywhere in the file
    bad = _scan_sentinels(doc)
    if bad:
        raise SystemExit(f"  !! sentinel survived into the chain at {bad[:5]} -- not writing")

    anchor_ts, alt_diff = {}, []
    for sym, (dfn, q) in q_by_sym.items():
        anchor_ts[q["ts"]] = anchor_ts.get(q["ts"], 0) + 1
        if q["bid"] is not None and q["ask"] is not None and q["bid"] <= q["ask"]:
            mid = 0.5 * (q["bid"] + q["ask"])
            if mid >= 0.5:
                for _, m2 in q.get("alt", []):
                    alt_diff.append(abs(m2 - mid) / mid)
    alt_diff.sort()
    diag = dict(quotes=len(q_by_sym), qual={k: sum(1 for v in qual.values() if v == k)
                                            for k in ("ok", "wide", "locked", "crossed", "one-sided", "unquoted")},
                anchor_ts=anchor_ts,
                alt_n=len(alt_diff),
                alt_med=alt_diff[len(alt_diff) // 2] if alt_diff else None,
                alt_p90=alt_diff[int(0.9 * len(alt_diff))] if alt_diff else None,
                oi_instruments=len(oi_by_sym),
                oi_records=sum(o["n_records"] for _, o in oi_by_sym.values()),
                oi_disagree=sum(1 for _, o in oi_by_sym.values() if len(o["values"]) > 1),
                oi_with_zeros=sum(1 for _, o in oi_by_sym.values() if o.get("zeros")),
                oi_in_price_leg=sum(1 for s in oi_by_sym if s in q_by_sym),
                skipped=skipped, excluded=excluded, graded=graded, settled=settled)
    return doc, diag


def _scan_sentinels(obj, path=""):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += _scan_sentinels(v, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += _scan_sentinels(v, f"{path}[{i}]")
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        if abs(obj) >= 1e8:
            out.append(path)
    return out


# --------------------------------------------------------------- report ---

def report(doc, diag, hist_price=None, hist_oi=None, oi_totals=None):
    root, d0 = doc["root"], datetime.date.fromisoformat(doc["date"])
    chain, fut = doc["chain"], doc["futures_settle"]
    print(f"\n  {root} {doc['date']}   exercise {doc['exercise']}   price basis: EOD NBBO mid"
          f"   OI vintage: {doc['oi_vintage']}")
    for label, hist in (("price leg", hist_price), ("OI leg", hist_oi)):
        if not hist:
            continue
        print(f"  publication clock, {label} (records by UTC minute, largest buckets first):")
        for st in sorted(hist):
            h = hist[st]
            top = ", ".join(f"{_hhmm(mn)}:{n:,}" for mn, n in sorted(h.items(), key=lambda kv: -kv[1])[:6])
            print(f"    stat {st:2d} {STAT_NAMES.get(st, '?'):14} {sum(h.values()):8,}   {top}")
    at = ", ".join(f"{_hhmm(mn)}:{n:,}" for mn, n in sorted(diag["anchor_ts"].items(), key=lambda kv: -kv[1])[:4])
    print(f"  quote anchor: the bucket nearest the {nqvol.close_utc(d0):%H:%M}Z close, per contract -> {at}")
    if diag["alt_n"]:
        print(f"  other quote buckets (curb session etc.), |mid - anchor mid| / mid over {diag['alt_n']:,} "
              f"contracts: median {diag['alt_med'] * 100:.2f}%, p90 {diag['alt_p90'] * 100:.2f}%  (NOT blended)")
    if oi_totals:
        print("  open interest by window (deduped totals -- the two are different vintages):")
        for k, v in oi_totals.items():
            print(f"    {k:34} {v:>12,}")
    q = diag["qual"]
    print(f"  quotes: {diag['quotes']:,} contracts   ok {q['ok']:,}  wide {q['wide']:,}  locked {q['locked']:,}"
          f"  crossed {q['crossed']:,}  one-sided {q['one-sided']:,}  unquoted {q['unquoted']:,}")
    print(f"  open interest: {diag['oi_instruments']:,} contracts from {diag['oi_records']:,} records "
          f"({diag['oi_records'] / max(diag['oi_instruments'], 1):.1f} per contract, deduped, NOT summed)"
          f"   exchanges disagree on {diag['oi_disagree']:,}   zero records on {diag.get('oi_with_zeros', 0):,}"
          f"   quoted {diag['oi_in_price_leg']:,}")
    print(f"  skipped: {diag['skipped']}")
    for ed, why in diag["excluded"].items():
        print(f"  !! expiry {ed} EXCLUDED: {why}")

    print(f"\n  {'expiry':10} {'set':3} {'dte':>4} {'strikes':>7} {'call OI':>9} {'put OI':>9} "
          f"{'F (parity)':>11} {'D':>8} {'r%':>5} {'pairs':>5} {'rms':>6} {'ATM IV':>7} {'solved':>7} {'ok/quoted':>10}")
    tot_c = tot_p = tot_k = 0
    gaps = []                       # (|log-moneyness|, |iv_C - iv_P|)
    for key in sorted(chain, key=lambda k: k.split("|")[0]):
        strikes = chain[key]
        ed = datetime.date.fromisoformat(key.split("|")[0])
        und = next(c["underlying"] for r in strikes.values() for c in r.values())
        fw = doc["forwards"][und]
        F = fut[und]
        c_oi = sum(r.get("C", {}).get("oi", 0) for r in strikes.values())
        p_oi = sum(r.get("P", {}).get("oi", 0) for r in strikes.values())
        pts, ivs, solved, quoted = [], [], 0, 0
        for ks, rec in strikes.items():
            both = {}
            for cp in ("C", "P"):
                c = rec.get(cp)
                if not c:
                    continue
                if c.get("settle") is not None:
                    quoted += 1
                iv = c.get("iv")
                if iv and 0.03 < iv < 3.0:
                    pts.append((log(float(ks) / F), iv)); ivs.append(iv); solved += 1
                    both[cp] = iv
            if len(both) == 2:
                gaps.append((abs(log(float(ks) / F)), abs(both["C"] - both["P"])))
        med = sorted(ivs)[len(ivs) // 2] if ivs else 0.2
        atm = nqvol.Smile(pts, med).atm if pts else None
        dte = nqvol.sessions_between(d0, ed) if ed > d0 else 0
        tot_c += c_oi; tot_p += p_oi; tot_k += len(strikes)
        nan = float("nan")
        print(f"  {ed.isoformat():10} {fw['settlement']:3} {dte:4d} {len(strikes):7,} {c_oi:9,} {p_oi:9,} "
              f"{F:11,.2f} {fw['D'] if fw['D'] is not None else nan:8.4f} "
              f"{(fw.get('rate') * 100) if fw.get('rate') is not None else nan:5.1f} {fw['pairs']:5d} "
              f"{fw['resid_rms'] if fw['resid_rms'] is not None else nan:6.2f} "
              f"{(atm * 100 if atm else nan):6.2f}% {solved:7,} {solved:>4,}/{quoted:<5,}"
              + (f"  {fw['note']}" if fw["note"] else "") + (f"  ({fw['settlement_note']})" if fw["settlement_note"] else ""))
    if not chain:
        print("  (chain is EMPTY)")
    print(f"  {'total':10} {'':3} {'':4} {tot_k:7,} {tot_c:9,} {tot_p:9,}    total OI {tot_c + tot_p:,}")
    print(f"  solved IV on {diag['graded']:,} of {diag['settled']:,} priced contracts")
    if gaps:
        for label, sel in (("|log K/F| <= 5%", [g for m, g in gaps if m <= 0.05]),
                           ("|log K/F| >  5%", [g for m, g in gaps if m > 0.05])):
            if not sel:
                continue
            sel.sort()
            print(f"  call-vs-put IV gap, both sides solved, {label} (n={len(sel):,}): median "
                  f"{sel[len(sel) // 2] * 100:.2f} vol pts, p90 {sel[int(0.9 * len(sel))] * 100:.2f}, "
                  f"max {sel[-1] * 100:.2f}")

    am = doc.get("american")
    if am and am.get("contracts"):
        ys = sorted(am["yields"].values())
        rf = am["refits"]
        big = sorted(rf.items(), key=lambda kv: -abs(kv[1]["shift"]))[:3]
        print(f"  american: {am['model']}")
        print(f"    rate {am['rate'] * 100:.2f}% ({am['rate_source']})   spot proxy {am['spot_proxy']:,.2f} "
              f"({am['spot_proxy_expiry']})   yield {ys[0] * 100:.2f}%..{ys[-1] * 100:.2f}% over {len(ys)} expiries"
              f"{'   ' + str(am['clamped_yields']) + ' CLAMPED' if am['clamped_yields'] else ''}")
        print(f"    rate path {' -> '.join(f'{x * 100:.2f}%' for x in am.get('rate_path', []))}   "
              f"forward refit on EEP-corrected mids: {sum(1 for v in rf.values() if abs(v['shift']) > REFIT_SHIFT)} "
              f"of {len(rf)} expiries moved > {REFIT_SHIFT * 1e4:.0f} bp; largest "
              + ", ".join(f"{k} {v['shift'] * 1e4:+.1f} bp" for k, v in big))
        shifts = []                              # (|m|, is_put, iv_eur - iv_amer, eep/mid)
        for key, strikes in chain.items():
            und = next(c["underlying"] for r in strikes.values() for c in r.values())
            F = fut[und]
            for ks, rec in strikes.items():
                for cp, c in rec.items():
                    if c.get("iv") and c.get("iv_eur"):
                        shifts.append((abs(log(float(ks) / F)), cp == "P", c["iv_eur"] - c["iv"],
                                       c.get("eep", 0.0) / max(c.get("mid", 1e-9), 1e-9)))
        for label, sel in (("puts,  |log K/F| <= 5%", [x for x in shifts if x[1] and x[0] <= 0.05]),
                           ("puts,  |log K/F| >  5%", [x for x in shifts if x[1] and x[0] > 0.05]),
                           ("calls, |log K/F| <= 5%", [x for x in shifts if not x[1] and x[0] <= 0.05]),
                           ("calls, |log K/F| >  5%", [x for x in shifts if not x[1] and x[0] > 0.05])):
            if not sel:
                continue
            d = sorted(x[2] for x in sel); e = sorted(x[3] for x in sel)
            print(f"    European-minus-American vol, {label} (n={len(sel):,}): median {d[len(d) // 2] * 100:+.2f} "
                  f"vol pts, p90 {d[int(0.9 * len(d))] * 100:+.2f}, max {d[-1] * 100:+.2f}   "
                  f"EEP/mid median {e[len(e) // 2] * 100:.2f}%, p90 {e[int(0.9 * len(e))] * 100:.2f}%")
    elif am:
        print(f"  american: {am.get('model') or am.get('note')}")

    fails = {}
    for key, strikes in chain.items():
        for ks, rec in strikes.items():
            for cp, c in rec.items():
                if c.get("iv"):
                    continue
                why = c.get("iv_note") or ("no quote" if "bid" not in c else
                                           c["quality"] if c["quality"] != "ok" else "inversion failed")
                fails.setdefault(why, []).append(f"{key.split('|')[0]} {ks}{cp}")
    if fails:
        print("  contracts without a solved vol, by reason:")
        for why, lst in sorted(fails.items(), key=lambda kv: -len(kv[1])):
            print(f"    {len(lst):6,}  {why:28} e.g. {', '.join(lst[:4])}")
    print()


# ------------------------------------------------------------ estimate ---

def _requests(date_str, roots, oi_day=None, stats_hours=None, live=False):
    """The exact requests --fetch would make, in order, with their windows."""
    d0 = datetime.date.fromisoformat(date_str)
    d_oi = (d0 + datetime.timedelta(days=oi_day)) if oi_day is not None else nqvol.next_session(d0)
    out = []
    for r in roots:
        p = ROOTS[r]["parent"]
        out.append((r, "definition", p, "defs D", _day_window(d0)))
        out.append((r, "statistics", p, "stats D", _hours_window(d0, stats_hours) if stats_hours else _day_window(d0)))
        if d_oi != d0 and not live:
            out.append((r, "definition", p, "defs OI-day", _day_window(d_oi)))
            out.append((r, "statistics", p, "stats OI-day", _day_window(d_oi)))
    return out, d_oi


def estimate(date_str, roots, oi_day=None, stats_hours=None, quiet=False, live=False):
    """FREE. Sizes and prices every request the fetch makes. Returns total MB."""
    c = client()
    reqs, d_oi = _requests(date_str, roots, oi_day, stats_hours, live)
    a = _avail_end(c)
    if not quiet:
        print(f"\nsize estimate for {date_str}, roots {' '.join(roots)}   (OI day {d_oi}; "
              f"dataset ends {a or '?'}; nothing downloaded, nothing billed)\n")
        print(f"  {'root':5} {'leg':13} {'schema':11} {'window':41} {'MB':>8} {'USD':>9}")
    tot_mb = tot_usd = 0.0
    for r, schema, parent, leg, (s, e) in reqs:
        e2, clamped = (e, False)
        if a and e > a:
            e2, clamped = (a, True) if s < a else (None, True)
        if e2 is None:
            if not quiet:
                print(f"  {r:5} {leg:13} {schema:11} {s} -> {e}   PAST DATASET END")
            continue
        try:
            mb = c.metadata.get_billable_size(dataset=DATASET, symbols=[parent], stype_in="parent",
                                              schema=schema, start=s, end=e2) / 1e6
            usd = c.metadata.get_cost(dataset=DATASET, symbols=[parent], stype_in="parent",
                                      schema=schema, start=s, end=e2)
        except Exception as ex:
            if not quiet:
                print(f"  {r:5} {leg:13} {schema:11} {s} -> {e2}   FAILED {str(ex).splitlines()[0][:60]}")
            continue
        tot_mb += mb; tot_usd += usd
        if not quiet:
            print(f"  {r:5} {leg:13} {schema:11} {s} -> {e2}{' (clamped)' if clamped else '':10} "
                  f"{mb:8.1f} {usd:9.4f}")
    if not quiet:
        print(f"\n  total {tot_mb:.1f} MB   ${tot_usd:.4f}   (~{tot_mb * 252 / 1000:.1f} GB/yr at this scope)")
        print("  the account has priced every request at $0.0000 so far; the gate that matters is --max-mb\n")
    return tot_mb


# --------------------------------------------------------------- fetch ---

DEF_CACHE = None


def _defs(c, root, day, label):
    """Definitions for ONE utc day and one root, cached when the window was complete."""
    ds, de = _day_window(day)
    f = (DEF_CACHE / f"{root}-{day}.pkl") if DEF_CACHE else None
    if f and f.exists():
        try:
            import pandas as pd
            defs = pd.read_pickle(f)
            print(f"  [{label}] defs {root} {day}  (cached, {len(defs):,} rows)")
            return defs
        except Exception as ex:
            print(f"  [{label}] defs cache unreadable ({ex}); refetching")
    de_c, clamped = _clamp(c, ds, de, label, "defs")
    print(f"  [{label}] defs {root} {ds} -> {de_c}")
    store = c.timeseries.get_range(dataset=DATASET, schema="definition", stype_in="parent",
                                  symbols=[ROOTS[root]["parent"]], start=ds, end=de_c)
    defs = _to_df(store, price_type="float")
    keep = [k for k in ("instrument_id", "raw_symbol", "instrument_class", "strike_price",
                        "expiration", "underlying") if k in defs.columns]
    if keep and "instrument_id" in keep:
        defs = defs[keep].reset_index(drop=True)
    elif keep:
        defs = defs[keep].reset_index()
    if f and not clamped and len(defs):
        try:
            DEF_CACHE.mkdir(parents=True, exist_ok=True)
            defs.to_pickle(f)
        except Exception as ex:
            print(f"  [{label}] could not cache defs: {ex}")
    return defs


def _stats(c, root, day, label, hours=None):
    ss, se = _hours_window(day, hours) if hours else _day_window(day)
    se, _ = _clamp(c, ss, se, label, "stats")
    print(f"  [{label}] stats {root} {ss} -> {se}")
    store = c.timeseries.get_range(dataset=DATASET, schema="statistics", stype_in="parent",
                                  symbols=[ROOTS[root]["parent"]], start=ss, end=se)
    return _to_df(store, price_type="fixed")


def sibling_rate(outdir, date_str, override=None):
    """The rate an American root prices with: --rate if given, else the median
    parity rate of the same-date SPX / SPXW chain (European, clean slopes)."""
    if override is not None:
        return float(override), "--rate"
    rs = []
    for sib in ("SPX", "SPXW"):
        p = Path(outdir) / f"{date_str}-{sib}-chain.json"
        if not p.exists():
            continue
        try:
            fw = json.load(open(p, encoding="utf-8")).get("forwards", {})
        except Exception:
            continue
        d0 = datetime.date.fromisoformat(date_str)
        for und, f in fw.items():
            ed = datetime.date.fromisoformat(f["expiry"])
            if f.get("rate") is not None and not f.get("unreliable") and 20 <= (ed - d0).days <= 400:
                rs.append(f["rate"])
    if not rs:
        return None, None
    rs.sort()
    return rs[len(rs) // 2], f"median of {len(rs)} SPX/SPXW parity rates, 20-400d, same date"


LIVE_QUIET_S = 8.0               # stop the replay this long after the last record
LIVE_MAX_S = 240.0               # and never listen longer than this


def live_oi(root, day, quiet_s=LIVE_QUIET_S, max_s=LIVE_MAX_S, client_factory=None):
    """
    The open-interest leg from the LIVE gateway: an intraday replay of the
    statistics schema from 00:00 UTC on `day`, which carries the 10:30 UTC
    stat-9 wave hours before Databento's historical endpoint will serve the
    day (it serves complete days only -- measured 2026-09-06). Symbol mapping
    comes from the stream's own SymbolMappingMsg records, so no definitions
    snapshot is needed. Returns (defs, rows, meta) in the shapes parse_defs /
    parse_stats produce, or (None, None, meta) when the gateway refuses, the
    start is outside the 24-hour replay window, or nothing arrives -- the
    caller then falls back to the historical leg and stamps STALE.
    Only stat 9 is kept; the price legs stay historical.
    """
    import time
    meta = dict(accepted=False, mappings=0, records=0, oi_records=0, instruments=0, seconds=0.0, note=None)
    start = datetime.datetime(day.year, day.month, day.day, tzinfo=nqvol.UTC)
    now = datetime.datetime.now(nqvol.UTC)
    if now - start > datetime.timedelta(hours=24) or start > now:
        meta["note"] = f"replay start {start:%Y-%m-%d %H:%M}Z is outside the gateway's 24-hour window"
        return None, None, meta
    sym_of, rows, last = {}, [], [time.time()]

    def on_record(rec):
        last[0] = time.time()
        name = type(rec).__name__
        if name == "SymbolMappingMsg":
            sym_of[int(rec.instrument_id)] = str(getattr(rec, "stype_out_symbol", None)
                                                or getattr(rec, "stype_in_symbol", ""))
            meta["mappings"] += 1
        elif name == "StatMsg":
            meta["records"] += 1
            if int(rec.stat_type) == STAT_OI:
                rows.append(StatRow((int(rec.instrument_id), int(getattr(rec, "publisher_id", 0) or 0), STAT_OI,
                             None, _qty(rec.quantity), _ts_minute(rec.ts_event)), ts_event=rec.ts_event,
                             ts_ref=getattr(rec, "ts_ref", None), update_action=getattr(rec, "update_action", 1)))
    try:
        if client_factory is None:
            import databento as db
            client_factory = lambda: db.Live(key=os.environ["DATABENTO_API_KEY"])
        live = client_factory()
        live.subscribe(dataset=DATASET, schema="statistics", stype_in="parent",
                       symbols=[ROOTS[root]["parent"]], start=start)
        live.add_callback(on_record)
        live.start()
        meta["accepted"] = True
        t0 = time.time()
        while time.time() - t0 < max_s:
            time.sleep(0.5)
            if (meta["records"] or meta["mappings"]) and time.time() - last[0] > quiet_s:
                break
        try:
            live.stop()
        except Exception:
            pass
        meta["seconds"] = round(time.time() - t0, 1)
    except Exception as ex:
        meta["note"] = f"gateway: {type(ex).__name__}: {str(ex).strip().splitlines()[0][:120] if str(ex).strip() else ''}"
        return None, None, meta
    # definitions from the mapping messages, in parse_defs' shape
    defs = {}
    for iid, sym in sym_of.items():
        osi = parse_osi(sym)
        if osi and osi[0] == root:
            defs[iid] = dict(symbol=sym, root=osi[0], cp=osi[2], strike=osi[3], exp_date=osi[1], exp_raw=None)
    rows = [r for r in rows if r[0] in defs and r[4] is not None]
    meta["oi_records"] = len(rows)
    meta["instruments"] = len({r[0] for r in rows})
    if not rows:
        meta["note"] = meta["note"] or "replay carried no open-interest records (before 10:30 UTC, a holiday, or unsupported)"
        return None, None, meta
    return defs, rows, meta


def fetch(date_str, roots, outdir, oi_day=None, stats_hours=None, max_mb=200.0,
          european_only=False, rate=None, live=False):
    mb = estimate(date_str, roots, oi_day, stats_hours, quiet=False, live=live)
    if mb > max_mb:
        raise SystemExit(f"  estimate {mb:.1f} MB exceeds --max-mb {max_mb:.0f}; not fetching blind.")
    c = client()
    d0 = datetime.date.fromisoformat(date_str)
    d_oi = (d0 + datetime.timedelta(days=oi_day)) if oi_day is not None else nqvol.next_session(d0)
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    for r in roots:
        print(f"\n== {r} ==")
        defsD = _defs(c, r, d0, "price")
        statsD = _stats(c, r, d0, "price", stats_hours)
        pd_, pmeta = parse_defs(defsD, r)
        rowsD, smeta = parse_stats(statsD)
        print(f"  [price] defs {len(defsD):,} rows -> {len(pd_):,} {r} contracts "
              f"(strays {pmeta['strays']}, unparsed {pmeta['unparsed']}, strike mismatches {pmeta['mismatch']})")
        print(f"  [price] stats {smeta['records']:,} records; sentinels removed: "
              f"{smeta['price_sentinels']:,} price, {smeta['qty_sentinels']:,} quantity")
        histD = hour_histogram(rowsD)
        oiD = open_interest(rowsD)
        totals = {f"{d0} window (OI as of {nqvol.prev_session(d0)} close)":
                  sum(o["oi"] for o in oiD.values())}
        got_live = False
        if live and d_oi != d0:
            od_, rowsO, lm = live_oi(r, d_oi)
            print(f"  [oi] live replay from {d_oi} 00:00Z: "
                  + (f"accepted, {lm['mappings']:,} mappings, {lm['records']:,} statistics, "
                     f"{lm['oi_records']:,} OI records on {lm['instruments']:,} contracts in {lm['seconds']}s"
                     if lm["accepted"] else "REFUSED") + (f"  ({lm['note']})" if lm["note"] else ""))
            if od_ is not None and lm["instruments"] >= 100:
                histO = hour_histogram(rowsO)
                oiO = open_interest(rowsO)
                totals[f"live replay {d_oi} (OI as of {d0} close)"] = sum(o["oi"] for o in oiO.values())
                defs_oi, rows_oi = od_, rowsO
                oi_label = f"OI as of {d0} close, live replay {d_oi} {_hhmm(next(iter(histO.get(STAT_OI, {0: 0}))))}Z"
                got_live = True
            else:
                print("  [oi] live replay unusable -- trying the historical window")
        if got_live:
            pass
        elif d_oi != d0:
            try:
                defsO = _defs(c, r, d_oi, "oi")
                statsO = _stats(c, r, d_oi, "oi")
            except SystemExit as ex:
                # the OI day is past the dataset end (a same-morning pull): stale, stamped
                print(f"  [oi] {str(ex).strip().splitlines()[0][:100]}")
                defsO, statsO = None, None
            if defsO is not None:
                od_, ometa = parse_defs(defsO, r)
                rowsO, ometa2 = parse_stats(statsO)
                histO = hour_histogram(rowsO)
                oiO = open_interest(rowsO)
                totals[f"{d_oi} window (OI as of {d0} close)"] = sum(o["oi"] for o in oiO.values())
            else:
                histO, oiO = None, {}
            if len(oiO) >= 100:
                defs_oi, rows_oi, oi_label = od_, rowsO, f"OI as of {d0} close, from the {d_oi} window"
            else:
                print(f"  [oi] the {d_oi} window carried {len(oiO)} open-interest instruments -- "
                      f"falling back to the {d0} window (one session STALE)")
                defs_oi, rows_oi, oi_label = pd_, rowsD, f"STALE: OI as of {nqvol.prev_session(d0)} close, from the {d0} window"
        else:
            histO, defs_oi, rows_oi = None, pd_, rowsD
            oi_label = f"OI as of {nqvol.prev_session(d0)} close, from the {d0} window (--oi-day 0)"
        hint = None
        if ROOTS[r]["exercise"] == "american" and not european_only:
            hint, src = sibling_rate(out, date_str, rate)
            print(f"  [american] rate {hint * 100:.2f}% from {src}" if hint is not None else
                  f"  [american] no SPX/SPXW chain for {date_str} and no --rate: iterating the rate from "
                  f"{RATE_FALLBACK * 100:.0f}% (pull SPX first for a cleaner anchor)")
        doc, diag = build_chain(date_str, r, pd_, rowsD, defs_oi, rows_oi, oi_label, european_only, hint)
        if hint is not None:
            doc["american"]["rate_source"] = src
        path = out / f"{date_str}-{r}-chain.json"
        report(doc, diag, histD, histO, totals)
        if not doc["chain"]:
            raise SystemExit(f"  no contracts with open interest for {r} -- not writing {path}. "
                             f"The report above says which gate emptied it (skipped: no_forward means "
                             f"no parity pairs formed; zero_oi means the OI leg carried nothing).")
        json.dump(doc, open(path, "w", encoding="utf-8"), indent=1)
        print(f"  wrote {path}  ({path.stat().st_size / 1e6:.2f} MB)")


# ------------------------------------------------------------- selftest ---

def selftest(verbose=True, out_path=None):
    """
    No network, no key. A synthetic OPRA tape with every trap planted:
    INT64_MAX in the price of OI records and the quantity of price records,
    18 exchange records per contract's open interest, per-exchange bids and
    offers of varying width (so the NBBO is the tightest pair), one crossed
    quote, stat 11 = 0 on an illiquid contract, and a bogus SPX expiry that is
    not a third Friday. The chain must recover the forward, the discount, the
    ATM vol and the OI total, and carry no sentinel.
    """
    import random
    import pandas as pd
    random.seed(7)
    date_str, root = "2026-09-03", "SPX"
    d0 = datetime.date.fromisoformat(date_str)
    ref = nqvol.close_utc(d0)
    r_, q_ = 0.04, 0.013
    S = 6500.0
    exps = [datetime.date(2026, 9, 18), datetime.date(2026, 10, 16), datetime.date(2026, 12, 18),
            third_friday(2030, 12)]                              # a LEAPS: D ~0.84 must pass the rate guard
    bogus = datetime.date(2026, 9, 25)
    truth = {}
    defs, stats = [], []
    iid = 1000
    ns = lambda h: int((datetime.datetime(d0.year, d0.month, d0.day, h, 5, tzinfo=nqvol.UTC)
                        .timestamp()) * 1e9)
    widths = [0.10, 0.30, 0.20, 0.50, 0.15]                      # per-exchange half-spreads
    oi_truth = 0
    for ed in exps + [bogus]:
        inst, _, note = settlement_instant(root, ed)
        T = nqvol.years_between(ref, inst) if inst else 0.05
        F = S * exp((r_ - q_) * T); D = exp(-r_ * T)
        truth[ed] = dict(F=F, D=D, T=T, sig_atm=0.15)
        for k in range(5500, 7525, 25):
            m = log(k / F)
            sig = 0.15 - 0.55 * m + 1.2 * m * m
            for cp in "CP":
                px = D * nqvol.bs76_price(F, k, sig, T, cp == "C")
                sym = f"{root:<6}{ed:%y%m%d}{cp}{int(round(k * 1000)):08d}"
                iid += 1
                defs.append(dict(instrument_id=iid, raw_symbol=sym, instrument_class=cp,
                                 strike_price=float(k), expiration=int(datetime.datetime(
                                     ed.year, ed.month, ed.day, tzinfo=nqvol.UTC).timestamp() * 1e9),
                                 underlying="SPX"))
                oi_n = random.randint(0, 3000) if abs(m) < 0.12 else random.randint(0, 200)
                if ed != bogus:
                    oi_truth += oi_n
                for ex in range(18):                                   # 18 OI records, same figure
                    stats.append(dict(instrument_id=iid, publisher_id=100 + ex, stat_type=9,
                                      price=INT64_MAX, quantity=oi_n, ts_event=ns(10)))
                if px > 0.02:
                    for ex, w in enumerate(widths):
                        b = math.floor((px - w) / 0.05) * 0.05
                        a = math.ceil((px + w) / 0.05) * 0.05
                        if b <= 0:
                            b = 0.0
                        stats.append(dict(instrument_id=iid, publisher_id=100 + ex, stat_type=8,
                                          price=int(round(b / PRICE_SCALE)), quantity=INT64_MAX,
                                          ts_event=ns(20)))
                        stats.append(dict(instrument_id=iid, publisher_id=100 + ex, stat_type=7,
                                          price=int(round(a / PRICE_SCALE)), quantity=INT64_MAX,
                                          ts_event=ns(20)))
                    # the curb-session summary an hour later, 2% richer: a blended
                    # NBBO would take its bid and the close's ask
                    for ex, w in enumerate(widths[:2]):
                        px2 = px * 1.02
                        b2 = max(0.0, math.floor((px2 - w) / 0.05) * 0.05)
                        a2 = math.ceil((px2 + w) / 0.05) * 0.05
                        stats.append(dict(instrument_id=iid, publisher_id=100 + ex, stat_type=8,
                                          price=int(round(b2 / PRICE_SCALE)), quantity=INT64_MAX,
                                          ts_event=ns(21)))
                        stats.append(dict(instrument_id=iid, publisher_id=100 + ex, stat_type=7,
                                          price=int(round(a2 / PRICE_SCALE)), quantity=INT64_MAX,
                                          ts_event=ns(21)))
                close = 0.0 if oi_n < 20 else px
                stats.append(dict(instrument_id=iid, publisher_id=100, stat_type=11,
                                  price=int(round(close / PRICE_SCALE)), quantity=INT64_MAX, ts_event=ns(20)))
                stats.append(dict(instrument_id=iid, publisher_id=100, stat_type=6,
                                  price=INT64_MAX, quantity=random.randint(0, 5000), ts_event=ns(20)))
    # one crossed contract: an exchange bid above the consolidated offer
    victim = [d for d in defs if d["raw_symbol"].startswith("SPX   261016C06800000")][0]["instrument_id"]
    stats.append(dict(instrument_id=victim, publisher_id=140, stat_type=8,
                      price=int(round(50.0 / PRICE_SCALE)), quantity=INT64_MAX, ts_event=ns(20)))
    defs_df = pd.DataFrame(defs)
    stats_df = pd.DataFrame(stats)
    pd_, pmeta = parse_defs(defs_df, root)
    rows, smeta = parse_stats(stats_df)
    doc, diag = build_chain(date_str, root, pd_, rows, pd_, rows, "selftest")
    if verbose:
        report(doc, diag, hour_histogram(rows))
    # ---- assertions ---------------------------------------------------------
    errs = []
    if smeta["price_sentinels"] == 0 or smeta["qty_sentinels"] == 0:
        errs.append("sentinels were not detected")
    if _scan_sentinels(doc):
        errs.append("a sentinel survived into the chain")
    if bogus.isoformat() not in doc["excluded_expiries"]:
        errs.append("bogus SPX expiry was not excluded by the settlement rule")
    tot_oi = sum(c["oi"] for st in doc["chain"].values() for r in st.values() for c in r.values())
    if tot_oi != oi_truth:
        errs.append(f"OI total {tot_oi} != truth {oi_truth} (dedupe failed?)")
    for ed in exps:
        und = f"SPX@{ed.isoformat()}"
        fw = doc["forwards"][und]
        t = truth[ed]
        if abs(fw["F"] - t["F"]) > 0.15:
            errs.append(f"{ed}: F {fw['F']:.2f} vs truth {t['F']:.2f}")
        if abs(fw["D"] - t["D"]) > 1e-3:
            errs.append(f"{ed}: D {fw['D']:.5f} vs truth {t['D']:.5f}")
        want = nqvol.rth_open_utc(ed).isoformat()          # 13:30Z in summer, 14:30Z in winter
        if fw["settlement"] != "AM" or fw["expiry_utc"] != want:
            errs.append(f"{ed}: settlement instant {fw['expiry_utc']} is not the 09:30 ET open ({want})")
        strikes = doc["chain"][f"{ed}|SPX"]
        pts = [(log(float(k) / fw["F"]), c["iv"]) for k, r in strikes.items() for c in r.values() if c.get("iv")]
        atm = nqvol.Smile(pts, 0.15).atm
        if abs(atm - t["sig_atm"]) > 0.003:
            errs.append(f"{ed}: ATM IV {atm:.4f} vs truth {t['sig_atm']:.4f}")
    if diag["anchor_ts"] != {20 * 60 + 5: diag["quotes"]}:
        errs.append(f"anchor bucket is not 20:05 for every contract: {diag['anchor_ts']}")
    if diag["alt_med"] is None or not (0.015 < diag["alt_med"] < 0.03):
        errs.append(f"curb-bucket difference not reported as ~2%: {diag['alt_med']}")
    leaps = doc["forwards"][f"SPX@{third_friday(2030, 12)}"]
    if leaps["rate"] is None or abs(leaps["rate"] - r_) > 0.002 or leaps["unreliable"]:
        errs.append(f"LEAPS parity: rate {leaps['rate']} vs {r_}, unreliable={leaps['unreliable']}")
    vc = doc["chain"]["2026-10-16|SPX"]["6800"]["C"]
    if vc["quality"] != "crossed" or vc["settle"] is not None or vc.get("iv"):
        errs.append("crossed quote was not stamped and skipped")
    errs += _selftest_american(verbose)
    print("  SELFTEST", "PASS" if not errs else "FAIL")
    for e in errs:
        print("    -", e)
    if out_path:
        json.dump(doc, open(out_path, "w", encoding="utf-8"), indent=1)
        print(f"  synthetic chain written to {out_path} (for a trinity_db.py dry run)")
    return not errs


def _selftest_american(verbose=True):
    """
    A synthetic SPY tape priced as AMERICAN options on a CRR tree in CALENDAR
    time at a known vol (r 4.5%, q 1.2%), with the OPRA traps planted again.
    The engine works in business time with the carry rescaled, which is the
    same tree, so the recovered vol must convert back to the calendar vol at
    the money within 0.3 vol pts -- and a European-only run must show the
    ITM-put bias the American pass exists to remove.
    """
    import random
    import pandas as pd
    import amer
    random.seed(11)
    date_str, root = "2026-09-03", "SPY"
    d0 = datetime.date.fromisoformat(date_str)
    close = nqvol.close_utc(d0)
    r_, q_, S0 = 0.045, 0.012, 700.0
    exps = [datetime.date(2026, 9, 4), datetime.date(2026, 9, 8), datetime.date(2026, 10, 16),
            datetime.date(2026, 12, 18)]
    sig_cal = {}
    defs, stats, iid = [], [], 5000
    ns = lambda h: int(datetime.datetime(d0.year, d0.month, d0.day, h, 23, tzinfo=nqvol.UTC).timestamp() * 1e9)
    widths = [0.02, 0.05, 0.03, 0.08, 0.04]
    for ed in exps:
        t_cal = (ed - d0).days / 365.25
        F = S0 * exp((r_ - q_) * t_cal)
        for k in range(600, 805, 5):
            m = log(k / F)
            sc = 0.17 - 0.5 * m + 1.0 * m * m
            sig_cal[(ed, k)] = sc
            for cp in "CP":
                px, _ = amer.crr(S0, float(k), r_, q_, sc, t_cal, cp == "C")
                sym = f"{root:<6}{ed:%y%m%d}{cp}{int(round(k * 1000)):08d}"
                iid += 1
                defs.append(dict(instrument_id=iid, raw_symbol=sym, instrument_class=cp,
                                 strike_price=float(k), expiration=0, underlying=root))
                oi_n = random.randint(0, 5000) if abs(m) < 0.1 else random.randint(0, 300)
                for ex in range(18):
                    stats.append(dict(instrument_id=iid, publisher_id=200 + ex, stat_type=9,
                                      price=INT64_MAX, quantity=oi_n, ts_event=ns(10)))
                if px > 0.02:
                    for ex, w in enumerate(widths):
                        b = max(0.0, math.floor((px - w) / 0.01) * 0.01)
                        a = math.ceil((px + w) / 0.01) * 0.01
                        # each exchange publishes in its own minute, and this
                        # one's bid and offer straddle a minute boundary; a
                        # re-broadcast an hour later must stay a separate wave
                        for st, p, mn in ((8, b, 47 + ex), (7, a, 48 + ex), (8, b, 67 + ex), (7, a, 67 + ex)):
                            stats.append(dict(instrument_id=iid, publisher_id=200 + ex, stat_type=st,
                                              price=int(round(p / PRICE_SCALE)), quantity=INT64_MAX,
                                              ts_event=ns(20) + mn * 60_000_000_000))
                    # the consolidated placeholder nearest the close: OHLC and volume, NO quote
                    for st in (7, 8):
                        stats.append(dict(instrument_id=iid, publisher_id=199, stat_type=st, price=0,
                                          quantity=INT64_MAX, ts_event=ns(20) + 17 * 60_000_000_000))
    pd_, _ = parse_defs(pd.DataFrame(defs), root)
    rows, _ = parse_stats(pd.DataFrame(stats))
    errs = []
    doc, diag = build_chain(date_str, root, pd_, rows, pd_, rows, "selftest")
    doc_e, _ = build_chain(date_str, root, pd_, rows, pd_, rows, "selftest", european_only=True)
    if verbose:
        print("\n  --- American root (synthetic SPY) ---")
        report(doc, diag)
    am = doc["american"]
    if abs(am["rate"] - r_) > 0.003:
        errs.append(f"american rate {am['rate']} vs {r_}")
    for ed in exps[2:]:
        key = f"{ed}|SPY"
        F = doc["futures_settle"][f"SPY@{ed}"]
        T_bus = nqvol.years_between(close, nqvol.close_utc(ed))
        t_cal = (ed - d0).days / 365.25
        k_atm = min((float(k) for k in doc["chain"][key]), key=lambda k: abs(k - F))
        for cp in "CP":
            c = doc["chain"][key][f"{k_atm:g}"][cp]
            ce = doc_e["chain"][key][f"{k_atm:g}"][cp]
            if not c.get("iv"):
                errs.append(f"{ed} {k_atm}{cp}: no American vol"); continue
            got = c["iv"] * sqrt(T_bus / t_cal)
            want = sig_cal[(ed, int(k_atm))]
            if abs(got - want) > 0.003:
                errs.append(f"{ed} {k_atm}{cp}: vol {got:.4f} vs truth {want:.4f}")
            if cp == "P":
                # the stamped European vol at the SAME forward is the honest comparison;
                # a --european-only run fits its own (biased) forward and is not
                bias = (c["iv_eur"] - c["iv"]) * 100
                if bias < 0.1:
                    errs.append(f"{ed} ATM put: European vol only {bias:+.2f} vol pts above American -- the pass is not doing anything")
                elif verbose:
                    print(f"    {ed} ATM put {k_atm:g}: a European inversion at the same forward reads {bias:+.2f} vol pts too high")
    if not doc_e.get("american", {}).get("model", "").startswith("NONE"):
        errs.append("--european-only did not stamp the bias")
    tot = sum(c["oi"] for st in doc["chain"].values() for r in st.values() for c in r.values())
    if diag["oi_records"] != 18 * diag["oi_instruments"] or diag["oi_disagree"]:
        errs.append("SPY OI dedupe: records per contract != 18 or disagreements")
    if diag["qual"]["one-sided"] > 0.05 * diag["quotes"]:
        errs.append(f"wave bucketing: {diag['qual']['one-sided']} one-sided quotes out of {diag['quotes']} "
                    f"-- exchanges publishing across minutes were not merged")
    quoted_anchor = {mn for mn in diag["anchor_ts"] if mn == 21 * 60 + 10}
    if not quoted_anchor or sum(n for mn, n in diag["anchor_ts"].items() if mn != 21 * 60 + 10) > diag["qual"]["unquoted"] + diag["qual"]["one-sided"]:
        errs.append(f"anchor should be the first QUOTED wave (21:10), not the 20:40 placeholder: {diag['anchor_ts']}")
    if diag["alt_n"] == 0 or diag["alt_med"] > 0.001:
        errs.append(f"the 21:30 re-broadcast should be a separate, identical wave: n={diag['alt_n']} med={diag['alt_med']}")
    return errs


# ---------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="quote date, YYYY-MM-DD (a complete day, not the dataset edge)")
    ap.add_argument("--roots", default="SPX", help="comma separated: SPX, SPXW, QQQ, SPY")
    ap.add_argument("--estimate", action="store_true", help="free: size and price every request")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="no network: synthetic tape with the traps planted")
    ap.add_argument("--selftest-out", default=None, help="also write the synthetic chain here")
    ap.add_argument("--out", default=os.path.join("data", "opra"))
    ap.add_argument("--max-mb", type=float, default=200.0, help="refuse a fetch whose estimate exceeds this")
    ap.add_argument("--oi-day", type=int, default=None,
                    help="calendar days after --date to read open interest from (default: next session; "
                         "0 = same day, which is one session STALE)")
    ap.add_argument("--stats-hours", default=None, metavar="H0-H1",
                    help="UTC hour window for the price-leg statistics (default: the whole day, so "
                         "the publication clock is measured)")
    ap.add_argument("--rate", type=float, default=None,
                    help="continuous rate for the American pass (default: the same-date SPX/SPXW parity curve)")
    ap.add_argument("--live-oi", action="store_true",
                    help="take the open-interest leg from a LIVE replay of the OI day (00:00Z on) instead of the "
                         "historical window -- for a same-morning pull; falls back to the historical leg, then STALE")
    ap.add_argument("--european-only", action="store_true",
                    help="price QQQ/SPY as European (Black-76) with the bias stamped -- for comparison only")
    a = ap.parse_args()
    roots = [r.strip().upper() for r in a.roots.split(",") if r.strip()]
    bad = [r for r in roots if r not in ROOTS]
    if bad:
        ap.error(f"unknown root(s) {bad}; known: {' '.join(ROOTS)}")
    if a.selftest:
        sys.exit(0 if selftest(out_path=a.selftest_out) else 1)
    if not a.date:
        ap.error("--date is required")
    if a.estimate:
        estimate(a.date, roots, a.oi_day, a.stats_hours, live=a.live_oi)
    elif a.fetch:
        global DEF_CACHE
        DEF_CACHE = Path(a.out) / "defs"
        fetch(a.date, roots, a.out, a.oi_day, a.stats_hours, a.max_mb, a.european_only, a.rate, a.live_oi)
    else:
        ap.error("pass --estimate, --fetch or --selftest")


if __name__ == "__main__":
    main()
