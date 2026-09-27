#!/usr/bin/env python3
"""
session_scale.py -- how far price can still travel, from RIGHT NOW to the close.

The premarket and Asia plans both describe a session that has not started, so
their rails are a settled constant: cumulative variance from the settle, 0.503x
for Asia, 0.674x for Europe, 1.000x for RTH. A plan built at 09:15 PT is a
different question. Price is already somewhere, and only part of the day's
movement is still ahead of it. Both halves of the sheet have to change:

  * TIME TO EXPIRY is the hours actually left to the 20:00 UTC expiry, not 6.5.
    This is the part that moves the numbers most -- the 0DTE flip shifted about
    100 points across the plausible TTE range on the one day we measured it.
  * THE RAILS have to shrink, and NOT in proportion to the time left.

That second point is the whole reason this file exists. RTH variance is heavily
front-loaded: a quarter of it is spent in the first half hour and more than half
of it inside the first hour. So at 07:30 PT -- one hour in, 85% of the clock
still to run -- only 47% of the session's movement is still to come. Scaling the
rails by remaining TIME would draw them about a third too wide, every day, in
the same direction. That is exactly the kind of quiet, one-sided error the rails
were the one validated level type for avoiding.

MEASUREMENT (64 complete sessions of NQ 1-minute bars, Jun-Sep 2026)
--------------------------------------------------------------------
CUM below is the share of RTH realised variance spent by each 15-minute mark
after the 06:30 PT open. It reproduces the published segment shares the rest of
the project uses (post-close 7.1%, Asia 18.0%, Europe 19.5%, RTH 55.4%, against
the 6.4 / 18.9 / 20.1 / 54.6 already in teddy.py) so it sits on the same basis.

The rail scale is NOT sqrt(remaining variance), though. Realised variance sums
every wiggle; the rail only cares where price ENDS UP, and NQ mean-reverts
intraday, so terminal displacement grows more slowly than realised variance
does. Measured directly -- the ratio of the p-quantile of |close - price(t)| to
the p-quantile of |close - previous settle|, which is the yardstick the
validated full-day rail is drawn in -- the shape is a clean square root with a
constant shrink:

    scale(t) = 0.628 * sqrt(1 - CUM(t))

The 0.628 against sqrt(0.546) = 0.739 is that mean-reversion shrink, 0.85x. The
ratio held across p = 0.50, 0.60, 0.68, 0.75 and 0.80, so it is a property of
the scaling and not an artefact of one quantile. Containment of the close inside
the resulting band ran 64-74% across the session against the 68% target -- the
same neighbourhood as the full-day rail's measured 73%.

Re-measure with /tmp-style scratch analysis if the vol regime changes character;
these are 2026 summer numbers.
"""

import datetime
import cme_calendar

OPEN_UTC = 13.5     # 06:30 PT
EXPIRY_UTC = 20.0   # 13:00 PT -- the front option expires here, and the CME
                    # settlement anchor the overnight curve is measured from
ASIA_END_UTC = 7.0  # 00:00 PT
STEP_MIN = 15

# share of RTH variance spent by +0.00h, +0.25h, ... +6.25h after the open
CUM = [0.1411, 0.2523, 0.3278, 0.4044, 0.4707, 0.5251, 0.5681, 0.6032,
       0.6353, 0.6682, 0.6932, 0.7158, 0.7424, 0.7636, 0.7840, 0.8042,
       0.8206, 0.8357, 0.8573, 0.8710, 0.8879, 0.9039, 0.9251, 0.9431,
       0.9628, 1.0000]

SHRINK = 0.628      # fitted; see the docstring
FLOOR = 0.08        # a band that collapses onto price is a broken sheet, not a
                    # tight one. Inside the last ~20 minutes the honest answer
                    # is "the rail has stopped saying anything" -- say that in
                    # the prose rather than drawing a zero-width box.


def _session_bounds(day=None):
    """(open, close) in UTC hours for a session date, default today."""
    d = day or datetime.datetime.now(datetime.timezone.utc).date()
    if not cme_calendar.is_session(d):
        return OPEN_UTC, EXPIRY_UTC          # caller has already decided; keep shape
    return cme_calendar.session_hours_utc(d)


def cum_spent(minutes_in, span_min=None):
    """
    Share of session variance already spent, `minutes_in` after the open.

    The profile was measured on FULL 390-minute sessions. On a half session it
    is applied by fraction of the session rather than by clock minutes -- an
    assumption, not a measurement: it says a half session has the same shape
    compressed. That is far better than treating 210 minutes as though 180 of
    the day's variance were still ahead, but it has not been verified, and
    there are only three half sessions a year to verify it on.
    """
    span = span_min or (390.0)
    if minutes_in <= 0:
        return 0.0
    x = (minutes_in / span) * (len(CUM) - 0.0)
    i = int(x)
    if i >= len(CUM):
        return 1.0
    lo = CUM[i - 1] if i else 0.0
    return lo + (CUM[i] - lo) * (x - i)


# ---------------------------------------------------------------- overnight --
# The same question for the other end of the day: standing HERE, how far can
# price be at the 07:00 UTC Asia close? Measured the same way, on the same 50
# sessions, in the same units -- the ratio of the p-quantile of
# |price(07:00 UTC) - price(t)| to the p-quantile of |RTH close - prev settle|,
# with t measured in hours from the 20:00 UTC settle:
#
#     scale(t) = 0.424 * (1 - t/11)^0.550
#
# Containment of the 07:00 UTC price inside that band ran 68-76% across the
# window against the 68% target.
#
# TWO THINGS FALL OUT OF THIS, and both were wrong before.
#
# 1. The settle-anchored figure is 0.424, not the published 0.503. Same
#    mean-reversion shrink as RTH -- 0.503 x 0.85 = 0.428, which is the measured
#    value to three decimals. 0.503 came from summing realised variance, which
#    counts every wiggle; the rail only cares where price ends up.
# 2. The scheduled Asia build runs at 17:15 PT, which is 4.25 HOURS AFTER the
#    settle it was anchoring to. A quarter of the overnight was already gone
#    before the plan was written, so the honest band there is ~0.32, not 0.503.
#    Rebuild the same plan at 21:42 and it is ~0.19. A fixed constant cannot
#    express that; only the clock can.
ON_A, ON_B, ON_SPAN = 0.424, 0.550, 11.0

# ------------------------------------------------------------------ london --
# The curve above STOPS at the 07:00 UTC Asia close and returned None past it,
# and scale_for_hours refuses anything starting before the RTH open ("not ours
# to scale"). Between them sat Europe, with nothing. Asked for a London report
# on 2026-09-15 the pipeline ran the asia job at 05:30 UTC and produced a sheet
# whose clock said 14.47 hours and whose band said +/-36 points -- the dying
# tail of Asia drawn around a plan covering the rest of the day. Reach and the
# structural stop are both fractions of the drawn band, so every card inherited
# it: the whole plan was two strikes within 13 points of price.
#
# Fitted here the same way, on the same yardstick, over 236 sessions of
# NQ 1-minute bars (Oct 2025 - Sep 2026): the ratio of the p-quantile of
# |price(13:30 UTC) - price(t)| to the p-quantile of |RTH close - prev close|,
# t in hours UTC across the 07:00-13:30 window.
#
#     t      p50    p60    p68    p75    p80    mean
#     07:00  0.346  0.332  0.317  0.376  0.359  0.346
#     09:00  0.294  0.282  0.278  0.310  0.306  0.294
#     11:00  0.204  0.240  0.248  0.277  0.274  0.249
#     13:00  0.137  0.138  0.131  0.145  0.145  0.139
#
#     scale(t) = 0.346 * (1 - (t - 7)/6.5)^0.337
#
# The ratio holds across every quantile, as it does for RTH and the overnight,
# so this is a property of the scaling and not an artefact of one p. The 0.346
# at the Asia close hands over cleanly from the overnight curve's own floor, and
# the exponent is flatter than the overnight's 0.550 because European hours put
# less of their movement in early -- the decay is closer to linear in time.
#
# THE TARGET IS THE 13:30 UTC NY OPEN, not the 20:00 expiry. That is the same
# convention as the other two curves -- each answers "how far can price be at
# the END of the window this plan covers" -- and it is why precommentary.py
# compares the band against the plan's window rather than its option expiry.
LON_A, LON_B = 0.346, 0.337
LONDON_START_UTC, LONDON_END_UTC = 7.0, 13.5


def scale_london(hour_utc):
    """
    Rail scale for a plan that runs from now to the 13:30 UTC NY open.

    `hour_utc` is the hour of day in UTC, 7.0 to 13.5. Returns None outside that
    window -- before it the overnight curve is the one describing anything, after
    it the RTH one is.
    """
    t = hour_utc
    if t < LONDON_START_UTC or t >= LONDON_END_UTC:
        return None
    span = LONDON_END_UTC - LONDON_START_UTC
    return max(LON_A * (1 - (t - LONDON_START_UTC) / span) ** LON_B, FLOOR)


def scale_now(hour_utc, hours_since_settle=None):
    """
    The right curve for the moment, whichever window it falls in -- so a caller
    never has to know which of the three to reach for, and never gets None back
    for a time of day that plainly exists.
    """
    if hours_since_settle is not None:
        v = scale_overnight(hours_since_settle)
        if v is not None:
            return v
    v = scale_london(hour_utc)
    if v is not None:
        return v
    return scale_for_hours(max(EXPIRY_UTC - hour_utc, 0.01))


def scale_overnight(hours_since_settle):
    """
    Rail scale for a plan that runs from now to the 07:00 UTC Asia close.

    `hours_since_settle` is measured from 20:00 UTC (13:00 PT). Returns None
    before the settle or after the Asia close -- outside that window this curve
    is not describing anything.
    """
    t = hours_since_settle
    if t < 0 or t >= ON_SPAN:
        return None
    return max(ON_A * (1 - t / ON_SPAN) ** ON_B, FLOOR)


def scale_for_hours(hours, day=None):
    """
    Rail scale for a plan with `hours` left to the 20:00 UTC expiry.

    Separate from intraday() so a forced --hours still gets the measured rail
    rather than a full-day band -- getting the TTE right and the rails wrong is
    the wrong half of the fix.
    """
    o, c = _session_bounds(day)
    span = (c - o) * 60.0
    minutes_in = (c - hours - o) * 60.0
    if minutes_in < 0:                # a plan that starts before the RTH open
        return None                   # is an Asia/Europe plan; not ours to scale
    if minutes_in > span:
        return None
    return max(SHRINK * (1.0 - cum_spent(minutes_in, span)) ** 0.5, FLOOR)


def intraday(now=None):
    """
    (hours_to_expiry, rail_scale, label) for a plan that describes RIGHT NOW.

    `now` is local time, which this project reasons in as Pacific throughout.
    Returns None outside 06:30-13:00 PT -- there is no "rest of the session" to
    describe, and the caller should build the premarket or Asia plan instead.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        # A naive datetime is LOCAL, and this project's local is Pacific. _utc_offset_hours is EASTERN's
        # offset (4 under EDT, 5 under EST) and Pacific is three hours behind Eastern, so PT is
        # -(offset + 3) from UTC: -7 in summer, -8 in winter.
        #
        # It read "-offset + 1" (UTC-3) until 2026-09-08 -- four hours off, every day of the year -- and the
        # damage was not a crash. autopull calls this with datetime.now(), so between 06:30 and 10:30 PT the
        # shifted clock landed before the RTH open and intraday() returned None ("outside 06:30-13:00"),
        # while between 10:30 and 13:00 PT it landed INSIDE the session and returned a plausible answer
        # carrying four hours too much time. The 09-08 11:13 build priced a 1.78-hour book as 5.78: rails
        # 0.518x instead of 0.240x (+/-117 points instead of +/-54) and the three flips drawn ~20 low. The
        # window where it appeared to work was exactly the window where it lied.
        now = now.replace(tzinfo=datetime.timezone(datetime.timedelta(
            hours=-(cme_calendar._utc_offset_hours(now.date()) + 3))))
    now = now.astimezone(datetime.timezone.utc)
    day = now.date()
    if not cme_calendar.is_session(day):
        return None
    o, c = cme_calendar.session_hours_utc(day)
    utc = now.hour + now.minute / 60.0 + now.second / 3600.0
    if not (o <= utc < c):
        return None
    hours_left = c - utc
    return hours_left, scale_for_hours(hours_left, day), "Rest of session"


def describe(now=None, day=None):
    v = intraday(now)
    if v is None:
        return "outside the RTH session -- use the premarket or Asia plan"
    hours, scale, _ = v
    d = day or datetime.datetime.now(datetime.timezone.utc).date()
    o, c = _session_bounds(d)
    span = (c - o) * 60.0
    spent = cum_spent((c - hours - o) * 60.0, span)
    return (f"{hours:.2f}h to the {c:05.2f} UTC expiry; {spent*100:.0f}% of "
            f"session variance already spent, so the rails run {scale:.3f}x the "
            f"full-day expected move (proportional time would say "
            f"{(0.546 * hours / (c - o)) ** 0.5:.3f}x)")


# DST is no longer this file's problem: the session bounds come from
# cme_calendar, which derives the UTC offset from the date.
#
# Worth recording why the old code was RIGHT despite looking wrong, because the
# obvious fix would have broken it. It read `utc = h + 7.0` (PDT) against
# EXPIRY_UTC = 20.0. Under PST both are off by an hour in the SAME direction --
# 13:00 PT is 21:00 UTC, so the correct pair is h + 8.0 against 21.0 -- and
# 20 - (h + 7) == 21 - (h + 8). hours_left was exact year-round. Changing the
# offset alone, which is what a DST sweep would have done, would have
# INTRODUCED a one-hour TTE error where there was none. Constants that cancel
# have to move together or not at all.

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        t = datetime.datetime.fromisoformat(sys.argv[1])
        print(f"{t:%H:%M} PT  {describe(t)}")
    else:
        print(f"now  {describe()}")
        print()
        for m in range(0, 391, 30):
            t = datetime.datetime(2026, 1, 1, 6, 30) + datetime.timedelta(minutes=m)
            v = intraday(t)
            if v:
                hrs, sc, _ = v
                print(f"  {t:%H:%M} PT   {hrs:4.2f}h left   rails {sc:.3f}x   "
                      f"(time-proportional would be "
                      f"{(0.546 * hrs / 6.5) ** 0.5:.3f}x)")
