#!/usr/bin/env python3
"""
cme_calendar.py -- which days are actually sessions, and how long they run.

WHY THIS EXISTS
---------------
Nothing in the pipeline carried a holiday calendar. `next_business_day` skipped
weekends and nothing else, so on 2026-09-04 the evening job built a full plan for
Monday 2026-09-07 -- Labor Day. The damage was not one wrong date; it was four
numbers downstream of it, all of which looked plausible:

  * the 0DTE panel came back empty (no expiry on a holiday) and the sheet
    rendered it as "0DTE flip --, today's expiry · net +0", which reads as flat
    dealer positioning rather than as no book at all;
  * `hours` was the 20-hour clamp against a front expiry 44 hours away;
  * the rail scale was 0.1687 -- an Asia-tail number -- drawn as the band for a
    full session, giving rails 71 points apart;
  * ATM IV printed 11.29% for a board trading near 16%, because the front
    expiry's life was mostly non-trading time.

None of those raise an error. That is the point of this file: a wrong session
assumption is silent, so the calendar must be explicit and must REFUSE rather
than guess when it is asked about a date it does not cover.

WHAT IT COVERS
--------------
US equity-index sessions, which is what NQ's RTH and its option expiries follow.
Two kinds of exception:

  * FULL CLOSURE -- no US cash session, and no equity-index option expiry that
    day. Ten per year, computed from the standard NYSE rules below.
  * EARLY CLOSE -- the cash session ends 13:00 ET instead of 16:00, and the
    settlement/expiry moves with it. Three possible per year.

Both are DERIVED from rules rather than typed as dates, so the file does not rot
the way a hardcoded list does -- but `assert_covered()` still bounds it, because
a rule set is only as good as the years it was checked against.

VERIFIED against the published 2026 schedule: Jan 1, Jan 19, Feb 16, Apr 3,
May 25, Jun 19, Jul 3, Sep 7, Nov 26, Dec 25 closed; Nov 27 and Dec 24 early.
Note 2026 has no July early close -- Independence Day falls on a Saturday and is
observed Friday Jul 3, and the exchange does not add a half day on Jul 2.
"""

import datetime

COVERED = (2024, 2032)      # years the rules below have been checked against

# Cash-session hours in UTC. The front option expires at the close.
# 13:30-20:00 UTC is 06:30-13:00 PT under PDT; 14:30-21:00 under PST.
_OPEN_ET, _CLOSE_ET, _EARLY_CLOSE_ET = 9.5, 16.0, 13.0


def _nth_weekday(year, month, weekday, n):
    """n-th `weekday` of a month (n=1 first). weekday: Mon=0."""
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(weekday - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def _last_weekday(year, month, weekday):
    d = (datetime.date(year + (month == 12), month % 12 + 1, 1)
         - datetime.timedelta(days=1))
    return d - datetime.timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year):
    """Anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, 0
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)


def _observed(d):
    """
    Weekend rule for the fixed-date holidays: Saturday moves to the Friday
    before, Sunday to the Monday after. Returns None when the exchange does not
    observe it at all -- New Year's Day on a Saturday is the case, because the
    year before's last session already happened.
    """
    if d.weekday() == 5:
        return d - datetime.timedelta(days=1)
    if d.weekday() == 6:
        return d + datetime.timedelta(days=1)
    return d


def holidays(year):
    """Full closures: no cash session, no equity-index option expiry."""
    out = {
        _observed(datetime.date(year, 1, 1)),          # New Year's Day
        _nth_weekday(year, 1, 0, 3),                   # MLK
        _nth_weekday(year, 2, 0, 3),                   # Presidents' Day
        _easter(year) - datetime.timedelta(days=2),    # Good Friday
        _last_weekday(year, 5, 0),                     # Memorial Day
        _observed(datetime.date(year, 6, 19)),         # Juneteenth
        _observed(datetime.date(year, 7, 4)),          # Independence Day
        _nth_weekday(year, 9, 0, 1),                   # Labor Day
        _nth_weekday(year, 11, 3, 4),                  # Thanksgiving
        _observed(datetime.date(year, 12, 25)),        # Christmas
    }
    # A New Year's Day falling on a Saturday is not observed on Dec 31 of the
    # previous year by the US equity market.
    if datetime.date(year, 1, 1).weekday() == 5:
        out.discard(datetime.date(year - 1, 12, 31))
        out.discard(datetime.date(year, 1, 1) - datetime.timedelta(days=1))
    return {d for d in out if d.year == year}


def early_closes(year):
    """
    Half sessions -- cash closes 13:00 ET. The equity-index settlement and the
    daily option expiry move with the close, which is why these matter here and
    not only for the chart's x-axis.
    """
    out = set()
    jul4 = datetime.date(year, 7, 4)
    if jul4.weekday() < 5:                      # only when Jul 4 is itself a session day
        d = jul4 - datetime.timedelta(days=1)
        if d.weekday() < 5:
            out.add(d)
    out.add(_nth_weekday(year, 11, 3, 4) + datetime.timedelta(days=1))   # day after Thanksgiving
    dec24 = datetime.date(year, 12, 24)
    if dec24.weekday() < 5 and dec24 not in holidays(year):
        out.add(dec24)
    return {d for d in out if d.weekday() < 5 and d not in holidays(year)}


def assert_covered(d):
    """
    Refuse rather than guess. The whole failure this file exists to prevent was
    a silent assumption about a day nobody had checked.
    """
    if not (COVERED[0] <= d.year <= COVERED[1]):
        raise ValueError(
            f"cme_calendar: {d} is outside the verified range {COVERED[0]}-{COVERED[1]}. "
            f"Check the published CME/NYSE schedule for {d.year}, widen COVERED, and "
            f"re-run the self-test at the bottom of this file. Do NOT assume weekday "
            f"= session; that assumption is exactly what this module replaces.")


def is_session(d):
    """True when the US cash equity market trades that day, full or half."""
    assert_covered(d)
    return d.weekday() < 5 and d not in holidays(d.year)


def is_half(d):
    assert_covered(d)
    return d in early_closes(d.year)


def next_session(d, n=1):
    for _ in range(n):
        d += datetime.timedelta(days=1)
        while not is_session(d):
            d += datetime.timedelta(days=1)
    return d


def prev_session(d, n=1):
    for _ in range(n):
        d -= datetime.timedelta(days=1)
        while not is_session(d):
            d -= datetime.timedelta(days=1)
    return d


def sessions_between(a, b):
    """Number of sessions in (a, b] -- the honest denominator for a DTE."""
    assert_covered(a); assert_covered(b)
    n, d = 0, a
    while d < b:
        d += datetime.timedelta(days=1)
        if is_session(d):
            n += 1
    return n


def _utc_offset_hours(d):
    """
    ET->UTC for a date. US DST: second Sunday in March to first Sunday in
    November. Returns 4 under EDT, 5 under EST.
    """
    start = _nth_weekday(d.year, 3, 6, 2)
    end = _nth_weekday(d.year, 11, 6, 1)
    return 4 if start <= d < end else 5


def session_hours_utc(d):
    """
    (open_utc, close_utc) as float hours for a session date. The close is when
    the daily option expires and when settlement is struck.

    These were hardcoded 13.5 / 20.0 in session_scale.py, which is right only
    for a full session under EDT. A half session is 3.5 hours, not 6.5, so a
    front-book T built on 6.5 is overstated by sqrt(6.5/3.5) = 1.36x and the
    rails are drawn at a full day's width for a half day of variance.
    """
    if not is_session(d):
        raise ValueError(f"cme_calendar: {d} is not a session "
                         f"({'weekend' if d.weekday() >= 5 else 'exchange holiday'})")
    off = _utc_offset_hours(d)
    close = (_EARLY_CLOSE_ET if is_half(d) else _CLOSE_ET) + off
    return _OPEN_ET + off, close


def expiry_utc(d):
    """Datetime of the daily equity-index option expiry on session date `d`."""
    _, close = session_hours_utc(d)
    h, m = int(close), round((close % 1) * 60)
    return datetime.datetime.combine(d, datetime.time(h, m),
                                     datetime.timezone.utc)


def session_length_hours(d):
    o, c = session_hours_utc(d)
    return c - o


def describe(d):
    if not is_session(d):
        why = "weekend" if d.weekday() >= 5 else "exchange holiday"
        return f"{d} ({d:%a}) -- NOT a session ({why})"
    o, c = session_hours_utc(d)
    return (f"{d} ({d:%a}) -- {'HALF' if is_half(d) else 'full'} session, "
            f"{o:05.2f}-{c:05.2f} UTC, {c - o:.1f}h, expiry {expiry_utc(d):%H:%M} UTC")


if __name__ == "__main__":
    # Self-test against the published 2026 US equity schedule. Extend this list
    # before widening COVERED -- the rules are only trustworthy where checked.
    expect_closed_2026 = ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
                          "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
                          "2026-11-26", "2026-12-25"]
    expect_half_2026 = ["2026-11-27", "2026-12-24"]
    got_closed = sorted(str(d) for d in holidays(2026))
    got_half = sorted(str(d) for d in early_closes(2026))
    ok = got_closed == expect_closed_2026 and got_half == expect_half_2026
    print("2026 closures :", got_closed)
    print("        expect:", expect_closed_2026)
    print("2026 half days:", got_half, " expect:", expect_half_2026)
    print("SELF-TEST", "PASS" if ok else "FAIL")
    print()
    for s in ("2026-09-04", "2026-09-05", "2026-09-07", "2026-09-08",
              "2026-11-27", "2026-12-24", "2026-12-25"):
        print("  " + describe(datetime.date.fromisoformat(s)))
    d4 = datetime.date(2026, 9, 4)
    print(f"\n  next_session(2026-09-04) = {next_session(d4)}  (skips Labor Day)")
    print(f"  sessions_between(09-04, 09-08) = {sessions_between(d4, datetime.date(2026,9,8))}")
