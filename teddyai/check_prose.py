# -*- coding: utf-8 -*-
"""
check_prose.py -- does the published prose actually follow CARD_RULES.md?

    python check_prose.py                 check the live narrative.json against trinity.json
    python check_prose.py --sheet <path>  also require the lead's branch block to RENDER

Exit 0 when every checkable rule passes, 1 otherwise.

WHY THIS EXISTS (2026-09-17). Mark asked whether CARD_RULES.md was really a description of how
the prose gets written. Asserting it was worthless -- the first draft of the file said cards are
ranked on structural quality with the read as a tiebreak, and two sheets on the same book with
identical stops had swapped their top two cards when the read flipped, which is the opposite.
So the question became: run the rules against the sheet and see. That first run came back 25/29,
and three of the four failures were real -- no Primary on a `look` gate, a TP1 paying 1.15R, and
the open-location branch still sitting in `reading`, which build.py has thrown away since
2026-09-16, so the block I had said belonged in the lead was still not rendering.

Only the MECHANICAL rules are checkable here. Selection, merging, ranking and salience are
judgement and this script cannot see them; a clean run means nothing was dropped, not that the
page is good. Run it after publish_plan and before shipping.
"""
import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent

# The lead's eight beats, section 6. Probe strings are deliberately loose -- they test that the
# beat is PRESENT, not that it is phrased a particular way.
# EVERY probe is matched case-insensitively (see _has). Case alone produced false failures on
# "Tension I am not resolving" against a probe for "tension" -- five times today a rule was
# reported broken when the prose had it. When a beat fails, read the lead before believing it.
BEATS = [
    ("1 what the plan is for", ("plan for the", "overnight plan", "rth plan",
                               "rest of the overnight", "asia plan", "built ")),
    ("2 book / clock / tape", ("book has not changed", "book changed", "the clock", "the tape")),
    ("3 the read", ("the read", "no read", "higher floors", "lower ceilings",
                    "directional confidence", "confidence.directional",
                    "no directional view")),
    ("4 level states", ("wicked", "broken", "first touch", "reclaimed", "second visit",
                        "has traded", "have traded", "untested")),
    ("5 location, both sums", ("chain-wide", "net gamma at spot", "at spot")),
    # NOT a bare "reopen": the lead says that word in other sentences, so the probe passed with
    # the branch block deleted (negative test, 2026-09-17). Require the block's own language.
    ("6 open-location branch", ("Where price opens", "where price opens",
                               "decides which cards are live", "Open-location branch",
                               "open-location branch")),
    ("7 tension left unresolved", ("do not resolve", "not resolving", "tension", "disagree")),
    ("8 gate / primary / releases", ("gate reads", "trade gate", "red folder", "release")),
]
# The counter-argument opens with a BOLDED lead-in a reader can scan for; the wording is free,
# because it should name what actually argues against THIS card. Requiring two stock phrases
# failed the 14:54 sheet on "Why it is third and not first:" and "...which is the whole
# trade-off:", both of which are better than a generic opener (2026-09-17).
COUNTER = ("against it", "wrong with it", "trade-off", "the problem", "why it is",
           "why this is", "argues against", "against this")


def rungs_just_outside(card, gmap, margin=12.0, material=25.0):
    """Material STRIKES the why names that sit just outside the card's own entry zone.

    The engine hands a zone in the candidate list; narrowing it throws fills away. On 2026-09-17
    the afternoon-floor card was written 29,650-29,670 against an offered 29,652-29,675, while its
    own why called 29,675 the first of its three rungs. The session low was 29,674.00 -- four
    points short, on five that had been trimmed off the top.

    Three filters, because the first version flagged six things for every real one: the number has
    to be a REAL STRIKE on the board, it has to carry MATERIAL gamma, and stops and targets are
    excluded. Swing prices (29,639.50), scenario triggers (29,781.47) and trivial rungs (+1) are
    named in prose all the time and belong outside a zone.
    """
    lo, hi = card.get("zone_lo"), card.get("zone_hi")
    if lo is None or hi is None:
        return []
    lo, hi = float(lo), float(hi)
    known = {round(float(card["sl"]), 2), round(float(card["entry"]), 2), round(lo, 2), round(hi, 2)}
    known |= {round(float(t), 2) for t in (card.get("tp") or [])}
    out = []
    text = (card.get("why") or "") + " " + (card.get("tag") or "")
    for m in re.findall(r"\d{2},\d{3}(?:\.\d+)?", text):
        v = float(m.replace(",", ""))
        if round(v, 2) in known:
            continue
        if not ((lo - margin) <= v < lo or hi < v <= (hi + margin)):
            continue
        g = gmap.get(round(v, 1))
        if g is not None and abs(g) >= material:
            out.append(v)
    return sorted(set(out))


def has_counter(why):
    """Is there a bolded lead-in that introduces what argues against the card?

    Bold is the requirement -- it is what makes the block scannable on the page. The wording is
    not, so a card can say "Why it is third and not first:" where that is the honest objection.
    """
    for m in re.findall(r"<b>(.*?)</b>", why, re.S):
        low = m.lower()
        if any(w in low for w in COUNTER):
            return True
    return False


def session_bars():
    """The 1-minute bars of the session in progress, in tape_ledger's shape."""
    import datetime as _dt
    out = []
    for d in sorted(pathlib.Path(ROOT / "data" / "bars").glob("2*.json"))[-3:]:
        try:
            for x in json.loads(d.read_text(encoding="utf-8")):
                out.append({"ts": x["ts"], "o": x["o"], "h": x["h"], "l": x["l"], "c": x["c"]})
        except Exception:
            continue
    out.sort(key=lambda r: r["ts"])
    if not out:
        return out
    last = _dt.datetime.fromisoformat(out[-1]["ts"])
    open_ = last.replace(hour=13, minute=30, second=0, microsecond=0)
    if last < open_:
        open_ = (last - _dt.timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
    cut = open_.isoformat(sep=" ")[:16]
    return [b for b in out if b["ts"][:16] >= cut]


def live_on(prev_sheet):
    """Cards on a previous sheet that are IN PLAY and not invalidated.

    CARD_RULES.md section 3: a live card is carried, not replaced. This is the check for it, and
    it exists because the 23:17 rebuild on 2026-09-17 retired the 29,650-29,675 hold while it was
    live and uninvalidated, replaced it with a 29,625 hold, and the level it dropped filled at
    01:16 and ran 2.58R while the replacement never came within 17 points.
    """
    try:
        import tape_ledger as TL, setup_track as ST
        bars = session_bars()
        if not bars:
            return []
        pl = TL.Plan("ASIA", pathlib.Path(prev_sheet))
        pl.refresh()
        out = []
        for c in (pl.cards or []):
            try:
                if ST.track(c, bars).get("state") == "in play":
                    out.append(c)
            except Exception:
                continue
        return out
    except Exception:
        return []


def _has(text, probes):
    """Case-insensitive substring match. Case alone has cost five false failures in one day."""
    low = (text or "").lower()
    return any(p.lower() in low for p in probes)


def levels_from(T):
    """Every price the board actually carries: strikes, flips, rails."""
    out = set()
    for p in (T.get("panels") or {}).values():
        out |= {float(k) for k in (p.get("gamma") or {})}
        out |= {round(float(h), 1) for h in (p.get("hvl") or [])}
    for k in ("rail_lo", "rail_hi"):
        if T.get(k) is not None:
            out.add(round(float(T[k]), 1))
    return out


def check(prose, T, sheet=None, carried=()):
    levels = levels_from(T)
    gmap = {round(float(k), 1): float(v)
            for k, v in ((T.get("panels") or {}).get("0DTE", {}).get("gamma") or {}).items()}
    near = lambda x: min((abs(x - v) for v in levels), default=9e9)
    cards, lead = prose.get("setups") or [], prose.get("lead") or ""
    r = []
    add = lambda n, ok, d="": r.append((n, bool(ok), d))

    sides = [c.get("side") for c in cards]
    n = len(cards)
    short_set = n < 4
    explains = _has(lead, ("not a card", "not cards", "level, not a card", "named here instead",
                           "pay inside", "only two cards", "only one card", "a level on the page",
                           "map levels", "the reason is reach", "cards, not four"))
    add("S3  two to four cards", 2 <= n <= 4, str(n))
    # Four is the aim, not a quota: a plan that finds fewer cards that pay and names the rest as
    # levels is following the rule, not breaking it -- so a short set only fails if the lead does
    # not say why.
    add("S3  short set is explained", (not short_set) or explains,
        "fewer than four and the lead does not say why" if short_set and not explains else
        (f"{n} cards, explained" if short_set else ""))
    # One-sided is allowed when the BOOK is one-sided -- CARD_RULES.md section 3: "if the
    # structure genuinely offers only one side, write that and say so in the lead". On
    # 2026-09-17 the 18:41 board graded every hold "level, not a card" because 29,650 carried
    # +1 and 29,670 +1, so all twelve writable cards were shorts. Fail only when the lead is
    # silent about it, exactly like a short set.
    one_sided = len(set(sides)) < 2
    # "the book is" was in this list and matched any prose at all -- a negative test slipped
    # straight through it. Every probe here has to be a phrase about SIDEDNESS.
    # "there is no short on this page" says exactly what "no short card" says and was failing
    # (2026-09-17, the 2026-09-18 RTH sheet). Each addition is still a phrase about SIDEDNESS --
    # that is the constraint, not the particular noun.
    says_so = _has(lead, ("no long card", "no short card", "not a long card", "not a short card",
                          "one-sided", "only one side", "every one is a short",
                          "every one is a long", "all of them short", "all of them long",
                          "no short on this page", "no long on this page",
                          "there is no short", "there is no long",
                          "both cards here are long", "both cards here are short"))
    add("S3  both sides, or the lead says why not", (not one_sided) or says_so,
        f"{sides.count('long')}L/{sides.count('short')}S"
        + (", explained" if one_sided and says_so else
           " and the lead does not say why" if one_sided else ""))
    add("S3  every entry is a zone", all(c.get("zone_lo") is not None for c in cards))

    # A card that was in play on the previous sheet must still be on this one, unchanged.
    for c in (carried or []):
        got = [d for d in cards
               if abs(float(d["entry"]) - float(c["entry"])) <= 1
               and abs(float(d["sl"]) - float(c["stop"])) <= 1]
        add(f"S3  carried forward    {str(c.get('id', c.get('name')))[:24]}", bool(got),
            "" if got else f"was in play at E {float(c['entry']):,.0f} / SL {float(c['stop']):,.0f} and is not on this sheet")


    prim = [c for c in cards if c.get("rank") == "Primary"]
    add("S4  exactly one Primary", len(prim) == 1,
        prim[0]["name"] if len(prim) == 1 else f"{len(prim)} found")
    # build.py gives card one the .pri border unconditionally, so a Primary anywhere else means
    # the page marks one card and labels another.
    add("S4  Primary is card one", bool(prim) and cards and cards[0] is prim[0])

    for c in cards:
        nm = (c.get("name") or "?")[:24]
        tps = c.get("tp") or []
        worst = max((near(t) for t in tps), default=9e9)
        add(f"S5  3 real targets  {nm}", len(tps) == 3 and worst <= 2, f"worst gap {worst:.1f}pts")
        risk = abs(float(c["entry"]) - float(c["sl"]))
        r1 = abs(float(tps[0]) - float(c["entry"])) / risk if (tps and risk) else 0
        add(f"S5  TP1 >= 1.2R      {nm}", r1 >= 1.2, f"{r1:.2f}R")
        add(f"S5  VOID named       {nm}", "VOID IF" in (c.get("tag") or ""))
        add(f"S5  counter-argument {nm}", has_counter(c.get("why") or ""))
        # A rung the why names but the entry zone excludes by a hair is a fill thrown away.
        near_out = rungs_just_outside(c, gmap)
        add(f"S5  zone spans its rungs {nm}", not near_out,
            f"names {", ".join(f"{v:,.0f} ({gmap.get(round(v,1)):+.0f})" for v in near_out)} just outside the zone" if near_out else "")
        # The prose must name the target the card actually carries. Retarget a card and it is
        # easy to update tp=[...] and leave the sentence quoting the old number (2026-09-17,
        # card D kept saying 29,450 after moving to 29,430).
        prose_txt = (c.get("why") or "") + " " + (c.get("tag") or "")
        t1 = float(tps[0]) if tps else 0
        add(f"S5  prose names TP1   {nm}",
            any(f"{t1:,.{d}f}" in prose_txt for d in (0, 1, 2)), f"{t1:,.2f}")

    # The lead and the cards must agree about the Primary. Changing a rank without changing the
    # sentence that describes it leaves the page contradicting itself (2026-09-17).
    denies = any(k in lead for k in ("no card is Primary", "none is Primary", "no Primary"))
    add("S4/S6  lead agrees about Primary", not (prim and denies),
        "lead denies a Primary that exists" if (prim and denies) else "")
    if prim:
        nm = prim[0].get("name") or ""
        head = nm.split(chr(8211))[0].split("-")[0].strip()
        add("S4/S6  lead names the Primary", head and head in lead, head)

    for name, probes in BEATS:
        add(f"S6  lead beat {name}", _has(lead, probes))
    if sheet is not None:
        # The branch block lived in `reading` for two sheets, and build.py throws `reading` away.
        add("S6  branch RENDERS in the sheet",
            _has(sheet, ("where price opens", "open-location branch",
                         "decides which cards are live")))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--narrative", default=str(ROOT / "narrative.json"))
    ap.add_argument("--trinity", default=str(ROOT / "trinity.json"))
    ap.add_argument("--sheet", default=None, help="also require the branch block to render")
    ap.add_argument("--carry", default=None,
                    help="previous sheet: any card still in play on it must be carried")
    a = ap.parse_args()

    prose = json.loads(pathlib.Path(a.narrative).read_text(encoding="utf-8"))["prose"]
    T = json.loads(pathlib.Path(a.trinity).read_text(encoding="utf-8"))
    sheet = pathlib.Path(a.sheet).read_text(encoding="utf-8") if a.sheet else None

    carried = live_on(a.carry) if a.carry else []
    rows = check(prose, T, sheet, carried)
    w = max(len(n) for n, _, _ in rows)
    bad = 0
    for n, ok, d in rows:
        if not ok:
            bad += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {n:<{w}}  {d}")
    print(f"\n  {len(rows) - bad}/{len(rows)} pass"
          + ("" if not bad else f"  --  {bad} rule(s) in CARD_RULES.md not followed"))
    print("  mechanical rules only: selection, merging, ranking and salience are not checkable here")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
