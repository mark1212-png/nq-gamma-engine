# -*- coding: utf-8 -*-
"""
Do NQ weekend gaps actually fill, or is "gap fill" just distance?

Method mirrors the node study: every claim is scored against a DISTANCE-MATCHED
control, because the naive number ("gaps fill 80% of the time!") is mostly a
statement about how far price travels in a week, not about gaps.

  gap      = reopen_first_minute_CLOSE - friday_last_CLOSE
  FILL     = price trades back to friday_last_close   (distance |gap| AGAINST the gap)
  MIRROR   = price trades to reopen + gap             (distance |gap| WITH the gap)

Both are the same distance from the same starting point, measured over the same
window. If gaps "want" to fill, FILL must beat MIRROR. If they match, the fill
rate is geometry and the folklore is empty.

First-minute CLOSES, not opens: the 2026-09-13 reopen printed an NQU6 open that
implied a 383-point calendar basis against NQZ6 versus ~296 on every bar after
it -- a stub print at a thin reopen. Closes agree between contracts; opens do not.
"""
import datetime, json, pathlib, statistics, sys

SRC = pathlib.Path(__file__).parent / "nq-1m-2021-2026.json"
DEGRADED = {"2021-12-05", "2022-01-02", "2024-09-18"}   # Databento condition flags
WEEKEND_BREAK_H = 12.0          # a session break this long is a weekend/holiday
MIN_GAP_PTS = 5.0               # below this the "gap" is a tick of noise


def parse(ts):
    return datetime.datetime.fromisoformat(str(ts).replace(" ", "T"))


def third_friday(d):
    """NQ.c.0 is CALENDAR-roll: it steps to the next quarter at expiry, which
    would otherwise read as a several-hundred-point weekend gap that is pure
    bookkeeping. Drop the weekends adjacent to quarterly expiry."""
    if d.month not in (3, 6, 9, 12):
        return False
    fridays = [x for x in range(1, 32)
               if x <= 31 and _valid(d.year, d.month, x) and datetime.date(d.year, d.month, x).weekday() == 4]
    return len(fridays) >= 3 and d.day in (fridays[2], fridays[2] + 1, fridays[2] - 1)


def _valid(y, m, day):
    try:
        datetime.date(y, m, day); return True
    except ValueError:
        return False


def main():
    rows = json.loads(SRC.read_text())
    print(f"loaded {len(rows):,} bars")
    ts = [parse(r["ts"]) for r in rows]

    # ---- daily true range, for normalising gap size ----------------------
    days = {}
    for r, t in zip(rows, ts):
        d = t.date()
        v = days.setdefault(d, [r["h"], r["l"], r["c"]])
        v[0] = max(v[0], r["h"]); v[1] = min(v[1], r["l"]); v[2] = r["c"]
    dkeys = sorted(days)
    atr = {}
    for i, d in enumerate(dkeys):
        w = dkeys[max(0, i - 10):i]
        atr[d] = statistics.mean([days[x][0] - days[x][1] for x in w]) if len(w) >= 5 else None

    # ---- find weekend breaks ---------------------------------------------
    events = []
    for i in range(1, len(rows)):
        dt = (ts[i] - ts[i - 1]).total_seconds() / 3600.0
        if dt < WEEKEND_BREAK_H:
            continue
        fri, sun = rows[i - 1], rows[i]
        fd, sd = ts[i - 1].date(), ts[i].date()
        if str(fd) in DEGRADED or str(sd) in DEGRADED:
            continue
        if third_friday(fd):
            continue                       # quarterly roll, not a gap
        a = atr.get(fd)
        if not a:
            continue
        gap = sun["c"] - fri["c"]
        if abs(gap) < MIN_GAP_PTS:
            continue
        events.append(dict(i=i, fri_date=fd, sun_date=sd, fri_close=fri["c"],
                           reopen=sun["c"], gap=gap, atr=a, gap_atr=abs(gap) / a))

    print(f"{len(events)} weekend gaps >= {MIN_GAP_PTS:g} pts "
          f"(quarterly-roll and degraded weekends excluded)\n")

    # ---- score each against its mirror -----------------------------------
    def scan(start_i, level, up, stop_at):
        """did price reach `level` before stop_at? returns hours, or None"""
        for j in range(start_i, len(rows)):
            if ts[j] > stop_at:
                return None
            if (rows[j]["h"] >= level) if up else (rows[j]["l"] <= level):
                return (ts[j] - ts[start_i]).total_seconds() / 3600.0
        return None

    for label, hours in (("first session (~24h)", 24), ("first 3 days", 72), ("full week", 120)):
        f_hit = m_hit = 0
        f_times = []
        for e in events:
            t0 = ts[e["i"]]
            stop = t0 + datetime.timedelta(hours=hours)
            fill_level = e["fri_close"]
            mirror_level = e["reopen"] + e["gap"]
            up_fill = e["gap"] < 0          # a gap DOWN fills by going UP
            tf = scan(e["i"], fill_level, up_fill, stop)
            tm = scan(e["i"], mirror_level, not up_fill, stop)
            if tf is not None:
                f_hit += 1; f_times.append(tf)
            if tm is not None:
                m_hit += 1
        n = len(events)
        edge = (f_hit - m_hit) / n * 100
        med = statistics.median(f_times) if f_times else float("nan")
        print(f"{label:22}  FILL {f_hit:3}/{n} = {f_hit/n*100:5.1f}%   "
              f"MIRROR {m_hit:3}/{n} = {m_hit/n*100:5.1f}%   edge {edge:+5.1f}pp   "
              f"median time to fill {med:5.1f}h")

    # ---- by gap size, on the full-week horizon ---------------------------
    print("\nby gap size (full week horizon), gap measured in daily ATRs:")
    buckets = [(0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 99)]
    for lo, hi in buckets:
        sub = [e for e in events if lo <= e["gap_atr"] < hi]
        if not sub:
            continue
        f = m = 0
        for e in sub:
            t0 = ts[e["i"]]; stop = t0 + datetime.timedelta(hours=120)
            up_fill = e["gap"] < 0
            if scan(e["i"], e["fri_close"], up_fill, stop) is not None: f += 1
            if scan(e["i"], e["reopen"] + e["gap"], not up_fill, stop) is not None: m += 1
        n = len(sub)
        pts = statistics.median([abs(e["gap"]) for e in sub])
        print(f"  {lo:.1f}-{hi:.1f} ATR  n={n:3}  median {pts:6.1f} pts   "
              f"FILL {f/n*100:5.1f}%   MIRROR {m/n*100:5.1f}%   edge {(f-m)/n*100:+5.1f}pp")

    # ---- direction split --------------------------------------------------
    print("\nby direction (full week):")
    for name, sel in (("gap DOWN", lambda e: e["gap"] < 0), ("gap UP", lambda e: e["gap"] > 0)):
        sub = [e for e in events if sel(e)]
        f = m = 0
        for e in sub:
            t0 = ts[e["i"]]; stop = t0 + datetime.timedelta(hours=120)
            up_fill = e["gap"] < 0
            if scan(e["i"], e["fri_close"], up_fill, stop) is not None: f += 1
            if scan(e["i"], e["reopen"] + e["gap"], not up_fill, stop) is not None: m += 1
        n = len(sub)
        print(f"  {name:9} n={n:3}   FILL {f/n*100:5.1f}%   MIRROR {m/n*100:5.1f}%   edge {(f-m)/n*100:+5.1f}pp")

    # ---- where tonight's sits --------------------------------------------
    cur = 300.0
    cur_atr = cur / statistics.mean([days[d][0] - days[d][1] for d in dkeys[-11:-1]])
    print(f"\n2026-09-13 gap: 300.0 pts = {cur_atr:.2f} daily ATRs -> the "
          f"{[f'{lo:.1f}-{hi:.1f}' for lo,hi in buckets if lo <= cur_atr < hi][0]} bucket")


if __name__ == "__main__":
    main()
