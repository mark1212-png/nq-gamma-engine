#!/usr/bin/env python3
"""
trinity_db.py — Teddy Analysis from a Databento chain, not from QuikStrike PDFs.

    python trinity_db.py --chain data/2026-09-01-chain.json --report-date 2026-09-03

Writes trinity.json in the same shape trinity.py did, so build.py renders the
report unchanged, plus chain_live.json for the command center's live re-price.

The upgrade over the PDF path: gamma is computed from the REAL per-strike
implied vol, solved from settlement prices, instead of QuikStrike's integer-
rounded Gamma (1 Pct) column.

2026-09-05 (audit). Four things changed in what this file MEANS:

  * TIME IS BUSINESS TIME. All T come from nqvol.years_between -- sessions,
    variance-weighted, weekends and holidays zero. Chains solved on the old
    calendar convention are re-based on load from the settlement prices they
    already hold (nqvol.load_chain), so nothing needs to be re-pulled. The
    expected move S*iv/sqrt(252) is now consistent with the vol it uses.
  * EXPIRIES COME FROM THE DEFINITIONS. The chain carries each contract's real
    expiration instant -- 20:00 UTC summer, 21:00 winter, 13:30/14:30 for the
    AM-settled quarterlies -- so nothing here hardcodes 20:00 any more.
  * FOREIGN-UNDERLYING BOOKS ARE BASIS-SHIFTED ONTO THE REFERENCE AXIS. An
    option on NQZ6 struck at 29,500 hedges in NQZ6 futures; with NQZ6 294
    points over NQU6 that flow reaches the NQU6 tape at ~29,206, not 29,500.
    The ladder used to plot it at 29,500 (82% of that node was one QNEU6 line).
    Each book's strikes now land at K - (F_book - F_ref), bucketed to the
    25-point grid. The aggregate flip was already basis-safe and is unchanged.
  * CONFLUENCE IS SCORED ON PARTITIONED BOOKS. The displayed columns are still
    0DTE / NEAR (<=9) / FULL, nested as before so every flip keeps its meaning.
    The +-score, though, now counts 0DTE / 1-9 DTE / 10+ DTE -- three books
    that share no contract -- because a strike that is big in the front book
    was mechanically big in both supersets, and the score could never be
    evidence of agreement. (The backtest's null result on confluence was
    structural, not bad luck.)
"""

import json, math, argparse, datetime
from pathlib import Path
from math import log, sqrt

import nqvol
from nqvol import greeks, Smile, UTC

GRID = 25.0                       # NQ strike grid the shifted mass is bucketed to
MONEYNESS_CAP = 0.25              # |log(K/F)| beyond this a strike carries no hedge flow today
HVL_SWEEP = (-500, 500, 1.0)      # points either side of the reference the flip is searched over
# 2026-09-06: the three above are NQ-scaled (25 points is 0.09% of NQ and 0.4%
# of SPX; +-500 is 1.7% of NQ and would sweep SPY below zero). An equity chain
# from opra_pull.py carries `grid`, `moneyness_cap` and `hvl_sweep` of its own;
# a chain without them -- every NQ chain -- gets exactly these constants, so
# the NQ path is unchanged by construction (proved by check_identical.py).
HVL_REFINE_EVERY = 5              # 2026-09-12: the flip sweep reprices every 5th point and refines only
                                  # where the sign changes; 1 restores the full 1,001-point sweep exactly


def load(path, report_date, spot_override=None, hours_front=6.5, ref_symbol=None, asof=None):
    """
    Returns (raw, books, S, ref_dt, F_ref, ref_symbol).

    `hours_front` is the plan's as-of offset: the plan describes the moment
    `hours_front` before the report session's close (16:00 ET -- 20:00 UTC in
    summer, 21:00 in winter, 18:00 on a half-day). 6.5 is the RTH open. That
    is the ONE convention every job in autopull respects, so it is kept; what
    changed is that the close is read from the calendar instead of hardcoded.
    """
    raw = nqvol.load_chain(path)
    fut, chain = raw["futures_settle"], raw["chain"]

    # AN OVERRIDE THE CHAIN CANNOT PRICE IS FATAL, NOT A FALLBACK (2026-09-16).
    # F_ref was `fut.get(ref_symbol) or books[0]["F"]`, which on a typo or a chain
    # from before the contract was listed took the FRONT book's forward and still
    # stamped the requested symbol on out["ref_symbol"] and chain_live -- a file
    # whose levels are on one axis and whose label names another, with nothing to
    # say so. That is the exact failure this whole roll week has been about, and
    # the silent fallback is what would let it back in. Checked here, before any
    # work, because it is an argument error and not a property of the book.
    if ref_symbol and ref_symbol not in fut:
        raise SystemExit(f"--ref {ref_symbol}: this chain has no settle for it (it settles "
                         + ", ".join(sorted(k for k in fut if "-" not in k))
                         + ") -- the levels would be priced on the front book's forward "
                           "and labelled with a contract they are not on")
    grid = float(raw.get("grid", GRID))
    cap = float(raw.get("moneyness_cap", MONEYNESS_CAP))
    rd = datetime.date.fromisoformat(report_date)
    if asof is not None:
        ref = asof                                      # an explicit moment (the holiday job)
    elif nqvol.is_globex_holiday(rd):
        # a Globex-only holiday: the plan describes the session from its
        # reopen (Sunday 18:00 ET for a Monday holiday) to the 12:00 CT halt
        ref = nqvol.open_utc(rd)
        print(f"  {rd} is a Globex-only holiday: plan runs from the {ref:%a %H:%M}Z reopen "
              f"to the {nqvol.close_utc(rd):%H:%M}Z halt; no settlement, no expiry")
    else:
        if not nqvol.is_session(rd):
            print(f"  !! {rd} is not a CME session (weekend/full closure) -- the plan describes a day "
                  f"with no settlement and no expiry. Check the report date.")
        ref = nqvol.close_utc(rd) - datetime.timedelta(hours=hours_front)

    books = []
    excluded_risk = []
    for key, strikes in chain.items():
        day, root = key.split("|")
        exp_d = datetime.date.fromisoformat(day)
        texp = nqvol.book_expiry(strikes)
        if texp is None or texp <= ref:
            continue                                   # expired before the plan's "now"
        unds = [r[c]["underlying"] for r in strikes.values() for c in r if r[c].get("underlying")]
        und = max(set(unds), key=unds.count) if unds else None
        F = fut.get(und)
        if not F:
            continue
        T = nqvol.years_between(ref, texp)
        pts, ivs = [], []
        for ks, rec in strikes.items():
            for cp in ("C", "P"):
                iv = rec.get(cp, {}).get("iv")
                if iv and 0.03 < iv < 3.0:
                    pts.append((log(float(ks) / F), iv)); ivs.append(iv)
        forward_bad = raw.get("forwards", {}).get(und, {}).get("unreliable", False)
        if not ivs or forward_bad:
            excluded_risk.append(dict(book=key, reason="unreliable forward" if forward_bad else "no usable IV",
                                      oi=sum(c.get("oi", 0) or 0 for r in strikes.values() for c in r.values())))
            continue
        med = sorted(ivs)[len(ivs) // 2]
        smile = Smile(pts, med)
        books.append(dict(root=root, day=day, exp=exp_d, expiry=texp, F=F, T=T, und=und,
                          dte=nqvol.sessions_between(rd, exp_d) if exp_d > rd else 0,
                          smile=smile, atm=smile.atm, atm_median=med, strikes=strikes,
                          grid=grid, cap=cap))
    raw["excluded_risk_books"] = excluded_risk
    books.sort(key=lambda b: (b["expiry"], b["root"]))
    if not books:
        raise SystemExit("no live expiries in this chain for that report date")

    # The reference future is the one the FRONT book hedges into -- that is the
    # contract whose tape the intraday plan is read against. Overridable for
    # the days around the quarterly roll when the tape has moved on.
    ref_symbol = ref_symbol or books[0]["und"]
    F_ref = fut[ref_symbol] if ref_symbol in fut else books[0]["F"]
    for b in books:
        b["basis"] = round(b["F"] - F_ref, 2)
    S = spot_override or F_ref
    return raw, books, S, ref, F_ref, ref_symbol


def panel_cols(books):
    z = [b for b in books if b["dte"] == 0]
    n = [b for b in books if b["dte"] <= 9]
    return {"0DTE": z, "NEAR": n, "FULL": books}


def confluence_cols(books):
    """Three books that share no contract. Only the +-score reads these."""
    return {"0DTE": [b for b in books if b["dte"] == 0],
            "1-9": [b for b in books if 1 <= b["dte"] <= 9],
            "10+": [b for b in books if b["dte"] >= 10]}


def axis_strike(K, basis, grid=GRID):
    """Where a strike on another underlying lands on the reference axis."""
    if not basis:
        return K
    return round((K - basis) / grid) * grid


def net_position(rec, dealer_long_calls=True):
    """
    Signed DEALER contracts at one strike record. Positive = dealers long.

    `rec["pos"]["dealer"]` when dealer_flow.apply() has stamped a flow-measured
    position on the loaded book (in memory only -- chain files on disk never
    carry it, so every loader that walks a record's keys is untouched); else
    the METHOD.md section 9 convention, dealers long calls / short puts, which
    is +(C - P) and has been the number on every sheet.

    `dealer_long_calls=False` is THE OPPOSITE BOOK: -(C - P), and a measured
    position negated too. Nothing reads the book that way except the
    sensitivity block in engine.py, which exists because section 9 says every
    sign on the sheet inverts if the convention is wrong and, until 2026-09-12,
    nothing measured what that would change.
    """
    p = rec.get("pos")
    if isinstance(p, dict) and p.get("dealer") is not None:
        d = float(p["dealer"])
        return d if dealer_long_calls else -d
    n = rec.get("C", {}).get("oi", 0) - rec.get("P", {}).get("oi", 0)
    return n if dealer_long_calls else -n


def strike_metrics(books, S_shift=0.0, on_axis=True, dealer_long_calls=True):
    """
    net gamma (contracts of delta per 1% move) and vanna (per 1 vol pt), keyed
    by strike ON THE REFERENCE AXIS (basis-shifted) when on_axis, else raw.
    Vol is re-read from the smile at the shifted log-moneyness (sticky
    moneyness), which is what hvl() has always done.

    The signed position per strike comes from net_position(): the convention
    unless a flow-measured position was stamped on the record, and the opposite
    book when dealer_long_calls is False. With the default and no stamped
    positions this is the function it was before 2026-09-12, number for number.
    """
    g, v = {}, {}
    for b in books:
        F = b["F"] + S_shift
        basis = b["basis"] if on_axis else 0.0
        cap, grid = b.get("cap", MONEYNESS_CAP), b.get("grid", GRID)
        for ks, rec in b["strikes"].items():
            K = float(ks)
            n = net_position(rec, dealer_long_calls)
            if not n:
                continue
            m = log(K / F)
            if abs(m) > cap:
                continue
            gg, va = greeks(F, K, b["smile"](m), b["T"])
            k = axis_strike(K, basis, grid)
            g[k] = g.get(k, 0.0) + n * gg * 0.01 * F
            v[k] = v.get(k, 0.0) + n * va * 0.01
    return g, v


def strike_charm(books, S_shift=0.0, on_axis=True, dealer_long_calls=True):
    """
    net charm (contracts of delta per SESSION of time passing), keyed exactly
    like strike_metrics: on the reference axis when on_axis, else raw.

    A sibling of strike_metrics rather than a third value returned from it: a
    dozen callers unpack that pair and tests/test_baseline_regression.py
    freezes its output, so strike_metrics is untouched. This loop repeats its
    moneyness cap, sticky-moneyness vol and basis shift so the two agree strike
    for strike. Per session, not per year, because business time is the
    project's clock (see nqvol) and one session is what the sheet is for.
    Added 2026-09-12 with nodescore's charm component.
    """
    c = {}
    for b in books:
        F = b["F"] + S_shift
        basis = b["basis"] if on_axis else 0.0
        cap, grid = b.get("cap", MONEYNESS_CAP), b.get("grid", GRID)
        for ks, rec in b["strikes"].items():
            K = float(ks)
            n = net_position(rec, dealer_long_calls)
            if not n:
                continue
            m = log(K / F)
            if abs(m) > cap:
                continue
            ch = nqvol.charm(F, K, b["smile"](m), b["T"])
            k = axis_strike(K, basis, grid)
            c[k] = c.get(k, 0.0) + n * ch / nqvol.SESSIONS_PER_YEAR
    return c


def _sweep_points(lo, hi, step):
    """The sweep grid EXACTLY as hvl() has always walked it -- accumulated, not
    lo + i*step -- so the refined search lands on bit-identical abscissae and
    the interpolated crossing is the same float the full sweep produced."""
    xs, x = [], float(lo)
    while x <= hi:
        xs.append(x)
        x += step
    return xs


def hvl_profile(books, F_ref, lo=-500, hi=500, step=1.0, every=HVL_REFINE_EVERY,
                dealer_long_calls=True):
    """
    (crossings, profile): the zero crossings hvl() returns, and the aggregate
    gamma curve G(F) they were found on.

    2026-09-12. The sweep used to reprice the whole book at every one of the
    1,001 points and keep only the sign changes -- 95% of engine.run's wall
    time (17.5 of 18.4 profiled seconds, 3.7 million greeks() calls) for three
    scalars. Now it reprices every `every`-th point, refines the fine grid ONLY
    inside the intervals whose ends differ in sign, and interpolates with the
    same formula on the same abscissae, so each crossing is the float the full
    sweep produced. Checked old-against-new on every cached chain and panel
    before it shipped (HANDOFF 2026-09-12). `every=1` is the old sweep exactly.

    What the full sweep threw away is kept. `profile` is G(F) on the coarse
    grid, in the engine's units (contracts of delta per 1% move, summed over
    the panel) against F = F_ref + x. Its slope at spot is the SIZE of the
    hedging response, not just its sign -- which the flip's location alone
    cannot say, and which regime.gamma_regime reduces to a band.

    The one thing the refinement cannot see is two crossings inside one coarse
    interval (`every` fine steps: 5 points on NQ). G(F) is a sum of smooth
    kernels hundreds of points wide, so that needs a local extremum within a
    fifth of a strike of zero; it was not observed on any cached chain.
    """
    xs = _sweep_points(lo, hi, step)
    n = len(xs)
    every = max(1, int(every))
    prof = {"x": [], "g": [], "step": step * every, "F_ref": F_ref, "evaluations": 0,
            "units": "contracts of delta per 1% move, summed over the panel, at F = F_ref + x"}
    if n == 0:
        return [], prof
    vals = [None] * n

    def g(i):
        if vals[i] is None:
            gm, _ = strike_metrics(books, S_shift=xs[i], on_axis=False,
                                   dealer_long_calls=dealer_long_calls)
            vals[i] = sum(gm.values())
        return vals[i]

    def crosses(ya, yb):
        return (ya < 0 <= yb) or (ya >= 0 > yb)      # hvl()'s predicate, unchanged

    coarse = list(range(0, n, every))
    if coarse[-1] != n - 1:
        coarse.append(n - 1)
    for i in coarse:
        g(i)
    for a, b in zip(coarse, coarse[1:]):
        if crosses(vals[a], vals[b]):
            for i in range(a + 1, b):
                g(i)

    out = []
    done = [i for i in range(n) if vals[i] is not None]
    for i, j in zip(done, done[1:]):
        if j != i + 1:
            continue                    # an unrefined interval: same sign at both ends
        ya, yb = vals[i], vals[j]
        if crosses(ya, yb):
            out.append(round(F_ref + xs[i] + (0 - ya) * (xs[j] - xs[i]) / (yb - ya), 1))
    prof["x"] = [xs[i] for i in coarse]
    prof["g"] = [round(vals[i], 4) for i in coarse]
    prof["evaluations"] = len(done)
    return out, prof


def hvl(books, F_ref, lo=-500, hi=500, step=1.0, dealer_long_calls=True):
    """
    Absolute futures price (reference contract) where aggregate dealer gamma
    crosses zero. Each expiry is shifted by the same delta from its own
    underlying, which keeps the basis intact; the crossing is a property of the
    open-interest distribution and does not move because price moved.

    Since 2026-09-12 a thin wrapper over hvl_profile(); same crossings.
    """
    return hvl_profile(books, F_ref, lo, hi, step, dealer_long_calls=dealer_long_calls)[0]


def profile_read(prof, x):
    """
    G and dG/dF at F = F_ref + x, read off the coarse curve: G by linear
    interpolation, the slope as the secant of the bracketing coarse points.
    None outside the swept range. The slope is in contracts of delta per 1%
    move PER POINT of the reference -- positive means the aggregate book gets
    longer gamma as price rises here.
    """
    xs, gs = prof.get("x") or [], prof.get("g") or []
    if len(xs) < 2 or x < xs[0] or x > xs[-1]:
        return {"x": round(x, 2), "g": None, "slope": None}
    import bisect
    j = min(max(bisect.bisect_right(xs, x), 1), len(xs) - 1)
    i = j - 1
    dx = xs[j] - xs[i]
    w = (x - xs[i]) / dx if dx else 0.0
    gv = gs[i] + w * (gs[j] - gs[i])
    slope = (gs[j] - gs[i]) / dx if dx else None
    return {"x": round(x, 2), "g": round(gv, 3),
            "slope": (round(slope, 5) if slope is not None else None),
            "slope_units": "contracts of delta per 1% move, per point of the reference"}


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(dict(spot=None, hours=6.5, ref=None, asof=None, rail_scale=1.0, rail_label="1D",
                                  last=None, last_at="", oi_vintage="final", out="trinity.json", quiet=False))
        self.__dict__.update(kw)


def analyse(chain, report_date, **kw):
    """
    The whole computation as a function: (out, books, live) for a chain file and
    a report date, with the same keyword options as the command line. This is
    what backtest.py calls, so validation and production run ONE implementation
    -- the old backtest carried its own copy (median IV, flat vol, calendar T)
    and validated a band the sheet did not draw.
    """
    a = _Args(chain=chain, report_date=report_date, **kw)
    return _analyse(a)


def _say(a, msg):
    if not getattr(a, "quiet", False):
        print(msg)


def _analyse(a):
    asof_in = (datetime.datetime.fromisoformat(a.asof.replace("Z", "+00:00")) if getattr(a, "asof", None) else None)
    if asof_in is not None and asof_in.tzinfo is None:
        asof_in = asof_in.replace(tzinfo=UTC)
    raw, books, S, asof, F_ref, ref_symbol = load(a.chain, a.report_date, a.spot, a.hours, a.ref, asof_in)
    P = panel_cols(books)
    front = P["0DTE"][0] if P["0DTE"] else books[0]
    front_hours = nqvol.hours_to(asof, front["expiry"])
    iv_atm = front["atm"]
    em = nqvol.expected_move(S, iv_atm)             # one session
    _say(a, f"spot {S:,.2f}   ref {ref_symbol} {F_ref:,.2f}   report {a.report_date}   as of {asof:%Y-%m-%d %H:%M}Z"
          f"   {len(books)} live expiries   front {front['root']} in {front_hours:.1f}h")
    if not P["0DTE"]:
        _say(a, f"  !! no expiry on {a.report_date}: the 0DTE panel is EMPTY, not flat. Front book is "
              f"{front['root']} ({front['dte']} sessions out).")
    _say(a, f"{'panel':6} {'expiries':>9} {'HVL':>22} {'net gamma':>11}")
    # hours from the plan's as-of to the report unit's close: equals --hours on a
    # normal build, and the reopen-to-halt span on a holiday or an --asof build
    hours_out = round(nqvol.hours_to(asof, nqvol.close_utc(datetime.date.fromisoformat(a.report_date))), 4)
    out = {"spot": S, "hours": hours_out, "front_expiry_hours": round(front_hours, 2),
           "front_root": front["root"], "front_expiry": front["expiry"].isoformat(timespec="minutes"),
           "panels": {}, "vex": {}, "confluence": {},
           "iv_atm": iv_atm, "iv_basis": nqvol.IV_BASIS, "em": em, "em_basis": "one session",
           "rail_hi": S + em * a.rail_scale, "rail_lo": S - em * a.rail_scale,
           "rail_scale": a.rail_scale, "rail_label": a.rail_label, "em_full": em,
           "asof": asof.isoformat(timespec="minutes"),
           "settle": raw.get("date"), "report_date": a.report_date,
           "oi_vintage": a.oi_vintage,
           "last_print": a.last, "last_at": a.last_at,
           "last_vs_settle": (a.last - S) if a.last else None,
           "ref_symbol": ref_symbol, "ref_settle": F_ref,
           "basis_shifted": True, "grid": books[0].get("grid", GRID),
           "expiries": len(books),
           "no_0dte": not P["0DTE"],
           "excluded_risk_books": raw.get("excluded_risk_books", []),
           "risk_coverage": "partial" if raw.get("excluded_risk_books") else "all live books have an IV curve"}
    gmap = {}
    sweep = tuple(raw.get("hvl_sweep") or HVL_SWEEP)
    for name, bs in P.items():
        g, v = strike_metrics(bs, S_shift=S - F_ref)
        # nearest the reference FIRST: everything downstream reads hvl[0], and
        # "the flip" on a board with two crossings is the one next to price.
        raw_cross, prof = hvl_profile(bs, F_ref, *sweep) if bs else ([], None)
        crossings = sorted(raw_cross, key=lambda x: abs(x - S))
        gmap[name] = g
        out["panels"][name] = {
            "hvl": crossings,
            "gamma": {str(k): int(round(val)) for k, val in sorted(g.items()) if round(val)},
            "net_total": int(round(sum(g.values()))) if bs else None,
            "expiries": len(bs),
            # 2026-09-12: the curve the crossings were found on, and its read at
            # spot. Additive -- every consumer of this block names its keys.
            "profile": prof,
            "at_spot": profile_read(prof, S - F_ref) if prof else None,
        }
        _say(a, f"{name:6} {len(bs):9} {str(crossings):>22} "
              + (f"{sum(g.values()):11,.0f}" if bs else f"{'(no book)':>11}"))

    _, vex = strike_metrics(P["FULL"], S_shift=S - F_ref)
    out["vex"] = {str(k): round(val, 1) for k, val in sorted(vex.items()) if abs(val) >= 0.5}
    out["vex_total"] = sum(vex.values())
    out["by_expiry"] = []
    for b in books:
        g1, v1 = strike_metrics([b], S_shift=S - F_ref, on_axis=False)
        out["by_expiry"].append({"expiry": b["day"], "expiry_utc": b["expiry"].isoformat(timespec="minutes"),
                                 "root": b["root"], "dte": b["dte"], "T_sessions": round(b["T"] * 252, 3),
                                 "atm_iv": round(b["atm"], 5), "underlying": b["F"], "und": b["und"],
                                 "basis": b["basis"],
                                 "gamma": int(round(sum(g1.values()))), "vex": round(sum(v1.values()), 1),
                                 "oi": int(sum(r.get(c, {}).get("oi", 0) or 0 for r in b["strikes"].values() for c in ("C", "P")))})
    _say(a, f"\nVEX total {sum(vex.values()):+,.0f} contracts per vol point")
    rs, rl = a.rail_scale, a.rail_label
    _say(a, f"ATM IV {iv_atm*100:.2f}% (business time)   EM +/-{em:,.1f} (one session)"
          + (f"   {rl} band +/-{em*rs:,.1f} ({rs}x)" if rs != 1.0 else "")
          + f"   {rl} max {S+em*rs:,.0f}   {rl} min {S-em*rs:,.0f}")

    # Confluence on the PARTITIONED books. Thresholds are the old 0DTE / NEAR
    # ones for the two front books; the far book gets 20 (it used to be FULL's
    # 25, and FULL included everything).
    thr = {"0DTE": 5, "1-9": 12, "10+": 20}
    hits = {}                                    # k -> {book: sign}
    for name, bs in confluence_cols(books).items():
        if not bs:
            continue
        g, _ = strike_metrics(bs, S_shift=S - F_ref)
        for k, val in g.items():
            if abs(val) >= thr[name]:
                hits.setdefault(k, {})[name] = 1 if val > 0 else -1
    # Agreement means the SAME SIGN in two or more disjoint books. A strike
    # that is a call wall in the front book and a put wall in the far one is
    # a disagreement, not confluence.
    conf, cbooks = {}, {}
    for k, bysign in hits.items():
        for sgn in (1, -1):
            names = [n for n, s_ in bysign.items() if s_ == sgn]
            if len(names) > conf.get(k, 0):
                conf[k], cbooks[k] = len(names), names
    out["confluence"] = {str(k): c for k, c in sorted(conf.items()) if c >= 2}
    out["confluence_books"] = {str(k): v for k, v in sorted(cbooks.items()) if len(v) >= 2}
    out["confluence_basis"] = "same sign in 0DTE / 1-9 DTE / 10+ DTE (disjoint books)"
    return out, books, live_from(books, out, F_ref, ref_symbol, S, a)


def clock_knots(start, end):
    """Export the exact piecewise-linear business clock, including flat gaps.

    Starts seven calendar days before valuation for short replay/refresh windows.
    Rebuild the chain if a client asks outside the exported range.
    """
    d = start.date() - datetime.timedelta(days=7)
    last = end.date() + datetime.timedelta(days=1)
    points = {}
    def add(t):
        points[t.timestamp() * 1000] = nqvol.tau(t)
    while d <= last:
        if nqvol.unit_weight(d):
            o, c = nqvol.open_utc(d), nqvol.close_utc(d)
            add(o); add(c)
            if not nqvol.is_globex_holiday(d):
                add(nqvol.et(d - datetime.timedelta(days=1), 20))
                add(nqvol.et(d, 3))
                r = nqvol.rth_open_utc(d)
                for i in range(27):
                    add(r + (c - r) * (i / 26))
        d += datetime.timedelta(days=1)
    return sorted(points.items())


def live_from(books, out, F_ref, ref_symbol, S, a):
    """chain_live.json content: the inputs behind the map for the live re-price."""
    live = {"F_ref": F_ref, "ref_symbol": ref_symbol, "spot": S, "asof": out["asof"],
            "report_date": a.report_date, "settle": out["settle"],
            "oi_vintage": a.oi_vintage, "iv_basis": nqvol.IV_BASIS, "time_basis": "sessions",
            "sessions_per_year": nqvol.SESSIONS_PER_YEAR,
            "clock_knots": clock_knots(nqvol.expiry_dt(out["asof"]), max(b["expiry"] for b in books)),
            "risk_coverage": out.get("risk_coverage"), "excluded_risk_books": out.get("excluded_risk_books", []),
            "books": []}
    for b in books:
        rows = []
        for ks, rec in b["strikes"].items():
            K = float(ks)
            n = net_position(rec)
            m = log(K / b["F"])
            if not n:
                continue
            rows.append([K, int(n), b["smile"](m)])
        live["books"].append({"root": b["root"], "day": b["day"], "dte": b["dte"], "F": b["F"],
                              "und": b["und"], "basis": b["basis"], "T_sessions": round(b["T"] * 252, 4),
                              "expiry": b["expiry"].isoformat(timespec="minutes"),
                              "atm_iv": b["atm"], "smile": list(zip(b["smile"].x, b["smile"].y)),
                              "grid": b.get("grid", GRID), "cap": b.get("cap", MONEYNESS_CAP),
                              "strikes": sorted(rows)})
    return live


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", required=True)
    ap.add_argument("--report-date", required=True)
    ap.add_argument("--spot", type=float, default=None)
    ap.add_argument("--hours", type=float, default=6.5,
                    help="hours before the report session's close that the plan describes (6.5 = RTH open)")
    ap.add_argument("--ref", default=None, help="reference future symbol (default: the front book's underlying)")
    ap.add_argument("--asof", default=None, help="ISO UTC moment the plan describes (overrides --hours); "
                                                 "the holiday job passes the Globex reopen")
    # The expected move is ONE SESSION, settle to settle. Drawing that band
    # around a part-session makes it too wide, so the jobs pass a scale.
    ap.add_argument("--rail-scale", type=float, default=1.0)
    ap.add_argument("--rail-label", default="1D")
    ap.add_argument("--last", type=float, default=None, help="last traded price at build time")
    ap.add_argument("--last-at", default="", help="label for when the last price was struck, e.g. '05:32 UTC'")
    ap.add_argument("--oi-vintage", default="final", choices=["final", "preliminary"])
    ap.add_argument("--out", default="trinity.json")
    a = ap.parse_args()

    if a.hours <= 0:
        raise SystemExit(f"--hours {a.hours} <= 0: the plan would describe a moment after its own expiry")
    out, books, live = _analyse(a)
    json.dump(out, open(a.out, "w", encoding="utf-8"), indent=1)
    print(f"wrote {a.out}")
    lp = Path(a.out).with_name("chain_live.json")
    json.dump(live, open(lp, "w", encoding="utf-8"), separators=(",", ":"))
    print(f"wrote {lp} ({sum(len(b['strikes']) for b in live['books'])} strike rows)")


if __name__ == "__main__":
    main()
