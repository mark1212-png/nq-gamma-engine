#!/usr/bin/env python3
"""
backtest_opra.py -- does SPX-complex dealer gamma tell us anything about NQ?

    python backtest_opra.py --bars   --start 2024-09-03 --end 2026-09-03     (NQ.c.0 and ES.c.0 1-minute bars, L0)
    python backtest_opra.py --score  --start 2024-09-03 --end 2026-09-03     (the test; per-day results cached)
    python backtest_opra.py --selftest                                        (synthetic: planted effect must be found, placebo must not)

Build step 5c (2026-09-06). PRE-REGISTERED: the definitions below were fixed
and approved by Mark before any history existed to run them on, and they are
not to be changed after a result is seen. If the result is null, it is a
fourth documented failure beside confluence (p=0.38), nodes (p=0.059) and
the flip's range claim (p=0.46), and the cross row in opra_compare stays out
of the prose. "Never change code to fit one day's outcome" applies to two
years of outcomes just as much.

THE HYPOTHESIS. SPX-complex dollar gamma is ~20x NDX's and ~35-40% of SPX by
weight is the same mega-caps that dominate NDX, so dealer hedging in the SPX
complex plausibly propagates into NDX -- and not meaningfully in reverse.
The signal, if any, is DISAGREEMENT between the complexes.

THE TEST, binary, with placebos, in the style of the node and rail tests:

  event      on settle date D the SPX complex (SPX + SPXW + SPY, dollars
             added; SPX + SPXW when SPY's file is absent, stamped) has net
             dealer gamma within WITHIN_PCT of spot < 0 while NQ's is > 0,
             both at the plan moment (the RTH open of D+1, HOURS before the
             close), both books priced at their own settle/EOD quotes.
  outcome    NQ's RTH on D+1 BREACHES its one-session rail: high above
             S + EM or low below S - EM, with S = the NQ settle and EM =
             S * IV_atm / sqrt(252) from the NQ chain, rail scale 1.0 -- the
             construct whose 73% containment the project measured.
  baseline   the breach rate over all scored days.
  placebo 1  WITHIN the SPX-negative days: event days (NQ positive) against
             the complementary cell (NQ negative). Does NQ's sign add anything
             once SPX is short?
  placebo 2  WITHIN the NQ-positive days: event days (SPX negative) against
             the complementary cell (SPX positive). Does SPX's sign add
             anything once NQ is long?
             (A placebo defined as "SPX sign alone" CONTAINS every event day
             and inherits its lift -- the self-test showed it; the conditional
             form is the same question asked properly. Fixed 2026-09-06 before
             any history existed.)
  mirror     the reverse event (NQ < 0 while SPX complex > 0) scored on ES's
             RTH breach of the SPX complex's own rail (em_pct of its front
             forward, applied to ES's 16:00 ET close). If the mirror works
             as well, dominance is not SPX -> NDX.
  p-value    one-sided permutation test, N_PERM shuffles of the event label
             across scored days, on the difference in breach rate. ALPHA
             0.05, the standard the node test was held to.

  secondary  (reported, not decisive) the same with the NDX complex (QQQ +
             NQ) in place of NQ alone; mean realised range / (2 EM).

Every per-day number is cached in data/opra/backtest-days.json so the
scoring re-runs in seconds; delete a date's entry to recompute it. Chains
are read with trinity_db.load + strike_metrics only -- no flip sweep, which
is what makes a two-year run minutes rather than hours.
"""

import os, sys, json, math, random, argparse, datetime
from pathlib import Path

import nqvol
import trinity_db
import opra_ladder

# ---- PRE-REGISTERED PARAMETERS. Do not change after a result is seen. ----
WITHIN_PCT = 1.0            # the "at spot" window for the sign, % of spot
HOURS = 6.5                 # the plan moment: the RTH open of the next session
RAIL_SCALE = 1.0            # one-session expected move, as the RTH sheet
N_PERM = 10000
ALPHA = 0.05
SEED = 20260906
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OPRA = DATA / "opra"
BARS = DATA / "bars"
CACHE = OPRA / "backtest-days.json"
NQ_MULT, EQ_MULT = 20.0, 100.0


# ------------------------------------------------------------------ bars ---

def pull_bars(symbol, start, end):
    """1-minute continuous-front bars from GLBX, cached. L0, in the plan."""
    import databento as db
    BARS.mkdir(parents=True, exist_ok=True)
    path = BARS / f"{symbol.replace('.', '-')}-{start}-{end}.json"
    if path.exists():
        print(f"  cached {path}")
        return path
    c = db.Historical(os.environ["DATABENTO_API_KEY"])
    end_ts = f"{end}T23:59:00"
    try:
        avail = str(c.metadata.get_dataset_range(dataset="GLBX.MDP3").get("end") or "")[:19]
        if avail and avail < end_ts:
            end_ts = avail
            print(f"  clamped end to {end_ts}")
    except Exception:
        pass
    sz = c.metadata.get_billable_size(dataset="GLBX.MDP3", schema="ohlcv-1m", symbols=[symbol],
                                      stype_in="continuous", start=f"{start}T00:00:00", end=end_ts) / 1e6
    print(f"  {symbol} ohlcv-1m {start} -> {end_ts}: {sz:.1f} MB")
    df = c.timeseries.get_range(dataset="GLBX.MDP3", schema="ohlcv-1m", symbols=[symbol],
                                stype_in="continuous", start=f"{start}T00:00:00", end=end_ts).to_df()
    rows = [dict(ts=str(t), o=float(r.open), h=float(r.high), l=float(r.low), c=float(r.close))
            for t, r in df.iterrows()]
    json.dump(rows, open(path, "w", encoding="utf-8"))
    print(f"  {len(rows):,} bars -> {path}")
    return path


def load_bars(symbol, start, end):
    path = BARS / f"{symbol.replace('.', '-')}-{start}-{end}.json"
    if not path.exists():
        cands = sorted(BARS.glob(f"{symbol.replace('.', '-')}-*.json"))
        if not cands:
            sys.exit(f"  no {symbol} bars under {BARS}; run --bars first")
        path = cands[-1]
    rows = json.load(open(path, encoding="utf-8"))
    out = []
    for r in rows:
        ts = datetime.datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=nqvol.UTC)
        out.append((ts, r["h"], r["l"], r["c"]))
    out.sort()
    return out


def rth(bars, d):
    """(high, low, last close before the cash close) of session d's RTH; None if the tape is missing."""
    o, c = nqvol.rth_open_utc(d), nqvol.close_utc(d)
    sel = [b for b in bars if o <= b[0] < c]
    if len(sel) < 300 * (1 if not nqvol.is_half_day(d) else 0.5):
        return None
    return max(b[1] for b in sel), min(b[2] for b in sel), sel[-1][3]


def settle_close(bars, d):
    """The last 1-minute close at or before the 16:00 ET cash close of session d (ES reference)."""
    c = nqvol.close_utc(d)
    sel = [b for b in bars if c - datetime.timedelta(minutes=30) <= b[0] < c]
    return sel[-1][3] if sel else None


# ------------------------------------------------------------ the books ---

def dollar_within(chain_paths, rd, pct, mult=None):
    """
    Net dealer dollar gamma per 1% within +-pct of spot at the plan moment,
    from trinity_db.load + strike_metrics on the FULL book -- the sheet's own
    arithmetic without the flip sweep. Returns dict(musd, S, F, em, em_pct, atm, expiries, fallback).
    """
    raw, root, path = opra_ladder.merge_chains(chain_paths)
    if root != "NQ" and "grid" not in raw:
        import opra_pull, tempfile
        raw.update(opra_pull.ROOTS[root]["scale"])
        path = Path(tempfile.gettempdir()) / f"opra-bt-{Path(chain_paths[0]).name}"
        json.dump(raw, open(path, "w", encoding="utf-8"))
    m = float(mult if mult is not None else raw.get("multiplier", NQ_MULT))
    _, books, S, ref, F_ref, _ = trinity_db.load(str(path), rd, None, HOURS, None, None)
    g, _ = trinity_db.strike_metrics(books)
    within = sum(v for k, v in g.items() if abs(k - S) / S * 100 <= pct)
    P = trinity_db.panel_cols(books)
    front = P["0DTE"][0] if P["0DTE"] else books[0]
    atm = front["atm"]
    em = nqvol.expected_move(S, atm)
    return dict(musd=round(within * F_ref * m / 1e6, 2), S=S, F=F_ref, em=round(em, 3),
                em_pct=round(em / S * 100, 4), atm=round(atm, 5), expiries=len(books),
                fallback=sum(1 for b in books if not b["smile"].x))


def day_record(d, rd):
    """Everything the test needs for settle date d, plan for rd. None if the SPX complex or NQ is missing."""
    f = lambda r: OPRA / f"{d}-{r}-chain.json"
    nqp = DATA / f"{d}-chain.json"
    if not (f("SPX").exists() and f("SPXW").exists()) or not nqp.exists() or nqp.stat().st_size < 500_000:
        return None
    spx = dollar_within([str(f("SPX")), str(f("SPXW"))], rd, WITHIN_PCT)
    spy = dollar_within([str(f("SPY"))], rd, WITHIN_PCT) if f("SPY").exists() else None
    qqq = dollar_within([str(f("QQQ"))], rd, WITHIN_PCT) if f("QQQ").exists() else None
    nq = dollar_within([str(nqp)], rd, WITHIN_PCT, mult=NQ_MULT)
    complex_musd = spx["musd"] + (spy["musd"] if spy else 0.0)
    return dict(date=d, report_date=rd,
                spx_complex_musd=round(complex_musd, 2), spx_complex_has_spy=spy is not None,
                spx_musd=spx["musd"], spy_musd=spy["musd"] if spy else None, qqq_musd=qqq["musd"] if qqq else None,
                nq_musd=nq["musd"], ndx_complex_musd=round(nq["musd"] + (qqq["musd"] if qqq else 0.0), 2),
                nq_S=nq["S"], nq_em=nq["em"], nq_atm=nq["atm"], nq_em_pct=nq["em_pct"],
                spx_em_pct=spx["em_pct"], spx_F=spx["F"],
                fallback_books=spx["fallback"] + nq["fallback"] + (spy["fallback"] if spy else 0))


# ------------------------------------------------------------- scoring ---

def sessions(start, end):
    d, out = datetime.date.fromisoformat(start), []
    e = datetime.date.fromisoformat(end)
    while d <= e:
        if nqvol.is_session(d):
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def perm_p(hits, flags, n_perm, seed, within=None):
    """
    One-sided permutation p: P(breach rate in a random subset of the same
    size, drawn from `within` (default: all days), >= the observed rate on
    the flagged days). Returns (observed rate, p).
    """
    pool = [i for i in range(len(hits)) if (within is None or within[i])]
    idx = [i for i in pool if flags[i]]
    if not idx or len(idx) == len(pool):
        return None, None
    obs = sum(hits[i] for i in idx) / len(idx)
    rng = random.Random(seed)
    n, ge = len(idx), 0
    for _ in range(n_perm):
        pick = rng.sample(pool, n)
        if sum(hits[i] for i in pick) / n >= obs - 1e-12:
            ge += 1
    return obs, (ge + 1) / (n_perm + 1)


def score(days, nq_bars, es_bars, n_perm=N_PERM, seed=SEED, quiet=False):
    """
    days: list of day_record dicts. Returns the result dict; prints unless quiet.
    Outcomes are computed here so the cache holds only the book numbers.
    """
    rows = []
    for r in days:
        rd = datetime.date.fromisoformat(r["report_date"])
        nqr = rth(nq_bars, rd)
        if nqr is None:
            continue
        hi, lo, _ = nqr
        S, em = r["nq_S"], r["nq_em"] * RAIL_SCALE
        breach = hi > S + em or lo < S - em
        row = dict(r, nq_hi=hi, nq_lo=lo, nq_breach=breach, nq_range_over_2em=round((hi - lo) / (2 * em), 3))
        esr = rth(es_bars, rd) if es_bars else None
        es_ref = settle_close(es_bars, datetime.date.fromisoformat(r["date"])) if es_bars else None
        if esr and es_ref:
            ehi, elo, _ = esr
            band = es_ref * r["spx_em_pct"] / 100 * RAIL_SCALE
            row["es_breach"] = ehi > es_ref + band or elo < es_ref - band
        rows.append(row)
    if not rows:
        return dict(n=0, verdict="NOT SCORED: no usable sessions"), []
    hits = [1 if r["nq_breach"] else 0 for r in rows]
    spx_neg = [r["spx_complex_musd"] < 0 for r in rows]
    nq_pos = [r["nq_musd"] > 0 for r in rows]
    event = [a and b for a, b in zip(spx_neg, nq_pos)]
    ndx_pos = [r["ndx_complex_musd"] > 0 for r in rows]
    event_ndx = [a and b for a, b in zip(spx_neg, ndx_pos)]
    base = sum(hits) / len(hits)
    res = dict(n=len(rows), baseline_breach=round(base, 4), n_event=sum(event),
               n_spx_neg=sum(spx_neg), n_nq_pos=sum(nq_pos), parameters=dict(
                   within_pct=WITHIN_PCT, hours=HOURS, rail_scale=RAIL_SCALE, n_perm=n_perm, alpha=ALPHA))
    def cell(flags, within=None):
        pool = [i for i in range(len(hits)) if (within is None or within[i])]
        return sum(hits[i] for i in pool) / len(pool) if pool else None
    for name, flags, within, sd in (("event", event, None, 1),
                                    ("placebo_within_spx_neg", event, spx_neg, 2),
                                    ("placebo_within_nq_pos", event, nq_pos, 3),
                                    ("secondary_event_ndx_complex", event_ndx, None, 4)):
        obs, p = perm_p(hits, flags, n_perm, seed + sd, within)
        ref = cell(flags=None, within=within) if within is not None else base
        res[name] = dict(n=sum(f and (within is None or w) for f, w in zip(flags, within or [True] * len(flags))),
                         n_pool=sum(within) if within is not None else len(hits),
                         breach_rate=round(obs, 4) if obs is not None else None,
                         reference_rate=round(ref, 4) if ref is not None else None,
                         lift=round(obs - ref, 4) if obs is not None and ref is not None else None,
                         p=round(p, 4) if p is not None else None)
    # mirror: NQ short / SPX long, scored on ES against the SPX complex's rail
    es_rows = [r for r in rows if "es_breach" in r]
    if es_rows:
        ehits = [1 if r["es_breach"] else 0 for r in es_rows]
        mirror = [r["nq_musd"] < 0 and r["spx_complex_musd"] > 0 for r in es_rows]
        obs, p = perm_p(ehits, mirror, n_perm, seed + 5)
        res["mirror"] = dict(n=sum(mirror), n_scored=len(es_rows), baseline_es_breach=round(sum(ehits) / len(ehits), 4),
                             breach_rate=round(obs, 4) if obs is not None else None,
                             lift=round(obs - sum(ehits) / len(ehits), 4) if obs is not None else None,
                             p=round(p, 4) if p is not None else None)
    ev_ranges = [r["nq_range_over_2em"] for r, f in zip(rows, event) if f]
    res["secondary_range_over_2em"] = dict(event_mean=round(sum(ev_ranges) / len(ev_ranges), 3) if ev_ranges else None,
                                           all_mean=round(sum(r["nq_range_over_2em"] for r in rows) / len(rows), 3))
    ok = lambda k: res[k]["p"] is not None and res[k]["p"] < ALPHA
    res["verdict"] = ("SURVIVES: the event beats baseline AND beats both complementary cells at p < 0.05"
                      if ok("event") and ok("placebo_within_spx_neg") and ok("placebo_within_nq_pos")
                      else "DOES NOT SURVIVE: do not wire the dominance claim into the prose")
    if not quiet:
        _print(res)
    return res, rows


def _print(res):
    print(f"\n  dominance test: {res['n']} scored sessions   baseline NQ rail breach {res['baseline_breach'] * 100:.1f}%")
    print(f"  parameters (pre-registered): within {WITHIN_PCT}% of spot, plan at {HOURS}h, rail x{RAIL_SCALE}, "
          f"{res['parameters']['n_perm']:,} permutations, alpha {ALPHA}\n")
    print(f"  {'condition':34} {'n':>5} {'of':>5} {'breach':>8} {'ref':>8} {'lift':>8} {'p':>8}")
    for name in ("event", "placebo_within_spx_neg", "placebo_within_nq_pos", "secondary_event_ndx_complex"):
        r = res[name]
        nan = float("nan")
        print(f"  {name:34} {r['n']:5d} {r['n_pool']:5d} {(r['breach_rate'] * 100 if r['breach_rate'] is not None else nan):7.1f}% "
              f"{(r['reference_rate'] * 100 if r['reference_rate'] is not None else nan):7.1f}% "
              f"{(r['lift'] * 100 if r['lift'] is not None else nan):+7.1f}% {(r['p'] if r['p'] is not None else nan):8.4f}")
    print("  (placebo rows: the event against the complementary cell INSIDE the one-sided set; 'ref' is that set's rate)")
    if "mirror" in res:
        m = res["mirror"]
        print(f"  {'mirror (NQ short / SPX long -> ES)':34} {m['n']:5d} {(m['breach_rate'] * 100 if m['breach_rate'] is not None else float('nan')):7.1f}% "
              f"{(m['lift'] * 100 if m['lift'] is not None else float('nan')):+7.1f}% {(m['p'] if m['p'] is not None else float('nan')):8.4f}"
              f"   (ES baseline {m['baseline_es_breach'] * 100:.1f}% over {m['n_scored']} days)")
    s = res["secondary_range_over_2em"]
    print(f"\n  secondary: NQ range / (2 EM) on event days {s['event_mean']} vs all days {s['all_mean']}")
    print(f"\n  VERDICT: {res['verdict']}\n")


# ------------------------------------------------------------- selftest ---

def selftest():
    """Synthetic days with a planted effect: event days breach 55% of the time,
    others 27%; the placebo flags are independent of the outcome. The test
    must find the event and not the placebo. Then the same with NO effect:
    the event must not be found (a 5% false-positive rate is the alpha)."""
    rng = random.Random(3)

    def make(effect, n=500):
        rows = []
        for i in range(n):
            spx = rng.uniform(-1, 1) * 5000
            nq = rng.uniform(-1, 1) * 500
            ev = spx < 0 and nq > 0
            p = 0.55 if (ev and effect) else 0.27
            breach = rng.random() < p
            S, em = 20000.0, 200.0
            hi = S + (em * 1.2 if breach and rng.random() < 0.5 else em * 0.6)
            lo = S - (em * 1.2 if breach and hi < S + em else em * 0.6)
            rows.append(dict(date=f"d{i}", report_date=f"d{i}", spx_complex_musd=spx, nq_musd=nq,
                             ndx_complex_musd=nq, nq_S=S, nq_em=em, spx_em_pct=1.0, nq_hi=hi, nq_lo=lo))
        return rows

    def run(rows):
        hits = [1 if (r["nq_hi"] > r["nq_S"] + r["nq_em"] or r["nq_lo"] < r["nq_S"] - r["nq_em"]) else 0 for r in rows]
        spx_neg = [r["spx_complex_musd"] < 0 for r in rows]
        nq_pos = [r["nq_musd"] > 0 for r in rows]
        event = [a and b for a, b in zip(spx_neg, nq_pos)]
        return (perm_p(hits, event, 2000, 1)[1], perm_p(hits, event, 2000, 2, spx_neg)[1],
                perm_p(hits, event, 2000, 3, nq_pos)[1])

    p_ev, p_s, p_n = run(make(True))
    p_ev0, p_s0, p_n0 = run(make(False))
    errs = []
    if p_ev is None or p_ev >= 0.01 or p_s >= 0.01 or p_n >= 0.01:
        errs.append(f"planted effect not found: event p={p_ev}, within spx_neg p={p_s}, within nq_pos p={p_n}")
    if p_ev0 is None or p_ev0 < 0.02:
        errs.append(f"no-effect run found an effect: p={p_ev0}")
    print(f"  planted effect: event p={p_ev:.4f}  within spx_neg p={p_s:.4f}  within nq_pos p={p_n:.4f}   "
          f"no-effect run: event p={p_ev0:.4f}, within spx_neg p={p_s0:.4f}, within nq_pos p={p_n0:.4f}")
    print("  BACKTEST SELFTEST", "PASS" if not errs else "FAIL")
    for e in errs:
        print("    -", e)
    return not errs


# ---------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--bars", action="store_true", help="pull NQ.c.0 and ES.c.0 1-minute bars for the window")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--recompute", action="store_true", help="ignore the per-day cache")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    if not (a.start and a.end):
        ap.error("--start and --end are required")
    end_plus = nqvol.next_session(datetime.date.fromisoformat(a.end)).isoformat()
    if a.bars:
        pull_bars("NQ.c.0", a.start, end_plus)
        pull_bars("ES.c.0", a.start, end_plus)
        return
    if not a.score:
        ap.error("pass --bars, --score or --selftest")
    cache = json.load(open(CACHE, encoding="utf-8")) if CACHE.exists() and not a.recompute else {}
    dates = sessions(a.start, a.end)
    days, missing = [], 0
    for i, d in enumerate(dates, 1):
        if d in cache:
            days.append(cache[d]); continue
        rd = nqvol.next_session(datetime.date.fromisoformat(d)).isoformat()
        try:
            rec = day_record(d, rd)
        except SystemExit as ex:
            print(f"  {d}: {str(ex).strip()[:100]}"); rec = None
        if rec is None:
            missing += 1; continue
        cache[d] = rec
        days.append(rec)
        if i % 20 == 0:
            OPRA.mkdir(parents=True, exist_ok=True)
            json.dump(cache, open(CACHE, "w", encoding="utf-8"), indent=1)
            print(f"  {i}/{len(dates)} dates, {len(days)} scored, {missing} missing", flush=True)
    OPRA.mkdir(parents=True, exist_ok=True)
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), indent=1)
    print(f"  {len(days)} days with books, {missing} without (no SPX/SPXW/NQ chain)")
    nq_bars = load_bars("NQ.c.0", a.start, end_plus)
    try:
        es_bars = load_bars("ES.c.0", a.start, end_plus)
    except SystemExit:
        es_bars = None
        print("  no ES bars: the mirror is skipped")
    res, rows = score(days, nq_bars, es_bars)
    outp = Path(a.out) if a.out else OPRA / f"backtest-{a.start}-{a.end}.json"
    json.dump(dict(result=res, days=rows), open(outp, "w", encoding="utf-8"), indent=1)
    print(f"  wrote {outp}")


if __name__ == "__main__":
    main()
