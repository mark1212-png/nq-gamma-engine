#!/usr/bin/env python3
"""
databento_pull.py — licensed replacement for the manual QuikStrike export.

Pulls, for one settlement date:
  * per-strike open interest        (statistics schema, stat_type 9)
  * per-strike settlement price     (statistics schema, stat_type 3)
  * the underlying futures settles  (same, on the futures roots)
  * contract definitions            (definition schema: strike, expiry, C/P)

and writes a normalised JSON the gamma analysis consumes.

Two modes:
    python databento_pull.py --estimate --date 2026-09-03
    python databento_pull.py --fetch    --date 2026-09-03

RUN --estimate FIRST. It calls metadata.get_cost, which is free and downloads
no data, and prints the dollar figure for the exact query --fetch would run.
Nothing is billed until you run --fetch.

Setup:
    pip install --user databento numpy
    setx DATABENTO_API_KEY "db-xxxxxxxx"        (Windows, new shell after)

Why this exists: CME and QuikStrike both prohibit automated retrieval from
their websites.  Databento is a licensed redistributor, so this route is
permitted -- but you must complete their licensing questionnaire first and
your subscriber status determines the exchange fee passed through.  See
METHOD.md.
"""

import os, re, sys, json, math, argparse, datetime
from math import log, sqrt, exp, pi

try:
    import databento as db
except ImportError:
    sys.exit("pip install --user databento")

import nqvol      # the one implementation of time, vol and Greeks (2026-09-05)

DATASET = "GLBX.MDP3"

# CME splits an option chain across many roots.  NQ quarterlies live on NQ,
# end-of-month on NE, Friday weeklies on QN1..QN5, and the intraweek weeklies
# on Q<n>A (Mon), Q<n>B (Tue), Q<n>C (Wed), Q<n>D (Thu).  Requesting NQ.OPT
# alone silently returns only the quarterlies -- which is most of the open
# interest but none of the 0DTE contracts the intraday plan is built on.
# Root codes are not guessable and a wrong one fails silently on a batch request.
# Probed 2026-09-02: "NE" errors; the end-of-month root is "QNE" (2.7 MB, the
# largest single root -- it was 16,696 contracts of missing open interest).
# Q1A / Q4x / Q5x / QN5 come back empty because those weeks are expired or not
# yet listed, not because the codes are wrong; they stay so they work later in
# the month.  Do NOT add E1A or other E-mini S&P roots -- they resolve fine and
# would silently mix ES options into the NQ book.  The asset filter below is the
# backstop for exactly that mistake.
ASSET = "NQ"

OPT_ROOTS = (
    ["NQ", "QNE"]
    + [f"QN{i}" for i in range(1, 6)]
    + [f"Q{i}{c}" for i in range(1, 6) for c in "ABCD"]
)
FUT_ROOTS = ["NQ"]

OPT_SYMBOLS = [f"{r}.OPT" for r in OPT_ROOTS]
FUT_SYMBOLS = [f"{r}.FUT" for r in FUT_ROOTS]

STAT_SETTLEMENT = 3
STAT_OPEN_INTEREST = 9


# Statistics publish many stat_types across the session; we need only 3
# (settlement) and 9 (open interest), and those post in a narrow band after the
# close.  A wide window bills for a lot of records we discard.  Times are UTC.
# CME equity-index close is 20:00 UTC (4pm ET, EDT); the final settlement
# bulletin lands ~15:00 UTC the next business day.
# Definitions and statistics want DIFFERENT windows, and conflating them is a
# trap.  Databento publishes the instrument-definition snapshot at UTC midnight,
# so a definition request that does not start at midnight silently returns a
# partial chain -- that is why the "final" window showed 0.9 MB of definitions
# against 21.5 MB for the midnight-anchored one.  Statistics have no such
# constraint and only need the post-close band.
WINDOWS = {
    "full":   (0,  0, 1, 18),   # the original 42h window
    "eod":    (0, 20, 1,  2),   # close -> midnight, preliminary settle
    "final":  (1, 12, 1, 18),   # next-day final settlement bulletin
    "narrow": (0, 20, 0, 23),   # 3h after the close
}
DEF_WINDOW = (0, 0, 0, 6)       # midnight-anchored: required for a full snapshot

# The two numbers we need are published at DIFFERENT times, for a reason:
#   settlement price -- struck at the 15:00 CT close, disseminated that evening
#                       (20:00-23:00 UTC on the trade date)
#   open interest    -- a CLEARING figure, computed overnight once the day's
#                       trades are matched, published the next morning
#                       (~15:00 UTC, T+1)
# One window cannot hold both without spanning ~22h and paying for the whole
# session in between. So we make two requests and join them on raw_symbol --
# not on instrument_id, which is not stable across UTC days on GLBX.
SETTLE_DAY, SETTLE_WINDOW = 0, (0, 20, 0, 23)   # trade date, evening
OI_DAY,     OI_WINDOW     = 1, (0, 12, 0, 18)   # next day, morning


def next_business_day(d, n=1):
    """
    "T+1" for a clearing figure means the next day the clearing house runs, not
    the next day on the calendar. A Friday trade date settles overnight and its
    open interest publishes MONDAY morning; asking for Saturday returns futures
    settles (stamped Friday evening) and an empty option chain -- which is
    exactly the silent failure that emptied every Friday in the backtest.
    Holiday-aware since 2026-09-05 (nqvol.HOLIDAYS); the walk-forward retry in
    fetch() still covers an unlisted closure or a late publication.
    """
    return nqvol.next_session(d, n)


def _win(d, spec):
    d0, h0, d1, h1 = spec
    return (f"{d + datetime.timedelta(days=d0)}T{h0:02d}:00:00",
            f"{d + datetime.timedelta(days=d1)}T{h1:02d}:00:00")


def session_window(date_str, window="final"):
    return _win(datetime.date.fromisoformat(date_str), WINDOWS[window])


def def_window(date_str, window="final"):
    """
    Definitions must start at UTC midnight, AND must be anchored to the same UTC
    day the statistics are drawn from.  Databento does not guarantee that
    instrument_id is stable across days on GLBX, so definitions from Sep 1 will
    not reliably join to statistics from Sep 2 -- the ids simply do not line up.
    """
    d = datetime.date.fromisoformat(date_str)
    stats_start_day = d + datetime.timedelta(days=WINDOWS[window][0])
    return _win(stats_start_day, DEF_WINDOW)


AVAIL_HINT = """
  Databento historical gates RECENT data behind a live subscription. The error
  names the cutoff: any window ending after it needs a live plan, not
  pay-as-you-go. Measured 2026-09-03: the cutoff trails real time by ~8 hours.

  Consequence for a premarket workflow: open interest for session T publishes
  T+1 morning (~12:00-18:00 UTC), and that window only clears the embargo about
  8h later -- i.e. the evening of T+1. So historical-only lands you one full
  session behind what QuikStrike shows you for free.

  Options: (a) run the pull in the EVENING for the next morning's plan and
  accept the staleness, (b) add a live subscription to remove the embargo,
  (c) keep QuikStrike for freshness and use Databento for the settlement-derived
  smile and history.
"""


def availability(date_str):
    """
    FREE. Reports how current the dataset is, using the metadata endpoint
    rather than parsing an error string.

    Two different failures look similar and mean opposite things:
      dataset_unavailable_range   -> LICENSING. Recent data needs a live plan.
      data_start_after_available_end -> the data simply is not published yet.
    The first is a wall; the second is a clock.
    """
    c = client()
    try:
        rng = c.metadata.get_dataset_range(dataset=DATASET)
    except Exception as ex:
        print(f"\n  could not read dataset range: {type(ex).__name__}: {ex}\n")
        return
    end = str(rng.get("end") or rng.get("end_date") or "")
    start = str(rng.get("start") or rng.get("start_date") or "")
    now = datetime.datetime.now(datetime.timezone.utc)
    print(f"\n  {DATASET} available range")
    print(f"    from {start}")
    print(f"    to   {end}")
    try:
        e = datetime.datetime.fromisoformat(end.replace("Z", "+00:00").split(".")[0]
                                            + ("+00:00" if "+" not in end else ""))
        lag = (now - e).total_seconds() / 60
        print(f"    now  {now:%Y-%m-%d %H:%M} UTC   -> {lag:+.0f} min behind real time")
        print("    live subscription active: no embargo" if lag < 120 else
              "    still embargoed -- recent data needs a live plan")
    except Exception:
        pass

    d = datetime.date.fromisoformat(date_str)
    # Three windows, because there are three different things: the settlement
    # bulletin (evening of T), PRELIMINARY open interest (00:00-06:00 UTC the
    # next calendar day -- what --job prelim pulls), and FINAL open interest
    # (next BUSINESS day, 12:00-18:00 UTC -- what the morning build pulls).
    # Reporting only the final made a Saturday check read "NOT YET PUBLISHED"
    # for a Friday whose preliminary had been available since Friday evening.
    rows = [("settlement", d + datetime.timedelta(days=SETTLE_DAY), SETTLE_WINDOW),
            ("prelim OI", d + datetime.timedelta(days=1), (0, 0, 0, 6)),
            ("final OI", next_business_day(d, OI_DAY), OI_WINDOW)]
    for label, day, spec in rows:
        ws, we = _win(day, spec)
        ready = ("in the dataset" if we <= end[:19] else
                 "PARTLY in the dataset" if ws < end[:19] else "NOT YET in the dataset")
        print(f"    {label:14} window {ws[5:16]} -> {we[5:16]}   {ready}")
    print("    ('in the dataset' means Databento has the window; whether CME put OI in it is\n"
          "     what --job prelim / the fetch retry find out)")
    print()


def client():
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        sys.exit("set DATABENTO_API_KEY first")
    return db.Historical(key)


# ------------------------------------------------------------------ cost ---

def estimate(date_str, window=None):
    """Free: get_cost transfers nothing. Definitions are always priced on the
    midnight-anchored window, because anything else undercounts the chain."""
    c = client()
    ds, de = def_window(date_str, window or "final")
    dsz = c.metadata.get_billable_size(dataset=DATASET, symbols=OPT_SYMBOLS + FUT_SYMBOLS,
                                       schema="definition", stype_in="parent", start=ds, end=de)
    dcost = c.metadata.get_cost(dataset=DATASET, symbols=OPT_SYMBOLS + FUT_SYMBOLS,
                                schema="definition", stype_in="parent", start=ds, end=de)
    print(f"\ncost estimate for {date_str}   (no data downloaded, nothing billed)\n")
    print(f"  definitions  {ds[5:16]} -> {de[5:16]}  midnight-anchored  "
          f"{dsz/1e6:7.1f} MB  ${dcost:.4f}")
    print(f"\n  {'stats win':<10} {'span':<26} {'MB':>9} {'USD':>9} {'TOTAL/day':>11}")
    best = None
    for w in ([window] if window else list(WINDOWS)):
        start, end = session_window(date_str, w)
        sz = tot = 0
        for syms in (OPT_SYMBOLS, FUT_SYMBOLS):
            kw = dict(dataset=DATASET, symbols=syms, schema="statistics",
                      stype_in="parent", start=start, end=end)
            sz += c.metadata.get_billable_size(**kw); tot += c.metadata.get_cost(**kw)
        grand = tot + dcost
        print(f"  {w:<10} {start[5:16]+' -> '+end[5:16]:<26} {sz/1e6:9.1f} "
              f"{tot:9.4f} {grand:11.4f}")
        if best is None or grand < best[1]:
            best = (w, grand)
    print(f"\n  cheapest complete: definitions + {best[0]} stats = ${best[1]:.4f}/day"
          f"  (~${best[1]*252:.2f}/yr)")
    print(f"TEDDY_COST={best[1]:.6f}")
    print(f"  $125 credit = ~{int(125/best[1]) if best[1] else 0} days.")
    print("  Verify after the first fetch: strike count and total OI must match the\n"
          "  baseline. A cheap window that drops half the chain is not a saving.\n")


# ----------------------------------------------------------------- fetch ---

_AVAIL_END = []          # memoised; one metadata call per process


def _avail_end(c):
    """
    The last timestamp GLBX.MDP3 actually holds. It trails real time by a few
    hours, so ANY window whose end runs past it is rejected outright with
    422 data_end_after_available_end -- even when every record we want is
    already inside it. The open-interest window ends at 18:00 UTC and OI
    publishes around 12:00, so on a same-day pull the request is refused hours
    after the data we need has landed. Clamping keeps the request legal without
    changing what it returns.
    """
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
            raise SystemExit(
                f"  [{label}] {what} window {start} -> {end} is entirely past the end of the\n"
                f"  dataset ({a}). Nothing to fetch yet -- wait for CME to publish and retry.")
        print(f"  [{label}] {what} end clamped {end} -> {a} (dataset ends there)")
        return a
    return end


DEF_CACHE = None          # set from --out: <out>/defs/<utc-day>.pkl
DEF_COLS = ["instrument_id", "raw_symbol", "instrument_class", "strike_price",
            "expiration", "underlying", "asset"]


def _defs(c, day, label):
    """
    The instrument-definition snapshot for ONE utc day, from a local cache
    when we already have it.

    This is where a pull spends its time: the midnight snapshot is ~21 MB
    across 27 roots, it is requested twice per fetch (settle day and
    open-interest day) and again for every retry, and a morning that runs
    premarket three times paid for it six times. The snapshot for a PAST day
    is immutable, so it is cached once the window is known to be complete --
    i.e. never when the dataset end clamped it, because a clamped snapshot is
    a partial chain and caching it would turn a delay into a permanent hole.
    Only the columns the join uses are kept.
    """
    ds, de = _win(day, DEF_WINDOW)
    f = (DEF_CACHE / f"{day}.pkl") if DEF_CACHE else None
    if f and f.exists():
        try:
            import pandas as pd
            defs = pd.read_pickle(f)
            print(f"  [{label}] defs {ds} -> {de}  (cached, {len(defs):,} rows)")
            return defs, True
        except Exception as ex:
            print(f"  [{label}] defs cache unreadable ({ex}); refetching")
    de_c = _clamp(c, ds, de, label, "defs")
    print(f"  [{label}] defs {ds} -> {de_c}")
    defs = c.timeseries.get_range(dataset=DATASET, schema="definition", stype_in="parent",
                                  symbols=OPT_SYMBOLS + FUT_SYMBOLS, start=ds, end=de_c).to_df()
    if f and de_c == de and len(defs):
        try:
            keep = [k for k in DEF_COLS if k in defs.columns]
            DEF_CACHE.mkdir(parents=True, exist_ok=True)
            defs[keep].to_pickle(f)
        except Exception as ex:
            print(f"  [{label}] could not cache defs: {ex}")
    return defs, False


def _pull(c, day, stats_spec, label):
    """Definitions + statistics for ONE utc day, keyed by raw_symbol."""
    defs, _ = _defs(c, day, label)
    ss, se = _win(day, stats_spec)
    se = _clamp(c, ss, se, label, "stats")
    print(f"  [{label}] stats {ss} -> {se}")
    # One statistics request for options and futures together. They were two
    # round trips to the same window; the join is by instrument_id either way.
    stats = c.timeseries.get_range(dataset=DATASET, schema="statistics", stype_in="parent",
                                   symbols=OPT_SYMBOLS + FUT_SYMBOLS, start=ss, end=se).to_df()
    d = defs.drop_duplicates("instrument_id", keep="last").set_index("instrument_id")
    sym = d["raw_symbol"].astype(str).to_dict()
    counts = stats["stat_type"].value_counts().to_dict() if len(stats) else {}
    print(f"  [{label}] defs {len(defs):,}  stats {len(stats):,}  stat_types "
          + ", ".join(f"{k}:{v:,}" for k, v in sorted(counts.items())[:10]))
    return d, sym, stats


def _by_symbol(stats, sym, stat_type, col):
    if not len(stats) or "stat_type" not in stats.columns:
        return {}
    s_ = stats[stats["stat_type"] == stat_type].drop_duplicates("instrument_id", keep="last")
    return {sym[i]: v for i, v in zip(s_["instrument_id"], s_[col]) if i in sym}


def fetch(date_str, outdir, window="final", oi_day=None, oi_hours=None,
          settle_only=False):
    c = client()
    d0 = datetime.date.fromisoformat(date_str)
    d_settle = d0 + datetime.timedelta(days=SETTLE_DAY)
    d_oi     = ((d0 + datetime.timedelta(days=oi_day)) if oi_day is not None
                else next_business_day(d0, OI_DAY))
    oi_win = OI_WINDOW
    if oi_hours:
        h0, h1 = (int(x) for x in oi_hours.split("-"))
        oi_win = (0, h0, 1, 0) if h1 >= 24 else (0, h0, 0, h1)
        print(f"  [oi] window overridden to {d_oi} {h0:02d}:00-{h1:02d}:00 UTC")
    if (d_oi - d0).days > OI_DAY:
        print(f"  [oi] {d0} is a {d0:%A} -- open interest publishes {d_oi}, not "
              f"{d0 + datetime.timedelta(days=OI_DAY)} (weekend)")

    # Ask the free question first. When the open-interest window has not
    # opened in the dataset yet, the old order pulled the whole settle leg
    # (definitions and all, ~50 s) and THEN found out there was nothing to
    # join it to. Same outcome, same message, before the download instead of after.
    if not settle_only:
        os_, oe_ = _win(d_oi, oi_win)
        a_ = _avail_end(c)
        if a_ and os_ >= a_:
            raise SystemExit(
                f"  [oi] stats window {os_} -> {oe_} is entirely past the end of the\n"
                f"  dataset ({a_}). Nothing to fetch yet -- wait for CME to publish and retry.")

    dS, symS, statS = _pull(c, d_settle, SETTLE_WINDOW, "settle")
    settle = _by_symbol(statS, symS, STAT_SETTLEMENT, "price")

    # Skipping the weekend is not enough: a Thursday whose open interest would
    # publish on a HOLIDAY Friday (Juneteenth, July 3rd) comes back empty the
    # same way. Rather than carry a CME holiday calendar that will rot, walk
    # forward until a window actually contains open interest. Statistics is a
    # tiny L0 schema, so a wasted attempt costs almost nothing, and this also
    # absorbs an ordinary publication delay.
    oi, dO, symO = {}, None, None
    if settle_only:
        # The settlement bulletin is complete by ~23:00 UTC; open interest is a
        # clearing figure and will not exist until tomorrow morning. Asking for
        # it now guarantees an empty window and a wasted request.
        print("  [oi] skipped -- settle-only run")
        d_oi = d_settle
    else:
        for attempt in range(3):
            dO, symO, statO = _pull(c, d_oi, oi_win, "oi")
            oi = _by_symbol(statO, symO, STAT_OPEN_INTEREST, "quantity")
            if len(oi) >= 100:
                break
            if oi_day is not None or oi_hours:
                print(f"  [oi] {d_oi} carried {len(oi)} records in the overridden "
                      f"window -- not retrying, this is a probe.")
                break
            nxt = next_business_day(d_oi, 1)
            print(f"  [oi] {d_oi} carried {len(oi)} open-interest records -- market "
                  f"holiday or delayed publication. Trying {nxt}.")
            d_oi = nxt
    print(f"\n  settlement prices: {len(settle):,}   open interest: {len(oi):,}"
          f"   symbols in both: {len(set(settle) & set(oi)):,}"
          f"   (OI window: {d_oi})")

    def as_class(v):
        t = str(v).strip().upper()
        return "C" if t in ("C", "CALL", "67") else ("P" if t in ("P", "PUT", "80") else None)

    def num(v):
        # Databento fixed-price fields are int64 scaled by 1e-9, always. The old
        # magnitude guess (">1e6 means scaled") happened to work because the
        # smallest option tick is 0.05; it would have passed a genuinely tiny
        # value through unscaled. Use the library's own constant when present.
        x = float(v)
        scale = getattr(db, "FIXED_PRICE_SCALE", 1e-9)
        return x * scale if float(v).is_integer() and abs(x) >= 1e6 else x

    # futures settles come from the settle-day pull, keyed by symbol
    fut = {}
    for iid, row in dS.iterrows():
        sym_ = str(row.get("raw_symbol", ""))
        if as_class(row.get("instrument_class")) is None and sym_ in settle:
            fut[sym_] = num(settle[sym_])

    meta = {}
    for d in (dO, dS):
        if d is None:          # settle-only: there was no OI leg to define from
            continue
        for iid, row in d.iterrows():
            meta.setdefault(str(row.get("raw_symbol", "")), row)

    chain, skipped, wrong_assets = {}, {"no_def": 0, "not_option": 0, "bad": 0,
                                        "wrong_asset": 0}, {}
    source = oi if not settle_only else {sym_: 0 for sym_ in settle}
    for sym_, n in source.items():
        row = meta.get(sym_)
        if row is None:
            skipped["no_def"] += 1; continue
        # Guard against a foreign root (E1A etc). Check UNDERLYING, not asset:
        # for CME weeklies `asset` is the option root itself ("Q1D", "QN1"), so
        # testing asset == "NQ" drops every weekly and leaves only quarterlies.
        # `underlying` is the futures contract it settles into -- NQU6, NQZ6 --
        # which is the thing that actually identifies the product.
        und = str(row.get("underlying", "")).strip().upper()
        if und and not und.startswith(ASSET):
            skipped["wrong_asset"] += 1
            wrong_assets[und[:4]] = wrong_assets.get(und[:4], 0) + 1
            continue
        cls = as_class(row.get("instrument_class"))
        if cls is None:
            skipped["not_option"] += 1; continue
        try:
            strike = num(row["strike_price"]); exp_raw = row.get("expiration")
            # A zero-OI contract is noise in a normal pull, but in a settle-only
            # run EVERY contract has zero OI by construction -- filtering on it
            # would empty the chain.
            if not settle_only and (n is None or float(n) <= 0):
                continue
            if exp_raw is None or str(exp_raw) == "NaT":
                continue
        except Exception:
            skipped["bad"] += 1; continue
        root = sym_.split()[0] if " " in sym_ else sym_[:5]
        key = f"{str(exp_raw)[:10]}|{root}"
        rec = chain.setdefault(key, {}).setdefault(f"{strike:.0f}", {})
        rec[cls] = {"oi": int(n),
                    "settle": num(settle[sym_]) if sym_ in settle else None,
                    "underlying": str(row.get("underlying", "")),
                    "expiration": str(exp_raw), "root": root, "symbol": sym_}
    print(f"  skipped: {skipped}")
    if skipped["wrong_asset"]:
        print(f"  !! {skipped['wrong_asset']:,} contracts dropped, underlying not {ASSET}*: "
              + ", ".join(f"{k} x{v:,}" for k, v in sorted(wrong_assets.items(),
                                                           key=lambda kv: -kv[1])[:6]))
    roots_seen = sorted({k.split("|")[1] for k in chain})
    label = "roots with settlement prices" if settle_only else "roots with open interest"
    print(f"  {label} ({len(roots_seen)}): {' '.join(roots_seen)}")

    # ---- solve the volatility smile from settlement prices -------------------
    # CME does not publish IV; we invert it. One implementation for the whole
    # project (nqvol.solve_chain): Black-76, bisection, T in BUSINESS TIME --
    # sessions, variance-weighted -- measured from the 20:00 UTC settlement
    # instant (it was 21:00 here, an hour late, and calendar time; see the
    # 2026-09-05 audit). Deep OTM options settle at or near the minimum tick,
    # where the inverted vol is meaningless, so anything with under
    # nqvol.MIN_PREMIUM points of extrinsic is marked rather than solved.
    graded, settled = nqvol.solve_chain({"date": date_str, "futures_settle": fut,
                                         "chain": chain}, settle_date=date_str, force=True)
    print(f"  solved IV on {graded} of {settled} settled contracts "
          f"({settled-graded} below the {nqvol.MIN_PREMIUM:g}-point premium floor)")

    os.makedirs(outdir, exist_ok=True)
    suffix = "settle" if settle_only else "chain"
    path = os.path.join(outdir, f"{date_str}-{suffix}.json")
    if not settle_only and not chain:
        # An empty chain with a real filename is a failed pull wearing a name.
        # It sits under autopull's 500 KB gate so nothing builds on it, but it
        # also blocks the eye: "the file is there". Friday evenings produce
        # this every week -- CME's feed is dark from Friday 16:00 CT to Sunday
        # 17:00 CT, so Friday's preliminary OI is not broadcast until the
        # Sunday reopen (--oi-day 2 --oi-hours 22-24) or the Tuesday final.
        raise SystemExit(f"  no open-interest records in the {d_oi} window -- not writing {path}.\n"
                         f"  (a Friday trade date: preliminary OI is broadcast at the Sunday 22:00 UTC "
                         f"reopen, not Saturday; the final comes on the next business morning)")
    json.dump({"date": date_str, "settle_only": bool(settle_only),
               "iv_basis": nqvol.IV_BASIS, "iv_ref": nqvol.close_utc(d0).isoformat(timespec="minutes"),
               "futures_settle": fut, "chain": chain},
              open(path, "w", encoding="utf-8"), indent=1)

    nexp = len(chain)
    nk = sum(len(v) for v in chain.values())
    tot_oi = sum(r[c]["oi"] for v in chain.values() for r in v.values() for c in r)
    if settle_only:
        print(f"  settle-only: {nk:,} strikes priced, open interest not yet published")
    else:
        print(f"  total open interest: {tot_oi:,}   (baseline for 2026-09-01 was 123,488)")
    print(f"\n  wrote {path}")
    print(f"  {nexp} expiries, {nk} strikes with open interest")
    print(f"  futures settles: " + ", ".join(f"{k} {v:,.2f}" for k, v in sorted(fut.items())[:6]))
    atm = []
    for expiry, strikes in sorted(chain.items()):
        best = None
        for ks, rec in strikes.items():
            for cp in ("C", "P"):
                iv = rec.get(cp, {}).get("iv")
                if not iv: continue
                und = rec[cp].get("underlying"); F = fut.get(und)
                if not F: continue
                dist = abs(float(ks) - F)
                if best is None or dist < best[0]: best = (dist, float(ks), iv)
        if best: atm.append((expiry, best[1], best[2]))
    if atm:
        print("\n  ATM implied vol by expiry (from settlement, not from a screenshot):")
        for e, k, v in atm[:8]:
            print(f"    {e:8} K {k:9,.0f}   {v*100:6.2f}%")


# -------------------------------------------------------------------- iv ---
# Kept as names for anything that imported them; the implementation is nqvol's.
bs76_price = nqvol.bs76_price
implied_vol = nqvol.implied_vol


def probe(date_str, extra):
    """
    FREE. Prices each option root on its own so dead roots show as 0 bytes.
    Use it when an expiry you expect is missing from a fetch -- CME root codes
    are not guessable and a wrong one fails silently rather than erroring.
    """
    c = client()
    d = datetime.date.fromisoformat(date_str)
    ds, de = _win(d, DEF_WINDOW)
    roots = OPT_ROOTS + [r for r in extra if r not in OPT_ROOTS]
    print(f"\nroot probe for {date_str}  (definitions only, nothing billed)\n")
    live, dead = [], []
    for r in roots:
        try:
            sz = c.metadata.get_billable_size(dataset=DATASET, symbols=[f"{r}.OPT"],
                                              schema="definition", stype_in="parent",
                                              start=ds, end=de)
        except Exception as ex:
            sz = -1
            note = f"  ({type(ex).__name__})"
        else:
            note = ""
        (live if sz > 0 else dead).append(r)
        flag = "ok " if sz > 0 else ("ERR" if sz < 0 else "-- ")
        print(f"  {flag} {r:<6} {max(sz,0):>10,} bytes{note}")
    print(f"\n  live roots ({len(live)}): {' '.join(live)}")
    print(f"  dead roots ({len(dead)}): {' '.join(dead)}")
    print("  a dead root returns no data and no error -- that is how an expiry")
    print("  goes missing without anything looking broken.\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="settlement date, YYYY-MM-DD")
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--avail", action="store_true",
                    help="free: report the current data-availability cutoff")
    ap.add_argument("--probe", action="store_true",
                    help="free: price each option root separately to find dead ones")
    ap.add_argument("--roots", default="",
                    help="extra candidate roots to test with --probe, comma separated")
    ap.add_argument("--out", default="data")
    # Let the OI window be overridden so "is preliminary open interest out yet?"
    # can be answered by measurement rather than assumption.
    ap.add_argument("--oi-day", type=int, default=None,
                    help="days after the trade date to look for open interest "
                         "(default: next business day)")
    ap.add_argument("--oi-hours", default=None, metavar="H0-H1",
                    help="UTC hour window for the OI pull, e.g. 22-24 or 0-6")
    ap.add_argument("--settle-only", action="store_true",
                    help="capture SETTLEMENT prices only and skip open interest. "
                         "Useful in the afternoon, when the settlement bulletin is "
                         "complete but OI will not clear until tomorrow morning.")
    ap.add_argument("--window", default=None, choices=list(WINDOWS),
                    help="time window to request; default prices all on --estimate, "
                         "and uses 'eod' on --fetch")
    a = ap.parse_args()
    if a.avail:
        availability(a.date)
    elif a.probe:
        probe(a.date, [r.strip().upper() for r in a.roots.split(",") if r.strip()])
    elif a.estimate:
        estimate(a.date, a.window)
    elif a.fetch:
        global DEF_CACHE
        DEF_CACHE = __import__("pathlib").Path(a.out) / "defs"
        fetch(a.date, a.out, a.window or "final", a.oi_day, a.oi_hours,
              a.settle_only)
    else:
        ap.error("pass --estimate or --fetch")


if __name__ == "__main__":
    main()
