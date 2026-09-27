#!/usr/bin/env python3
"""
scenario.py -- conditional paths, generated deterministically from the map.

    import scenario
    sc = scenario.generate(state, topo, nodes, spot, em)

Not predictions. Each scenario says: IF price does a specific, observable thing,
THEN here is the structure it would be travelling through, here are the levels in
that path, and here is what would prove the whole idea wrong.

--------------------------------------------------------------------------------
HOW THIS AVOIDS TURNING STRUCTURE INTO DIRECTION
--------------------------------------------------------------------------------

The engine supplies the map. The TAPE supplies the direction. That division is
what makes deterministic generation honest:

  * The TRIGGER is always a price event -- acceptance beyond a level, or a test
    of one. It is the market committing, not the model predicting.
  * The TARGETS are scored nodes lying in the path beyond the trigger. They are
    where structure is, not where price is going.
  * The INVALIDATION is the structural level on the other side of the trigger.
  * Every scenario carries OPPOSING EVIDENCE, populated from the same map.

Both directions are always generated where structure allows it. A scenario is
never emitted for one side only because the gamma sign "looked bullish" -- the
generator has no such concept, and a test asserts the up and down scenarios are
built by the same code path with the sign mirrored.

--------------------------------------------------------------------------------
THE TRIGGER DEFINITIONS ARE THE ENGINE'S OWN, NOT INVENTED HERE
--------------------------------------------------------------------------------

Mark's brief warned against inventing something like "5-minute acceptance" unless
the engine already defines acceptance. It does, in two places, and both are
reused rather than replaced:

  ACCEPTANCE   the tape puts in a SWING beyond the level and has not reclaimed
               it. `precommentary.level_state` computes exactly this from
               `swings.lows` / `swings.highs` and calls it "broken".

               It used to be a close on a 30-minute bar. Mark, 2026-09-16:
               "lets remove 30 minute close logic, a lot can happen in 30 minutes
               including a massive stop run", then "I want 30 minute close gone
               completely." A run that spikes through a level and is taken
               straight back is precisely what prints a close beyond it, so the
               rule was keyed on the signature of the thing it meant to exclude.
               A swing is a place price actually turned; a spike that reverses
               leaves none. There is no clock in the definition any more.

  TEST         a bar SPANS the level, which is `command_center._fold_card`'s
               trigger condition, with the approach fraction from `TEST_FRAC`.

--------------------------------------------------------------------------------
THE PIN CASE IS NOT A RANGE CLAIM
--------------------------------------------------------------------------------

A pin scenario is emitted when exposure is concentrated close to price. Its
evidence is structural -- concentration, distance, time remaining -- and it
carries an explicit note that pinning is NOT validated in this engine. HANDOFF is
unambiguous that the gamma sign implies nothing about range (p = 0.46), and the
one pin-shaped thing that was tested, the charm pin, failed in the wrong
direction. The pin scenario exists because "the map offers nothing to trade" is a
useful output, not because the engine can predict compression.

--------------------------------------------------------------------------------
THE GATE: SHOULD A TRADE BE LOOKED FOR AT ALL (2026-09-12)
--------------------------------------------------------------------------------

Mark, 2026-09-12: "sometimes when the book is thin we're still outputting an A,
B, C and D trade when they're all technically low quality because the book and
the ladder are bad." They were. Six paths came out of every book that had one
scored node in it, and nothing in the document said the book was not worth a
path. `book_gate()` now runs first and returns a VERDICT with every check that
produced it:

    look          the book supports a path; scenarios as before
    thin          it supports at most one small path; the report carries the
                  top one or two and says why
    stand aside   it does not support a trade; the paths are still generated
                  and kept in the document, and the report WITHHOLDS them

The checks are the engine's own measurements -- data quality, the number of
usable anchors within reach, whether one line is the whole book, the size of
the material strikes, how legible the structure is -- each with its value and
its threshold, so the verdict can be argued with. The THRESHOLDS ARE
PROVISIONAL and the output says so: whether a book this thin actually trades
worse has not been measured, and measuring it is a Crucible study, not a
constant here.

--------------------------------------------------------------------------------
UNITS
--------------------------------------------------------------------------------

prices, triggers, targets, invalidation   points of the reference contract
distances                                 points, and in expected moves
confidence                                dimensionless, never a probability

Written 2026-09-09, Phase E of the market-state upgrade.
"""

from dataclasses import dataclass, asdict

import confidence as confidence_mod


@dataclass
class Config:
    trigger_offset: float = 0.0    # points beyond the level the trigger is quoted at
    max_targets: int = 3           # matches the sheet's three-target card shape
    min_target_gap: float = 0.15   # targets closer than this many EMs apart are one target
    reach_ems: float = 2.0         # targets beyond this many EMs are not offered
    boundary_ems: float = 1.0      # trigger levels are sought within this many EMs of price
    pin_concentration: float = 0.45
    pin_distance_ems: float = 0.30
    min_quality: float = 0.25      # a node under this does not anchor a trigger or a target
    # GEOMETRY (2026-09-12). Measured on 98 sessions before this existed: the median
    # T1 sat 25 points beyond the trigger and the median invalidation 24 points the
    # other way, against a session whose typical swing leg ran far further. So the
    # move that WAS the trigger ran through T1 in 89 of 110 triggered paths before
    # anyone could act on it -- the path was over when it began. A
    # target now has to sit at least this many expected moves beyond the trigger
    # (0.35 EM is about one median half-hour bar on a 2026 book) and the
    # invalidation at least this far the other way (half a bar; a stop inside one
    # bar's noise is not a stop). Both PROVISIONAL, both stamped on every scenario.
    min_target_travel_ems: float = 0.35
    min_invalidation_ems: float = 0.15
    # ONE SIDE PER BOUNDARY (2026-09-12). A level below price has one path,
    # acceptance BELOW it; "acceptance above it" is a trigger price already satisfies.
    # Measured on 98 sessions: with both sides emitted, 309 of 584 paths were
    # already beyond their trigger at the RTH open, and re-striking the whole
    # book at the open still left 332 of 586 -- the both-sides rule, not the
    # overnight gap, was the source. This is a PRICE criterion; the gamma sign
    # still never selects a side. False restores the old generator exactly.
    one_side_per_boundary: bool = True
    max_scenarios: int = 6         # what is RETURNED; everything generated is counted in the result
    # THE GATE. All PROVISIONAL; see the docstring. `gate_quality` is above
    # `min_quality` on purpose: a node good enough to anchor one trigger is not
    # evidence the BOOK is worth trading.
    gate_quality: float = 0.40      # an anchor is a node at least this good...
    gate_reach_ems: float = 1.0     # ...within this many expected moves of price
    gate_thin_anchors: int = 2      # fewer anchors than this reads thin; none reads stand aside
    gate_data_quality: float = 0.70 # regime.data_quality score below this: stand aside
    gate_dominance: float = 0.60    # one strike holding this share of window gross: stand aside
    gate_gamma_hard: float = 10.0   # p95 |gamma| of the candidates below this: stand aside (METHOD.md s.2)
    gate_gamma_soft: float = 25.0   # ...below this: thin
    gate_struct_hard: float = 0.25  # structural confidence below this: stand aside
    gate_struct_soft: float = 0.40  # ...below this: thin
    thin_scenarios: int = 2         # what the REPORT carries on a thin book

    def merged(self, **kw):
        d = asdict(self)
        d.update({k: v for k, v in kw.items() if v is not None and k in d})
        return Config(**d)


def acceptance_rule(level, side, cfg=None):
    """The trigger sentence, built from the engine's own acceptance definition.

    Clock-free since 2026-09-16 (see the module docstring). `cfg` is kept in the
    signature because every call site passes it and the shape is part of the
    contract; nothing in the sentence depends on it now."""
    return f"acceptance {'above' if side == 'up' else 'below'} {level:,.0f}"


def _nodes_beyond(nodes, level, side, spot, em, cfg):
    """Scored nodes lying beyond `level` in `side`, nearest first, thinned so two
    targets a few points apart are not offered as separate levels."""
    out = []
    for n in nodes:
        p = n["price"]
        if side == "up" and p <= level:
            continue
        if side == "down" and p >= level:
            continue
        if (n.get("quality") or 0) < cfg.min_quality:
            continue
        if em and abs(p - spot) / em > cfg.reach_ems:
            continue
        if em and abs(p - level) / em < cfg.min_target_travel_ems:
            continue                       # inside the trigger bar's own travel; see Config
        out.append(n)
    out.sort(key=lambda n: abs(n["price"] - level))
    thinned = []
    for n in out:
        if em and any(abs(n["price"] - t["price"]) / em < cfg.min_target_gap for t in thinned):
            continue
        thinned.append(n)
        if len(thinned) >= cfg.max_targets:
            break
    return thinned


def _invalidation(nodes, level, side, cfg, em=None):
    """The nearest scored node on the OTHER side of the trigger that is not
    inside one bar's noise of it.

    An invalidation anchored to structure rather than to a points band is the
    rule command_center already enforces on every card: a stop sits beyond the
    nearest material strike in the adverse direction, never beyond a flip,
    because a zero crossing is not a wall. Since 2026-09-12 it must also sit at
    least `min_invalidation_ems` from the trigger (see Config): the adjacent
    strike, 24 points away, was being closed through by ordinary rotation.
    """
    opp = "down" if side == "up" else "up"
    cands = [n for n in nodes
             if (n["price"] < level if opp == "down" else n["price"] > level)
             and (n.get("quality") or 0) >= cfg.min_quality
             and (not em or abs(n["price"] - level) / em >= cfg.min_invalidation_ems)]
    if not cands:
        return None
    return min(cands, key=lambda n: abs(n["price"] - level))


def _boundaries(topo, nodes, spot, em, cfg):
    """Candidate trigger levels: the edges of structure near price.

    Transitions and formation edges are where an acceptance means something. A
    level in the middle of a shelf is not a boundary and triggers nothing.
    """
    out = []
    lim = cfg.boundary_ems * em if em else float("inf")
    for f in (topo or {}).get("features", []):
        if f["kind"] == "transition":
            if abs(f["center"] - spot) <= lim:
                out.append({"price": f["center"], "why": "sign change between opposite-signed formations",
                            "feature": f["kind"]})
        elif f["kind"] in ("wall", "shelf"):
            for edge, tag in ((f["lower"], "lower edge"), (f["upper"], "upper edge")):
                if abs(edge - spot) <= lim:
                    out.append({"price": edge, "why": f"{tag} of a {f['kind']}"
                                                      + (f" ({', '.join(f['labels'])})" if f["labels"] else ""),
                                "feature": f["kind"]})
    # de-duplicate boundaries that land on the same price
    seen, uniq = set(), []
    for b in sorted(out, key=lambda b: abs(b["price"] - spot)):
        k = round(b["price"], 2)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(b)
    return uniq


def _opposing(side, state, nodes_beyond, spot, em):
    """Evidence AGAINST this path, from the same map that produced it.

    A scenario that cannot state what argues against it is not a scenario, it is
    an advertisement.
    """
    out = []
    dims = (state or {}).get("dimensions") or {}
    g = dims.get("gamma_regime") or {}
    if g.get("measures_agree") is False:
        out.append("the at-spot and chain-wide gamma measures disagree in sign, so the book's "
                   "character is not well determined")
    fs = dims.get("flip_state") or {}
    if fs.get("at_flip"):
        out.append(f"price is within {fs.get('at_flip_threshold_ems')} expected moves of a flip; "
                   f"the regime either side is not settled")
    heavy = [n for n in nodes_beyond if (n.get("quality") or 0) >= 0.6]
    if heavy:
        n = heavy[0]
        out.append(f"a high-quality node sits in the path at {n['price']:,.0f} "
                   f"(quality {n['quality']:.2f}); it is a target and equally an obstacle")
    conc = dims.get("concentration") or {}
    if conc.get("label") == "low":
        out.append("exposure is spread rather than concentrated, so no level in the path is "
                   "strongly defended")
    ex = dims.get("expiry_state") or {}
    if ex.get("label") == "dominant":
        out.append("the 0DTE book dominates; its structure decays through the session and the "
                   "map will not look like this by the close")
    return out


def _prereqs(side, level, state, spot):
    """What must be true NOW for this path to be live."""
    out = [f"price is {'below' if side == 'up' else 'above'} {level:,.0f} and has not yet accepted through it"]
    dims = (state or {}).get("dimensions") or {}
    fs = dims.get("flip_state") or {}
    if fs.get("label") and fs["label"] != "unknown":
        out.append(f"price is {fs['label']}")
    g = dims.get("gamma_regime") or {}
    if g.get("label") and g["label"] != "unknown":
        out.append(f"dealer gamma at spot reads {g['label']} ({g.get('strength', '')})".strip())
    return out


def _scenario(kind, side, boundary, nodes, state, spot, em, cfg):
    level = boundary["price"]
    tgts = _nodes_beyond(nodes, level, side, spot, em, cfg)
    if not tgts:
        return None
    inval = _invalidation(nodes, level, side, cfg, em)
    t1 = tgts[0]["price"]
    sc = {
        "kind": kind,
        "side": side,
        # THE GEOMETRY, stamped so it can be read and argued with (2026-09-12).
        "geometry": {
            "t1_travel": round(abs(t1 - level), 2),
            "t1_travel_ems": round(abs(t1 - level) / em, 3) if em else None,
            "risk": round(abs(level - inval["price"]), 2) if inval else None,
            "risk_ems": round(abs(level - inval["price"]) / em, 3) if inval and em else None,
            "R": round(abs(t1 - level) / abs(level - inval["price"]), 2) if inval and level != inval["price"] else None,
            "min_target_travel_ems": cfg.min_target_travel_ems,
            "min_invalidation_ems": cfg.min_invalidation_ems,
            "note": ("measured from the trigger LEVEL; the tradeable entry is the acceptance "
                     "close, which sits beyond it by however far that bar ran"),
        },
        "trigger": {
            "level": round(level, 2),
            "rule": acceptance_rule(level, side, cfg),
            "basis": "acceptance = the tape swings beyond the level and does not reclaim it, "
                     "which is precommentary.level_state's \"broken\"; no clock is involved",
        },
        "structural_reasoning": [
            f"{level:,.0f} is the {boundary['why']}",
            f"beyond it the nearest scored structure is "
            + ", ".join(f"{t['price']:,.0f} (quality {t['quality']:.2f})" for t in tgts),
        ],
        "targets": [{
            "price": t["price"],
            "quality": t["quality"],
            "distance": round(t["price"] - spot, 2),
            "distance_ems": round((t["price"] - spot) / em, 3) if em else None,
            "sign": t["sign"],
            "topology": t.get("topology", {}).get("kind"),
            "why": f"scored node, quality {t['quality']:.2f}"
                   + (f", inside a {t['topology']['kind']}" if t.get("topology", {}).get("kind") else ""),
        } for t in tgts],
        "invalidation": ({
            "level": inval["price"],
            "rule": acceptance_rule(inval["price"], "down" if side == "up" else "up", cfg),
            "why": f"nearest scored node on the other side of the trigger, quality {inval['quality']:.2f}",
            "basis": "structural, not a points band; a zero crossing is never used as an anchor",
        } if inval else None),
        "prerequisites": _prereqs(side, level, state, spot),
        "opposing_evidence": _opposing(side, state, tgts, spot, em),
        "risks": [],
    }
    if inval is None:
        sc["risks"].append("no scored node sits on the other side of the trigger, so this path has "
                           "no structural invalidation; it cannot be risk-managed off the map")
    if em and abs(level - spot) / em > 0.75:
        sc["risks"].append(f"the trigger sits {abs(level - spot) / em:.2f} expected moves away; "
                           f"the session may never reach it")
    return sc


def _pin(state, nodes, spot, em, cfg):
    """The no-trade case: exposure concentrated close to price."""
    dims = (state or {}).get("dimensions") or {}
    conc = dims.get("concentration") or {}
    score = conc.get("score")
    if score is None or not nodes:
        return None
    top = nodes[0]
    d = abs(top["price"] - spot) / em if em else 99.0
    if score < cfg.pin_concentration or d > cfg.pin_distance_ems:
        return None
    return {
        "kind": "pin",
        "side": None,
        "trigger": None,
        "structural_reasoning": [
            f"exposure is concentrated: the three largest formations hold {score:.0%} of window gross",
            f"the highest-quality node, {top['price']:,.0f} at quality {top['quality']:.2f}, "
            f"sits {abs(top['price'] - spot):,.0f} pts from price ({d:.2f} EM)",
            "there is no boundary near enough to price for an acceptance to mean anything",
        ],
        "targets": [],
        "invalidation": {
            "level": None,
            "rule": "acceptance out of the concentrated zone in either direction",
            "why": "the pin case ends when price leaves the structure that defines it",
        },
        "prerequisites": [f"price within {cfg.pin_distance_ems} expected moves of the dominant node"],
        "opposing_evidence": _opposing("none", state, nodes[:3], spot, em),
        "risks": [
            "PINNING IS NOT VALIDATED IN THIS ENGINE. The 60-session backtest found no support "
            "for a gamma sign implying a compressed range (p=0.46), and the one pin-shaped claim "
            "that was tested, the charm pin, failed in the wrong direction. This scenario says "
            "the map offers nothing to trade, NOT that price will be still.",
        ],
    }


def book_gate(state, topo, nodes_result, spot, em, cfg=None):
    """
    Should a trade be looked for in this book at all. See the module docstring.

    Returns {"verdict": "look" | "thin" | "stand aside", "checks": [...],
             "reasons": [...], "validated": False, ...}. Every check carries its
    measured value and its threshold. A hard fail is stand aside; a soft fail is
    thin; neither is look.
    """
    cfg = cfg or Config()
    nodes = (nodes_result or {}).get("nodes") or []
    dq = ((state or {}).get("data_quality") or {})
    checks, hard, soft = [], [], []

    def check(name, value, threshold, ok, severity, why):
        c = {"check": name, "value": value, "threshold": threshold, "pass": bool(ok),
             "severity": severity, "why": why}
        checks.append(c)
        if not ok:
            (hard if severity == "hard" else soft).append(f"{name}: {why}")

    # 1. what the book was measured ON
    dqs = dq.get("score")
    check("data_quality", dqs, cfg.gate_data_quality,
          dqs is None or dqs >= cfg.gate_data_quality, "hard",
          "; ".join(i for i in dq.get("issues", []) if "no data-quality issue" not in i) or "inputs incomplete")

    # 2. anchors: nodes good enough, close enough
    anchors = [n for n in nodes
               if (n.get("quality") or 0.0) >= cfg.gate_quality
               and abs(((n.get("raw") or {}).get("distance_ems")) or 99.0) <= cfg.gate_reach_ems]
    check("anchors_within_reach", len(anchors), cfg.gate_thin_anchors,
          len(anchors) >= cfg.gate_thin_anchors, "soft",
          f"{len(anchors)} node(s) of quality >= {cfg.gate_quality} within {cfg.gate_reach_ems:g} EM")
    check("any_anchor", len(anchors), 1, len(anchors) >= 1, "hard",
          f"no node of quality >= {cfg.gate_quality} within {cfg.gate_reach_ems:g} EM of price")

    # 3. one line is not a book
    dom = ((nodes_result or {}).get("dominance") or {})
    share = dom.get("share")
    check("single_line_dominance", share, cfg.gate_dominance,
          share is None or share < cfg.gate_dominance, "hard",
          (f"one strike ({dom.get('price', 0):,.0f}) holds {share:.0%} of the window's gross exposure"
           if share is not None else "no dominance measured"))

    # 4. the material strikes are big enough to be structure (METHOD.md section 2:
    #    single-digit nodes carry real uncertainty; only double digits are structure)
    gsc = ((nodes_result or {}).get("scales") or {}).get("gamma")
    check("material_gamma", gsc, cfg.gate_gamma_hard,
          gsc is None or gsc >= cfg.gate_gamma_hard, "hard",
          f"p95 |gamma| of the candidates is {gsc if gsc is None else round(gsc, 1)} contracts per 1%")
    check("material_gamma_soft", gsc, cfg.gate_gamma_soft,
          gsc is None or gsc >= cfg.gate_gamma_soft, "soft",
          f"p95 |gamma| of the candidates is {gsc if gsc is None else round(gsc, 1)}, under {cfg.gate_gamma_soft:g}")

    # 5. how legible the shape is
    st = confidence_mod.structural_confidence(topo, nodes_result)
    sv = st.get("value")
    check("structural_confidence", sv, cfg.gate_struct_hard,
          sv is None or sv >= cfg.gate_struct_hard, "hard",
          "; ".join(st.get("limited_by") or []) or "structure not legible")
    check("structural_confidence_soft", sv, cfg.gate_struct_soft,
          sv is None or sv >= cfg.gate_struct_soft, "soft",
          "; ".join(st.get("limited_by") or []) or "structure weak")

    verdict = "stand aside" if hard else "thin" if soft else "look"
    text = {"look": "the book supports a conditional path; look for a trade",
            "thin": "a thin book: at most one small path, and the reasons are listed",
            "stand aside": "the book does not support a trade; the paths are withheld from the report"}[verdict]
    return {
        "verdict": verdict,
        "text": text,
        "reasons": hard + soft,
        "checks": checks,
        "anchors": [n["price"] for n in anchors],
        "validated": False,
        "note": ("PROVISIONAL thresholds. Whether a book that fails these trades worse than one that "
                 "passes has not been measured; the gate exists so a thin book is named as thin "
                 "instead of dressed as four trades. Measuring it is a Crucible study."),
    }


def generate(state, topo, nodes_result, spot, em, cfg=None, cross_asset=None, **kw):
    """
    Every conditional path the map supports, ranked by how well specified it is.

    Ranking is by scenario confidence, which measures SPECIFICATION -- has a
    trigger, has targets with structure behind them, has an invalidation -- and
    not likelihood. The labels base / alternative / second alternative are
    positions in that ranking and nothing more.
    """
    cfg = (cfg or Config()).merged(**kw)
    nodes = (nodes_result or {}).get("nodes") or []
    gate = book_gate(state, topo, nodes_result, spot, em, cfg)
    if not nodes:
        return {"scenarios": [], "note": "no scored nodes; nothing to build a path from",
                "gate": gate, "config": asdict(cfg)}

    out = []
    for b in _boundaries(topo, nodes, spot, em, cfg):
        # THE SIDE IS SET BY WHERE PRICE IS, NEVER BY THE GAMMA SIGN. A boundary
        # above price can only be accepted upward; one below, downward; one AT
        # price (a transition centred on spot) can go either way. With
        # one_side_per_boundary off, both sides are built for every boundary,
        # which is what the generator did until 2026-09-12.
        if cfg.one_side_per_boundary and b["price"] > spot:
            sides = ("up",)
        elif cfg.one_side_per_boundary and b["price"] < spot:
            sides = ("down",)
        else:
            sides = ("up", "down")
        for side in sides:
            sc = _scenario("continuation", side, b, nodes, state, spot, em, cfg)
            if sc:
                sc["side_basis"] = (f"price is {'below' if side == 'up' else 'above'} {b['price']:,.0f}; "
                                    f"a close {'above' if side == 'up' else 'below'} it is the path"
                                    if b["price"] != spot else "the boundary sits at price; both sides are paths")
                out.append(sc)

    p = _pin(state, nodes, spot, em, cfg)
    if p:
        out.append(p)

    # DEDUPLICATE BY WHAT THE PATH ACTUALLY IS.
    #
    # Every edge of every formation near price is a candidate boundary, so two
    # triggers a few points apart routinely produce the SAME side and the SAME
    # target sequence. On the 2026-09-08 near book that generated 21 scenarios of
    # which most were restatements: a close above 29,515 and a close above 29,578
    # both ran to 29,600 / 29,650 / 29,700. A path is identified by its side and
    # its targets; where several triggers reach it, the one NEAREST price is kept,
    # because that is the one the session can actually act on.
    generated = len(out)
    best = {}
    for sc in out:
        if sc["kind"] == "pin":
            best[("pin",)] = sc
            continue
        key = (sc["side"], tuple(t["price"] for t in sc["targets"]))
        cur = best.get(key)
        if cur is None or abs(sc["trigger"]["level"] - spot) < abs(cur["trigger"]["level"] - spot):
            best[key] = sc
    out = list(best.values())

    for sc in out:
        c = confidence_mod.decompose(state=state, topo=topo, nodes=nodes_result,
                                     node=None, scenario=sc, cross_asset=cross_asset)
        sc["confidence"] = c["dimensions"]
        sc["confidence_note"] = c["calibration_note"]

    # Primary sort is specification. It ties often -- a well-formed path is a
    # well-formed path -- so the tie-break is how close the trigger sits to price.
    # That is a STRUCTURAL tie-break, not a directional one: a nearer trigger is
    # more actionable this session regardless of which way it points.
    out.sort(key=lambda s: (-(s["confidence"]["scenario"]["value"] or 0),
                            abs((s["trigger"] or {}).get("level", spot) - spot)))
    kept = out[:cfg.max_scenarios]
    labels = ["base case", "alternative", "second alternative"]
    for i, sc in enumerate(kept):
        sc["rank"] = i + 1
        sc["label"] = ("no-trade / pin case" if sc["kind"] == "pin"
                       else labels[i] if i < len(labels) else f"further alternative {i - 2}")
        sc["book_verdict"] = gate["verdict"]

    return {
        "scenarios": kept,
        "gate": gate,
        "report_carries": (0 if gate["verdict"] == "stand aside"
                           else min(cfg.thin_scenarios, len(kept)) if gate["verdict"] == "thin"
                           else len(kept)),
        "generated": generated,
        "distinct_paths": len(out),
        "returned": len(kept),
        "dedupe_basis": "one scenario per (side, target sequence); the nearest trigger wins",
        "one_side_per_boundary": cfg.one_side_per_boundary,
        "side_basis": ("a boundary above price is only accepted upward, one below only downward; "
                       "a price criterion, never a gamma-sign one. Config(one_side_per_boundary=False) "
                       "restores both sides for every boundary."),
        "tie_break": "equal specification is broken by trigger distance from price, never by direction",
        "spot": spot,
        "em": em,
        "ranking_basis": ("scenario confidence, which measures SPECIFICATION -- trigger, targets, "
                          "invalidation, opposing evidence -- and NOT likelihood. 'Base case' is a "
                          "position in that ranking, not a forecast."),
        "direction_note": ("Both directions are generated wherever structure allows. The engine "
                           "supplies the map; the trigger is the tape committing. No scenario is "
                           "emitted for one side because the gamma sign looked directional."),
        "config": asdict(cfg),
    }
