#!/usr/bin/env python3
"""
make_baseline.py -- freeze the engine's current numbers as the regression baseline.

    python tests/make_baseline.py            (writes tests/fixtures/baseline.json)
    python tests/make_baseline.py --check    (compares, writes nothing)

RUN THIS DELIBERATELY, NEVER AUTOMATICALLY. The whole point of the file it
writes is that it was produced by code known to be correct. Regenerating it
after a change that moved a number would silently bless that change -- which is
exactly the failure the 2026-09-05 audit found, where the backtest carried its
own copy of the maths and validated a band the sheet did not draw.

WHAT IS STORED, AND WHY IT IS NOT A SNAPSHOT. Storing whole gamma maps would
give a diff of thousands of lines whenever one strike moves. Instead each
session stores the numbers a human reads -- spot, the expected move, the ATM
vol, each panel's flip and column total, net VEX -- plus a SHA-256 of the full
gamma map per panel. A failure then says either "the flip moved" (a number you
can reason about) or "the 0DTE gamma map changed" (a hash), and the test that
reports the hash also dumps the first differing strikes.

The sessions are chosen for coverage, not recency:
  2024-09-10  the oldest cached chain; exercises the pre-2026 holiday tables
  2025-03-12  three days after the 2025 DST change; negative gamma
  2026-09-01  negative gamma in every book, the sign the sheet gets wrong most
  2026-09-08  the most recent complete book; positive gamma in every book
"""
import sys
import json
import hashlib
import argparse
import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import nqvol
import trinity_db

OUT = Path(__file__).resolve().parent / "fixtures" / "baseline.json"

SESSIONS = ["2024-09-10", "2025-03-12", "2026-09-01", "2026-09-08"]
PANELS = ("0DTE", "NEAR", "FULL")


def gamma_hash(gmap):
    """Order-independent digest of a {strike: gamma} map."""
    return hashlib.sha256(json.dumps(gmap, sort_keys=True).encode("utf-8")).hexdigest()


def snapshot(settle):
    """The engine's own output for `settle`, reduced to what a regression needs."""
    rd = nqvol.next_session(datetime.date.fromisoformat(settle)).isoformat()
    out, books, live = trinity_db.analyse(str(ROOT / "data" / f"{settle}-chain.json"),
                                          rd, hours=6.5, quiet=True)
    rec = {
        "settle": settle,
        "report_date": rd,
        "spot": out["spot"],
        "ref_symbol": out["ref_symbol"],
        "iv_atm": out["iv_atm"],
        "em": out["em"],
        "expiries": out["expiries"],
        "no_0dte": out["no_0dte"],
        "vex_total": round(out["vex_total"], 6),
        "confluence_n": len(out["confluence"]),
        "front_root": out["front_root"],
        "front_expiry_hours": out["front_expiry_hours"],
        "panels": {},
        "by_expiry_n": len(out["by_expiry"]),
        "by_expiry_gamma_sum": sum(b["gamma"] for b in out["by_expiry"]),
        "by_expiry_abs_gamma_sum": sum(abs(b["gamma"]) for b in out["by_expiry"]),
    }
    for p in PANELS:
        pan = out["panels"][p]
        rec["panels"][p] = {
            "hvl": pan["hvl"],
            "net_total": pan["net_total"],
            "expiries": pan["expiries"],
            "n_strikes": len(pan["gamma"]),
            "gamma_sha256": gamma_hash(pan["gamma"]),
        }
    return rec


def build():
    return {
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "iv_basis": nqvol.IV_BASIS,
        "note": "Frozen production numbers. Regenerate only when a change is INTENDED to move them.",
        "sessions": {s: snapshot(s) for s in SESSIONS},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="compare against the stored baseline, write nothing")
    a = ap.parse_args()

    fresh = build()
    if not a.check:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(fresh, indent=1), encoding="utf-8")
        print(f"wrote {OUT.relative_to(ROOT)} ({len(fresh['sessions'])} sessions, iv_basis {fresh['iv_basis']})")
        for s, r in fresh["sessions"].items():
            print(f"  {s} -> {r['report_date']}  spot {r['spot']:,.2f}  "
                  f"flips {[ (r['panels'][p]['hvl'] or ['-'])[0] for p in PANELS ]}  "
                  f"nets {[ r['panels'][p]['net_total'] for p in PANELS ]}")
        return

    if not OUT.exists():
        sys.exit(f"{OUT} missing -- run without --check first")
    old = json.loads(OUT.read_text(encoding="utf-8"))
    bad = 0
    for s, new in fresh["sessions"].items():
        cur = old["sessions"].get(s)
        if cur is None:
            print(f"  {s}: NOT IN BASELINE"); bad += 1; continue
        for k, v in new.items():
            if cur.get(k) != v:
                print(f"  {s}.{k}: baseline {cur.get(k)!r} -> now {v!r}"); bad += 1
    print("MATCH" if not bad else f"{bad} DIFFERENCES")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
