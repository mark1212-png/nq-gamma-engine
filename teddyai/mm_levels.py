r"""
mm_levels.py — do iceberg refills and pulled size line up with the sheet's levels,
and does price react at them? Placebo-controlled, on the cached MBO hours.

Runs on Mark's machine from the cache only (no key needed, nothing fetched). Each
slice is paired with the plan that was IN FORCE at the slice's start — never a plan
built later in the session:

  2026-09-04 06:25-07:25 PT   archive\2026-09-04\rth-trinity.json            (built 06:13 PT)
  2026-09-10 06:25-07:25 PT   archive\2026-09-10\rthprelim-trinity.json      (built 06:00 PT)
  2026-09-04 10:00-11:00 PT   archive\2026-09-04\intraday-0658-trinity.json  (built 06:59 PT)

Levels = the sheet's confluence strikes + the three HVLs + the two rails.

Events
  iceberg cluster   one order refreshed >= MIN_REFILLS times (display-size iceberg);
                    price, side, first/last refill, total refilled contracts
  pulled event      big_pulled contracts at one price inside one second >= MIN_PULLED

Test 1 — location. Share of events within 5 and 10 ticks of a level, against the
share of TRADED VOLUME within the same distance (the placebo: where trading happened,
not where a grid happens to be). p = bootstrap over n draws from the traded-price
distribution, 4000 resamples.

Test 2 — reaction. From the cluster's last refill: signed forward return at 1, 3 and
5 minutes (+ for a bid iceberg, - for an ask iceberg: "the defended side wins"), and
whether the level HELD (price never traded 2+ ticks through it inside 5 min). Placebo:
random times in the same hour, signed by the OPPOSITE of the previous 30 s aggressor
delta — an iceberg bid is, by construction, being hit by sellers, and the ordinary
one-minute reversal after selling must not be credited to the iceberg. Bootstrap 95%
CI on the difference of means. n per hour is tens, so the pooled line is the one to
read, and even that is a first look, not a result.

    python mm_levels.py                      # all three slices, pooled
    python mm_levels.py --min-refills 5      # stricter cluster definition
"""
from __future__ import annotations

import argparse, bisect, collections, json, pathlib, random, statistics, sys

HERE = pathlib.Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE))
from mm_footprint import FootprintTracker, NS, PRICE_SCALE, _get  # noqa: E402
from mm_probe import pt_to_utc, CACHE  # noqa: E402

TICK = 0.25
SLICES = [
    dict(day="2026-09-04", t0="06:25", t1="07:25", plan=r"archive\2026-09-04\rth-trinity.json"),
    dict(day="2026-09-10", t0="06:25", t1="07:25", plan=r"archive\2026-09-10\rthprelim-trinity.json"),
    dict(day="2026-09-04", t0="10:00", t1="11:00", plan=r"archive\2026-09-04\intraday-0658-trinity.json"),
]
HORIZONS_S = (60, 180, 300)


def load_levels(path: pathlib.Path):
    d = json.loads(path.read_text())
    lv = set(float(k) for k in d.get("confluence", {}))
    for p in d.get("panels", {}).values():
        for h in p.get("hvl", []) or []:
            lv.add(float(h))
    for k in ("rail_hi", "rail_lo"):
        if d.get(k) is not None:
            lv.add(float(d[k]))
    return sorted(lv), d


def nearest_dist(levels, px):
    i = bisect.bisect_left(levels, px)
    c = []
    if i < len(levels):
        c.append(abs(levels[i] - px))
    if i > 0:
        c.append(abs(levels[i - 1] - px))
    return min(c) if c else float("inf")


def replay(store):
    """One pass: trades (ts, px, size, side), iceberg clusters, pulled events."""
    trk = FootprintTracker(window_s=60)
    trades = []                                   # (ts, px_pts, size, side)
    ice = {}                                      # order_id -> cluster dict
    pulled = collections.Counter()                # (px_pts, second) -> contracts
    n = 0
    for rec in store:
        n += 1
        if _get(rec, "action") == "T":
            side, size = _get(rec, "side"), int(_get(rec, "size"))
            if side in ("A", "B") and size > 0:
                trades.append((int(_get(rec, "ts_event")), int(_get(rec, "price")) * PRICE_SCALE, size, side))
        for ev in trk.on_record(rec):
            if ev.kind == "iceberg":
                oid = ev.extra["order_id"]
                c = ice.get(oid)
                if c is None:
                    c = ice[oid] = dict(order_id=oid, side=ev.side, px=ev.price * PRICE_SCALE,
                                        first=ev.ts, last=ev.ts, refills=0, qty=0)
                c["last"] = ev.ts
                c["refills"] += 1
                c["qty"] += ev.size
            elif ev.kind == "big" and ev.extra.get("pulled"):
                pulled[(ev.price * PRICE_SCALE, ev.ts // NS)] += ev.size
    trk.flush()
    return n, trades, list(ice.values()), pulled


class Tape:
    """Trade tape with time lookups."""
    def __init__(self, trades):
        self.ts = [t[0] for t in trades]
        self.px = [t[1] for t in trades]
        self.tr = trades

    def price_at(self, ts):
        i = bisect.bisect_right(self.ts, ts) - 1
        return self.px[i] if i >= 0 else None

    def delta_between(self, t0, t1):
        i0, i1 = bisect.bisect_left(self.ts, t0), bisect.bisect_right(self.ts, t1)
        d = 0
        for k in range(i0, i1):
            _, _, sz, sd = self.tr[k]
            d += sz if sd == "B" else -sz
        return d

    def extreme_between(self, t0, t1):
        i0, i1 = bisect.bisect_left(self.ts, t0), bisect.bisect_right(self.ts, t1)
        seg = self.px[i0:i1]
        return (min(seg), max(seg)) if seg else (None, None)


def reaction(tape, ts, level_px, sign, end_ts):
    """Signed forward returns from price at ts, and whether level held for 5 min."""
    p0 = tape.price_at(ts)
    if p0 is None:
        return None
    out = {}
    for h in HORIZONS_S:
        t1 = ts + h * NS
        if t1 > end_ts:
            return None                            # needs the full horizon inside the slice
        p1 = tape.price_at(t1)
        out[f"ret{h}"] = sign * (p1 - p0)
    lo, hi = tape.extreme_between(ts, ts + 300 * NS)
    if lo is None:
        return None
    through = (level_px - lo) if sign > 0 else (hi - level_px)   # how far price went THROUGH the level
    out["held"] = 1 if through < 2 * TICK - 1e-9 else 0
    return out


def boot_ci(a, b, n=4000, rnd=None):
    """95% CI for mean(a) - mean(b), bootstrap."""
    rnd = rnd or random.Random(1)
    if not a or not b:
        return (float("nan"), float("nan"), float("nan"))
    diffs = []
    for _ in range(n):
        sa = [a[rnd.randrange(len(a))] for _ in a]
        sb = [b[rnd.randrange(len(b))] for _ in b]
        diffs.append(statistics.mean(sa) - statistics.mean(sb))
    diffs.sort()
    return (statistics.mean(a) - statistics.mean(b), diffs[int(0.025 * n)], diffs[int(0.975 * n)])


def location_test(events_px, levels, tape, d_ticks, rnd):
    """Share within d of a level vs traded-volume share; bootstrap p."""
    d = d_ticks * TICK + 1e-9
    n = len(events_px)
    if n == 0:
        return None
    hit = sum(1 for px in events_px if nearest_dist(levels, px) <= d) / n
    # placebo distribution: traded prices weighted by size
    px_w = [(t[1], t[2]) for t in tape.tr]
    tot = sum(w for _, w in px_w)
    base = sum(w for px, w in px_w if nearest_dist(levels, px) <= d) / tot
    # bootstrap: draw n traded prices (by volume), count hits, see how often >= observed
    cum, acc = [], 0
    for px, w in px_w:
        acc += w
        cum.append(acc)
    ge = 0
    B = 4000
    for _ in range(B):
        k = 0
        for _ in range(n):
            r = rnd.random() * tot
            i = bisect.bisect_left(cum, r)
            if nearest_dist(levels, px_w[i][0]) <= d:
                k += 1
        if k / n >= hit:
            ge += 1
    return dict(n=n, hit=hit, base=base, p=(ge + 1) / (B + 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-refills", type=int, default=3)
    ap.add_argument("--min-pulled", type=int, default=50)
    ap.add_argument("--placebo-n", type=int, default=400, help="random times per slice")
    a = ap.parse_args()
    import databento as db
    rnd = random.Random(7)

    pooled = collections.defaultdict(list)
    for sl in SLICES:
        t0, t1 = pt_to_utc(sl["day"], sl["t0"]), pt_to_utc(sl["day"], sl["t1"])
        path = CACHE / f"mbo-{sl['day']}-{t0:%H%M}-{t1:%H%M}.dbn.zst"
        plan = HERE / sl["plan"].replace("\\", "/")
        label = f"{sl['day']} {sl['t0']}-{sl['t1']} PT  plan={pathlib.Path(sl['plan']).name}"
        print(f"\n== {label} ==")
        if not path.exists() or not plan.exists():
            print(f"   missing: {'cache' if not path.exists() else 'plan'} — skipped")
            continue
        levels, d = load_levels(plan)
        n, trades, clusters, pulled = replay(db.DBNStore.from_file(path))
        tape = Tape(trades)
        lo, hi = min(tape.px), max(tape.px)
        near = [l for l in levels if lo - 10 <= l <= hi + 10]
        print(f"   {n:,} records, {len(trades):,} prints, traded {lo:.2f}-{hi:.2f}; "
              f"{len(levels)} sheet levels, {len(near)} inside the traded range (+/-10): "
              + ", ".join(f"{l:.0f}" if l == int(l) else f"{l:.1f}" for l in near))
        end_ts = trades[-1][0]
        start_ts = trades[0][0]

        # ---- icebergs
        cl = [c for c in clusters if c["refills"] >= a.min_refills]
        cl.sort(key=lambda c: -c["refills"])
        print(f"   iceberg clusters (>= {a.min_refills} refills): {len(cl)} of {len(clusters)} refreshed orders; "
              f"top: " + "; ".join(f"{c['side']} {c['px']:.2f} x{c['refills']} ({c['qty']} ctr)" for c in cl[:6]))
        for dt in (5, 10):
            r = location_test([c["px"] for c in cl], levels, tape, dt, rnd)
            if r:
                print(f"   location <= {dt:2d} ticks: {r['hit']:.2f} of clusters vs {r['base']:.2f} of traded volume   p={r['p']:.3f}   (n={r['n']})")
                pooled[f"ice_loc{dt}"].append((r["hit"], r["base"], r["n"]))
        ice_re = []
        for c in cl:
            rr = reaction(tape, c["last"], c["px"], +1 if c["side"] == "B" else -1, end_ts)
            if rr:
                ice_re.append(rr)
        # ---- placebo: random times, sign = opposite of previous-30s aggressor delta
        pl_re = []
        for _ in range(a.placebo_n):
            ts = start_ts + int(rnd.random() * (end_ts - start_ts - 320 * NS))
            dlt = tape.delta_between(ts - 30 * NS, ts)
            sign = -1 if dlt > 0 else +1
            p = tape.price_at(ts)
            rr = reaction(tape, ts, p, sign, end_ts)
            if rr:
                pl_re.append(rr)
        if ice_re and pl_re:
            print(f"   reaction (n={len(ice_re)} clusters vs {len(pl_re)} placebo times; + = defended side wins, points)")
            for h in HORIZONS_S:
                k = f"ret{h}"
                m, lo_, hi_ = boot_ci([x[k] for x in ice_re], [x[k] for x in pl_re], rnd=rnd)
                print(f"     {h:3d}s  iceberg {statistics.mean(x[k] for x in ice_re):+6.2f}  placebo {statistics.mean(x[k] for x in pl_re):+6.2f}  "
                      f"diff {m:+6.2f}  [{lo_:+.2f}, {hi_:+.2f}]")
                pooled[k].append(([x[k] for x in ice_re], [x[k] for x in pl_re]))
            m, lo_, hi_ = boot_ci([x["held"] for x in ice_re], [x["held"] for x in pl_re], rnd=rnd)
            print(f"     held 5m  iceberg {statistics.mean(x['held'] for x in ice_re):.2f}  placebo {statistics.mean(x['held'] for x in pl_re):.2f}  "
                  f"diff {m:+.2f}  [{lo_:+.2f}, {hi_:+.2f}]")
            pooled["held"].append(([x["held"] for x in ice_re], [x["held"] for x in pl_re]))

        # ---- pulled size
        ev = [(px, sec, q) for (px, sec), q in pulled.items() if q >= a.min_pulled]
        ev.sort(key=lambda e: -e[2])
        print(f"   pulled events (>= {a.min_pulled} ctr at one price in one second): {len(ev)}; "
              f"top: " + "; ".join(f"{px:.2f} {q} ctr" for px, sec, q in ev[:5]))
        for dt in (5, 10):
            r = location_test([e[0] for e in ev], levels, tape, dt, rnd)
            if r:
                print(f"   location <= {dt:2d} ticks: {r['hit']:.2f} of events vs {r['base']:.2f} of traded volume   p={r['p']:.3f}   (n={r['n']})")
                pooled[f"pull_loc{dt}"].append((r["hit"], r["base"], r["n"]))

    # ---- pooled
    print("\n== pooled over slices ==")
    for k, lab in (("ice_loc5", "icebergs <= 5 ticks"), ("ice_loc10", "icebergs <= 10 ticks"),
                   ("pull_loc5", "pulled <= 5 ticks"), ("pull_loc10", "pulled <= 10 ticks")):
        rows = pooled.get(k)
        if rows:
            n = sum(r[2] for r in rows)
            hit = sum(r[0] * r[2] for r in rows) / n
            base = sum(r[1] * r[2] for r in rows) / n
            print(f"   {lab:22s} {hit:.2f} of events vs {base:.2f} of traded volume  (n={n})")
    for k, lab in (("ret60", "60s"), ("ret180", "180s"), ("ret300", "300s"), ("held", "held 5m")):
        rows = pooled.get(k)
        if rows:
            A = [v for a_, _ in rows for v in a_]
            B = [v for _, b_ in rows for v in b_]
            m, lo_, hi_ = boot_ci(A, B, rnd=rnd)
            print(f"   {lab:8s} iceberg {statistics.mean(A):+6.2f}  placebo {statistics.mean(B):+6.2f}  diff {m:+6.2f}  [{lo_:+.2f}, {hi_:+.2f}]  (n={len(A)} vs {len(B)})")
    print("\nDescriptive on three hours. Not a result until it survives more days and a placebo grid.")


if __name__ == "__main__":
    main()
