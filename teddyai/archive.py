#!/usr/bin/env python3
"""
archive.py -- keep every build, and a row of numbers for every build.

    python archive.py --session 2026-09-04 --kind rth

Artifacts are good for reading one plan. They are useless for asking "where has
the 0DTE flip been drifting all week", or "is IV trending down", or "did the
book ever look like this before" -- because the numbers behind each sheet live
in trinity.json, which the very next run overwrites.

So each build is copied into:

    archive/<session>/<kind>-sheet.html      the page exactly as published
    archive/<session>/<kind>-trinity.json    the numbers it was built from
    archive/<session>/<kind>-chain_live.json the per-strike inputs behind them (for replays)
    archive/history.csv                      one row per build, appended

history.csv is the point. It is a real time series -- reference, IV, expected
move, all three flips, net gamma per book, VEX -- so day-to-day drift is a
spreadsheet away, and a future backtest has something to read that is not a
directory of HTML.

Safe to run twice: re-archiving a session overwrites that session's files and
appends a fresh row, so the history keeps the rebuild rather than hiding it.
"""

import json, csv, re, shutil, argparse, datetime, pathlib

ROOT = pathlib.Path(__file__).parent.resolve()
ARCHIVE = ROOT / "archive"
HISTORY = ARCHIVE / "history.csv"

COLUMNS = ["built_at", "session", "kind", "settle", "hours", "expiries",
           "reference", "iv_atm", "em", "rail_lo", "rail_hi",
           "last_print", "oi_vintage",
           "flip_0dte", "flip_near", "flip_full",
           "net_0dte", "net_near", "net_full", "vex_total",
           "spot_vs_flips", "sheet",
           # 2026-09-05: the series was not self-describing -- a rail move could
           # not be told apart from a rail-SCALE change, and an intraday row
           # could not be placed on the clock. iv_basis marks the day-count
           # convention so pre- and post-audit rows are not compared blindly.
           "rail_scale", "rail_label", "asof", "front_expiry_hours", "iv_basis", "ref_symbol"]


def flip(t, name):
    h = ((t.get("panels") or {}).get(name) or {}).get("hvl") or []
    if not h:
        return None
    # the crossing NEAREST the reference, not the lowest-priced one -- a second
    # crossing appearing 400 points below must not read as "the flip moved"
    s = t.get("last_print") or t.get("spot") or 0
    return round(min(h, key=lambda x: abs(x - s)), 1)


def net(t, name):
    return ((t.get("panels") or {}).get(name) or {}).get("net_total")


def where(t):
    """One word for the configuration, so the CSV is scannable without maths."""
    s = t.get("last_print") or t.get("spot")   # where price IS, not the settle
    f = [v for v in (flip(t, "0DTE"), flip(t, "NEAR"), flip(t, "FULL")) if v is not None]
    if not s or not f:
        return ""
    if s > max(f):
        return "above all"
    if s < min(f):
        return "below all"
    return "inside band"


def archive(session, kind, trinity=None, sheet=None):
    t_path = pathlib.Path(trinity or ROOT / "trinity.json")
    s_path = pathlib.Path(sheet or ROOT / "trinity.html")
    if not t_path.exists():
        raise SystemExit(f"no {t_path} to archive")

    t = json.loads(t_path.read_text(encoding="utf-8"))
    day = ARCHIVE / session
    day.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(t_path, day / f"{kind}-trinity.json")
    # 2026-09-07: the live re-price's inputs go with the sheet, so a replay can re-price NQ on the
    # replayed day's own book instead of whatever chain_live.json is current (replay_teddy.ps1 reads it)
    c_path = t_path.with_name("chain_live.json")
    if c_path.exists():
        shutil.copyfile(c_path, day / f"{kind}-chain_live.json")
    sheet_name = ""
    if s_path.exists():
        shutil.copyfile(s_path, day / f"{kind}-sheet.html")
        sheet_name = f"{kind}-sheet.html"

    em = t.get("em")
    row = {
        # UTC, always. This ran on Windows (local PT) and in a Linux container
        # (UTC), so the column silently mixed two clocks and the history would
        # not sort by time.
        "built_at": datetime.datetime.now(datetime.timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session": session,
        "kind": kind,
        "settle": t.get("settle") or "",
        "hours": t.get("hours"),
        "expiries": t.get("expiries"),
        "reference": round(t["spot"], 2) if t.get("spot") else None,
        "iv_atm": round(t["iv_atm"] * 100, 2) if t.get("iv_atm") else None,
        "last_print": round(t["last_print"], 2) if t.get("last_print") else None,
        "oi_vintage": t.get("oi_vintage") or "final",
        "em": round(em, 1) if em else None,
        "rail_lo": round(t["rail_lo"], 0) if t.get("rail_lo") else None,
        "rail_hi": round(t["rail_hi"], 0) if t.get("rail_hi") else None,
        "flip_0dte": flip(t, "0DTE"),
        "flip_near": flip(t, "NEAR"),
        "flip_full": flip(t, "FULL"),
        "net_0dte": net(t, "0DTE"),
        "net_near": net(t, "NEAR"),
        "net_full": net(t, "FULL"),
        "vex_total": round(t["vex_total"]) if t.get("vex_total") is not None else None,
        "spot_vs_flips": where(t),
        "sheet": sheet_name,
        "rail_scale": t.get("rail_scale"),
        "rail_label": t.get("rail_label") or "",
        "asof": t.get("asof") or "",
        "front_expiry_hours": t.get("front_expiry_hours"),
        "iv_basis": t.get("iv_basis") or "calendar",
        "ref_symbol": t.get("ref_symbol") or "",
    }

    # Appending under a stale header writes every value into the wrong column
    # and the damage is silent, so migrate the file when COLUMNS has grown.
    fresh = not HISTORY.exists() or HISTORY.stat().st_size == 0   # an empty file has no header either
    if not fresh:
        with open(HISTORY, newline="", encoding="utf-8") as f:
            old = list(csv.DictReader(f))
        have = list(old[0].keys()) if old else COLUMNS
        if have != COLUMNS:
            with open(HISTORY, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=COLUMNS)
                w.writeheader()
                for r in old:
                    w.writerow({c: r.get(c, "") for c in COLUMNS})
            print(f"  history.csv migrated to {len(COLUMNS)} columns "
                  f"({len(old)} rows preserved)")
    with open(HISTORY, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if fresh:
            w.writeheader()
        w.writerow(row)

    print(f"  archived {session}/{kind}: ref {row['reference']}, "
          f"flips {row['flip_0dte']} / {row['flip_near']} / {row['flip_full']}, "
          f"{row['spot_vs_flips']}")
    print(f"  -> {day}  and a row in {HISTORY.name}")
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True, help="the session the plan is FOR")
    # "rth", "asia", "europe", "manual", or "intraday-HHMM" -- the mid-session
    # builds are timestamped because there can be a dozen in a day and the point
    # of them is the comparison, so a fixed name would overwrite the evidence.
    ap.add_argument("--kind", default="rth",
                    type=lambda s: s if re.fullmatch(r"[a-z]+(-\d{4})?", s)
                    else ap.error(f"odd --kind {s!r}"))
    ap.add_argument("--trinity", default=None)
    ap.add_argument("--sheet", default=None)
    archive(**vars(ap.parse_args()))


if __name__ == "__main__":
    main()
