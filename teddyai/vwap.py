# -*- coding: utf-8 -*-
"""VWAP, its deviation bands, and the volume that makes them possible.

2026-09-16. VWAP was on Mark's chart and nowhere in the engine, and it was not merely
unwired: the bar cache had been dropping `volume` from ohlcv-1m since it was written, so
it was not computable. A volume profile was built here too and removed the same day --
"I dont really use VAH VAL and POC" -- so what is left is the mean he does trade off and
its deviations.

Bars without volume fall back to one unit each, which degrades VWAP to a time-weighted
mean rather than making it wrong; `weighted` says which you got.
"""

def _rth(bars, start="13:30"):
    r = [b for b in bars if b.get("ts", "")[11:16] >= start and b["ts"][:10] == bars[-1]["ts"][:10]]
    return r or bars


def vwap(bars, start="13:30"):
    """Session VWAP on the typical price. Returns (vwap, weighted)."""
    r = _rth(bars, start)
    if not r:
        return None, False
    w = any(b.get("v") for b in r)
    num = den = 0.0
    for b in r:
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        v = float(b.get("v") or 1)
        num += tp * v
        den += v
    return (num / den if den else None), w


# ---------------------------------------------------------------- vwap bands ---
# Mark, 2026-09-16: "I typically take longs below weekly vwap near its -1 -2 or -3
# deviations and shorts above the +1 +2 +3 deviations. The same applies for session
# vwap. Also though I use session vwap the weekly should have more weight if there
# are points of interest further out."
#
# NOT IN THE GRADING (his call, same message): "lets not hard encode that in the
# grading logic but just have it as a note for now until we watch it." So the bands
# are levels on the map and a note on a card that sits near one. They move no score.

def week_start(bars):
    """The Sunday 22:00 UTC reopen that the current week runs from."""
    import datetime
    if not bars:
        return None
    last = datetime.datetime.fromisoformat(bars[-1]["ts"].replace(" ", "T"))
    d = last.date()
    while d.weekday() != 6:                      # 6 = Sunday
        d -= datetime.timedelta(days=1)
    return "{} 22:00".format(d)


def bands(bars, start=None, n=(1, 2, 3), weighted=None):
    """Volume-weighted mean and its standard-deviation bands.

    `start` is a "YYYY-MM-DD HH:MM" cut; None means the whole slice given. Returns
    {vwap, sigma, weighted, bands:{+1:..,-1:..}} with sigma the standard deviation of
    the typical price about the mean.

    `weighted` picks the weight explicitly: True = volume, False = one per bar (a TIME
    weighted mean, i.e. a TWAP band), None = volume when the bars carry it.

    BOTH ARE KEPT ON PURPOSE (2026-09-16). The feed spent the afternoon writing
    volume-less bars, so a session band went out time-weighted and labelled VWAP: -1
    read 29,440.57 where the volume-weighted one read 29,425.99. The low of the move
    was 29,439.75 -- the TIME-weighted band was bottom-ticked by 0.82 of a point and
    the volume-weighted one was never reached. One instance proves nothing and the
    mislabelling was still a bug; but dropping the number that marked the low because
    the other is more orthodox would be choosing the theory over the tape. Both are
    computed, both are labelled, and the tape can settle it.
    """
    r = [b for b in bars if start is None or b.get("ts", "") >= start]
    if not r:
        return {"vwap": None, "sigma": None, "weighted": False, "bands": {}}
    have = any(b.get("v") for b in r)
    w = have if weighted is None else bool(weighted)
    wt = (lambda b: float(b.get("v") or 1)) if w else (lambda b: 1.0)
    num = den = 0.0
    for b in r:
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        v = wt(b)
        num += tp * v
        den += v
    if not den:
        return {"vwap": None, "sigma": None, "weighted": w, "bands": {}}
    mean = num / den
    var = 0.0
    for b in r:
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        var += wt(b) * (tp - mean) ** 2
    sig = (var / den) ** 0.5
    out = {}
    for k in n:
        out["+%d" % k] = mean + k * sig
        out["-%d" % k] = mean - k * sig
    return {"vwap": mean, "sigma": sig, "weighted": w, "bands": out, "n": len(r)}


def near_band(px, *books, tol=None):
    """Which band `px` is sitting on, if any. `books` are (label, bands()) pairs,
    checked in order, so pass the weekly first when it should win a tie -- his rule:
    'the weekly should have more weight if there are points of interest further out'.
    Returns (label, band_name, band_price, distance) or None."""
    best = None
    for label, b in books:
        if not b or not b.get("bands"):
            continue
        t = tol if tol is not None else max(6.0, 0.25 * (b["sigma"] or 0))
        for name, lv in b["bands"].items():
            d = abs(px - lv)
            if d <= t and (best is None or d < best[3]):
                best = (label, name, lv, d)
        if best:
            break
    return best
