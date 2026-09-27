#!/usr/bin/env python3
"""
setup_track.py — has each setup in the report already played out?

    python setup_track.py                       # today, from trinity.html
    python setup_track.py --date 2026-09-03
    python setup_track.py --sheet sheets/2026-09-03-teddyai.html
    python setup_track.py --replay              # grade every archived sheet

THE REPORT IS INPUT, NEVER OUTPUT. Nothing here writes to trinity.html, build.py
or the narrative. The setups are READ back out of the rendered sheet, so the
authored report stays the single source of truth and the writing workflow does
not change. The cost of that choice is that this parser is coupled to the sheet's
markup -- so it fails LOUDLY (see `parse_setups`) rather than quietly tracking
nothing, which is the only failure mode that would actually mislead you.

IT IS A SCOREBOARD, NOT A SIGNAL. It reports what price has done relative to the
levels you wrote down. It does not decide whether a setup is "good", does not
evaluate the prose trigger ("acceptance below", "a dip that holds"), and does not
tell you to take anything. Those are yours; this only answers "has this already
happened, and in what order".

STATE IS DERIVED FROM ORDERING, NOT FROM TOUCHES. Whether TP1 printed before or
after the stop is the entire difference between +1.4R and -1R, so every level
carries its first-touch time and the outcome falls out of the sequence.
"""

import os, re, sys, json, glob, html, hashlib, argparse, datetime, pathlib

ROOT = pathlib.Path(__file__).parent.resolve()
DATA, SHEETS = ROOT / "data", ROOT / "sheets"
TRACKDIR = DATA / "tracking"

# --------------------------------------------------------------- parsing ---

# The card's opening tag may carry data-* attributes (build.py stamps data-entry / data-sl /
# data-tp on every card for the command center since 2026-09-05); the class is still the anchor.
CARD = re.compile(r'<div class="pl( pri)?"([^>]*)>(.*?)(?=<div class="pl(?: pri)?"[^>]*>|</div>\s*</div>\s*$|$)',
                  re.S)
TITLE = re.compile(r'<h3>(.*?)</h3>', re.S)
TRIG = re.compile(r'<div class="tg">(.*?)</div>', re.S)
CONV = re.compile(r'<div class="cv">(.*?)</div>', re.S)
LEVEL = re.compile(r'<div class="a">(Entry|Stop|TP\d)</div>\s*'
                   r'<div class="b2 mono">([\d,\.]+)</div>', re.S)
# THE DATA ATTRIBUTES ARE THE TRUTH; THE CELLS ARE THE DISPLAY (2026-09-16). The entry
# cell now shows the zone as a range ("29,419-29,451") because Mark asked for entries as
# a range rather than a flat number, and [\d,.]+ cannot read that. build.py has stamped
# data-entry / data-sl / data-tps on every card since 2026-09-05, so the tracker reads
# those and falls back to the cells only for a sheet old enough to lack them. A tracker
# that breaks when a number is reformatted for a reader is a tracker waiting to break.
DATA_ATTR = re.compile(r'data-(entry|sl|tps)="([^"]+)"')
WHY = re.compile(r'<div class="wy">(.*?)</div>', re.S)
TAGS = re.compile(r'<[^>]+>')

# The dashboard mirrors the day's reading and the "why" prose above the chart, so
# some inline markup has to survive -- the bold is where the emphasis lives and
# flattening it to plain text loses the author's stress. Everything outside this
# allowlist is dropped, along with every attribute except class="mono", so a
# malformed sheet can break its own rendering without breaking the dashboard's.
KEEP = {"b", "strong", "em", "i", "br", "code", "span"}
TAG = re.compile(r'</?([a-zA-Z0-9]+)([^>]*)>')


def safe_inline(frag):
    def sub(m):
        name, attrs = m.group(1).lower(), m.group(2)
        if name not in KEEP:
            return ""
        if m.group(0).startswith("</"):
            return f"</{name}>"
        cls = ' class="mono"' if 'mono' in attrs else ""
        return f"<{name}{cls}>"
    out = TAG.sub(sub, frag)
    return re.sub(r"\s+", " ", out).strip()


def _txt(s):
    return html.unescape(TAGS.sub("", s or "")).strip()


def _num(s):
    return float(s.replace(",", ""))


def parse_setups(sheet_html):
    """
    Pull the gameplan cards out of a rendered sheet.

    Deliberately strict. A sheet that yields no setups, or a setup missing an
    entry or a stop, raises -- because the alternative is a tracker that shows a
    tidy empty scoreboard while the report on screen has four live setups on it,
    and you would have no way to tell those two states apart.
    """
    body = sheet_html
    i = body.find('<div class="plans">')
    if i < 0:
        raise ValueError("no <div class=\"plans\"> in this sheet -- markup changed?")
    body = body[i:]
    j = body.find('<div class="sech">', 1)          # next section ends the block
    if j > 0:
        body = body[:j]

    out = []
    for m in CARD.finditer(body):
        primary, attrs, card = bool(m.group(1)), m.group(2), m.group(3)
        # data-exec="close" (build.py stamps it from a card's `exec` key): the card is executed
        # on 5-minute closes and graded by track_close, not by the touch tracker below.
        exec_mode = "close" if 'data-exec="close"' in attrs else "touch"
        t = TITLE.search(card)
        if not t:
            continue
        title = _txt(t.group(1))
        # "A · Above the flip cluster" -> id "A", the rest is the name
        sid, _, name = title.partition("·")
        sid, name = sid.strip(), (name.strip() or title)

        lv = {k: _num(v) for k, v in LEVEL.findall(card)}
        dat = dict(DATA_ATTR.findall(attrs or ""))
        entry = float(dat["entry"]) if "entry" in dat else lv.get("Entry")
        stop = float(dat["sl"]) if "sl" in dat else lv.get("Stop")
        if entry is None or stop is None:
            raise ValueError(f"setup {sid!r} has no Entry/Stop -- markup changed?")
        if "tps" in dat:
            tps = [float(x) for x in dat["tps"].split(",") if x.strip()]
        else:
            tps = [lv[k] for k in sorted(k for k in lv if k.startswith("TP"))]
        # Direction is not written down anywhere, but it is unambiguous from the
        # geometry: targets sit beyond the entry, the stop sits behind it.
        side = "long" if (tps and tps[0] > entry) or stop < entry else "short"

        w = WHY.search(card)
        out.append(dict(id=sid, name=name, side=side,
                        priority="primary" if primary else "secondary",
                        trigger=_txt(TRIG.search(card).group(1)) if TRIG.search(card) else "",
                        why=safe_inline(w.group(1)) if w else "",
                        entry=entry, stop=stop, tps=tps,
                        risk=round(abs(entry - stop), 2), exec=exec_mode))
    if not out:
        raise ValueError("the plans block parsed to zero setups -- markup changed?")
    return out


def parse_reading(sheet_html):
    """
    The "Reading the day" bullets, as written, for mirroring above the chart.

    Read out of the sheet for the same reason the setups are: the words stay
    authored in one place. If the section is renamed or restructured this returns
    nothing and the strip simply does not appear -- a missing summary is a
    cosmetic loss, unlike a missing setup, so this one does not raise.
    """
    i = sheet_html.find("Reading the day")
    if i < 0:
        return []
    seg = sheet_html[i:]
    j = seg.find('<div class="sech">', 1)          # next section ends it
    if j > 0:
        seg = seg[:j]
    ul = re.search(r'<ul class="t">(.*?)</ul>', seg, re.S)
    if not ul:
        return []
    return [safe_inline(m) for m in re.findall(r'<li>(.*?)</li>', ul.group(1), re.S)]


_SHEET_CACHE = {}


def read_sheet(path):
    """
    Parsed setups + reading for a sheet, memoised on (path, mtime, size).

    The dashboard polls this several times a minute. Re-running the regexes over
    44 KB of HTML every time was most of the cost of a refresh, and the sheet
    only changes when a report is built -- so the parse is keyed on the file's
    own stamp and re-runs exactly when the file does. A rebuild changes mtime and
    the cache misses on the next call, which is the behaviour you want: no
    invalidation to remember, and no way to serve setups from a sheet that is no
    longer on disk.
    """
    p = pathlib.Path(path)
    st = p.stat()
    key = (str(p), st.st_mtime_ns, st.st_size)
    hit = _SHEET_CACHE.get(key)
    if hit is None:
        doc = p.read_text(encoding="utf-8")
        hit = (parse_setups(doc), parse_reading(doc))
        _SHEET_CACHE.clear()          # only ever one sheet in play; do not grow
        _SHEET_CACHE[key] = hit
    return hit


def fingerprint(setups):
    """Identity of this set of levels, so a rebuilt report is noticed."""
    key = json.dumps([[s["id"], s["entry"], s["stop"], s["tps"]] for s in setups],
                     sort_keys=True)
    return hashlib.sha1(key.encode()).hexdigest()[:12]


# -------------------------------------------------------------- tracking ---

def track_close(setup, bars):
    """
    The card executed on 5-MINUTE CLOSES (data-exec="close") -- the reactionary rule measured
    on 2026-09-15 across 355 Asia cards for CONTINUATION shapes: 73% win at the same
    expectancy as the touch entry, and "invalidated, then ran anyway" down from 54% to 38%.
    The same rule tested worse for fades (-0.02R against +0.15R: you buy the bounce, not the
    level, and a fade that then fails gets gapped), so it is stamped on break / acceptance
    cards only; a fade stays a touch card.

      trigger   a 5-minute close beyond the LEVEL by more than the risk, then a RETEST (a
                1-minute bar back within the risk of the level), then a 5-minute close beyond
                again -- the fill is that close, wherever it printed. A close back inside
                before the re-break means the retest failed: no fill.
      stop      a 5-minute close back on the near side of the level by more than the risk;
                the exit is that close, which can be worse than the printed stop, and the
                detail says by how much in R -- close-based exits gap (mean loss -1.14R on
                continuation in the measurement) and that cost belongs on the record.
      targets   touched intrabar, as for touch cards. On a bucket's last bar the close-back
                is checked before the targets: a tie resolves against the card.

    The LEVEL is the strike the card leans on -- the entry rounded to the 25-point rung --
    and "beyond" is the card's own direction: a long acceptance is beyond ABOVE the level, a
    short break beyond BELOW it. Returns the same dict shape as track().
    """
    entry_px, stop_px, targets, direction = (setup[k] for k in ("entry", "stop", "tps", "side"))
    risk = abs(entry_px - stop_px) or 10.0
    level = round(entry_px / 25.0) * 25.0
    long_ = direction == "long"
    beyond = (lambda c: (c - level) > risk) if long_ else (lambda c: (level - c) > risk)
    back = (lambda c: (level - c) > risk) if long_ else (lambda c: (c - level) > risk)
    n = len(bars)

    def bucket(i):
        return (bars[i]["ts"][:10], int(bars[i]["ts"][11:13]), int(bars[i]["ts"][14:16]) // 5)
    ends = [i + 1 == n or bucket(i + 1) != bucket(i) for i in range(n)]

    hit, fill, fill_i, exit_px = {}, None, None, None
    b1 = next((i for i in range(n) if ends[i] and beyond(bars[i]["c"])), None)
    if b1 is not None:
        r0 = next((i for i in range(b1 + 1, n)
                   if bars[i]["l"] <= level + risk and bars[i]["h"] >= level - risk), None)
        if r0 is not None:
            for i in range(r0, n):
                if ends[i]:
                    c = bars[i]["c"]
                    if back(c):
                        hit["retest_failed"] = bars[i]["ts"]
                        break
                    if beyond(c):
                        fill, fill_i, hit["entry"] = c, i, bars[i]["ts"]
                        break
    done = bool(bars) and (int(bars[-1]["ts"][11:13]), int(bars[-1]["ts"][14:16])) >= (19, 59)
    if fill is None:
        state = "expired" if done else "not in play yet"
        if "retest_failed" in hit:
            detail = (f"closed beyond {level:,.0f}, then the retest failed at "
                      f"{hit['retest_failed'][11:16]} UTC -- no fill")
        elif b1 is None:
            detail = f"no 5-minute close beyond {level:,.0f}" + ("" if done else " yet")
        else:
            detail = f"closed beyond {level:,.0f} at {bars[b1]['ts'][11:16]} UTC, waiting for the retest"
    else:
        for i in range(fill_i + 1, n):
            b = bars[i]
            if ends[i] and back(b["c"]):
                hit["stop"], exit_px = b["ts"], b["c"]
                break
            for k, tp in enumerate(targets, 1):
                if f"tp{k}" not in hit and b["l"] <= tp <= b["h"]:
                    hit[f"tp{k}"] = b["ts"]
        best = max((k for k in range(len(targets), 0, -1) if f"tp{k}" in hit), default=0)
        sgn = 1 if long_ else -1
        if best:
            state = f"tp{best} hit"
            detail = (f"filled {fill:,.2f} on the close at {hit['entry'][11:16]}, "
                      f"TP{best} {hit[f'tp{best}'][11:16]} UTC")
            if "stop" in hit:
                detail += f", closed back later at {hit['stop'][11:16]}"
        elif "stop" in hit:
            state = "stopped"
            realised = sgn * (exit_px - fill) / risk
            detail = (f"filled {fill:,.2f} on the close at {hit['entry'][11:16]}, closed back at "
                      f"{hit['stop'][11:16]} UTC -- exit {exit_px:,.2f}, {realised:+.2f}R against "
                      f"the printed stop")
        else:
            state = "in play"
            detail = f"filled {fill:,.2f} on the close at {hit['entry'][11:16]} UTC, no TP or close-back yet"
    mfe = mae = None
    if fill is not None and bars[fill_i + 1:]:
        seg = bars[fill_i + 1:]
        hi, lo = max(b["h"] for b in seg), min(b["l"] for b in seg)
        mfe = round((hi - fill) if long_ else (fill - lo), 2)
        mae = round((fill - lo) if long_ else (hi - fill), 2)
    return dict(id=setup["id"], name=setup["name"], side=direction,
                priority=setup["priority"], trigger=setup["trigger"],
                entry=entry_px, stop=stop_px, tps=targets, risk=setup["risk"],
                state=state, detail=detail, touches=hit, mfe=mfe, mae=mae,
                r_mfe=round(mfe / setup["risk"], 2) if mfe and setup["risk"] else None,
                exec="close")


def track(setup, bars):
    """
    Walk the session once and record when each level was FIRST touched.

    A bar touches a level if the level lies within [low, high] -- these are
    1-minute bars, so anything finer is invented precision. Order of touches
    within a single bar is unknowable, and where that ambiguity decides the
    outcome the result says so rather than picking the flattering one.
    """
    if setup.get("exec") == "close":           # a continuation card executed on 5-minute closes
        return track_close(setup, bars)
    e, s, tps, side = setup["entry"], setup["stop"], setup["tps"], setup["side"]
    hit = {}                                   # level name -> (ts, bar index)

    # How close price must come to the entry before the setup counts as having
    # been live at all. One stop-width: near enough that you would have been
    # watching it, and it is a number the setup already carries rather than one
    # invented here.
    #
    # Without this gate "missed" fires on setups price never went near. On the
    # 9/3 sheet, D was a fade from 29,590 whose third target sat at 29,291 --
    # which is roughly where price OPENED. Price oscillating there at 14:01,
    # three hundred points below the entry, was being reported as "the move
    # happened without a fill". The move never happened; the target merely
    # happened to sit where price already was.
    APPROACH = abs(e - s) or 25.0
    approached = False

    def touched(bar, level):
        return bar["l"] <= level <= bar["h"]

    def near_entry(bar):
        if bar["l"] <= e <= bar["h"]:
            return True
        return min(abs(bar["l"] - e), abs(bar["h"] - e)) <= APPROACH

    # THERE IS NO GEOMETRIC "INVALIDATED" STATE, and assuming one was a bug worth
    # recording. Every setup on the sheet is approached from the stop side: A is
    # a dip down to 29,470 with its stop below at 29,432, C is acceptance up
    # through 29,600 with its stop below at 29,562. Price sitting beyond the stop
    # before the entry prints is the STARTING CONDITION, not a failure -- a rule
    # that called it invalidation marked all four dead at the opening bell.
    #
    # Real invalidation lives in the prose ("acceptance above 29,600"), which is
    # a judgement, and it stays yours. What IS derivable, and worth knowing, is
    # whether the move happened without you: targets reached, entry never filled.
    trig_i = None
    for i, b in enumerate(bars):
        if trig_i is None:
            if touched(b, e):
                trig_i, hit["entry"] = i, b["ts"]
                # A bar that reaches the entry AND the stop is genuinely
                # ambiguous at 1-minute resolution. Flag it; do not guess.
                if touched(b, s):
                    hit["ambiguous_entry_bar"] = b["ts"]
            else:
                if not approached:
                    approached = near_entry(b)
                    if not approached:
                        continue
                # Untriggered -- but did the move happen without you? This has to
                # be a CROSSING, not a level test. A is a dip-buy at 29,470 whose
                # targets are 29,525/29,600/29,700, and price opens at 29,629 --
                # already above two of them. A bare "high >= target" called that
                # a missed runner every single time, when in truth price simply
                # started there and the dip never came. So a target only counts
                # as missed if price was on the entry side of it FIRST and then
                # traded through.
                for n, tp in enumerate(tps, 1):
                    below, k = f"under_tp{n}", f"missed_tp{n}"
                    if side == "long":
                        if b["l"] < tp:
                            hit[below] = True
                        elif hit.get(below) and k not in hit and b["h"] >= tp:
                            hit[k] = b["ts"]
                    else:
                        if b["h"] > tp:
                            hit[below] = True
                        elif hit.get(below) and k not in hit and b["l"] <= tp:
                            hit[k] = b["ts"]
            continue
        if "stop" not in hit and touched(b, s):
            hit["stop"] = b["ts"]
        for n, tp in enumerate(tps, 1):
            k = f"tp{n}"
            if k not in hit and touched(b, tp):
                hit[k] = b["ts"]

    # ---- outcome from the ordering ----------------------------------------
    def idx(k):
        if k not in hit:
            return None
        return next(i for i, b in enumerate(bars) if b["ts"] == hit[k])

    # Is the session over? 20:00 UTC is the front-expiry cutoff the whole stack
    # uses. Only then does "never triggered" harden from "not yet" into "expired".
    done = bool(bars) and (int(bars[-1]["ts"][11:13]),
                           int(bars[-1]["ts"][14:16])) >= (19, 59)

    state, detail = "not in play yet", ""
    if trig_i is None:
        missed = [n for n in range(len(tps), 0, -1) if f"missed_tp{n}" in hit]
        if missed:
            state = "missed"
            n = missed[0]
            detail = (f"entry {e:,.0f} never printed, but price reached TP{n} "
                      f"{tps[n-1]:,.0f} at {hit[f'missed_tp{n}'][11:16]} UTC — "
                      f"the move happened without a fill")
        elif done:
            state = "expired"
            detail = (f"session closed, entry {e:,.0f} never reached"
                      + ("" if approached
                         else f" (price never came within {APPROACH:,.0f} pts of it)"))
        elif bars:
            state = "not in play yet"
            detail = (f"entry {e:,.0f} not reached"
                      + ("  — approached" if approached else ""))
        else:
            detail = "no bars for this session yet"
    else:
        # THE STOP WINS A SAME-BAR TIE (2026-09-15). This used to be `ti <= si`,
        # which handed every bar that touched a target AND the stop to the target.
        # At 1-minute resolution that bar's internal order is unknowable, so the
        # rule decided nothing honestly -- it just always chose the flattering
        # reading, which the docstring above already said it must not do. On 66
        # RTH sessions with a 13-point stop and a 24-point TP1 (the 09-15 card
        # geometry) it turned 14.4 percentage points of stop-outs into wins:
        # 53.4% "tp hit" under the old rule, 39.0% under this one. It costs
        # nothing on wider geometry -- at a 22-point stop and a 99-point TP1 no
        # tie fires at all, because the two levels no longer sit inside one bar.
        #
        # A tie is still REPORTED, because "you were stopped" and "you were
        # stopped in the bar that also printed your target" are different facts
        # and the second one is usually telling you the stop is too tight to
        # resolve on this timeframe.
        si = idx("stop")
        best, best_i, tied = 0, None, 0
        for n in range(len(tps), 0, -1):
            ti = idx(f"tp{n}")
            if ti is None:
                continue
            if si is None or ti < si:
                best, best_i = n, ti
                break
            if ti == si and not tied:
                tied = n
        if best:
            state = f"tp{best} hit"
            detail = f"triggered {hit['entry'][11:16]}, TP{best} {hit[f'tp{best}'][11:16]} UTC"
            if si is not None:
                detail += f", stop later at {hit['stop'][11:16]}"
        elif si is not None:
            state = "stopped"
            detail = f"triggered {hit['entry'][11:16]}, stopped {hit['stop'][11:16]} UTC"
            if tied:
                hit["ambiguous_exit_bar"] = hit["stop"]
                detail += (f"  [TP{tied} {tps[tied-1]:,.0f} printed in that same 1m bar"
                           " -- order unknown, scored as the stop]")
        else:
            state = "in play"
            detail = f"triggered {hit['entry'][11:16]} UTC, no TP or stop yet"
    if "ambiguous_entry_bar" in hit:
        detail += "  [entry and stop in the same 1m bar -- order unknown]"

    # ---- excursion --------------------------------------------------------
    mfe = mae = None
    if trig_i is not None:
        seg = bars[trig_i:]
        if seg:
            hi, lo = max(b["h"] for b in seg), min(b["l"] for b in seg)
            mfe = round((hi - e) if setup["side"] == "long" else (e - lo), 2)
            mae = round((e - lo) if setup["side"] == "long" else (hi - e), 2)

    return dict(id=setup["id"], name=setup["name"], side=setup["side"],
                priority=setup["priority"], trigger=setup["trigger"],
                entry=e, stop=s, tps=tps, risk=setup["risk"],
                state=state, detail=detail, touches=hit,
                mfe=mfe, mae=mae,
                r_mfe=round(mfe / setup["risk"], 2) if mfe and setup["risk"] else None)


# ------------------------------------------------------------------ bars ---

# THE WINDOW IS THE SHEET'S OWN (2026-09-15). `load_bars` took `rth_only` and
# every caller but `--full-day` passed True, so an ASIA plan was graded against
# RTH bars -- a window that begins seven hours after the one it was written for
# and shares not a single minute with it. On the 09-15 Asia sheet that is the
# difference between "expired, entry never reached" and a card that triggered at
# 04:21. Intraday sheets had the mirror problem: a plan rebuilt at 08:44 PT was
# graded from 13:30 UTC, an hour and a half before it existed, so it could be
# recorded as triggering on a move that happened before it was written.
#
# The sheet already says which window it is for -- build.py stamps the rail label
# on the page, and on an intraday build it stamps the build clock into it too
# ("Rest of session 0844"). Reading it back from there keeps the rule the rest of
# this module follows: the report is the input, and nothing about it is guessed
# from a filename.
# The rail label itself, not prose: build.py:161 emits it as `<div class="k">{RLABEL}
# range</div>`, so this reads the one element that IS the label. Matching the bare word
# "Asia" anywhere in the document would catch "Asia MAX", "Asia band" and the lead's
# prose on an RTH sheet, and grade a day session against the overnight.
RAIL = re.compile(r'<div class="k">([^<]{0,40}?) range</div>')
# The build clock is in the TITLE, not the rail label: build.py:161 renders the bare
# RLABEL ("Rest of session range") while build.py:997 appends the PT clock to the title
# ("Basilisk 2026-09-14 Rest of session 0844"). Kind from the label, clock from the title;
# both are build.py's own stamps, neither is inferred from the filename.
CLOCK = re.compile(r'<title>[^<]*Rest of session\s+(\d{4})\s*</title>')
PT = "America/Los_Angeles"


def parse_window(sheet_html):
    """
    -> (kind, start, end) in UTC hours/minutes, from the sheet's own rail label.

      asia      the plan runs to the RTH open; 00:00 -> 13:30 UTC
      intraday  from the build clock (stamped PT) to the front-expiry cutoff
      rth       13:30 -> 20:00 UTC, the label the 1D sheets carry

    An intraday clock is PT because that is what build.py writes, and it is
    converted through the zone rather than a fixed -7 so a November sheet is not
    read an hour early.
    """
    m = RAIL.search(sheet_html or "")
    label = html.unescape(m.group(1)).strip() if m else "1D"
    # the same three-way split build.py's own KIND makes off this label
    if label.startswith("London"):
        # 07:00-13:30 UTC, the window session_scale.scale_london measures. Asia sits
        # BEHIND it, which is why a London sheet's cards are graded against ETH.
        return "london", (7, 0), (13, 30)
    if label.startswith(("Asia", "Rest of overnight")):
        return "asia", (0, 0), (13, 30)
    if label.startswith("Rest of session"):
        clock = CLOCK.search(sheet_html or "")
        if not clock:
            return "intraday", (13, 30), (20, 0)  # no clock stamped: grade the whole day session
        hh, mm = int(clock.group(1)[:2]), int(clock.group(1)[2:])
        return "intraday", (hh, mm), (20, 0)      # PT; converted in load_bars
    return "rth", (13, 30), (20, 0)


def _pt_to_utc(day, hh, mm):
    import datetime, zoneinfo
    d = datetime.date.fromisoformat(day)
    t = datetime.datetime(d.year, d.month, d.day, hh, mm,
                          tzinfo=zoneinfo.ZoneInfo(PT)).astimezone(datetime.timezone.utc)
    return (t.hour, t.minute)


def load_bars(day, window=None, rth_only=None):
    """Bars for `day`, clipped to `window` -- (kind, start, end) from parse_window.

    `rth_only` is kept for callers that predate the window: True is the old RTH
    clip, False the whole UTC day."""
    f = DATA / "bars" / f"{day}.json"
    if not f.exists():
        return []
    rows = json.loads(f.read_text(encoding="utf-8"))
    if window is None:
        if rth_only is False:
            return rows
        window = ("rth", (13, 30), (20, 0))
    kind, start, end = window
    if kind == "intraday" and start != (13, 30):
        start = _pt_to_utc(day, *start)
        if start < (13, 30):
            start = (13, 30)                      # a premarket rebuild still grades from the open
    out = []
    for r in rows:
        hh, mm = int(r["ts"][11:13]), int(r["ts"][14:16])
        if start <= (hh, mm) <= end:
            out.append(r)
    return out


# ------------------------------------------------------------------- run ---

def run(sheet, day, rth_only=True, quiet=False):
    doc = pathlib.Path(sheet).read_text(encoding="utf-8")
    setups = parse_setups(doc)
    window = parse_window(doc)
    bars = load_bars(day, None if not rth_only else window, rth_only)
    rows = [track(s, bars) for s in setups]

    out = dict(date=day, sheet=str(pathlib.Path(sheet).name),
               fingerprint=fingerprint(setups),
               bars=len(bars),
               first=bars[0]["ts"][11:16] if bars else None,
               last=bars[-1]["ts"][11:16] if bars else None,
               last_px=bars[-1]["c"] if bars else None,
               created=datetime.datetime.now(datetime.timezone.utc)
                       .isoformat(timespec="seconds"),
               setups=rows)

    TRACKDIR.mkdir(parents=True, exist_ok=True)
    (TRACKDIR / f"{day}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")

    if not quiet:
        print(f"\n  {out['sheet']}   {day}   {out['bars']} bars"
              + (f"   {out['first']}-{out['last']} UTC   last {out['last_px']:,.2f}"
                 if bars else "   NO BARS"))
        print(f"  setups fingerprint {out['fingerprint']}")
        print("  " + "-" * 74)
        for r in rows:
            print(f"  {r['id']}  {r['state']:<16} {r['name'][:34]:<34} "
                  f"{r['side']:<5} E {r['entry']:>9,.0f}  S {r['stop']:>9,.0f}")
            if r["detail"]:
                print(f"       {r['detail']}")
            if r["mfe"] is not None:
                print(f"       ran {r['mfe']:+,.0f} for / {r['mae']:+,.0f} against"
                      + (f"  ({r['r_mfe']}R best)" if r["r_mfe"] else ""))
        print()
    return out


def replay():
    """Grade every archived sheet against the bars for its own session."""
    print(f"\n  {'session':<12}{'sheet':<34}" + "".join(f"{c:<12}" for c in "ABCD"))
    print("  " + "-" * 86)
    tally = {}
    for p in sorted(SHEETS.glob("*-teddyai.html")):
        day = p.name[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            continue
        if not (DATA / "bars" / f"{day}.json").exists():
            continue
        try:
            o = run(p, day, quiet=True)
        except Exception as ex:
            print(f"  {day:<12}{p.name[:32]:<34}{type(ex).__name__}: {ex}")
            continue
        cells = {r["id"]: r["state"] for r in o["setups"]}
        print(f"  {day:<12}{p.name[:32]:<34}"
              + "".join(f"{cells.get(c,'-')[:11]:<12}" for c in "ABCD"))
        for r in o["setups"]:
            tally[r["state"]] = tally.get(r["state"], 0) + 1
    print("\n  totals: " + "   ".join(f"{k} {v}" for k, v in sorted(tally.items())) + "\n")


def main():
    ap = argparse.ArgumentParser(description="track whether report setups have played out")
    ap.add_argument("--sheet", default=None, help="sheet to read (default trinity.html)")
    ap.add_argument("--date", default=None, help="session whose bars to walk")
    ap.add_argument("--full-day", action="store_true", help="walk the whole UTC day, not just RTH")
    ap.add_argument("--replay", action="store_true", help="grade every archived sheet")
    a = ap.parse_args()

    if a.replay:
        return replay() or 0

    sheet = a.sheet or (ROOT / "trinity.html")
    day = a.date
    if not day:
        try:
            day = json.loads((ROOT / "trinity.json").read_text())["report_date"]
        except Exception:
            day = datetime.date.today().isoformat()
    try:
        run(sheet, day, rth_only=not a.full_day)
    except Exception as ex:
        print(f"\n  FAILED: {type(ex).__name__}: {ex}\n"
              f"  The sheet's markup is what this reads. If it changed, this parser\n"
              f"  needs updating -- it refuses to report an empty scoreboard.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
