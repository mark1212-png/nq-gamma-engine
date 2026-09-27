r"""
mm_calib.py — set the mm_footprint cutoffs from real NQ, then check the shape holds
on a different day and a different hour.

Runs on Mark's machine (key from the environment; never leaves it). For every slice it
prints the billable size and cost estimate FIRST and pulls only when the estimate is
$0.00 — a non-zero estimate is printed and that slice is skipped, never fetched.
Cached slices are replayed free and keyless.

    python mm_calib.py                 # default slices, writes mm_calib.json, full report
    python mm_calib.py --no-write      # report only, leave mm_calib.json alone
    python mm_calib.py --reuse         # skip pass 1, take the cutoffs from mm_calib.json

Slices (PT):
  CALIB   2026-09-04 06:25-07:25   the RTH open hour already cached by mm_probe
  CHECK   2026-09-10 06:25-07:25   a different day, same hour
  CHECK   2026-09-04 10:00-11:00   the same day, a quieter hour

Cutoffs are percentiles of the CALIB slice's closed orders:
  fast_life_s = p90 lifetime      slow_life_s = p99 lifetime      big_size = floor(p99 size) + 1
Written to mm_calib.json with provenance; mm_footprint loads it when present.

For each slice the report gives the bucket shares, then two DESCRIPTIVE checks per
per-minute series (no claim of predictive power is made or tested here):
  CV      coefficient of variation across minutes — a series that never moves
          (the flicker share) cannot carry information; the tail buckets must move
  corr    Pearson correlation with |aggressor delta| and with the minute's price range,
          contemporaneous — does the tail show up when the tape is doing something
It also prints the raw record sequence behind the first iceberg events of the CALIB
slice, to settle whether the one-lot "refills" are real refreshes or an artefact.

Standing book (step 4). Closure-based balances are partly mechanical (a lifted ask
closes in the minute of the buying). The standing measures are a STATE at each
window's end: contracts alive past slow_life_s by side, and big shown size by side.
For each, the report gives r with the SAME minute's delta and return, and r with the
NEXT minute's — the second is the only one that could mean anything, and with ~58
minutes per slice it is a description, not a test. Baselines printed beside them:
r(delta_t, ret_t), which must be large and positive, and r(delta_t, ret_t+1).
"""
from __future__ import annotations

import argparse, datetime, json, math, os, pathlib, statistics, sys

HERE = pathlib.Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE))
import mm_footprint  # noqa: E402
from mm_footprint import FootprintTracker, KINDS  # noqa: E402
from mm_probe import fetch_or_cache, pt_to_utc, run, pct, CACHE  # noqa: E402

SLICES = [
    dict(day="2026-09-04", t0="06:25", t1="07:25", role="CALIB"),
    dict(day="2026-09-10", t0="06:25", t1="07:25", role="CHECK"),
    dict(day="2026-09-04", t0="10:00", t1="11:00", role="CHECK"),
]
SERIES = ["flicker_share", "mid_share", "resting_share", "big", "big_pulled",
          "resting_bal", "big_bal", "iceberg_refills"]


def estimate_then_fetch(sl):
    """Estimate first; fetch only at $0.00. Returns a DBNStore or None."""
    import databento as db
    t0, t1 = pt_to_utc(sl["day"], sl["t0"]), pt_to_utc(sl["day"], sl["t1"])
    path = CACHE / f"mbo-{sl['day']}-{t0:%H%M}-{t1:%H%M}.dbn.zst"
    if path.exists():
        print(f"  cache hit: {path.name} ({path.stat().st_size/1e6:,.1f} MB)")
        return db.DBNStore.from_file(path)
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        print("  DATABENTO_API_KEY not set and no cache — skipping")
        return None
    cl = db.Historical(key)
    kw = dict(dataset="GLBX.MDP3", symbols=["NQ.c.0"], stype_in="continuous", schema="mbo",
              start=t0.isoformat(), end=t1.isoformat())
    size = cl.metadata.get_billable_size(**kw) / 1e6
    cost = cl.metadata.get_cost(**kw)
    print(f"  estimate: {size:,.1f} MB billable, ${cost:.2f}")
    if cost > 0.0:
        print("  NOT free on this plan — skipped, not fetched. Say so before pulling it.")
        return None
    print("  pulling...")
    store = cl.timeseries.get_range(**kw)
    CACHE.mkdir(parents=True, exist_ok=True)
    store.to_file(path)
    print(f"  cached -> {path.name}")
    return db.DBNStore.from_file(path)


def cv(xs):
    xs = [float(x) for x in xs]
    if len(xs) < 2 or statistics.mean(xs) == 0:
        return float("nan")
    return statistics.pstdev(xs) / abs(statistics.mean(xs))


def corr(xs, ys):
    xs, ys = [float(x) for x in xs], [float(y) for y in ys]
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs); syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return float("nan")
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def describe(rep, label):
    m = rep["minutes"]
    print(f"\n-- {label}: {rep['records']:,} records, {rep['orders_closed']:,} orders closed, "
          f"{len(m)} minutes; cutoffs {rep['cutoffs']}")
    tot = sum(rep["volume_by_kind"].values()) or 1
    print("   volume share  " + "  ".join(f"{k}={rep['volume_by_kind'].get(k,0)/tot:.3f}" for k in KINDS))
    print("   life s   " + "  ".join(f"p{q}={v:.3g}" for q, v in rep["life_s_pct"].items()))
    print("   size     " + "  ".join(f"p{q}={v:.3g}" for q, v in rep["size_pct"].items()))
    print(f"   throughput {rep['processed_per_s']:,.0f} rec/s vs peak {rep['tape_peak_per_s']:,.0f}")
    absd = [abs(x["aggr_delta"]) for x in m]
    rng = [x["range_pts"] for x in m]
    print(f"   {'series':15s} {'mean':>10s} {'CV':>6s} {'r|delta|':>9s} {'r range':>8s}")
    for k in SERIES:
        xs = [x[k] for x in m]
        mean = statistics.mean(xs) if xs else float("nan")
        # balances are signed: correlate their magnitude
        xs_c = [abs(x) for x in xs] if k.endswith("_bal") else xs
        print(f"   {k:15s} {mean:10.3f} {cv(xs_c):6.2f} {corr(xs_c, absd):9.2f} {corr(xs_c, rng):8.2f}")
    # signed balance vs signed delta: does the tail lean the way the tape is pushing?
    sd = [x["aggr_delta"] for x in m]
    print(f"   signed: r(resting_bal, delta)={corr([x['resting_bal'] for x in m], sd):+.2f}   "
          f"r(big_bal, delta)={corr([x['big_bal'] for x in m], sd):+.2f}")
    # ---- standing book: state at window end vs the same and the NEXT window's flow
    ret = [x["ret_pts"] for x in m]
    print(f"   standing book (state at window end; n={len(m)} same-minute, {len(m)-1} next-minute)")
    print(f"   {'series':15s} {'mean':>8s} {'CV':>6s}  {'r delta':>7s} {'r ret':>6s}  | {'r delta+1':>9s} {'r ret+1':>7s}")
    for k in ("st_tail_bal", "st_big_bal", "st_mid_bal"):
        xs = [x[k] for x in m]
        print(f"   {k:15s} {statistics.mean(xs):8.3f} {cv([abs(v) for v in xs]):6.2f}  "
              f"{corr(xs, sd):+7.2f} {corr(xs, ret):+6.2f}  | {corr(xs[:-1], sd[1:]):+9.2f} {corr(xs[:-1], ret[1:]):+7.2f}")
    for k, lab in (("st_tail_bid", "tail bid ctr"), ("st_tail_ask", "tail ask ctr"), ("st_n_tail", "tail orders")):
        xs = [x[k] for x in m]
        print(f"   {lab:15s} {statistics.mean(xs):8.1f} {cv(xs):6.2f}")
    print(f"   baselines: r(delta_t, ret_t)={corr(sd, ret):+.2f}   r(delta_t, ret_t+1)={corr(sd[:-1], ret[1:]):+.2f}   "
          f"r(ret_t, ret_t+1)={corr(ret[:-1], ret[1:]):+.2f}")
    if rep.get("standing_ladder"):
        print(f"   standing tail ladder at the end of the hour (last print {rep['last_px']:.2f}), "
              f"contracts alive >= slow / shown >= big:")
        print(f"   {'price':>10s}  {'tail bid':>8s} {'tail ask':>8s}   {'big bid':>7s} {'big ask':>7s}")
        for r in rep["standing_ladder"]:
            mark = "  <- last" if abs(r["price"] - rep["last_px"]) < 1e-9 else ""
            print(f"   {r['price']:10.2f}  {r['tail_bid']:8d} {r['tail_ask']:8d}   {r['big_bid']:7d} {r['big_ask']:7d}{mark}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-write", action="store_true", help="do not write mm_calib.json")
    ap.add_argument("--window", type=float, default=60.0)
    ap.add_argument("--reuse", action="store_true", help="skip pass 1; cutoffs from mm_calib.json")
    a = ap.parse_args()

    stores = {}
    for sl in SLICES:
        print(f"\n[{sl['role']}] {sl['day']} {sl['t0']}-{sl['t1']} PT")
        st = estimate_then_fetch(sl)
        if st is not None:
            stores[(sl["day"], sl["t0"])] = st
    calib = [sl for sl in SLICES if sl["role"] == "CALIB" and (sl["day"], sl["t0"]) in stores]
    if not calib:
        sys.exit("no calibration slice available")

    # ---- pass 1: raw distributions on the CALIB slice with the current defaults
    sl = calib[0]
    cut = mm_footprint.load_calib() if a.reuse else {}
    if a.reuse and len(cut) == len(mm_footprint.CALIB_KEYS):
        print(f"\n== pass 1 skipped: cutoffs from {mm_footprint.CALIB_PATH.name}: {cut} ==")
    else:
        print(f"\n== pass 1: distributions on {sl['day']} {sl['t0']}-{sl['t1']} (defaults, no json) ==")
        rep0 = run(stores[(sl["day"], sl["t0"])], a.window, quiet=True,
                   trk=FootprintTracker(window_s=a.window, use_calib=False), trace_icebergs=6)
        lp, sp = rep0["life_s_pct"], rep0["size_pct"]
        cut = dict(fast_life_s=float(lp[90]), slow_life_s=float(lp[99]), big_size=int(math.floor(sp[99])) + 1)
        print(f"   derived cutoffs: {cut}   (p90 life, p99 life, floor(p99 size)+1)")
    if not a.no_write and not a.reuse:
        doc = dict(**cut, derived_from=f"{sl['day']} {sl['t0']}-{sl['t1']} PT",
                   rule="fast=p90 life, slow=p99 life, big=floor(p99 size)+1",
                   orders_closed=rep0["orders_closed"], life_s_pct=lp, size_pct=sp,
                   written=datetime.datetime.now().isoformat(timespec="seconds"))
        mm_footprint.CALIB_PATH.write_text(json.dumps(doc, indent=1, default=float))
        print(f"   wrote {mm_footprint.CALIB_PATH.name}")

    # ---- pass 2: every slice with the derived cutoffs
    print("\n== pass 2: all slices with the derived cutoffs ==")
    reports = {}
    for sl in SLICES:
        key = (sl["day"], sl["t0"])
        if key not in stores:
            continue
        label = f"{sl['role']} {sl['day']} {sl['t0']}-{sl['t1']} PT"
        print(f"\n{label}")
        # re-open: a DBNStore iterates once
        rep = run(estimate_then_fetch(sl), a.window, quiet=False,
                  trk=FootprintTracker(window_s=a.window, use_calib=False, **cut))
        reports[label] = rep
    print("\n== summary ==")
    for label, rep in reports.items():
        describe(rep, label)
    out = HERE / "Claude outputs" / "mm_calib-report.json"
    out.parent.mkdir(exist_ok=True)
    slim = {k: {kk: vv for kk, vv in v.items() if kk != "top_levels"} for k, v in reports.items()}
    out.write_text(json.dumps(dict(cutoffs=cut, reports=slim), indent=1, default=float))
    print(f"\nreport json -> {out}")


if __name__ == "__main__":
    main()
