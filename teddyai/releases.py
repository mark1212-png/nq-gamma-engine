#!/usr/bin/env python3
"""
releases.py -- the session's scheduled high-impact USD releases, as TIME GATES on
the levels.

    import releases
    r = releases.for_report("2026-09-14", asof="2026-09-14T13:30:00+00:00")
    python releases.py --date 2026-09-14
    python releases.py --date 2026-09-14 --no-fetch          # the cached week only

TAKEN FROM NICKY (Nicky24B/nq-pma-nicky, red_folder.py, 2026-09-10), written again
on this project's clock. His rule, which this engine had no equivalent of: a level
reached in the minutes around a scheduled release is a RELEASE PRINT, not a
dealer-hedging reaction, and the plan should say so at that level before the
session rather than after. A book read without its release times is half a read.

SOURCE. The Forex Factory weekly calendar as published by faireconomy -- public
JSON, no key, THIS WEEK ONLY. Red folder == impact "High"; USD only, because NQ
trades the US tape. The fetched week is cached under data/releases/ so an
unattended build with no network reads the last copy and says how old it is. A
build inside autopull must never fail on this: every entry point returns a
document with `error` set rather than raising.

WHAT IT DOES NOT DO. It scores nothing and moves no level. It attaches times.
The 500-session node study did not condition on release windows, so whether a
touch inside one behaves differently is UNMEASURED, and the output says so.

WINDOWS are the report unit's own, from nqvol: `globex` from the reopen to the
RTH open, `rth` from the RTH open to the close, `after` past it. `in_plan` marks
a release inside the span the sheet describes, the plan's as-of to the close.
"""

import json
import datetime
import argparse
import urllib.request
from pathlib import Path

import nqvol

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "data" / "releases"
SOURCE = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
IMPACT = {"High"}
CURRENCIES = {"USD"}
GATE_MINUTES = 15           # a touch within this many minutes of a release is a release print
MAX_AGE_HOURS = 12.0        # a cached week older than this is re-fetched when fetching is allowed
UTC = datetime.timezone.utc

UNMEASURED = ("release windows were not a conditioning variable in the 500-session node study; "
              "whether a touch inside one behaves differently from one outside is unmeasured")


# --------------------------------------------------------------- fetch ---

def fetch(url=SOURCE, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 TeddyAI releases.py"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        events = json.load(r)
    return {"source": url, "fetched": datetime.datetime.now(UTC).isoformat(timespec="seconds"),
            "events": events}


def _cache_path(now=None):
    now = now or datetime.datetime.now(UTC)
    iso = now.isocalendar()
    return CACHE_DIR / f"ff-{iso[0]}-W{iso[1]:02d}.json"


def _age_hours(cal, now=None):
    try:
        t = datetime.datetime.fromisoformat(cal["fetched"].replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        return round(((now or datetime.datetime.now(UTC)) - t).total_seconds() / 3600.0, 2)
    except Exception:
        return None


def load_week(allow_fetch=True, max_age_hours=MAX_AGE_HOURS, now=None):
    """The cached week when fresh enough (or when fetching is off), else a fresh
    pull written to the cache. Never raises: returns {"error": ...} instead."""
    p = _cache_path(now)
    cached = None
    if p.exists():
        try:
            cached = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            cached = None
    if cached is not None:
        age = _age_hours(cached, now)
        if not allow_fetch or (age is not None and age <= max_age_hours):
            cached["age_hours"] = age
            cached["from_cache"] = True
            return cached
    if not allow_fetch:
        return {"error": "no cached calendar for this week and fetching is off", "events": []}
    try:
        cal = fetch()
    except Exception as e:
        if cached is not None:
            cached["age_hours"] = _age_hours(cached, now)
            cached["from_cache"] = True
            cached["fetch_error"] = f"{type(e).__name__}: {e}"
            return cached
        return {"error": f"{type(e).__name__}: {e}", "events": []}
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cal, indent=1), encoding="utf-8")
    except Exception:
        pass
    cal["age_hours"] = 0.0
    cal["from_cache"] = False
    return cal


# ---------------------------------------------------------------- read ---

def _as_utc(s):
    """An aware UTC datetime. The feed stamps each event in its own offset
    (-04:00); comparisons would survive that, a clock rendered from it would not."""
    if s is None:
        return None
    t = s if isinstance(s, datetime.datetime) else \
        datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t.astimezone(UTC) if t.tzinfo else t.replace(tzinfo=UTC)


def _et_clock(t_utc, d):
    """HH:MM Eastern for a UTC instant, on this project's DST rule (nqvol)."""
    return (t_utc + datetime.timedelta(hours=nqvol.et_offset_hours(d))).strftime("%H:%M")


def events_for(cal, report_date, asof=None, impact=IMPACT, currencies=CURRENCIES):
    """The red folder for one report unit, in time order, each event placed in
    the unit's own windows."""
    d = datetime.date.fromisoformat(str(report_date)[:10])
    open_, rth, close = nqvol.open_utc(d), nqvol.rth_open_utc(d), nqvol.close_utc(d)
    asof_t = _as_utc(asof)
    out = []
    for e in cal.get("events") or []:
        if e.get("impact") not in impact or e.get("country") not in currencies:
            continue
        try:
            t = _as_utc(e["date"])
        except Exception:
            continue
        if not (open_ <= t <= close + datetime.timedelta(hours=4)):
            continue
        window = ("globex" if t < rth else "rth" if t <= close else "after")
        out.append({
            "time_utc": t.isoformat(timespec="minutes"),
            "time_et": _et_clock(t, d),
            "title": e.get("title", ""),
            "impact": e.get("impact"),
            # Forex Factory's own field is called "forecast"; renamed on purpose. The
            # LLM subset in engine.py is forbidden a field by that name.
            "consensus": e.get("forecast") or None,
            "previous": e.get("previous") or None,
            "actual": e.get("actual") or None,
            "window": window,
            "minutes_from_rth_open": round((t - rth).total_seconds() / 60.0),
            "in_plan": bool(asof_t is not None and asof_t <= t <= close),
            "gate_minutes": GATE_MINUTES,
        })
    out.sort(key=lambda x: x["time_utc"])
    return out


def summary_line(events, report_date):
    d = datetime.date.fromisoformat(str(report_date)[:10])
    if not events:
        return f"No red folder {d:%a %d %b}: no high-impact USD release inside the report unit."
    parts = [f"{len(events)} high-impact USD release{'s' if len(events) > 1 else ''} {d:%a %d %b}: "
             + ", ".join(f"{e['title']} {e['time_et']} ET" for e in events) + "."]
    pre = [e for e in events if e["window"] == "globex" and e["minutes_from_rth_open"] >= -120]
    rth = [e for e in events if e["window"] == "rth"]
    if pre:
        parts.append(f"{len(pre)} print{'s' if len(pre) > 1 else ''} before the open: the overnight levels are "
                     f"provisional until that tape settles; a level taken on the print is a release print, "
                     f"not a hedging reaction.")
    if rth:
        parts.append(f"{len(rth)} inside RTH ({', '.join(e['time_et'] for e in rth)} ET): a touch within "
                     f"{GATE_MINUTES} minutes of it is a release print, not a node reaction.")
    return " ".join(parts)


def for_report(report_date, asof=None, allow_fetch=True, calendar=None, now=None):
    """
    The document engine.py carries. `calendar` injects a week (tests, replays);
    otherwise the cache or a fetch. Never raises.
    """
    try:
        cal = calendar if calendar is not None else load_week(allow_fetch=allow_fetch, now=now)
        evs = events_for(cal, report_date, asof=asof) if cal.get("events") else []
        doc = {
            "date": str(report_date)[:10],
            "source": cal.get("source", SOURCE),
            "fetched": cal.get("fetched"),
            "age_hours": cal.get("age_hours"),
            "from_cache": cal.get("from_cache"),
            "events": evs,
            "n_in_plan": sum(1 for e in evs if e["in_plan"]),
            "summary": summary_line(evs, report_date),
            "gate_rule": (f"a level reached within {GATE_MINUTES} minutes of a scheduled release is a "
                          f"release print, not a dealer-hedging reaction"),
            "measured": UNMEASURED,
        }
        if cal.get("error"):
            doc["error"] = cal["error"]
            doc["summary"] = (f"The release calendar could not be read ({cal['error']}). Check the "
                              f"session's releases by hand; do not read this as 'no red folder'.")
        if cal.get("fetch_error"):
            doc["fetch_error"] = cal["fetch_error"]
        return doc
    except Exception as e:
        return {"date": str(report_date)[:10], "events": [], "n_in_plan": 0,
                "error": f"{type(e).__name__}: {e}",
                "summary": "The release calendar could not be read; check the session's releases by hand.",
                "measured": UNMEASURED}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="report date (the session the plan is for)")
    ap.add_argument("--asof", default=None, help="ISO UTC moment the plan describes")
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()
    r = for_report(a.date, asof=a.asof, allow_fetch=not a.no_fetch)
    for e in r["events"]:
        print(f"{e['time_et']} ET  {e['title']:<32} {e['window']:<7} {'in plan' if e['in_plan'] else '':<8} "
              f"c={e['consensus'] or '-':<8} p={e['previous'] or '-'}")
    print(r["summary"])
    if r.get("error"):
        print("error:", r["error"])


if __name__ == "__main__":
    main()
