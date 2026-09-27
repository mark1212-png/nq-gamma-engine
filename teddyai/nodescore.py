#!/usr/bin/env python3
"""
nodescore.py -- not every large gamma number is an equally important level.

    import nodescore
    nodes = nodescore.score_nodes(books, spot, F_ref, em)

Returns a ranked list of strikes, each carrying a quality score in [0, 1] AND
the individual components that produced it, so the engine can always say why a
node scored what it did.

--------------------------------------------------------------------------------
2026-09-12: VANNA AND CHARM ENTER THE SCORE, BY DECISION
--------------------------------------------------------------------------------

Mark asked for vanna and charm to be scored, having heard the measured case
against them (kept below as written, because it is still what was measured).
SEVEN components now ship: the original five plus |vanna| and |charm|, at equal
weight like the rest, magnitudes only -- sign is still carried and never
scored. What that means, so nobody reads it as a finding:

  * |vanna| is an inverse-distance measure (-0.728 with proximity), so it pulls
    against proximity: a far strike now gets back part of what proximity took
    off it, and the score's distance weighting is weaker than before.
  * charm is dDelta/dt per session, scored as a magnitude. The 63-session
    result that net charm called DIRECTION 43% of the time was about sign;
    this component never sees the sign. It is simply unmeasured as a quality
    input.
  * the 500-session measurement stamped in `measured` was made on the FIVE-
    component score. The seven-component score has not been run through
    histval.py, and `measured.scored_with` says so.

`Weights(vanna=0.0, charm=0.0)` restores the five-component score exactly, and
a test holds that identity.

--------------------------------------------------------------------------------
WHICH FACTORS SHIPPED ON 2026-09-09, AND WHY THE OBVIOUS ONES DID NOT
--------------------------------------------------------------------------------

Mark's brief was explicit that the weighted sum must not be implemented blindly:
work out which factors are available, which are redundant, and whether
correlated features would double-count. That was measured before this file was
written -- `notes/tests/2026-09-09-node-features/study.py`, 29 sessions, 237
material strikes. Spearman rank correlations:

                 gamma     oi   vanna   prox   persist  cluster   topo
    gamma        1.000  0.442   0.237  0.165    0.370    0.681   0.423
    oi           0.442  1.000   0.409 -0.086    0.551    0.085   0.063
    vanna        0.237  0.409   1.000 -0.728    0.288   -0.165  -0.073
    proximity    0.165 -0.086  -0.728  1.000   -0.044    0.454   0.413
    persistence  0.370  0.551   0.288 -0.044    1.000    0.055   0.088
    cluster      0.681  0.085  -0.165  0.454    0.055    1.000   0.688
    topo         0.423  0.063  -0.073  0.413    0.088    0.688   1.000

FIVE COMPONENTS SHIPPED (seven since 2026-09-12; see the decision block above).

  gamma        normalised |net gamma| at the strike. The base quantity.
  oi           normalised call + put open interest. Only 0.442 with gamma, so it
               carries real separate information -- which is what METHOD.md
               section 7 argues from a different direction: gamma weighting
               discounts a distant strike because it generates little hedging
               flow TODAY, but the strike is still a target and its gamma
               multiplies if price travels to it. Rank both ways.
  proximity    exp(-|K - S| / EM). Nearly independent of gamma at 0.165.
  persistence  share of live expiries in which the strike is material within its
               own book. Cross-expiry agreement.
  structure    strength of the topology formation containing the strike, i.e. its
               share of the window's gross exposure.

TWO WERE EXCLUDED, FOR THE RECORD. Vanna has since been added by decision;
cluster stays out.

  VANNA. Rank correlation with proximity is -0.728, and that is structural, not a
  sample artefact: vanna / gamma = -d2 * F * sqrt(T), so |vanna| is MINIMAL at
  the money and grows with distance. Measured at F 29,000, sigma 0.14, T 0.02 it
  runs 0.028 at the money, 1.29 at +290 and 1.76 at +580. Scoring both vanna and
  proximity would weight distance twice with opposite signs, partly cancelling
  and leaving a score nobody can interpret. Vanna is REPORTED per node as a
  diagnostic and is used by the market-state layer for what it actually
  describes -- spot/vol interaction -- not as a quality weight.

  CLUSTER (raw local mass). 0.681 with gamma and 0.688 with structure. It is the
  same information as those two, smoothed. `structure` keeps the part that is not
  already in `gamma`, because a topology formation's strength reflects the whole
  formation rather than one strike.

CHARM WAS NOT HERE, AND THIS IS WHY. It is now implemented (nqvol.charm) and
scored as a magnitude by decision on 2026-09-12; the measurement below stands.
At the time it was not implemented in nqvol, and
`Claude outputs/RESULTS-1.md` tested it over 63 sessions: net charm called the
day's direction 43% of the time, worse than chance, and the charm "pin" sat
FARTHER from the close than a random material strike (42% and 37% on the two
books). Adding it would import a measured negative into a quality score.

--------------------------------------------------------------------------------
THE WEIGHTS ARE NOT VALIDATED, AND THE OUTPUT SAYS SO
--------------------------------------------------------------------------------

There is no empirical basis yet for weighting one component above another; that
is what the historical-validation phase is for. Equal weights across five
normalised components is the honest default, `weights_validated` is stamped
false on every result, and `Weights` is a plain dataclass so a fitted set can
replace it without touching this logic.

Do not read the score as a probability. It is a ranking device. The one
historical figure that exists says node reactions run 58.7% against a measured
placebo of 55.8%, p = 0.059 -- real but small, and not calibrated to anything
this score outputs.

--------------------------------------------------------------------------------
SIGN IS CARRIED, NEVER SCORED
--------------------------------------------------------------------------------

`quality` is built entirely from magnitudes and distances. The sign of the node's
gamma is reported in `sign` and never enters the score, so a large positive node
and a large negative node of equal size rank equally. Turning "big positive
gamma" into "support" requires evidence this layer does not have, and the schema
is built so that conversion cannot happen by accident.

Units: gamma and vanna are the engine's native contracts of delta per 1% move and
per vol point; oi is contracts; price is points of the reference contract; every
component and the score itself are dimensionless in [0, 1].

Written 2026-09-09, Phase C of the market-state upgrade.
"""

import math
from dataclasses import dataclass, asdict, field

import topology

# ------------------------------------------------------------------ config --

@dataclass
class Weights:
    """Equal by default. NOT fitted -- see the module docstring."""
    gamma: float = 1.0
    oi: float = 1.0
    proximity: float = 1.0
    persistence: float = 1.0
    structure: float = 1.0
    # Added 2026-09-12 by decision. Zero both to recover the five-component score.
    vanna: float = 1.0
    charm: float = 1.0

    def as_dict(self):
        return asdict(self)

    def total(self):
        return sum(asdict(self).values())


@dataclass
class Config:
    material: float = 0.25        # a strike must carry this share of the book's largest |gamma| to be a candidate
    scale_pct: float = 0.95       # component normalisers use this quantile, not the max
    window_ems: float = 2.0       # candidates must sit within this many expected moves of price
    window_min: float = 400.0     # ...or this many points, whichever is wider
    cluster_pts: float = 25.0     # matches command_center.CLUSTER_PTS
    top_n: int = 0                # 0 = return every candidate; the report filters later

    def merged(self, **kw):
        d = asdict(self)
        d.update({k: v for k, v in kw.items() if v is not None and k in d})
        return Config(**d)


def _quantile(xs, q):
    v = sorted(x for x in xs if x is not None)
    if not v:
        return 0.0
    if len(v) == 1:
        return v[0]
    i = q * (len(v) - 1)
    lo = int(math.floor(i))
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def _norm(x, scale):
    """Map a magnitude onto [0, 1] against a robust scale. Values above the scale
    clip at 1 rather than running away, so one outlier cannot compress every
    other node's component into the floor."""
    if scale <= 0:
        return 0.0
    return min(1.0, abs(x) / scale)


# -------------------------------------------------------------- ingredients --

def oi_by_strike(books):
    """Call + put open interest per strike ON THE REFERENCE AXIS.

    A book on another underlying must be basis-shifted the same way
    trinity_db.strike_metrics shifts it, or the open interest and the gamma would
    key on different prices and the join would silently mismatch.
    """
    import trinity_db
    out = {}
    for b in books:
        basis = b.get("basis") or 0.0
        grid = b.get("grid", trinity_db.GRID)
        for ks, rec in b["strikes"].items():
            k = trinity_db.axis_strike(float(ks), basis, grid)
            n = (rec.get("C", {}).get("oi") or 0) + (rec.get("P", {}).get("oi") or 0)
            if n:
                out[k] = out.get(k, 0) + n
    return out


def persistence_by_strike(books, spot, F_ref, material=0.25):
    """Share of live expiries in which a strike is material WITHIN ITS OWN BOOK.

    Measured per book rather than against the aggregate, so a strike that is
    small in absolute terms but dominant in a thin far expiry still counts. That
    is the cross-expiry agreement confluence tries to capture, on a continuous
    scale rather than a 2-or-3 dot.
    """
    import trinity_db
    hits, n = {}, 0
    for b in books:
        g, _ = trinity_db.strike_metrics([b], S_shift=spot - F_ref)
        if not g:
            continue
        n += 1
        mx = max(abs(v) for v in g.values()) or 1.0
        for k, v in g.items():
            if abs(v) >= material * mx:
                hits[k] = hits.get(k, 0) + 1
    return ({k: v / n for k, v in hits.items()}, n) if n else ({}, 0)


# ------------------------------------------------------------------- score --

def score_nodes(books, spot, F_ref, em, weights=None, cfg=None, topo=None, **kw):
    """
    Rank the strikes that matter, and explain every rank.

    books   trinity_db's book list (or a panel subset)
    spot    the reference distances are measured from
    F_ref   the reference future's settle, for the basis shift
    em      expected move, used for the proximity scale and the window

    Returns {"nodes": [...], "scale": {...}, "weights": {...}, ...}. Each node
    carries `quality`, `components`, `sign`, and the raw values behind them.
    """
    import trinity_db

    cfg = (cfg or Config()).merged(**kw)
    w = weights or Weights()

    if not books:
        return {"nodes": [], "n_candidates": 0, "weights": w.as_dict(),
                "weights_validated": False, "note": "empty book"}

    g, v = trinity_db.strike_metrics(books, S_shift=spot - F_ref)
    if not g:
        return {"nodes": [], "n_candidates": 0, "weights": w.as_dict(),
                "weights_validated": False, "note": "no exposure in this book"}

    topo = topo if topo is not None else topology.from_books(books, spot, F_ref, em=em)
    half = max(cfg.window_ems * (em or 0.0), cfg.window_min)
    lo, hi = spot - half, spot + half
    if topo.get("window"):
        lo, hi = max(lo, topo["window"][0]), min(hi, topo["window"][1])

    # THE CANDIDATE THRESHOLD USES THE SAME ROBUST SCALE AS THE COMPONENTS.
    #
    # command_center._material has thresholded against max(|gamma|) since
    # 2026-09-05, and that is fragile in exactly the way this domain produces:
    # METHOD.md section 6 records 29,500 reading as a major call wall on +142 net
    # gamma of which almost all was ONE 1,838-lot line. Against the max, a single
    # outsized line pushes every genuine strike under the bar and the book comes
    # back with one node in it. Measured on a synthetic book with five strikes
    # near 45 contracts and one at 1,000,000, the max rule returned exactly one
    # candidate and discarded the other five.
    #
    # p95 over the strikes carrying exposure inside the window keeps that from
    # happening. Setting scale_pct = 1.0 restores the old max convention exactly,
    # so the divergence is a default rather than a decision taken away.
    in_window = [abs(x) for k, x in g.items() if lo <= k <= hi and x]
    g_ref = _quantile(in_window, cfg.scale_pct) or (max(in_window) if in_window else 1.0)
    cands = [k for k, x in g.items() if lo <= k <= hi and abs(x) >= cfg.material * g_ref]
    if not cands:
        return {"nodes": [], "n_candidates": 0, "weights": w.as_dict(),
                "weights_validated": False,
                "note": "no strike reaches the material threshold inside the window"}

    oi = oi_by_strike(books)
    per, n_books = persistence_by_strike(books, spot, F_ref, cfg.material)
    ch = trinity_db.strike_charm(books, S_shift=spot - F_ref)

    # Normalisers are computed over the CANDIDATE set, not the whole book: the
    # question is which of these matters most, so the far tail must not set the
    # scale. p95 rather than max so a single outlier cannot flatten the rest.
    g_scale = _quantile([abs(g[k]) for k in cands], cfg.scale_pct)
    oi_scale = _quantile([oi.get(k, 0) for k in cands], cfg.scale_pct)
    v_scale = _quantile([abs(v.get(k, 0.0)) for k in cands], cfg.scale_pct)
    c_scale = _quantile([abs(ch.get(k, 0.0)) for k in cands], cfg.scale_pct)
    feat_scale = max([f["strength"] for f in topo.get("features", [])
                      if f["kind"] in ("wall", "shelf")] or [0.0])

    def containing(k):
        best, kind, labels = 0.0, None, []
        for f in topo.get("features", []):
            if f["kind"] in ("wall", "shelf") and f["lower"] <= k <= f["upper"]:
                if f["strength"] >= best:
                    best, kind, labels = f["strength"], f["kind"], f["labels"]
        return best, kind, labels

    nodes = []
    for k in sorted(cands):
        gv = g[k]
        vv = v.get(k, 0.0)
        cv = ch.get(k, 0.0)
        strength, kind, labels = containing(k)
        near = sum(abs(x) for kk, x in g.items() if abs(kk - k) <= cfg.cluster_pts)

        comp = {
            "gamma": _norm(gv, g_scale),
            "oi": _norm(oi.get(k, 0), oi_scale),
            "proximity": math.exp(-abs(k - spot) / em) if em else 0.0,
            "persistence": per.get(k, 0.0),
            "structure": (strength / feat_scale) if feat_scale > 0 else 0.0,
            # By decision, 2026-09-12. Magnitudes against the same p95 scale as
            # gamma and oi; the signs stay in `raw` and never enter the score.
            "vanna": _norm(vv, v_scale),
            "charm": _norm(cv, c_scale),
        }
        comp = {a: min(1.0, max(0.0, b)) for a, b in comp.items()}
        wd = w.as_dict()
        total = w.total() or 1.0
        quality = sum(comp[a] * wd[a] for a in comp) / total

        nodes.append({
            "price": k,
            "quality": round(quality, 4),
            "components": {a: round(b, 4) for a, b in comp.items()},
            # SIGN IS CARRIED, NOT SCORED. See the module docstring.
            "sign": 1 if gv > 0 else -1 if gv < 0 else 0,
            "raw": {
                "gamma": round(gv, 3),
                "vanna": round(vv, 3),
                "charm": round(cv, 4),
                "oi": int(oi.get(k, 0)),
                "distance": round(k - spot, 2),
                "distance_ems": round((k - spot) / em, 3) if em else None,
                "cluster_mass": round(near, 3),
                "expiries_material_in": int(round(per.get(k, 0.0) * n_books)),
            },
            "topology": {"kind": kind, "strength": round(strength, 4), "labels": labels},
            "vanna_note": ("scored as a magnitude since 2026-09-12, by decision: |vanna| "
                           "correlates -0.728 with proximity, so this component partly "
                           "offsets proximity"),
            "charm_note": ("scored as a magnitude since 2026-09-12, by decision: dDelta/dt "
                           "per session, sign never scored, unmeasured as a quality input"),
            "evidence": _evidence(k, gv, comp, oi.get(k, 0), kind, labels,
                                  per.get(k, 0.0), n_books, em, spot),
        })

    nodes.sort(key=lambda n: -n["quality"])
    if cfg.top_n:
        nodes = nodes[:cfg.top_n]

    # DOMINANCE: does one strike carry most of the window's gross exposure?
    # METHOD.md section 6 records the failure this guards: 29,500 read as a major
    # call wall on +142 net gamma of which almost all was one 1,838-lot line in a
    # 23-DTE book, irrelevant intraday. The robust scale keeps such a line from
    # erasing the rest of the book, but the caller still needs to SEE it, because
    # a node resting on one line is a different kind of evidence from a node
    # built out of thirty.
    win_gross = sum(abs(x) for k, x in g.items() if lo <= k <= hi) or 1.0
    top_k = max(cands, key=lambda k: abs(g[k]))
    dominance = {
        "price": top_k,
        "share": round(abs(g[top_k]) / win_gross, 4),
        "basis": "largest single strike's share of gross exposure in the window",
    }

    return {
        "nodes": nodes,
        "n_candidates": len(cands),
        "n_exposed_in_window": len(in_window),
        "dominance": dominance,
        "window": [round(lo, 2), round(hi, 2)],
        "spot": spot,
        "em": em,
        "weights": w.as_dict(),
        "weights_validated": False,
        "weights_note": ("Equal by default and NOT fitted. Fitting them to reaction rate would "
                         "be fitting to noise -- see `measured` below."),
        "score_note": ("A ranking device for ATTENTION, not a probability of holding. "
                       "MEASURED on 500 sessions and 4,556 node touches: this score does NOT "
                       "rank reaction probability."),
        # THE MEASUREMENT, CARRIED WITH THE SCORE SO IT CANNOT BE FORGOTTEN.
        # histval.py, 2026-09-10, Sep 2024 - Sep 2026. Full write-up in
        # `Claude outputs/RESULTS-nodes-500-sessions.md`.
        "measured": {
            "scored_with": ("the five-component score (gamma, oi, proximity, persistence, "
                            "structure); the seven-component score of 2026-09-12 is unmeasured"),
            "sessions": 500,
            "node_touches_resolved": 4556,
            "node_reaction_rate": 0.568,
            "placebo_reaction_rate": 0.570,
            "edge_pp": -0.2,
            "edge_z": -0.17,
            "by_quality_bucket": {"q1_low": 0.579, "q2": 0.570, "q3": 0.527, "q4_high": 0.582},
            "verdict": ("NO EDGE over a distance-matched round-number placebo, and the quality "
                        "buckets are NOT monotone. The ~57% is a property of the touch geometry, "
                        "not of gamma. Nodes mark where hedging concentrates, not where price "
                        "turns. This score decides which levels are worth showing; it does not "
                        "say they hold."),
        },
        "excluded": {
            "cluster": "0.681 with gamma and 0.688 with structure; the same information smoothed",
        },
        "added_by_decision": {
            "date": "2026-09-12",
            "vanna": ("rank correlation -0.728 with proximity, an inverse-distance measure; "
                      "scored anyway, magnitude only, unvalidated"),
            "charm": ("measured worse than chance for DIRECTION over 63 sessions; scored as "
                      "a magnitude per session, unvalidated as a quality input"),
            "restore_five": "Weights(vanna=0.0, charm=0.0)",
        },
        "scales": {"gamma": round(g_scale, 4), "oi": round(oi_scale, 2),
                   "vanna": round(v_scale, 4), "charm": round(c_scale, 4),
                   "topology": round(feat_scale, 4),
                   "basis": f"p{cfg.scale_pct * 100:.0f} over the candidate set"},
        "config": asdict(cfg),
    }


def _evidence(k, gv, comp, oi_v, kind, labels, per, n_books, em, spot):
    """Plain statements a human can check against the ladder. The score must
    always be explainable, so the reasons are built here rather than inferred
    later from the numbers."""
    ev = [f"net gamma {gv:+,.1f} at {k:,.0f}"]
    if oi_v:
        ev.append(f"{oi_v:,} contracts of open interest")
    if em:
        ev.append(f"{abs(k - spot):,.0f} pts from the reference, {abs(k - spot) / em:.2f} expected moves")
    if kind:
        ev.append(f"inside a {kind}" + (f" ({', '.join(labels)})" if labels else ""))
    else:
        ev.append("not inside any topology formation")
    if n_books:
        ev.append(f"material in {int(round(per * n_books))} of {n_books} live expiries")
    top = max(comp, key=comp.get)
    ev.append(f"strongest component: {top} at {comp[top]:.2f}")
    return ev
