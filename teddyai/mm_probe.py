r"""
mm_probe.py — replay a bounded slice of real NQ MBO through mm_footprint and report.

Runs ON MARK'S MACHINE ONLY (needs DATABENTO_API_KEY in the environment; the key never
leaves it). Never fetches blind: without --fetch it prints the billable size and cost
estimate for the slice and exits. With --fetch it pulls once, caches the DBN file under
teddychart\cache\, and every later run replays from the cache with no key needed.

    python mm_probe.py --day 2026-09-04                       # estimate only
    python mm_probe.py --day 2026-09-04 --fetch               # pull 06:25-07:25 PT, replay, report
    python mm_probe.py --day 2026-09-04 --from 06:25 --to 07:25 --window 60

What the report answers, in order:
  1. Can Python keep up?  Records/s the tracker processes vs the tape's PEAK 1-second
     message rate. Live is only feasible in-process if processed >> peak.
  2. What do the lifetimes and sizes actually look like?  Percentiles of order life and
     shown size, so the thresholds in mm_footprint.DEFAULTS are looked at, not guessed.
  3. The split.  Per-minute flicker / mid / resting / big shares, big_pulled, the
     bid-ask balance of the tail buckets, aggressor delta, minute range, icebergs, and
     the levels carrying the most tail.
Nothing is written anywhere except the cache file and a JSON copy of the report.
"""
from __future__ import annotations

import argparse, collections, datetime, json, os, pathlib, sys, time
from zoneinfo import ZoneInfo

HERE = pathlib.Path(__file__).parent.resolve()
CACHE = HERE / "teddychart" / "cache"
sys.path.insert(0, str(HERE))
from mm_footprint import FootprintTracker, KINDS, NS, PRICE_SCALE  # noqa: E402

DATASET, SYMBOL, STYPE = "GLBX.MDP3", "NQ.c.0", "continuous"
PT = ZoneInfo("America/Los_Angeles")


def pt_to_utc(day: str, hhmm: str) -> datetime.datetime:
    h, m = map(int, hhmm.split(":"))
    y, mo, d = map(int, day.split("-"))
    return datetime.datetime(y, mo, d, h, m, tzinfo=PT).astimezone(datetime.timezone.utc)


def pct(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(round(q / 100 * (len(xs) - 1)))))
    return xs[i]


def fetch_or_cache(day, t0, t1, do_fetch):
    import databento as db
    path = CACHE / f"mbo-{day}-{t0:%H%M}-{t1:%H%M}.dbn.zst"
    if path.exists():
        print(f"cache hit: {path.name} ({path.stat().st_size/1e6:,.1f} MB)")
        return db.DBNStore.from_file(path)
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        sys.exit("DATABENTO_API_KEY is not set — run this on the trading machine.")
    cl = db.Historical(key)
    kw = dict(dataset=DATASET, symbols=[SYMBOL], stype_in=STYPE, schema="mbo",
              start=t0.isoformat(), end=t1.isoformat())
    size = cl.metadata.get_billable_size(**kw) / 1e6
    cost = cl.metadata.get_cost(**kw)
    print(f"mbo {SYMBOL} {t0:%Y-%m-%d %H:%M}-{t1:%H:%M} UTC: {size:,.1f} MB billable, "
          f"estimated cost ${cost:.2f}")
    if not do_fetch:
        print("estimate only — re-run with --fetch to pull.")
        sys.exit(0)
    print("pulling...")
    store = cl.timeseries.get_range(**kw)
    CACHE.mkdir(parents=True, exist_ok=True)
    store.to_file(path)
    print(f"cached -> {path}")
    return db.DBNStore.from_file(path)


def run(records, window_s: float, quiet=False, trk=None, trace_icebergs=0):
    """Shared replay loop. `trk` lets the caller pass a tracker with chosen cutoffs;
    `trace_icebergs=N` prints the raw record sequence behind the first N iceberg events."""
    trk = trk or FootprintTracker(window_s=window_s)
    per_sec = collections.Counter()
    inst = collections.Counter()
    lives, sizes, kinds = [], [], collections.Counter()
    by_kind_vol = collections.Counter()
    minutes = []
    step = int(window_s * NS)
    next_print = None
    n = 0
    wall0 = time.perf_counter()
    first_ts = last_ts = None
    win_lo = win_hi = None
    last_px = prev_close = None
    ring = {} if trace_icebergs else None
    traced = 0
    for rec in records:
        n += 1
        isd = isinstance(rec, dict)
        ts = int(rec["ts_event"] if isd else rec.ts_event)
        first_ts = ts if first_ts is None else first_ts
        last_ts = ts
        per_sec[ts // NS] += 1
        iid = rec.get("instrument_id") if isd else getattr(rec, "instrument_id", None)
        inst[iid] += 1
        act = rec["action"] if isd else rec.action
        if act == "T":
            p = int(rec["price"] if isd else rec.price)
            win_lo = p if win_lo is None or p < win_lo else win_lo
            win_hi = p if win_hi is None or p > win_hi else win_hi
            last_px = p
        if ring is not None and act != "T":
            oid = int(rec["order_id"] if isd else rec.order_id)
            r = ring.get(oid)
            if r is None:
                r = ring[oid] = collections.deque(maxlen=10)
            r.append((ts, act, rec["side"] if isd else rec.side,
                      int(rec["price"] if isd else rec.price), int(rec["size"] if isd else rec.size)))
        for ev in trk.on_record(rec):
            if ev.kind in KINDS:
                kinds[ev.kind] += 1
                by_kind_vol[ev.kind] += ev.size
                if ev.extra.get("life_s") is not None:
                    lives.append(ev.extra["life_s"])
                sizes.append(ev.size)
                if ring is not None:
                    ring.pop(ev.extra.get("order_id"), None)
            elif ev.kind == "iceberg" and ring is not None and traced < trace_icebergs:
                traced += 1
                oid = ev.extra.get("order_id")
                print(f"  iceberg #{traced}: order {oid} side {ev.side} px {ev.price*PRICE_SCALE:.2f} "
                      f"refill {ev.size} (refills so far {ev.extra.get('refills')})")
                t0 = None
                for (t, a, sd, p, sz) in ring.get(oid, []):
                    t0 = t if t0 is None else t0
                    print(f"      +{(t-t0)/1e6:9.3f} ms  {a} {sd} {p*PRICE_SCALE:.2f} x{sz}")
        if ring is not None and act == "C" and oid not in trk.orders:
            ring.pop(oid, None)                       # closed, or never seen added
        if next_print is None:
            next_print = ts + step
        if ts >= next_print:
            s = trk.window_summary(ts)
            s["t_utc"] = datetime.datetime.fromtimestamp(ts / NS, datetime.timezone.utc).strftime("%H:%M:%S")
            s["range_pts"] = ((win_hi - win_lo) * PRICE_SCALE) if win_lo is not None else 0.0
            s["ret_pts"] = ((last_px - prev_close) * PRICE_SCALE) if (last_px is not None and prev_close is not None) else 0.0
            prev_close = last_px if last_px is not None else prev_close
            win_lo = win_hi = None
            st = trk.standing(ts)                       # the book as it stands at the window's end
            for k, v in st.items():
                s["st_" + k] = v
            minutes.append(s)
            if not quiet:
                print(f"  {s['t_utc']}  pass={s['passive_vol']:6d} flick={s['flicker_share']:.2f} mid={s['mid_share']:.2f} "
                      f"rest={s['resting_share']:.3f} big={s['big']:4d} pulled={s['big_pulled']:3d} "
                      f"rbal={s['resting_bal']:+.2f} bbal={s['big_bal']:+.2f} "
                      f"delta={s['aggr_delta']:+6d} rng={s['range_pts']:5.2f} ret={s['ret_pts']:+6.2f} ice={s['iceberg_refills']:3d}"
                      f"  | standing tail {s['st_tail_bid']:5d}/{s['st_tail_ask']:<5d} bal={s['st_tail_bal']:+.2f} "
                      f"big {s['st_big_bid']:4d}/{s['st_big_ask']:<4d} bal={s['st_big_bal']:+.2f} alive={s['st_n_alive']}")
            while next_print <= ts:               # a gap in the tape skips windows, never repeats one
                next_print += step
    trk.flush()
    wall = time.perf_counter() - wall0
    span = (last_ts - first_ts) / NS if first_ts is not None and last_ts != first_ts else float("nan")
    peak = max(per_sec.values()) if per_sec else 0
    p95 = pct(list(per_sec.values()), 95)
    rep = {
        "cutoffs": {k: trk.p[k] for k in ("fast_life_s", "slow_life_s", "big_size")},
        "records": n, "tape_seconds": span, "wall_seconds": wall,
        "processed_per_s": n / wall if wall else float("nan"),
        "tape_mean_per_s": n / span if span == span and span else float("nan"),
        "tape_peak_per_s": peak, "tape_p95_per_s": p95,
        "instruments": dict(inst),
        "orders_closed": sum(kinds.values()), "orders_by_kind": dict(kinds),
        "volume_by_kind": dict(by_kind_vol),
        "life_s_pct": {q: pct(lives, q) for q in (10, 25, 50, 75, 90, 95, 99, 99.9)},
        "size_pct": {q: pct(sizes, q) for q in (10, 25, 50, 75, 90, 95, 99, 99.9)},
        "minutes": minutes,
    }
    lv = trk.level_table()
    lv.sort(key=lambda r: r["tail"] + r["aggr_buy"] + r["aggr_sell"], reverse=True)
    rep["top_levels"] = lv[:20]
    # the standing tail ladder around the last print, +-20 ticks
    st = trk.standing(last_ts, with_levels=True) if last_ts is not None else {}
    rep["standing_final"] = {k: v for k, v in st.items() if k != "levels"}
    if st.get("levels") and last_px is not None:
        lp = last_px * PRICE_SCALE
        tick = trk.p["tick"]
        rows = [(px, v) for px, v in st["levels"].items() if abs(px - lp) <= 20 * tick]
        rows.sort(reverse=True)
        rep["standing_ladder"] = [dict(price=px, tail_bid=v[0], tail_ask=v[1], big_bid=v[2], big_ask=v[3])
                                  for px, v in rows]
        rep["last_px"] = lp
    return rep


def print_report(rep):
    print("\n== 1. throughput ==")
    print(f"  records {rep['records']:,} over {rep['tape_seconds']:,.0f} s of tape; "
          f"processed in {rep['wall_seconds']:,.1f} s wall")
    print(f"  processed {rep['processed_per_s']:,.0f} rec/s  vs tape mean {rep['tape_mean_per_s']:,.0f}, "
          f"p95 {rep['tape_p95_per_s']:,.0f}, PEAK {rep['tape_peak_per_s']:,.0f} rec/s")
    ratio = rep["processed_per_s"] / rep["tape_peak_per_s"] if rep["tape_peak_per_s"] else float("nan")
    verdict = ("in-process live is plausible" if ratio > 3 else
               "marginal — separate process, or sample" if ratio > 1 else
               "cannot keep up live in Python — replay/offline only")
    print(f"  headroom over peak: {ratio:.1f}x -> {verdict}")
    if len(rep["instruments"]) > 1:
        print(f"  NOTE more than one instrument_id in the slice: {rep['instruments']}")
    print(f"\n== 2. distributions (closed orders; cutoffs {rep['cutoffs']}) ==")
    print(f"  orders closed {rep['orders_closed']:,}; by kind {rep['orders_by_kind']}")
    print(f"  volume by kind {rep['volume_by_kind']}")
    print("  life s   " + "  ".join(f"p{q}={v:.3g}" for q, v in rep["life_s_pct"].items()))
    print("  size     " + "  ".join(f"p{q}={v:.3g}" for q, v in rep["size_pct"].items()))
    if rep.get("standing_ladder"):
        print(f"\n== standing tail ladder at the end (last print {rep['last_px']:.2f}; contracts alive >= slow_life_s / >= big_size) ==")
        print(f"  {'price':>10s}  {'tail bid':>8s} {'tail ask':>8s}   {'big bid':>7s} {'big ask':>7s}")
        for r in rep["standing_ladder"]:
            mark = " <" if abs(r["price"] - rep["last_px"]) < 1e-9 else ""
            print(f"  {r['price']:10.2f}  {r['tail_bid']:8d} {r['tail_ask']:8d}   {r['big_bid']:7d} {r['big_ask']:7d}{mark}")
    print("\n== 3. levels with the most tail (resting+big) and aggression ==")
    for r in rep["top_levels"]:
        print(f"  {r['price']:10.2f}  flicker={r['flicker']:6d} mid={r['mid']:5d} resting={r['resting']:5d} "
              f"big={r['big']:5d} (pulled {r['big_pulled']:4d})  aggrB={r['aggr_buy']:6d} aggrS={r['aggr_sell']:6d}  ice={r['iceberg_refills']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="trading date YYYY-MM-DD (the RTH day)")
    ap.add_argument("--from", dest="t0", default="06:25", help="PT start, default 06:25")
    ap.add_argument("--to", dest="t1", default="07:25", help="PT end, default 07:25")
    ap.add_argument("--window", type=float, default=60.0, help="summary window seconds")
    ap.add_argument("--fetch", action="store_true", help="actually pull if not cached")
    ap.add_argument("--quiet", action="store_true", help="no per-window lines")
    a = ap.parse_args()
    t0, t1 = pt_to_utc(a.day, a.t0), pt_to_utc(a.day, a.t1)
    store = fetch_or_cache(a.day, t0, t1, a.fetch)
    print(f"replaying {a.day} {a.t0}-{a.t1} PT through mm_footprint (window {a.window:.0f}s)")
    rep = run(store, a.window, quiet=a.quiet)
    print_report(rep)
    out = HERE / "Claude outputs" / f"mm_probe-{a.day}-{a.t0.replace(':','')}-{a.t1.replace(':','')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rep, indent=1, default=float))
    print(f"\nreport json -> {out}")


if __name__ == "__main__":
    main()
