#!/usr/bin/env python3
"""
interpret.py -- the OPTIONAL narrative layer, and the checker that polices it.

    import interpret
    ok, problems = interpret.verify(text, doc)      # no API key needed
    out = interpret.interpret(doc)                  # needs ANTHROPIC_API_KEY

The deterministic engine produces `state.json`. This turns it into prose. The
interesting half of this file is not the API call -- it is `verify()`, which
checks the prose back against the document.

--------------------------------------------------------------------------------
WHY A CHECKER AND NOT JUST A PROMPT
--------------------------------------------------------------------------------

"Do not invent a level" is a rule a prompt can state and cannot enforce. The
project already learned this the expensive way on the other narrative path:
`command_center` does not trust the model's stops, it RECOMPUTES them from the
gamma map and overrides the reply when they differ. Same principle here.

`verify()` is the enforcement, and it needs no API key, so it is fully testable:

  NUMBERS      every number in the prose must be one the document contains, a
               plain rounding of one, or a DIFFERENCE between two of them --
               "61 points above 29,600" is legitimate arithmetic on published
               values. Anything else is invented and is named.
  LEVELS       any price-shaped number must be a level, flip, rail, trigger,
               target or invalidation that actually exists in the document.
  PROBABILITY  the engine calibrates nothing, so a percentage that is not in the
               document is refused outright. This is the failure mode with the
               worst consequences: a fabricated "72% of the time" reads as
               evidence and is not.
  TRIGGERS     each scenario's trigger must appear verbatim if that scenario is
               named. A reworded trigger is a different condition.
  MISSING DATA when the document carries a null the narrative must say so rather
               than filling the gap.

--------------------------------------------------------------------------------
WHAT THE MODEL IS FOR
--------------------------------------------------------------------------------

Ordering, emphasis and English. It is not for arithmetic, and every quantity it
needs is already in the payload. The contract travels inside the payload as
`llm_contract`, so it cannot drift away from the data it governs.

Written 2026-09-09, Phase I of the market-state upgrade.
"""

import os
import re
import json
import math
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent

MODEL = os.environ.get("CC_COMMENTARY_MODEL", "claude-opus-5")
MAX_TOKENS = 16000

# A number that looks like an index price. NQ trades near 29,000, SPX near 7,700;
# anything at or above this is treated as a level claim and must be a real one.
PRICE_FLOOR = 1000.0
NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\w])")
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|per\s?cent|percent)")


BRIEF = """You are writing the premarket note for TeddyAI, an NQ dealer-gamma sheet, for a
discretionary intraday trader who already knows the method. Every quantity you need is in the
JSON. You are writing English, not doing arithmetic.

Return ONLY a JSON object, no preamble and no fences:

{"thesis": "<2-4 sentences: where the reference sits against the flips and the structure, which
            levels are heavy and which stretch is thin, and what decides the session>",
 "base_case": "<2-3 sentences on scenario rank 1: its trigger VERBATIM, its targets, its
                invalidation, and the structure behind it>",
 "alternatives": ["<one sentence per remaining scenario, each naming its trigger verbatim>"],
 "risks": ["<what would make this map wrong today>"],
 "no_trade": "<the conditions under which the map offers nothing worth acting on>"}

HARD RULES, enforced by a checker that reads your reply back against the JSON:

1. Every number you write must appear in the JSON, be a plain rounding of one, or be a
   DIFFERENCE between two of them. Nothing else.
2. Never write a percentage or a probability that is not already in the JSON. The engine
   calibrates nothing. A fabricated "holds 70% of the time" reads as evidence and is not.
3. Quote each scenario trigger exactly as `trigger` gives it. A reworded trigger is a
   different condition.
4. Never turn a gamma sign into a direction. Positive gamma at a strike is not "support".
   Structure says where hedging concentrates; it does not say which way price goes.
5. Where a field is null, say it is missing. Do not fill the gap.
6. Do not restate the method and do not add disclaimers beyond the caveats already given.
"""


# --------------------------------------------------------------- permitted --

def _walk(obj, strings=True, keys=False):
    """Every number in the document, INCLUDING those inside strings.

    Numbers embedded in text are legitimately quotable: a trigger reads
    "acceptance above 29,515" and a card reads "1.4R", and neither exists as a
    numeric field. Harvesting only numeric values made the checker reject the
    narrative for quoting a trigger correctly, which is the one thing it most
    wants to see.

    `keys` matters for trinity.json, where the STRIKE IS THE KEY: a gamma map is
    {"29600.0": 255}, so walking values alone harvests 255 and misses the level
    entirely. Off by default because state.json keys are field names.
    """
    if isinstance(obj, dict):
        if keys:
            for k in obj:
                if isinstance(k, str):
                    for m in NUM_RE.finditer(k):
                        try:
                            yield float(m.group(1).replace(",", ""))
                        except ValueError:
                            continue
        for v in obj.values():
            yield from _walk(v, strings, keys)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v, strings, keys)
    elif isinstance(obj, bool):
        return
    elif isinstance(obj, (int, float)):
        yield float(obj)
    elif strings and isinstance(obj, str):
        for m in NUM_RE.finditer(obj):
            try:
                yield float(m.group(1).replace(",", ""))
            except ValueError:
                continue


def permitted_numbers(doc):
    """
    Every number the narrative is allowed to use.

    Three sources, in order of how much latitude they represent:
      * every numeric value anywhere in the document
      * their plain roundings, because prose says 29,600 for 29,600.0
      * pairwise differences between PRICE-LIKE values, because "61 points above"
        is arithmetic on published numbers and refusing it would make the prose
        unwritable
    """
    vals = {round(v, 6) for v in _walk(doc) if math.isfinite(v)}
    out = set(vals)
    for v in vals:
        out.add(round(v))
        out.add(round(v, 1))
        out.add(round(v, 2))
        out.add(round(abs(v)))
    prices = sorted(v for v in vals if abs(v) >= PRICE_FLOOR)
    for i, a in enumerate(prices):
        for b in prices[i + 1:]:
            d = round(abs(b - a), 2)
            out.add(d)
            out.add(round(d))
    return out


def pools_from(*objs):
    """Numbers and price-shaped numbers from any additional documents the
    narrative was written against.

    The commentary is written against the WHOLE payload -- trinity.json, the tape
    summary and state.json -- so the permitted universe has to be the whole
    payload too. Checking prose against state.json alone produced 63 false
    positives on a real sheet: it flagged the open interest (238,570), the term
    structure (17.24%) and the last print (29,375.75), every one of which is a
    real figure the model was handed in trinity.json.

    Dict KEYS are harvested here, because in trinity.json the strike is the key.
    """
    nums, prices = set(), set()
    for o in objs:
        if o is None:
            continue
        for v in _walk(o, strings=True, keys=True):
            if not math.isfinite(v):
                continue
            nums.add(round(v, 6)); nums.add(round(v, 2)); nums.add(round(v, 1)); nums.add(round(v))
            if abs(v) >= PRICE_FLOOR:
                prices.add(round(v))
    return nums, prices


def permitted_prices(doc):
    """Price-shaped values a narrative may name as a level."""
    out = set()

    def add(v):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) >= PRICE_FLOOR:
            out.add(round(float(v)))

    for l in doc.get("levels", []) or (doc.get("report", {}) or {}).get("levels", []):
        add(l.get("price"))
    for f in (doc.get("flips") or {}).values():
        add(f)
    r = doc.get("reference") or {}
    for k in ("spot", "ref_settle", "last_print", "rail_lo", "rail_hi"):
        add(r.get(k))
    scen = doc.get("scenarios") or (doc.get("report", {}) or {}).get("scenarios", [])
    # engine.run() writes state.json with the list NESTED under scenarios.scenarios,
    # beside its own metadata; the report view carries a flat list. The `or` above
    # short-circuits on the truthy dict, so without this the loop iterated the dict's
    # KEYS and died on "'str' object has no attribute 'get'" -- which took the whole
    # commentary gate down on any real state.json. Accept both shapes.
    if isinstance(scen, dict):
        scen = scen.get("scenarios") or []
    for s in scen:
        if not isinstance(s, dict):
            continue
        for t in (s.get("targets") or []):
            add(t if isinstance(t, (int, float)) else t.get("price"))
        for key in ("trigger", "invalidation"):
            v = s.get(key)
            if isinstance(v, dict):
                add(v.get("level"))
            elif isinstance(v, str):
                for m in NUM_RE.finditer(v):
                    add(float(m.group(1).replace(",", "")))
    topo = doc.get("topology") or []
    # Same shape trap as `scenarios` above: engine.run() nests the list under
    # topology.features beside the scale/threshold metadata, while the report view
    # passes a bare list. Iterating the dict yields its KEYS.
    if isinstance(topo, dict):
        topo = topo.get("features") or []
    for f in topo:
        if not isinstance(f, dict):
            continue
        for k in ("lower", "upper", "center", "peak_price"):
            add(f.get(k))
    return out


def permitted_pcts(doc):
    """Percentages already in the document, as written and as decimals."""
    out = set()
    # NUMERIC values only. Sweeping numbers out of strings here would make every
    # digit that appears in prose a permitted percentage, and the strictest check
    # in the file would stop catching fabricated rates.
    for v in _walk(doc, strings=False):
        if not math.isfinite(v):
            continue
        if 0.0 <= abs(v) <= 1.0:
            out.add(round(abs(v) * 100, 1))
            out.add(round(abs(v) * 100))
        if 0.0 <= abs(v) <= 100.0:
            out.add(round(abs(v), 1))
            out.add(round(abs(v)))
    return out


# ------------------------------------------------------------------ verify --

def _text_of(narr):
    if isinstance(narr, str):
        return narr
    parts = []
    for v in (narr or {}).values():
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            parts.extend(str(x) for x in v)
    return "\n".join(parts)


def verify(narrative, doc, tol=0.51, also=()):
    """
    Check a narrative back against the document. Returns (ok, problems).

    `tol` is absolute and defaults to just over half a point, so a price written
    as a round number passes and a genuinely different level does not.
    """
    text = _text_of(narrative)
    problems = []

    nums = permitted_numbers(doc)
    prices = permitted_prices(doc)
    pcts = permitted_pcts(doc)
    # Everything else the narrative was written against -- trinity.json, the tape.
    if also:
        extra_n, extra_p = pools_from(*(also if isinstance(also, (list, tuple)) else [also]))
        nums |= extra_n
        prices |= extra_p
        # a percentage present as a decimal anywhere in the payload is quotable
        pcts |= {round(v * 100, 1) for v in extra_n if 0.0 <= v <= 1.0}
        pcts |= {round(v * 100) for v in extra_n if 0.0 <= v <= 1.0}
        pcts |= {round(v, 1) for v in extra_n if 0.0 <= v <= 100.0}

    def known(x, pool, t=tol):
        return any(abs(x - p) <= t for p in pool)

    # 1. percentages -- the strictest check, because a fabricated rate reads as evidence
    for m in PCT_RE.finditer(text):
        v = float(m.group(1))
        if not known(v, pcts, 0.6):
            problems.append(f"invented percentage: {m.group(0)!r} is not in the document. "
                            f"The engine calibrates nothing; a rate that is not published "
                            f"must not be written.")

    # 2. every other number must be supported, and a price-shaped one must be a level.
    #
    # ORDER MATTERS. Checking "is it price-shaped" first flagged an open-interest
    # figure of 1,809 as an invented level, because open interest, gamma and
    # contract counts all run into the thousands. A number that appears ANYWHERE
    # in the document is legitimate whatever its size; only a number that appears
    # nowhere gets classified, and the price test then decides which complaint to
    # make.
    #
    # "Price-shaped" is judged against the document's OWN reference rather than a
    # fixed constant, so it works on QQQ near 718 as well as on NQ near 29,500.
    # PRICE_FLOOR is the FALLBACK, not a lower bound on the derived floor. Taking
    # max() of the two put the floor at 1,000 on a QQQ document whose reference
    # is 718, so an invented ETF level fell below it and was demoted to a mere
    # "unsupported number" complaint.
    ref = float((doc.get("reference") or {}).get("spot") or 0.0)
    floor = (0.5 * ref) if ref else PRICE_FLOOR

    pct_spans = [m.span() for m in PCT_RE.finditer(text)]
    for m in NUM_RE.finditer(text):
        if any(a <= m.start() < b for a, b in pct_spans):
            continue
        v = float(m.group(1).replace(",", ""))
        if known(v, nums):
            continue
        if abs(v) >= floor:
            problems.append(f"invented level: {m.group(1)} is not a level, flip, rail, "
                            f"trigger, target or invalidation in the document")
        else:
            problems.append(f"unsupported number: {m.group(1)} does not appear in the document "
                            f"and is not a difference between two values that do")

    # 3. triggers must be verbatim
    scen = doc.get("scenarios") or (doc.get("report", {}) or {}).get("scenarios", [])
    # engine.run() writes state.json with the list NESTED under scenarios.scenarios,
    # beside its own metadata; the report view carries a flat list. The `or` above
    # short-circuits on the truthy dict, so without this the loop iterated the dict's
    # KEYS and died on "'str' object has no attribute 'get'" -- which took the whole
    # commentary gate down on any real state.json. Accept both shapes.
    if isinstance(scen, dict):
        scen = scen.get("scenarios") or []
    for s in scen:
        if not isinstance(s, dict):
            continue
        trig = s.get("trigger")
        trig = trig.get("rule") if isinstance(trig, dict) else trig
        if not trig:
            continue
        label = (s.get("label") or "").lower()
        if label and label in text.lower() and trig.lower() not in text.lower():
            problems.append(f"scenario {s.get('label')!r} is named but its trigger is not quoted "
                            f"verbatim; expected {trig!r}. A reworded trigger is a different condition.")

    # 4. missing data must be acknowledged
    for path, label in (("confidence.directional", "directional confidence"),):
        d = (doc.get("confidence") or {}).get("directional") or {}
        if d.get("value") is None:
            low = text.lower()
            if not any(w in low for w in ("no direction", "not directional", "carries no direction",
                                          "no directional", "does not say which way",
                                          "structure does not", "no view on direction")):
                problems.append("the document has NO directional confidence and the narrative does "
                                "not say so; a reader will assume a direction was assessed")

    # 5. the forbidden conversion
    for pat, why in (
        (r"positive gamma[^.]{0,40}\bsupport\b", "positive gamma described as support"),
        (r"negative gamma[^.]{0,40}\bresistance\b", "negative gamma described as resistance"),
        (r"\bgamma\b[^.]{0,40}\b(?:bullish|bearish)\b", "a gamma reading described as bullish or bearish"),
    ):
        if re.search(pat, text, re.I):
            problems.append(f"forbidden conversion: {why}. Structure says where hedging "
                            f"concentrates, not which way price goes.")

    return (not problems), problems


# ----------------------------------------------------- the card level check --

TARGET_TOL = 1.0      # a target is a level; it should land ON one
ANCHOR_TOL = 20.0     # an entry or a stop sits deliberately PAST its level

def check_card_levels(cards, T, target_tol=TARGET_TOL, anchor_tol=ANCHOR_TOL):
    """
    Are a card's prices real? Returns (ok, problems).

    This is the hole the existing checks leave open. `_check_cards` tests the
    card's SHAPE -- four cards, three targets, ordering, TP1 at 1.2R -- and
    `_structure_check` recomputes the STOP from the gamma map and overrides it.
    Neither has ever asked whether the ENTRY and the TARGETS are prices that exist.
    A card could name 29,555 as its second target with no such strike on the board
    and nothing would notice.

    TWO TOLERANCES, because the three fields are not the same kind of number.
    Measured on the 2026-09-10 sheet's four cards:

        targets        all twelve landed EXACTLY on a listed strike, 0.0 pts
        entries        2.0 to 5.0 pts off -- the brief says "a few points past
                       the level", so the offset is the design
        stops          2.0 to 5.0 pts off, for the same reason

    So a target is held to a point and an anchor is given twenty. Twenty is wide
    enough for every offset the sheet has ever used and far too narrow to admit a
    price that is not near structure at all.

    The universe is trinity.json's own: every strike in any panel, the three
    flips, and the two rails. That is broader than state.json's ranked levels,
    which is correct -- a card may legitimately target a quiet strike that did not
    make the shortlist.
    """
    P = (T or {}).get("panels") or {}
    pool = {float(k) for p in P.values() for k in ((p.get("gamma") or {}))}
    for p in P.values():
        for f in (p.get("hvl") or []):
            if f:
                pool.add(float(f))
    for k in ("rail_lo", "rail_hi", "spot", "last_print", "ref_settle"):
        v = (T or {}).get(k)
        if v:
            pool.add(float(v))
    if not pool:
        return True, []                      # nothing to check against; do not invent a failure

    problems = []
    for c in cards or []:
        name = c.get("name", "?")
        tps = c.get("tp") or []
        tps = tps if isinstance(tps, (list, tuple)) else [tps]
        for i, t in enumerate(tps):
            if not isinstance(t, (int, float)):
                continue
            d = min(abs(float(t) - x) for x in pool)
            if d > target_tol:
                problems.append(
                    f"card {name}: target {i + 1} at {float(t):,.0f} is not a level -- the nearest "
                    f"strike, flip or rail is {d:,.0f} points away. Targets must be prices that exist.")
        for lab in ("entry", "sl"):
            v = c.get(lab)
            if not isinstance(v, (int, float)):
                continue
            d = min(abs(float(v) - x) for x in pool)
            if d > anchor_tol:
                problems.append(
                    f"card {name}: {lab} at {float(v):,.0f} is {d:,.0f} points from the nearest "
                    f"structure. An entry sits a few points past a level, not in open space.")
    return (not problems), problems


# Half the drawn band. NOT scenario.Config.boundary_ems (1.0): that governs where the
# SCENARIO generator seeks trigger levels, and measured on the card archive 0.5 beats it
# on both fill and win rate. Measured, not inherited -- see check_card_reach.
REACH_EMS = 0.5
DAY_EMS = 1.0        # the day's map: how far a card may sit and still belong on the sheet at all


def bands(T):
    """The two frames a card is judged in. They answer different questions.

    LIVE is the drawn band -- em x rail_scale -- the move that is left between now
    and the expiry. It answers "can price get here before the sheet is rebuilt".

    DAY is the session expected move, unscaled. It answers "is this one of today's
    levels". It does not shrink as the session runs down, because the board does
    not: 29,600 was the largest gamma print on the 2026-09-16 board at 17:00 the
    night before and still was at 08:00 the next morning.

    Collapsing the two made the sheet a snapshot of wherever price happened to sit
    when the build ran (Mark, 2026-09-16: "the gameplan is being forced to create
    scenarios at the time the commentary is forced to generate but not an overall
    read on the day"). That day nine sheets were built and the top node on the board
    was carded by none of them -- outside half the REMAINING band at every build,
    while never once leaving the day's map.
    """
    T = T or {}
    ref = T.get("last_print") or T.get("spot")
    em = float(T.get("em") or 0)
    live = em * float(T.get("rail_scale") or 1.0)
    day = float(T.get("em_full") or em or 0)
    return (float(ref) if ref else None), live, day


def reach_state(card, T, max_ems=None, day_ems=None):
    """Where one card sits: 'live', 'watch' or 'off-map'.

    live     inside REACH_EMS of the reference -- price can plausibly get there in
             what is left of the session, so the card is actionable now.
    watch    outside that but inside the day's map -- a real level on today's board
             that price is not near yet. It SHIPS, flagged, because a level the
             sheet does not name is a level nobody is watching for.
    off-map  beyond the session expected move. Still refused: at that distance the
             card is not a plan for today.
    """
    max_ems = REACH_EMS if max_ems is None else max_ems
    day_ems = DAY_EMS if day_ems is None else day_ems
    ref, live, day = bands(T)
    e = (card or {}).get("entry")
    if not ref or not live or not isinstance(e, (int, float)):
        return {"state": "live", "dist": None, "live_ems": None, "day_ems": None,
                "live_window": None, "day_window": None}
    d = abs(float(e) - ref)
    state = ("live" if d <= max_ems * live
             else "watch" if day and d <= day_ems * day
             else "off-map")
    return {"state": state, "dist": d,
            "live_ems": (d / live if live else None),
            "day_ems": (d / day if day else None),
            "live_window": (ref - max_ems * live, ref + max_ems * live),
            "day_window": ((ref - day_ems * day, ref + day_ems * day) if day else None)}


def check_card_reach(cards, T, max_ems=None, day_ems=None):
    """
    Is the card near enough to price to fill? Returns (ok, problems).

    THIS IS NOT A NEW GATE. `scenario.Config.boundary_ems` has said since the
    module was written that trigger levels are sought within one expected move of
    price -- but that config governs the SCENARIO generator, and the cards are not
    generated, they are written by the commentary model from the levels it is
    handed. Nothing has ever applied the engine's own reach rule to them, so the
    cards have been free to sit anywhere on the board.

    Measured over 189 archived cards with a real expected move on their build:

        distance / EM      n     fill rate
        0.00 - 0.15       12       67%
        0.15 - 0.30       19       42%
        0.30 - 0.50       25       52%
        0.50 - 0.75       28       46%
        0.75 - 1.00       32       34%
        over 1.00         73       22%

    Thirty-nine per cent of every card ever written sat beyond the one EM the
    config already claims to enforce, and those cards filled at 22%. Across the
    whole archive 71% of cards never filled at all -- the single largest defect in
    the record, and larger than anything measurable about whether the cards were
    RIGHT, because a card that never fills is not a trade, it is a line.

    HALF A BAND, NOT A WHOLE ONE (2026-09-15, Mark: "half a band, use 0.5 EM").
    The archive regraded over each sheet's own window, with rebuilds of one plan
    counted once:

        cap 0.50 EM   keeps 43 cards   fill 44%   win|fill 63%   (12W / 7L)
        cap 0.75 EM   keeps 61 cards   fill 43%   win|fill 50%
        cap 1.00 EM   keeps 90 cards   fill 37%   win|fill 52%

    Better on both axes than the config's 1.0, and it holds under
    leave-one-session-out: ten refits, 56% to 85%, none collapsing. The filled
    winners sit at a median 0.47 EM and the losers at 0.56, so distance does not
    predict whether a card WINS -- it predicts whether you ever get to find out.

    There is deliberately NO lower bound. Cards very close to price looked weak
    (0.10-0.25 EM won 25%) but that bin holds eight filled cards and the one
    inside it does not; a floor is not supported by this record.

    It reports a problem rather than dropping the card, so the corrective round
    re-picks a nearer level and the plan still carries its four setups. The reach
    is measured from the reference the sheet is drawn from, in units of the band
    it actually draws (em x rail_scale), so an Asia sheet is judged on the Asia
    band and an RTH sheet on the RTH one.
    """
    ref, live, day = bands(T)
    if not ref or not live:
        return True, []                      # no reference or no band; do not invent a failure
    day_ems = DAY_EMS if day_ems is None else day_ems
    problems = []
    for c in cards or []:
        if not isinstance((c or {}).get("entry"), (int, float)):
            continue
        r = reach_state(c, T, max_ems=max_ems, day_ems=day_ems)
        c["reach"] = r["state"]              # 'live' or 'watch' -- the page and the tracker read it
        if r["state"] == "off-map":
            lo, hi = r["day_window"]
            problems.append(
                f"card {c.get('name', '?')}: entry {float(c['entry']):,.0f} is {r['dist']:,.0f} points "
                f"from the reference {ref:,.0f} -- {r['day_ems']:.2f} of the session expected move, off "
                f"today's map entirely. The day's map runs {lo:,.0f} to {hi:,.0f}; write this card at a "
                f"level inside it. (A card past {REACH_EMS:.2f} of the drawn band but inside the map is "
                f"NOT a problem -- it ships flagged as a watch.)")
    return (not problems), problems


# A commentary problem blocks only when it makes the prose WRONG. The three that
# do are a fabricated rate, a reworded trigger, and a gamma sign written as a
# direction: each of those puts something in front of a trader that is not true.
#
# "the document has NO directional confidence" is deliberately NOT here, though it
# IS blocking in verify() for the standalone narrative path. The difference is
# that the sheet already carries the disclaimer structurally: build.py's _caveat()
# generates "Do not read a range from the gamma sign" on every build, and the
# method footer says gamma is not a directional signal. The commentary omitting it
# is an incompleteness, not a falsehood, and blocking on it would send an
# otherwise-correct plan round the retry loop. On the standalone path the
# narrative IS the whole output and nothing else carries that caveat, so there it
# still blocks.
BLOCKING_PREFIXES = ("invented percentage", "scenario ", "forbidden conversion")


def verify_commentary(C, state, T=None, tape=None):
    """
    The commentary policy: which of verify()'s findings may STOP a plan.

    Returns (blocking, advisory). Measured on the 2026-09-10 sheet, which is a
    correctly written plan, so anything it trips is a false positive:

        block   invented percentage, reworded trigger, gamma written as a
                direction, an unacknowledged null. NONE of these has a legitimate
                exception in any block of the sheet.
        advise  a price or a number the payload does not contain. This one DOES
                have legitimate exceptions and cannot be blocking:

                  * the READING block is a RETROSPECTIVE. It reviews what
                    yesterday's cards did and quotes yesterday's entries, stops
                    and flips, none of which are in today's documents. On the
                    2026-09-10 sheet that alone accounts for 9 of them.
                  * derived aggregates are legitimate. The lead quotes total open
                    interest of 238,570, which is the sum of `by_expiry` and
                    appears nowhere as a field.

    The real protection for the numbers a trader ACTS on is check_card_levels(),
    which is blocking and which that same sheet passes cleanly. This function
    guards the prose around them.
    """
    _ok, probs = verify(prose_of(C), state, also=[x for x in (T, tape) if x])
    blocking = [p for p in probs if p.startswith(BLOCKING_PREFIXES)]
    advisory = [p for p in probs if not p.startswith(BLOCKING_PREFIXES)]
    return blocking, advisory


def prose_of(C):
    """The narrative half of a commentary reply, for verify().

    The setups are deliberately EXCLUDED. Their entries and stops are offset from
    their levels by design, so running them through the level check in verify()
    would reject a correctly written card. They get check_card_levels() instead,
    which knows about the offsets. What is kept is everything a trader READS: the
    lead, the reading bullets, the VEX note, and each card's reasoning.
    """
    out = {"lead": (C or {}).get("lead", ""),
           "vex_note": (C or {}).get("vex_note", ""),
           "reading": list((C or {}).get("reading") or [])}
    out["why"] = [str(c.get("why", "")) for c in ((C or {}).get("setups") or [])]
    return out


# ---------------------------------------------------------------- the call --

def build_prompt(doc):
    return BRIEF + "\n\nstate.json:\n" + json.dumps(doc, indent=1)


def interpret(doc, model=None, rounds=2, _retry="", _round=0):
    """
    Ask the model, then check the reply. A failed check is fed back once with the
    specific problems, which is the shape command_center's card checker already
    uses. Requires ANTHROPIC_API_KEY; nothing here runs without it.
    """
    import urllib.request
    import urllib.error

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set; interpret() makes a paid API call. "
                           "verify() needs no key and is what the tests exercise.")

    body = {
        "model": model or MODEL,
        "max_tokens": MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
        "system": "You return exactly one JSON object and nothing else.",
        "messages": [{"role": "user",
                      "content": build_prompt(doc) + (f"\n\n{_retry}" if _retry else "")}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", method="POST",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        data=json.dumps(body).encode("utf-8"))
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read().decode("utf-8"))

    text = "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type", "text") == "text")
    (ROOT / "notes").mkdir(exist_ok=True)
    (ROOT / "notes" / "interpret-last-reply.txt").write_text(
        f"model {data.get('model')} stop {data.get('stop_reason')}\n\n{text}", encoding="utf-8")

    if data.get("stop_reason") == "max_tokens":
        raise RuntimeError("reply truncated at max_tokens; raise MAX_TOKENS")

    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("no JSON object in the reply; see notes/interpret-last-reply.txt")
    narr = json.loads(m.group(0))

    ok, problems = verify(narr, doc)
    if ok:
        return {"narrative": narr, "verified": True, "problems": [], "rounds": _round + 1}
    if _round + 1 < rounds:
        again = ("Your reply was rejected by the checker for these reasons:\n- "
                 + "\n- ".join(problems)
                 + "\nReturn the corrected JSON object only.")
        return interpret(doc, model=model, rounds=rounds, _retry=again, _round=_round + 1)
    return {"narrative": narr, "verified": False, "problems": problems, "rounds": _round + 1}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--check", help="verify an existing narrative JSON file, no API call")
    ap.add_argument("--out", default="narrative.json")
    a = ap.parse_args()

    doc = json.loads(Path(a.state).read_text(encoding="utf-8"))
    view = doc.get("llm_contract") and doc or None
    if view is None:
        import engine
        view = engine.for_llm(doc)

    if a.check:
        narr = json.loads(Path(a.check).read_text(encoding="utf-8"))
        ok, problems = verify(narr, view)
        print("VERIFIED" if ok else f"{len(problems)} PROBLEM(S)")
        for p in problems:
            print("  -", p)
        raise SystemExit(0 if ok else 1)

    res = interpret(view)
    Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(f"wrote {a.out}   verified={res['verified']}  rounds={res['rounds']}")
    for p in res["problems"]:
        print("  -", p)


if __name__ == "__main__":
    main()
