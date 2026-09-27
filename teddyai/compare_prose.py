# -*- coding: utf-8 -*-
"""
compare_prose.py -- two plans for the same board, side by side.

    python compare_prose.py                       the two most recent revisions of the live snapshot
    python compare_prose.py --list                every revision on disk, newest first
    python compare_prose.py A.json B.json         two narrative.json files by path
    python compare_prose.py --a <rev> --b <rev>   two revision timestamps

Mark, 2026-09-17: *"can we always generate prose using that and then I ask you and compare?"*
The scheduled gameplan run writes one plan from CARD_RULES.md; a hand-written /build writes
another. Both land in notes/commentary-revisions/<snapshot_id>/<timestamp>/, so both survive
and this prints what they actually disagree about.

COMPARE WITHIN ONE SNAPSHOT. The revisions directory is keyed by the sha256 of trinity.json, so
two plans under the same key were written against the same board and the differences are the
writer's. Across keys the board moved too, and the comparison is worth much less -- this warns
when that happens. For a clean A/B: let the machine run and publish, then write the second plan
against the SAME trinity.json (publish_plan.py alone, no plan_build).
"""
import argparse
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
REV = ROOT / "notes" / "commentary-revisions"


def live_snapshot_id():
    try:
        return json.loads((ROOT / "publication.json").read_text(encoding="utf-8"))["snapshot_id"]
    except Exception:
        t = (ROOT / "trinity.json").read_bytes()
        return hashlib.sha256(t).hexdigest()


def revisions(sid=None):
    """[(snapshot_id, timestamp, path-to-narrative.json)], newest first."""
    out = []
    for sd in REV.iterdir() if REV.exists() else []:
        if sid and sd.name != sid:
            continue
        for td in sd.iterdir():
            n = td / "narrative.json"
            if n.exists():
                out.append((sd.name, td.name, n))
    return sorted(out, key=lambda r: r[1], reverse=True)


def load(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def card_row(c):
    e, sl = float(c["entry"]), float(c["sl"])
    tps = [float(t) for t in (c.get("tp") or [])]
    risk = abs(e - sl) or 1
    r1 = abs(tps[0] - e) / risk if tps else 0
    return dict(name=c.get("name", "?"), side=c.get("side", "?"), rank=c.get("rank", "?"),
                entry=e, sl=sl, risk=risk, tp1=tps[0] if tps else 0, r1=r1)


def summarise(doc):
    prose = doc["prose"]
    return dict(cards=[card_row(c) for c in prose.get("setups") or []],
                lead=prose.get("lead") or "", stamp=doc.get("stamp") or {},
                generated=doc.get("generated", ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", help="two narrative.json paths")
    ap.add_argument("--a"), ap.add_argument("--b")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--label-a", default="A"), ap.add_argument("--label-b", default="B")
    a = ap.parse_args()

    if a.list:
        for sid, ts, _ in revisions():
            print(f"  {sid[:12]}  {ts}")
        return 0

    if len(a.paths) == 2:
        pa, pb = a.paths
        sa = sb = "(by path)"
    else:
        revs = revisions(live_snapshot_id()) or revisions()
        pick = lambda t: next((r for r in revs if r[1] == t), None)
        ra, rb = (pick(a.a), pick(a.b)) if (a.a and a.b) else (revs[0] if revs else None,
                                                               revs[1] if len(revs) > 1 else None)
        if not ra or not rb:
            print("  need two revisions; run --list"); return 1
        sa, pa, sb, pb = ra[0], ra[2], rb[0], rb[2]
        print(f"  A  {ra[1]}\n  B  {rb[1]}")
        if sa != sb:
            print("\n  WARNING: different snapshots -- the board moved between these two, so the\n"
                  "  differences are not purely the writer's.")

    A, B = summarise(load(pa)), summarise(load(pb))
    for label, S in ((a.label_a, A), (a.label_b, B)):
        print(f"\n=== {label} ===")
        print(f"  lead {len(S['lead']):,} chars")
        for i, c in enumerate(S["cards"], 1):
            print(f"  {i}. {c['rank']:<13} {c['side']:<5} {c['name'][:30]:<30} "
                  f"E {c['entry']:>9,.0f}  SL {c['sl']:>9,.1f} ({c['risk']:>5.1f}pt)  "
                  f"TP1 {c['tp1']:>9,.0f} = {c['r1']:.2f}R")

    print("\n=== where they differ ===")
    # Pair on the LEVEL, not the name: the same trade gets written "29,750 rejection" by one
    # writer and "29,748-29,775 rejection" by another, and name-matching called that two
    # different cards.
    TOL = 30.0
    pairs, usedB = [], set()
    for ca in A["cards"]:
        best, bd = None, 1e9
        for j, cb in enumerate(B["cards"]):
            if j in usedB or ca["side"] != cb["side"]:
                continue
            d = abs(ca["entry"] - cb["entry"])
            if d < bd:
                best, bd = j, d
        if best is not None and bd <= TOL:
            usedB.add(best); pairs.append((ca, B["cards"][best], bd))
    onlyA = [c for c in A["cards"] if c not in [p[0] for p in pairs]]
    onlyB = [c for j, c in enumerate(B["cards"]) if j not in usedB]
    print(f"  same trade, both : {[f'{a[chr(39)+chr(39)] if False else a[chr(110)+chr(97)+chr(109)+chr(101)]} / {b[chr(110)+chr(97)+chr(109)+chr(101)]} ({d:.0f}pt apart)' for a, b, d in pairs] or 'none'}")
    print(f"  only in A        : {[c['name'] for c in onlyA] or 'none'}")
    print(f"  only in B        : {[c['name'] for c in onlyB] or 'none'}")
    for ca, cb, _ in pairs:
        if ca["rank"] != cb["rank"]:
            print(f"  {ca['name'][:28]:<28} RANK   A={ca['rank']}  B={cb['rank']}")
        for k, lab in (("entry", "entry"), ("sl", "stop "), ("tp1", "TP1  ")):
            if abs(ca[k] - cb[k]) > 0.01:
                print(f"  {ca['name'][:28]:<28} {lab}  A={ca[k]:,.1f}  B={cb[k]:,.1f}"
                      + (f"   ({abs(ca['risk'] - cb['risk']):.0f}pt of risk apart)" if k == "sl" else ""))
    pa_, pb_ = [c for c in A["cards"] if c["rank"] == "Primary"], [c for c in B["cards"] if c["rank"] == "Primary"]
    print(f"  Primary         : A={pa_[0]['name'] if pa_ else 'NONE'}  B={pb_[0]['name'] if pb_ else 'NONE'}")
    print(f"  lead length     : A={len(A['lead']):,}  B={len(B['lead']):,}")
    print("\n  Card selection, merging, ranking and salience are the comparison. This prints the\n"
          "  geometry; read both leads to judge the rest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
