#!/usr/bin/env python3
"""
engine.py -- run the whole deterministic stack and emit ONE structured document.

    python engine.py --chain data/2026-09-08-chain.json --report-date 2026-09-09
    python engine.py --from-trinity            (re-uses whatever trinity.json names)

    import engine
    doc = engine.run(chain, report_date)

This is the composition point. Everything below it is a layer that knows nothing
about the layers above:

    trinity_db   exposure          contracts of delta per 1% move, per strike
    topology     shape             walls, shelves, pockets, transitions
    nodescore    which levels      quality with stored components
    expiry       expiry mix        both cancellation conventions
    regime       market state      six dimensions, each with its own confidence
    scenario     conditional paths trigger, targets, invalidation
    confidence   decomposition     six dimensions, none of them a probability
    crossasset   agreement         optional, needs the equity books
    releases     time gates        optional, the session's scheduled releases

--------------------------------------------------------------------------------
WHAT THIS DOES NOT TOUCH
--------------------------------------------------------------------------------

`trinity.json`, `build.py`, `gen.py` and `command_center.py` are untouched by
this file. It writes a SEPARATE document, `state.json`, so the sheet, the
tracker, the archive and the command centre all keep working exactly as they
did. The existing ladder is preserved, not replaced -- Mark's brief was explicit
that the compact trader-readable output stays.

`state.json` is additive. Nothing reads it unless it asks for it.

--------------------------------------------------------------------------------
2026-09-12: WHAT WAS ADDED, AND WHERE IT CAME FROM
--------------------------------------------------------------------------------

  gamma_profile     the aggregate G(F) curve each panel's flip was found on, and
                    its slope at spot. The sweep always computed it and threw it
                    away (trinity_db.hvl_profile). Ours.
  sign_convention   the dealer-sign convention is an ASSUMPTION (METHOD.md s.9)
                    and this block MEASURES what inverting it changes, by running
                    the opposite book through topology, the node score and the
                    gamma regime. Nothing here decides which convention is right.
                    Ours; the flow-based measurement that could decide it is
                    dealer_flow.py, taken from Nicky's engine, not yet fed.
  rail_sensitivity  every reported level's inside/outside-the-rail call re-run
                    against a rail 15% wider, and flagged when the call flips.
                    Taken from Nicky's engine (opra_read.ENVELOPE_SENSITIVITY).
  releases          the session's scheduled high-impact USD releases as time
                    gates: a touch within minutes of one is a release print, not
                    a hedging reaction. Taken from Nicky's engine (red_folder.py).
  flip crossings    how many zero crossings each book has, in the report and the
                    LLM view; a fragmented book used to reach both looking clean.
  panel fallback    an empty panel now falls back to the NEXT panel out (0DTE ->
                    NEAR -> FULL) and names the one it used, instead of jumping
                    to the full book.
  levels_measured   the 500-session null travels in the LLM view as data beside
                    the levels it qualifies, not only as caveat prose.

--------------------------------------------------------------------------------
THE LLM CONTRACT
--------------------------------------------------------------------------------

The `for_llm()` view is the ONLY thing an interpretation layer should ever
receive. It is deliberately a strict subset: every number in it was computed by
the layers above, and there is no field an LLM could write back into. The rules
that go with it are in the document itself, under `llm_contract`, so they travel
with the payload rather than living in a prompt that can drift away from it.

Written 2026-09-09, Phase H of the market-state upgrade.
"""

import sys
import json
import argparse
import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import trinity_db
import topology as topology_mod
import nodescore as nodescore_mod
import expiry as expiry_mod
import regime as regime_mod
import scenario as scenario_mod
import confidence as confidence_mod
import releases as releases_mod

DEFAULT_PANEL = "NEAR"
TOP_NODES = 8           # what the report shows; the full set stays in the document
PANEL_ORDER = ("0DTE", "NEAR", "FULL")
RAIL_SENSITIVITY = 1.15  # Nicky's ENVELOPE_SENSITIVITY: re-run every rail call against a band this much wider

SIGN_ASSUMED = "dealers long calls / short puts, net = +(C - P) (METHOD.md section 9)"


FILTER_PLAYED = True     # withhold from the REPORT any path whose trigger the live print is already beyond


def run(chain, report_date, panel=DEFAULT_PANEL, hours=6.5, spot=None,
        iv_history=None, cross_asset=None, top_nodes=TOP_NODES, releases=None,
        sign_sensitivity=True, last=None, filter_played=FILTER_PLAYED, ref=None, **kw):
    """
    The whole deterministic stack for one chain and one report date.

    `panel` selects which book the structural layers read. NEAR (<=9 DTE) is the
    default because METHOD.md section 6 recommends 0-2 DTE as the primary basis
    with the near book as ballast, and because the 0DTE panel is EMPTY -- not
    flat -- on any date with no expiry.

    `releases`: None (nothing fetched -- the default, so a library call never
    touches the network), "auto" (the cached week, or a fetch), or a document
    from releases.for_report() to carry as-is.
    """
    out, books, live = trinity_db.analyse(chain, report_date, hours=hours, spot=spot, quiet=True,
                                          last=last, ref=ref)
    S = out["spot"]
    F_ref = out["ref_settle"]
    em = out["em"]

    P = trinity_db.panel_cols(books)
    bs, panel_used, empty_panel = P.get(panel), panel, False
    if not bs:
        # FALL BACK ONE PANEL OUT, NOT TO THE WHOLE BOOK. The panels nest (0DTE
        # within NEAR within FULL), so the next one out is the narrowest book that
        # exists; the full book is the basis METHOD.md section 6 warns can fake a
        # wall out of one 23-DTE line. And say which was used.
        empty_panel = True
        start = PANEL_ORDER.index(panel) if panel in PANEL_ORDER else 0
        for name in PANEL_ORDER[start + 1:]:
            if P.get(name):
                bs, panel_used = P[name], name
                break
        else:
            bs, panel_used = books, "FULL"

    topo = topology_mod.from_books(books, S, F_ref, em=em, panel=bs)
    nodes = nodescore_mod.score_nodes(bs, S, F_ref, em, topo=topo)
    dom = expiry_mod.dominance(books, S, F_ref)
    state = regime_mod.market_state(out, bs, S, F_ref, em, iv_history=iv_history,
                                    topo=topo, dom=dom)
    scen = scenario_mod.generate(state, topo, nodes, S, em, cross_asset=cross_asset)
    _mark_played(scen, out.get("last_print"), S, filter_played)

    top = (nodes.get("nodes") or [])[:1]
    conf = confidence_mod.decompose(state=state, topo=topo, nodes=nodes,
                                    node=top[0] if top else None,
                                    scenario=(scen.get("scenarios") or [None])[0],
                                    cross_asset=cross_asset)

    sign = (_sign_sensitivity(out, books, bs, S, F_ref, em, nodes, state, top_nodes)
            if sign_sensitivity else {"assumed": SIGN_ASSUMED, "source": _sign_source(books),
                                      "under_inversion": None, "note": "sensitivity not run"})

    rel = None
    if releases == "auto":
        rel = releases_mod.for_report(report_date, asof=out.get("asof"), allow_fetch=True)
    elif isinstance(releases, dict):
        rel = releases

    panels = out.get("panels") or {}
    profile = {k: {
        "at_spot": (panels.get(k) or {}).get("at_spot"),
        "curve": (panels.get(k) or {}).get("profile"),
        "crossings": len(((panels.get(k) or {}).get("hvl")) or []),
    } for k in PANEL_ORDER}

    return {
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "engine_version": "state.2",
        "source": {
            "chain": str(chain),
            "settle": out.get("settle"),
            "report_date": report_date,
            "asof": out.get("asof"),
            "hours": out.get("hours"),
            "panel": panel,
            "panel_used": panel_used,
            "panel_was_empty": empty_panel,
            "oi_vintage": out.get("oi_vintage"),
            "iv_basis": out.get("iv_basis"),
            "ref_symbol": out.get("ref_symbol"),
        },
        "reference": {
            "spot": S,
            "ref_settle": F_ref,
            "last_print": out.get("last_print"),
            "em": em,
            "em_basis": out.get("em_basis"),
            "rail_lo": out.get("rail_lo"),
            "rail_hi": out.get("rail_hi"),
            "rail_label": out.get("rail_label"),
            "rail_scale": out.get("rail_scale"),
            "iv_atm": out.get("iv_atm"),
            # THE ONE VALIDATED COMPONENT, carried with its number. HANDOFF:
            # containment of the CLOSE inside the one-session band ran 64-74%
            # against a 68% target across the session-scale runs; the whole-range
            # figure is a running-maximum problem with a ~40% benchmark, not 68%.
            "rails_measured": {
                "close_containment": "64-74% against a 68% target",
                "basis": "close inside S +/- EM, session-scale runs (HANDOFF, backtest.py)",
                "not_measured": "range containment against 68% (the right benchmark is ~40%)",
            },
        },
        "flips": {k: (((panels.get(k) or {}).get("hvl") or [None])[0]) for k in PANEL_ORDER},
        "flip_crossings": {k: len(((panels.get(k) or {}).get("hvl")) or []) for k in PANEL_ORDER},
        "net": {k: ((panels.get(k) or {}).get("net_total")) for k in PANEL_ORDER},
        "gamma_profile": profile,
        "sign_convention": sign,
        "releases": rel,
        "market_state": state,
        "topology": topo,
        "nodes": nodes,
        "expiry": dom,
        "scenarios": scen,
        "confidence": conf,
        "cross_asset": cross_asset,
        "units": {
            "gamma": "contracts of delta per 1% move (the QuikStrike Gamma(1 Pct) convention)",
            "vanna": "contracts of delta per vol point",
            "charm": "contracts of delta per session",
            "gamma_profile": "aggregate gamma at F = F_ref + x; slope per point of the reference",
            "price": "points of the reference contract",
            "iv": "decimal, not percent",
            "time": "business time, sessions, 252 per year",
            "dollar_gamma": "only in cross_asset: contracts/1% x reference x multiplier",
        },
        "report": _report_view(out, state, topo, nodes, scen, conf, S, em, top_nodes,
                               profile=profile, sign=sign, releases=rel),
    }


# ------------------------------------------------------------ played paths ---

def _mark_played(scen, last_print, spot, filter_played=True):
    """
    A path whose trigger the LIVE PRINT is already beyond is not a path the
    session can take: the overnight has played it. Stamped on every scenario
    as `played`; when `filter_played`, the report withholds those paths and
    says so, and the document keeps them (2026-09-12).

    The live print is `last_print` -- the overnight Globex print on a premarket
    build, the --last print on an intraday one; None when the build did not
    carry one, in which case nothing is marked. Measured on 98 sessions: the
    06:00 PT print and the RTH open agree on which paths are played 94% of the
    time, a 0.05-EM buffer changes almost nothing, and overnight ACCEPTANCE (a
    acceptance) is far too aggressive -- it stripped 474 of 584 paths and
    left 41 sessions with nothing. So the rule is the plain one: beyond the
    level, by any amount.
    """
    n_played = 0
    for sc in scen.get("scenarios") or []:
        if sc.get("kind") == "pin" or not sc.get("trigger"):
            sc["played"] = None
            continue
        L = sc["trigger"]["level"]
        if last_print is None:
            sc["played"] = {"is_played": False, "by": None, "note": "no live print on this build"}
            continue
        beyond = (last_print > L) if sc["side"] == "up" else (last_print < L)
        sc["played"] = {"is_played": bool(beyond), "by": last_print, "level": L,
                        "note": (f"the live print {last_print:,.2f} is already "
                                 f"{'above' if sc['side'] == 'up' else 'below'} {L:,.0f}; the overnight played this path"
                                 if beyond else "the trigger is still ahead of the live print")}
        n_played += int(beyond)
    scen["played_paths"] = n_played
    scen["live_print"] = last_print
    scen["filter_played"] = bool(filter_played)
    if filter_played and (scen.get("scenarios") or []):
        live = [s for s in scen["scenarios"] if not ((s.get("played") or {}).get("is_played"))]
        # the report carries only what the gate allowed AND the overnight has not played
        scen["report_carries"] = min(scen.get("report_carries", len(live)), len(live))
        scen["report_order"] = [s["rank"] for s in live]


# ---------------------------------------------------------- sign convention ---

def _sign_source(books):
    """'convention', or a flow summary when dealer_flow.apply() stamped the books."""
    stamped, oi_stamped, oi_all, meas = 0, 0, 0, 0.0
    for b in books:
        for rec in b["strikes"].values():
            oi = ((rec.get("C") or {}).get("oi", 0) or 0) + ((rec.get("P") or {}).get("oi", 0) or 0)
            oi_all += oi
            p = rec.get("pos")
            if isinstance(p, dict) and p.get("dealer") is not None:
                stamped += 1
                oi_stamped += oi
                meas += float(p.get("flow_share", 0.0)) * oi
    if not stamped:
        return "convention"
    return {"source": "flow (dealer_flow.apply)", "strikes_stamped": stamped,
            "oi_share_stamped": round(oi_stamped / oi_all, 4) if oi_all else None,
            "oi_share_measured": round(meas / oi_all, 4) if oi_all else None}


def _invert_books(books):
    """The opposite book: every call line becomes a put line and vice versa, and a
    measured position is negated. Shallow copies; the smiles are shared."""
    out = []
    for b in books:
        st = {}
        for ks, rec in b["strikes"].items():
            r = {"C": rec.get("P", {}), "P": rec.get("C", {})}
            if isinstance(rec.get("pos"), dict) and rec["pos"].get("dealer") is not None:
                r["pos"] = dict(rec["pos"], dealer=-float(rec["pos"]["dealer"]))
            st[ks] = r
        out.append(dict(b, strikes=st))
    return out


def _sign_sensitivity(out, books, bs, S, F_ref, em, nodes, state, top_n):
    """
    MEASURE, not assert, what the opposite dealer-sign convention changes.

    METHOD.md section 9: the convention is an assumption, and every sign on the
    sheet inverts if it is wrong. What does NOT invert is worth knowing too: the
    flip locations (G -> -G has the same zeros), the rails (they never read the
    book), and -- checked here rather than claimed -- the node ranking, because
    every quality component is a magnitude or a distance, and the formations,
    because topology is built on |exposure|. The gamma regime label is the
    thing that flips, and the sensitivity block says so with both labels.
    """
    inv_all = _invert_books(books)
    inv_bs = _invert_books(bs) if bs is not books else inv_all
    try:
        topo_i = topology_mod.from_books(inv_all, S, F_ref, em=em, panel=inv_bs)
        nodes_i = nodescore_mod.score_nodes(inv_bs, S, F_ref, em, topo=topo_i)
        cfg = regime_mod.Config()
        pen = ((state.get("data_quality") or {}).get("penalties")) or []
        reg_i = regime_mod.gamma_regime(out, inv_bs, S, F_ref, em, cfg, pen)
    except Exception as e:                                    # never let the check cost the document
        return {"assumed": SIGN_ASSUMED, "source": _sign_source(books),
                "under_inversion": None, "error": f"{type(e).__name__}: {e}"}

    rank = [n["price"] for n in (nodes.get("nodes") or [])[:top_n]]
    rank_i = [n["price"] for n in (nodes_i.get("nodes") or [])[:top_n]]
    reg = ((state.get("dimensions") or {}).get("gamma_regime")) or {}

    def _forms(t):
        return sorted((f["kind"], round(f["lower"], 2), round(f["upper"], 2))
                      for f in (t.get("features") or []) if f["kind"] in ("wall", "shelf"))

    forms_same = _forms(topology_mod.from_books(books, S, F_ref, em=em, panel=bs)) == _forms(topo_i)
    return {
        "assumed": SIGN_ASSUMED,
        "source": _sign_source(books),
        "under_inversion": {
            "flips": "invariant by construction: G(F) -> -G(F) has the same zero crossings",
            "rails": "invariant: S +/- EM never reads the book",
            "node_ranking_identical": rank == rank_i,
            "node_ranking": rank,
            "node_ranking_inverted": rank_i,
            "formations_identical": forms_same,
            "gamma_regime": {"assumed": reg.get("label"), "inverted": reg_i.get("label"),
                             "score_assumed": reg.get("score"), "score_inverted": reg_i.get("score")},
            "what_inverts": [
                "the sign of every strike on the ladder, and which side of a formation is the call side",
                "the gamma regime label and score",
                "the vanna state",
                "every sentence in a scenario's risks or opposing evidence that reads a sign",
            ],
        },
        "note": ("This block measures the blast radius of the convention being wrong; it does not say "
                 "whether it is. dealer_flow.py is the measurement that could, and it has not been fed."),
    }


# ------------------------------------------------------------------ report ---

def _report_view(out, state, topo, nodes, scen, conf, S, em, top_n, profile=None, sign=None,
                 releases=None):
    """
    The trader-facing subset. RANKED AND FILTERED.

    Mark's brief: do not flood the trader with thirty important levels. The full
    node list stays in the document for anything that wants it; this carries the
    top few, each with the topology label and quality that earned it a place.
    """
    ns = (nodes.get("nodes") or [])[:top_n]
    dims = state.get("dimensions") or {}

    def dim(name, *keys):
        d = dims.get(name) or {}
        return {k: d.get(k) for k in keys if d.get(k) is not None}

    # RAIL SENSITIVITY (Nicky's ENVELOPE_SENSITIVITY, 2026-09-12). The rails are
    # the one validated component, so a call that depends on them deserves a
    # second look: is this level inside the band, and is it still inside a band
    # 15% wider? A level whose answer changes sits on the edge of the one
    # component the engine trusts, and the report says so.
    lo, hi, scale = out.get("rail_lo"), out.get("rail_hi"), float(out.get("rail_scale") or 1.0)
    half = (em or 0.0) * scale
    wide = half * RAIL_SENSITIVITY

    def rail(price):
        if lo is None or hi is None or not em:
            return {}
        inside = lo <= price <= hi
        inside_w = (S - wide) <= price <= (S + wide)
        return {"inside_rail": bool(inside), "inside_rail_wide": bool(inside_w),
                "rail_call_fragile": bool(inside != inside_w)}

    levels = [{
        "price": n["price"],
        "quality": n["quality"],
        "sign": n["sign"],
        "gamma": n["raw"]["gamma"],
        "oi": n["raw"]["oi"],
        "distance": n["raw"]["distance"],
        "distance_ems": n["raw"]["distance_ems"],
        "topology": (n.get("topology") or {}).get("kind"),
        "labels": (n.get("topology") or {}).get("labels") or [],
        "why": n["evidence"][:3],
        **rail(n["price"]),
    } for n in ns]

    flip = dim("flip_state", "label", "at_flip", "nearest_distance_ems", "confidence")
    per = ((dims.get("flip_state") or {}).get("per_book")) or {}
    crossings = {k: v.get("crossings") for k, v in per.items() if isinstance(v, dict) and v.get("crossings")}
    if crossings:
        flip["crossings"] = crossings
        flip["fragmented"] = any(c > 1 for c in crossings.values())

    prof = {}
    for k, v in (profile or {}).items():
        a = (v or {}).get("at_spot") or {}
        if a.get("g") is not None:
            prof[k] = {"gamma_at_spot": a.get("g"), "slope_at_spot": a.get("slope")}

    caveats = [
        "Structure and location are stated confidently; regime and range claims are not. "
        "The 60-session backtest found no support for a gamma sign implying a compressed or "
        "expanded range (p=0.46).",
        "NODES DO NOT PREDICT REACTIONS. Measured on 500 sessions and 4,556 touches "
        "(2024-09 to 2026-09): scored nodes reacted 56.8% against 57.0% for a "
        "distance-matched round-number placebo, an edge of -0.2pp (z=-0.17), and the "
        "quality buckets are not monotone. The ~57% is the touch geometry, not gamma. "
        "These levels mark where hedging concentrates, not where price turns.",
        "The rails are the one component with validation behind them: containment of the "
        "close ran 64-74% against a 68% target.",
        "No confidence figure here is a probability. None has been calibrated against outcomes.",
        "The dealer sign convention is an assumption, not an observation. `sign_convention` "
        "measures what the opposite book would change: the flips, the rails, the node ranking and "
        "the formations do not move; every sign and the gamma regime label do.",
    ]
    if releases:
        caveats.append("A level reached within minutes of a scheduled release is a release print, "
                       "not a hedging reaction. Whether touches inside release windows behave "
                       "differently is unmeasured.")

    # THE GATE DECIDES WHAT THE REPORT CARRIES (2026-09-12). Everything generated
    # stays under `scenarios.scenarios` in the document; the trader-facing list is
    # empty on a stand-aside book, short on a thin one, and says which.
    gate = scen.get("gate") or {}
    all_sc = scen.get("scenarios") or []
    n_carry = scen.get("report_carries", len(all_sc))
    live = [s for s in all_sc if not ((s.get("played") or {}).get("is_played"))] \
        if scen.get("filter_played") else all_sc
    carried = live[:n_carry]
    played = [s for s in all_sc if (s.get("played") or {}).get("is_played")]

    return {
        "levels": levels,
        "trade_gate": {
            "verdict": gate.get("verdict"),
            "text": gate.get("text"),
            "reasons": gate.get("reasons") or [],
            "scenarios_generated": len(all_sc),
            "scenarios_played": len(played),
            "scenarios_carried": len(carried),
            "live_print": scen.get("live_print"),
            "validated": False,
        } if gate else None,
        "played_paths": [{"rank": s["rank"], "side": s["side"], "trigger": (s["trigger"] or {}).get("level"),
                          "note": (s.get("played") or {}).get("note")} for s in played],
        "rail_sensitivity": {
            "factor": RAIL_SENSITIVITY,
            "fragile_levels": [l["price"] for l in levels if l.get("rail_call_fragile")],
            "basis": "each level's inside-the-rail call re-run against a band this much wider",
        },
        "state": {
            "gamma": dim("gamma_regime", "label", "strength", "score", "measures_agree", "confidence"),
            "concentration": dim("concentration", "label", "score", "confidence"),
            "flip": flip,
            "expiry": dim("expiry_state", "label", "gross_share", "net_share", "confidence"),
            "vanna": dim("vanna_state", "label", "score", "confidence"),
            "vol": dim("vol_state", "label", "atm_iv", "percentile", "confidence"),
        },
        "gamma_profile": prof,
        "sign_convention": {
            "assumed": (sign or {}).get("assumed", SIGN_ASSUMED),
            "source": (sign or {}).get("source", "convention"),
            "invariant_under_inversion": {
                k: ((sign or {}).get("under_inversion") or {}).get(k)
                for k in ("node_ranking_identical", "formations_identical")
            },
            "gamma_regime_if_inverted": (((sign or {}).get("under_inversion") or {}).get("gamma_regime") or {}).get("inverted"),
        },
        "releases": ({
            "summary": releases.get("summary"),
            "n_in_plan": releases.get("n_in_plan"),
            "events": [{k: e.get(k) for k in ("time_et", "time_utc", "title", "window", "in_plan",
                                              "minutes_from_rth_open")}
                       for e in (releases.get("events") or [])],
            "gate_rule": releases.get("gate_rule"),
            "error": releases.get("error"),
        } if releases else None),
        "scenarios": [{
            "rank": s["rank"],
            "label": s["label"],
            "side": s["side"],
            "trigger": (s["trigger"] or {}).get("rule"),
            "targets": [t["price"] for t in s["targets"]],
            "invalidation": (s["invalidation"] or {}).get("rule"),
            "risks": s["risks"],
            "opposing": s["opposing_evidence"][:2],
            "book_verdict": s.get("book_verdict"),
        } for s in carried],
        "confidence": {k: {"value": v["value"], "limited_by": v["limited_by"][:2]}
                       for k, v in conf["dimensions"].items()},
        "caveats": caveats,
    }


LLM_CONTRACT = {
    "may": [
        "write a concise premarket thesis in prose",
        "describe the base case and the alternatives already generated",
        "restate triggers, invalidations and risks verbatim from the document",
        "say plainly when something is missing or uncertain",
        "name a scheduled release by its time and say a level near it is a release print",
        "lead with `trade_gate` when its verdict is 'stand aside' or 'thin', and offer no trade on a stand-aside book",
    ],
    "must_not": [
        "modify any calculated value",
        "invent a level that is not in `nodes` or `report.levels`",
        "invent a probability, a percentage or a confidence figure",
        "recalculate any Greek, exposure, flip or expected move",
        "overwrite or reword a scenario trigger into a different condition",
        "manufacture data that is absent -- if a field is null, the narrative says it is missing",
        "convert a gamma sign into a directional call",
        "treat the dealer sign convention as an observation; it is an assumption and `sign_convention` says what it costs",
        "describe a trade on a book whose `trade_gate.verdict` is 'stand aside'; the paths were withheld on purpose",
    ],
    "why": ("Every number in this document is deterministic, reproducible and testable. A "
            "narrative that recomputes one of them breaks that chain and there is no way to "
            "tell from the output that it happened."),
}


def for_llm(doc):
    """
    The strict subset an interpretation layer may see.

    A subset rather than the whole document on purpose: the fewer raw internals
    an interpretation layer is handed, the less there is for it to recompute and
    quietly contradict.
    """
    rep = doc["report"]
    return {
        "source": doc["source"],
        "reference": doc["reference"],
        "flips": doc["flips"],
        "flip_crossings": doc.get("flip_crossings"),
        "net": doc["net"],
        "gamma_profile": rep.get("gamma_profile"),
        "market_state": rep["state"],
        "levels": rep["levels"],
        # THE MEASUREMENT RIDES WITH THE LEVELS IT QUALIFIES, as data, not only as
        # caveat prose. Not under `nodes`: that key is the scored internals and the
        # contract keeps it out of this view.
        "levels_measured": (doc.get("nodes") or {}).get("measured"),
        "rail_sensitivity": rep.get("rail_sensitivity"),
        "sign_convention": rep.get("sign_convention"),
        "trade_gate": rep.get("trade_gate"),
        "releases": rep.get("releases"),
        "topology": [{k: f[k] for k in ("kind", "lower", "upper", "center", "sign",
                                        "width", "strength", "labels")}
                     for f in doc["topology"].get("features", [])
                     if f["kind"] in ("wall", "shelf") or "vacuum" in f.get("labels", [])],
        "expiry": {
            "zero_dte": doc["expiry"].get("zero_dte"),
            "buckets": {k: {"gamma_share": v.get("gamma_share"),
                            "gamma_gross_share": v.get("gamma_gross_share"),
                            "expiries": v.get("expiries")}
                        for k, v in (doc["expiry"].get("buckets") or {}).items()},
        },
        "scenarios": rep["scenarios"],
        "confidence": rep["confidence"],
        "data_quality": (doc["market_state"].get("data_quality") or {}),
        "cross_asset": doc.get("cross_asset"),
        "caveats": rep["caveats"],
        "units": doc["units"],
        "llm_contract": LLM_CONTRACT,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain")
    ap.add_argument("--report-date")
    ap.add_argument("--from-trinity", action="store_true",
                    help="take the chain and report date from the existing trinity.json")
    ap.add_argument("--panel", default=DEFAULT_PANEL, choices=["0DTE", "NEAR", "FULL"])
    ap.add_argument("--hours", type=float, default=6.5)
    ap.add_argument("--spot", type=float, default=None)
    ap.add_argument("--out", default="state.json")
    ap.add_argument("--llm-out", default=None, help="also write the LLM subset here")
    ap.add_argument("--no-history", action="store_true", help="skip the vol percentile")
    ap.add_argument("--no-releases", action="store_true",
                    help="do not read or fetch the week's release calendar")
    ap.add_argument("--no-sign-sensitivity", action="store_true",
                    help="skip the opposite-book check")
    ap.add_argument("--last", type=float, default=None,
                    help="the live print; --from-trinity inherits the sheet's last_print")
    ap.add_argument("--keep-played", action="store_true",
                    help="carry paths the live print is already beyond in the report (the pre-2026-09-12 behaviour)")
    # SOFT MODE, for the scheduled pipeline.
    #
    # autopull's render loop fails the whole job on any non-zero exit, and this
    # step is OPTIONAL: state.json feeds the market-state panel and the commentary
    # payload, and a sheet without it is the sheet the project had before this
    # layer existed. A failure here must never cost Mark a premarket build.
    #
    # `run()` in autopull carries the same principle in its docstring: "a scheduled
    # task that throws is a scheduled task you stop trusting."
    ap.add_argument("--soft", action="store_true",
                    help="report failures on stdout and exit 0; for the scheduled pipeline")
    a = ap.parse_args()
    if a.soft:
        try:
            return _main(ap, a)
        except SystemExit as e:
            if e.code not in (0, None):
                print(f"engine.py skipped (soft): {e}")
            return
        except Exception as e:
            print(f"engine.py skipped (soft): {type(e).__name__}: {e}")
            return
    return _main(ap, a)


def _main(ap, a):

    chain, rd = a.chain, a.report_date
    hours, spot, last = a.hours, a.spot, a.last
    ref = getattr(a, "ref", None)
    if a.from_trinity:
        T = json.loads((ROOT / "trinity.json").read_text(encoding="utf-8"))
        rd = rd or T.get("report_date")
        settle = T.get("settle")
        prelim = ROOT / "data" / "prelim" / f"{settle}-chain.json"
        final = ROOT / "data" / f"{settle}-chain.json"
        chain = chain or str(prelim if (T.get("oi_vintage") == "preliminary" and prelim.exists()) else final)
        # INHERIT THE HOURS AND THE REFERENCE, not just the chain.
        #
        # Found live on 2026-09-10. --from-trinity took the chain and the report
        # date and then used its own defaults for everything else, so on an
        # intraday sheet it built the PREMARKET view: 6.5 hours against the
        # sheet's 3.96, the settle at 29,448.75 against the sheet's 29,216
        # reference, and a NEAR book reading +1,269 beside a sheet showing -857.
        # The panel would have sat under the ladder contradicting it.
        #
        # `hours` in trinity.json is the plan's as-of offset to the report unit's
        # close, which is exactly what analyse() wants. `spot` IS the live print on
        # an intraday or asia build, because those pass it as --spot; on a
        # premarket build it is the settle, which is also correct.
        if hours == 6.5 and T.get("hours"):
            hours = float(T["hours"])
        if spot is None and T.get("spot"):
            spot = float(T["spot"])
        # and the live print, which decides which paths the overnight has played
        if last is None and T.get("last_print"):
            last = float(T["last_print"])
        # AND THE REFERENCE CONTRACT, not just the reference price. Without this
        # analyse() falls back to the front book's own underlying while `spot`
        # above is the overridden one, so the flips and every level are computed
        # on one axis and price on another -- in ONE state.json, with nothing to
        # say so. Found 2026-09-14 on the NQU6 -> NQZ6 roll: spot 29,483 (NQZ6)
        # beside flips 29,393 / 29,339 / 29,292 (NQU6), which flipped the read
        # from "below all three" to "above all three" and inverted every
        # scenario with it. The comment above says "not just the chain"; the
        # reference SYMBOL is the other half of what has to be inherited.
        if ref is None and T.get("ref_symbol"):
            ref = T["ref_symbol"]
    if not chain or not rd:
        ap.error("need --chain and --report-date, or --from-trinity")

    hist = []
    if not a.no_history:
        import build_iv_history
        hist = build_iv_history.load()

    doc = run(chain, rd, panel=a.panel, hours=hours, spot=spot, iv_history=hist,
              releases=(None if a.no_releases else "auto"),
              sign_sensitivity=not a.no_sign_sensitivity,
              last=last, filter_played=not a.keep_played, ref=ref)
    Path(a.out).write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print(f"wrote {a.out}")

    if a.llm_out:
        Path(a.llm_out).write_text(json.dumps(for_llm(doc), indent=1), encoding="utf-8")
        print(f"wrote {a.llm_out}")

    r = doc["report"]
    st = r["state"]
    print(f"\n  {doc['source']['report_date']}  ref {doc['reference']['spot']:,.2f}  "
          f"EM +/-{doc['reference']['em']:,.0f}  panel {doc['source']['panel_used']}"
          + ("  (requested panel was empty)" if doc['source']['panel_was_empty'] else ""))
    print(f"  gamma {st['gamma'].get('label')} ({st['gamma'].get('strength')}), "
          f"measures agree: {st['gamma'].get('measures_agree')}   "
          f"concentration {st['concentration'].get('label')}   "
          f"flip {st['flip'].get('label')}"
          + ("   FRAGMENTED " + str(st['flip'].get('crossings')) if st['flip'].get('fragmented') else ""))
    print(f"  vol {st['vol'].get('label')} at {st['vol'].get('atm_iv', 0):.4f}"
          + (f", percentile {st['vol']['percentile']:.2f}" if st['vol'].get('percentile') else "")
          + f"   0DTE {st['expiry'].get('label')}")
    gp = r.get("gamma_profile") or {}
    if gp:
        print("  gamma profile at spot: " + "   ".join(
            f"{k} G {v['gamma_at_spot']:+,.0f} slope {v['slope_at_spot']:+.2f}/pt"
            for k, v in gp.items() if v.get("slope_at_spot") is not None))
    sc = r.get("sign_convention") or {}
    inv = sc.get("invariant_under_inversion") or {}
    print(f"  sign convention: {sc.get('source')}; if inverted the gamma regime reads "
          f"{sc.get('gamma_regime_if_inverted')}, node ranking identical: {inv.get('node_ranking_identical')}, "
          f"formations identical: {inv.get('formations_identical')}")
    if r.get("releases"):
        print(f"  releases: {r['releases'].get('summary')}")
    tg = r.get("trade_gate") or {}
    if tg:
        print(f"  TRADE GATE: {str(tg.get('verdict')).upper()} -- {tg.get('text')}"
              f"  ({tg.get('scenarios_carried')} of {tg.get('scenarios_generated')} paths carried"
              + (f", {tg.get('scenarios_played')} already played by the live print {tg.get('live_print'):,.2f}"
                 if tg.get("live_print") is not None else ", no live print on this build") + ")")
        for why in tg.get("reasons") or []:
            print(f"       - {why}")
    print(f"\n  {'level':>10} {'qual':>6} {'sign':>5} {'gamma':>9} {'oi':>8} {'dist':>8}  topology")
    for l in r["levels"]:
        print(f"  {l['price']:>10,.0f} {l['quality']:>6.2f} {l['sign']:>+5d} "
              f"{l['gamma']:>9,.1f} {l['oi']:>8,} {l['distance']:>+8,.0f}  "
              f"{l['topology'] or '-'}" + (f" ({', '.join(l['labels'])})" if l['labels'] else "")
              + ("  [rail call fragile]" if l.get("rail_call_fragile") else ""))
    print()
    if tg and not r["scenarios"]:
        print(f"  (no scenarios carried: {tg.get('verdict')})")
    for s in r["scenarios"]:
        print(f"  #{s['rank']} {s['label']:<22} {s['trigger'] or '(no trigger)'}")
        print(f"       targets {', '.join(f'{t:,.0f}' for t in s['targets']) or '(none)'}"
              f"   invalidation {s['invalidation'] or '(none)'}")
    print()
    for k, v in r["confidence"].items():
        print(f"  confidence.{k:<12} {('%.2f' % v['value']) if v['value'] is not None else 'none':>6}"
              + (f"   limited by: {v['limited_by'][0][:70]}" if v["limited_by"] else ""))
    print()


if __name__ == "__main__":
    main()
