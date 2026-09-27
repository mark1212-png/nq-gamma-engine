#!/usr/bin/env python3
"""
amer.py -- the early-exercise premium of an American option, and nothing else.

    python amer.py            (self-test, no data)

WHY THIS FILE EXISTS. QQQ and SPY options are American; SPX, SPXW and NQ's
options are European (NQ's are American in name but on a future with no carry,
where early exercise is never optimal). nqvol.bs76_price is the project's ONE
Black-76, and it misprices an American ITM put: with rates at ~4.5% a deep put
is worth its intrinsic today, not the discounted intrinsic, and a European
inversion of that price reads the difference as extra vol. Under a both-sides
smile that leaks into the upside knots.

WHAT IT DOES, AND HOW IT KEEPS nqvol THE ONE SOLVER. A Cox-Ross-Rubinstein
binomial tree prices the American and the European option on the SAME tree,
so the tree's discretisation error cancels in the difference:

    EEP = P_american - P_european         (discounted dollars, same tree)

opra_pull.py then stores (mid - EEP) / D as the contract's anchor -- the
European-equivalent price in forward space -- and nqvol.solve_chain, unchanged,
returns an American-consistent implied vol. The EEP depends on the vol, so the
anchor is found by a short fixed-point iteration (american_anchor), starting
from the European vol; three rounds are usually within 1e-4 of vol.

Inputs are the parity forward F and discount D that opra_pull already has,
plus a rate r and a continuous dividend yield q: S = F * exp(-(r - q) T).
Discrete ETF dividends are represented by that continuous yield, which is
exact for the forward and an approximation for the early-exercise boundary of
a call just before an ex-date. Stamped on every file that uses it.

What it does NOT do: Greeks. trinity_db keeps Black-76 gamma at the solved
vol, which for a deep-ITM American option (where exercise is optimal and the
true gamma is ~0) overstates gamma. Near the money -- where the hedge flow
is -- the difference is second order.

Written 2026-09-06, build step 3.
"""

import math
import numpy as np

import nqvol

N_STEPS = 200


def crr(S, K, r, q, sigma, T, is_call, n=N_STEPS):
    """(american, european) prices, discounted dollars, on one CRR tree."""
    if T <= 0 or sigma <= 0:
        v = max(0.0, (S - K) if is_call else (K - S))
        return v, v
    dt = T / n
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    disc = math.exp(-r * dt)
    p = (math.exp((r - q) * dt) - d) / (u - d)
    if not (0.0 < p < 1.0):                      # dt too coarse for this vol/carry: refine
        return crr(S, K, r, q, sigma, T, is_call, n * 2) if n < 4000 else (float("nan"), float("nan"))
    j = np.arange(n + 1)
    ST = S * u ** (2 * j - n)                     # terminal nodes, low to high
    pay = np.maximum(ST - K, 0.0) if is_call else np.maximum(K - ST, 0.0)
    am, eu = pay.copy(), pay.copy()
    for i in range(n - 1, -1, -1):
        eu = disc * (p * eu[1:] + (1 - p) * eu[:-1])
        am = disc * (p * am[1:] + (1 - p) * am[:-1])
        Si = S * u ** (2 * np.arange(i + 1) - i)
        ex = np.maximum(Si - K, 0.0) if is_call else np.maximum(K - Si, 0.0)
        am = np.maximum(am, ex)
    return float(am[0]), float(eu[0])


def eep(F, D, K, sigma, T, is_call, r, q, n=N_STEPS):
    """Early-exercise premium in discounted dollars for a contract quoted
    against forward F with discount D. S is backed out of F, r and q."""
    if T <= 0 or sigma <= 0:
        return 0.0
    S = F * math.exp(-(r - q) * T)
    a, e = crr(S, K, r, q, sigma, T, is_call, n)
    if math.isnan(a):
        return 0.0
    return max(a - e, 0.0)


def american_anchor(mid, F, D, K, T, is_call, r, q, rounds=3, tol=1e-4):
    """
    (anchor, eep, iv_eur, iv_amer, rounds_used) -- the European-equivalent
    forward-space price nqvol should solve on. None anchor when even the
    European inversion fails (below intrinsic, above the vol cap).
    """
    iv0 = nqvol.implied_vol(mid / D, F, K, T, is_call)
    if iv0 is None:
        return None, 0.0, None, None, 0
    iv, e = iv0, 0.0
    used = 0
    for k in range(rounds):
        used = k + 1
        e = eep(F, D, K, iv, T, is_call, r, q)
        anchor = (mid - e) / D
        iv_new = nqvol.implied_vol(anchor, F, K, T, is_call)
        if iv_new is None:                        # EEP ate the whole extrinsic: option is worth intrinsic
            return None, e, iv0, None, used
        if abs(iv_new - iv) < tol:
            iv = iv_new
            break
        iv = iv_new
    return (mid - e) / D, e, iv0, iv, used


# ------------------------------------------------------------- self-test ---

def selftest(verbose=True):
    errs = []
    F, D = 700.0, math.exp(-0.045 * 0.25)
    r, T = 0.045, 0.25
    q = 0.012
    S = F * math.exp(-(r - q) * T)
    # 1. the European leg of the tree converges to Black-76 (discounted)
    for K in (630.0, 700.0, 770.0):
        for cp in (True, False):
            a, e = crr(S, K, r, q, 0.18, T, cp)
            bs = D * nqvol.bs76_price(F, K, 0.18, T, cp)
            # CRR oscillates around the continuous price at O(1/n); 0.5% at
            # n=200 on an OTM option is that oscillation, and it CANCELS in the
            # American-minus-European difference the anchor uses
            if abs(e - bs) > 0.005 * max(bs, 0.5):
                errs.append(f"European CRR {K}{'C' if cp else 'P'} {e:.4f} vs Black-76 {bs:.4f}")
            if a < e - 1e-9:
                errs.append(f"American below European at {K}")
    # 2. deep ITM American put is worth intrinsic; European is worth less
    a, e = crr(S, 900.0, r, 0.0, 0.18, T, False)
    if abs(a - (900.0 - S)) > 0.02:
        errs.append(f"deep ITM American put {a:.3f} != intrinsic {900 - S:.3f}")
    if e >= a:
        errs.append("deep ITM European put not below American")
    # 3. no-dividend American call = European call
    a, e = crr(S, 700.0, r, 0.0, 0.18, T, True)
    if abs(a - e) > 1e-6:
        errs.append(f"q=0 American call {a:.5f} != European {e:.5f}")
    # 4. the anchor round-trips: an American price made from a known vol is inverted back to it
    sigma = 0.21
    for K in (660.0, 700.0, 740.0):
        for cp in (True, False):
            a, _ = crr(S, K, r, q, sigma, T, cp)
            anchor, e_, iv0, iv, used = american_anchor(a, F, D, K, T, cp, r, q)
            if iv is None or abs(iv - sigma) > 0.0015:
                errs.append(f"round-trip {K}{'C' if cp else 'P'}: iv {iv} vs {sigma} (eur {iv0})")
            if verbose:
                print(f"  K {K:6.0f} {'C' if cp else 'P'}  amer {a:8.4f}  EEP {e_:7.4f}  "
                      f"iv_eur {iv0 * 100:6.2f}%  iv_amer {iv * 100 if iv else float('nan'):6.2f}%  rounds {used}")
    print("  AMER SELFTEST", "PASS" if not errs else "FAIL")
    for x in errs:
        print("    -", x)
    return not errs


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
