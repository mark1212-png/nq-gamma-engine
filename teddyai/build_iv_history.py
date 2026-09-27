#!/usr/bin/env python3
"""
build_iv_history.py -- the distribution regime.py ranks today's vol against.

    python build_iv_history.py                 (writes data/iv_history.json)
    python build_iv_history.py --check         (reports, writes nothing)

WHY THIS EXISTS. `regime.vol_state` refuses to call a vol level "elevated" unless
it has a distribution to say so against, because a level called elevated with no
history behind it is a heuristic wearing a number. This builds that distribution
from the cached chains that are already on disk.

WHAT IT COSTS. Nothing. It reads `data/*-chain.json` and touches no API. It calls
`trinity_db.load()` rather than `analyse()` -- the smile fit without the flip
sweep -- which is about 0.02s per session against 6s, so the whole history takes
seconds rather than an hour.

WHAT IS STORED. For each settle date, the FRONT book's at-the-money vol on the
business-time basis, valued at the next session's 06:30 PT open (hours = 6.5) --
the same moment and the same convention the premarket sheet uses, so today's
reading and the history are the same measurement.

`iv_atm` is the SMILE AT THE MONEY, not the median across strikes. HANDOFF
records why: on a steep 0DTE smile the median sits above at-the-money and
inflated both rails by about 11 points.

The chains re-solve on load if they carry an older `iv_basis`, so a history built
today is on today's convention throughout. The stamp is written into the file;
if `nqvol.IV_BASIS` is ever bumped again, rebuild rather than append.

Written 2026-09-09, Phase D of the market-state upgrade.
"""
import os
import sys
import json
import glob
import argparse
import datetime
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import nqvol
import trinity_db

OUT = ROOT / "data" / "iv_history.json"


def front_atm(chain_path, report_date, hours=6.5):
    """The front book's ATM vol for a plan describing `report_date`."""
    raw, books, S, asof, F_ref, sym = trinity_db.load(chain_path, report_date, hours_front=hours)
    zero = [b for b in books if b["dte"] == 0]
    front = zero[0] if zero else books[0]
    return {
        "iv_atm": round(front["atm"], 8),
        "front_root": front["root"],
        "front_dte": front["dte"],
        "spot": S,
        "ref_symbol": sym,
        "expiries": len(books),
    }


def build(paths):
    rows, failed = {}, []
    for p in paths:
        settle = os.path.basename(p).split("-chain")[0]
        try:
            d = datetime.date.fromisoformat(settle)
        except ValueError:
            continue
        try:
            rd = nqvol.next_session(d).isoformat()
            rec = front_atm(p, rd)
            rec["report_date"] = rd
            rows[settle] = rec
        except SystemExit as e:
            # a chain with no live expiry for the next session -- Good Friday and
            # similar. trinity_db raises SystemExit, not Exception.
            failed.append((settle, f"SystemExit: {e}"))
        except Exception as e:
            failed.append((settle, f"{type(e).__name__}: {e}"))
    return rows, failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report without writing")
    ap.add_argument("--glob", default="data/*-chain.json")
    a = ap.parse_args()

    paths = sorted(glob.glob(str(ROOT / a.glob)))
    if not paths:
        sys.exit(f"no chains matched {a.glob}")
    rows, failed = build(paths)
    if not rows:
        sys.exit("no chain produced an ATM vol")

    vals = sorted(r["iv_atm"] for r in rows.values())
    q = lambda p: vals[int(p * (len(vals) - 1))]
    print(f"  {len(rows)} sessions   {len(failed)} skipped")
    for s, e in failed[:5]:
        print(f"    skipped {s}: {e}")
    print(f"  range {min(rows)} .. {max(rows)}")
    print(f"  ATM vol  min {vals[0]:.4f}  p25 {q(.25):.4f}  median {statistics.median(vals):.4f}"
          f"  p75 {q(.75):.4f}  max {vals[-1]:.4f}")

    if a.check:
        return
    doc = {
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "iv_basis": nqvol.IV_BASIS,
        "hours": 6.5,
        "measure": "front book ATM vol from the smile, business-time basis, at the next session's RTH open",
        "n": len(rows),
        "skipped": failed,
        "sessions": rows,
    }
    OUT.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print(f"  wrote {OUT.relative_to(ROOT)}")


def load(path=None):
    """The vol series, oldest first, for regime.market_state(iv_history=...).

    Returns [] when the file is missing, so a caller degrades to "level only"
    rather than failing -- which is what vol_state is built to do.
    """
    p = Path(path or OUT)
    if not p.exists():
        return []
    doc = json.loads(p.read_text(encoding="utf-8"))
    if doc.get("iv_basis") != nqvol.IV_BASIS:
        # A history solved on a superseded convention is not comparable with
        # today's reading. Refuse it rather than ranking against the wrong ruler.
        return []
    return [r["iv_atm"] for _, r in sorted(doc.get("sessions", {}).items())]


if __name__ == "__main__":
    main()
