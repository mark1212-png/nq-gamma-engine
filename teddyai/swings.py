# -*- coding: utf-8 -*-
"""Swing structure from the tape: the floors and ceilings price actually made.

WHY THIS EXISTS (Mark, 2026-09-16). Two things the engine had no way to say, and
both of them are this one object:

  DIRECTION.  "all throughout last night we saw floors rolling up all the way from
  29000". The engine refused to carry a directional view because gamma does not
  supply one -- and then wrote fades into a tape that put in seven consecutive
  higher lows. A rising sequence of defended floors is not a gamma claim, it is
  what the tape did, and it is the most ordinary directional read there is.

  INVALIDATION.  His 29,250 long was "invalidated if we made a new low at around
  1900" -- the 19:52Z swing low, 29,207.50. The engine's stop was 0.15 x the drawn
  band = 20 points, at 29,231. The overnight low came in at 29,226.00, 18.50 above
  his invalidation and 5 under the engine's. It stopped the card for -1R and price
  ran 288 points off that exact low. Same level, same side, entry one point apart;
  the only difference was that one stop came from a place the market defended and
  the other from a fraction of a volatility estimate.

They are the same structure, which is why asking him for a stop rule separately got
"I think we should have directional bias" back. The floors give both.

A swing low is the low of a +/-WINDOW bar window; a swing high the mirror. WINDOW is
40 one-minute bars, the setting whose output he read and said "lets keep this swing
low". No smoothing, no ATR, no fitted parameter: the extreme of a window either is
the extreme or it is not.
"""

WINDOW = 40          # bars either side; 1-minute bars, so ~40 minutes -- the DAY read
WINDOW_INTRA = 15    # the SESSION read: an 81-bar window is half an RTH session and finds
                     # nothing inside it (2026-09-16: zero pivots in 159 bars). 15 reproduces
                     # the tape you can see -- the 14:13 low on the flips, the 14:37 low under
                     # the 29,450 wall, the 15:47 spike high -- without picking up noise.
MIN_GAP = 40         # bars between two swings of the same kind


def _pivots(bars, window, gap, key, pick):
    out = []
    n = len(bars)
    for i in range(window, n - window):
        w = bars[i - window: i + window + 1]
        if bars[i][key] == pick(r[key] for r in w):
            if out and (i - out[-1][2]) < gap:
                # the better of two that are too close together replaces the first
                if pick((bars[i][key], out[-1][1])) == bars[i][key]:
                    out[-1] = (bars[i].get("ts"), bars[i][key], i)
                continue
            out.append((bars[i].get("ts"), bars[i][key], i))
    return out


def lows(bars, window=WINDOW, gap=MIN_GAP):
    """[(ts, price, index)] -- the floors, oldest first."""
    return _pivots(bars, window, gap, "l", min)


def highs(bars, window=WINDOW, gap=MIN_GAP):
    """[(ts, price, index)] -- the ceilings, oldest first."""
    return _pivots(bars, window, gap, "h", max)


def entry_swing(bars, side, near=None, window=WINDOW, gap=MIN_GAP):
    """Where to get in: the swing itself.

    Mark, 2026-09-16: "we should make entries as close to the swing low as
    possible." A long is bought AT the floor, not a point inside whatever gamma
    strike happens to sit nearby -- the floor is the thing that has to hold, so it
    is the thing to lean the entry on, and every point of distance from it is risk
    bought for nothing.

    `near` is an optional level to prefer (the strike a card is named for); without
    it the most recent swing on that side is used. Returns (price, ts, index) or
    (None, None, None).
    """
    pool = lows(bars, window, gap) if side == "long" else highs(bars, window, gap)
    if not pool:
        return None, None, None
    ts, p, i = (min(pool, key=lambda t: abs(t[1] - near)) if near is not None else pool[-1])
    return p, ts, i


def invalidation(bars, side, entry=None, window=None, gap=None, min_dist=0.0):
    """What has to happen for the trade to be wrong: the MOST RECENT swing beyond the entry.

    Mark, 2026-09-16, correcting the first version: "the stop wouldve been structural at
    around 29428, the most recent swing low next to it."

    Latest in TIME, not nearest in price, and the two of his trades pin it:

        29,450 long   swing lows below it: 29,418 @14:13Z, 29,426 @14:37Z
                      -> 29,426, the later one, even though 29,418 is only 8 further away
        29,250 long   swing lows below it: 29,241.25 @17:24Z, 29,229.75 @18:49Z,
                      29,207.50 @19:52Z  ->  29,207.50, the last one before he entered,
                      and the level he named ("a new low at around 1900")

    The nearest-in-price reading picks 29,241.25 there, nine points under the entry, and
    that is noise inside the structure. The most recent pivot is the one the sequence is
    standing on: everything after it has held above it, which is exactly the claim the
    trade makes. Returns (price, ts) or (None, None).
    """
    W = window or WINDOW_INTRA
    g = gap or W
    pool = lows(bars, W, g) if side == "long" else highs(bars, W, g)
    # `min_dist` keeps the stop off the pivot the ENTRY is standing on. Without it a short
    # at 29,549 anchored on the 29,549.75 spike high it was entering against: a 2-point stop
    # reading 13.71R, which is an artefact, not a trade (2026-09-16).
    beyond = [(ts, p) for ts, p, _ in pool
              if (p < entry - min_dist if side == "long" else p > entry + min_dist)]              if entry is not None else [(ts, p) for ts, p, _ in pool]
    if not beyond:
        return None, None
    ts, p = beyond[-1]          # pool is oldest-first, so the last one is the most recent
    return p, ts


def bias(bars, window=WINDOW, gap=MIN_GAP, n=4):
    """The directional read the floors and ceilings give, over the last `n` of each.

    up    the last n floors are rising and the last high is not lower than the one
          before it -- price is being bid from higher and higher places
    down  the mirror
    none  they disagree, or there are not enough swings to say

    It carries `floors` and `ceilings` so the prose can name them, and `since`, the
    oldest floor in the rising run -- the level the whole sequence is built on, and
    the one whose loss ends it.
    """
    lo, hi = lows(bars, window, gap), highs(bars, window, gap)
    out = {"bias": "none", "floors": [(p, ts) for ts, p, _ in lo[-n:]],
           "ceilings": [(p, ts) for ts, p, _ in hi[-n:]], "since": None, "run": 0,
           "why": "not enough swings to read a sequence"}
    if len(lo) < 2:
        return out
    f = [p for _, p, _ in lo]
    run, i = 0, len(f) - 1
    while i > 0 and f[i] > f[i - 1]:
        run += 1
        i -= 1
    fall, j = 0, len(f) - 1
    while j > 0 and f[j] < f[j - 1]:
        fall += 1
        j -= 1
    c = [p for _, p, _ in hi]
    ch = (c[-1] - c[-2]) if len(c) >= 2 else 0.0
    if run >= 2 and ch >= 0:
        out.update(bias="up", run=run, since=f[i],
                   why=f"{run} higher floors in a row, {f[i]:,.2f} to {f[-1]:,.2f}"
                       + (f"; the last ceiling is {ch:+,.2f} on the one before" if c else ""))
    elif fall >= 2 and ch <= 0:
        out.update(bias="down", run=fall, since=f[j],
                   why=f"{fall} lower floors in a row, {f[j]:,.2f} to {f[-1]:,.2f}"
                       + (f"; the last ceiling is {ch:+,.2f} on the one before" if c else ""))
    else:
        out["why"] = (f"floors {'rising' if run else 'falling' if fall else 'flat'}, "
                      f"last ceiling {ch:+,.2f}: no agreement")
    return out


def where_in_range(bars, entry, window=None, gap=None, mid=(0.30, 0.70), price=None):
    """Where an entry sits in the range the session has actually carved out.

    Mark, 2026-09-16: the gameplan "outputted entries in the middle of the range, shorts
    at the bottom of the range (which are inherently more risky) and longs at the top of
    the range (also risky)".

    THE RANGE IS THE SESSION'S EXTENT, NOT THE LAST TWO PIVOTS. Taking only the most
    recent floor and ceiling produced a window price had already left: at 16:11Z on
    2026-09-16 that read 29,363.75-29,484.25 with price at 29,478, so 29,450 -- sitting
    28 points BELOW price, where a long is buying support -- came out at 72% and was
    refused as "a long at the ceiling". A range that does not contain price inverts the
    read exactly when the tape is trending, which is when it matters. So: the lowest
    swing low to the highest swing high the session has made, widened to include the last
    print, and measured on the INTRADAY window -- the day window needs 81 bars and finds
    nothing inside a session.

        below    under the range      a short here is a breakdown, not selling support
        floor    bottom third         a SHORT here is selling the level price turned on
        middle   no structure         neither side has anything to lean on
        ceiling  top third            a LONG here is buying resistance
        above    over the range       a long here is a breakout (his own 29,300 trade)
    """
    W = window or WINDOW_INTRA
    g = gap or W
    lo_p = lows(bars, W, g)
    hi_p = highs(bars, W, g)
    if (not lo_p and not hi_p) or entry is None:
        return {"at": None, "where": "unknown", "lo": None, "hi": None, "inside": False}
    last = price if price is not None else (bars[-1].get("c") if bars else None)
    cands_lo = [p for _, p, _ in lo_p] + ([last] if last is not None else [])
    cands_hi = [p for _, p, _ in hi_p] + ([last] if last is not None else [])
    lo, hi = min(cands_lo), max(cands_hi)
    if hi <= lo:
        return {"at": None, "where": "unknown", "lo": lo, "hi": hi, "inside": False}
    at = (float(entry) - lo) / (hi - lo)
    where = ("below" if at < 0 else "above" if at > 1
             else "floor" if at <= mid[0] else "ceiling" if at >= mid[1] else "middle")
    return {"at": at, "where": where, "lo": lo, "hi": hi, "inside": 0 <= at <= 1}
