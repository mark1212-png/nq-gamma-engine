#!/usr/bin/env python3
"""
check_identical.py -- is the NQ pipeline bit-identical after a change?

    python check_identical.py                       (every data/*-chain.json)
    python check_identical.py --dates 2026-09-02 2026-09-04

Runs trinity_db.py TWICE on every NQ chain: once from the dated backups
(`trinity_db.py.bak-<date>` + `nqvol.py.bak-<date>`, copied into a scratch
folder under their real names) and once from the working files, with the same
arguments, and byte-compares trinity.json, chain_live.json and the printed
output. Any difference is a FAIL with the first differing line shown.

Why this exists: on 2026-09-06 trinity_db.py and nqvol.py learned to read
`grid`, `moneyness_cap`, `hvl_sweep` and `min_premium` from the chain file so
the OPRA equity books can use their own scale. No NQ chain carries those keys,
so NQ should be unchanged by construction -- "should" is not the standard this
project holds itself to after the 2026-09-05 audit, so this proves it. Three
argument sets per chain -- the premarket build, an intraday build with a rail
scale, and an explicit --asof -- plus a forced nqvol.solve_chain() re-solve,
because the cached chains are already on the current IV basis and would
otherwise never exercise the solver.

Run it after ANY edit to either file. It needs nothing but Python and the
cached chains; it does not touch Databento.
"""

import os, sys, json, shutil, argparse, subprocess, tempfile, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import nqvol   # only for next_session

ARGSETS = [
    ("premarket", ["--hours", "6.5"]),
    ("intraday",  ["--hours", "2.5", "--rail-scale", "0.5", "--rail-label", "test", "--last", "29400"]),
    ("asof",      ["--asof", "{asof}", "--rail-scale", "0.324", "--rail-label", "Asia"]),
]


def run(pydir, chain, report_date, extra, outdir):
    out = outdir / "trinity.json"
    args = [sys.executable, str(pydir / "trinity_db.py"), "--chain", str(chain),
            "--report-date", report_date, "--out", str(out)] + extra
    r = subprocess.run(args, cwd=str(pydir), capture_output=True, text=True, encoding="utf-8")
    txt = (r.stdout + r.stderr).replace(str(outdir), "<out>").replace(str(pydir), "<py>")
    return r.returncode, txt, out, outdir / "chain_live.json"


RESOLVE = """
import sys, json, nqvol
raw = json.load(open(sys.argv[1], encoding='utf-8'))
r = nqvol.solve_chain(raw, force=True)
json.dump(raw, open(sys.argv[2], 'w', encoding='utf-8'), sort_keys=True)
print(r)
"""


def resolve(pydir, chain, outdir):
    """nqvol.solve_chain(force=True) from the given folder: the cached chains
    are already on the current IV basis, so trinity_db never re-solves them
    and a change inside solve_chain would go untested without this."""
    out = outdir / "resolved.json"
    r = subprocess.run([sys.executable, "-c", RESOLVE, str(chain), str(out)], cwd=str(pydir),
                       capture_output=True, text=True, encoding="utf-8")
    return r.returncode, r.stdout + r.stderr, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bak", default="2026-09-06", help="date suffix of the .bak files to compare against")
    ap.add_argument("--dates", nargs="*", default=None)
    ap.add_argument("--data", default="data")
    a = ap.parse_args()

    baks = [ROOT / f"trinity_db.py.bak-{a.bak}", ROOT / f"nqvol.py.bak-{a.bak}"]
    for b in baks:
        if not b.exists():
            sys.exit(f"missing {b}")
    tmp = Path(tempfile.mkdtemp(prefix="teddy-identical-"))
    old = tmp / "old"; old.mkdir()
    shutil.copy(baks[0], old / "trinity_db.py"); shutil.copy(baks[1], old / "nqvol.py")
    # session_scale is not imported by trinity_db; nothing else is needed.
    chains = sorted((ROOT / a.data).glob("*-chain.json"))
    if a.dates:
        chains = [c for c in chains if c.name[:10] in a.dates]
    print(f"{len(chains)} chains, {len(ARGSETS)} argument sets each, old = .bak-{a.bak}\n")
    fails = skipped = ok = 0
    import time
    t0 = time.time()
    for ci, chain in enumerate(chains, 1):
        if ci > 1:
            el = time.time() - t0
            print(f"  [{ci - 1}/{len(chains)}] {ok} identical, {fails} different, {el / 60:.0f} min elapsed, "
                  f"~{el / (ci - 1) * (len(chains) - ci + 1) / 60:.0f} min left", flush=True)
        d = datetime.date.fromisoformat(chain.name[:10])
        rd = nqvol.next_session(d).isoformat()
        asof = (nqvol.close_utc(d) + datetime.timedelta(hours=4.25)).isoformat(timespec="minutes")
        # forced re-solve through nqvol.solve_chain, old vs new
        o1 = tmp / f"{chain.name[:10]}-resolve-old"; o1.mkdir()
        o2 = tmp / f"{chain.name[:10]}-resolve-new"; o2.mkdir()
        rc1, txt1, f1 = resolve(old, chain, o1)
        rc2, txt2, f2 = resolve(ROOT, chain, o2)
        if rc1 == rc2 and txt1 == txt2 and (rc1 != 0 or f1.read_bytes() == f2.read_bytes()):
            ok += 1
        else:
            fails += 1
            print(f"FAIL {chain.name} [re-solve]  rc {rc1}/{rc2}  {txt1.strip()[:80]} | {txt2.strip()[:80]}")
        for label, extra in ARGSETS:
            extra = [e.replace("{asof}", asof) for e in extra]
            o1 = tmp / f"{chain.name[:10]}-{label}-old"; o1.mkdir()
            o2 = tmp / f"{chain.name[:10]}-{label}-new"; o2.mkdir()
            rc1, txt1, t1, l1 = run(old, chain, rd, extra, o1)
            rc2, txt2, t2, l2 = run(ROOT, chain, rd, extra, o2)
            if rc1 != 0 and rc1 == rc2 and txt1 == txt2:
                skipped += 1                          # both refuse identically (empty holiday chain etc.)
                continue
            same = (rc1 == rc2 and txt1 == txt2
                    and t1.exists() and t2.exists() and t1.read_bytes() == t2.read_bytes()
                    and l1.exists() and l2.exists() and l1.read_bytes() == l2.read_bytes())
            if same:
                ok += 1
                continue
            fails += 1
            print(f"FAIL {chain.name} [{label}]  rc {rc1}/{rc2}")
            if txt1 != txt2:
                for x, y in zip(txt1.splitlines(), txt2.splitlines()):
                    if x != y:
                        print(f"   old: {x[:150]}\n   new: {y[:150]}"); break
            elif t1.exists() and t2.exists() and t1.read_bytes() != t2.read_bytes():
                j1, j2 = json.load(open(t1, encoding="utf-8")), json.load(open(t2, encoding="utf-8"))
                for k in j1:
                    if j1.get(k) != j2.get(k):
                        print(f"   trinity.json differs at '{k}'"); break
            else:
                print("   chain_live.json differs")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nidentical {ok}   refused-identically {skipped}   DIFFERENT {fails}")
    print("BIT-IDENTICAL" if not fails else "NOT IDENTICAL -- do not ship")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
