"""precommentary: the structural check that runs before any commentary is written.

Mark, 2026-09-15: "I dont want any ABSURD or outlandish cards being outputted moving
forward... If price traded through something ask yourself does it make sense????? If we
opened up nowhere near a setup does it make it valid still???"
"""
import sys, pathlib, importlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import precommentary as PC


def setup_function():
    PC.rows.clear()


def levels(hi=29500.0, lo=29300.0, med=15.0, rng=None):
    return dict(open=29450.0, hi=hi, lo=lo, last=29350.0,
                rng=rng if rng is not None else hi - lo, med_bar=med)


def bars(n=60, lo=29300.0, hi=29500.0):
    return [{"ts": f"2026-09-15 0{i // 60}:{i % 60:02d}:00+00:00",
             "o": 29400.0, "h": hi, "l": lo, "c": 29350.0} for i in range(n)]


# --- band vs clock ---------------------------------------------------------

def test_the_2026_09_15_band_clock_mismatch_fails():
    """The London build: the overnight curve's window had 1.5h left on a 14.47h clock."""
    PC.check_band_vs_clock({"rail_scale": 0.140, "hours": 14.47, "rail_label": "Rest of overnight"})
    lvl, _, msg = PC.rows[0]
    assert lvl == PC.HARD and "ENDS LONG BEFORE THE PLAN" in msg


def test_an_asia_plan_at_its_scheduled_time_passes():
    """17:15 PT: 4.25h after the settle, 6.75h of the overnight window left on a 19.75h clock."""
    PC.check_band_vs_clock({"rail_scale": 0.324, "hours": 19.75, "rail_label": "Asia"})
    assert PC.rows[0][0] == PC.OK, PC.rows[0][2]


def test_a_london_plan_built_before_its_open_passes():
    PC.check_band_vs_clock({"rail_scale": 0.346, "hours": 14.03, "rail_label": "London"})
    assert PC.rows[0][0] == PC.OK, PC.rows[0][2]


def test_a_full_session_plan_draws_the_full_day_rail():
    PC.check_band_vs_clock({"rail_scale": 1.0, "hours": 6.5, "rail_label": "1D"})
    assert PC.rows[0][0] == PC.OK
    PC.rows.clear()
    PC.check_band_vs_clock({"rail_scale": 0.628, "hours": 6.5, "rail_label": "1D"})
    assert PC.rows[0][0] == PC.HARD


def test_an_intraday_plan_is_judged_against_the_measured_intraday_rail():
    import session_scale as SS
    ok_scale = SS.scale_for_hours(3.0)
    PC.check_band_vs_clock({"rail_scale": ok_scale, "hours": 3.0, "rail_label": "Rest of session"})
    assert PC.rows[0][0] == PC.OK, PC.rows[0][2]
    PC.rows.clear()
    PC.check_band_vs_clock({"rail_scale": 0.08, "hours": 0.10, "rail_label": "Rest of session"})
    assert PC.rows[0][0] == PC.HARD and "HALF AN HOUR" in PC.rows[0][2]


def test_a_holiday_plan_is_not_band_checked():
    PC.check_band_vs_clock({"rail_scale": 0.55, "hours": 12.0, "rail_label": "Globex holiday"})
    assert PC.rows[0][0] == PC.SOFT


# --- the reference ---------------------------------------------------------

def test_a_reference_the_tape_has_left_behind_fails():
    PC.check_reference({"last_print": 29500.0}, bars())
    assert PC.rows[0][0] == PC.HARD


def test_a_live_reference_passes():
    PC.check_reference({"last_print": 29352.0}, bars())
    assert PC.rows[0][0] == PC.OK


# --- Mark's first question: price traded through it ------------------------

def test_a_hold_at_a_wall_the_tape_closed_beyond_is_a_second_visit_not_absurd(tmp_path):
    """Measured 2026-09-15 on 355 Asia cards: a fade at a broken wall is a coin flip (+0.01R),
    not a loser -- so it is a warning the prose must carry, never a hard failure. The
    earlier hard rule was common sense the record did not back."""
    card = _sheet(tmp_path, "29,350 hold", "long", 29351, 29339, [29375, 29400, 29425])
    T = {"last_print": 29350.0, "em": 253.7, "rail_scale": 0.554}
    since = _day(29280.0, 29330.0)            # 30-minute closes ~29,305: beyond 29,350 and stayed
    PC.check_cards(card, T, levels(lo=29308.5, med=6.2), sess_open=29457.0, since=since)
    card_rows = [(l, m) for l, k, m in PC.rows if k == "card"]
    assert card_rows[0][0] == PC.OK, card_rows[0][1]
    assert any(l == PC.SOFT and "SECOND VISIT" in m for l, m in card_rows)


def test_a_continuation_through_a_wall_the_tape_closed_beyond_and_stayed_is_absurd(tmp_path):
    """The one robust level-state result: 17% win against a 27% coin flip, -0.42R, and it
    held under both touch and close execution."""
    card = _sheet(tmp_path, "29,400 acceptance", "long", 29406, 29392, [29450, 29500, 29550])
    T = {"last_print": 29350.0, "em": 253.7, "rail_scale": 0.554}
    since = _day(29430.0, 29480.0)            # closes ~29,455: above 29,400 and stayed
    PC.check_cards(card, T, levels(hi=29495.25, med=6.2), sess_open=29457.0, since=since)
    assert PC.rows[0][0] == PC.HARD and "chases a move already made" in PC.rows[0][2]


def test_a_wall_the_tape_has_only_touched_is_still_a_card(tmp_path):
    """Touched is not broken -- the tolerance is the card's own risk."""
    card = _sheet(tmp_path, "29,350 hold", "long", 29351, 29339, [29375, 29400, 29425])
    T = {"last_print": 29350.0, "em": 253.7, "rail_scale": 0.554}
    PC.check_cards(card, T, levels(lo=29345.0, med=6.2), sess_open=29360.0)
    assert PC.rows[0][0] == PC.OK


# --- Mark's second question: we opened nowhere near it ---------------------

def test_a_card_the_session_opened_a_band_away_from_and_never_approached_is_absurd(tmp_path):
    card = _sheet(tmp_path, "28,500 retest", "long", 28501, 28489, [28600, 28700, 28800])
    T = {"last_print": 29350.0, "em": 253.7, "rail_scale": 0.554}   # band 140
    PC.check_cards(card, T, levels(hi=29500.0, lo=29300.0, med=6.2), sess_open=29457.0)
    assert PC.rows[0][0] == PC.HARD
    assert "opened" in PC.rows[0][2] and "never come near" in PC.rows[0][2]


def test_a_far_card_price_DID_reach_is_not_flagged_for_opening_away(tmp_path):
    card = _sheet(tmp_path, "29,300 hold", "long", 29301, 29289, [29400, 29450, 29500])
    T = {"last_print": 29350.0, "em": 253.7, "rail_scale": 0.554}
    PC.check_cards(card, T, levels(hi=29500.0, lo=29295.0, med=6.2), sess_open=29457.0)
    assert "never come near" not in PC.rows[0][2]


# --- the absurdity floor ---------------------------------------------------

def test_a_stop_inside_one_bars_noise_is_a_note(tmp_path):
    """2026-09-16, f26c027: "A stop inside one bar is a note, not a veto."

    It says the card is a scalp at this fill, which is a thing to know and not a thing
    to refuse. Since the generator anchors stops on the tape's own swings, a stop inside
    one bar is now an ordinary output rather than a mistake.
    """
    card = _sheet(tmp_path, "29,350 hold", "long", 29351, 29339, [29375, 29400, 29425])
    T = {"last_print": 29350.0, "em": 253.7, "rail_scale": 0.554}
    PC.check_cards(card, T, levels(lo=29345.0, med=17.2))      # RTH median bar
    assert PC.rows[0][0] == PC.OK
    assert any(r[0] == PC.SOFT and "inside one bar's noise" in r[2] for r in PC.rows)


def test_a_long_whose_stop_sits_above_its_entry_is_absurd(tmp_path):
    """The shape a roll-week mistranslation produces on a live page."""
    card = _sheet(tmp_path, "29,397 hold", "long", 29106, 29359, [29200, 29400, 29419])
    T = {"last_print": 29350.0, "em": 253.7, "rail_scale": 0.554}
    PC.check_cards(card, T, levels(med=6.2))
    assert PC.rows[0][0] == PC.HARD


def test_the_floor_is_the_tapes_own_not_a_constant(tmp_path):
    """A 12-point stop is ordinary overnight and a scalp in RTH.

    The threshold is the tape's own median bar, not a constant, which is the point of
    the test. Since f26c027 the RTH case raises a note rather than a failure, so what
    distinguishes the two tapes is whether the note appears at all.
    """
    card = _sheet(tmp_path, "29,350 hold", "long", 29351, 29339, [29375, 29400, 29425])
    T = {"last_print": 29350.0, "em": 253.7, "rail_scale": 0.554}
    noise = lambda: [r for r in PC.rows if "inside one bar's noise" in r[2]]
    PC.check_cards(card, T, levels(lo=29345.0, med=6.2))
    quiet = noise()
    PC.rows.clear()
    PC.check_cards(card, T, levels(lo=29345.0, med=17.2))
    loud = noise()
    assert not quiet
    assert loud and loud[0][0] == PC.SOFT


# --- Mark's third question: ETH already triggered it -----------------------

def _rth_sheet(tmp, name, side, entry, stop, tps):
    """A sheet whose rail label makes it an RTH plan, so ETH sits behind its window."""
    s = pathlib.Path(_sheet(tmp, name, side, entry, stop, tps))
    s.write_text('<title>Basilisk 2026-09-15</title>'
                 '<div class="k">1D range</div>' + s.read_text(encoding="utf-8"), encoding="utf-8")
    return str(s)


def _day(lo, hi, upto=(20, 0)):
    """One bar a minute across the UTC day, so an entry inside [lo, hi] prints."""
    out = []
    for h in range(0, upto[0] + 1):
        for m in (0, 15, 30, 45):
            out.append({"ts": f"2026-09-15 {h:02d}:{m:02d}:00+00:00",
                        "o": (lo + hi) / 2, "h": hi, "l": lo, "c": (lo + hi) / 2})
    return out


def test_an_rth_card_price_has_LEFT_is_a_hard_failure(tmp_path):
    card = _rth_sheet(tmp_path, "29,400 rejection", "short", 29399, 29411, [29375, 29350, 29300])
    T = {"last_print": 29150.0, "em": 253.7, "rail_scale": 0.346}   # price 249 pts away
    PC.check_eth_already_triggered(card, T, _day(29308.5, 29495.25))
    flagged = [r for r in PC.rows if r[1] == "ETH card"]
    assert flagged and flagged[0][0] == PC.HARD
    assert "price has left it" in flagged[0][2]


def test_a_card_at_a_level_price_is_STILL_ON_is_a_second_visit_not_a_veto(tmp_path):
    """A London plan has the whole Asia session behind it; the level having traded
    overnight is why the card is a second visit, not a reason to withhold it."""
    card = _rth_sheet(tmp_path, "29,400 acceptance", "long", 29401, 29387, [29425, 29450, 29500])
    T = {"last_print": 29382.25, "em": 253.7, "rail_scale": 0.346}  # price 19 pts away
    PC.check_eth_already_triggered(card, T, _day(29308.5, 29495.25))
    flagged = [r for r in PC.rows if r[1] == "ETH card"]
    assert flagged and flagged[0][0] == PC.SOFT
    assert "SECOND VISIT" in flagged[0][2]


def test_an_rth_card_ETH_never_reached_is_not_flagged(tmp_path):
    card = _rth_sheet(tmp_path, "29,700 rejection", "short", 29699, 29711, [29650, 29600, 29550])
    T = {"last_print": 29382.25, "em": 253.7, "rail_scale": 0.346}
    PC.check_eth_already_triggered(card, T, _day(29308.5, 29495.25))
    assert not [r for r in PC.rows if r[1] == "ETH card"]


def test_an_overnight_plan_has_no_eth_behind_it(tmp_path):
    card = _sheet(tmp_path, "29,350 hold", "long", 29351, 29339, [29375, 29400, 29425])
    p = pathlib.Path(card)
    p.write_text('<title>Basilisk 2026-09-15 Asia</title><div class="k">Asia range</div>'
                 + p.read_text(encoding="utf-8"), encoding="utf-8")
    PC.check_eth_already_triggered(card, {"last_print": 29382.25, "em": 253.7, "rail_scale": 0.346},
                                   _day(29308.5, 29495.25))
    assert PC.rows[0][1] == "ETH" and "no overnight sits behind" in PC.rows[0][2]
    assert not [r for r in PC.rows if r[1] == "ETH card"]


def _sheet(tmp, name, side, entry, stop, tps):
    """Minimal markup setup_track.parse_setups will read."""
    rows = f'<div class="a">Entry</div>\n<div class="b2 mono">{entry:,}</div>\n' \
           f'<div class="a">Stop</div>\n<div class="b2 mono">{stop:,}</div>\n' \
           + "".join(f'<div class="a">TP{i}</div>\n<div class="b2 mono">{t:,}</div>\n'
                     for i, t in enumerate(tps, 1))
    p = tmp / "sheet.html"
    p.write_text('<div class="plans">\n'
                 f'<div class="pl pri" data-side="{side}"><div class="ph"><div><h3>{name}</h3>\n'
                 f'<div class="tg">t</div></div><div class="cv">Primary</div></div>\n'
                 f'<div class="lv">\n{rows}</div>\n<div class="wy">w</div>\n</div>\n</div>\n'
                 '<div class="sech">next</div>', encoding="utf-8")
    return str(p)


# --- the shape decides which question applies ------------------------------

def test_a_break_short_below_a_broken_wall_is_the_setup_not_an_absurdity(tmp_path):
    """The first version flagged a 29,300 break for sitting under a level the session
    had traded through -- the only condition under which that card exists."""
    card = _sheet(tmp_path, "29,300 break", "short", 29287, 29301, [29250, 29200, 29150])
    T = {"last_print": 29343.5, "em": 253.7, "rail_scale": 0.346}
    PC.check_cards(card, T, levels(hi=29495.25, lo=29308.5, med=6.2), sess_open=29457.25)
    assert PC.rows[0][0] == PC.OK, PC.rows[0][2]


def test_a_rejection_at_a_wall_the_tape_closed_above_is_a_second_visit(tmp_path):
    """A fade at a wall the tape closed beyond: noted as a second visit, not refused."""
    card = _sheet(tmp_path, "29,400 rejection", "short", 29399, 29413, [29350, 29300, 29250])
    T = {"last_print": 29343.5, "em": 253.7, "rail_scale": 0.346}
    since = _day(29430.0, 29480.0)
    PC.check_cards(card, T, levels(hi=29495.25, lo=29308.5, med=6.2), sess_open=29457.25, since=since)
    card_rows = [(l, m) for l, k, m in PC.rows if k == "card"]
    assert card_rows[0][0] == PC.OK, card_rows[0][1]
    assert any(l == PC.SOFT and "SECOND VISIT" in m for l, m in card_rows)


def test_an_acceptance_long_above_a_reclaimed_wall_is_not_flagged(tmp_path):
    card = _sheet(tmp_path, "29,400 acceptance", "long", 29406, 29392, [29450, 29500, 29550])
    T = {"last_print": 29343.5, "em": 253.7, "rail_scale": 0.346}
    PC.check_cards(card, T, levels(hi=29495.25, lo=29308.5, med=6.2), sess_open=29457.25)
    assert "wall it leans on" not in PC.rows[0][2]


# --- interpret structure: the candidates stage -----------------------------

def _book(ref=29402.5, em=253.7, scale=0.346, gamma=None):
    g = gamma or {"29500": 158, "29450": -39, "29400": -82, "29300": -79, "29375": -11}
    return {"last_print": ref, "em": em, "rail_scale": scale,
            "panels": {"NEAR": {"gamma": g}, "0DTE": {"gamma": g}, "FULL": {"gamma": g}}}


def _tape(lo=29308.5, hi=29495.25, n=40):
    """Ordinary 8-point bars that happen to reach `lo` and `hi` once each -- the extremes
    have to come from two bars, not from every bar, or the median bar range IS the session
    range and every stop reads as inside one bar's noise."""
    mid = (lo + hi) / 2
    out = [{"ts": f"2026-09-15 0{i//20}:{(i*3)%60:02d}:00+00:00",
            "o": mid, "h": mid + 4, "l": mid - 4, "c": mid} for i in range(n)]
    out[1].update(h=hi, l=hi - 8, o=hi - 4, c=hi - 4)
    out[-2].update(h=lo + 8, l=lo, o=lo + 4, c=lo + 4)
    return out


def test_candidates_flags_what_is_out_of_reach_instead_of_dropping_it():
    """2026-09-16: a level past half the drawn band is a WATCH, not a deletion. It
    stays on the roster, carries the flag, and says how far out it is."""
    rows, ref, band = PC.candidates(_book(), _tape())
    far = [r for r in rows if r["strike"] == 29500][0]
    assert far["reach"] == "watch"
    assert all(s["reach"] == "watch" for s in far["sides"])
    assert any("bands out" in s["note"] for s in far["sides"])
    assert "bands out" not in " ".join(s["why"] for s in far["sides"])


def _closed_below(L=29400.0, n=48):
    """Narrow bars whose 30-minute closes all sit beyond L (below it) and stay there:
    the tape ACCEPTED the break, not just wicked it."""
    return [{"ts": f"2026-09-15 {i // 4:02d}:{(i % 4) * 15:02d}:00+00:00",
             "o": L - 30, "h": L - 28, "l": L - 32, "c": L - 30} for i in range(n)]


def test_candidates_disqualifies_a_continuation_through_a_wall_closed_beyond_and_keeps_the_fade():
    """The measured policy (2026-09-15): continuation through a wall the tape closed beyond
    and stayed loses (robust); a fade there is a second visit -- noted, not disqualified."""
    rows, ref, band = PC.candidates(_book(), _closed_below())
    at = [r for r in rows if r["strike"] == 29400][0]
    assert at["state"] == "broken"
    hold = [s for s in at["sides"] if s["shape"] == "hold"][0]
    brk = [s for s in at["sides"] if s["shape"] == "break"][0]
    # `ok` now folds in LOCATION as well as state (2026-09-16) and these synthetic bars put
    # the level wherever they put it. What this guards is the state policy: the continuation
    # is disqualified outright, the fade survives it and carries the second-visit note.
    assert hold["grade"] != "none" and "second visit" in hold["note"], hold["why"]
    assert not brk["ok"] and "closed beyond" in brk["why"]


def test_candidates_treats_a_wick_as_held():
    """Closes on the near side with the extremes on two bars: wicked, and every card stands."""
    rows, ref, band = PC.candidates(_book(), _tape())
    at = [r for r in rows if r["strike"] == 29400][0]
    assert at["state"] == "wicked"
    assert all(s["grade"] != "none" for s in at["sides"]), [s["why"] for s in at["sides"]]
    assert all(not s["why"] for s in at["sides"]), [s["why"] for s in at["sides"]]


def test_candidates_cover_both_sides_of_price():
    """The roster is the day's map, so a level above the reference is on it whether or
    not price is near it. Before 2026-09-16 the shrinking band left whole sides of the
    book uncarded -- on that day the sheet read NOTHING BELOW while 29,400 and 29,350
    sat 100 and 150 points away on the board."""
    rows, ref, band = PC.candidates(_book(), _tape())
    # The ROSTER covers both sides -- that is the day's map. Which of them is a card right
    # now is a location question and moves with price; the map does not.
    writable = [(r, s) for r in rows for s in r["sides"] if s["grade"] != "none"]
    assert writable
    assert [r for r, s in writable if r["dist"] > 0], "nothing above the reference"
    assert [r for r, s in writable if r["dist"] < 0], "nothing below the reference"


def test_candidates_needs_a_reference_and_a_band():
    assert PC.candidates({}, [])[0] == []
    assert PC.candidates({"last_print": 29400.0}, [])[0] == []


def test_candidates_reach_follows_the_cap_it_is_given():
    wide, _, _ = PC.candidates(_book(), _tape(), reach_frac=1.0)
    tight, _, _ = PC.candidates(_book(), _tape(), reach_frac=0.2)
    n = lambda rs: sum(1 for r in rs for s in r["sides"] if s["ok"])
    assert n(wide) >= n(tight)


# ----------------------------------------------------------------------------------
# Location: where an entry sits decides whether it is a card (2026-09-16)
# ----------------------------------------------------------------------------------
def test_where_in_range_separates_the_edge_from_the_far_side():
    """A long AT the ceiling is buying resistance; a long ABOVE it is a breakout --
    Mark's own 29,300 trade. Collapsing the two would refuse the breakout."""
    import swings
    bars = ([{"ts": f"2026-09-16 0{i//60}:{i%60:02d}:00+00:00", "o": 100.0, "h": 100.0,
              "l": 100.0, "c": 100.0} for i in range(50)])
    for i, (lo, hi) in enumerate([(90.0, 95.0)] * 50):
        pass
    # a floor at 100 then a ceiling at 200, with enough padding either side
    b = []
    def bar(t, l, h): b.append({"ts": f"2026-09-16 {t//60:02d}:{t%60:02d}:00+00:00",
                               "o": (l+h)/2, "h": h, "l": l, "c": (l+h)/2})
    for t in range(0, 45): bar(t, 150.0, 210.0)
    for t in range(45, 90): bar(t, 100.0, 210.0)      # the floor
    for t in range(90, 180): bar(t, 150.0, 210.0)
    for t in range(180, 225): bar(t, 150.0, 300.0)    # the ceiling
    for t in range(225, 300): bar(t, 150.0, 210.0)
    w = swings.where_in_range(b, 100.0)
    assert w["where"] in ("floor", "below")
    assert swings.where_in_range(b, 400.0)["where"] == "above"
    assert swings.where_in_range(b, 50.0)["where"] == "below"


def test_candidates_give_every_side_an_entry_zone():
    """Mark: 'I want entries to be shown as a range as well rather than a flat number
    described basically as fact.'"""
    rows, ref, band = PC.candidates(_book(), _tape())
    sides = [s for r in rows for s in r["sides"]]
    assert sides
    for s in sides:
        assert s["entry_lo"] <= s["entry"] <= s["entry_hi"], s
        assert s["entry_hi"] > s["entry_lo"], s


def test_candidates_carry_the_tape_s_directional_read():
    rows, ref, band = PC.candidates(_book(), _tape())
    assert rows and rows[0]["read"]["bias"] in ("up", "down", "none")
    assert rows[0]["read"]["why"]
