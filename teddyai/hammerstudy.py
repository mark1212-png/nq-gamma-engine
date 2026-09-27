# -*- coding: utf-8 -*-
"""
Does a hammer at a local low predict anything in NQ?

Same discipline as the gap and node studies: the naive number ("price rose after
70% of hammers!") is mostly a statement about where hammers occur, not about the
shape. A hammer forms at a local low, and a local low is already a place where
price tends to bounce. So the control is NOT "all other bars" -- it is

    CONTROL = bars at the SAME kind of local low that are NOT hammers.

Location held constant, shape varied. Anything left over is the pattern.

Definitions fixed before scoring:
  hammer  lower wick >= 2 x |body|, upper wick <= 0.25 x lower wick,
          lower wick >= 0.3 x ATR(14)          (so it is not a noise bar)
  at a low low == min(low) of this bar and the previous 7  (2 hours on 15m)
  forward returns measured from the bar's CLOSE, in ATR units so 2021 and
  2026 are comparable.
"""
import datetime, json, math, pathlib, statistics

SRC = pathlib.Path(__file__).parent / "nq-1m-2021-2026.json"
TF = 15                 # minutes per bar
LOOKBACK = 7            # bars before, for the "local low" test
HORIZONS = (1, 2, 4)    # bars forward: 15, 30, 60 minutes


def build(tf):
    rows = json.loads(SRC.read_text())
    buckets = {}
    for r in rows:
        t = datetime.datetime.fromisoformat(str(r["ts"]).replace(" ", "T"))
        k = t.replace(minute=(t.minute // tf) * tf, second=0, microsecond=0)
        b = buckets.get(k)
        if b is None:
            buckets[k] = dict(ts=k, o=r["o"], h=r["h"], l=r["l"], c=r["c"], n=1)
        else:
            b["h"] = max(b["h"], r["h"]); b["l"] = min(b["l"], r["l"])
            b["c"] = r["c"]; b["n"] += 1
    # a complete bar only; partial buckets straddle the maintenance break or a
    # feed outage and their wicks are an artefact of missing minutes
    return [buckets[k] for k in sorted(buckets) if buckets[k]["n"] >= tf - 2]


def main():
    bars = build(TF)
    print(f"{len(bars):,} complete {TF}-minute bars "
          f"({bars[0]['ts']:%Y-%m-%d} -> {bars[-1]['ts']:%Y-%m-%d})")

    tr = [b["h"] - b["l"] for b in bars]
    atr = [None] * len(bars)
    for i in range(14, len(bars)):
        atr[i] = statistics.mean(tr[i - 14:i])

    ham, ctrl = [], []
    maxh = max(HORIZONS)
    for i in range(LOOKBACK + 14, len(bars) - maxh):
        b = bars[i]
        a = atr[i]
        if not a or a <= 0:
            continue
        # contiguity: a gap in the series means the "previous 7 bars" are not
        # the previous 7 bars. Skip anything spanning a break.
        if (b["ts"] - bars[i - LOOKBACK]["ts"]).total_seconds() / 60 != TF * LOOKBACK:
            continue
        if (bars[i + maxh]["ts"] - b["ts"]).total_seconds() / 60 != TF * maxh:
            continue
        if b["l"] > min(bars[j]["l"] for j in range(i - LOOKBACK, i)):
            continue                                   # not a local low
        o, h, l, c = b["o"], b["h"], b["l"], b["c"]
        body = abs(c - o); lower = min(o, c) - l; upper = h - max(o, c)
        is_ham = (lower >= 2 * body) and (upper <= 0.25 * lower) and (lower >= 0.3 * a)
        fwd = {k: (bars[i + k]["c"] - c) / a for k in HORIZONS}
        (ham if is_ham else ctrl).append(fwd)

    print(f"\nat a 2-hour local low:  {len(ham):,} hammers   {len(ctrl):,} non-hammer controls\n")

    def stats(rs, k):
        v = [r[k] for r in rs]
        m = statistics.mean(v); sd = statistics.pstdev(v)
        return m, sd, len(v), sum(1 for x in v if x > 0) / len(v) * 100, statistics.median(v)

    print(f"{'horizon':>9} {'HAMMER mean':>12} {'win%':>6} | {'CONTROL mean':>13} {'win%':>6} | "
          f"{'edge(ATR)':>10} {'edge win%':>10} {'t':>6} {'p':>7}")
    for k in HORIZONS:
        hm, hs, hn, hw, hmed = stats(ham, k)
        cm, cs, cn, cw, cmed = stats(ctrl, k)
        se = math.sqrt(hs * hs / hn + cs * cs / cn)
        t = (hm - cm) / se if se else 0.0
        # two-sided normal approximation; n is in the thousands
        p = math.erfc(abs(t) / math.sqrt(2))
        print(f"{k*TF:>7}m  {hm:+12.4f} {hw:6.1f} | {cm:+13.4f} {cw:6.1f} | "
              f"{hm-cm:+10.4f} {hw-cw:+9.1f} {t:6.2f} {p:7.3f}")

    # how big is that in points, at tonight's volatility
    print(f"\nfor scale: a {TF}-minute ATR tonight is about 5 points, so an edge of "
          f"0.01 ATR is 0.05 points -- a fifth of a tick.")


if __name__ == "__main__":
    main()
