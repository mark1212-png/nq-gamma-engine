#!/usr/bin/env python3
"""
crossasset.py -- do the index complexes agree about the state of the book?

    import crossasset
    a = crossasset.agreement(books_by_root)

--------------------------------------------------------------------------------
READ THIS BEFORE READING THE NUMBER
--------------------------------------------------------------------------------

The brief asked for a scalar running from -1 "strongly bearish" to +1 "strongly
bullish", built from an explicitly defined state model rather than from a raw
gamma sign. Building the explicit state model is what showed that the axis cannot
honestly be labelled bullish or bearish, so it is not labelled that here. The
field is `state_score` and it measures POSITIONING STATE, not price direction.

The reason is measured, not squeamish. Everything available to compare across
these books is gamma structure, flip location and skew, and this project has
tested each for directional content:

  gamma sign          the 60-session backtest found no support for a gamma sign
                      implying even a compressed or expanded RANGE (p = 0.46),
                      let alone a direction
  break into "air"    an artefact of node density over 63 sessions; of 4,354
                      breaks only 15 had a gap wider than half an expected move
  25-delta skew       rank correlation with direction 0.00 (p = 0.99). It showed
                      a weak link to RANGE (rho +0.23, p 0.08) and none at all
                      to which way price went

A composite built from those inputs and labelled "bullish" would be the exact
conversion the rest of this engine is built to prevent, dressed in a decimal.

WHAT `state_score` DOES MEAN, precisely:

  +1   dealers are long gamma at spot, price sits above the flip, skew is flat
       or calls are bid. A book positioned to DAMPEN movement.
  -1   dealers are short gamma at spot, price sits below the flip, puts are bid.
       A book positioned to AMPLIFY movement.

That is a statement about hedging posture. Whether it predicts price is exactly
the question Phase G exists to answer, and until it does the output carries
`predicts_direction: false`.

--------------------------------------------------------------------------------
WHY THE WEIGHTS ARE NOT EQUAL, AND WHAT THEY ARE INSTEAD
--------------------------------------------------------------------------------

There are five instruments and only TWO underlyings. `opra_compare.py` states it:
QQQ and NQ are NDX cash and NDX futures; SPY and the SPX complex are one
underlying seen through two books. Agreement inside a pair is near-tautological.

Weighting five instruments equally would therefore count the SPX complex three
times and NDX twice, and a "composite" built that way would mostly be measuring
SPX. So the aggregation is two-stage:

  1. WITHIN a complex, instruments are weighted by SIZE -- their share of the
     complex's gross dollar gamma. A bigger book is more informative about that
     underlying, and this needs no free parameter.
  2. ACROSS complexes, configurable weights, equal by default because NDX and
     SPX are genuinely different underlyings and there is no evidence yet that
     one leads the other.

The comparison the layer exists for is BETWEEN complexes. Within-complex
agreement is reported too, but as a data-quality check: two books on one
underlying that disagree are telling you something is wrong with one of them,
not something about the market.

--------------------------------------------------------------------------------
NOTHING IS COMPARED IN CONTRACT GAMMA
--------------------------------------------------------------------------------

Sizes are converted to DOLLAR GAMMA PER 1% MOVE, exactly as `opra_compare.py`
does it: contracts of delta per 1% times the reference price times the contract
multiplier, 20 for NQ and 100 for the equity roots. A 1% move is a 1% move
whatever the ticker. Contract gamma is not comparable across a 7,750 index, a 775
ETF and a 29,500 future, and is never used for weighting here.

Written 2026-09-09, Phase F of the market-state upgrade.
"""

import math
from dataclasses import dataclass, field, asdict

# Which instruments sit on which underlying. From opra_compare.py's mapping,
# which is computed daily from the forwards and never hardcoded as a ratio.
COMPLEXES = {
    "NDX": ("NQ", "QQQ"),
    "SPX": ("SPX", "SPXW", "SPY"),
}

MULTIPLIER = {"NQ": 20.0, "SPX": 100.0, "SPXW": 100.0, "QQQ": 100.0, "SPY": 100.0}


@dataclass
class Config:
    # ACROSS complexes. Equal by default: NDX and SPX are different underlyings
    # and no evidence exists in this project that one leads the other.
    complex_weights: dict = field(default_factory=lambda: {"NDX": 1.0, "SPX": 1.0})
    # Contribution of each state component to state_score. Equal, and unfitted.
    gamma_w: float = 1.0
    flip_w: float = 1.0
    skew_w: float = 1.0
    flip_scale_ems: float = 1.0     # flip distance is squashed on this scale
    min_instruments: int = 1

    def merged(self, **kw):
        d = asdict(self)
        d.update({k: v for k, v in kw.items() if v is not None and k in d})
        return Config(**d)


def complex_of(root):
    for name, roots in COMPLEXES.items():
        if root in roots:
            return name
    return None


def dollar_gamma(contracts_per_pct, reference_price, root):
    """contracts of delta per 1% -> dollars of delta per 1%.

    The one unit in which a future, an index and an ETF are comparable.
    """
    return contracts_per_pct * reference_price * MULTIPLIER.get(root, 100.0)


def state_score(gamma_at_spot, gamma_gross, flip_distance_ems, skew_spread, cfg):
    """
    One instrument's positioning state in [-1, +1]. See the module docstring for
    what the sign means and, more importantly, what it does not.

    Every component is normalised to [-1, 1] before it is combined, so a dollar
    figure and a vol spread never enter the same sum in their own units.
    """
    comps, used, wts = {}, [], []

    if gamma_gross:
        g = max(-1.0, min(1.0, gamma_at_spot / gamma_gross))
        comps["gamma"] = round(g, 4)
        used.append(g); wts.append(cfg.gamma_w)
    else:
        comps["gamma"] = None

    if flip_distance_ems is not None:
        # tanh so a book with the flip far away saturates instead of dominating
        f = math.tanh(flip_distance_ems / cfg.flip_scale_ems) if cfg.flip_scale_ems else 0.0
        comps["flip"] = round(f, 4)
        used.append(f); wts.append(cfg.flip_w)
    else:
        comps["flip"] = None

    if skew_spread is not None:
        # puts bid (positive spread) is the AMPLIFY side, hence the negation
        s = max(-1.0, min(1.0, -skew_spread / 0.05))
        comps["skew"] = round(s, 4)
        used.append(s); wts.append(cfg.skew_w)
    else:
        comps["skew"] = None

    if not used:
        return None, comps
    total = sum(wts) or 1.0
    return round(sum(u * w for u, w in zip(used, wts)) / total, 4), comps


def agreement(instruments, cfg=None, **kw):
    """
    instruments: {root: {...}} where each value carries at least
        gamma_at_spot, gamma_gross   contracts of delta per 1% move
        reference                    the price the book is struck against
      and optionally
        flip_distance_ems, skew_spread

    Returns per-instrument scores, per-complex scores, a composite, and the
    agreement / conflict pair -- all measured BETWEEN complexes.
    """
    cfg = (cfg or Config()).merged(**kw)
    if not instruments:
        return {"instruments": {}, "complexes": {}, "composite": None,
                "note": "no instruments supplied", "predicts_direction": False}

    per = {}
    for root, d in instruments.items():
        s, comps = state_score(d.get("gamma_at_spot", 0.0), d.get("gamma_gross", 0.0),
                               d.get("flip_distance_ems"), d.get("skew_spread"), cfg)
        ref = d.get("reference") or 0.0
        per[root] = {
            "state_score": s,
            "components": comps,
            "complex": complex_of(root),
            "size_usd_per_pct": round(dollar_gamma(d.get("gamma_gross", 0.0), ref, root), 1),
            "reference": ref,
            "multiplier": MULTIPLIER.get(root, 100.0),
            "stale": bool(d.get("stale")),
        }

    # --- within a complex: weight by SIZE in dollar terms -------------------
    comps_out = {}
    for name, roots in COMPLEXES.items():
        members = {r: v for r, v in per.items()
                   if v["complex"] == name and v["state_score"] is not None}
        if len(members) < cfg.min_instruments:
            continue
        tot = sum(abs(v["size_usd_per_pct"]) for v in members.values())
        if tot > 0:
            score = sum(v["state_score"] * abs(v["size_usd_per_pct"]) for v in members.values()) / tot
            wts = {r: round(abs(v["size_usd_per_pct"]) / tot, 4) for r, v in members.items()}
        else:
            score = sum(v["state_score"] for v in members.values()) / len(members)
            wts = {r: round(1.0 / len(members), 4) for r in members}
        vals = [v["state_score"] for v in members.values()]
        spread = (max(vals) - min(vals)) if len(vals) > 1 else 0.0
        comps_out[name] = {
            "state_score": round(score, 4),
            "members": sorted(members),
            "size_weights": wts,
            "size_usd_per_pct": round(tot, 1),
            "internal_spread": round(spread, 4),
            # Two books on ONE underlying disagreeing is a data problem, not a
            # market signal. opra_compare.py makes the same point.
            "internal_note": ("books on one underlying; agreement here is near-tautological and a "
                              "wide internal spread indicates a data problem, not a market view"),
            "internal_warning": bool(spread > 0.5),
        }

    if not comps_out:
        return {"instruments": per, "complexes": {}, "composite": None,
                "note": "no complex had a scorable instrument", "predicts_direction": False}

    # --- across complexes: configurable, equal by default -------------------
    names = sorted(comps_out)
    wts = {n: float(cfg.complex_weights.get(n, 1.0)) for n in names}
    wtot = sum(wts.values()) or 1.0
    composite = sum(comps_out[n]["state_score"] * wts[n] for n in names) / wtot

    vals = [comps_out[n]["state_score"] for n in names]
    if len(vals) > 1:
        # Agreement is 1 when the complexes sit on top of each other and 0 when
        # they are at opposite ends of the scale. The scale is 2 wide, so the
        # spread is divided by 2.
        spread = (max(vals) - min(vals)) / 2.0
        agree = max(0.0, 1.0 - spread)
        same_sign = all(v >= 0 for v in vals) or all(v <= 0 for v in vals)
    else:
        spread, agree, same_sign = None, None, None

    return {
        "instruments": per,
        "complexes": comps_out,
        "composite": round(composite, 4),
        "agreement_strength": None if agree is None else round(agree, 4),
        "conflict": None if agree is None else round(1.0 - agree, 4),
        "complexes_same_sign": same_sign,
        "complex_weights": wts,
        "n_complexes": len(names),
        "scale": {
            "+1": "dealers long gamma at spot, price above the flip, skew flat or calls bid; "
                  "a book positioned to DAMPEN movement",
            "-1": "dealers short gamma at spot, price below the flip, puts bid; "
                  "a book positioned to AMPLIFY movement",
            "0": "neutral, or the components disagree",
        },
        # The single most important field in this file.
        "predicts_direction": False,
        "direction_note": (
            "state_score is POSITIONING STATE, not a price forecast, and must not be read as "
            "bullish or bearish. Every input available here has been tested for directional "
            "content in this project and none survived: gamma sign implies nothing about even "
            "the range (p=0.46), the break-into-air reading is an artefact of node density, and "
            "25-delta skew correlates 0.00 with direction (p=0.99)."),
        "weights_validated": False,
        "weights_note": (
            "Within a complex, weights are each instrument's share of gross DOLLAR gamma, which "
            "needs no free parameter. Across complexes they are equal, and MEASURED not to matter "
            "-- see `measured` below. Five instruments are NOT weighted equally because they are "
            "0.59 to 0.91 correlated with one another: five equal votes would count one factor "
            "five times."),
        # THE MEASUREMENT, CARRIED WITH THE SCORE. 502 sessions, Sep 2024 - Sep 2026.
        # Full write-up in `Claude outputs/RESULTS-crossasset-500-sessions.md`.
        "measured": {
            "sessions": 502,
            "scored_next_sessions": 492,
            "rho_composite_vs_next_return": -0.052,
            "rho_composite_vs_next_range": -0.006,
            "rho_conflict_vs_next_range": -0.016,
            "one_standard_error_on_rho": 0.045,
            "agreement_strength_median": 0.947,
            "complexes_same_sign_share": 0.85,
            "weight_scan_rho_range": [-0.037, -0.061],
            "correlation_within_complex_median": 0.784,
            "correlation_across_complex_median": 0.718,
            "verdict": (
                "NO measurable information about the next session. Every predictor sits inside "
                "1.5 standard errors of zero and the composite beats no single instrument in the "
                "set. The structural reason is that the books almost always agree: the two "
                "complexes share a sign on 85% of sessions and agreement runs a median 0.947, so "
                "there is little disagreement to surface. Weights barely move the composite "
                "because the inputs are 0.72-plus correlated; an eleven-value scan spans 0.024 of "
                "rho against a standard error of 0.045. DO NOT FIT THE WEIGHTS."),
            "caveat_to_the_grouping": (
                "The two-complex grouping is only weakly supported. QQQ correlates MORE with SPXW "
                "(0.857) and SPY (0.866) than with its own complex partner NQ (0.746), so the "
                "'same underlying, near-tautological' rationale is wrong on this data -- a futures "
                "options book and a retail ETF book genuinely differ in positioning. The DECISION "
                "not to weight five instruments equally survives on the correlation structure "
                "alone."),
        },
        "unit_note": ("Sizes are dollar gamma per 1% move: contracts of delta per 1% times the "
                      "reference price times the multiplier (20 for NQ, 100 for the equity roots). "
                      "Contract gamma is never compared across instruments."),
        "config": asdict(cfg),
    }


def from_analyses(analyses, cfg=None, **kw):
    """
    Convenience: build the `instruments` dict from {root: (out, books, spot, F_ref, em)}
    tuples, where `out` is trinity_db.analyse's first return.

    Each instrument contributes its at-spot gamma inside one expected move, its
    gross, its flip distance in expected moves and its skew spread -- the same
    quantities regime.py measures, so one instrument's state here and its state
    in the NQ report are the same measurement.
    """
    import trinity_db
    import regime as regime_mod

    inst = {}
    for root, (out, books, spot, F_ref, em) in analyses.items():
        g, _ = trinity_db.strike_metrics(books, S_shift=spot - F_ref)
        win = em if em else 0.0
        at_spot = sum(v for k, v in g.items() if abs(k - spot) <= win) if win else 0.0
        gross = sum(abs(v) for v in g.values())
        h = ((out.get("panels") or {}).get("FULL") or {}).get("hvl") or []
        fd = ((spot - h[0]) / em) if (h and em) else None
        st = regime_mod.market_state(out, books, spot, F_ref, em)
        sk = (st["dimensions"].get("vol_state") or {}).get("skew") or {}
        inst[root] = {
            "gamma_at_spot": at_spot,
            "gamma_gross": gross,
            "reference": out.get("ref_settle") or spot,
            "flip_distance_ems": fd,
            "skew_spread": sk.get("spread"),
            "stale": "stale" in (out.get("last_at") or ""),
        }
    return agreement(inst, cfg=cfg, **kw)
