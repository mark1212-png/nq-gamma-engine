#!/usr/bin/env python3
"""
backtest.py — does the Teddy model actually predict anything?

Reconstructs the 0DTE gamma map for each past session, then scores it against
what price really did. Three phases, run them in order or use --all:

    python backtest.py --pull  --start 2026-08-03 --end 2026-09-02
    python backtest.py --bars  --start 2026-08-03 --end 2026-09-02
    python backtest.py --score --start 2026-08-03 --end 2026-09-02

Cost: statistics, definition and ohlcv are all L0 schemas, and the Standard
subscription includes 16+ years of L0 history -- so this is covered by the
subscription with no marginal charge. --pull skips any date already cached.

What it measures, and what would count as the model NOT working:

  rails      Did the session CLOSE inside the one-session expected move?
             A +/-1 sigma band should contain roughly 68% of closes. Much
             above and we are overstating vol; much below and understating.
             The whole-range containment is reported too, but WITHOUT a 68%
             benchmark: a running-maximum test on a true 1-sigma band sits
             well under 50%, and quoting 68% against it (as this file did
             until 2026-09-05) made a band ~1.5-2x too wide look validated.

  flip       Is the tape genuinely different either side of the HVL?
             The claim is that below the flip moves amplify and above it they
             damp. If realised range is the same on both sides, the flip is
             decoration.

  nodes      When price reaches a gamma node, does it react?
             Scored as touch -> reversal vs touch -> straight through. A level
             with no better than coin-flip hold rate is not a level.

  confluence Do 3/3 levels hold more often than 2/3 or 1/3?
             This is the sharpest test: if the confluence score carries no
             information, the whole three-book construction is wasted effort.
"""

import os, sys, json, math, argparse, datetime, subprocess, statistics
from math import log, sqrt, exp, pi

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
BARS = os.path.join(DATA, "bars")
DATASET = "GLBX.MDP3"


def sessions(start, end):
    """CME sessions in [start, end] -- weekends AND holidays skipped, so
    dates[i+1] really is the next session and a holiday does not become a
    scored day with an empty chain."""
    import nqvol
    d, out = datetime.date.fromisoformat(start), []
    e = datetime.date.fromisoformat(end)
    while d <= e:
        if nqvol.is_session(d):
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


# ------------------------------------------------------------------- pull ---

def pull(dates):
    os.makedirs(DATA, exist_ok=True)
    todo = [d for d in dates if not os.path.exists(os.path.join(DATA, f"{d}-chain.json"))]
    print(f"  {len(dates)} settle dates, {len(dates)-len(todo)} cached, {len(todo)} to fetch")
    if len(todo) > 30:
        print(f"  a quarter is ~{len(todo)*4} API calls; expect 15-30 min. Safe to Ctrl+C --")
        print("  every finished date is cached, so re-running resumes where it stopped.")
    for i, d in enumerate(todo, 1):
        print(f"  [{i}/{len(todo)}] {d}", flush=True)
        r = subprocess.run([sys.executable, "databento_pull.py", "--fetch", "--date", d,
                            "--window", "final", "--out", "data"],
                           cwd=ROOT, capture_output=True, text=True)
        if r.returncode:
            print("    FAILED:", (r.stderr or r.stdout).strip().splitlines()[-1][:160])


def bars(start, end):
    """1-minute NQU6 bars. ohlcv-1m is L0, so included in the subscription."""
    import databento as db
    os.makedirs(BARS, exist_ok=True)
    path = os.path.join(BARS, f"nq-front-{start}-{end}.json")
    if os.path.exists(path):
        print(f"  cached {path}"); return
    c = db.Historical(os.environ["DATABENTO_API_KEY"])
    end_ts = f"{end}T23:59:00"
    try:                       # never request past the end of the dataset
        avail = str(c.metadata.get_dataset_range(dataset=DATASET).get("end") or "")[:19]
        if avail and avail < end_ts:
            end_ts = avail
            print(f"  clamped end to {end_ts} (dataset ends there)")
    except Exception:
        pass
    # NQ.c.0 = continuous front month. A quarter ago the front contract was
    # NQM6, not NQU6, so a raw symbol would silently return a stale/illiquid
    # series for the early part of the range.
    df = c.timeseries.get_range(dataset=DATASET, schema="ohlcv-1m",
                                symbols=["NQ.c.0"], stype_in="continuous",
                                start=f"{start}T00:00:00", end=end_ts).to_df()
    # VOLUME IS KEPT (2026-09-15). The cache used to drop it, so nothing downstream could
    # ask whether a level broke on volume or on air; every reader keys on ts/o/h/l/c and
    # ignores the extra field, so old files and new ones stay interchangeable.
    rows = [dict(ts=str(t), o=float(r.open), h=float(r.high),
                 l=float(r.low), c=float(r.close), v=int(getattr(r, 'volume', 0) or 0))
            for t, r in df.iterrows()]
    json.dump(rows, open(path, "w"))
    print(f"  {len(rows):,} minute bars -> {path}")


# ------------------------------------------------------------------ score ---
# ONE implementation. Until 2026-09-05 this file carried its own Black-76, its
# own smile (median IV across strikes, flat -- the estimate trinity_db.py had
# already replaced), calendar T and an upward-only flip sweep. It therefore
# validated a band the sheet did not draw and a flip the sheet did not report.
# Now it calls trinity_db.analyse(), which is exactly what the sheet runs.

import trinity_db, nqvol


def zero_dte_map(chain_path, session):
    """The sheet's own numbers for `session`, from the previous settle's chain."""
    try:
        out, books, _ = trinity_db.analyse(chain_path, session, hours=6.5, quiet=True)
    except SystemExit:
        return None
    if out.get("no_0dte"):
        return None
    raw = json.load(open(chain_path, encoding="utf-8"))
    fut = raw["futures_settle"]
    p0 = out["panels"]["0DTE"]
    return dict(F=out["spot"], ref_symbol=out["ref_symbol"], atm=out["iv_atm"], em=out["em"],
                flip=(p0["hvl"][0] if p0["hvl"] else None),
                flip_near=(out["panels"]["NEAR"]["hvl"] or [None])[0],
                conf={float(k): v for k, v in out["confluence"].items()},
                nodes={float(k): v for k, v in p0["gamma"].items() if abs(v) >= 4},
                futures={k: v for k, v in fut.items() if "-" not in k})


def roll_basis(m, open_px):
    """
    The bar file is NQ.c.0, the continuous front month; the map is priced on a
    NAMED contract. Around the quarterly roll the two differ by the calendar
    basis (~290 points) for a few sessions, and every level would then be
    compared against the wrong tape. If the RTH open sits closer to another
    listed future's settle than to the reference, that is the contract the bars
    are on: return the basis to shift every level by.
    """
    best, bdist = m["ref_symbol"], abs(open_px - m["F"])
    for sym, px in m["futures"].items():
        if abs(open_px - px) < bdist and abs(px - m["F"]) > 50:
            best, bdist = sym, abs(open_px - px)
    return (m["futures"][best] - m["F"]) if best != m["ref_symbol"] else 0.0, best


def rth(rows, day):
    from session_bars import rth_bars as select
    return select(rows, day)


def score(dates, start, end):
    barfile = os.path.join(BARS, f"nq-front-{start}-{end}.json")
    if not os.path.exists(barfile):
        sys.exit(f"run --bars first ({barfile} missing)")
    allbars = json.load(open(barfile))

    res, rolled = [], 0
    for i, settle in enumerate(dates[:-1]):
        session = dates[i+1]
        cp = os.path.join(DATA, f"{settle}-chain.json")
        if not os.path.exists(cp): continue
        m = zero_dte_map(cp, session)
        if not m: continue
        bars_ = rth(allbars, session)
        if len(bars_) < 60: continue
        o = bars_[0]["o"]; hi = max(b["h"] for b in bars_); lo = min(b["l"] for b in bars_)
        cl = bars_[-1]["c"]
        rng = hi - lo
        basis, on = roll_basis(m, o)
        if basis:
            rolled += 1
        F = m["F"] + basis                      # every level on the bars' contract
        em = m["em"]
        inside_close = (F - em) <= cl <= (F + em)
        inside_range = (hi <= F + em) and (lo >= F - em)
        flip = None if m["flip"] is None else m["flip"] + basis
        side = "none" if flip is None else ("above" if o > flip else "below")

        # node reactions: first touch, then did it reverse 0.25*EM without
        # first travelling 0.25*EM through?
        tol, need = max(6.0, em * 0.02), em * 0.25
        node_hits = []
        for K0, g in m["nodes"].items():
            K = K0 + basis
            idx = next((j for j, b in enumerate(bars_) if b["l"] - tol <= K <= b["h"] + tol), None)
            if idx is None: continue
            fwd = bars_[idx:]
            up = next((j for j, b in enumerate(fwd) if b["h"] >= K + need), 10**9)
            dn = next((j for j, b in enumerate(fwd) if b["l"] <= K - need), 10**9)
            if up == dn: continue             # neither, or both in one bar: unresolvable
            approached_from_below = (bars_[idx - 1]["c"] < K) if idx else (bars_[0]["o"] < K)
            went_up = up < dn
            reacted = (approached_from_below and not went_up) or (not approached_from_below and went_up)
            node_hits.append(dict(K=K0, g=round(g), reacted=bool(reacted),
                                  sign=("pos" if g > 0 else "neg"),
                                  conf=m["conf"].get(K0, 1)))
        res.append(dict(session=session, F=m["F"], contract=on, basis=basis, open=o, high=hi, low=lo, close=cl,
                        em=round(em, 1), atm=round(m["atm"], 4),
                        flip=None if flip is None else round(flip, 1),
                        range=round(rng, 1), inside_close=inside_close, inside_range=inside_range,
                        flip_side=side, nodes=node_hits))

    if not res:
        sys.exit("no scored sessions -- check that chain files and bars overlap")

    n = len(res)
    se = lambda p, k: (p * (1 - p) / k) ** 0.5 if k else 0.0
    print(f"\n  scored {n} sessions" + (f"   ({rolled} on a rolled front month, levels basis-shifted)" if rolled else "") + "\n")
    ic = sum(r["inside_close"] for r in res); pc = ic / n
    ir = sum(r["inside_range"] for r in res); pr = ir / n
    print(f"  RAILS      close inside +/-1 EM of the settle: {ic}/{n} = {100*pc:.0f}%  +/-{100*se(pc,n):.0f}pp"
          f"   (theory 68% for a 1-sigma one-session band)")
    print(f"             whole RTH range inside the band:     {ir}/{n} = {100*pr:.0f}%  +/-{100*se(pr,n):.0f}pp"
          f"   (NO 68% benchmark here: a running-max test on a true 1-sigma band")
    print(f"              sits well under 50% -- ~37% for a full-horizon path; this window is RTH only)")
    ab = [r["range"] for r in res if r["flip_side"] == "above"]
    be = [r["range"] for r in res if r["flip_side"] == "below"]
    nf = [r["range"] for r in res if r["flip_side"] == "none"]
    def med(x): return statistics.median(x) if x else float("nan")
    print(f"  FLIP       median range, opened ABOVE flip: {med(ab):7.1f} pts  (n={len(ab)})")
    print(f"             median range, opened BELOW flip: {med(be):7.1f} pts  (n={len(be)})")
    print(f"             median range, NO flip in +/-500:  {med(nf):7.1f} pts  (n={len(nf)})   <- used to be filed under BELOW")
    if ab and be:
        print(f"             ratio below/above: {med(be)/med(ab):.2f}x"
              f"   (model says >1: below the flip amplifies)")
    hits = [h for r in res for h in r["nodes"]]
    if hits:
        rr = sum(h["reacted"] for h in hits); p = rr / len(hits)
        print(f"  NODES      touches: {len(hits)}   reacted: {rr} = {100*p:.0f}%  +/-{100*se(p,len(hits)):.0f}pp"
              f"   (benchmark: the measured placebo, not 50%)")
        for sg, lab in (("pos", "positive-gamma nodes (model: hold)"), ("neg", "negative-gamma nodes (model: accelerate)")):
            sub = [h for h in hits if h["sign"] == sg]
            if sub:
                r_ = sum(h["reacted"] for h in sub)
                print(f"             {lab}: {r_}/{len(sub)} = {100*r_/len(sub):.0f}% reversed")
        big = [h for h in hits if abs(h["g"]) >= 15]
        if big:
            rb = sum(h["reacted"] for h in big)
            print(f"             |gamma|>=15 only: {rb}/{len(big)} = {100*rb/len(big):.0f}%")
        print("  CONFLUENCE reaction rate by number of DISJOINT books (0DTE / 1-9 / 10+) the level is material in:")
        for c in (1, 2, 3):
            sub = [h for h in hits if h.get("conf", 1) == c]
            if sub:
                r_ = sum(h["reacted"] for h in sub)
                print(f"             {c}/3 books: {r_:4}/{len(sub):4} = {100*r_/len(sub):5.1f}%")
        print("             if these rates are flat, the three-book construction adds nothing.")
    json.dump(res, open(os.path.join(DATA, "backtest.json"), "w"), indent=1)
    print(f"\n  wrote data/backtest.json ({len(res)} sessions)\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    for f in ("pull", "bars", "score", "all"):
        ap.add_argument(f"--{f}", action="store_true")
    a = ap.parse_args()
    ds = sessions(a.start, a.end)
    if a.pull or a.all:  pull(ds)
    if a.bars or a.all:  bars(a.start, (datetime.date.fromisoformat(a.end)
                                        + datetime.timedelta(days=3)).isoformat())
    if a.score or a.all: score(ds, a.start, (datetime.date.fromisoformat(a.end)
                                             + datetime.timedelta(days=3)).isoformat())


if __name__ == "__main__":
    main()
