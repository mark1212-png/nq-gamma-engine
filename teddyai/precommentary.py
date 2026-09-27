#!/usr/bin/env python3
"""
precommentary.py -- does the plan make sense STRUCTURALLY, before a word is written?

    python precommentary.py                        exit 0 = write the commentary
    python precommentary.py --cards trinity.html   also grade the cards on that sheet
    python precommentary.py --open 29457           the session's opening print, if known

Mark, 2026-09-15: "before generating a commentary make sure everything makes sense
structurally. Meaning read the bars, read the current price, read the settle, read what
played out. I dont want any ABSURD or outlandish cards being outputted moving forward."
And, on what makes a card absurd: "If price traded through something ask yourself does it
make sense????? If we opened up nowhere near a setup does it make it valid still???"

verify_pull.py asks whether the BOOK is trustworthy -- chain size, reference against the
settle bar, open-interest delta. Nothing has ever asked whether the PLAN built on that book
is coherent, and on 2026-09-15 that gap shipped a sheet whose clock said 14.47 hours and
whose band said 36 points: session_scale.scale_overnight runs only to the 07:00 UTC Asia
close, so a 05:30 build drew the last ninety minutes of Asia around a plan covering the next
fourteen hours. Reach and the structural stop are both fractions of the drawn band, so every
card inherited it -- the whole plan was two strikes within 13 points of price on 5-point
stops, and nothing in the pipeline objected.

EVERY BOUND IS MEASURED OFF THE TAPE IN FRONT OF US, never from a constant, so a quiet book
and a violent one get different verdicts from the same rule. A stop narrower than the
session's median bar cannot be resolved on the timeframe anyone reads it on, so it is not a
stop. A target further than the session has travelled all night is not a target. A wall the
tape has already broken is not a wall.

Each check is a thing that has actually gone wrong. Each fails LOUDLY, because a plan that is
quietly wrong is worse than no plan.
"""
import sys, re, json, argparse, pathlib, statistics, datetime

ROOT = pathlib.Path(__file__).parent.resolve()
DATA = ROOT / "data"
SHRINK = 0.628                # session_scale.SHRINK; the fitted rail coefficient
SESSION_HOURS = 23.0          # a CME session
rows = []
HARD, SOFT, OK = "FAIL", "warn", "ok  "


def say(level, check, msg):
    rows.append((level, check, msg))


def _bars(day):
    f = DATA / "bars" / f"{day}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def _hhmm(b):
    return b["ts"][11:16]


# --------------------------------------------------------------- the checks ---

def check_band_vs_clock(T):
    """
    The band has to describe the window the clock claims.

    rail_scale is SHRINK x sqrt(share of the session's variance still to come), so
    (scale/SHRINK)^2 IS that share -- and `hours` says how much of the session is left. Put
    both on the same footing and they have to agree. On the 09-15 05:30 build the band said
    2% of the variance remained while the clock said 14.47 of 23 hours; the rails were
    describing a session ending at 07:00 UTC and the plan was not.
    """
    scale, hours = T.get("rail_scale"), T.get("hours")
    if not scale or not hours:
        return say(SOFT, "band/clock", "no rail_scale or hours on this build; not checked")
    scale, hours = float(scale), float(hours)
    label = str(T.get("rail_label") or "1D")
    # THE CHECK KNOWS WHICH CURVE DREW THE BAND (2026-09-15, second version). The first
    # assumed every band came from the intraday rail (SHRINK x sqrt of variance left) and
    # so refused every full-day sheet (rails 1.000x by design) and every Asia sheet (a
    # different fit, to the 07:00 UTC close) when replayed over the archive. Each plan
    # kind has its own curve and its own notion of "the window this band describes"; the
    # failure being caught is a band whose window ends long before the plan's clock does.
    import session_scale as SS
    if "Globex" in label:
        return say(SOFT, "band/clock", "holiday plan; rails not checked")
    if label.startswith(("Asia", "Rest of overnight")):
        since = max(0.0, 24.0 - hours)                 # settle 20:00 UTC -> expiry 20:00 next day
        window = SS.ON_SPAN - since                    # what is left of the curve's own window
        frac = window / hours if hours else 0.0
        ok = window >= 2.0 and frac >= 0.25
        say(OK if ok else HARD, "band/clock",
            f"rails {scale:.3f}x describe the overnight to the 07:00 UTC Asia close: {window:.1f}h of "
            f"that window left, {frac:.0%} of the {hours:.2f}h the clock claims"
            + ("" if ok else "  -- THE BAND ENDS LONG BEFORE THE PLAN DOES"))
    elif label.startswith("London"):
        since = max(0.0, 24.0 - hours)
        hour_utc = (20.0 + since) % 24
        window = SS.LONDON_END_UTC - max(hour_utc, SS.LONDON_START_UTC)
        frac = window / hours if hours else 0.0
        ok = window >= 2.0 and frac >= 0.25
        say(OK if ok else HARD, "band/clock",
            f"rails {scale:.3f}x describe the European window to the 13:30 UTC NY open: {window:.1f}h "
            f"of it left, {frac:.0%} of the {hours:.2f}h the clock claims"
            + ("" if ok else "  -- THE BAND ENDS LONG BEFORE THE PLAN DOES"))
    elif label.startswith("Rest of session"):
        exp = SS.scale_for_hours(hours)
        ok = exp is not None and hours >= 0.5 and abs(scale - exp) <= 0.25 * max(exp, SS.FLOOR)
        say(OK if ok else HARD, "band/clock",
            f"rails {scale:.3f}x against the measured intraday rail {exp if exp is not None else float('nan'):.3f}x "
            f"for {hours:.2f}h left"
            + ("" if ok else ("  -- UNDER HALF AN HOUR LEFT: the rail has stopped saying anything" if hours < 0.5
                              else "  -- THE BAND DOES NOT MATCH THE CLOCK")))
    else:
        ok = abs(scale - 1.0) <= 0.05
        say(OK if ok else HARD, "band/clock",
            f"a full-session plan draws the full-day rail; rails {scale:.3f}x"
            + ("" if ok else "  -- NOT THE FULL-DAY RAIL ON A FULL-DAY PLAN"))


def check_reference(T, bars):
    """The reference has to be where price is; the rails are drawn around it."""
    ref = T.get("last_print") or T.get("spot")
    if not ref:
        return say(HARD, "reference", "no reference on this build")
    ref = float(ref)
    if not bars:
        return say(SOFT, "reference", f"reference {ref:,.2f}; no bars to check it against")
    last = bars[-1]
    gap = ref - last["c"]
    inside = last["l"] <= ref <= last["h"]
    # A full-day plan (label "1D": premarket and preliminary sheets) anchors on the previous
    # SETTLE by design -- the rails are drawn around the settle and the levels are read from
    # it -- so its reference is not supposed to be the live print, and a gap to the pre-open
    # tape is information for the lead, not a defect. Replayed over the archive the first
    # version refused every premarket sheet for exactly that gap. A plan that claims a live
    # anchor (Asia, London, intraday) is held to it.
    settle_anchored = (str(T.get("rail_label") or "1D") in ("1D", "")) and not T.get("last_print")
    if settle_anchored and abs(gap) > 25:
        return say(SOFT, "reference",
                   f"settle-anchored plan: reference {ref:,.2f} sits {gap:+,.2f} from the newest bar "
                   f"({_hhmm(last)} UTC close {last['c']:,.2f}) -- say so in the lead; distances on the page are from the settle")
    say(OK if abs(gap) <= 25 else HARD, "reference",
        f"reference {ref:,.2f} vs newest bar {_hhmm(last)} UTC close {last['c']:,.2f} ({gap:+,.2f})"
        + ("; inside the bar" if inside else "")
        + ("" if abs(gap) <= 25 else "  -- THE TAPE HAS LEFT THE REFERENCE BEHIND"))


def check_settle(T):
    settle, sym = T.get("ref_settle") or T.get("spot"), T.get("ref_symbol") or "?"
    ref = T.get("last_print") or T.get("spot")
    if not settle:
        return say(HARD, "settle", "no settle on this build")
    gap = (float(ref) - float(settle)) if ref else 0.0
    say(OK, "settle", f"{sym} settle {float(settle):,.2f} ({T.get('settle') or '?'}); "
                      f"the reference sits {gap:+,.2f} from it")


def check_tape(bars):
    """What actually played out -- the lead has to say it and the cards have to respect it."""
    if not bars:
        say(SOFT, "tape", "no bars for this session yet")
        return None
    o, c = bars[0], bars[-1]
    hi, lo = max(bars, key=lambda b: b["h"]), min(bars, key=lambda b: b["l"])
    t = dict(open=o["o"], hi=hi["h"], lo=lo["l"], last=c["c"], rng=hi["h"] - lo["l"],
             med_bar=statistics.median(b["h"] - b["l"] for b in bars))
    say(OK, "tape",
        f"{len(bars)} bars {_hhmm(o)}-{_hhmm(c)} UTC   O {t['open']:,.2f}  H {t['hi']:,.2f} ({_hhmm(hi)})  "
        f"L {t['lo']:,.2f} ({_hhmm(lo)})  last {t['last']:,.2f}   range {t['rng']:,.0f} pts, "
        f"median bar {t['med_bar']:,.1f}")
    return t


def check_levels_already_traded(T, tape):
    """A strike the session has been through is written for its NEXT visit, or withheld."""
    if not tape:
        return
    G = {float(k): abs(float(v)) for k, v in
         (((T.get("panels") or {}).get("NEAR") or {}).get("gamma") or {}).items()}
    ref = float(T.get("last_print") or T.get("spot") or 0)
    band = float(T.get("em") or 0) * float(T.get("rail_scale") or 1)
    near = {k: v for k, v in G.items() if band and abs(k - ref) <= band and v >= 3}
    done = sorted(k for k in near if tape["lo"] <= k <= tape["hi"])
    say(OK, "played out",
        (f"{len(done)} material strike(s) in the band have ALREADY traded: "
         + ", ".join(f"{k:,.0f}" for k in done)
         + " -- write them for the next visit, not the first")
        if done else "no material strike in the band has traded yet this session")


def eth_segment(bars, sheet):
    """
    The bars BEFORE the window this plan covers -- the overnight a premarket sheet was
    written after, and which its cards have to account for.

    The split comes from the sheet's own rail label (setup_track.parse_window), so an RTH
    sheet splits at 13:30 UTC and an intraday rebuild at its own build clock. An overnight
    sheet has no ETH behind it inside its own session and returns nothing.
    """
    sys.path.insert(0, str(ROOT))
    import setup_track as ST
    try:
        kind, start, _ = ST.parse_window(pathlib.Path(sheet).read_text(encoding="utf-8"))
    except Exception:
        return "?", []
    if kind == "asia":
        return kind, []
    if kind == "intraday" and start != (13, 30):
        start = ST._pt_to_utc((bars[0]["ts"][:10] if bars else "2026-01-01"), *start)
        start = max(start, (13, 30))
    return kind, [b for b in bars
                  if (int(b["ts"][11:13]), int(b["ts"][14:16])) < start]


def check_eth_already_triggered(sheet, T, bars):
    """
    Which cards had their entry printed before the plan's window even opened.

    IT ONLY DISQUALIFIES A CARD PRICE HAS LEFT (2026-09-15). The first version fired on
    any entry the overnight had touched, and on a London plan -- where the whole Asia
    session is the ETH behind the window -- that flagged the 29,400 zone while price was
    sitting eighteen points from it. The level having traded overnight is exactly why that
    card is written as a second visit; it is not a reason to withhold it.

    What actually kills a card is the combination: the overnight used it AND price is no
    longer near it. A premarket RTH sheet written at 22:00 PT naming a level Globex ran
    through and left two hundred points behind is a dead card. The same level with price
    still on it is a live one, and the tape ledger the card contract asks for -- say what
    the first visit did -- is prose, not a veto. So: out of reach now, hard failure; still
    in reach, a warning that the prose has to carry.
    """
    sys.path.insert(0, str(ROOT))
    import setup_track as ST
    kind, eth = eth_segment(bars, sheet)
    if not eth:
        return say(OK, "ETH", f"a {kind} plan: no overnight sits behind this window")
    lo = min(b["l"] for b in eth)
    hi = max(b["h"] for b in eth)
    say(OK, "ETH", f"{len(eth)} bars of ETH before this window: {lo:,.2f} to {hi:,.2f}")
    ref = float(T.get("last_print") or T.get("spot") or 0)
    band = float(T.get("em") or 0) * float(T.get("rail_scale") or 1)
    reach = REACH_FRAC * band
    try:
        cards = ST.parse_setups(pathlib.Path(sheet).read_text(encoding="utf-8"))
    except Exception:
        return
    for c in cards:
        e = c["entry"]
        if not (lo <= e <= hi):
            continue
        r = ST.track(c, eth)                    # what ETH did with it, on the fixed tracker
        done = r["state"] in ("stopped",) or r["state"].startswith("tp")
        live = bool(ref and band and abs(e - ref) <= reach)
        what = (f" and the trade FINISHED there ({r['state']}, {r['detail'][:46]})"
                if done else f" ({r['state']})")
        if live:
            say(SOFT, "ETH card",
                f"{c['id'][:24]:<24} entry {e:,.0f} printed overnight{what}"
                f" -- price is still {abs(e - ref):,.0f} pts away, so this is a SECOND VISIT:"
                " the prose must say what the first one did")
        else:
            say(HARD, "ETH card",
                f"{c['id'][:24]:<24} entry {e:,.0f} ALREADY PRINTED overnight{what}"
                f" and price has left it ({abs(e - ref):,.0f} pts, {abs(e - ref) / band:.2f} bands)"
                " -- write it for its next visit or withhold it")


def check_cards(sheet, T, tape, sess_open=None, since=None, bars=None):
    """
    The absurdity floor, and Mark's two questions.

      side          a long's stop sits below its entry and its targets above. The inverse has
                    shipped on a live page and renders the card unreadable, not merely wrong.
      stop          wider than the session's MEDIAN bar, or it cannot be resolved at all.
      reach         interpret.reach_state: live inside the live band, `watch` inside the day's
                    map (flagged, still ships), `off-map` beyond it and refused.
      target        TP1 no further than the session's own range so far.
      R             TP1 at 1.0R or better grades a card; below that it grades a LEVEL and the
                    reading names it. A grade, not a veto.
      TRADED THROUGH  "If price traded through something ask yourself does it make sense?"
                    A hold whose wall the tape has already broken BELOW is void -- the wall
                    failed, and the card describes a level that no longer exists in the
                    state it assumed. Same for a rejection price has already accepted above.
      OPENED AWAY   "If we opened up nowhere near a setup does it make it valid still?"
                    A card price has never approached, in a session that opened more than a
                    band from it, was never live. It is a level for a different day.
      ETH USED      "If ETH already triggered it does it make sense???"
                    The overnight is not a rehearsal. A premarket RTH sheet is written hours
                    after Globex has already run through its levels, so a card whose entry
                    PRINTED overnight is not a setup waiting to happen -- its first touch is
                    spent, and if the stop or TP1 printed too the whole trade is over before
                    the plan naming it was published. The card contract's own rule: a level
                    already triggered is written for its NEXT visit or withheld. This splits
                    the tape at the plan's own window start and says which side each entry
                    printed on, so the distinction cannot be skipped by accident.
    """
    sys.path.insert(0, str(ROOT))
    import setup_track as ST
    doc = pathlib.Path(sheet).read_text(encoding="utf-8")
    try:
        cards = ST.parse_setups(doc)
    except Exception as e:
        return say(SOFT, "cards", f"could not read cards off {pathlib.Path(sheet).name}: {e}")
    ref = float(T.get("last_print") or T.get("spot") or 0)
    band = float(T.get("em") or 0) * float(T.get("rail_scale") or 1)
    op = sess_open if sess_open is not None else (tape or {}).get("open")
    # THE SAME INPUTS THE GENERATOR USES (2026-09-16). candidates() cuts the session at
    # the 13:30 open and takes the median bar as the min_dist that keeps a stop off the
    # pivot the entry is standing on. The checker derives them the same way so that the
    # stop it demands is the stop candidates() proposed, not a second opinion.
    sess_bars, floor = [], 0.0
    if bars:
        sess_bars = [x for x in bars if x["ts"] >= (bars[-1]["ts"][:10] + " 13:30")] or bars
        floor = statistics.median(b["h"] - b["l"] for b in bars)
    for c in cards:
        e, s, tps, side, lvl = c["entry"], c["stop"], c["tps"], c["side"], _level_of(c["id"], c["entry"])
        bad, soft = [], []
        if side == "long" and not (s < e < (tps[0] if tps else e + 1)):
            bad.append("LONG whose stop is not below its entry with targets above")
        if side == "short" and not (s > e > (tps[0] if tps else e - 1)):
            bad.append("SHORT whose stop is not above its entry with targets below")
        # A STOP INSIDE ONE BAR IS A NOTE, NOT A VETO (2026-09-16, f26c027). It says the
        # card is a scalp at this fill, which is a thing to know and not a thing to refuse.
        if tape and c["risk"] < tape["med_bar"]:
            soft.append(f"stop {c['risk']:,.0f} pts is inside one bar's noise "
                        f"(median bar {tape['med_bar']:,.1f}) -- a scalp at this fill")
        # THE STOP IS THE TAPE'S OWN STRUCTURE, NOT THE MAP (2026-09-16, 1df972d/720b6a6).
        # swings.invalidation() -- the MOST RECENT swing beyond the entry, latest in time --
        # is what candidates() writes and what Mark's own 29,250 invalidation was. The gamma
        # map is the fallback for a level the session never turned on, exactly as in the
        # generator. Until 2026-09-16 this tested against the map alone and so rejected every
        # swing stop candidates() had just proposed, in the same file: the 1028 build's
        # 29,425-29,450 hold was failed for a 29,417 stop the generator wrote one point under
        # the 14:13Z swing. MAX_STRUCT_STOP is gone; nothing here caps the width.
        try:
            sys.path.insert(0, str(ROOT))
            import command_center as CC
            import swings as SW
            want = origin = None
            if sess_bars:
                inv_p, _ = SW.invalidation(sess_bars, side, e, min_dist=floor)
                if inv_p is not None:
                    want = inv_p - 1 if side == "long" else inv_p + 1
                    origin = f"one point beyond the most recent swing at {inv_p:,.2f}"
            if want is None:
                # the generator's bars too, not []: _material() admits the levels the tape
                # turned on and _closes_through() demotes the ones it cut, so without them
                # the fallback anchors on different walls from candidates() (2026-09-23:
                # 30,864 proposed, 30,885.72 demanded, on the same card in the same run).
                cc_bars = [{"t": int(datetime.datetime.fromisoformat(b["ts"]).timestamp() * 1000), "c": b["c"]}
                           for b in bars] if bars else []
                st = CC._struct_stop({"entry": e, "side": side}, T, cc_bars)
                if st.get("stop") is not None and st.get("ok"):
                    want = float(st["stop"])
                    origin = (f"{st['buffer']:g} beyond {st['anchor']:,.0f}"
                              + (" (the wall behind)" if st.get("behind") else " (the card's own wall)")
                              + " -- the session has no swing beyond this entry")
            if want is not None and abs(float(s) - float(want)) > 5:
                bad.append(f"stop {s:,.2f} is not structural: the structure puts it at "
                           f"{want:,.2f}, {origin}")
        except Exception as ex:
            say(SOFT, "card", f"{c['id'][:24]:<24} structural stop not checked: {type(ex).__name__}: {ex}")
        # REACH IS A FLAG, NOT A VETO (2026-09-16, 0e17afd). This tested the LIVE band and
        # hard-failed anything beyond it, which is the contract interpret.reach_state()
        # replaced in the same file: the roster comes off the DAY band and only `off-map` is
        # refused. Left as it was, it deleted every card candidates() had just graded CARD at
        # 1.3 bands out -- on a short overnight, whose live band is a third of the day's, that
        # is the whole far side of the sheet and the one-sided plan it produces.
        try:
            import interpret as _IN
            rs = _IN.reach_state({"entry": e}, T)
            if rs["state"] == "off-map":
                bad.append(f"entry sits {rs['day_ems']:.2f} day-bands from the reference, "
                           "off the day's map entirely")
            elif rs["state"] == "watch":
                soft.append(f"entry sits {rs['live_ems']:.2f} live-bands out "
                            f"({rs['dist']:,.0f} pts): a WATCH card, not live from here")
        except Exception as ex:
            say(SOFT, "card", f"{c['id'][:24]:<24} reach not checked: {type(ex).__name__}: {ex}")
        # bounded by the drawn band, as in structure_problems: early in a quiet session the
        # range is a few bars wide and every honest target is beyond it (2026-09-15)
        if tps and tape and tape["rng"] and abs(tps[0] - e) > tape["rng"] and not inside_band(T, tps[0], e, band):
            if not inside_day_map(T, tps[0]):
                bad.append(f"TP1 {tps[0]:,.0f} is {abs(tps[0] - e):,.0f} pts out, beyond the session's whole "
                           f"range so far ({tape['rng']:,.0f}) and off the day's map entirely")
            else:
                soft.append(f"TP1 {tps[0]:,.0f} is {abs(tps[0] - e):,.0f} pts out, beyond the session's "
                            f"range so far ({tape['rng']:,.0f}) and outside the drawn band -- "
                            "reachable on the day's map, so a flag: say the distance on the card")
        # R IS A GRADE, NOT A VETO (2026-09-16, 0e17afd): the 1.2R floor "arrived with the
        # 09-09 import, was never measured, and constrained TARGETS to fix a STOP problem".
        # A side whose T1 pays at least its own risk grades `card`; below that it is a
        # `level` and the reading names it. Grade it and print the number -- do not delete it.
        if tps and c["risk"] and abs(tps[0] - e) / c["risk"] < 1.0:
            soft.append(f"TP1 is {abs(tps[0] - e) / c['risk']:.2f}R, under 1.0R: this side "
                        "grades a LEVEL, not a card -- name it in the reading instead")
        # -- Mark's questions, applied to the shape they actually belong to ------
        #
        # "Price traded through it" invalidates a card whose THESIS IS THAT THE LEVEL
        # HOLDS -- a hold (long at a wall below price) or a rejection (short at a wall
        # above it). For the other two shapes the thesis is that the level FAILS, so a
        # prior break is the setup rather than its refutation: a break short entering
        # under a wall, or an acceptance long entering over one, is a continuation card
        # and reads the same failure as confirmation. The first version of this check
        # did not make that distinction and flagged a 29,300 break for the crime of
        # sitting below a level the session had already traded through, which is the
        # only condition under which that card exists at all.
        #
        # The shape is geometry, not the name: a long whose LEVEL is below the reference
        # is a hold, above it an acceptance; a short above is a rejection, below a break.
        # It has to be the level and not the entry, because the entry deliberately sits a
        # few points past the wall -- a hold at 29,350 with the reference on 29,350 enters
        # at 29,351, and testing the entry would read that card as an acceptance.
        #
        # LEVEL-STATE POLICY (2026-09-15), on swings since the book -- the same
        # measured rule candidates() applies. Continuation through a wall the tape closed
        # beyond and stayed beyond: hard failure (17% win, -0.42R, robust). A fade at a
        # broken or reclaimed wall: a warning that the prose must carry as a second visit,
        # because the record measures it as a coin flip, not a loser. A wick is not a
        # failure. The earlier hard rule for holds was common sense the data did not back.
        if tape:
            reverting = ((side == "long" and lvl <= ref) or (side == "short" and lvl >= ref))
            state = level_state(since or [], lvl, c["risk"], wall_below=(lvl <= ref))[0] if since else None
            if state == "broken" and not reverting:
                bad.append(f"the tape closed beyond {lvl:,.0f} and stayed there -- a continuation "
                           "through it chases a move already made")
            elif state in ("broken", "reclaimed") and reverting:
                soft.append(f"{lvl:,.0f} is a SECOND VISIT ({state} since the book): the prose "
                            "must say what the first visit did")
            # "We opened nowhere near it" is about whether the card was ever LIVE, which
            # applies to every shape -- but a continuation card is conditional by design,
            # so it is only absurd when price has neither opened near it nor approached it.
            if op is not None and band:
                opened_away = abs(op - e) / band
                approached = (tape["lo"] - c["risk"]) <= e <= (tape["hi"] + c["risk"])
                if opened_away > 1.0 and not approached and reverting:
                    bad.append(f"the session opened {opened_away:.2f} bands from this entry "
                               f"({op:,.0f} vs {e:,.0f}) and has never come near it")
        say(HARD if bad else OK, "card",
            f"{c['id'][:24]:<24} {side:<5} E {e:>9,.0f} SL {s:>9,.0f}  "
            + (("ABSURD -- " + "; ".join(bad)) if bad else "ok"))
        for w in soft:
            say(SOFT, "card", f"{c['id'][:24]:<24} {w}")


# ------------------------------------------------------- interpret structure ---

CLUSTER_ZONE = 40     # entry zones within this span, same side and shape, are one trade
REACH_FRAC = 0.5      # interpret.REACH_EMS -- half the drawn band; measured, see that module


def inside_band(T, px, entry=None, band=None):
    """Is `px` inside the drawn band? The rails when the build has them (the band is drawn around
    the reference, so a target 123 points from an entry 26 points below the reference can still
    sit on the upper rail); otherwise within `band` points of `entry`."""
    lo, hi = T.get("rail_lo"), T.get("rail_hi")
    if lo is not None and hi is not None:
        return float(lo) - 0.01 <= float(px) <= float(hi) + 0.01
    return entry is None or band is None or abs(float(px) - float(entry)) <= band


def inside_day_map(T, px):
    """Is `px` inside the DAY's map -- the reference plus or minus one FULL expected move?

    Same definition candidates() prints as "the day's map" and the build skill states as the
    roster window ("the DAY band, em_full, unscaled, does not shrink with the clock").

    2026-09-21. The TP1 sanity veto below used `inside_band`, the DRAWN band, which on an
    overnight build is the Asia window's rails: 0.283x and 72.6 points wide on a plan whose
    own horizon is 18.26 hours. Measured on that build, 83% of the 0DTE gross gamma sat
    OUTSIDE that band, and the only card that survived the veto was the one whose TP1 was
    the lower rail -- i.e. the band's own edge, which is inside it by construction. The page
    was selected by the test rather than by the structure.

    This is the same correction `reach_state` already carries a few hundred lines up ("the
    roster comes off the DAY band and only `off-map` is refused"); the TP1 test was simply
    left behind. The band still tightens the test -- a target inside it is never questioned
    -- but only the day's map refuses.

    NOT a licence to widen further. The engine's own candidate targets are already chosen
    inside the map, so for those rows this check is近 inert by design; what it still catches
    is a HAND-WRITTEN target beyond the reach the plan itself claims.
    """
    ref = T.get("spot")
    em = T.get("em_full") or T.get("em")
    if ref is None or not em:
        return True
    return float(ref) - float(em) - 0.01 <= float(px) <= float(ref) + float(em) + 0.01


VISIT_GAP = 15        # bars apart for two spans of a level to count as separate visits
_LEVEL_IN_NAME = re.compile(r"^\s*([\d]{1,3}(?:,\d{3})+|\d{4,6})(?:\.\d+)?")


def _level_of(name, entry):
    """
    The wall a card leans on. Cards are named for their level ("29,400 failure"), so the
    name is the authority; the entry rounded to the 25-point rung is only the fallback.
    Rounding the entry misidentified the mirror card of a one-zone pair on 2026-09-15: the
    failure card enters where the acceptance stops, 13 points under the wall, and 29,387
    rounds to 29,375 -- a strike that is not even material, and whose state was "broken"
    while the real wall's was "reclaimed". That flagged a card the policy should pass.
    """
    m = _LEVEL_IN_NAME.match(str(name or ""))
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return round(float(entry) / 25.0) * 25.0


def level_state(bars, level, tol, wall_below):
    """
    What the tape has already done to a level SINCE THE BOOK WAS STRUCK.

    Mark, 2026-09-15: "since we're building asia game plans based off of NY books. If NY had
    already revisited it and its not new we should take that into consideration." A level's
    history is a fact about the tape, not a judgement, and the tape is on disk -- so this is
    a lookup. `bars` is everything since the book's settle: for an Asia or London plan that is
    NY's own session plus the overnight so far (see _since_book), not just tonight.

      first touch   no bar has spanned the level
      tested        spanned; never traded beyond it by more than `tol`, even intrabar
      wicked        traded beyond intrabar by more than `tol`, but the tape never SWUNG beyond
      broken        at least one swing beyond (by more than `tol`), and price is still beyond
      reclaimed     a swing beyond, then price back on the
                    near side

    A WICK IS NOT A FAILURE (Mark, 2026-09-15: "maybe it failed but it failed without volume
    or just wicked"). The first version called a level broken on a single 1-minute wick and
    judged reclaim by one bar. Re-measured on swings -- the basis the project now uses to
    demote a strike to a pivot (DEMOTE_CROSSES) -- over 355 Asia cards on
    NY's book: a level NY closed beyond and stayed beyond kills a continuation card through
    it (17% win, -0.42R, robust), while a level NY merely wicked through behaves like one
    that held (+0.70R reversion, +0.76R continuation). When NY does break a wall on this
    definition it is not marginal: median three hours beyond, 221 points deep.

    "Beyond" depends on which side of price the wall sits: below the reference, beyond means
    under it; above, over it. `tol` is the card's structural buffer. -> (state, visits,
    first HH:MM, last HH:MM); `visits` counts separate spans, VISIT_GAP bars apart.
    """
    if not bars:
        return "first touch", 0, None, None
    spans = [i for i, b in enumerate(bars) if b["l"] <= level <= b["h"]]
    visits, last = 0, -10 ** 9
    for i in spans:
        if i - last > VISIT_GAP:
            visits += 1
        last = i
    lo, hi = min(b["l"] for b in bars), max(b["h"] for b in bars)
    intrabar = (level - lo) > tol if wall_below else (hi - level) > tol
    # SWINGS, NOT 30-MINUTE CLOSES (2026-09-16). Mark: "lets remove 30 minute close logic, a
    # lot can happen in 30 minutes including a massive stop run" -- and a run through a level
    # that gets taken straight back is precisely what prints a close beyond it. A swing is a
    # pivot the tape turned on: a spike that reverses leaves none, a real acceptance leaves
    # one on the far side. So "did the tape accept it" is asked of the pivots, not the clock.
    import swings as _sw
    piv = sorted([(i, p) for _, p, i in _sw.lows(bars)] + [(i, p) for _, p, i in _sw.highs(bars)])
    pts = [p for _, p in piv] or [bars[-1]["c"]]
    beyond = [((level - p) > tol) if wall_below else ((p - level) > tol) for p in pts]
    last = bars[-1]["c"]
    last_near = (last > level) if wall_below else (last < level)
    if not spans and not intrabar:
        state = "first touch"
    elif not intrabar:
        state = "tested"
    elif not any(beyond):
        state = "wicked"
    elif beyond[-1] or not last_near:
        state = "broken"
    else:
        state = "reclaimed"
    return (state, visits,
            bars[spans[0]]["ts"][11:16] if spans else None,
            bars[spans[-1]]["ts"][11:16] if spans else None)


def _since_book(T, bars):
    """
    The tape from the book's settle forward -- the bars the structure stage should read.

    A premarket RTH plan is written on last night's settle, so "since the book" is the
    overnight, which `bars` already is. An Asia or London plan is written on the book NY
    just closed on, so NY's own session is since-the-book too: a strike NY broke at 14:40
    is not new to Asia, and the level state has to say so. Prepends the book day's RTH for
    those plans; returns `bars` unchanged for the rest.
    """
    label = str(T.get("rail_label") or "")
    if not label.startswith(("Asia", "Rest of overnight", "London")):
        return bars
    book_day = T.get("settle")
    if not book_day:
        return bars
    # EVERY DAY FROM THE BOOK TO THE PLAN, NOT JUST THE BOOK DAY (2026-09-16). The original
    # prepended the book day's RTH and assumed that WAS the session NY had just closed --
    # true only when the book is yesterday's. Tonight it is not: the 09-16 chain is not
    # fetchable until preliminary OI lands (~19:00 PT), so the Asia plan for 09-17 is built
    # on the 09-15 book, and `bars` for report_date 09-17 is empty. The structure stage then
    # read 09-15's session as though it were today's and missed a 497-point day: it called
    # 29,128 "first touch" and "chasing" when the tape had gone to 29,053 through every rung
    # below, and put the whole ranked plan above the reference. Walk the days instead.
    days = sorted({b["ts"][:10] for b in bars} | {book_day})
    stop = T.get("report_date") or days[-1]
    d = datetime.date.fromisoformat(book_day)
    end = datetime.date.fromisoformat(stop)
    span = []
    while d <= end:
        span.append(d.isoformat())
        d += datetime.timedelta(days=1)
    seen, out = set(), []
    for day in span:
        for b in _bars(day):
            if day == book_day and not ((13, 30) <= (int(b["ts"][11:13]), int(b["ts"][14:16])) <= (20, 0)):
                continue          # the book day contributes the session that made the settle
            if b["ts"] not in seen:
                seen.add(b["ts"])
                out.append(b)
    for b in bars:
        if b["ts"] not in seen:
            seen.add(b["ts"])
            out.append(b)
    out.sort(key=lambda b: b["ts"])
    return out or bars


def candidates(T, bars, reach_frac=REACH_FRAC, show=1.2):
    """
    Every material strike around price, and what disqualifies each one as a card.

    THIS IS THE STAGE THAT DID NOT EXIST (Mark, 2026-09-15, stating the workflow):
    "pull data --> interpret data --> interpret structure --> does this make sense
    structurally and based on common sense --> commentary". Pull and interpret-data
    were automated; the sanity gate was automated but only over cards that already
    existed, so it ran a stage too late. Interpreting the STRUCTURE -- which levels
    are even writable, and why the others are not -- was done by hand every time,
    which is how a plan came to be written on two strikes 13 points from price.

    It reuses command_center's own contract rather than restating it: `_material`
    (a strike carrying MATERIAL_PCT of its book's largest within one EM) and
    `_struct_stop` (the buffer beyond the nearest material strike on the adverse
    side, clustered, tape-demoted). One implementation
    of the card rules, and it is the one the sheet already recomputes stops with.

    Shape follows geometry: a strike BELOW price offers a hold (long) and a break
    (short); one ABOVE offers a rejection (short) and an acceptance (long). Written
    out per side because the same strike is often cardable one way and not the other.
    """
    sys.path.insert(0, str(ROOT))
    import command_center as CC
    import swings as SW
    import vwap as PR
    # command_center's tape helpers key on epoch-ms `t` (the dashboard's shape); the bar
    # cache on disk keys on an ISO `ts`. Convert once here rather than teaching either
    # side about the other -- _closes_through is what demotes a strike the tape has
    # already cut through, and dropping it silently would quietly re-promote pivots.
    cc_bars = [{"t": int(datetime.datetime.fromisoformat(b["ts"]).timestamp() * 1000), "c": b["c"]}
               for b in bars] if bars else []
    ref = float(T.get("last_print") or T.get("spot") or 0)
    band = float(T.get("em") or 0) * float(T.get("rail_scale") or 1)
    # THE DAY'S MAP (2026-09-16). `band` is what is LEFT of the session and it shrinks all
    # day; the board does not. Building the roster off the shrinking band made the sheet a
    # snapshot of wherever price sat at build time -- on 2026-09-16 the top node on the
    # board, 29,600, was carded by none of the day's nine sheets. The roster comes off the
    # session move, the reach flag comes off the drawn band, and both go on the page.
    day_band = float(T.get("em_full") or T.get("em") or 0) or band
    if not ref or not band:
        return [], ref, band
    reach = reach_frac * band
    in_map = lambda px: abs(float(px) - ref) <= day_band + 0.01
    mat = CC._material(T, ref, cc_bars)              # {strike: {book: gamma}}; a union, see _material
    # THE MAP IS NOT ONLY GAMMA STRIKES (2026-09-16, Mark: "the engine is not taking into
    # consideration the session rails or hvl flips both still important"). It was not: rails
    # appeared once, as T1 candidates, and the flips did not appear in this module at all --
    # so the two structures the research actually supports were the two the roster ignored.
    # The rails are the one validated type on the page (containment of the close); a flip is
    # where dealer hedging changes sign, and price crossing one changes what every other level
    # on the board does. They join the roster as levels in their own right. A rail or flip
    # within TAG_PTS of a material strike is that strike wearing a second hat, not a new line.
    TAG_PTS = 6.0
    extra = []
    for key, lab in (("rail_lo", "lower rail"), ("rail_hi", "upper rail")):
        if T.get(key) is not None:
            extra.append((float(T[key]), lab))
    for book in ("0DTE", "NEAR", "FULL"):
        for h in ((T.get("panels") or {}).get(book) or {}).get("hvl") or []:
            extra.append((float(h), f"{book} flip"))
    # VWAP AND THE VOLUME PROFILE (2026-09-16). Both were on Mark's chart and neither was
    # in the engine -- he read a whole short scenario off the POC, the value-area low and
    # price sitting on VWAP, and the sheet could not name one of them. VWAP reproduces his
    # chart to the tick once the bar cache stopped dropping volume; the POC and value area
    # are approximations, spread from OHLC rather than built from ticks.
    # VWAP DEVIATION BANDS (Mark, 2026-09-16): "I typically take longs below weekly vwap
    # near its -1 -2 or -3 deviations and shorts above the +1 +2 +3 deviations. The same
    # applies for session vwap. Also though I use session vwap the weekly should have more
    # weight if there are points of interest further out." They are LEVELS AND A NOTE, not
    # a score: "lets not hard encode that in the grading logic but just have it as a note
    # for now until we watch it." On the day this went in, weekly +1 came out at 29,451.32
    # against a 29,450 put wall, and weekly VWAP itself at 29,342.57 against the 29,341 rail
    # and a +55 rung at 29,350 -- three unrelated structures inside nine points.
    _wk = _se = None
    if bars:
        try:
            # THE WEEKLY BOOK NEEDS THE WEEK (2026-09-16). `bars` here is the since-the-book
            # tape, which on an Asia build starts at the book day's 13:30Z -- two sessions,
            # not a week. week_start() then found no Sunday reopen inside it and anchored on
            # the first bar, so the "weekly" bands were struck on two days of tape: +1 came
            # out at 29,448 against build.py's own panel on the same page saying 29,464, and
            # the sheet contradicted its own prose. Rebuild the window the way build.py does.
            _d0 = datetime.date.fromisoformat(bars[-1]["ts"][:10])
            _week = []
            for _k in range(7, -1, -1):
                _week += _bars((_d0 - datetime.timedelta(days=_k)).isoformat())
            _week.sort(key=lambda b: b["ts"])
            _week = _week or bars
            _wk = PR.bands(_week, PR.week_start(_week))
            _se = PR.bands(bars, bars[-1]["ts"][:10] + " 13:30")
            for lab, bk in (("weekly", _wk), ("session", _se)):
                if not bk or not bk.get("vwap"): continue
                extra.append((bk["vwap"], lab + " VWAP"))
                for nm, lv in bk["bands"].items():
                    extra.append((lv, lab + " " + nm))
        except Exception:
            _wk = _se = None

    tags = {}                                        # strike -> [label, ...]
    for px, lab in extra:
        near = min(mat, key=lambda k: abs(k - px)) if mat else None
        if near is not None and abs(near - px) <= TAG_PTS:
            tags.setdefault(float(near), []).append(lab)
        else:
            tags.setdefault(px, []).append(lab)
    roster = sorted(set(list(mat) + [k for k in tags if k not in mat]), reverse=True)
    # THE TAPE'S OWN STRUCTURE (2026-09-16). Three things the roster never knew:
    # which way the floors are going, where an entry sits in the range, and where the
    # market last turned. Audited over 210 archived cards, the engine put 66% of its
    # SHORTS at the floor and only 48% of all cards at the edge matching their side --
    # because it carded every strike symmetrically and called that neutrality.
    read = SW.bias(bars) if bars else {"bias": "none", "why": "no bars", "since": None}
    # location is a SESSION question; the read is a multi-day one
    sess_bars = [x for x in bars if x["ts"] >= (bars[-1]["ts"][:10] + " 13:30")] or bars
    floor = statistics.median(b["h"] - b["l"] for b in bars) if bars else 0.0
    lo = min((b["l"] for b in bars), default=None)
    hi = max((b["h"] for b in bars), default=None)
    out = []
    for k in roster:
        d = k - ref
        if abs(d) > day_band:
            continue
        g = mat.get(k) or {}
        kind = "strike" if k in mat else "line"      # a rail or flip that is not also a strike
        traded = (lo is not None and lo <= k <= hi)
        first = next((b["ts"][11:16] for b in bars if b["l"] <= k <= b["h"]), None) if traded else None
        # the level's history since the book, with the structural buffer as the tolerance
        tol = max(CC.STOP_BUFFER, CC.STOP_BUFFER_EM * band)
        state, visits, v_first, v_last = level_state(bars, k, tol, wall_below=(d < 0))
        live = abs(d) <= reach
        row = dict(strike=k, kind=kind, tags=tags.get(k, []), read=read,
                   gex_0dte=g.get("0DTE", 0),
                   gex=g.get("NEAR", g.get("FULL", 0)), dist=d,
                   bands=d / band, day_bands=(d / day_band if day_band else None),
                   in_reach=live, reach=("live" if live else "watch"), day_band=day_band,
                   traded=traded, first=first,
                   state=state, visits=visits, last_visit=v_last, sides=[])
        for side, offset in (("long", 1), ("short", -1)):
            above = d > 0
            shape = ("acceptance" if side == "long" else "rejection") if above else \
                    ("hold" if side == "long" else "break")
            entry = k + offset            # one point inside the level, the harness rule
            # ENTRY RIDES THE SWING (Mark: "make entries as close to the swing low as
            # possible"). When the tape turned on this level, the turn is the price worth
            # leaning on -- every point away from it is risk bought for nothing.
            sw_p, sw_ts, _ = SW.entry_swing(bars, side, near=k) if bars else (None, None, None)
            # The swing has to be ON this level AND on the level's own side of the
            # reference -- otherwise riding to it moves a short below the last print, or
            # hands a level 300 points above price a stop from a floor down at the lows.
            same_side = sw_p is not None and (sw_p - ref) * (k - ref) >= 0
            on_swing = sw_p is not None and abs(sw_p - k) <= CC.CLUSTER_PTS and same_side
            if on_swing:
                entry = sw_p
            # Levels off the grid -- VWAP, its bands, the rails -- carry fractions the tape
            # cannot trade. Round to the quarter tick, which is also what the entry zone
            # rounds to, so an entry can never fall outside its own zone (2026-09-16).
            entry = round(entry * 4) / 4.0
            # STOP FROM THE STRUCTURE, NOT THE BAND. invalidation() is the swing before the
            # one the entry sits on -- the floor whose loss ends the sequence. One point
            # beyond it, because "a new low" means trading through it.
            # Only a level the tape has actually turned on gets a swing stop. Without a
            # swing here there is no "place the market defended" to lean on, and the gamma
            # structure is the honest fallback.
            # Use it whenever the session HAS a swing beyond the entry -- not only when the
            # entry happens to sit on one. 29,450 is a gamma wall between two pivots and its
            # stop still belongs under the pivot, not behind the next strike (2026-09-16).
            inv_p, inv_ts = SW.invalidation(sess_bars, side, entry, min_dist=floor)                 if sess_bars else (None, None)
            st = CC._struct_stop({"entry": entry, "side": side}, T, cc_bars)
            if inv_p is not None:
                st = dict(st or {}, stop=(inv_p - 1 if side == "long" else inv_p + 1),
                          anchor=inv_p, dist=abs(entry - inv_p), ok=True, swing=True,
                          swing_ts=inv_ts, pivot=False, wide=False)
            loc = SW.where_in_range(sess_bars, entry, price=ref) if sess_bars else {"where": "unknown", "at": None}
            why = []
            # A WIDE STOP IS A NUMBER, NOT A VETO (2026-09-16). The old 80-point cap was a
            # prop-firm limit, and it deleted the card whenever the nearest undemoted strike sat
            # further out -- exactly what happens to a level the tape has closed through all day,
            # i.e. the levels most worth watching on a trending session. On 2026-09-16 it left the
            # sheet reading NOTHING BELOW while 29,400 and 29,350 sat right there on the board.
            # The stop is now wherever the structure is; its width and its R are printed on the
            # card, and only a card with no adverse-side structure at all is refused.
            wide = bool(st and st.get("stop") is not None and not st.get("ok"))
            if not st or st.get("stop") is None:
                why.append("every material strike on the adverse side has been closed through "
                           "today -- no stop can be anchored")
            risk = abs(entry - st["stop"]) if st and st.get("stop") is not None else None
            # A stop inside one bar's range is a NOTE, not a veto (2026-09-16). The swing
            # stop is sometimes very tight -- the most recent pivot can be seven points away --
            # and that is a small, structural risk, not an absurdity. It gets said on the card.
            tight = bool(risk and floor and risk < floor)
            # T1 = the first structure beyond the entry -- a material strike or a rail, inside the
            # band -- that pays 1.2R against the structural stop, naming the walls it passes.
            # With the stop behind the wall behind (2026-09-15) the nearest strike rarely pays; Mark
            # chose fewer, wider cards with further targets over none at all.
            rails = [float(x) for x in tags]        # every rail and flip, plus the strikes they tag
            beyond = sorted((m for m in list(mat) + rails
                             if (m > entry if side == "long" else m < entry) and in_map(m)),
                            key=lambda m: abs(m - entry))
            t1, r1, passed = None, None, []
            for m in beyond:
                if not risk:
                    break
                rr = abs(m - entry) / risk
                if rr >= 1.2:
                    t1, r1 = m, rr
                    break
                passed.append(m)
            notes = []
            # A SHORT T1 IS A NUMBER, NOT A VETO (2026-09-16). The 1.2R floor arrived with the
            # original import on 09-09, before any of the studies, to catch RTH cards whose
            # noise-scaled stop left T1 inside 1R. It was never measured against 1.0 or against
            # no floor, and it constrains the wrong variable: the only lever the engine has to
            # satisfy it is the TARGET, so structure that does not pay walks TP1 out to a level
            # that is not there, or deletes the card. Breakeven at TP1 -- which IS supported --
            # already covers the risk it was guarding. The card ships with the R printed on it.
            if risk and t1 is None and beyond:
                t1, r1 = beyond[0], abs(beyond[0] - entry) / risk
            if risk and t1 is None:
                why.append("no structure of any kind beyond the entry inside the day's map")
            # T2 and T3 walk on from T1 to the next two structures. A plan states three
            # targets or it is not a plan someone can scale out of (2026-09-16).
            t2 = t3 = None
            if t1 is not None:
                further = [m for m in beyond
                           if (m > t1 + 1 if side == "long" else m < t1 - 1)]
                further.sort(key=lambda m: abs(m - entry))
                t2 = further[0] if further else None
                t3 = further[1] if len(further) > 1 else None
            if not live:
                notes.append(f"watch: {abs(d) / band:.2f} bands out, live inside {reach:,.0f}")
            # the weekly is checked first, so it wins a tie -- his rule
            _nb = PR.near_band(entry, ("weekly", _wk), ("session", _se)) if (_wk or _se) else None
            if _nb:
                _bk, _nm, _lv, _ = _nb
                _his = (side == "long" and _nm.startswith("-")) or (side == "short" and _nm.startswith("+"))
                notes.append(f"on {_bk} VWAP {_nm} ({_lv:,.0f})"
                             + (" -- your side of it" if _his else ""))
            if tight:
                notes.append(f"tight stop: {risk:.0f} pts, inside one median bar ({floor:.1f})")
            if wide:
                notes.append(f"wide stop: {st['dist']:,.0f} pts to the structure behind it")
            if st and st.get("pivot"):
                notes.append(f"stop anchored on a pivot the tape has closed through, not a wall")
            if st and st.get("swing"):
                notes.append(f"stop 1 under the {st['swing_ts'][5:16]}Z swing {st['anchor']:,.2f}"
                             if side == "long" else
                             f"stop 1 over the {st['swing_ts'][5:16]}Z swing {st['anchor']:,.2f}")
            elif st and st.get("stop") is not None:
                notes.append(f"stop behind {st['anchor']:,.0f}" + (" (the wall behind)" if st.get("behind") else " (own wall)"))
            if passed:
                notes.append("T1 through " + ", ".join(f"{m:,.0f}" for m in passed))
            # LEVEL-STATE POLICY (2026-09-15; 355 Asia cards on NY's book, contrasts named
            # before the run, see level_state). The only robust result: a continuation
            # through a wall the tape CLOSED beyond and stayed beyond loses -- 17% win vs
            # a 27% coin flip, -0.42R, and it held under both touch and close execution.
            # It chases a move already made. A fade at a broken or reclaimed wall is a
            # coin flip either way (+0.01R / -0.06R), so that is a note the prose has to
            # carry -- "second visit; what the first one did" -- never a disqualifier.
            # The earlier rule, "a hold of a wall that failed is void", was common sense
            # and the record does not support it. A wick is not a failure.
            if shape in ("break", "acceptance") and state == "broken":
                why.append("the tape closed beyond this wall and stayed there: a continuation "
                           "through it chases a move already made")
            elif state in ("broken", "reclaimed"):
                notes.append(f"second visit ({state})")
            elif state == "wicked":
                notes.append("wicked, held")
            # GRADE, NOT A GATE (2026-09-16). Three tiers, and every level stays on the page:
            #   card   -- writable now: structure on the adverse side, and T1 pays at least its
            #             own risk. 1.0R is break-even, not a fitted number; the old 1.2 floor
            #             was never measured and deleted the card instead of grading it.
            #   level  -- a real level on today's map whose geometry does not pay from HERE.
            #             It is named in the reading with what it would take, and it becomes a
            #             card when price comes to it and the structure recomputes.
            #   none   -- nothing to anchor a stop to at all.
            # WHERE THE ENTRY SITS DECIDES WHETHER IT IS A CARD (Mark, 2026-09-16: the
            # gameplan "outputted entries in the middle of the range, shorts at the bottom
            # of the range (which are inherently more risky) and longs at the top of the
            # range (also risky)"). A short at a floor is selling the level price just
            # turned up from; mid-range has no structure under it at all. These stay on the
            # page as levels -- they are real levels -- but they are not cards.
            # Only INSIDE the range is an edge the wrong edge. Above the range a long is a
            # breakout (his 29,300 trade) and below it a short is a breakdown; those are the
            # continuation shapes, not resistance-buying.
            wrong_edge = (side == "short" and loc["where"] == "floor") or                          (side == "long" and loc["where"] == "ceiling")
            breakout = (side == "long" and loc["where"] == "above") or                        (side == "short" and loc["where"] == "below")
            # CHASING is the biggest defect in the record: 36% of 210 archived cards were a
            # SHORT above a range that had just broken upward, or a LONG below one that had
            # broken down -- fading the move after it happened, with the whole range between
            # the entry and any structure. Bigger than mid-range and wrong-edge combined.
            chasing = (side == "short" and loc["where"] == "above") or                       (side == "long" and loc["where"] == "below")
            against = (read["bias"] == "up" and side == "short") or                       (read["bias"] == "down" and side == "long")
            grade = ("none" if why else "card" if (r1 or 0) >= 1.0 else "level")
            if grade == "card" and chasing:
                grade = "level"
                notes.append(f"chasing: {side} at {loc['at']:.0%} of the "
                             f"{loc['lo']:,.0f}-{loc['hi']:,.0f} range -- the move already went "
                             f"the other way and there is no structure between here and it")
            elif grade == "card" and loc["where"] == "middle":
                grade = "level"
                notes.append(f"mid-range entry ({loc['at']:.0%} of {loc['lo']:,.0f}-{loc['hi']:,.0f})"
                             " -- no structure under it")
            elif grade == "card" and wrong_edge:
                grade = "level"
                notes.append(f"{side} at the {loc['where']} ({loc['at']:.0%} of the range)"
                             f" -- the wrong edge to be taking this side")
            elif grade == "card" and against:
                notes.append(f"against the read ({read['bias']}: {read['why']})")
            elif grade == "card" and breakout:
                notes.append(f"breakout entry, {loc['at']:.0%} of the {loc['lo']:,.0f}-{loc['hi']:,.0f} range")
            elif grade == "card" and loc["where"] in ("floor", "ceiling"):
                notes.append(f"at the {loc['where']} ({loc['at']:.0%} of the range)")
            if grade == "level":
                notes.append(f"level, not a card from here: T1 pays {r1:.2f}R" if r1
                             else "level, not a card from here: no target inside the map")
            # AN ENTRY IS A ZONE, NOT A PRICE (Mark, 2026-09-16: "I want entries to be shown
            # as a range as well rather than a flat number described basically as fact"). A
            # single number reads as an instruction and implies a precision no fill has. The
            # zone runs between the two structures the card leans on -- the level and the swing
            # the tape turned on -- and where there is only one, one median bar of the session's
            # own tape, which is the smallest move the reader can actually transact inside.
            edges = [entry] + ([float(k)] if on_swing and abs(entry - k) > 0.01 else [])
            if len(edges) == 1:
                edges.append(entry - floor if side == "long" else entry + floor)
            e_lo, e_hi = round(min(edges) * 4) / 4, round(max(edges) * 4) / 4
            # THE GATE (2026-09-16, Mark's short scenario: "the short doesn't exist until
            # 29,450 goes"). A card on the far side of a structure is not a trade yet, it is
            # a trade that needs that structure to fail first. The gate is the nearest
            # material level strictly between the reference and the entry.
            between = [m for m in mat
                       if (entry < m < ref) or (ref < m < entry)]
            gate = (max(between) if entry < ref else min(between)) if between else None
            row["sides"].append(dict(shape=shape, side=side, entry=entry, gate=gate,
                                     entry_lo=e_lo, entry_hi=e_hi,
                                     stop=(st or {}).get("stop"), risk=risk, t1=t1, r1=r1,
                                     t2=t2, t3=t3, tight=tight,
                                     reach=row["reach"], grade=grade, wide=wide,
                                     loc=loc["where"], at=loc["at"], on_swing=on_swing,
                                     swing_stop=bool((st or {}).get("swing")), against=against,
                                     ok=(grade == "card"), why="; ".join(why),
                                     note=" · ".join(notes)))
        out.append(row)
    return out, ref, band


def band_map(T, bars, width=20.0, start=None, label="weekly"):
    """The weekly VWAP bands read against the gamma book. Notes, not rules.

    Mark, 2026-09-16: "consider we are trending upwards, weekly vwap +1 might hold
    because we are trending, with +2 as potential resistance -- can you map that
    against the gamma levels and tell me what you find."

    What the map is for is the SIGN of the gamma sitting on each band, because it
    decides what the level does when price gets there:

      positive   dealers are long it and sell rallies into it -- resistance whose
                 hedging flow runs AGAINST the move
      negative   dealers sell into weakness -- the level can still hold on the tape
                 (29,450 held three times that afternoon and the desk bought it),
                 but a failure has flow running WITH it, so the stop is the point
      thin       nothing behind it either way

    And whether the band is even reachable in what is left of the session: on the
    day this was written, weekly +3 sat 77 points past the upper rail with +7 on the
    expiring book -- a multi-day level, not a today one.
    """
    import vwap as PR
    out = []
    if not bars:
        return out
    try:
        wk = PR.bands(bars, PR.week_start(bars) if start is None else start)
    except Exception:
        return out
    if not wk.get("vwap"):
        return out
    ref = float(T.get("last_print") or T.get("spot") or 0)
    lo, hi = T.get("rail_lo"), T.get("rail_hi")
    g = {float(k): int(v) for k, v in
         ((T.get("panels") or {}).get("0DTE") or {}).get("gamma", {}).items()}
    # BOTH SIDES (2026-09-16). bands() computes all six and this kept three. Mark's rule is
    # symmetric -- "I typically take longs below weekly vwap near its -1 -2 or -3 deviations
    # and shorts above the +1 +2 +3" -- so a map that only draws the upper half describes
    # half his method. They are still notes: they move no score.
    rows = [(label + " VWAP", wk["vwap"])] + [
        (label + " " + k, wk["bands"][k])
        for k in ("+3", "+2", "+1", "-1", "-2", "-3") if k in wk["bands"]]
    for lab, px in sorted(rows, key=lambda t: -t[1]):
        near = sorted(((k, g[k]) for k in g if abs(k - px) <= width),
                      key=lambda t: abs(t[0] - px))
        tot = sum(v for _, v in near)
        sign = ("positive" if tot >= 25 else "negative" if tot <= -10 else "thin")
        read = {"positive": "dealers long it sell rallies into it -- flow against the move",
                "negative": "a failure here has hedging flow behind it; the stop is the point",
                "thin": "little behind it either way"}[sign]
        reach = (lo is not None and hi is not None and float(lo) <= px <= float(hi))
        out.append(dict(label=lab, px=px, dist=px - ref, inside=bool(reach), sign=sign,
                        read=read, gex=tot,
                        near=[[k, v] for k, v in near[:2]],
                        note=("" if reach else "outside the rest-of-session rails -- not a today level")))
    return out


def rank(rows, read, z_max=None):
    """Score every writable side and return them best first -- the A trade at the top.

    WHY (Mark, 2026-09-16, holding Nicky's post up next to a sheet carrying 36 things):
    "This is genuinely harder to read, why are there so many scenarios? we should only be
    doing the highest probability ones." His example is one line -- a direction, an entry,
    an invalidation, three targets, a caveat. That is the shape a plan gets read in.

    Nothing here is a probability. None was measured and none is claimed. It is a ranking
    over what the sheet can actually see, weighted in the order he has been giving all day:

      the read        a card with the floors is worth more than one against them, and one
                      against them is worth less than nothing
      location        at its own edge -- a long at a floor, a short at a ceiling -- or a
                      breakout past the range, which is his own 29,300 trade
      live            inside the window price can still reach before the next rebuild
      the stop        anchored on a swing the tape turned on, not on a gamma fallback
      0DTE weight     real paper on the book that expires today, scored against that book

    Stop width does not rank (Mark, 2026-09-25: "we dont need to favor tighter stops just more
    directional accuracy"). R at T1 used to score here, and it is the same preference in other
    clothes: off one target, a tighter stop is a bigger R. Ties break on distance from the
    reference, nearer first -- the card price reaches first.
    """
    bias = (read or {}).get("bias", "none")
    out = []
    for r in rows:
        for sd in r["sides"]:
            if sd["grade"] != "card":
                continue
            sc, why = 0.0, []
            if bias != "none":
                if (bias == "up") == (sd["side"] == "long"):
                    sc += 3; why.append("with the read")
                else:
                    sc -= 3; why.append("against the read")
            if (sd["side"] == "long" and sd.get("loc") == "floor") or                (sd["side"] == "short" and sd.get("loc") == "ceiling"):
                sc += 2; why.append("at its own edge")
            elif (sd["side"] == "long" and sd.get("loc") == "above") or                  (sd["side"] == "short" and sd.get("loc") == "below"):
                sc += 1; why.append("breakout")
            if sd.get("reach") == "live":
                sc += 2; why.append("live")
            if sd.get("swing_stop"):
                sc += 1; why.append("swing stop")
            if sd.get("tight"):
                # 2026-09-16: the desk's own stops run 40-60 points (his 29,250 trade was 43,
                # Nicky's posted invalidation 56). A stop inside one median bar is a scalp on
                # a different timeframe, not the A trade, so it ranks below one that is not.
                sc -= 1.5; why.append("stop inside one bar")
            z = abs(r.get("gex_0dte") or 0)
            if z_max and z >= 0.25 * z_max:
                sc += 1; why.append("0DTE weight")
            out.append(dict(strike=r["strike"], row=r, side=sd,
                            score=round(sc, 2), why=", ".join(why)))
    out.sort(key=lambda c: (-c["score"], abs(c["row"].get("dist") or 0)))

    # ADJACENT CARDS ON THE SAME SIDE ARE ONE ZONE (Mark, 2026-09-16, on Nicky's post:
    # "it did output a flat 200 but it did recognize 250-200, it just chose the bottom of
    # that. The team all took longs from that range."). 29,425 hold and 29,450 hold are two
    # rungs of one pullback, not two trades competing for the top of a ranking. Merged they
    # read the way the desk actually worked it: entries across the range, the invalidation
    # of the deeper one, the targets of the further one.
    merged, used = [], set()
    for i, c in enumerate(out):
        if i in used:
            continue
        sd, grp = c["side"], [c]
        for j, o2 in enumerate(out):
            if j <= i or j in used or o2["side"]["side"] != sd["side"]:
                continue
            if o2["side"]["shape"] != sd["shape"]:
                continue
            lo = min(sd["entry_lo"], o2["side"]["entry_lo"])
            hi = max(sd["entry_hi"], o2["side"]["entry_hi"])
            if hi - lo <= CLUSTER_ZONE:
                grp.append(o2); used.add(j)
        if len(grp) == 1:
            merged.append(c); continue
        sides = [g["side"] for g in grp]
        lo = min(x["entry_lo"] for x in sides); hi = max(x["entry_hi"] for x in sides)
        # the invalidation of the deepest rung, the targets of the furthest
        far = max(sides, key=lambda x: abs((x.get("t3") or x.get("t1") or 0) - lo))
        stop = min(x["stop"] for x in sides if x["stop"] is not None) if sd["side"] == "long"             else max(x["stop"] for x in sides if x["stop"] is not None)
        # THE ZONE CANNOT CONTAIN ITS OWN STOP (2026-09-16). The merge takes the widest
        # entry span across the rungs and the DEEPEST rung's invalidation, and those two can
        # cross: 29,413-29,464 with the stop at 29,417 means a fill at the bottom of the zone
        # is already stopped. Clamp the adverse edge to one tick inside the stop.
        if sd["side"] == "long":
            lo = max(lo, stop + 0.25)
        else:
            hi = min(hi, stop - 0.25)
        if hi < lo:
            lo = hi = (stop + 0.25) if sd["side"] == "long" else (stop - 0.25)
        # The card is quoted at the WORST fill in the zone -- the top for a long, the
        # bottom for a short -- so the R on the page is the R you get if you are filled
        # last, and the printed entry, the printed risk and the R all agree. Quoting the
        # best edge made the sheet's own risk (10 pts) disagree with the ranking's (47.5).
        entry = round((hi if sd["side"] == "long" else lo) * 4) / 4.0
        risk = abs(entry - stop)
        # A merged zone can swallow the shallower rungs' targets: 29,417-29,464 with T1 at
        # 29,450 is a long target BELOW its own entry. Keep only targets beyond the entry.
        tps = [t for t in (far.get("t1"), far.get("t2"), far.get("t3")) if t is not None
               and (t > entry if sd["side"] == "long" else t < entry)]
        if not tps or not risk:
            merged.append(c)                 # nothing left to aim at; keep the best rung
            continue
        # A MERGE MUST NOT MAKE THE TRADE WORSE. Quoting the worst fill across a wide zone
        # can leave less R than the best single rung had: on 2026-09-16 a four-rung merge
        # spanning 47 points came out at 0.22R where the rung alone paid 2.67R. The zone is
        # a convenience for the reader, not a licence to degrade the card.
        best_single = max((g["side"].get("r1") or 0) for g in grp)
        if abs(tps[0] - entry) / risk < best_single:
            merged.append(c)
            continue
        z = dict(sd, entry=entry, entry_lo=round(lo * 4) / 4.0, entry_hi=round(hi * 4) / 4.0,
                 stop=stop, risk=risk,
                 t1=tps[0], t2=(tps[1] if len(tps) > 1 else None),
                 t3=(tps[2] if len(tps) > 2 else None),
                 r1=abs(tps[0] - entry) / risk)
        merged.append(dict(strike=min(g["strike"] for g in grp), row=c["row"], side=z,
                           score=max(g["score"] for g in grp),
                           zone=[g["strike"] for g in grp],
                           why=c["why"] + f", {len(grp)} rungs as one zone"))
    merged.sort(key=lambda c: (-c["score"], abs(c["row"].get("dist") or 0)))
    return merged


def print_plan(ranked, ref, read, n=2):
    """The plan as it should be read: the A trade, and at most one alternate."""
    print()
    if not ranked:
        print("  NO TRADE -- nothing on the map is a card from here.")
        print()
        return
    b = (read or {}).get("bias", "none")
    head = {"up": "BULLISH", "down": "BEARISH", "none": "NO DIRECTION"}[b]
    print("  " + head + "   reference {:,.2f}".format(ref))
    for i, c in enumerate(ranked[:n]):
        sd = c["side"]
        tag = "A TRADE  " if i == 0 else "alternate"
        tps = [t for t in (sd.get("t1"), sd.get("t2"), sd.get("t3")) if t]
        tp = ", ".join("T{} {:,.0f}".format(j + 1, t) for j, t in enumerate(tps))
        print()
        print("  {}: {} {:,.0f} {}   [{}]".format(
            tag, sd["side"].upper(), c["strike"], sd["shape"], c["score"]))
        print("    entries {:,.0f}-{:,.0f}, invalidation {:,.0f} ({:.0f} pts), {}".format(
            sd["entry_lo"], sd["entry_hi"], sd["stop"] or 0, sd["risk"] or 0, tp))
        print("    " + c["why"])
    if len(ranked) > n:
        print()
        print("  {} further card(s) scored lower and are held back.".format(len(ranked) - n))
    print()


def cards_for_build(T, bars, n=2, path="notes/plan_cards.json"):
    """The ranked plan in build.py's CARDS shape, written where the sheet can find it.

    Mark, 2026-09-16: "make sure your proposed gameplan is now pushed into the dashboard
    updating the cards." Until now the model invented every entry, stop and target and
    the page corrected the stop afterwards. The engine computes all of it -- the zone,
    the swing invalidation, three targets, the gate and the rank -- so the cards come
    from here and the model writes only the prose.

    `n` is how many cards ship: the A trade and one alternate. Everything below them is
    counted, not printed ("we should only be doing the highest probability ones").
    """
    import json as _json
    rows, ref, band = candidates(T, bars)
    if not rows:
        return []
    read = rows[0].get("read") or {}
    zmax = max((abs(r.get("gex_0dte") or 0) for r in rows), default=0) or 1
    ranked = rank(rows, read, zmax)
    out = []
    for i, c in enumerate(ranked[:n]):
        sd = c["side"]
        zone = sorted({float(z) for z in (c.get("zone") or [c["strike"]])})
        nm = "{:,.0f}".format(min(zone)) + ("-{:,.0f}".format(max(zone)) if len(zone) > 1 else "")
        tps = [t for t in (sd.get("t1"), sd.get("t2"), sd.get("t3")) if t]
        loc = {"floor": "at the floor", "ceiling": "at the ceiling",
               "above": "breakout", "below": "breakdown"}.get(sd.get("loc"), "")
        bits = [b for b in (loc, "0DTE {:+d}".format(int(c["row"].get("gex_0dte") or 0)),
                            "{} rungs as one zone".format(len(zone)) if len(zone) > 1 else "") if b]
        tag = "Entry zone {:,.0f}-{:,.0f}, invalidation {:,.0f}".format(
            sd["entry_lo"], sd["entry_hi"], sd["stop"])
        if sd.get("gate"):
            tag += ". GATED: needs {:,.0f} to {} first".format(
                sd["gate"], "fail" if sd["side"] == "short" else "hold")
        why = ". ".join(x for x in (" &middot; ".join(bits), c.get("why", ""), sd.get("note", "")) if x)
        out.append(dict(name="{} {}".format(nm, sd["shape"]),
                        tag=tag, rank="Primary" if i == 0 else "Secondary", pri=(i == 0),
                        side=sd["side"], entry=sd["entry"], sl=sd["stop"], tp=tps,
                        why=why, zone_lo=sd["entry_lo"], zone_hi=sd["entry_hi"],
                        gate=sd.get("gate"), score=c["score"], reach=sd.get("reach"),
                        held_back=max(0, len(ranked) - n)))
    # THE HELD-BACK CARDS NEED NAMES, NOT A COUNT (2026-09-16). "Everything else is a
    # one-line held back list" -- a bare integer cannot be written as a list. The name is
    # the same expression the shipped cards use, so a held-back card reads exactly as it
    # would have if it had made the cut.
    held = []
    for c in ranked[n:]:
        z = sorted({float(x) for x in (c.get("zone") or [c["strike"]])})
        nm = "{:,.0f}".format(min(z)) + ("-{:,.0f}".format(max(z)) if len(z) > 1 else "")
        held.append(dict(name="{} {}".format(nm, c["side"]["shape"]),
                         side=c["side"]["side"], score=c["score"],
                         reach=c["side"].get("reach"), why=c.get("why", "")))
    if path:
        try:
            _json.dump({"reference": ref, "cards": out,
                        "held_back": max(0, len(ranked) - n), "held_back_cards": held},
                       open(path, "w", encoding="utf-8"), indent=1)
        except Exception:
            pass
    return out


def print_candidates(rows, ref, band):
    if not rows:
        print("\n  no material strike near the reference -- nothing to card.\n")
        return
    day = next((r["day_band"] for r in rows if r.get("day_band")), 0) or max((abs(r["dist"]) for r in rows), default=0)
    print(f"\n  CANDIDATE LEVELS   reference {ref:,.2f}")
    print(f"  live window  ±{REACH_FRAC * band:,.0f}  ({ref - REACH_FRAC * band:,.0f} to "
          f"{ref + REACH_FRAC * band:,.0f})   -- what price can reach in what is left")
    print(f"  the day's map  ±{day:,.0f}  ({ref - day:,.0f} to {ref + day:,.0f})"
          f"   -- today's levels, carded whether or not price is near them")
    if rows and rows[0].get("read"):
        rd = rows[0]["read"]
        print(f"  the read     {rd['bias'].upper()}  --  {rd['why']}")
    print("  " + "-" * 104)
    print(f"  {'strike':>8} {'gex':>6} {'dist':>7} {'since book':>16}   {'shape':<11} "
          f"{'entry':>8} {'stop':>8} {'risk':>5} {'T1':>8} {'R':>5}  verdict")
    for r in rows:
        st = r["state"] + (f" x{r['visits']}" if r["visits"] > 1 else "") + (f" {r['last_visit']}" if r["last_visit"] else "")
        tag = ("  " + "/".join(r.get("tags") or [])) if r.get("tags") else ""
        head = (f"  {r['strike']:>8,.0f} {r['gex']:>6,.0f} {r['dist']:>+7,.0f} "
                f"{st:>16}")
        if tag:
            print(f"  {r['strike']:>8,.0f}{tag}")
        for i, s in enumerate(r["sides"]):
            print((head if i == 0 else " " * len(head))
                  + f" {('' if r['reach'] == 'live' else 'w'):>1} {s['shape']:<11} "
                  + f"{s['entry_lo']:,.0f}-{s['entry_hi']:,.0f}".rjust(15) + " "
                  + (f"{s['stop']:>8,.0f}" if s["stop"] is not None else f"{'-':>8}") + " "
                  + (f"{s['risk']:>5,.0f}" if s["risk"] else f"{'-':>5}") + " "
                  + (f"{s['t1']:>8,.0f}" if s["t1"] else f"{'-':>8}") + " "
                  + (f"{s['r1']:>5.2f}" if s["r1"] else f"{'-':>5}")
                  + ("  " + {"card": "CARD", "level": "level"}.get(s.get("grade"), "--")
                     + (" · " + s["note"] if s.get("note") else "") if s.get("grade") != "none"
                     else "  " + s["why"]))
    ok = [(r, s) for r in rows for s in r["sides"] if s["ok"]]
    lvl = [(r, s) for r in rows for s in r["sides"] if s.get("grade") == "level"]
    up = {r["strike"] for r, s in ok if r["dist"] > 0}
    dn = {r["strike"] for r, s in ok if r["dist"] < 0}
    lv = {r["strike"] for r, s in ok if r["reach"] == "live"}
    wt = {r["strike"] for r, s in ok if r["reach"] == "watch"}
    on_map = sorted({r["strike"] for r, s in ok + lvl})
    print("  " + "-" * 104)
    print(f"  {len(ok)} writable card(s) on {len(up | dn)} level(s): "
          + (f"above {', '.join(f'{k:,.0f}' for k in sorted(up))}" if up else "NOTHING ABOVE the reference")
          + " / "
          + (f"below {', '.join(f'{k:,.0f}' for k in sorted(dn))}" if dn else "NOTHING BELOW the reference"))
    print(f"  live now: {', '.join(f'{k:,.0f}' for k in sorted(lv)) if lv else 'none'}"
          f"   |   watch: {', '.join(f'{k:,.0f}' for k in sorted(wt)) if wt else 'none'}")
    if lvl:
        print(f"  on the map but not a card from here ({len(lvl)}): "
              + ", ".join(f"{r['strike']:,.0f} {s['shape']}" for r, s in lvl)
              + " -- name these in the reading; they card when price comes to them.")
    if not up or not dn:
        print("  one side of price has no writable level -- the plan cannot cover both. "
              "Name the uncarded side in the prose.")
    print()


# -------------------------------------------- the commentary writer's entry point ---

def structure_problems(cards, T):
    """
    The hard structural failures on a set of card DICTS, as strings -- for command_center's
    narrative checker, so a model-written plan is sent back for the same reasons a hand-
    written one is refused. [] when clean.

    Until 2026-09-15 the bot's path checked shape, reach and "is it a level" and nothing
    else, so a plan could pass on a band describing the wrong session or through a wall
    the tape had already closed beyond. Cards arrive here in the commentary's own shape
    ({name, side, entry, sl, tp}), not as a rendered sheet, so the per-card rules are
    restated on the dict; same thresholds, same tape, same level-state policy.
    """
    global rows
    if not (T.get("last_print") or T.get("spot")) or not T.get("em"):
        return []                # no book behind these cards: do not invent a failure
    saved, rows = rows, []
    try:
        bars = _bars(T.get("report_date") or T.get("settle"))
        check_band_vs_clock(T)
        check_reference(T, bars)
        out = [f"{chk}: {msg}" for lvl, chk, msg in rows if lvl == HARD]
        tape = check_tape(bars) if bars else None
        since = _since_book(T, bars)
        ref = float(T.get("last_print") or T.get("spot") or 0)
        band = float(T.get("em") or 0) * float(T.get("rail_scale") or 1)
        label = str(T.get("rail_label") or "")
        overnight_plan = label.startswith(("Asia", "Rest of overnight", "London", "Rest of session"))
        for c in cards or []:
            try:
                e, sl = float(c["entry"]), float(c.get("sl", c.get("stop")))
                tps = c.get("tp") or []
                tps = [float(t) for t in (tps if isinstance(tps, (list, tuple)) else [tps])]
            except (KeyError, TypeError, ValueError):
                continue
            side = (c.get("side") or ("short" if tps and tps[0] < e else "long")).lower()
            name, risk = c.get("name", "?"), abs(e - sl)
            lvl = _level_of(name, e)
            if side == "long" and not (sl < e < (tps[0] if tps else e + 1)):
                out.append(f"card {name}: a LONG whose stop is not below its entry with targets above")
            if side == "short" and not (sl > e > (tps[0] if tps else e - 1)):
                out.append(f"card {name}: a SHORT whose stop is not above its entry with targets below")
            if tape and risk < tape["med_bar"]:
                out.append(f"card {name}: stop {risk:,.0f} pts is inside one bar's noise "
                           f"(this session's median bar is {tape['med_bar']:,.1f})")
            # TP1 beyond the session's range so far is absurd once the session HAS a range -- but
            # two hours into a quiet overnight the range is 28 points against a 99-point band, and
            # this rule rejected every card of the 17:15 regeneration three rounds running
            # (2026-09-15: "TP1 is 70 pts out, beyond the session's whole range so far (28)").
            # The band is the plan's own claim about reach; the range only tightens the test once
            # it has grown past the band.
            if (tps and tape and tape["rng"] and abs(tps[0] - e) > tape["rng"]
                    and not inside_band(T, tps[0], e, band) and not inside_day_map(T, tps[0])):
                out.append(f"card {name}: TP1 {tps[0]:,.0f} is {abs(tps[0] - e):,.0f} pts out, beyond the session's "
                           f"whole range so far ({tape['rng']:,.0f}) and off the day's map entirely")
            if since and ref:
                reverting = (side == "long" and lvl <= ref) or (side == "short" and lvl >= ref)
                state = level_state(since, lvl, risk or 10.0, wall_below=(lvl <= ref))[0]
                if state == "broken" and not reverting:
                    out.append(f"card {name}: the tape closed beyond {lvl:,.0f} since the book and stayed "
                               "there -- a continuation through it chases a move already made")
            if bars and band and ref and not overnight_plan:
                eth = [b for b in bars if (int(b["ts"][11:13]), int(b["ts"][14:16])) < (13, 30)]
                if eth:
                    lo, hi = min(b["l"] for b in eth), max(b["h"] for b in eth)
                    if lo <= e <= hi and abs(e - ref) > REACH_FRAC * band:
                        out.append(f"card {name}: entry {e:,.0f} already printed overnight and price has "
                                   f"left it ({abs(e - ref) / band:.2f} bands) -- write it for its next "
                                   "visit or withhold it")
        return out
    finally:
        rows = saved


# ---------------------------------------------------------------------- run ---

def main():
    ap = argparse.ArgumentParser(description="structural sanity before any commentary is written")
    ap.add_argument("--trinity", default="trinity.json")
    ap.add_argument("--cards", default=None, help="a rendered sheet whose cards to grade")
    ap.add_argument("--open", type=float, default=None, help="the session's opening print")
    ap.add_argument("--plan", action="store_true",
                    help="write the ranked plan to notes/plan_cards.json for build.py")
    ap.add_argument("--candidates", action="store_true",
                    help="interpret the structure: which levels are writable, and why the rest are not")
    a = ap.parse_args()
    T = json.loads(pathlib.Path(a.trinity).read_text(encoding="utf-8"))
    bars = _bars(T.get("report_date") or T.get("settle"))

    check_band_vs_clock(T)
    check_reference(T, bars)
    check_settle(T)
    tape = check_tape(bars)
    check_levels_already_traded(T, tape)
    if a.cards:
        check_eth_already_triggered(a.cards, T, bars)
        # THE CHECKER GETS THE GENERATOR'S TAPE (2026-09-16). `since` was repaired by
        # _since_book and `bars` was not, so on an Asia build -- where report_date has no
        # bar file yet and `bars` is empty -- check_cards found no session bars, skipped
        # swings.invalidation() entirely and demanded the gamma-map fallback stop. It then
        # failed as ABSURD every swing stop candidates() had just proposed in the same run.
        # Its own comment says it derives these 'the same way ... not a second opinion'.
        _sb = _since_book(T, bars)
        check_cards(a.cards, T, tape, a.open, since=_sb, bars=_sb)

    print()
    for level, check, msg in rows:
        print(f"  {level}  {check:<12} {msg}")
    hard = sum(1 for l, _, _ in rows if l == HARD)
    # The structure is printed AFTER the checks and BEFORE the verdict, because it is
    # what you write the commentary from -- and printing it even on a failure is the
    # point: the table is usually what explains the failure.
    if a.plan:
        # WRITE THE RANKED PLAN FOR build.py (2026-09-16). This runs BEFORE build.py in the
        # pipeline, not with the gate afterwards, because the sheet has to read it while it
        # renders. build.py checks the reference on the file and falls back to whatever is
        # in its own CARDS block when it does not match, so a failure here degrades to the
        # old behaviour instead of putting a stale plan on the page.
        try:
            cs = cards_for_build(T, _since_book(T, bars))
            print()
            print("  ranked plan: %d card(s) -> notes/plan_cards.json" % len(cs))
            for c in cs:
                print("    %-26s %-5s entry %9.2f  stop %9.2f  tp %s"
                      % (c["name"], c["side"], c["entry"], c["sl"],
                         ", ".join("{:,.0f}".format(t) for t in c["tp"])))
            if not cs:
                print("    nothing ranked -- the sheet keeps the prose cards")
        except Exception as ex:
            print()
            print("  ranked plan FAILED (%s: %s) -- the sheet keeps the prose cards"
                  % (type(ex).__name__, ex))

    if a.candidates:
        # THE TAPE LEDGER COMES FIRST: if tape_ledger.py has been writing this session's
        # events (notes/tape/<session>.md), print them before the structure -- a card that
        # already triggered, was stopped or was spent is written for its next visit or withheld
        led = pathlib.Path("notes") / "tape" / f"{T.get('report_date')}.md"
        if led.exists():
            lines = [l for l in led.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
            print(f"\n  tape ledger ({led}, {len(lines)} events):")
            for l in lines[-40:]:
                print("    " + l)
        # the structure stage reads the tape SINCE THE BOOK -- NY's session too, for an
        # overnight plan -- so a level NY already broke this afternoon is not new tonight
        print_candidates(*candidates(T, _since_book(T, bars)))
    print()
    if hard:
        print(f"  {hard} structural failure(s). DO NOT WRITE THE COMMENTARY "
              f"until these are resolved.\n")
        return 1
    print("  structurally coherent -- write the commentary.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
