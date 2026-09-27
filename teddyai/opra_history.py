#!/usr/bin/env python3
"""
opra_history.py -- the two-year history pull for the dominance test.

    python opra_history.py --estimate                      (free: samples three dates, extrapolates)
    python opra_history.py --fetch                         (resumable; Ctrl+C any time)
    python opra_history.py --fetch --start 2024-09-03 --end 2026-09-03 --roots SPX,SPXW,QQQ,SPY --nq

Build step 5b (2026-09-06). For every CME session in the window it pulls,
in this order and skipping anything already on disk:

    SPX, SPXW, QQQ, SPY   via opra_pull.fetch  -> data/opra/<date>-<root>-chain.json
    NQ                    via databento_pull.py --fetch --window final -> data/<date>-chain.json
                          (the NQ pipeline's own tool, exactly as backtest.py --pull runs it)

SPX goes first on each date because the American roots take their rate from
it. Every date's console output lands in notes/opra-history/<date>-<root>.txt
and one line per (date, root) in notes/opra-history.log, so a failure is
findable without scrolling. Re-running resumes: existing chain files are
skipped, failed ones are retried.

Size and time, measured on 2026-09-03: SPX 26 MB, SPXW 55, QQQ 117, SPY 135
per day including the open-interest legs -- ~330 MB/day, ~165 GB over ~500
sessions, plus ~25 MB/day for NQ. The account has priced everything at
$0.0000 so far and --estimate prints the extrapolated total against
--max-gb before anything moves. Wall time is dominated by the American pass
(~1-2 minutes each for QQQ and SPY): expect ~5 minutes a date, ~40 hours for
two years. It is meant to run overnight, repeatedly, until the log is clean.

What it does NOT do: build ladders or run the test -- backtest_opra.py reads
the chain files this leaves behind.
"""

import os, sys, io, json, argparse, datetime, subprocess, contextlib, traceback, time
from pathlib import Path

import nqvol
import opra_pull

ROOT = Path(__file__).resolve().parent
OPRA = ROOT / "data" / "opra"
LOGDIR = ROOT / "notes" / "opra-history"
LOG = ROOT / "notes" / "opra-history.log"
ROOT_ORDER = ["SPX", "SPXW", "QQQ", "SPY"]


def sessions(start, end):
    d, out = datetime.date.fromisoformat(start), []
    e = datetime.date.fromisoformat(end)
    while d <= e:
        if nqvol.is_session(d):
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def log(line):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")
    print(f"  {line}", flush=True)


def estimate(dates, roots, nq, max_gb):
    """FREE. Sizes three sample dates exactly and extrapolates."""
    picks = sorted({dates[0], dates[len(dates) // 2], dates[-1]})
    per_day = []
    print(f"\n  history estimate: {len(dates)} sessions {dates[0]} -> {dates[-1]}, roots {' '.join(roots)}"
          f"{' + NQ' if nq else ''}\n")
    for d in picks:
        mb = opra_pull.estimate(d, roots, quiet=True)
        print(f"    {d}: {mb:8.1f} MB for {' '.join(roots)}")
        per_day.append(mb)
    avg = sum(per_day) / len(per_day)
    nq_mb = 25.0 if nq else 0.0
    total_gb = (avg + nq_mb) * len(dates) / 1000
    print(f"\n  ~{avg:.0f} MB/day OPRA{' + ~25 MB/day NQ' if nq else ''}  ->  ~{total_gb:.0f} GB over {len(dates)} sessions"
          f"   (~{len(dates) * 5 / 60:.0f} hours of wall time at ~5 min/date)")
    print(f"  --max-gb {max_gb:.0f}: {'OK' if total_gb <= max_gb else 'EXCEEDED -- raise it or narrow the window'}\n")
    return total_gb


def have_opra(d, root):
    return (OPRA / f"{d}-{root}-chain.json").exists()


def have_nq(d):
    p = ROOT / "data" / f"{d}-chain.json"
    return p.exists() and p.stat().st_size > 500_000


def pull_root(d, root, max_mb):
    LOGDIR.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    t0 = time.time()
    try:
        with contextlib.redirect_stdout(buf):
            opra_pull.DEF_CACHE = OPRA / "defs"
            opra_pull.fetch(d, [root], str(OPRA), max_mb=max_mb)
        status = "ok" if have_opra(d, root) else "EMPTY"
    except SystemExit as ex:
        status = f"refused: {str(ex).strip().splitlines()[-1][:120]}"
    except Exception as ex:
        status = f"ERROR {type(ex).__name__}: {str(ex)[:100]}"
        buf.write(traceback.format_exc())
    (LOGDIR / f"{d}-{root}.txt").write_text(buf.getvalue(), encoding="utf-8")
    log(f"{d} {root:5} {status}   ({time.time() - t0:.0f}s)")
    return status == "ok"


def pull_nq(d):
    t0 = time.time()
    r = subprocess.run([sys.executable, str(ROOT / "databento_pull.py"), "--fetch", "--date", d,
                        "--window", "final", "--out", "data"],
                       cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
    LOGDIR.mkdir(parents=True, exist_ok=True)
    (LOGDIR / f"{d}-NQ.txt").write_text(r.stdout + r.stderr, encoding="utf-8")
    ok = r.returncode == 0 and have_nq(d)
    tail = (r.stderr or r.stdout).strip().splitlines()[-1][:120] if (r.stdout or r.stderr) else ""
    log(f"{d} NQ    {'ok' if ok else 'FAILED: ' + tail}   ({time.time() - t0:.0f}s)")
    return ok


def main():
    ap = argparse.ArgumentParser()
    today = datetime.date.today()
    ap.add_argument("--start", default=(today - datetime.timedelta(days=731)).isoformat())
    ap.add_argument("--end", default=nqvol.prev_session(today, 2).isoformat(),
                    help="last date to pull; two sessions back by default so the OI leg is complete")
    ap.add_argument("--roots", default=",".join(ROOT_ORDER))
    ap.add_argument("--nq", action="store_true", default=True, help="also pull NQ chains (default on)")
    ap.add_argument("--no-nq", dest="nq", action="store_false")
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--max-gb", type=float, default=250.0)
    ap.add_argument("--max-mb", type=float, default=200.0, help="per-root, per-date gate passed to opra_pull")
    ap.add_argument("--newest-first", action="store_true", help="pull backwards from --end (the test can start on a partial history)")
    a = ap.parse_args()
    roots = [r.strip().upper() for r in a.roots.split(",") if r.strip()]
    roots = [r for r in ROOT_ORDER if r in roots] + [r for r in roots if r not in ROOT_ORDER]
    dates = sessions(a.start, a.end)
    if not dates:
        sys.exit("no sessions in the window")
    total = estimate(dates, roots, a.nq, a.max_gb)
    if not a.fetch:
        return
    if total > a.max_gb:
        sys.exit("  estimate exceeds --max-gb; not fetching")
    if a.newest_first:
        dates = dates[::-1]
    todo = [(d, r) for d in dates for r in roots if not have_opra(d, r)]
    todo_nq = [d for d in dates if a.nq and not have_nq(d)]
    log(f"history fetch: {len(dates)} sessions, {len(todo)} root-days and {len(todo_nq)} NQ days to pull "
        f"({len(dates) * len(roots) - len(todo)} root-days already on disk)")
    done = fails = 0
    for d in dates:
        for r in roots:
            if have_opra(d, r):
                continue
            ok = pull_root(d, r, a.max_mb)
            done += ok; fails += (not ok)
        if a.nq and not have_nq(d):
            ok = pull_nq(d)
            done += ok; fails += (not ok)
    log(f"history fetch finished: {done} pulled, {fails} failed -- re-run to retry the failures")


if __name__ == "__main__":
    main()
