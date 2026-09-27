#!/usr/bin/env python3
"""
opra_ladder.py -- one equity book through trinity_db.py, printed as a ladder.

    python opra_ladder.py --chain data/opra/2026-09-03-SPX-chain.json --report-date 2026-09-04
    python opra_ladder.py --chain ... --hours 2.5 --band 0.03 --rows 40

Build step 2 (2026-09-06). Runs trinity_db.analyse() -- the same function the
NQ sheet and the NQ backtest run -- on an OPRA chain and writes
data/opra/<date>-<root>-trinity.json and -chain_live.json beside it. It does
NOT go through gen.py / build.py: those render the TeddyAI sheet, and the
equity books are a cross-reference, not a second sheet.

What the ladder shows, and in which unit:

  net gamma   contracts of delta per 1% move at that strike ON THE FRONT
              FORWARD'S AXIS (each expiry basis-shifted by F_book - F_front,
              bucketed to the chain's `grid`), dealer sign convention as NQ:
              long calls / short puts, net = C - P open interest. Same number
              trinity_db puts on the NQ sheet.
  $gamma/1%   the same, times the forward, times the multiplier (100 for the
              equity roots; NQ is 20), in $ millions. THE ONLY UNIT IN WHICH
              SPX, SPY, QQQ AND NQ ARE COMPARABLE (build step 4). No
              cross-instrument statement may be made in the contract column.
  VEX         dDelta per vol point, contracts.
  net OI      C - P open interest landing at that axis strike, all books.

Two things about an SPX-root ladder that are not bugs: the root holds the
AM-settled monthlies only, so the 0DTE panel is empty on all but twelve days a
year and the NEAR (<= 9 sessions) panel is usually empty too -- the weeklies
live in SPXW (step 3); and the reference is the FRONT MONTHLY'S PARITY
FORWARD, not an index level, so "spot" here is F_front. Both are printed.

Aggregate gamma is reported AT SPOT (within one expected move) as well as the
chain-wide column sum, because HANDOFF says so: the two routinely disagree in
sign and the column is dominated by far strikes carrying no gamma.
"""

import sys, json, argparse, datetime
from pathlib import Path

import nqvol
import trinity_db

MULTIPLIER = {"NQ": 20.0}          # equity roots carry `multiplier` in the chain file


SCALE_KEYS = ("grid", "hvl_sweep", "moneyness_cap", "min_premium", "multiplier")


def merge_chains(paths):
    """
    (raw, root_label, path). One file: as is. Several: the union of their
    books -- the SPX complex is SPX (AM monthlies) + SPXW (PM weeklies), one
    underlying split across two roots exactly as NQ is split across 27, and
    trinity_db keys books by `date|root` so nothing collides. Refuses files
    from different dates, different underlyings or different scales; the
    merged file is written to the temp folder and the label is 'SPX+SPXW'.
    """
    raws = [json.load(open(p, encoding="utf-8")) for p in paths]
    if len(raws) == 1:
        return raws[0], raws[0].get("root", "NQ"), paths[0]
    import opra_pull
    for r in raws:                                    # a step-1 file without scale keys
        if "grid" not in r and r.get("root") in opra_pull.ROOTS:
            r.update(opra_pull.ROOTS[r["root"]]["scale"])
            print(f"  {r['root']} chain carried no scale keys; stamped {opra_pull.ROOTS[r['root']]['scale']} for the merge")
    base = raws[0]
    for r in raws[1:]:
        if r["date"] != base["date"]:
            raise SystemExit(f"  merge refused: {r['root']} is {r['date']}, {base['root']} is {base['date']}")
        if r.get("underlying") != base.get("underlying"):
            raise SystemExit(f"  merge refused: {r['root']} is on {r.get('underlying')}, "
                             f"{base['root']} on {base.get('underlying')} -- not one book")
        for k in SCALE_KEYS:
            if r.get(k) != base.get(k):
                raise SystemExit(f"  merge refused: {k} differs ({base.get(k)} vs {r.get(k)})")
    merged = dict(base)
    merged["root"] = "+".join(r["root"] for r in raws)
    merged["merged_from"] = [r["root"] for r in raws]
    merged["chain"] = {}; merged["futures_settle"] = {}; merged["forwards"] = {}
    merged["excluded_expiries"] = {}
    for r in raws:
        for k, v in r["chain"].items():
            if k in merged["chain"]:
                raise SystemExit(f"  merge refused: book {k} present twice")
            merged["chain"][k] = v
        merged["futures_settle"].update(r["futures_settle"])
        merged["forwards"].update(r.get("forwards", {}))
        merged["excluded_expiries"].update(r.get("excluded_expiries", {}))
        for k in ("oi_vintage", "price_basis"):
            if r.get(k) != base.get(k):
                merged[k] = f"{base.get(k)} | {r['root']}: {r.get(k)}"
    import tempfile
    path = Path(tempfile.gettempdir()) / f"opra-ladder-{base['date']}-{merged['root']}-chain.json"
    json.dump(merged, open(path, "w", encoding="utf-8"))
    return merged, merged["root"], str(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", required=True, action="append",
                    help="chain file; repeat to MERGE roots of one underlying (SPX + SPXW) into one book")
    ap.add_argument("--report-date", default=None, help="default: the session after the chain date")
    ap.add_argument("--hours", type=float, default=6.5)
    ap.add_argument("--asof", default=None)
    ap.add_argument("--rail-scale", type=float, default=1.0)
    ap.add_argument("--rail-label", default="1D")
    ap.add_argument("--band", type=float, default=0.04, help="ladder half-width as a fraction of spot")
    ap.add_argument("--rows", type=int, default=28, help="max rows either side of spot (largest |gamma| kept)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    raw, root, chain_path = merge_chains(a.chain)
    mult = float(raw.get("multiplier", MULTIPLIER.get(root, 20.0)))
    d0 = datetime.date.fromisoformat(raw["date"])
    rd = a.report_date or nqvol.next_session(d0).isoformat()
    if root != "NQ" and "grid" not in raw:
        # a step-1 file, written before the scale keys existed: stamp the root's
        # scale on a scratch copy exactly as a re-fetch would (same values, same
        # source: opra_pull.ROOTS), and say so
        import opra_pull, tempfile
        raw.update(opra_pull.ROOTS[root]["scale"])
        chain_path = Path(tempfile.gettempdir()) / f"opra-ladder-{Path(a.chain[0]).name}"
        json.dump(raw, open(chain_path, "w", encoding="utf-8"))
        print(f"  chain carried no scale keys (step-1 file); stamped {opra_pull.ROOTS[root]['scale']} "
              f"on a scratch copy -- the next opra_pull.py --fetch writes them into the file")
    out, books, live = trinity_db.analyse(str(chain_path), rd, hours=a.hours, asof=a.asof,
                                          rail_scale=a.rail_scale, rail_label=a.rail_label, quiet=True)
    outp = Path(a.out) if a.out else Path(a.chain[0]).with_name(f"{raw['date']}-{root}-trinity.json")
    json.dump(out, open(outp, "w", encoding="utf-8"), indent=1)
    json.dump(live, open(outp.with_name(outp.name.replace("-trinity.json", "-chain_live.json")), "w",
                         encoding="utf-8"), separators=(",", ":"))

    S, em = out["spot"], out["em"]
    F_ref = out["ref_settle"]
    front = books[0]
    print(f"\n  {root} book from {raw['date']}   plan for {rd} as of {out['asof']}Z   hours {out['hours']}")
    print(f"  price basis: {raw.get('price_basis', 'CME settlement')[:70]}")
    print(f"  OI vintage: {raw.get('oi_vintage', 'final')}   exercise: {raw.get('exercise', 'n/a')}   "
          f"multiplier {mult:g}   grid {out['grid']:g}")
    print(f"  reference {out['ref_symbol']} {F_ref:,.2f} (front forward, parity)   spot {S:,.2f}   "
          f"{out['expiries']} live expiries   front {out['front_root']} {front['day']} "
          f"in {out['front_expiry_hours']:.1f}h (dte {front['dte']})")
    print(f"  ATM IV {out['iv_atm'] * 100:.2f}% (business time)   EM +/-{em:,.1f} (one session)   "
          f"{a.rail_label} rails {out['rail_lo']:,.0f} / {out['rail_hi']:,.0f}")

    # ---- panels: column sum, at-spot sum, flips ---------------------------
    P = trinity_db.panel_cols(books)
    print(f"\n  {'panel':6} {'expiries':>8} {'flip (nearest)':>16} {'net G column':>13} {'net G within 1 EM':>18} "
          f"{'$G/1% at spot ($M)':>19}")
    for name, bs in P.items():
        pan = out["panels"][name]
        if not bs:
            print(f"  {name:6} {0:8}   {'(no book)':>14}")
            continue
        g = {float(k): v for k, v in pan["gamma"].items()}
        at_spot = sum(v for k, v in g.items() if abs(k - S) <= em)
        flips = pan["hvl"]
        print(f"  {name:6} {len(bs):8} {(f'{flips[0]:,.1f}' if flips else '--'):>16} "
              f"{pan['net_total']:13,} {at_spot:18,.0f} {at_spot * F_ref * mult / 1e6:19,.1f}")
    conf = out.get("confluence", {})
    print(f"  flips (all crossings, FULL): {', '.join(f'{x:,.1f}' for x in out['panels']['FULL']['hvl']) or 'none'}"
          f"   confluence strikes: {len(conf)}")

    # ---- per expiry -------------------------------------------------------
    print(f"\n  {'expiry':10} {'dte':>4} {'set':>3} {'F':>10} {'basis':>7} {'ATM IV':>7} {'net G':>9} "
          f"{'$G/1% ($M)':>11} {'VEX':>8} {'OI':>10}")
    fw = raw.get("forwards", {})
    fallback = [b for b in books if not b["smile"].x]
    for e in out["by_expiry"]:
        style = fw.get(e["und"], {}).get("settlement", "")
        if any(b["und"] == e["und"] for b in fallback):
            style += "!"
        print(f"  {e['expiry']:10} {e['dte']:4d} {style:>3} {e['underlying']:10,.2f} {e['basis']:7,.1f} "
              f"{e['atm_iv'] * 100:6.2f}% {e['gamma']:9,} {e['gamma'] * e['underlying'] * mult / 1e6:11,.1f} "
              f"{e['vex']:8,.1f} {e['oi']:10,}")

    if fallback:
        print(f"  !! {len(fallback)} book(s) marked '!' carry NO solved vol and are priced at trinity_db's "
              f"{fallback[0]['atm'] * 100:.0f}% fallback: " + ", ".join(b["day"] for b in fallback)
              + f"  -- {sum(int(round(sum(trinity_db.strike_metrics([b], on_axis=False)[0].values()))) for b in fallback):,} "
              f"contracts of delta per 1% of the FULL column rest on that guess")

    # ---- the ladder -------------------------------------------------------
    full = {float(k): v for k, v in out["panels"]["FULL"]["gamma"].items()}
    vex = {float(k): v for k, v in out["vex"].items()}
    # net OI per axis strike, all books, same shift and bucket as strike_metrics
    noi = {}
    for b in books:
        for ks, rec in b["strikes"].items():
            n = rec.get("C", {}).get("oi", 0) - rec.get("P", {}).get("oi", 0)
            if n:
                k = trinity_db.axis_strike(float(ks), b["basis"], b.get("grid", trinity_db.GRID))
                noi[k] = noi.get(k, 0) + n
    lo, hi = S * (1 - a.band), S * (1 + a.band)
    above = sorted([k for k in full if S < k <= hi], key=lambda k: -abs(full[k]))[:a.rows]
    below = sorted([k for k in full if lo <= k <= S], key=lambda k: -abs(full[k]))[:a.rows]
    rows = sorted(above + below, reverse=True)
    marks = {round(out["rail_hi"]): f"{a.rail_label} max", round(out["rail_lo"]): f"{a.rail_label} min"}
    for x in out["panels"]["FULL"]["hvl"]:
        marks[round(x)] = "FLIP"
    print(f"\n  ladder, FULL book, +/-{a.band * 100:.0f}% of spot, {len(rows)} largest strikes; "
          f"$G/1% = net G x F x {mult:g}\n")
    print(f"  {'strike':>9} {'vs spot':>8} {'net G':>9} {'$G/1% ($M)':>11} {'VEX':>8} {'net OI':>10}   note")
    printed_spot = False
    for k in rows:
        if not printed_spot and k < S:
            print(f"  {S:9,.1f} {'':>8} {'':>9} {'':>11} {'':>8} {'':>10}   <- spot / reference")
            printed_spot = True
        note = marks.get(round(k), "")
        for m, lab in marks.items():
            if abs(m - k) < (rows[0] - rows[-1]) / max(len(rows), 1) / 2 and lab not in note:
                note = (note + " " + lab).strip()
        print(f"  {k:9,.1f} {k - S:+8,.1f} {full[k]:9,} {full[k] * F_ref * mult / 1e6:11,.1f} "
              f"{vex.get(k, 0.0):8,.1f} {noi.get(k, 0):10,}   {note}")
    if not printed_spot:
        print(f"  {S:9,.1f}   <- spot / reference")
    print(f"\n  wrote {outp} and its chain_live.json\n")


if __name__ == "__main__":
    main()
