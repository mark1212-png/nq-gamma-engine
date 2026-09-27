#!/usr/bin/env python3
"""
regime.py -- the deterministic market-state layer.

    import regime
    st = regime.market_state(out, books, spot, F_ref, em)

Several SEPARATE state dimensions, each with a label, a continuous score, a
confidence and the evidence behind it. Not one summary word, and not an LLM
classifier: every number here is computed.

--------------------------------------------------------------------------------
WHAT THIS LAYER IS ALLOWED TO SAY, AND WHAT IT IS NOT
--------------------------------------------------------------------------------

It describes STATE. It does not predict range and it does not predict direction.

That restriction is measured, not stylistic. The 60-session backtest recorded in
HANDOFF.md tested whether the gamma sign implies a compressed or expanded range
and found no support at p = 0.46; the same file records the overreach it produced,
a "compression, not trend" call on a day that ran 460 points. HANDOFF's standing
instruction is to write structure and location confidently and regime claims
tentatively or not at all. So each dimension here reports what was measured and
how certain the measurement is, and no dimension carries a field a caller could
read as a range or direction forecast.

--------------------------------------------------------------------------------
THE TWO GAMMA MEASURES DISAGREE, AND THAT IS THE POINT OF THE FIRST DIMENSION
--------------------------------------------------------------------------------

HANDOFF records this from the 2026-09-09 intraday build: chain-wide the near and
full books were +85 and +457 while at spot they were -524 and -548, and price sat
above the flips, which read alone says dealers are long where at-spot says short.
Both went on the sheet, labelled.

`gamma_regime` therefore reports BOTH and flags whether they agree, rather than
choosing one and hiding the conflict. Disagreement is a state, and a common one:
the chain-wide column is dominated by far strikes carrying no hedging flow today.

--------------------------------------------------------------------------------
CONFIDENCE MEANS MEASUREMENT UNCERTAINTY
--------------------------------------------------------------------------------

Not "how strong is the signal". Each dimension's confidence is built from:

  * how far the measure sits from the boundary it was classified against -- a
    reading sitting on a threshold is genuinely uncertain and says so
  * whether the inputs were complete: risk coverage, excluded books, whether the
    open interest is preliminary, whether the reference print is stale
  * sample size, where a claim is made against a historical distribution

A heuristic firing is not evidence of certainty. Nothing here returns 0.95 because
a branch was taken.

--------------------------------------------------------------------------------
UNITS
--------------------------------------------------------------------------------

gamma            contracts of delta per 1% move
vanna            contracts of delta per vol point
distances        points of the reference contract, and in expected moves
iv               decimal (0.1306 = 13.06%), never percent
scores           dimensionless, [0, 1] for magnitudes, [-1, 1] where signed

Written 2026-09-09, Phase D of the market-state upgrade.
"""

import math
from dataclasses import dataclass, asdict

import expiry as expiry_mod
import topology


@dataclass
class Config:
    at_spot_ems: float = 1.0      # "at spot" = strikes within this many expected moves
    strong: float = 0.60          # |score| at or above this reads as a strong regime
    weak: float = 0.25            # |score| at or below this reads as flat/neutral
    near_flip_ems: float = 0.25   # inside this many expected moves of a flip is "at the flip"
    conc_high: float = 0.45       # top-bucket or top-node share at or above this is concentrated
    conc_low: float = 0.20
    # SKEW IS SAMPLED IN STANDARD DEVIATIONS, NOT IN FIXED MONEYNESS.
    # A fixed 3% is about 1 sigma on a 30-session book and about 10 sigma on a
    # 0DTE one, where the smile is pure wing extrapolation. Measured on the
    # 2026-09-08 front book, sampling at fixed 0.03 log-moneyness returned puts
    # 0.2096 against calls 0.0777 -- a 13-vol-point "skew" read entirely off the
    # extrapolated wings. One standard deviation, sigma * sqrt(T), is comparable
    # across tenors, which is the same reasoning behind quoting 25-delta skew.
    skew_sd: float = 1.0
    # Term structure for an INTRADAY plan. The board lists expiries out past two
    # years; comparing the front against an 841-session book measures the equity
    # vol curve, not this session's term structure.
    term_max_dte: int = 30
    zero_dte_high: float = 0.35   # 0DTE gross share at or above this dominates
    zero_dte_low: float = 0.10

    def merged(self, **kw):
        d = asdict(self)
        d.update({k: v for k, v in kw.items() if v is not None and k in d})
        return Config(**d)


def _band(x, cfg):
    """Label a signed, normalised score. Returns (label, distance from the nearest
    boundary) -- the second value is what makes the confidence honest."""
    a = abs(x)
    if a >= cfg.strong:
        return ("strong", a - cfg.strong)
    if a <= cfg.weak:
        return ("flat", cfg.weak - a)
    return ("moderate", min(a - cfg.weak, cfg.strong - a))


def _conf(margin, span, *penalties):
    """Confidence from distance to the classification boundary, then reduced by
    data-quality penalties. Deliberately capped below 1.0: a deterministic
    calculation on complete data is still a measurement of a noisy book."""
    base = 0.5 + 0.45 * min(1.0, margin / span) if span > 0 else 0.5
    for p in penalties:
        base *= (1.0 - p)
    return round(max(0.05, min(0.95, base)), 3)


def _norm_tanh(x, scale):
    """Squash an unbounded quantity into (-1, 1) without a hard clip, so a very
    large reading stays distinguishable from a merely large one."""
    if not scale:
        return 0.0
    return math.tanh(x / scale)


# ------------------------------------------------------------ dimensions ---

def gamma_regime(out, books, spot, F_ref, em, cfg, penalties):
    """Sign and strength of dealer gamma, on BOTH measures, with agreement."""
    import trinity_db
    g, _ = trinity_db.strike_metrics(books, S_shift=spot - F_ref)
    if not g:
        return {"label": "unknown", "score": None, "confidence": 0.05,
                "evidence": ["no exposure in this book"]}

    win = cfg.at_spot_ems * em if em else 0.0
    at_spot = sum(v for k, v in g.items() if abs(k - spot) <= win) if win else 0.0
    chain = sum(g.values())
    gross = sum(abs(v) for v in g.values()) or 1.0

    # Normalise each against the gross exposure of the SAME set, so the two are
    # on one scale. A raw comparison of +457 against -548 is not a comparison.
    gross_at_spot = sum(abs(v) for k, v in g.items() if abs(k - spot) <= win) or 1.0
    s_spot = max(-1.0, min(1.0, at_spot / gross_at_spot))
    s_chain = max(-1.0, min(1.0, chain / gross))

    agree = (s_spot > 0) == (s_chain > 0)
    label, margin = _band(s_spot, cfg)
    sign = "positive" if s_spot > 0 else "negative" if s_spot < 0 else "flat"

    ev = [f"at spot (within {cfg.at_spot_ems:g} EM): {at_spot:+,.0f} of {gross_at_spot:,.0f} gross",
          f"chain-wide column: {chain:+,.0f} of {gross:,.0f} gross"]
    if not agree:
        ev.append("THE TWO MEASURES DISAGREE IN SIGN. The chain-wide column is dominated by "
                  "far strikes carrying no hedging flow today; at-spot is what governs hedging.")
    return {
        "label": f"{sign}" if label != "flat" else "flat",
        "strength": label,
        "score": round(s_spot, 4),
        "at_spot": round(at_spot, 1),
        "chain_wide": round(chain, 1),
        "chain_wide_score": round(s_chain, 4),
        "measures_agree": bool(agree),
        # A disagreement is a genuine reduction in how well the book is characterised.
        "confidence": _conf(margin, cfg.strong, *penalties, 0.0 if agree else 0.30),
        "basis": "sign of net dealer gamma at spot, normalised by gross exposure over the same strikes",
        "evidence": ev,
    }


def concentration(out, books, spot, F_ref, em, cfg, penalties, topo=None):
    """Is the book's exposure gathered into a few places or spread out?"""
    topo = topo if topo is not None else topology.from_books(books, spot, F_ref, em=em)
    feats = [f for f in topo.get("features", []) if f["kind"] in ("wall", "shelf")]
    if not feats:
        return {"label": "unknown", "score": None, "confidence": 0.05,
                "evidence": ["no structural formation in the window"]}
    top = max(f["strength"] for f in feats)
    top3 = sum(sorted((f["strength"] for f in feats), reverse=True)[:3])
    label = ("high" if top3 >= cfg.conc_high else
             "low" if top3 <= cfg.conc_low else "moderate")
    margin = min(abs(top3 - cfg.conc_high), abs(top3 - cfg.conc_low))
    return {
        "label": label,
        "score": round(min(1.0, top3), 4),
        "largest_formation_share": round(top, 4),
        "n_formations": len(feats),
        "confidence": _conf(margin, 0.15, *penalties),
        "basis": "combined share of window gross exposure held by the three largest formations",
        "evidence": [f"{len(feats)} formations; largest holds {top:.1%} of window gross",
                     f"top three hold {top3:.1%}"],
    }


def flip_state(out, spot, em, cfg, penalties):
    """Where price sits relative to each book's zero crossing.

    LOCATION ONLY. HANDOFF is explicit that the flip's range claim is unsupported
    at p = 0.46, so nothing here says what the tape will do either side of it.
    """
    panels = out.get("panels") or {}
    per = {}
    nearest = None
    for name in ("0DTE", "NEAR", "FULL"):
        h = ((panels.get(name) or {}).get("hvl")) or []
        if not h:
            per[name] = {"flip": None, "note": "no crossing in the swept range"}
            continue
        f = h[0]
        d = spot - f
        dm = (d / em) if em else None
        per[name] = {"flip": f, "distance": round(d, 2),
                     "distance_ems": round(dm, 4) if dm is not None else None,
                     "side": "above" if d > 0 else "below" if d < 0 else "on",
                     "crossings": len(h)}
        if dm is not None and (nearest is None or abs(dm) < abs(nearest)):
            nearest = dm

    if nearest is None:
        return {"label": "unknown", "per_book": per, "confidence": 0.05,
                "evidence": ["no flip within the swept range in any book"]}
    # POSITION AND PROXIMITY ARE TWO FACTS, NOT ONE.
    #
    # Price can be above every flip AND sitting right on the nearest one; those
    # are different statements and a single label can only carry one of them. An
    # earlier version let "at the flip" overwrite the side, which silently
    # discarded the position of the book relative to price -- the thing the strip
    # on the sheet has always led with.
    at = abs(nearest) <= cfg.near_flip_ems
    sides = {v["side"] for v in per.values() if v.get("side")}
    label = ("above every flip" if sides == {"above"} else
             "below every flip" if sides == {"below"} else
             "on the flip" if sides == {"on"} else
             "inside the flip band")
    margin = abs(abs(nearest) - cfg.near_flip_ems)
    return {
        "label": label,
        "at_flip": bool(at),
        "at_flip_threshold_ems": cfg.near_flip_ems,
        "nearest_distance_ems": round(nearest, 4),
        "per_book": per,
        "confidence": _conf(margin, cfg.near_flip_ems, *penalties),
        "basis": "location of the reference price against each book's nearest zero crossing",
        "caveat": ("Location only. The 60-session backtest found no support for a gamma sign "
                   "implying a compressed or expanded range (p=0.46)."),
        "evidence": [f"{n}: {v['flip']:,.1f}, price {v['side']} by {abs(v['distance']):,.0f} pts"
                     for n, v in per.items() if v.get("flip") is not None],
    }


def expiry_state(dom, cfg, penalties):
    """How much of the board is front-dated."""
    if not dom or not dom.get("zero_dte"):
        return {"label": "unknown", "confidence": 0.05, "evidence": ["no expiry data"]}
    z = dom["zero_dte"]
    if not z.get("present"):
        return {"label": "no 0DTE book", "score": 0.0, "confidence": 0.9,
                "basis": "nothing expires on the report date",
                "evidence": [z.get("note", "")],
                "caveat": "EMPTY, not flat: dealers are not neutral, there is simply no book."}
    gs = z.get("gross_share") or 0.0
    ns = z.get("share") or 0.0
    label = ("dominant" if gs >= cfg.zero_dte_high else
             "minor" if gs <= cfg.zero_dte_low else "moderate")
    margin = min(abs(gs - cfg.zero_dte_high), abs(gs - cfg.zero_dte_low))
    tot = dom.get("totals") or {}
    return {
        "label": label,
        "score": round(gs, 4),
        "gross_share": round(gs, 4),
        "net_share": round(ns, 4),
        "oi_share": z.get("oi_share"),
        "confidence": _conf(margin, 0.12, *penalties),
        "basis": "0DTE share of gross gamma; the net-basis share is reported beside it",
        "evidence": [f"0DTE gross share {gs:.1%}, net-basis share {ns:.1%}",
                     f"board-wide, {tot.get('abs_over_gross')} of gross gamma survives "
                     f"cancellation within expiries"],
    }


def vanna_state(out, books, spot, F_ref, em, cfg, penalties):
    """Net vanna: how dealer delta moves when implied vol moves.

    This is what vanna actually describes, and why nodescore.py deliberately
    excludes it from node quality -- there it would double-count distance.
    """
    import trinity_db
    _, v = trinity_db.strike_metrics(books, S_shift=spot - F_ref)
    if not v:
        return {"label": "unknown", "score": None, "confidence": 0.05,
                "evidence": ["no vanna in this book"]}
    net = sum(v.values())
    gross = sum(abs(x) for x in v.values()) or 1.0
    s = max(-1.0, min(1.0, net / gross))
    label, margin = _band(s, cfg)
    sign = "positive" if s > 0 else "negative" if s < 0 else "flat"
    return {
        "label": sign if label != "flat" else "flat",
        "strength": label,
        "score": round(s, 4),
        "net": round(net, 1),
        "gross": round(gross, 1),
        "unit": "contracts of delta per vol point",
        "confidence": _conf(margin, cfg.strong, *penalties),
        "basis": "net vanna normalised by gross vanna",
        "evidence": [f"net {net:+,.0f} against {gross:,.0f} gross, contracts of delta per vol point"],
    }


def vol_state(out, books, cfg, penalties, iv_history=None):
    """ATM level, term structure and skew.

    `iv_history` is an optional list of past ATM vols. WITHOUT it no percentile
    claim is made and the confidence says so -- calling a level "elevated" with
    no distribution behind it is exactly the fake certainty the brief forbids.
    """
    iv = out.get("iv_atm")
    front = books[0] if books else None
    res = {"atm_iv": iv, "unit": "decimal", "evidence": []}

    if iv is None:
        return {"label": "unknown", "confidence": 0.05, "evidence": ["no ATM vol solved"]}
    res["evidence"].append(f"front ATM vol {iv:.4f} on the business-time basis")

    # Term structure over the horizon an intraday plan can act on. `term_max_dte`
    # keeps a two-year quarterly out of a number meant to describe this session.
    by = out.get("by_expiry") or []
    dated = [b for b in by if b.get("atm_iv")]
    inhoriz = [b for b in dated if b["dte"] <= cfg.term_max_dte] or dated
    if len(inhoriz) >= 2:
        near = min(inhoriz, key=lambda b: b["dte"])
        far = max(inhoriz, key=lambda b: b["dte"])
        slope = far["atm_iv"] - near["atm_iv"]
        res["term_structure"] = {
            "front_dte": near["dte"], "front_iv": near["atm_iv"],
            "back_dte": far["dte"], "back_iv": far["atm_iv"],
            "horizon_dte": cfg.term_max_dte,
            "slope": round(slope, 6),
            "label": "contango" if slope > 0.005 else "backwardation" if slope < -0.005 else "flat",
            "note": (f"measured inside {cfg.term_max_dte} sessions; the board lists expiries out to "
                     f"{max(b['dte'] for b in dated)} sessions, which is a different question"),
        }
        res["evidence"].append(
            f"term structure {near['dte']}d {near['atm_iv']:.4f} to {far['dte']}d {far['atm_iv']:.4f}")

    # Skew, sampled at +/- one standard deviation of the FRONT BOOK'S OWN move,
    # so the sample point sits where the smile was actually solved rather than
    # out in its extrapolated wing. See Config.skew_sd.
    if front is not None and getattr(front.get("smile"), "x", None):
        sm = front["smile"]
        sd = (front.get("atm") or iv) * math.sqrt(max(front.get("T") or 0.0, 1e-12))
        m = cfg.skew_sd * sd
        knots = sm.x
        extrapolated = bool(knots) and (-m < knots[0] or m > knots[-1])
        put_side, call_side = sm(-m), sm(+m)
        res["skew"] = {
            "sample_sd": cfg.skew_sd,
            "sample_moneyness": round(m, 6),
            "put_side_iv": round(put_side, 6),
            "call_side_iv": round(call_side, 6),
            "spread": round(put_side - call_side, 6),
            "extrapolated": extrapolated,
            "label": ("puts bid" if put_side - call_side > 0.005
                      else "calls bid" if put_side - call_side < -0.005 else "flat"),
        }
        res["evidence"].append(
            f"skew at {cfg.skew_sd:g} sd ({m:.4f} log-moneyness): "
            f"puts {put_side:.4f} vs calls {call_side:.4f}"
            + (" -- SAMPLED BEYOND THE SOLVED KNOTS, this is wing extrapolation" if extrapolated else ""))
        # SUPERSEDED 2026-09-10. This used to stamp "rho +0.23, p 0.08" from a
        # 63-session test and call it a weak link. Re-run on 493 sessions with the
        # endpoint fixed in advance, the link is GONE, and the earlier reading was
        # a volatility confound: skew correlates with the ATM level at rho +0.595,
        # and the ATM level is itself weakly NEGATIVE against range. Stratifying by
        # vol kills it in every tercile, and it is null in all four walk-forward
        # folds. The tercile medians are flat -- 1.185 / 1.142 / 1.152 EM going from
        # calls-bid to puts-bid -- so there is no effect size to trade even if the
        # sign were stable. The old text is kept in the sentence below because a
        # caveat that quietly changes its number teaches nothing; the point is that
        # the prior did not survive.
        # notes/tests/2026-09-10-skew-range/study.py
        res["skew_caveat"] = ("Measured on 493 sessions, skew against next-session range is "
                              "rho -0.036, p 0.43 -- null in every vol tercile and in all four "
                              "walk-forward folds, with tercile medians flat at 1.185 / 1.142 / "
                              "1.152 EM. Direction is null too (rho +0.05, p 0.28). An earlier "
                              "63-session read of rho +0.23, p 0.08 DID NOT SURVIVE the larger "
                              "sample; it was a vol confound (skew vs ATM IV rho +0.595). "
                              "Stamp it; never trade it as a rule.")

    if iv_history:
        h = sorted(x for x in iv_history if x)
        if len(h) >= 20:
            below = sum(1 for x in h if x < iv)
            pct = below / len(h)
            res["percentile"] = round(pct, 4)
            res["n_history"] = len(h)
            res["label"] = ("elevated" if pct >= 0.75 else "subdued" if pct <= 0.25 else "ordinary")
            margin = min(abs(pct - 0.75), abs(pct - 0.25))
            # A percentile from a short history is a weak claim; scale by sample size.
            small = max(0.0, 1.0 - len(h) / 250.0) * 0.5
            res["confidence"] = _conf(margin, 0.25, *penalties, small)
            res["evidence"].append(f"{pct:.0%} of {len(h)} historical sessions sat below this level")
            return res
        res["evidence"].append(f"history too short to rank ({len(h)} sessions, 20 needed)")

    res["label"] = "level only"
    res["percentile"] = None
    res["confidence"] = 0.25
    res["note"] = ("No historical distribution supplied, so no percentile claim is made. "
                   "Calling a level elevated without one would be a heuristic wearing a number.")
    return res


# ------------------------------------------------------------ data quality ---

def data_quality(out, penalties_out=None):
    """What the state was measured ON. Feeds every dimension's confidence."""
    issues, pen = [], []
    cov = out.get("risk_coverage")
    excl = out.get("excluded_risk_books") or []
    if excl:
        issues.append(f"{len(excl)} book(s) excluded for lack of a usable IV curve")
        pen.append(min(0.25, 0.05 * len(excl)))
    if out.get("oi_vintage") == "preliminary":
        issues.append("open interest is PRELIMINARY and will be revised")
        pen.append(0.10)
    if out.get("no_0dte"):
        issues.append("nothing expires on the report date; the 0DTE book is empty")
    if "stale" in (out.get("last_at") or ""):
        issues.append("the reference print is stale")
        pen.append(0.25)
    score = 1.0
    for p in pen:
        score *= (1.0 - p)
    return {
        "score": round(score, 4),
        "coverage": cov,
        "oi_vintage": out.get("oi_vintage"),
        "expiries": out.get("expiries"),
        "excluded_books": excl,
        "issues": issues or ["no data-quality issue detected"],
        "penalties": [round(p, 3) for p in pen],
    }


# ----------------------------------------------------------------- top level ---

def market_state(out, books, spot, F_ref, em, cfg=None, iv_history=None,
                 topo=None, dom=None, **kw):
    """
    Every state dimension, computed. `out` is trinity_db.analyse's first return.

    There is deliberately NO overall label and NO single confidence number. The
    dimensions are separate because they genuinely are separate, and collapsing
    them would recreate the one-word regime call the backtest found unsupported.
    """
    cfg = (cfg or Config()).merged(**kw)
    dq = data_quality(out)
    pen = dq["penalties"]

    if not books:
        return {"dimensions": {}, "data_quality": dq, "note": "empty book"}

    topo = topo if topo is not None else topology.from_books(books, spot, F_ref, em=em)
    dom = dom if dom is not None else expiry_mod.dominance(books, spot, F_ref)

    dims = {
        "gamma_regime": gamma_regime(out, books, spot, F_ref, em, cfg, pen),
        "concentration": concentration(out, books, spot, F_ref, em, cfg, pen, topo=topo),
        "flip_state": flip_state(out, spot, em, cfg, pen),
        "expiry_state": expiry_state(dom, cfg, pen),
        "vanna_state": vanna_state(out, books, spot, F_ref, em, cfg, pen),
        "vol_state": vol_state(out, books, cfg, pen, iv_history=iv_history),
    }
    return {
        "dimensions": dims,
        "data_quality": dq,
        "spot": spot,
        "em": em,
        "charm_state": ("not computed: charm is not implemented in nqvol and was measured "
                        "worse than chance over 63 sessions"),
        "cross_asset_agreement": ("not computed here: see crossasset.py, which needs the "
                                  "equity books and is a separate layer"),
        "no_overall_label": ("Deliberate. The dimensions are separate because they are separate; "
                             "collapsing them into one word recreates the range/regime call the "
                             "60-session backtest found unsupported at p=0.46."),
        "config": asdict(cfg),
    }
