#!/usr/bin/env python3
"""
nqvol.py -- the ONE implementation of time, volatility and Greeks for TeddyAI.

    databento_pull.py, trinity_db.py, backtest.py and autopull.py all import
    from here. Before 2026-09-05 each carried its own copy of Black-76, its own
    smile, its own idea of what T was -- and the 2026-09-05 audit found the
    copies had drifted: the backtest validated a different band from the one
    the sheet draws, and the sheet mixed a calendar-day vol with a trading-day
    expected move. One file, one convention.

THE CONVENTION: BUSINESS TIME, VARIANCE-WEIGHTED.

    Time is counted in SESSIONS, one unit per CME equity-index trading day,
    settle to settle. Weekends and holidays are zero. Inside a session the
    clock runs at the pace variance is actually realised -- the shares the
    project measured on 64 sessions (post-close 6.4%, Asia 18.9%, Europe
    20.1%, RTH 54.6%, and the 15-minute CUM table inside RTH from
    session_scale.py). One year is 252 sessions.

    Why: an option's price fixes its TOTAL remaining variance. Spreading that
    over calendar hours puts 27% of the last day's variance in RTH; the tape
    puts 55% there. Calendar time therefore overstated 0DTE gamma at the open
    by ~1.4x and, because the same calendar vol was then divided by sqrt(252),
    drew the 1-day rails 20% too wide midweek and ~20% too narrow across a
    weekend. On the 2026-09-07 (Labor Day) build the header read IV 11.29% on
    a board priced around 16% -- the entire "term structure" on that sheet was
    the long weekend.

    Everything that needs a T calls tau() or years_between(); everything that
    needs a vol calls solve_chain() or implied_vol(); everything that needs a
    Greek calls greeks(). The expected move for one session is S * iv / sqrt(252)
    and is now CONSISTENT with the vol it is computed from.

Written 2026-09-05 from the audit; see HANDOFF.md "Business time".
"""

import bisect, datetime, math
from math import log, sqrt, exp, pi, erf

UTC = datetime.timezone.utc
SESSIONS_PER_YEAR = 252.0

# ----------------------------------------------------------------- calendar --
# CME equity-index FULL closures: no RTH, no settlement, no OI file, no listed
# expiry. Days the cash market is closed but Globex trades a shortened session
# (MLK, Presidents, Memorial, Juneteenth, Labor) are in here too: nothing
# expires on them, no OI is published for them, and the pipeline treats them as
# non-sessions. Half-days (13:00 ET close) are separate and ARE sessions.
# Extend a year ahead every December; is_session() raises past the last year
# listed rather than silently treating an unlisted year as holiday-free.
# 2013-2025 (added 2026-09-06 for the OPRA history): generated from the NYSE
# rules in cme_calendar.py -- New Year (Saturday not observed), MLK,
# Presidents, Good Friday, Memorial, Juneteenth FROM 2022 ONLY, Independence
# observed, Labor, Thanksgiving, Christmas observed -- and checked against the
# published record where it is not a rule: 2022-01-01 fell on a Saturday and
# was not observed; 2016 and 2022 Christmas were observed on the 26th; the
# July-3 half days fell in 2013, 2014, 2017, 2018, 2019, 2023, 2024, 2025.
# Two SPECIAL closures the rules cannot know are listed and are Globex-only
# days here (weight 0.40) pending Mark's check of CME's hours on them:
# 2018-12-05 (national day of mourning, Bush) and 2025-01-09 (Carter).
# Good Fridays that carried a jobs report (2015-04-03, 2021-04-02) had an
# abbreviated Globex session at CME; they stay full closures here, as
# 2026-04-03 does -- one unit of time on a day with a 45-minute tape.
HOLIDAYS = {
    2013: {"01-01", "01-21", "02-18", "03-29", "05-27", "07-04", "09-02", "11-28", "12-25"},
    2014: {"01-01", "01-20", "02-17", "04-18", "05-26", "07-04", "09-01", "11-27", "12-25"},
    2015: {"01-01", "01-19", "02-16", "04-03", "05-25", "07-03", "09-07", "11-26", "12-25"},
    2016: {"01-01", "01-18", "02-15", "03-25", "05-30", "07-04", "09-05", "11-24", "12-26"},
    2017: {"01-02", "01-16", "02-20", "04-14", "05-29", "07-04", "09-04", "11-23", "12-25"},
    2018: {"01-01", "01-15", "02-19", "03-30", "05-28", "07-04", "09-03", "11-22", "12-05", "12-25"},
    2019: {"01-01", "01-21", "02-18", "04-19", "05-27", "07-04", "09-02", "11-28", "12-25"},
    2020: {"01-01", "01-20", "02-17", "04-10", "05-25", "07-03", "09-07", "11-26", "12-25"},
    2021: {"01-01", "01-18", "02-15", "04-02", "05-31", "07-05", "09-06", "11-25", "12-24"},
    2022: {"01-17", "02-21", "04-15", "05-30", "06-20", "07-04", "09-05", "11-24", "12-26"},
    2023: {"01-02", "01-16", "02-20", "04-07", "05-29", "06-19", "07-04", "09-04", "11-23", "12-25"},
    2024: {"01-01", "01-15", "02-19", "03-29", "05-27", "06-19", "07-04", "09-02", "11-28", "12-25"},
    2025: {"01-01", "01-09", "01-20", "02-17", "04-18", "05-26", "06-19", "07-04", "09-01", "11-27", "12-25"},
    2026: {"01-01", "01-19", "02-16", "04-03", "05-25", "06-19", "07-03",
           "09-07", "11-26", "12-25"},
    2027: {"01-01", "01-18", "02-15", "03-26", "05-31", "06-18", "07-05",
           "09-06", "11-25", "12-24"},
}
HALF_DAYS = {
    2013: {"07-03", "11-29", "12-24"},
    2014: {"07-03", "11-28", "12-24"},
    2015: {"11-27", "12-24"},
    2016: {"11-25"},
    2017: {"07-03", "11-24"},
    2018: {"07-03", "11-23", "12-24"},
    2019: {"07-03", "11-29", "12-24"},
    2020: {"11-27", "12-24"},
    2021: {"11-26"},
    2022: {"11-25"},
    2023: {"07-03", "11-24"},
    2024: {"07-03", "11-29", "12-24"},
    2025: {"07-03", "11-28", "12-24"},
    2026: {"11-27", "12-24"},
    2027: {"11-26", "12-23"},          # 2027-12-24 is the observed Christmas closure
}
# Holidays on which GLOBEX STILL TRADES the equity indexes -- Sunday 17:00 CT
# reopen, halt at 12:00 CT, no settlement, no expiry -- as opposed to the full
# closures (New Year, Good Friday, Christmas). They are not sessions, but they
# are not zero time either: Labor Day 2026 is 19 hours of NQ tape. Each carries
# a variance weight against a normal session; 0.40 is the overnight share
# (0.253 to the Asia close, ~0.35 to the European morning) plus a thin US
# morning. Measure it when there are enough of them in the bar cache.
GLOBEX_HOLIDAYS = {
    2013: {"01-21", "02-18", "05-27", "07-04", "09-02", "11-28"},
    2014: {"01-20", "02-17", "05-26", "07-04", "09-01", "11-27"},
    2015: {"01-19", "02-16", "05-25", "07-03", "09-07", "11-26"},
    2016: {"01-18", "02-15", "05-30", "07-04", "09-05", "11-24"},
    2017: {"01-16", "02-20", "05-29", "07-04", "09-04", "11-23"},
    2018: {"01-15", "02-19", "05-28", "07-04", "09-03", "11-22", "12-05"},
    2019: {"01-21", "02-18", "05-27", "07-04", "09-02", "11-28"},
    2020: {"01-20", "02-17", "05-25", "07-03", "09-07", "11-26"},
    2021: {"01-18", "02-15", "05-31", "07-05", "09-06", "11-25"},
    2022: {"01-17", "02-21", "05-30", "06-20", "07-04", "09-05", "11-24"},
    2023: {"01-16", "02-20", "05-29", "06-19", "07-04", "09-04", "11-23"},
    2024: {"01-15", "02-19", "05-27", "06-19", "07-04", "09-02", "11-28"},
    2025: {"01-09", "01-20", "02-17", "05-26", "06-19", "07-04", "09-01", "11-27"},
    2026: {"01-19", "02-16", "05-25", "06-19", "07-03", "09-07", "11-26"},
    2027: {"01-18", "02-15", "05-31", "06-18", "07-05", "09-06", "11-25"},
}
GLOBEX_HOLIDAY_WEIGHT = 0.40
GLOBEX_HOLIDAY_HALT_ET = 13            # 12:00 CT
_LAST_YEAR = max(HOLIDAYS)
_FIRST_YEAR = min(HOLIDAYS)


def _key(d):
    return f"{d.month:02d}-{d.day:02d}"


def is_session(d):
    """True when CME equity-index products settle on date d."""
    if d.weekday() >= 5:
        return False
    if d.year > _LAST_YEAR or d.year < _FIRST_YEAR:
        # Beyond the listed years only the fixed-date closures are known. A
        # one-day error on a 3-year quarterly is immaterial; a raise here would
        # stop every chain that lists NQZ9. Extend HOLIDAYS each December.
        # (Before the first listed year the same fallback applies -- a history
        # pull that reaches past 2013 must extend the tables first.)
        return _key(d) not in {"01-01", "07-04", "12-25"}
    return _key(d) not in HOLIDAYS.get(d.year, set())


def is_half_day(d):
    return _key(d) in HALF_DAYS.get(d.year, set())


def is_globex_holiday(d):
    """A holiday with a Globex-only session (reopen Sunday/eve, halt 12:00 CT)."""
    return d.weekday() < 5 and _key(d) in GLOBEX_HOLIDAYS.get(d.year, set())


def unit_weight(d):
    """Business-time weight of date d: 1 for a session, the holiday weight for a
    Globex-only holiday, 0 for a weekend or full closure."""
    if is_session(d):
        return 1.0
    if is_globex_holiday(d):
        return GLOBEX_HOLIDAY_WEIGHT
    return 0.0


def next_session(d, n=1):
    """The n-th session strictly after date d."""
    while n:
        d += datetime.timedelta(days=1)
        if is_session(d):
            n -= 1
    return d


def prev_session(d, n=1):
    while n:
        d -= datetime.timedelta(days=1)
        if is_session(d):
            n -= 1
    return d


def sessions_between(d0, d1):
    """Number of sessions in (d0, d1] -- settles strictly after d0 up to and including d1."""
    n, d = 0, d0
    while d < d1:
        d += datetime.timedelta(days=1)
        if is_session(d):
            n += 1
    return n


def _dst_start(y):
    d = datetime.date(y, 3, 8)                      # second Sunday of March
    return d + datetime.timedelta(days=(6 - d.weekday()) % 7)


def _dst_end(y):
    d = datetime.date(y, 11, 1)                     # first Sunday of November
    return d + datetime.timedelta(days=(6 - d.weekday()) % 7)


def et_offset_hours(d):
    """US Eastern offset from UTC on date d: -4 (EDT) or -5 (EST)."""
    return -4 if _dst_start(d.year) <= d < _dst_end(d.year) else -5


def et(d, hour, minute=0):
    """A wall-clock Eastern time on date d, as an aware UTC datetime."""
    return (datetime.datetime(d.year, d.month, d.day, hour, minute, tzinfo=UTC)
            - datetime.timedelta(hours=et_offset_hours(d)))


def close_utc(d):
    """The settlement / PM-expiry instant for session d: 16:00 ET, 13:00 ET on a
    half-day. For a Globex-only holiday, the 12:00 CT halt."""
    if is_globex_holiday(d):
        return et(d, GLOBEX_HOLIDAY_HALT_ET)
    return et(d, 13 if is_half_day(d) else 16)


def open_utc(d):
    """Globex open for session d: 18:00 ET on the previous calendar day."""
    return et(d - datetime.timedelta(days=1), 18)


def rth_open_utc(d):
    return et(d, 9, 30)


# --------------------------------------------------------- variance clock --
# Cumulative share of a session's variance realised by each boundary, in ET.
# Overnight shares are the project's (teddy.py); the RTH profile is the
# 15-minute table measured in session_scale.py. Both are 2026 summer numbers.
_POST_CLOSE = 0.064          # 18:00 -> 20:00 ET (the Globex reopen)
_ASIA = 0.189                # 20:00 -> 03:00 ET
_EUROPE = 0.201              # 03:00 -> 09:30 ET
_RTH = 0.546                 # 09:30 -> 16:00 ET
_RTH_CUM = [0.1411, 0.2523, 0.3278, 0.4044, 0.4707, 0.5251, 0.5681, 0.6032,
            0.6353, 0.6682, 0.6932, 0.7158, 0.7424, 0.7636, 0.7840, 0.8042,
            0.8206, 0.8357, 0.8573, 0.8710, 0.8879, 0.9039, 0.9251, 0.9431,
            0.9628, 1.0000]                        # by +0.25h ... +6.25h, then 1.0 at 6.5h


def rth_cum(frac):
    """Share of RTH variance spent at `frac` in [0,1] of the RTH clock."""
    if frac <= 0:
        return 0.0
    if frac >= 1:
        return 1.0
    x = frac * 26.0                                 # 26 quarter-hours in 6.5h
    i = int(x)
    lo = _RTH_CUM[i - 1] if i else 0.0
    hi = _RTH_CUM[i] if i < len(_RTH_CUM) else 1.0
    return lo + (hi - lo) * (x - i)


def session_frac(dt, d):
    """Variance share of session d elapsed at aware datetime dt, in [0, 1]."""
    o, c = open_utc(d), close_utc(d)
    if dt <= o:
        return 0.0
    if dt >= c:
        return 1.0
    if is_globex_holiday(d):                        # thin, no RTH: clock-linear
        return (dt - o) / (c - o)
    b1 = et(d - datetime.timedelta(days=1), 20)     # 20:00 ET prev day
    b2 = et(d, 3)                                   # 03:00 ET
    b3 = rth_open_utc(d)
    if dt <= b1:
        return _POST_CLOSE * (dt - o) / (b1 - o)
    if dt <= b2:
        return _POST_CLOSE + _ASIA * (dt - b1) / (b2 - b1)
    if dt <= b3:
        return _POST_CLOSE + _ASIA + _EUROPE * (dt - b2) / (b3 - b2)
    return _POST_CLOSE + _ASIA + _EUROPE + _RTH * rth_cum((dt - b3) / (c - b3))


_EPOCH = datetime.date(2025, 12, 31)
_INDEX = {}


def _session_index(d):
    """Cumulative business-time weight of every unit (session or Globex
    holiday) from the epoch to date d inclusive (memoised). Before the epoch
    the index runs NEGATIVE -- the weight of every unit in (d, epoch] taken
    off zero -- so that index(d2) - index(d1) is the business time between
    them on either side of the epoch, and every post-epoch value is computed
    exactly as it was before 2026-09-06 (the epoch did not move; a moved
    epoch would have changed the float sums the 0.40 weights sit in)."""
    if d not in _INDEX:
        n, x = 0.0, _EPOCH
        step = datetime.timedelta(days=1)
        if d < _EPOCH:
            while x > d:
                n -= unit_weight(x)
                x -= step
        else:
            while x < d:
                x += step
                n += unit_weight(x)
        _INDEX[d] = n
    return _INDEX[d]


def session_of(dt):
    """The unit (session or Globex holiday) whose (open, close] holds dt; None in a gap."""
    d = dt.date()
    for cand in (d, d + datetime.timedelta(days=1)):
        if unit_weight(cand) and open_utc(cand) < dt <= close_utc(cand):
            return cand
    return None


def tau(dt):
    """
    Continuous business time at aware datetime dt, in sessions from the epoch.
    Integer at every settle; flat across the halt, the weekend and full
    closures; advancing at the holiday weight through a Globex-only holiday.
    """
    if dt.tzinfo is None:
        raise ValueError("tau() needs an aware datetime")
    d = session_of(dt)
    if d is not None:
        w = unit_weight(d)
        return _session_index(d) - w + w * session_frac(dt, d)
    # in a gap: the last completed unit's close
    x = dt.date()
    while not (unit_weight(x) and close_utc(x) <= dt):
        x -= datetime.timedelta(days=1)
    return float(_session_index(x))


def years_between(t0, t1):
    """Business-time year fraction from t0 to t1 (aware datetimes). Never negative."""
    return max(tau(t1) - tau(t0), 0.0) / SESSIONS_PER_YEAR


def hours_to(t0, t1):
    return (t1 - t0).total_seconds() / 3600.0


# ------------------------------------------------------------------ pricing --
_ph = lambda x: exp(-0.5 * x * x) / sqrt(2 * pi)
_N = lambda x: 0.5 * (1.0 + erf(x / sqrt(2.0)))


def bs76_price(F, K, sigma, T, is_call, r=0.0):
    if not all(math.isfinite(x) for x in (F, K, sigma, T, r)) or F <= 0 or K <= 0 or sigma < 0:
        raise ValueError("Black-76 requires finite inputs, positive F/K and nonnegative volatility")
    if T <= 0 or sigma == 0:
        return exp(-r * max(T, 0.0)) * max(0.0, (F - K) if is_call else (K - F))
    d1 = (log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    disc = exp(-r * T)
    return disc * (F * _N(d1) - K * _N(d2)) if is_call else disc * (K * _N(-d2) - F * _N(-d1))


def implied_vol(price, F, K, T, is_call, lo=1e-4, hi=5.0, n=80):
    """Bisection; price is monotone in sigma so the root is unique."""
    if not all(math.isfinite(x) for x in (price, F, K, T, lo, hi)) or F <= 0 or K <= 0 or not 0 < lo < hi:
        return None
    if T <= 0 or price <= max(0.0, (F - K) if is_call else (K - F)) + 1e-9:
        return None
    if bs76_price(F, K, hi, T, is_call) < price:
        return None
    for _ in range(n):
        mid = 0.5 * (lo + hi)
        if bs76_price(F, K, mid, T, is_call) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def greeks(F, K, sig, T):
    """(gamma per point of F, vanna = dDelta/dVol per 1.00 of vol) under Black-76."""
    if T <= 0 or sig <= 0:
        return 0.0, 0.0
    st = sig * sqrt(T)
    d1 = (log(F / K) + 0.5 * sig * sig * T) / st
    d2 = d1 - st
    return _ph(d1) / (F * st), -_ph(d1) * d2 / sig


def charm(F, K, sig, T):
    """dDelta/dt per YEAR of time passing, under Black-76 with no carry.

    The time derivative of N(d1), with d1 built from log(F/K):

        dDelta/dT = -phi(d1) * d2 / (2T)        T = time to expiry, years
        charm     = -dDelta/dT = phi(d1) * d2 / (2T)

    An out-of-the-money call (d2 < 0) therefore has NEGATIVE charm -- its delta
    decays toward zero as expiry approaches -- and an in-the-money call has
    positive charm, its delta climbing toward one. With no carry a put's delta
    is the call's minus one, so their time derivatives agree and ONE charm
    serves a strike's net call-minus-put open interest, the same identity
    strike_metrics relies on for gamma.

    Per year because T is in years. trinity_db.strike_charm divides by
    SESSIONS_PER_YEAR to report per session, the project's unit of time.
    A sibling of greeks() rather than a third return value: a dozen callers
    unpack that pair. Added 2026-09-12 when charm entered the node score by
    decision; nodescore.py carries the measured caveat that goes with it.
    """
    if T <= 0 or sig <= 0:
        return 0.0
    st = sig * sqrt(T)
    d1 = (log(F / K) + 0.5 * sig * sig * T) / st
    d2 = d1 - st
    return _ph(d1) * d2 / (2.0 * T)


def expected_move(S, iv, sessions=1.0):
    """Expected move over `sessions` trading sessions from a business-time vol."""
    return S * iv * sqrt(sessions / SESSIONS_PER_YEAR)


# -------------------------------------------------------------------- smile --
class Smile:
    """
    Per-expiry vol curve: linear in log-moneyness between solved points.

    Beyond the last solved point on each side the curve EXTENDS THE WING SLOPE
    from the outermost two points (capped between 0.5x and 2x the ATM level)
    rather than going flat. Flat wings understated put-side vol -- and therefore
    downside gamma, which is where the stops sit -- by construction, because the
    premium floor drops exactly the strikes that carry the skew. Points at the
    same strike (a call and a put) are averaged before the fit; the old code fed
    both in and the interpolator silently took the lower.
    """

    def __init__(self, pts, fallback):
        agg = {}
        for m, v in pts:
            agg.setdefault(round(m, 9), []).append(v)
        xs = sorted(agg)
        self.x = xs
        self.y = [sum(agg[m]) / len(agg[m]) for m in xs]
        self.fallback = fallback
        self.atm = self._interp(0.0) if xs else fallback
        k = max(2, min(5, len(xs) // 4))
        self._lo_slope = self._slope(list(range(k))) if len(xs) >= 2 else 0.0
        self._hi_slope = self._slope(list(range(len(xs) - k, len(xs)))) if len(xs) >= 2 else 0.0

    def _slope(self, idx):
        """Least-squares slope over the outermost knots -- two adjacent wing
        points are one tick of premium apart and their slope is noise."""
        xs = [self.x[i] for i in idx]
        ys = [self.y[i] for i in idx]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx > 0 else 0.0

    def _interp(self, m):
        if m <= self.x[0]:
            return self.y[0]
        if m >= self.x[-1]:
            return self.y[-1]
        i = bisect.bisect_left(self.x, m)
        x0, x1, y0, y1 = self.x[i - 1], self.x[i], self.y[i - 1], self.y[i]
        return y0 + (y1 - y0) * (m - x0) / (x1 - x0) if x1 > x0 else y0

    def __call__(self, m):
        if not self.x:
            return self.fallback
        if m < self.x[0]:
            v = self.y[0] + self._lo_slope * (m - self.x[0])
        elif m > self.x[-1]:
            v = self.y[-1] + self._hi_slope * (m - self.x[-1])
        else:
            return self._interp(m)
        return min(max(v, 0.5 * self.atm), 2.0 * self.atm)

    def knots(self):
        """[[log-moneyness, iv], ...] -- enough to rebuild the curve elsewhere."""
        return [[round(m, 6), round(v, 6)] for m, v in zip(self.x, self.y)]

    @classmethod
    def from_knots(cls, knots, fallback):
        return cls([(m, v) for m, v in knots], fallback)


# ---------------------------------------------------------------- the chain --
MIN_PREMIUM = 1.0            # index points of extrinsic; below this the inversion is noise
IV_BASIS = "sessions.2"      # stamped into the chain file; bump when the calendar or the clock changes so old files re-solve


def expiry_dt(rec_expiration):
    """The definition's expiration timestamp -> aware UTC datetime. CME's
    definitions carry the real instant: 20:00 UTC for PM weeklies in summer,
    21:00 in winter, 13:30/14:30 for the AM-settled quarterlies."""
    s = str(rec_expiration).replace("Z", "+00:00")
    dt = datetime.datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def book_expiry(strikes):
    """One expiry instant for a book, read from its contracts."""
    for r in strikes.values():
        for c in r:
            e = r[c].get("expiration")
            if e and str(e) != "NaT":
                return expiry_dt(e)
    return None


def solve_chain(raw, settle_date=None, force=False):
    """
    Solve (or re-solve) the implied vol of every settled contract in a chain
    file IN PLACE, on the business-time convention. Returns (graded, settled).

    Idempotent: a chain already stamped `iv_basis == "sessions"` is left alone
    unless force=True. Chains written before 2026-09-05 carry calendar-time
    vols; loading one through here re-bases it from the settlement prices it
    already holds, so nothing needs to be re-pulled.
    """
    if raw.get("iv_basis") == IV_BASIS and not force:
        return None
    fut, chain = raw["futures_settle"], raw["chain"]
    sd = datetime.date.fromisoformat(settle_date or raw["date"])
    ref = close_utc(sd)
    # The premium floor is in index points, so it is a property of the
    # instrument, not of the solver: 1.0 on NQ at 29,000 is 0.3 bp; 1.0 on
    # SPY at 650 would be 15 bp. An equity chain (opra_pull.py, 2026-09-06)
    # carries its own `min_premium`; NQ chains carry none and get MIN_PREMIUM.
    floor = float(raw.get("min_premium", MIN_PREMIUM))
    settled = graded = 0
    for key, strikes in chain.items():
        unds = [r[c]["underlying"] for r in strikes.values() for c in r if r[c].get("underlying")]
        und = max(set(unds), key=unds.count) if unds else None
        F = fut.get(und)
        texp = book_expiry(strikes)
        if not F or texp is None:
            continue
        T = years_between(ref, texp)
        for ks, rec in strikes.items():
            K = float(ks)
            for cp in ("C", "P"):
                if cp not in rec:
                    continue
                r = rec[cp]
                r["T"] = round(T, 8)
                px = r.get("settle")
                if px is None:
                    r["iv"] = None
                    continue
                settled += 1
                intrinsic = max(0.0, (F - K) if cp == "C" else (K - F))
                if px - intrinsic < floor or T <= 0:
                    r["iv"] = None
                    r["iv_note"] = "below premium floor" if T > 0 else "expired at settle"
                    continue
                iv = implied_vol(px, F, K, T, cp == "C")
                r["iv"] = round(iv, 6) if iv else None
                r.pop("iv_note", None)
                if iv:
                    graded += 1
    raw["iv_basis"] = IV_BASIS
    raw["iv_ref"] = ref.isoformat(timespec="minutes")
    return graded, settled


def load_chain(path, force=False):
    """Read a chain file and make sure its vols are on the business-time basis."""
    import json
    raw = json.load(open(path, encoding="utf-8"))
    solve_chain(raw, force=force)
    return raw


if __name__ == "__main__":
    import sys
    d = datetime.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.date.today()
    print(f"{d}: session={is_session(d)} half={is_half_day(d)} "
          f"open={open_utc(d):%Y-%m-%d %H:%M}Z close={close_utc(d):%H:%M}Z "
          f"next={next_session(d)} prev={prev_session(d)}")
    for h in (0, 2, 7, 13.5, 14.5, 16.5, 19, 20):
        t = open_utc(d) + datetime.timedelta(hours=h - 22 + 24) if h < 22 else open_utc(d)
        t = datetime.datetime.combine(d, datetime.time(0), UTC) + datetime.timedelta(hours=h)
        print(f"  {t:%H:%M}Z  frac {session_frac(t, d):.3f}  tau {tau(t):.3f}")
