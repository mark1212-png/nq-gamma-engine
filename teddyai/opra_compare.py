#!/usr/bin/env python3
"""
opra_compare.py -- the five books on one axis, in the one comparable unit.

    python opra_compare.py --date 2026-09-03
    python opra_compare.py --date 2026-09-03 --nq-chain data/prelim/2026-09-03-chain.json --hours 6.5

Build step 4 (2026-09-06). Numbers only -- no commentary. Each book goes
through trinity_db.analyse(), the same function the NQ sheet runs, for the
same plan moment (the RTH open of the next session unless --hours/--asof say
otherwise), and everything is expressed in DOLLAR GAMMA PER 1% MOVE:

    $gamma/1% = net contracts of delta per 1% x reference price x multiplier
                (100 for the equity roots, 20 for NQ)

which is additive across instruments on the same underlying, because a 1%
move is a 1% move whatever the ticker. Nothing here is compared in contract
gamma; that unit is not comparable across a 7,750 index, a 775 ETF and a
29,500 future.

THE MAPPING, COMPUTED DAILY, NEVER HARDCODED. QQQ and NQ are the same
underlying: NDX cash and NDX futures. Neither NDX cash nor its forward is in
our data, so the honest construction is that the QQQ front forward IS the NDX
forward in QQQ units, and one measured number -- QQQ points per NQ point,
F_QQQ_front / F_NQ_ref -- carries both the ETF ratio and the futures basis.
F_NQ_ref is the settle of THE CONTRACT THE TAPE IS ON (nq_reference() below),
not necessarily the one the front option book hedges into; in roll week those
differ by the calendar basis and a factor struck on the wrong one puts every
derived equity price a percent out. The mapping names the contract.
Same for SPY against the SPX front forward. Both inputs are stamped. A level
on one axis maps to the other by that factor; MONEYNESS needs no mapping.

THE TWO PAIRS AND THE ONE CROSS. QQQ vs NQ and SPY vs the SPX complex are
each one underlying seen through two books: agreement there is expected and
near-tautological. The comparison the engine exists for is the SPX complex
(SPX + SPXW + SPY, in dollars) against the NDX complex (QQQ + NQ): sign at
spot, flip as a % of spot, dollar size. Disagreement is what gets surfaced.
Whether disagreement predicts anything is step 5's test; nothing here says
it does.

STALENESS. Every book is priced at its own quote instant -- the equity roots'
end-of-day quotes (16:00 ET, disseminated 20:23-21:10 UTC) and NQ's 16:00 ET
settlement -- so for a next-morning plan they are equally old. For an Asia
or London plan (--asof) the equity books cannot be refreshed; NQ can be
re-referenced on the last print (--nq-last). The output carries the age of
every book at the plan moment and refuses nothing, but stamps `stale` on any
equity book more than STALE_HOURS old when NQ has a fresher reference.

Output: data/opra/<date>-compare.json and a printed table.
"""

import sys, json, argparse, datetime
from pathlib import Path

import nqvol
import trinity_db
import opra_ladder

# THE NQ REFERENCE CONTRACT (2026-09-16). trinity_db, left to itself, references the
# front option book's underlying, which in roll week is the EXPIRING future. On the
# 2026-09-15 settle that put the NQ column and the QQQ-per-NQ factor on NQU6
# (28,955.00) while the bars, the sheet and the tape were on NQZ6 (29,246.75): the
# command center multiplied a December print by a September factor, QQQ's estimated
# spot read one percent high, and the QQQ ghost landed 292 points off the NQ axis.
#
# The factor has to be struck on the contract the tape is on, and that contract is
# decided in ONE place, live_feed.SYMBOL -- autopull passes the same symbol to
# trinity_db as --ref for every sheet. It is used here only when it is a raw contract
# the chain actually settles: a continuous symbol (NQ.c.0, once the roll is done) or
# a chain from another era (the backtest) falls back to the front book's underlying,
# and the mapping records which of the three happened.
try:
    from live_feed import SYMBOL as BAR_SYMBOL
except Exception:
    BAR_SYMBOL = None


def nq_reference(raw, override=None, pinned=BAR_SYMBOL):
    """
    (ref_symbol, how) for the NQ book: the explicit --ref, else the bar feed's contract
    when the chain settles it, else None -- trinity_db's default, the front book's
    underlying. A --ref the chain cannot price is refused rather than silently
    replaced: trinity_db.load falls back to the front book's forward but keeps the
    symbol you asked for, and the mapping would then be mislabelled.
    """
    fut = raw.get("futures_settle") or {}
    outrights = sorted(k for k in fut if "-" not in k)
    if override:
        if override not in fut:
            raise SystemExit(f"  --ref {override}: the NQ chain has no settle for it "
                             f"(it settles {', '.join(outrights)})")
        return override, "--ref"
    if pinned and not pinned.startswith("NQ.") and pinned in fut:
        return pinned, "the bar feed's contract (live_feed.SYMBOL)"
    return None, "the front book's underlying"


BUCKETS = [(-3.0, -2.0), (-2.0, -1.0), (-1.0, -0.5), (-0.5, 0.0), (0.0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 3.0)]
STALE_HOURS = 10.0
NQ_MULT = 20.0


def book(label, chain_paths, rd, hours, asof, mult=None, spot=None, ref=None):
    raw, root, path = opra_ladder.merge_chains(chain_paths)
    if root != "NQ" and "grid" not in raw:
        import opra_pull, tempfile
        raw.update(opra_pull.ROOTS[root]["scale"])
        path = Path(tempfile.gettempdir()) / f"opra-compare-{Path(chain_paths[0]).name}"
        json.dump(raw, open(path, "w", encoding="utf-8"))
    m = float(mult if mult is not None else raw.get("multiplier", NQ_MULT))
    out, books, live = trinity_db.analyse(str(path), rd, hours=hours, asof=asof, spot=spot, ref=ref, quiet=True)
    S, F, em = out["spot"], out["ref_settle"], out["em"]
    scale = F * m / 1e6                                     # contracts of delta per 1% -> $M per 1%
    g = {float(k): v for k, v in out["panels"]["FULL"]["gamma"].items()}
    buckets = {}
    for lo, hi in BUCKETS:
        buckets[f"{lo:+.1f}..{hi:+.1f}%"] = round(sum(v for k, v in g.items()
                                                      if lo <= (k - S) / S * 100 < hi) * scale, 1)
    within = lambda pct: round(sum(v for k, v in g.items() if abs(k - S) / S * 100 <= pct) * scale, 1)
    panels = {}
    for name in ("0DTE", "NEAR", "FULL"):
        p = out["panels"][name]
        gg = {float(k): v for k, v in p["gamma"].items()}
        panels[name] = dict(expiries=p["expiries"],
                            at_spot_musd=round(sum(v for k, v in gg.items() if abs(k - S) <= em) * scale, 1),
                            column_musd=round((p["net_total"] or 0) * scale, 1),
                            flip=p["hvl"][0] if p["hvl"] else None,
                            flip_pct=round((p["hvl"][0] - S) / S * 100, 3) if p["hvl"] else None)
    quote_utc = raw.get("iv_ref") or nqvol.close_utc(datetime.date.fromisoformat(raw["date"])).isoformat(timespec="minutes")
    anchors = {}
    for st in raw["chain"].values():
        for rec in st.values():
            for c in rec.values():
                q = c.get("quote_utc")
                if q:
                    anchors[q] = anchors.get(q, 0) + 1
    fallback = [b["day"] for b in books if not b["smile"].x]
    live["multiplier"] = m                                  # the page needs it to convert to NQ-equivalent bars
    return dict(label=label, roots=root, spot=S, ref=F, ref_symbol=out["ref_symbol"], multiplier=m, _live=live,
                front_symbol=books[0]["und"], front_forward=books[0]["F"],   # what the front book hedges into
                em=round(em, 3), em_pct=round(em / S * 100, 3), atm_iv=round(out["iv_atm"], 5),
                expiries=out["expiries"], front=out["front_root"], front_hours=out["front_expiry_hours"],
                asof=out["asof"], quote_instant=quote_utc,
                quote_anchor=max(anchors, key=anchors.get) if anchors else "settlement",
                price_basis=raw.get("price_basis", "CME settlement"), oi_vintage=raw.get("oi_vintage", "final"),
                exercise=raw.get("exercise", "european (futures option)"),
                american=(raw.get("american") or {}).get("model"),
                buckets=buckets, within_1pct_musd=within(1.0), within_2pct_musd=within(2.0),
                within_em_musd=panels["FULL"]["at_spot_musd"], column_musd=panels["FULL"]["column_musd"],
                panels=panels, fallback_books=fallback,
                gamma_axis={f"{k:g}": round(v * scale, 2) for k, v in sorted(g.items())})


def age_hours(b):
    """Hours from the book's quote instant to the plan's as-of."""
    q = datetime.datetime.fromisoformat(b["quote_instant"].replace("Z", "+00:00"))
    a = datetime.datetime.fromisoformat(b["asof"].replace("Z", "+00:00"))
    return round((a - q).total_seconds() / 3600, 2)


def pair(a, b, factor_name, factor):
    """Two books on one underlying: signs, flips (own points, % of spot, and b's flip mapped onto a's axis)."""
    sa, sb = a["within_1pct_musd"], b["within_1pct_musd"]
    fa, fb = a["panels"]["FULL"]["flip_pct"], b["panels"]["FULL"]["flip_pct"]
    return dict(a=a["label"], b=b["label"], factor_name=factor_name, factor=round(factor, 6),
                a_within_1pct_musd=sa, b_within_1pct_musd=sb,
                same_sign=(sa > 0) == (sb > 0) if sa and sb else None,
                dollar_ratio=round(sa / sb, 3) if sb else None,
                a_flip_pct=fa, b_flip_pct=fb,
                flip_gap_pct=round(fa - fb, 3) if fa is not None and fb is not None else None,
                # factor is "a points per b point", so a level on b's axis times the factor lands on a's
                b_flip_on_a_axis=round(b["panels"]["FULL"]["flip"] * factor, 2)
                if b["panels"]["FULL"]["flip"] is not None else None)


def complex_(label, members):
    """Dollar gamma of a complex: the members' buckets and at-spot sums added, flip = dollar-weighted mean of the members' flips in %."""
    tot = {k: round(sum(m["buckets"][k] for m in members), 1) for k in members[0]["buckets"]}
    w1 = sum(m["within_1pct_musd"] for m in members)
    flips = [(m["panels"]["FULL"]["flip_pct"], abs(m["within_1pct_musd"])) for m in members
             if m["panels"]["FULL"]["flip_pct"] is not None]
    fw = sum(w for _, w in flips)
    return dict(label=label, members=[m["label"] for m in members], buckets=tot,
                within_1pct_musd=round(w1, 1),
                within_2pct_musd=round(sum(m["within_2pct_musd"] for m in members), 1),
                within_em_musd=round(sum(m["within_em_musd"] for m in members), 1),
                sign_at_spot=("long" if w1 > 0 else "short") if w1 else None,
                flip_pct_dollar_weighted=round(sum(f * w for f, w in flips) / fw, 3) if fw else None,
                member_flips_pct={m["label"]: m["panels"]["FULL"]["flip_pct"] for m in members})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="quote / settle date of the chains")
    ap.add_argument("--report-date", default=None)
    ap.add_argument("--hours", type=float, default=6.5)
    ap.add_argument("--asof", default=None)
    ap.add_argument("--opra", default=str(Path("data") / "opra"))
    ap.add_argument("--nq-chain", default=None, help="NQ chain for the same settle date (default data/<date>-chain.json, then data/prelim/)")
    ap.add_argument("--ref", default=None,
                    help="NQ reference future (default: the bar feed's contract when the chain settles it, "
                         "else the front book's underlying)")
    ap.add_argument("--nq-last", type=float, default=None,
                    help="re-reference NQ on a last print ON THE REFERENCE CONTRACT (an Asia/London plan)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    d0 = datetime.date.fromisoformat(a.date)
    rd = a.report_date or nqvol.next_session(d0).isoformat()
    op = Path(a.opra)
    f = lambda root: str(op / f"{a.date}-{root}-chain.json")
    for root in ("SPX", "SPXW", "SPY", "QQQ"):
        if not Path(f(root)).exists():
            sys.exit(f"  missing {f(root)} -- run opra_pull.py --fetch --date {a.date} --roots {root}")
    nq = a.nq_chain
    if nq is None:
        for cand in (Path("data") / f"{a.date}-chain.json", Path("data") / "prelim" / f"{a.date}-chain.json"):
            if cand.exists() and cand.stat().st_size > 500_000:
                nq = str(cand); break
    if nq is None:
        sys.exit(f"  no NQ chain for {a.date} in data/ or data/prelim/ (>500 KB) -- pass --nq-chain")
    nq_raw = json.load(open(nq, encoding="utf-8"))
    nq_date = nq_raw.get("date")

    spx = book("SPX complex (SPX+SPXW)", [f("SPX"), f("SPXW")], rd, a.hours, a.asof)
    spy = book("SPY", [f("SPY")], rd, a.hours, a.asof)
    qqq = book("QQQ", [f("QQQ")], rd, a.hours, a.asof)
    nq_ref, nq_ref_how = nq_reference(nq_raw, a.ref)
    nqb = book("NQ", [nq], rd, a.hours, a.asof, mult=NQ_MULT, spot=a.nq_last, ref=nq_ref)
    nqb["chain"] = nq
    nqb["settle_date"] = nq_date
    nqb["oi_vintage"] = "preliminary" if "prelim" in nq else nqb["oi_vintage"]

    # the mapping: one measured number per pair, both inputs stamped
    k_qqq = qqq["ref"] / nqb["ref"]                 # QQQ points per NQ point (ratio x basis)
    k_spy = spy["ref"] / spx["ref"]                 # SPY points per SPX point
    roll_week = nqb["front_symbol"] != nqb["ref_symbol"]
    mapping = dict(qqq_per_nq_point=round(k_qqq, 8), qqq_front=qqq["ref"], qqq_front_expiry=qqq["ref_symbol"],
                   nq_ref=nqb["ref"], nq_ref_symbol=nqb["ref_symbol"], nq_ref_how=nq_ref_how, nq_settle_date=nq_date,
                   # the contract the front option book hedges into, and the calendar basis between the two
                   # when they differ (roll week): a reader holding a print on the other contract can undo it
                   nq_front_symbol=nqb["front_symbol"], nq_front_settle=nqb["front_forward"],
                   nq_basis=round(nqb["ref"] - nqb["front_forward"], 2) if roll_week else 0.0,
                   spy_per_spx_point=round(k_spy, 8), spy_front=spy["ref"], spx_front=spx["ref"],
                   spx_front_expiry=spx["ref_symbol"],
                   note="F_QQQ_front / F_NQ_ref carries the ETF ratio AND the futures basis; recomputed from "
                        "the day's files, never hardcoded. Moneyness needs no mapping.")

    books = [spx, spy, qqq, nqb]
    for b in books:
        b["age_hours"] = age_hours(b)
    nq_age = nqb["age_hours"] if a.nq_last is None else 0.0
    for b in (spx, spy, qqq):
        b["stale"] = bool(b["age_hours"] > STALE_HOURS and b["age_hours"] > nq_age + 1.0)

    ndx_complex = complex_("NDX complex (QQQ+NQ)", [qqq, nqb])
    spx_complex = complex_("SPX complex (SPX+SPXW+SPY)", [spx, spy])
    cross = dict(spx=spx_complex, ndx=ndx_complex,
                 same_sign_at_spot=spx_complex["sign_at_spot"] == ndx_complex["sign_at_spot"],
                 dollar_ratio=round(spx_complex["within_1pct_musd"] / ndx_complex["within_1pct_musd"], 2)
                 if ndx_complex["within_1pct_musd"] else None,
                 flip_gap_pct=(round(spx_complex["flip_pct_dollar_weighted"] - ndx_complex["flip_pct_dollar_weighted"], 3)
                               if None not in (spx_complex["flip_pct_dollar_weighted"], ndx_complex["flip_pct_dollar_weighted"]) else None),
                 disagreement=spx_complex["sign_at_spot"] != ndx_complex["sign_at_spot"])

    result = dict(date=a.date, report_date=rd, asof=spx["asof"], hours=a.hours, nq_chain=nq, nq_settle_date=nq_date,
                  date_mismatch=nq_date != a.date, unit="USD millions of dealer gamma per 1% move",
                  sign_convention="dealers long calls / short puts, net = C - P open interest (as NQ)",
                  mapping=mapping, books={b["label"]: b for b in books},
                  pairs=dict(qqq_vs_nq=pair(qqq, nqb, "qqq_per_nq_point", k_qqq),
                             spy_vs_spx=pair(spy, spx, "spy_per_spx_point", k_spy)),
                  cross=cross, hypothesis_status="UNTESTED -- step 5; no prose may use the cross row until then")
    outp = Path(a.out) if a.out else op / f"{a.date}-compare.json"
    # each equity book's chain_live.json beside the compare file, in exactly the NQ shape, for the
    # command center's Strike Exposure overlay (/cc/api/opra/chain?root=QQQ) -- 2026-09-06, step 6a
    for b in (spx, spy, qqq):
        lp = op / f"{a.date}-{b['roots']}-chain_live.json"
        json.dump(b.pop("_live"), open(lp, "w", encoding="utf-8"), separators=(",", ":"))
    nqb.pop("_live", None)
    slim = json.loads(json.dumps(result))
    for b in slim["books"].values():
        b.pop("gamma_axis", None)                      # the full axes go to the file, not the screen
    json.dump(result, open(outp, "w", encoding="utf-8"), indent=1)

    # ---- print --------------------------------------------------------------
    print(f"\n  five books, {a.date} quotes, plan for {rd} as of {spx['asof']}Z   unit: $M dealer gamma per 1% move")
    if result["date_mismatch"]:
        print(f"  !! NQ chain is settle {nq_date}, not {a.date} -- the NQ column is a different day")
    print(f"  mapping: QQQ per NQ point {k_qqq:.6f} ({qqq['ref']:.2f} / {nqb['ref']:,.2f} {nqb['ref_symbol']})   "
          f"SPY per SPX point {k_spy:.6f} ({spy['ref']:.2f} / {spx['ref']:,.2f})")
    print(f"  NQ referenced on {nqb['ref_symbol']} ({nq_ref_how})"
          + (f"; the front book hedges {nqb['front_symbol']} {nqb['front_forward']:,.2f}, basis "
             f"{nqb['ref'] - nqb['front_forward']:+,.2f} -- ROLL WEEK, the factor is on the traded contract"
             if roll_week else "") + "\n")
    cols = [b["label"] for b in books]
    w = 26
    print(f"  {'':22}" + "".join(f"{c[:w]:>{w}}" for c in cols))
    row = lambda name, vals: print(f"  {name:22}" + "".join(f"{v:>{w}}" for v in vals))
    row("reference", [f"{b['ref']:,.2f}" for b in books])
    row("quote anchor (UTC)", [f"{b['quote_anchor']}" for b in books])
    row("age at plan (h)", [f"{b['age_hours']:.1f}{' STALE' if b.get('stale') else ''}" for b in books])
    row("OI vintage", [("as of " + b["oi_vintage"][9:19] + " close") if b["oi_vintage"].startswith("OI as of") else b["oi_vintage"][:w] for b in books])
    row("exercise", [b["exercise"][:w] for b in books])
    row("expiries", [f"{b['expiries']}" for b in books])
    row("front / hours", [f"{b['front']} / {b['front_hours']:.1f}" for b in books])
    row("ATM IV (bus. time)", [f"{b['atm_iv'] * 100:.2f}%" for b in books])
    row("EM one session", [f"{b['em']:,.1f} ({b['em_pct']:.2f}%)" for b in books])
    print()
    row("$G/1% within 1 EM", [f"{b['within_em_musd']:,.1f}" for b in books])
    row("$G/1% within 1%", [f"{b['within_1pct_musd']:,.1f}" for b in books])
    row("$G/1% within 2%", [f"{b['within_2pct_musd']:,.1f}" for b in books])
    row("$G/1% column", [f"{b['column_musd']:,.1f}" for b in books])
    for name in ("0DTE", "NEAR"):
        row(f"{name} $G/1% at spot", [f"{b['panels'][name]['at_spot_musd']:,.1f}" if b["panels"][name]["expiries"] else "(no book)" for b in books])
    print()
    row("flip FULL (own pts)", [f"{b['panels']['FULL']['flip']:,.1f}" if b["panels"]["FULL"]["flip"] is not None else "--" for b in books])
    row("flip FULL (% of spot)", [f"{b['panels']['FULL']['flip_pct']:+.2f}%" if b["panels"]["FULL"]["flip_pct"] is not None else "--" for b in books])
    row("flip 0DTE (% of spot)", [f"{b['panels']['0DTE']['flip_pct']:+.2f}%" if b["panels"]["0DTE"]["flip_pct"] is not None else "--" for b in books])
    print()
    print("  $G/1% by moneyness bucket (FULL book):")
    for k in spx["buckets"]:
        row(f"  {k}", [f"{b['buckets'][k]:,.1f}" for b in books])
    fb = [(b["label"], b["fallback_books"]) for b in books if b["fallback_books"]]
    if fb:
        print("\n  !! books priced at the 20% fallback vol (no solved IV): "
              + "; ".join(f"{l}: {', '.join(d)}" for l, d in fb))

    print("\n  pairs (one underlying, two books -- agreement expected):")
    for name, p in result["pairs"].items():
        print(f"    {p['a']} vs {p['b']}: within-1% ${p['a_within_1pct_musd']:,.0f}M vs ${p['b_within_1pct_musd']:,.0f}M "
              f"(ratio {p['dollar_ratio']}), same sign {p['same_sign']}; flips {p['a_flip_pct']:+.2f}% vs "
              f"{p['b_flip_pct']:+.2f}% of spot (gap {p['flip_gap_pct']:+.2f}%); {p['b']} flip on {p['a']} axis "
              f"{p['b_flip_on_a_axis']:,.2f} vs {p['a']} own {result['books'][p['a']]['panels']['FULL']['flip']:,.2f}")
    c = cross
    print("\n  cross (the comparison the engine exists for -- UNTESTED, step 5):")
    for cx in (c["spx"], c["ndx"]):
        print(f"    {cx['label']:30} within-1% ${cx['within_1pct_musd']:,.0f}M  within-EM ${cx['within_em_musd']:,.0f}M  "
              f"sign at spot {cx['sign_at_spot']}  flip {cx['flip_pct_dollar_weighted']:+.2f}% ($-weighted; members "
              + ", ".join(f"{k} {v:+.2f}%" for k, v in cx["member_flips_pct"].items() if v is not None) + ")")
    print(f"    dollar ratio SPX/NDX {c['dollar_ratio']}   same sign at spot {c['same_sign_at_spot']}   "
          f"flip gap {c['flip_gap_pct']:+.2f}%   DISAGREEMENT {c['disagreement']}")
    print(f"\n  wrote {outp}\n")


if __name__ == "__main__":
    main()
