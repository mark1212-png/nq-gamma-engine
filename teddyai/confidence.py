#!/usr/bin/env python3
"""
confidence.py -- separate confidence dimensions, none of them a probability.

    import confidence
    c = confidence.decompose(state=st, topo=t, nodes=n, scenario=sc)

Mark's brief: do NOT create one fake "AI confidence = 87%" number. So there is no
single number here. There are named dimensions, each built from something
measurable, each carrying the evidence that produced it and the reason it is not
higher.

--------------------------------------------------------------------------------
NOTHING HERE IS A PROBABILITY
--------------------------------------------------------------------------------

Every value is stamped `calibrated: false` and the word "probability" appears
nowhere in the output schema. A deterministic score is a probability only after
it has been checked against outcomes, and this engine has exactly one such
figure: node reactions at 58.7% against a measured placebo of 55.8%, p = 0.059.
That is a real but small edge on ONE feature, and it is not a calibration of
anything computed here.

Phase G builds the historical validation that could calibrate these. Until it
does, `calibrated` stays false and a caller that wants to multiply these numbers
together as if they were independent probabilities is doing something the schema
warns against.

--------------------------------------------------------------------------------
THE DIMENSIONS
--------------------------------------------------------------------------------

data_quality     Completeness of the inputs. Excluded books, preliminary open
                 interest, a stale reference print. Computed by regime.py and
                 carried through, because everything else is measured ON this.

structural       How well defined the book's shape is. A book with three clear
                 formations holding most of the exposure is structurally legible;
                 one with eleven overlapping shelves and no dominant feature is
                 not, whatever its gamma total says.

level            For one price: how well anchored it is. The node's quality
                 score, whether it sits inside a formation, and whether it rests
                 on one open-interest line or on many -- METHOD.md section 6
                 records a "major call wall" that was 82% a single 1,556-lot line.

regime           Agreement across the market-state dimensions. Deliberately the
                 MINIMUM rather than the mean: a state description is only as
                 trustworthy as its weakest measured dimension, and averaging
                 lets a confident vol reading paper over a gamma sign the two
                 measures disagree about.

directional      Almost always None, and that is the point. Gamma structure
                 carries no direction -- the 63-session study found the one
                 directional reading it invites to be an artefact of node
                 density. This dimension stays None unless a genuinely
                 directional input is supplied, which today means cross-asset
                 agreement, and it says so rather than quietly defaulting to 0.5.

scenario         How well specified one conditional path is: does it have a
                 trigger, targets with structure behind them, and an
                 invalidation. NOT how likely it is to happen.

Written 2026-09-09, Phase E of the market-state upgrade.
"""

from dataclasses import dataclass, asdict


@dataclass
class Config:
    struct_good_share: float = 0.45   # top three formations holding this share reads as legible
    struct_many: int = 12             # more formations than this reads as fragmented
    dominance_warn: float = 0.50      # one strike holding this share of gross is a single-line node
    max_confidence: float = 0.90      # nothing uncalibrated is allowed above this
    measured_null_scale: float = 0.60 # the level dimension is scaled by this when the score it reads
                                      # was MEASURED not to rank reactions (nodes.measured, 2026-09-12)

    def merged(self, **kw):
        d = asdict(self)
        d.update({k: v for k, v in kw.items() if v is not None and k in d})
        return Config(**d)


def _dim(value, evidence, limits=None, cfg=None):
    """One dimension. `limits` says why it is not higher, which is the part a
    reader actually needs."""
    cfg = cfg or Config()
    v = None if value is None else round(max(0.0, min(cfg.max_confidence, value)), 3)
    return {
        "value": v,
        "calibrated": False,
        "evidence": evidence,
        "limited_by": limits or [],
    }


def structural_confidence(topo, nodes=None, cfg=None):
    """Is the book's shape legible?"""
    cfg = cfg or Config()
    feats = [f for f in (topo or {}).get("features", []) if f["kind"] in ("wall", "shelf")]
    ev, lim = [], []
    if not feats:
        return _dim(0.05, ["no structural formation in the analysis window"],
                    ["nothing to describe"], cfg)

    top3 = sum(sorted((f["strength"] for f in feats), reverse=True)[:3])
    ev.append(f"{len(feats)} formations; the three largest hold {top3:.1%} of window gross")

    v = 0.35 + 0.5 * min(1.0, top3 / cfg.struct_good_share)
    if len(feats) > cfg.struct_many:
        v *= 0.8
        lim.append(f"{len(feats)} formations is fragmented; structure is spread rather than concentrated")
    if top3 < cfg.struct_good_share:
        lim.append(f"top three hold under {cfg.struct_good_share:.0%} of gross exposure")

    n_str = (topo or {}).get("n_strikes")
    if n_str is not None and n_str < 20:
        v *= 0.7
        lim.append(f"only {n_str} listed strikes fed the window")

    if (topo or {}).get("window_truncated_by_coverage"):
        v *= 0.9
        lim.append("the window was truncated by the model's own moneyness cap; "
                   "structure beyond it was not measured")

    if nodes and nodes.get("dominance", {}).get("share", 0) >= cfg.dominance_warn:
        d = nodes["dominance"]
        v *= 0.75
        lim.append(f"one strike ({d['price']:,.0f}) holds {d['share']:.0%} of the window's gross "
                   f"exposure; the shape rests on a single line")
    return _dim(v, ev, lim, cfg)


def level_confidence(node, nodes_result=None, cfg=None):
    """How well anchored ONE price is."""
    cfg = cfg or Config()
    if not node:
        return _dim(None, ["no node supplied"], ["nothing to assess"], cfg)
    ev, lim = [], []
    q = node.get("quality") or 0.0
    ev.append(f"node quality {q:.2f} at {node['price']:,.0f}")
    v = 0.3 + 0.6 * q

    topo = node.get("topology") or {}
    if not topo.get("kind"):
        v *= 0.7
        lim.append("the strike sits inside no topology formation; it is a lone value, not structure")
    else:
        ev.append(f"inside a {topo['kind']}" + (f" ({', '.join(topo['labels'])})" if topo.get("labels") else ""))

    raw = node.get("raw") or {}
    exp = raw.get("expiries_material_in")
    if exp is not None:
        ev.append(f"material in {exp} live expiries")
        if exp <= 1:
            v *= 0.8
            lim.append("material in only one expiry; METHOD.md section 6 records a 'major call wall' "
                       "that was one 1,838-lot line in a 23-DTE book, irrelevant intraday")

    d = raw.get("distance_ems")
    if d is not None and abs(d) > 1.5:
        v *= 0.85
        lim.append(f"{abs(d):.2f} expected moves away; the session may not reach it")

    if nodes_result and not nodes_result.get("weights_validated", False):
        lim.append("the quality score's weights are equal and unfitted")

    # THE MEASUREMENT REACHES THE ARITHMETIC (2026-09-12). Until now the 500-session
    # null lived in the caveats prose while this dimension went on scaling with a
    # quality score that had been measured NOT to rank reaction probability. A
    # null result is not a calibration, so `calibrated` stays False; but it is a
    # ceiling on what this number may claim, and it says so in the limits.
    m = (nodes_result or {}).get("measured") or {}
    z = m.get("edge_z")
    if z is not None and abs(z) < 2.0:
        v *= cfg.measured_null_scale
        lim.append(f"MEASURED, not assumed: over {m.get('sessions')} sessions and "
                   f"{(m.get('node_touches_resolved') or 0):,} touches the quality score did not rank "
                   f"reaction probability (edge {m.get('edge_pp', 0):+.1f}pp against a distance-matched "
                   f"placebo, z={z:+.2f}); this value says how well the level is anchored in the book, "
                   f"not whether it holds")
        if "unmeasured" in str(m.get("scored_with", "")):
            lim.append("the score in use has changed since that measurement and is itself unmeasured "
                       "(nodes.measured.scored_with)")
    return _dim(v, ev, lim, cfg)


def regime_confidence(state, cfg=None):
    """Agreement across the market-state dimensions.

    The MINIMUM, not the mean. A state description is only as trustworthy as its
    weakest measured dimension, and a mean lets a confident vol reading paper
    over a gamma sign whose two measures disagree.
    """
    cfg = cfg or Config()
    dims = (state or {}).get("dimensions") or {}
    vals = {n: d.get("confidence") for n, d in dims.items() if d.get("confidence") is not None}
    if not vals:
        return _dim(None, ["no market-state dimension was measured"], ["nothing to assess"], cfg)

    worst = min(vals, key=vals.get)
    v = vals[worst]
    ev = [f"{len(vals)} dimensions measured; weakest is {worst} at {vals[worst]:.2f}",
          "the minimum is used, not the mean: a state read is only as good as its weakest dimension"]
    lim = [f"{worst} is the binding constraint"]

    g = dims.get("gamma_regime") or {}
    if g.get("measures_agree") is False:
        lim.append("the at-spot and chain-wide gamma measures disagree in sign")
    dq = (state or {}).get("data_quality") or {}
    for i in dq.get("issues", []):
        if "no data-quality issue" not in i:
            lim.append(i)
    return _dim(v, ev, lim, cfg)


def directional_confidence(cross_asset=None, cfg=None):
    """Direction, which gamma structure does not supply.

    Returns None unless something genuinely directional is passed in. It does NOT
    default to 0.5, because a neutral-looking number reads as a weak opinion when
    the truth is that no opinion was formed.
    """
    cfg = cfg or Config()
    if not cross_asset:
        return _dim(None,
                    ["no directional input was supplied"],
                    ["Gamma structure carries no direction. The 63-session study found the "
                     "one directional reading it invites -- that a break into thin structure "
                     "runs further -- to be an artefact of node density: of 4,354 breaks only "
                     "15 had a gap wider than half an expected move.",
                     "Direction requires price, flow or cross-asset evidence this engine was "
                     "not given."], cfg)
    comp = cross_asset.get("composite")
    agree = cross_asset.get("agreement_strength")
    if comp is None or agree is None:
        return _dim(None, ["cross-asset input present but incomplete"], ["no composite or agreement"], cfg)
    v = 0.2 + 0.6 * float(agree)
    lim = []
    if cross_asset.get("conflict", 0) > 0.3:
        v *= 0.8
        lim.append(f"cross-asset conflict {cross_asset['conflict']:.2f}")
    if not cross_asset.get("weights_validated", False):
        lim.append("cross-asset weights are unvalidated; whether disagreement predicts anything is untested")
    return _dim(v, [f"cross-asset composite {comp:+.2f}, agreement {agree:.2f}"], lim, cfg)


def scenario_confidence(scenario, cfg=None):
    """How well SPECIFIED a conditional path is. Not how likely it is."""
    cfg = cfg or Config()
    if not scenario:
        return _dim(None, ["no scenario supplied"], ["nothing to assess"], cfg)
    ev, lim = [], []
    v = 0.25
    if scenario.get("trigger"):
        v += 0.2
        ev.append("has an explicit trigger")
    else:
        lim.append("no trigger: the path has no defined starting condition")
    tg = scenario.get("targets") or []
    if tg:
        v += 0.15 * min(1.0, len(tg) / 3.0)
        ev.append(f"{len(tg)} target(s), each anchored to a scored node")
    else:
        lim.append("no targets")
    if scenario.get("invalidation"):
        v += 0.2
        ev.append("has an invalidation level")
    else:
        lim.append("no invalidation: the path cannot be proven wrong")
    if scenario.get("opposing_evidence"):
        v += 0.1
        ev.append(f"{len(scenario['opposing_evidence'])} piece(s) of opposing evidence recorded")
    else:
        lim.append("no opposing evidence recorded")
    lim.append("specification only; this says nothing about whether the path occurs")
    return _dim(v, ev, lim, cfg)


def decompose(state=None, topo=None, nodes=None, node=None, scenario=None,
              cross_asset=None, cfg=None, **kw):
    """Every dimension at once. Missing inputs give a None dimension, never a guess."""
    cfg = (cfg or Config()).merged(**kw)
    dq = (state or {}).get("data_quality") or {}
    out = {
        "data_quality": _dim(dq.get("score"),
                             dq.get("issues", ["no data-quality assessment supplied"]),
                             [i for i in dq.get("issues", []) if "no data-quality issue" not in i],
                             cfg),
        "structural": structural_confidence(topo, nodes, cfg),
        "level": level_confidence(node, nodes, cfg),
        "regime": regime_confidence(state, cfg),
        "directional": directional_confidence(cross_asset, cfg),
        "scenario": scenario_confidence(scenario, cfg),
    }
    return {
        "dimensions": out,
        "calibrated": False,
        "no_single_number": (
            "Deliberate. These are separate measurements of separate things and there is no "
            "defensible way to collapse them into one figure. They are also NOT independent "
            "probabilities and must not be multiplied together."),
        "calibration_note": (
            "None of these has been checked against outcomes. The engine's measured figures are "
            "NULLS, not calibrations: on 500 sessions and 4,556 touches scored nodes reacted 56.8% "
            "against 57.0% for a distance-matched placebo (z=-0.17), superseding the 60-session "
            "58.7% vs 55.8% (p=0.059). The one validated component is the rails, 64-74% close "
            "containment against a 68% target, and no dimension here reads them."),
        "measured_inputs": {
            "level": "scaled by nodes.measured when the quality score was measured not to rank reactions",
        },
        "config": asdict(cfg),
    }
