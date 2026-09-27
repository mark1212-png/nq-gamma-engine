import os, sys, json, datetime, pathlib
# Windows defaults text files to cp1252, which cannot encode the arrows,
# box-drawing glyphs, sigma or minus signs this page is built from -- so
# every read and write here is pinned to utf-8. Leaving it to the platform
# meant build.py worked on Linux and died on Windows the first time the
# staleness banner (which contains a right-arrow) actually fired.
lad = open('ladder_frag.html', encoding='utf-8').read()
# Ladder views (2026-09-13): gen.py tags every row `cur` / `rel`; the counts go on the
# radio labels, and the share of gross gamma the FULL view adds beyond 2 EM goes in the hint.
import re as _re_lv, json as _json_lv
_LV = [m.group(1).split() for m in _re_lv.finditer(r'<div class="(r(?: [a-z]+)*)"', lad)]
LVCUR, LVREL, LVFULL = (sum(1 for c in _LV if 'cur' in c), sum(1 for c in _LV if 'rel' in c), len(_LV))


def _lv_beyond():
    t = _json_lv.load(open('trinity.json', encoding='utf-8')); P = t['panels']; s, e = t['spot'], t['em']
    tot = sum(abs(v) for b in P for v in P[b]['gamma'].values()) or 1
    far = sum(abs(v) for b in P for k, v in P[b]['gamma'].items() if abs(float(k) - s) > 2 * e)
    return f'{far / tot * 100:.0f}%'


LVBEYOND = _lv_beyond()
T = json.load(open('trinity.json', encoding='utf-8'))


# ---------------------------------------------------------------------------
# Header and overview strip are TEMPLATED from trinity.json. They used to be
# typed literals, which is why a fresh pull left the reference price on the page
# unchanged -- the data moved and the page did not. The prose below is still
# hand-written; that is what the staleness check exists to catch.
def _flip(name):
    h = ((T.get('panels') or {}).get(name) or {}).get('hvl') or []
    return h[0] if h else None


def _net(name):
    return ((T.get('panels') or {}).get(name) or {}).get('net_total')


SPOT = T.get('spot') or 0
REFSET = T.get('ref_settle')          # the settle the book is priced on; on an intraday build SPOT is the tape
EM = T.get('em') or 0
IVP = (T.get('iv_atm') or 0) * 100
HRS = T.get('hours')
SETTLE = T.get('settle')
RDATE = T.get('report_date')
NEXP = T.get('expiries')
RSCALE = T.get('rail_scale') or 1.0
RLABEL = T.get('rail_label') or '1D'
EMR = (T.get('em') or 0) * RSCALE          # the band actually drawn
KIND = ('asia' if (T.get('rail_label') or '').startswith(('Asia', 'Rest of overnight'))
        else 'intraday' if (T.get('rail_label') or '').startswith('Rest of session')
        else 'holiday' if 'Globex' in (T.get('rail_label') or '')
        else 'rthprelim' if T.get('oi_vintage') == 'preliminary' else 'rth')
LAST = T.get('last_print')            # where price IS; SPOT is the settle
NO0 = bool(T.get('no_0dte'))          # no expiry on the report date: the 0DTE panel is EMPTY, not flat
FRONTH = T.get('front_expiry_hours')  # hours from the plan's as-of to the FRONT LISTED expiry
FRONTR = T.get('front_root') or ''
IVBASIS = T.get('iv_basis') or 'calendar'
REFSYM = T.get('ref_symbol') or 'NQ'
LASTAT = T.get('last_at') or ''
LASTSTALE = 'stale' in LASTAT
LASTGAP = T.get('last_vs_settle')
VEXTOT = T.get('vex_total')
if VEXTOT is None:
    VEXTOT = sum(float(v) for v in (T.get('vex') or {}).values())


def _n(v, dp=0):
    return "—" if v is None else f"{v:,.{dp}f}"


def _sdate(s, fmt="%b {d}"):
    """
    Day-of-month without a leading zero. "%-d" is a glibc extension that Windows
    rejects outright, and "%#d" is the MSVC spelling -- so neither is portable.
    The day is substituted directly instead, which works everywhere. The old
    version was wrapped in a bare except, so on Windows it silently fell through
    and printed the raw ISO date instead of "Sep 2".
    """
    try:
        d = datetime.date.fromisoformat(s)
    except Exception:
        return s or "—"
    return d.strftime(fmt.replace("{d}", str(d.day)))


def _mn(v):
    """typographic minus, so the strip matches the rest of the sheet"""
    return "—" if v is None else f"{v:+,.0f}".replace("-", "\u2212")


def _band():
    """Where PRICE sits against the flips -- the live print when we have one.
    Judging this from the settle was wrong by 87 points on 2026-09-03."""
    f = [v for v in (_flip('0DTE'), _flip('NEAR'), _flip('FULL')) if v is not None]
    p = LAST or SPOT
    if not f or not p:
        return "reference"
    return ("inside the flip band" if min(f) <= p <= max(f)
            else "above every flip" if p > max(f) else "below every flip")


def _near(name):
    """
    Net gamma in the strikes AROUND WHERE PRICE IS, inside ±1 expected move.

    The chain-wide column sum is the headline figure, and it has a blind spot:
    it is a property of the book alone, so it does not move when price does. Two
    builds on the same open interest produce identical totals and an identical
    ladder -- the bars ARE the book -- and the only thing that changes is the
    marker sliding a few rows. That made the sheet look inert on 2026-09-04,
    when price round-tripped 210 points overnight and every number on the page
    stayed the same.

    This is the figure that moves. It answers the question the ladder poses but
    cannot resolve by eye: of all that dealer gamma, how much is actually near
    enough to price to be hedged against right now.

    The window is the band ACTUALLY DRAWN -- EMR, the expected move times the
    session's rail scale -- not the full-day move. Mid-session those differ a
    lot (±178 against ±324 with six hours left), and using the full day would
    count gamma at strikes the session no longer has the variance to reach,
    while sitting immediately beside a "rest of session range ±178" tile saying
    exactly that.
    """
    pn = (T.get('panels') or {}).get(name) or {}
    if not pn.get('expiries', 1):      # an empty book has no gamma, not zero gamma
        return None
    g = pn.get('gamma') or {}
    p = LAST or SPOT
    w = EMR or EM
    if not p or not w:
        return None
    return sum(float(v) for k, v in g.items() if abs(float(k) - p) <= w)


STRIP = "".join([
    (f'<div class="st a"><div class="k">0DTE flip</div><div class="v mono">—</div>'
     f'<div class="n">no expiry {_sdate(RDATE)} · front {FRONTR} in {_n(FRONTH)}h</div></div>'
     if NO0 else
     f'<div class="st a"><div class="k">0DTE flip</div><div class="v mono">{_n(_flip("0DTE"))}</div>'
     f'<div class="n">today\'s expiry · net {_mn(_net("0DTE"))}</div></div>'),
    f'<div class="st a"><div class="k">Full-book flip</div><div class="v mono">{_n(_flip("FULL"))}</div>'
    f'<div class="n">{_n(NEXP)} expiries · net {_mn(_net("FULL"))}</div></div>',
    f'<div class="st a"><div class="k">Near-book flip</div><div class="v mono">{_n(_flip("NEAR"))}</div>'
    f'<div class="n">≤9 DTE · net {_mn(_net("NEAR"))}</div></div>',
    f'<div class="st b"><div class="k">'
    + ('Last · settle' if LAST else 'Reference') + '</div>'
    + (f'<div class="v mono">{_n(LAST)}</div>'
       f'<div class="n">settle {_n(SPOT)} · {_mn(LASTGAP)}'
       + (f' · <b style="color:var(--neg)">{LASTAT}</b>' if LASTSTALE
          else f' · {LASTAT}' if LASTAT else '')
       + f' · {_band()}</div></div>'
       if LAST else
       f'<div class="v mono">{_n(SPOT)}</div><div class="n">{_band()}</div></div>'),
    f'<div class="st b"><div class="k">&Gamma; near price</div>'
    f'<div class="v mono">{_mn(_near("FULL"))}</div>'
    f'<div class="n">full book · 0DTE {_mn(_near("0DTE"))}'
    f' · near {_mn(_near("NEAR"))}</div></div>',
    f'<div class="st"><div class="k">{RLABEL} range</div><div class="v mono">±{_n(EMR)}</div>'
    f'<div class="n">{_n(SPOT-EMR)} — {_n(SPOT+EMR)}'
    + (f' · {RSCALE}× day' if RSCALE != 1 else '') + '</div></div>',
    f'<div class="st c"><div class="k">Net VEX</div><div class="v mono">{_mn(VEXTOT)}</div>'
    f'<div class="n">contracts / vol pt</div></div>',
])

# ---------------------------------------------------------------------------
# ONE conversion, ONE offset. UTC -> Pacific, DERIVED FROM THE TIMESTAMP and
# never from the host clock: the pipeline runs on Windows in PT and in a Linux
# container in UTC, so datetime.now() silently means two different things.
#
# This used to be written out separately in three places. The masthead CLOCK
# converted; the two built_at stamps in _changes() did NOT, and printed raw
# UTC, unlabelled, directly under a masthead already reading PT -- "since
# 2026-09-09 16:19" on a sheet whose header said "as of 09:20 PT". Two clocks
# on one page, one of them unmarked and seven hours ahead.
#
# The offset is -7 (PDT). 2026-11-01 is still on the DST list; when it is done
# it is done HERE, once, instead of in three places that can drift apart.
PT_OFFSET = datetime.timedelta(hours=-7)


def _pt_dt(v):
    """UTC instant (ISO string or datetime) -> naive Pacific datetime, or None.

    A string with no zone marker is read as UTC, which is what archive.py
    writes now (it stamps with a trailing Z). Rows written before that fix
    carry a bare local stamp and will read an hour-block off; they only surface
    when an old row is the comparison baseline.
    """
    try:
        if isinstance(v, str):
            d = datetime.datetime.fromisoformat(v.strip().replace('Z', '+00:00'))
        else:
            d = v
        if d is None:
            return None
        if d.tzinfo is not None:
            d = d.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return d + PT_OFFSET
    except Exception:
        return None


def _pt(v, fmt='%Y-%m-%d %H:%M', suffix=' PT'):
    """Same, formatted. Empty string when the input will not parse."""
    d = _pt_dt(v)
    return f'{d:{fmt}}{suffix}' if d else ''


# A mid-session sheet has to say WHICH moment it describes, or it is
# indistinguishable from a premarket sheet with inexplicably tight rails. asof
# is the plan's own "now" -- expiry minus the hours to it -- in UTC; PT is -7
# (PDT; the 2026-11-01 changeover is on the DST fix list with the rest).
# Show the BUILD time next to it whenever the two differ by more than a few
# minutes. Without that, a plan rebuilt at 21:42 said only "as of 17:00 PT" and
# read as four-hour-old data rather than as a plan describing 17:00 -- which was
# itself the bug, but the header hid it instead of showing it.
ASOF = T.get('asof')
CLOCK = ''
if ASOF and RLABEL not in ('1D', None):
    try:
        _a = _pt_dt(ASOF)
        CLOCK = f' · as of {_a:%H:%M} PT'
        # PT DERIVED FROM UTC, never from the host clock. archive.py already
        # learned this one: the pipeline runs on Windows in PT and in a Linux
        # container in UTC, and datetime.now() silently means two different
        # things. It stamped a 07:02 PT build as "built 14:02" -- a plan that
        # looked seven hours old sitting next to an as-of line in PT.
        _now = _pt_dt(datetime.datetime.now(datetime.timezone.utc))
        if abs((_now - _a).total_seconds()) > 900:
            CLOCK += f' · built {_now:%H:%M}'
    except Exception:
        CLOCK = ''

MAST = (f'<h1>Basilisk <span>/ {_sdate(RDATE)}{CLOCK}</span></h1>')
# WHAT THE REFERENCE NUMBER ACTUALLY IS. A 1D plan is anchored on the SETTLE. An
# intraday plan is anchored on the LAST PRINT, which job_intraday passes as
# --spot with no separate --last -- so the page called a 13:39 print "Settle
# 29,628.8" while Thursday's actual settlement was 29,524.75, a hundred points
# away. Two different prices under one label is exactly the confusion the
# settle/last split exists to prevent.
SPOTLAB = 'Reference' if (LAST is None and RLABEL not in ('1D', None) and KIND != 'holiday') else 'Settle'

STAMP = (f'{SPOTLAB} <b class="mono">{T.get("ref_symbol") or "NQ"} {_n(SPOT,1)}</b>'
         + (f' · last <b class="mono">{_n(LAST,1)}</b>'
            + (f' <b style="color:var(--neg)">({LASTAT})</b>' if LASTSTALE else '')
            if LAST else '') + '<br>'
         # An intraday plan carries hours like 6.0333; printing the raw float put
         # "6.0333h to front expiry" on the page next to figures rounded to the
         # point. Two decimals, trailing zeros trimmed, so 6.5 stays 6.5.
         f'IV <b>{IVP:.2f}%</b>' + (' (business time)' if IVBASIS == 'sessions' else '')
         # Hours to the FRONT LISTED expiry, which is not the report date's close
         # whenever nothing expires that day (a holiday Monday plan, an evening
         # plan for a Monday). `hours` is the plan's as-of offset and used to be
         # printed here as if it were this.
         + (f" · {f'{FRONTH:.2f}'.rstrip('0').rstrip('.')}h to front expiry ({FRONTR})" if FRONTH
            else f" · {f'{HRS:.2f}'.rstrip('0').rstrip('.')}h to front expiry" if HRS else '')
         + '<br>'
         f'OI <b>CME settle, {_sdate(SETTLE, "%a %b {d}")}</b>'
         + ('' if T.get('oi_vintage', 'final') == 'final'
            else ' <b style="color:var(--brass)">(preliminary)</b>'))

# ---------------------------------------------------------------------------
# The numbers on this page are computed. The PROSE below -- "Reading the day",
# the four setups, every "why" block -- is hand-written by Claude against one
# specific snapshot. Regenerate against fresh data and the ladder updates while
# the words do not, which is the one failure that would actually cost money.
#
# So the narrative carries a stamp of what it was written for, and the build
# compares it to the live data. If they have drifted, the page says so at the
# top instead of quietly looking finished.
NARRATIVE_FOR = {"settle": "2026-09-10", "spot": 29038.25, "flips": {"0DTE": 29297.8, "NEAR": 29289.7, "FULL": 29243.7}, "rail_lo": 28898.77, "rail_hi": 29177.73, "em": 376.7, "iv": 20.59, "rail_scale": 0.3703, "net_0DTE": -314, "net_NEAR": -878, "net_FULL": -884, "vex_total": 739.1}
TOL = 15.0        # points; a level moving less than this does not change the read
_narrative = None
if pathlib.Path('narrative.json').exists():
    _narrative = json.loads(pathlib.Path('narrative.json').read_text(encoding='utf-8'))
    NARRATIVE_FOR = _narrative['stamp']
# Per-field tolerances for everything ELSE the prose quotes. The stamp used to
# check spot and two flips only, and on 2026-09-07 all three matched while the
# lead quoted rails 15 points off and a warning block quoted rails 140 points
# off -- with no banner. A key missing from the stamp is simply not checked, so
# an older one-line stamp still works.
TOLS = {"rail_lo": 8.0, "rail_hi": 8.0, "em": 15.0, "iv": 0.75, "rail_scale": 0.03,
        "net_0DTE": 40, "net_NEAR": 60, "net_FULL": 100, "vex_total": 80}

def staleness():
    try:
        t = json.load(open("trinity.json", encoding="utf-8"))
    except Exception:
        return ['snapshot unavailable']
    drift = []
    from publication import identity
    if NARRATIVE_FOR.get('snapshot_id'):
        if NARRATIVE_FOR['snapshot_id'] != identity(t): drift.append('snapshot identity changed')
    elif NARRATIVE_FOR.get('settle') != t.get('settle'):
        drift.append('settlement date changed')
    live_spot = t.get("spot")
    if live_spot and abs(live_spot - NARRATIVE_FOR["spot"]) > TOL:
        drift.append(f"reference {NARRATIVE_FOR['spot']:,.0f} → {live_spot:,.0f}")
    for k, was in NARRATIVE_FOR["flips"].items():
        now = ((t.get("panels") or {}).get(k) or {}).get("hvl") or []
        if now and abs(now[0] - was) > TOL:
            drift.append(f"{k} flip {was:,.0f} → {now[0]:,.0f}")
    live = {"rail_lo": t.get("rail_lo"), "rail_hi": t.get("rail_hi"), "em": t.get("em"),
            "iv": (t.get("iv_atm") or 0) * 100, "rail_scale": t.get("rail_scale"),
            "vex_total": t.get("vex_total")}
    for nm in ("0DTE", "NEAR", "FULL"):
        live[f"net_{nm}"] = ((t.get("panels") or {}).get(nm) or {}).get("net_total")
    labels = {"rail_lo": "rail low", "rail_hi": "rail high", "em": "expected move", "iv": "ATM IV",
              "rail_scale": "rail scale", "vex_total": "net VEX",
              "net_0DTE": "net 0DTE", "net_NEAR": "net near", "net_FULL": "net full"}
    for k, tol in TOLS.items():
        was, now = NARRATIVE_FOR.get(k), live.get(k)
        if was is None or now is None:
            continue
        if abs(float(now) - float(was)) > tol:
            dp = 2 if k in ("iv", "rail_scale") else 0
            drift.append(f"{labels[k]} {float(was):,.{dp}f} → {float(now):,.{dp}f}")
    return drift

DRIFT = staleness()
BANNER = ""
if DRIFT:
    BANNER = ('<div class="note w" style="margin-bottom:26px"><b>The numbers are fresh; '
              'the words are not.</b> This page was rebuilt from new data, but the '
              'commentary and the four setups were written for the '
              f'{NARRATIVE_FOR["settle"]} snapshot and have not been rewritten. Changed: '
              + '; '.join(f'<b class="mono">{d}</b>' for d in DRIFT)
              + '. Ask Claude to rewrite the narrative before trading these levels.</div>')


# ---------------------------------------------------------------------------
# THE PROSE, as data. Four blocks -- the lead note, the reading-the-day bullets,
# the four cards, the VEX note -- each fenced by a CC:TAG / /CC:TAG comment pair.
# command_center.py replaces the body between a pair when it regenerates the
# commentary, and reads the four cards back out of the rendered sheet by the
# data-entry / data-tp / data-sl attributes _cards() stamps on each card. The
# markup these render into is byte-for-byte what used to be typed inline in
# BODY; only the words moved up here. Strings may carry inline HTML (<b>, .mono).
#
# CARDS: name, tag (sub-line), rank (Primary / Secondary / Opportunistic),
# pri (green border), side (long|short), entry, sl, tp (list, TP1..TPn), why.
# Points and R-multiples are computed from entry / sl / tp, not typed.

# CC:LEAD
LEAD = "Sunday-open plan for the 2026-09-14 session, rebuilt at 15:24 PT on the reopen tape. Globex opened at 28,997.75, 389 points under Friday's 29,387.00 settle, printed 29,111.00 in its first minute and 28,981.00 six minutes later; the reference is <b class=\"mono\">29,038.25</b>, the 22:23 UTC print \u2014 349 points under the settle, 0.93 of the one-session expected move, and on the 29,000 strike that was the largest negative node on Friday's map, 1.02 expected moves away then. The book is still the 09-10 FINAL: Friday's own open interest is broadcast after this reopen and had not reached Databento at 15:21 PT, so the flips did not move \u2014 0DTE 29,297.8, near 29,289.7, full 29,243.7 \u2014 and price now sits 260, 251 and 205 points under all three. What moved is everything measured from price: the book reads net short at spot, -314 0DTE, -878 near, -884 full, the same figures chain-wide and at spot, against +117 / +362 / +641 on the 13:00 PT sheet; the Asia band is <b class=\"mono\">28,899</b> to <b class=\"mono\">29,178</b>, 0.3703\u00d7 the \u00b1377 one-session move at 20.59% IV, and the whole of Friday's map above 29,243 is outside it. Under the reference the near book is negative at every rung: 28,950 (-12 / -33 / -47), 28,900 (-11 / -34 / -39) on the lower rail, 28,850 (-5 / -44 / -44), the 28,800 wall (-42 / -80 / -95, 770 contracts), 28,750 (-20 / -49 / -49), 28,700 (-9 / -34 / -116) and the 28,500 wall (-22 / -90 / -96, 1,772 contracts) 1.43 expected moves down. Over it: 29,100 (-12 / -34 / -50), 29,150 (-18 / -33 / -33), the upper rail 29,178, 29,200 (-14 / -24 near; the +96 in the full column is a longer-dated line basis-shifted onto this axis), then the flips and the first positive rung at 29,300 (+38 near / +38 full). Gamma structure carries no direction: below every flip the hedging described here follows price rather than leaning against it, and nothing on this board says which way the tape commits. Why the gap happened is not in the book \u2014 the map is Thursday's open interest and knows nothing of the weekend."
# /CC:LEAD

# CC:READING
READING = ["What changed since the 13:00 PT sheet for this session: the reference, and with it every distance on the page. That sheet anchored on Friday's 29,387.00 settle with price 89 points over the nearest flip and the book net long; the reopen put the reference at 29,038.25, 349 lower, under all three flips, and the same 09-10 book now reads -314 / -878 / -884 at spot against +117 / +362 / +641. The flips are to the decimal where they were, because the book is the same and the weekend is zero variance in business time; the rails re-centred and narrowed a little \u2014 28,899 / 29,178 at 0.3703\u00d7 against 29,242 / 29,532 at 0.3797\u00d7, the overnight curve two and a half hours past the settle instead of two. Every card on the earlier sheet was written above 29,290; none of them triggered and none apply now.", "29,000 \u2014 the wall the session opened on: -84 0DTE / -131 near / -135 full, 2,192 contracts, the largest print on all three books within an expected move, with 28,950 (-12 / -33 / -47) and 29,050 (-8 / -10 / -10) making it a cluster. Node quality 0.68 ranks it first for attention; on 500 sessions and 4,556 touches scored nodes reacted no better than a distance-matched placebo, so the ranking says where hedging is concentrated, not that the level holds. The first minute traded through it both ways, 28,990 to 29,111; the low since is 28,981 at 22:06 UTC and the reference 29,038.25 at 22:23 UTC. Every stop on this sheet is structural: 21 points, 0.15\u00d7 the \u00b1139.5 Asia band, beyond the strike the card leans on, with the printed entry one point inside that strike \u2014 22 points of risk on every card. Each card names its entry zone, from the strike to halfway to the stop; a fill deeper in the zone lowers the risk toward 11, a retest entry raises it, and the stop does not move. No entry offset has measured edge here \u2014 fading a touch lost at every geometry over 500 sessions, and limits in front of the level were negative too \u2014 so the entry is the trader's read and the R shown is from the strike.", "Overhead \u2014 29,100 (-12 / -34 / -50) and 29,150 (-18 / -33 / -33) are the two material strikes between price and the upper rail at 29,178; 29,200 carries -14 0DTE / -24 near and a +96 full print that is a longer-dated line basis-shifted onto this axis, and 29,250 (-11 / -10 / -10) sits on the full flip 29,243.7. The near flip 29,289.7 and the 0DTE flip 29,297.8 are just under 29,300 (+38 near / +38 full, -9 0DTE), the first positive rung, with 29,400 (+20 / +32 / +24) behind it; all of that is outside the band. The engine's one path up is a 30-minute close above 29,085 toward 29,300, 29,400 and 29,500, invalidated by a 30-minute close below 29,000; the cards here are written on touches, and a reclaim of the flips would put price back on Friday's map, which the 13:00 PT sheet still describes.", "Below \u2014 28,950 (-12 / -33 / -47), 28,900 (-11 / -34 / -39) on the lower rail 28,899, 28,850 (-5 / -44 / -44), then the 28,800 wall (-42 / -80 / -95, 770 contracts, quality 0.60), 28,750 (-20 / -49 / -49), 28,700 (-9 / -34 / -116) and, 1.43 expected moves down, the 28,500 wall (-22 / -90 / -96, 1,772 contracts, quality 0.70) \u2014 the highest-ranked node on the board and an RTH extension, not an overnight target. The engine's base case is a 30-minute close below 29,010 toward 28,850, 28,750 and 28,500, invalidated by a 30-minute close above 29,100; its alternative a 30-minute close below 28,935 toward 28,800, 28,700 and 28,500. Every one of these rungs is negative on the near book: hedging below the flips follows price, and the sign says nothing about whether the move continues.", "The tape, now complete on this machine: Friday's RTH opened 29,417.50, printed 29,499.75 and 29,334.00, closed the 19:59 UTC bar at 29,386.75 against the 29,387.00 settle, and the last Globex bar of the week closed 29,391.75 at 20:59 UTC \u2014 the 29,468.75 on Friday's sheets was the 08:51 PT print of a feed that had died, and the 13:00 PT sheet was right to anchor on the settle instead. Thursday night's low was 29,040.50; the reopen printed under it in its first minute. Twenty-one formations, spread rather than concentrated, is a fragmented board (structural confidence 0.55); the trade gate's verdict is <i>look</i> \u2014 the book supports a conditional path \u2014 and directional confidence is none, because gamma structure carries no direction. No red-folder USD release inside Monday's unit; the FOMC decision on Wednesday is outside it."]
# /CC:READING

# THE GEOMETRY COMES FROM THE ENGINE (2026-09-16, Mark: "make sure your proposed gameplan
# is now pushed into the dashboard updating the cards"). precommentary.cards_for_build
# writes notes/plan_cards.json -- the ranked A trade and one alternate, with the entry ZONE,
# the swing invalidation, three targets, the gate and the rank all computed. If that file is
# for this build's reference it replaces CARDS below, so the model writes the words and the
# engine writes the levels. Absent or stale, the literal CARDS block is used exactly as before.
PLAN = {}


def _engine_cards(ref, path="notes/plan_cards.json", tol=0.5):
    """The ranked plan, when it was ranked for THIS reference.

    Keeps the whole payload in PLAN rather than just the cards: the held-back list is a
    sibling key and the sheet prints it under the two cards (2026-09-16).
    """
    global PLAN
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return None
    cs = d.get("cards") or []
    if not cs or ref is None or abs(float(d.get("reference") or -1) - float(ref)) > tol:
        return None
    PLAN = d
    return cs


# CC:CARDS
CARDS = [{"name": "29,000 wall hold", "tag": "Pullback into 29,000 that holds \u2014 entry zone 29,001 down to 28,990, stop fixed at 28,979", "rank": "Primary", "side": "long", "entry": 29001, "sl": 28979, "tp": [29100.0, 29150.0, 29200.0], "why": "29,000 (-84 0DTE / -131 near / -135 full, 2,192 contracts) is the strike the session opened on and the largest print on every book within an expected move; the reference sits 38 points over it. The stop is structural: 21 points beyond the strike \u2014 0.15\u00d7 the \u00b1139.5 Asia band, the buffer the command center applies to every card \u2014 fixed at 28,979, so 22 points of risk from the printed entry one point inside the wall; a fill lower in the zone, toward 28,990, cuts it toward 11. TP1 29,100 (-12 / -34 / -50), the first material strike overhead, 4.5R; TP2 29,150 (-18 / -33 / -33); TP3 29,200 (-14 / -24 near, +96 full), just over the upper rail at 29,178. Nothing here is positive gamma: the hedging around this strike follows price, and a hold is something to see printed, not to assume from the size of the strike \u2014 scored nodes reacted no better than a placebo over 500 sessions."}, {"name": "Loss of 29,000", "tag": "29,000 gives way \u2014 price trades below the wall and stays there; entry zone 28,999 up to 29,010, stop fixed at 29,021", "rank": "Secondary", "side": "short", "entry": 28999, "sl": 29021, "tp": [28950.0, 28900.0, 28800.0], "why": "One point under the wall the entry is beneath the largest negative strike on the board, with the near book negative at every rung below it. Stop 29,021, the buffer back above 29,000, fixed; 22 points of risk from the printed entry, less on a fill back inside the wall toward 29,010. TP1 28,950 (-12 / -33 / -47), 2.2R; TP2 28,900 (-11 / -34 / -39) on the lower Asia rail 28,899; TP3 the 28,800 wall (-42 / -80 / -95, 770 contracts). The engine's base case runs the same way \u2014 a 30-minute close below 29,010 toward 28,850, 28,750 and 28,500, invalidated by a 30-minute close above 29,100 \u2014 and this card is its touch-entry form. Nothing on the board forecasts the break; the gamma sign below the flips describes hedging that follows price, not a direction."}, {"name": "29,100 first touch", "tag": "Rejection on the first approach into 29,100 \u2014 entry zone 29,099 up to 29,110, stop fixed at 29,121", "rank": "Secondary", "side": "short", "entry": 29099, "sl": 29121, "tp": [29050.0, 29000.0, 28950.0], "why": "29,100 (-12 / -34 / -50) is the first material strike over the reference, 62 points up, with 29,150 (-18 / -33 / -33) and the upper rail 29,178 behind it. Stop 29,121, the buffer beyond 29,100, fixed; 22 points of risk from the printed entry one point inside the strike, half that on a fill toward 29,110. If price runs straight to 29,150 instead, the same card sits one point under it with the stop at 29,171 and targets 29,100 / 29,050 / 29,000. TP1 29,050 (-8 / -10 / -10), the thin rung between the two, 2.2R; TP2 29,000, the wall; TP3 28,950. The first minute of the reopen printed 29,111 and gave it all back, which is the shape this card wants to see again; the 500-session node study found fading a level loses at the trade level once the fill is counted \u2014 context for a rejection you see, not a resting order."}, {"name": "28,800 wall hold", "tag": "Extension below the band into the 28,800 wall that holds \u2014 entry zone 28,801 down to 28,790, stop fixed at 28,779", "rank": "Opportunistic", "side": "long", "entry": 28801, "sl": 28779, "tp": [28850.0, 28900.0, 29000.0], "why": "28,800 (-42 0DTE / -80 near / -95 full, 770 contracts, quality 0.60) is the next wall under the band, 99 points below the lower rail 28,899 and 238 under the reference \u2014 0.63 expected moves, an extension rather than the base case. Stop 28,779, the buffer beyond the wall, fixed; 22 points of risk from the printed entry, less on a fill toward 28,790; 28,750 (-20 / -49 / -49) and 28,700 (-9 / -34 / -116) are the next strikes under the stop and invalidate the hold, not just the card. TP1 28,850 (-5 / -44 / -44), 2.2R, which sits outside the Asia band; TP2 28,900 on the rail; TP3 29,000, the wall the session opened on. The rails' evidence is containment of the close, not a reaction at the touch; a wall this far down is a place to watch for a reaction, not to rest an order."}]
# /CC:CARDS

# CC:VEX
VEX_NOTE = "Net VEX +739 contracts per vol point, and none of it is on the strike price opened on: inside 350 points of the reference the prints are 6.6 at 28,700, 3.4 at 28,800, 2.1 at 28,750, 1.3 at 28,850 and 0.8 at 28,900 below, 3.3 at 29,200 and 1.8 at 29,300 above, nothing at 29,000, 29,100 or 29,150. The mass is far away \u2014 22.9 at 27,700, 22.0 at 28,000, 19.2 at 30,000, 21.4 at 30,200, 20.7 at 30,700, 32.6 at 31,700 \u2014 so a vol move re-hedges strikes this plan does not reach; the total fell from +787 with the reference, not with the book."
# /CC:VEX
if _narrative:
    _prose = _narrative['prose']
    LEAD, CARDS, VEX_NOTE = _prose['lead'], _prose['setups'], _prose.get('vex_note', '')
    READING = _prose.get('reading') or []          # accepted, no longer rendered


# ---------------------------------------------------------------------------
# THE SHEET'S OWN DATA (2026-09-16). This file renders; it does not compute. But
# publication.publish() stages seven files and runs this script in that directory,
# where swings / releases / precommentary are NOT importable. So: compute when we can
# (a run in the project folder), cache to notes/sheet_extra.json, and read the cache
# when we cannot. The cache is staged, so the published sheet carries the same numbers
# the live one did rather than silently dropping four sections.
EXTRA = {}


def _extra_compute():
    import datetime as _dt
    import swings as _SW
    import releases as _REL
    import precommentary as _PC
    import vwap as _VW
    day = RDATE or SETTLE
    d0 = _dt.date.fromisoformat(day)
    today = _PC._bars(day)
    week = []
    for _k in range(7, -1, -1):
        week += _PC._bars((d0 - _dt.timedelta(days=_k)).isoformat())
    week.sort(key=lambda b: b['ts'])
    sess = ([b for b in today if b['ts'] >= (today[-1]['ts'][:10] + ' 13:30')] or today) if today else []
    out = {}
    # The read is the MULTI-DAY window over the book-adjusted tape, the way
    # precommentary asks for it -- location is a session question, the read is not.
    if today:
        rd = _SW.bias(_PC._since_book(T, today)) or {}
        out['read'] = {k: rd.get(k) for k in ('bias', 'run', 'since', 'why')}
    if sess:
        out['session'] = {'open': sess[0]['o'], 'hi': max(b['h'] for b in sess),
                          'lo': min(b['l'] for b in sess), 'last': sess[-1]['c']}
        out['lows'] = [[t[11:16], p] for t, p, _ in
                       _SW.lows(sess, _SW.WINDOW_INTRA, _SW.WINDOW_INTRA)][-4:]
    # Both sides of both books. They are map levels and a note: they move no score.
    out['bands'] = []
    if week:
        out['bands'] += _PC.band_map(T, week, start=_VW.week_start(week), label='weekly')
    if sess:
        out['bands'] += _PC.band_map(T, sess, start=sess[-1]['ts'][:10] + ' 13:30', label='session')
    # allow_fetch=False on purpose: this runs inside publication's subprocess under
    # check=True, and a hung HTTP call would fail a publish. releases.py's own runs
    # keep the weekly cache warm.
    try:
        out['releases'] = _REL.for_report(day, asof=T.get('asof'), allow_fetch=False)
    except Exception as _e:
        out['releases'] = {'events': [], 'error': f'{type(_e).__name__}: {_e}'}
    return out


try:
    EXTRA = _extra_compute()
    try:
        pathlib.Path('notes').mkdir(exist_ok=True)
        pathlib.Path('notes/sheet_extra.json').write_text(json.dumps(EXTRA), encoding='utf-8')
    except Exception:
        pass
except Exception:
    try:
        EXTRA = json.loads(pathlib.Path('notes/sheet_extra.json').read_text(encoding='utf-8'))
    except Exception:
        EXTRA = {}


def _cd(t):
    """A countdown from the plan's as-of to an ISO instant. '' when either will not parse."""
    import datetime as _dt
    try:
        a = _dt.datetime.fromisoformat((ASOF or '').replace('Z', '+00:00'))
        e = _dt.datetime.fromisoformat(str(t).replace('Z', '+00:00'))
        m = int((e - a).total_seconds() // 60)
    except Exception:
        return ''
    if m < 0:
        return 'passed'
    h, mm = divmod(m, 60)
    return f'in {h}h {mm:02d}m' if h else f'in {mm}m'


def _read_head():
    """The headline is the TAPE'S STRUCTURE, not a forecast.

    Mark chose to keep the mock's headline but source it from swings.bias() and word it
    as a sequence -- 'five higher floors' -- rather than as a word like 'bullish' laid
    over the same data. The sentence is the one swings wrote, verbatim.
    """
    rd = EXTRA.get('read') or {}
    why = (rd.get('why') or '').strip()
    if not why:
        return ''
    cls = {'up': ' up', 'down': ' down'}.get(rd.get('bias') or '', '')
    return (f'<div class="call{cls}">{why[0].upper()}{why[1:]}</div>'
            f'<div class="sub">The tape\'s own sequence of swings, from the multi-day window. '
            f'It describes what the session has done, not what it does next.</div>')


def _alerts():
    """Red folder, expiry and read-invalidation, with the countdown from this build."""
    rel = EXTRA.get('releases') or {}
    rows = []
    for e in (rel.get('events') or []):
        t = e.get('time_utc') or ''
        gate = e.get('gate_minutes') or 15
        rows.append(('red', t[11:16] + 'Z', _cd(t),
                     'Red folder &middot; ' + (e.get('title') or ''),
                     f'No new entry inside {gate} minutes of it. A position carried '
                     'through is a decision about the decision.'))
    fe = T.get('front_expiry')
    if fe:
        rows.append(('', str(fe)[11:16] + 'Z', _cd(fe),
                     (FRONTR or '0DTE') + ' expiry',
                     'every level on the front book stops existing at the bell'))
    rd, lows = EXTRA.get('read') or {}, EXTRA.get('lows') or []
    if rd.get('bias') in ('up', 'down') and lows:
        ts, px = lows[-1]
        word = 'below' if rd.get('bias') == 'up' else 'above'
        rows.append(('', ts, '', f'the read ends {word} {_n(px)}',
                     'the last confirmed swing of the sequence; it is what the read is standing on'))
    if not rows:
        return ''
    body = ''.join(
        f'<div class="al{" " + k if k else ""}">'
        f'<span><span class="tm">{tm}</span><span class="ct">{cd}</span></span>'
        f'<span><span class="wt">{wt}</span><br><span class="nt">{nt}</span></span></div>'
        for k, tm, cd, wt, nt in rows)
    return ('<div class="alerts"><div class="ah"><span class="t">Alerts</span></div>'
            + body + '</div>')


def _held():
    """'Everything else is a one-line held back list.' Names, not a count."""
    cs = (PLAN.get('held_back_cards') or []) if PLAN else []
    n = ((PLAN.get('held_back') if PLAN else 0) or len(cs))
    if not n:
        return ''
    names = ' &middot; '.join(c.get('name') or '?' for c in cs)
    return f'<div class="held">{n} held back{": " + names if names else ""}</div>'


def _vwapbook():
    """The VWAP bands read against the gamma book. Notes, not rules -- they move no score."""
    rows = EXTRA.get('bands') or []
    if not rows:
        return ''
    out = []
    for book in ('weekly', 'session'):
        rs = [r for r in rows if (r.get('label') or '').startswith(book)]
        if not rs:
            continue
        body = ''.join(
            f'<div class="bmr {r.get("sign") or ""}">'
            f'<span class="l">{r.get("label")}</span>'
            f'<span class="p">{_n(r.get("px"))}</span>'
            f'<span class="d">{_mn(r.get("dist"))} &middot; 0DTE {_mn(r.get("gex"))}</span>'
            f'<span class="r">{r.get("note") or r.get("read") or ""}</span></div>'
            for r in rs)
        out.append(f'<div class="bm"><div class="ah"><span class="t">{book} VWAP against the book</span>'
                   f'</div>{body}</div>')
    if not out:
        return ''
    return ('<section class="sec"><div class="sech"><h2>VWAP bands</h2>'
            '<div class="hint">map levels and a card note &mdash; they move no score; '
            'weekly wins a tie with session</div></div>' + ''.join(out) + '</section>')


# ---------------------------------------------------------------------------
# COVERAGE: the plan has to card BOTH sides of the anchor (2026-09-15).
#
# On the 2026-09-15 Asia sheet every card sat below the reference. Cards were
# 29,401 (long), 29,388 (short) and 29,301 (long) against a reference of 29,422;
# the session reopened at 29,457, spent five hours between 29,455 and 29,495,
# and only reached the first card at 04:21. The move that was there -- a short at
# the material strike ABOVE price, 29,490 -- was not on the sheet at all, and the
# card that was primary stopped for -13.
#
# The generator emits four shapes around a wall (hold / break at the wall below,
# rejection / acceptance at the wall above), so two of the cards can be the SAME
# level facing opposite ways. When that pair is the only wall carded, the sheet
# reads as four setups and is really one level, on one side of price. That is the
# state this refuses: not a view about direction, just the arithmetic that a plan
# which cannot fill above the anchor has nothing to say if the session goes up.
#
# The anchor is the opening print where one exists (LAST) and the reference the
# plan is priced to otherwise -- an Asia plan is written before its own open, so
# the reference is the only anchor it has. A card sitting ON the anchor covers
# neither side: `ONE_SIDED_TOL` points of slack each way, since an entry one
# point inside a strike that happens to be the reference is not coverage.
#
# It BANNERS, it does not refuse (2026-09-15, Mark: "so many gates that it is
# losing freedom of interpretation -- it is supposed to be a gameplan not a
# bible"). This started as a refusal and that was one gate too many: the page
# already carries twenty narrowing thresholds upstream of it, and a plan that is
# one-sided is a plan worth reading with the gap named, not a plan worth
# withholding. The banner says which side is empty and where to look; what to do
# about it is the reader's. TEDDY_STRICT_COVERAGE=1 restores the hard refusal for
# an unattended run that would rather fail than publish a half-covered plan.
ONE_SIDED_TOL = 3.0


def coverage():
    """-> (anchor, [entries above], [entries below], reason) for the cards as they stand."""
    anchor = LAST if LAST is not None else SPOT
    if not anchor or not CARDS:
        return anchor, [], [], "no anchor or no cards; coverage not checked"
    above, below = [], []
    for c in CARDS:
        try:
            e = float(c["entry"])
        except (KeyError, TypeError, ValueError):
            continue
        if e > anchor + ONE_SIDED_TOL:
            above.append((e, c.get("name", "?")))
        elif e < anchor - ONE_SIDED_TOL:
            below.append((e, c.get("name", "?")))
    return anchor, above, below, ""


ANCHOR, _ABOVE, _BELOW, _COVNOTE = coverage()
ONE_SIDED = bool(ANCHOR and CARDS and not _COVNOTE and not (_ABOVE and _BELOW))
if ONE_SIDED:
    _missing = "above" if not _ABOVE else "below"
    _side = "below" if not _ABOVE else "above"
    _have = _BELOW if not _ABOVE else _ABOVE
    _at = "last print" if LAST is not None else "reference"
    _msg = [
        f"ONE-SIDED PLAN: every card sits {_side} the anchor {ANCHOR:,.0f} ({_at});",
        f"nothing on this page can fill {_missing} it.",
        "  cards: " + ", ".join(f"{n} {e:,.0f}" for e, n in sorted(_have, reverse=not _ABOVE)),
        f"  Card a material strike {_missing} {ANCHOR:,.0f}. The 2026-09-15 Asia sheet carded",
        "  29,400 twice and missed the 29,490 short the session actually gave.",
        "  Published anyway, with the gap named on the page.",
    ]
    for _m in _msg:
        print("  " + _m, file=sys.stderr)
    if os.environ.get("TEDDY_STRICT_COVERAGE") in ("1", "true", "yes"):
        print("  TEDDY_STRICT_COVERAGE is set: refusing to write trinity.html.", file=sys.stderr)
        raise SystemExit(2)
    BANNER += ('<div class="note w" style="margin-bottom:26px"><b>One side of the anchor '
               'is uncarded.</b> Every setup on this page sits '
               f'{"below" if not _ABOVE else "above"} <b class="mono">{ANCHOR:,.0f}</b>, so '
               f'none of them fills if the session goes {"up" if not _ABOVE else "down"}. '
               f'The nearest material strike {_missing} it is the one to watch; there is no '
               'card on it.</div>')


# ---- sizing: REMOVED 2026-09-16 -------------------------------------------------
# Mark: "the propfirm geometry backtest is messing everything up -- we're trading based off
# of prop firm sizing and that doesnt even make sense and shouldnt be determined mechanically.
# Can we remove that logic completely as well."
#
# What came out: _load_sizing / SIZING (notes/sizing.json), _book_gate, _size_row, the Size row
# on every card, and the sizing paragraph in the caveat block. The sheet described position
# size in contracts off an account cap, gated it on the book gate and the rail, and printed
# dollars at risk -- none of it a statement about the market, and the gates it leaned on were
# the same ones that had been shrinking the plan. Position size is the trader's decision with
# the trader's account in front of them; the sheet's job is the map and the geometry. Risk in
# POINTS is still on every card, which is what sizing is computed FROM.
# notes/sizing.json is left on disk and simply unread.

def _caveat():
    """
    The caveat block, GENERATED from trinity.json. Until 2026-09-05 this was
    literal text inside BODY -- outside every CC: fence, invisible to
    staleness() -- and the sheet on disk was asserting "the reference is 23
    minutes stale ... measured from the 13:39 UTC print of 29,628.75" and
    quoting rails of 29,451 / 29,807 on a build whose rails were 29,593 /
    29,664 and whose last_print was null. Every sentence here is now tied to a
    field, and the standing backtest caveat is the only fixed text.
    """
    paras = []
    if LASTSTALE:
        paras.append(f'<b>The reference is stale.</b> The last print on this sheet is '
                     f'<b class="mono">{_n(LAST, 2)}</b> ({LASTAT}). Every distance measured '
                     f'from it is measured from a price that has stopped updating. '
                     f'<b>Check your live price before you use any distance on this page</b>, '
                     f'and re-run the job once the cache is moving again.')
    elif KIND in ('intraday', 'asia'):
        # An intraday build has last_print NULL precisely because the print was passed as --spot and
        # became the reference; the settle is in ref_settle. Until 2026-09-08 this fell through to the
        # branch below and said the opposite of the truth: the 09-08 0955 sheet shipped asserting "the
        # reference is the settle" three paragraphs under a Reading block that called it a print two
        # minutes old.
        # 2026-09-08, later: an ASIA build passes --spot the same way and hit the identical falsehood
        # -- the 09-09 sheet asserted "the reference is the settle" against a 20:59 UTC print. The
        # branch was keyed on one KIND when the cause is the --spot path, which asia shares. Widened.
        gap = (SPOT - REFSET) if (SPOT and REFSET) else None
        _kindtxt = 'a mid-session build' if KIND == 'intraday' else 'an overnight build'
        paras.append(f'<b>The reference is the tape, not a settle.</b> This is {_kindtxt}: every '
                     f'distance on this page is measured from <b class="mono">{_n(SPOT, 2)}</b>, the last '
                     f'print when it ran'
                     + (f', {_n(abs(gap), 2)} points {"above" if gap > 0 else "below"} the '
                        f'{_sdate(SETTLE)} settle' if gap else '') + '.')
    elif LAST is None and RLABEL not in ('1D', None):
        paras.append('<b>No last print in this build.</b> The reference is the settle, not the tape.')
    if NO0:
        paras.append(f'<b>Nothing expires on {_sdate(RDATE)}.</b> The 0DTE column is empty because '
                     f'there is no 0DTE book -- not because dealers are flat. The front expiry is '
                     f'<b class="mono">{FRONTR}</b>, {_n(FRONTH, 1)} hours from the plan\'s as-of; '
                     f'read the Near column as the front book today.')
    if T.get('oi_vintage') == 'preliminary':
        paras.append(f'<b>This book is PRELIMINARY.</b> CME had not published final '
                     f'{_sdate(SETTLE)} open interest when this ran, so the plan uses the evening '
                     f'file -- a session fresher than stepping back would give, and subject to '
                     f'revision. The header stamps the vintage.')
    paras.append('<b>Do not read a range from the gamma sign.</b> The sign describes how hedging '
                 'behaves once price moves -- long gamma above the flip is not a promise of a quiet '
                 'session.')
    band = (f'<b class="mono">{_n(SPOT - EMR)} / {_n(SPOT + EMR)}</b>'
            if EMR else '—')
    paras.append(f'<b>The rails are the session’s expected move, drawn around the reference.</b> {band}'
                 + (f' at {RSCALE:g}× the one-session move of ±{_n(EM)}' if RSCALE != 1 else
                    f', one session at ±{_n(EM)}')
                 + '. They are the natural far target on this page. The confluence dots say which of '
                   'three disjoint books a strike is material in -- they are not a ranking.')
    return ('    <div class="note w">\n    ' + '<br><br>\n    '.join(paras) + '\n  </div>')


# _reading() removed 2026-09-16 -- "remove reading the day as well". The bullets
# restated levels the ladder already carries; the lead keeps what decides the session.


# Swap the model's geometry for the engine's, when the ranked plan is for THIS reference --
# but NEVER over a published narrative.
#
# publication.publish() renders in a staging directory that carries build.py, trinity.json,
# state.json and narrative.json and NOT notes/plan_cards.json, so the published sheet keeps the
# prose's own cards. A bare `python build.py` in the project root found that file and swapped
# them out, so the same narrative rendered as two different sheets depending on which directory
# it ran in -- and with no banner, because the narrative stamp still matched. The command
# center's boot() runs the pipeline on every page load, so every reload silently replaced the
# published plan with the ranked one (three times on 2026-09-17 before it was caught).
#
# The prose's cards are hand-written and their geometry is checked by interpret.check_card_levels
# and precommentary.structure_problems before publication; they are not a fallback. The ranked
# plan fills in when there is no published prose, which is what the CC:CARDS block was for.
# Keeping the two renders identical is also what makes publication.validated() true: it compares
# the root's trinity.html against the sha256 of the staged render.
_ENGINE_CARDS = None if _narrative else _engine_cards(LAST if LAST is not None else SPOT)
if _ENGINE_CARDS:
    CARDS = _ENGINE_CARDS


def _cards():
    """
    Render CARDS into the .pl markup. Accepts the hand-written shape above and
    the leaner one the commentary API returns ({name, side, entry, tp, sl, why}
    with a single tp) -- missing keys get quiet defaults, tp may be a number.
    Distances and R are computed, so a card can never carry an R that disagrees
    with its own stop. Levels are also stamped as data-* attributes on the card
    so the command center reads them exactly instead of scraping the labels.
    """
    out = []
    for i, c in enumerate(CARDS):
        entry, sl = float(c["entry"]), float(c.get("sl", c.get("stop")))
        tps = c.get("tp") or []
        tps = [float(t) for t in (tps if isinstance(tps, (list, tuple)) else [tps])]
        rank = c.get("rank") or ("Primary" if i == 0 else "Secondary")
        pri = c.get("pri", i == 0)
        side = c.get("side") or ("short" if tps and tps[0] < entry else "long")
        risk = abs(entry - sl)
        # ENTRIES ARE A RANGE, NOT A FLAT NUMBER (Mark, 2026-09-16: "I want entries to be
        # shown as a range as well rather than a flat number described basically as fact").
        # precommentary.cards_for_build has emitted zone_lo/zone_hi since 137e595 and this
        # renderer ignored them, so every sheet kept printing one price as though it were a
        # fact. `entry` stays the fill R is quoted from and is named in the row, because a
        # range with a single R is meaningless unless you say which end it came from.
        zlo, zhi = c.get("zone_lo"), c.get("zone_hi")
        zone = None
        if zlo is not None and zhi is not None and abs(float(zhi) - float(zlo)) >= 0.25:
            zone = (min(float(zlo), float(zhi)), max(float(zlo), float(zhi)))
        e_cell = f"{_n(zone[0])}&ndash;{_n(zone[1])}" if zone else _n(entry)
        e_note = f"R from {_n(entry)}" if zone else "zone" if False else ""
        # SQUARE CELLS, THREE UP (2026-09-16). Entry / Stop / Score, then the three
        # targets with R under each. The label/value/sub shape is the mock's; the
        # numbers are the same ones the old level rows carried.
        def _cell(k, v, s=''):
            return (f'        <div class="cell"><span class="k">{k}</span>'
                    f'<span class="v">{v}</span><span class="s">{s}</span></div>\n')
        sc = c.get('score')
        rows = (_cell('Entry', e_cell, e_note or ('zone' if zone else ''))
                + _cell('Stop', _n(sl), f'{_n(risk)} pts')
                # A hand-written card carries no score, so this cell read "SCORE —" on every
                # card and spent a third of the row saying nothing. Rank is what it was
                # actually conveying in the sub-line. (2026-09-17)
                + (_cell('Score', f'{float(sc):.1f}', f'rank {i + 1}') if sc is not None
                   else _cell('Rank', f'{i + 1}', f'of {len(CARDS)}')))
        for k, t in enumerate(tps, 1):
            dist = abs(t - entry)
            r = f'{dist / risk:.1f}R' if risk else '—'
            rows += _cell(f'T{k}', _n(t), r)
        attrs = (f' data-side="{side}" data-entry="{entry:g}" data-sl="{sl:g}"'
                 f' data-tp="{tps[0]:g}"' if tps else f' data-side="{side}" data-entry="{entry:g}" data-sl="{sl:g}"')
        if tps:
            attrs += ' data-tps="' + ",".join(f"{t:g}" for t in tps) + '"'
        if zone:
            attrs += f' data-zone-lo="{zone[0]:g}" data-zone-hi="{zone[1]:g}"'
        # exec="close" on a card dict: the card is executed on 5-minute closes -- the
        # reactionary rule measured 2026-09-15 for CONTINUATION cards (break / acceptance):
        # 73% win at the same expectancy as a touch entry, and half as many "invalidated,
        # then ran anyway". Fades stay touch cards -- the same rule tested worse for them.
        # setup_track.track_close and the command center read it back from this attribute.
        if c.get("exec") == "close":
            attrs += ' data-exec="close"'
        # The gate has its own strip under the title, so the engine's ". GATED: ..."
        # suffix is removed from the sub-line rather than printed twice. Only the
        # DISPLAY changes; the card dict and plan_cards.json keep the full string.
        tag = (c.get("tag", "") or "").split(". GATED:")[0]
        out.append(
            f'    <div class="pl{" pri" if pri else ""}"{attrs}>\n'
            f'      <div class="ph"><div><h3>{c.get("name", "Setup")}</h3>\n'
            f'        <div class="tg">{tag}</div></div>\n'
            f'        <div class="cv">{rank}</div></div>\n'
            + (f'      <div class="gate">Gated &middot; needs {_n(c["gate"])} to '
               f'{"fail" if side == "short" else "hold"} first</div>\n' if c.get('gate') else '')
            + f'      <div class="cells">\n{rows}      </div>\n'
            f'      <div class="wy">{c.get("why", "")}</div>\n'
            f'    </div>')
    return "\n".join(out)


# ---------------------------------------------------------------------------
# WHAT CHANGED since the previous build. Generated from archive/history.csv, not
# written by hand -- this is the one block on the page that must never be able
# to go stale, because its whole job is to describe drift. build.py runs BEFORE
# archive.py, so the last row in the file is genuinely the previous report.
#
# It reports the three drivers separately. A flip can move because the BOOK is a
# new vintage, because TIME TO EXPIRY changed, or because the plan is for a
# different session -- and blurring those together is how "the flip moved 80
# points" gets read as news when it is bookkeeping.
def _changes():
    import csv
    hist = pathlib.Path('archive/history.csv')
    if not hist.exists():
        return ""
    try:
        rows = list(csv.DictReader(hist.open(encoding='utf-8')))
    except Exception:
        return ""
    if not rows:
        return ""
    # Baseline = the last build of the SAME kind when there is one, else the
    # previous build. A dozen intraday rows a day meant the morning sheet always
    # compared itself with yesterday's 12:45 build -- different hours, different
    # rail scale, a different reference basis -- and every row read as news.
    base = lambda k: (k or "").split("-")[0]
    same = [r for r in rows if base(r.get("kind")) == KIND]
    p = same[-1] if same else rows[-1]

    def f(r, k):
        try:
            return float(r[k])
        except (TypeError, ValueError, KeyError):
            return None

    items, notes = [], []

    def row(label, was, now, dp=0, tol=0.5, unit=""):
        if was is None or now is None or abs(now - was) < tol:
            return
        items.append((label, f"{was:,.{dp}f}{unit}", f"{now:,.{dp}f}{unit}",
                      f"{now - was:+,.{dp}f}{unit}"))

    # the drivers, first -- they explain everything below them
    pv, nv = (p.get('oi_vintage') or None), T.get('oi_vintage', 'final')
    pb, nb = p.get('settle'), SETTLE
    if pb != nb or (pv is not None and pv != nv):
        # A blank vintage means the column predates that row -- say "unrecorded"
        # rather than silently attributing this build's vintage to it.
        items.append(("Book", f"{pb or '—'} {pv or '(vintage unrecorded)'}",
                      f"{nb or '—'} {nv}", "new vintage"))
        notes.append("A different book moves the flips and the net on its own. "
                     "That is the first thing to account for before reading any "
                     "level below as news.")
    ph, nh = f(p, 'hours'), HRS
    if ph is not None and nh is not None and abs(nh - ph) > 0.05:
        items.append(("Time to expiry", f"{ph:g}h", f"{nh:g}h", f"{nh - ph:+.2f}h"))
        notes.append("Time to expiry moves the flips too, and it pushes the near "
                     "and full books hardest.")
    if base(p.get('kind')) != KIND:
        items.append(("Plan", p.get('kind') or '—', KIND, "different plan"))
    pb_, nb_ = (p.get('iv_basis') or 'calendar'), IVBASIS
    if pb_ != nb_:
        items.append(("Day count", pb_, nb_, "different convention"))
        notes.append("The vol and every Greek are on a different time basis from the previous "
                     "build (calendar days before 2026-09-05, trading sessions after). IV, the "
                     "expected move, the rails and the 0DTE gamma all move on that alone; compare "
                     "the flips, which are far less sensitive to it.")
    ps, ns = f(p, 'rail_scale'), RSCALE
    if ps is not None and abs(ns - ps) > 0.01:
        items.append(("Rail scale", f"{ps:g}×", f"{ns:g}×", "different band"))
        notes.append("The rails are a different fraction of the expected move, so "
                     "they moved on their own -- that is the band, not the market.")
    if (p.get('session') or '') not in ('', RDATE):
        items.append(("Session", p.get('session'), RDATE or '—', "different day"))

    # 'reference' holds the settle on a premarket build and a live print on an
    # intraday one -- same column, two quantities. Label it for what it is.
    row(SPOTLAB, f(p, 'reference'), SPOT)
    row("Last print", f(p, 'last_print'), LAST)
    row("ATM IV", f(p, 'iv_atm'), IVP, 2, 0.02, "%")
    row("Expected move", f(p, 'em'), EM, 0, 1.0)
    for k, lab in (("0dte", "0DTE flip"), ("near", "Near flip"), ("full", "Full flip")):
        row(lab, f(p, f'flip_{k}'), _flip(lab.split()[0].upper() if k != "0dte" else "0DTE"),
            0, 1.0)
    for k, lab in (("0dte", "Net 0DTE"), ("near", "Net near"), ("full", "Net full")):
        was, now = f(p, f'net_{k}'), _net({"0dte": "0DTE", "near": "NEAR",
                                           "full": "FULL"}[k])
        if was is not None and now is not None and abs(now - was) >= 1:
            flip = " · sign inverts" if was * now < 0 else ""
            items.append((lab, f"{was:+,.0f}", f"{now:+,.0f}",
                          f"{now - was:+,.0f}{flip}"))
            if flip and lab == "Net full":
                notes.append("<b>Net gamma has changed sign.</b> The previous sheet "
                             "described the opposite regime; do not carry any read "
                             "across from it.")
    row("Rail low", f(p, 'rail_lo'), T.get('rail_lo'))
    row("Rail high", f(p, 'rail_hi'), T.get('rail_hi'))
    row("Net VEX", f(p, 'vex_total'), VEXTOT)

    if not items:
        return ('<div class="note chg0" style="margin-bottom:26px"><b>Nothing moved.</b> '
                'Every figure is within tolerance of the previous build'
                + (f' ({_pt(p.get("built_at"))})' if _pt(p.get("built_at")) else '')
                + '.</div>')

    body = "".join(
        f'<div class="chr"><div class="chk">{lab}</div>'
        f'<div class="chv mono">{was}</div>'
        f'<div class="cha mono">&rarr;</div>'
        f'<div class="chv mono">{now}</div>'
        f'<div class="chd mono">{dl}</div></div>' for lab, was, now, dl in items)
    prev = _pt(p.get('built_at'))
    return (f'<div class="chg"><div class="chgh">What changed'
            f'<span> · since {prev or "the previous build"}'
            f'{" · " + (p.get("kind") or "") if p.get("kind") else ""}</span></div>'
            f'{body}</div>'
            + ("".join(f'<div class="note w" style="margin:14px 0 26px">{n}</div>'
                       for n in notes) if notes else '<div style="height:26px"></div>'))


CHANGES = _changes()

HEAD = '''<title>Basilisk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700;800&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@500;600&display=swap">
<style>
:root{
  --ground:#F2F3F1;--panel:#FBFBFA;--panel2:#E9EBE7;--ink:#14181A;--ink2:#4A5350;--ink3:#7C8783;
  --rule:#D2D6D0;--rule2:#BFC5BE;--brass:#9A6E1F;--brassx:#EFE2C6;
  --neg:#A8412C;--negx:#F0D8D1;--pos:#1F6E62;--posx:#CFE3DE;--warn:#8A6A12;--vio:#5B4B8A;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#101413;--panel:#171C1B;--panel2:#1F2624;--ink:#E9EDEA;--ink2:#A8B2AE;--ink3:#77837E;
  --rule:#2A322F;--rule2:#3A443F;--brass:#D6A64A;--brassx:#3A3018;
  --neg:#E07A5F;--negx:#3A231C;--pos:#5FBFAC;--posx:#123029;--warn:#D6B04A;--vio:#A395D6;
}}
:root[data-theme="dark"]{
  --ground:#101413;--panel:#171C1B;--panel2:#1F2624;--ink:#E9EDEA;--ink2:#A8B2AE;--ink3:#77837E;
  --rule:#2A322F;--rule2:#3A443F;--brass:#D6A64A;--brassx:#3A3018;
  --neg:#E07A5F;--negx:#3A231C;--pos:#5FBFAC;--posx:#123029;--warn:#D6B04A;--vio:#A395D6;
}
*{box-sizing:border-box}
body{background:var(--ground);color:var(--ink);font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1400px;margin:0 auto;padding:22px 16px 56px;display:flex;flex-direction:column;gap:26px}
.mono{font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
h1,h2,h3{font-family:Archivo,Helvetica,Arial,sans-serif;margin:0;text-wrap:balance}
.mast{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:18px;border-bottom:2px solid var(--ink);padding-bottom:14px}
.mast h1{font-size:clamp(28px,4.4vw,42px);font-weight:800;letter-spacing:-.022em;line-height:1.02}
.mast h1 span{color:var(--brass)}
.mast .sub{color:var(--ink2);font-size:13.5px;margin-top:6px;max-width:60ch}
.stamp{text-align:right;font-size:11.5px;color:var(--ink3);letter-spacing:.06em;text-transform:uppercase;line-height:1.7}
.stamp b{color:var(--ink2);font-weight:600}
.chg{border:1px solid var(--rule);border-radius:3px;background:var(--panel);overflow:hidden;margin-bottom:26px}
.chgh{font:600 11px/1 Archivo,sans-serif;letter-spacing:.09em;text-transform:uppercase;color:var(--ink2);
 padding:11px 14px;border-bottom:1px solid var(--rule);background:var(--panel2)}
.chgh span{font-weight:400;text-transform:none;letter-spacing:0;color:var(--ink3)}
.chr{display:grid;grid-template-columns:minmax(120px,1.4fr) 1fr 22px 1fr 1.1fr;gap:8px;align-items:baseline;
 padding:7px 14px;border-bottom:1px solid var(--rule)}
.chr:last-child{border-bottom:0}
.chk{font:500 12px/1.3 'IBM Plex Sans',sans-serif;color:var(--ink2)}
.chv{font-size:12.5px;color:var(--ink)}
.cha{color:var(--ink3);text-align:center}
.chd{font-size:12.5px;color:var(--brass);font-weight:600}
@media(max-width:640px){.chr{grid-template-columns:1fr 1fr 18px 1fr;font-size:11.5px}.chd{grid-column:1/-1;padding-left:0}}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(146px,1fr));border:1px solid var(--rule);background:var(--panel);border-radius:3px;overflow:hidden}
.st{padding:13px 15px;border-right:1px solid var(--rule);text-align:center}
.st:last-child{border-right:0}
.st .k,.st .v,.st .n{text-align:center;padding-right:0!important}
.st .k{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--ink3);font-weight:600}
.st .v{font-size:21px;font-weight:600;margin-top:3px;letter-spacing:-.01em}
.st .n{font-size:11.5px;color:var(--ink2);margin-top:1px}
.st.a .v{color:var(--warn)}.st.b .v{color:var(--brass)}.st.c .v{color:var(--neg)}
.sec{display:flex;flex-direction:column;gap:14px}
.sech{display:flex;align-items:baseline;gap:12px;border-bottom:1px solid var(--rule2);padding-bottom:7px}
.sech h2{font-size:17px;font-weight:700;letter-spacing:-.01em}
.sech .hint{font-size:12px;color:var(--ink3);margin-left:auto;text-align:right}
/* trinity ladder */
.lad{border:1px solid var(--rule);background:var(--panel);border-radius:3px;padding:0 0 8px;overflow-x:auto}
.grid{min-width:900px;display:grid;grid-template-columns:74px 1fr 42px 1fr 44px 1fr 46px 1fr 40px 26px;align-items:center}
.hd{display:contents}
.hd>div{position:sticky;top:0;background:var(--panel2);border-bottom:1px solid var(--rule2);padding:8px 0 7px;
  font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);font-weight:700;text-align:center}
.hd .lft{text-align:right;padding-right:10px}
.hd .sp2{grid-column:span 2}
.r{display:contents}
.r>*{padding:2px 0}
.r.hi .k{color:var(--ink);font-weight:700}
.k{font-size:12.5px;color:var(--ink2);text-align:right;padding-right:10px!important;font-weight:500}
.b{position:relative;height:15px;background:linear-gradient(var(--rule),var(--rule)) center/1px 100% no-repeat;margin:0 4px}
.b i{position:absolute;top:2px;height:11px;display:block;border-radius:1px}
.b i.n{right:50%;background:var(--neg)}
.b i.p{left:50%;background:var(--pos)}
.b i.z{display:none}
.b.vx i.n{background:var(--vio);opacity:.55}
.b.vx i.p{background:var(--vio)}
.v{font-size:11.5px;text-align:right;padding-right:6px!important;font-weight:500}
.v.p{color:var(--pos)}.v.n{color:var(--neg)}.v.z{color:var(--ink3);opacity:.4}
.cf{text-align:center}
.cf b{display:inline-block;width:17px;height:17px;line-height:17px;border-radius:50%;font-size:10px;font-weight:700}
.cf .c3{background:var(--brass);color:var(--ground)}
.cf .c2{background:var(--brassx);color:var(--brass);border:1px solid var(--brass)}
.cf .c0{background:none}
.mk{display:contents}
.mk .mkl{grid-column:1/2;text-align:right;padding-right:10px!important;font-size:10px;font-weight:700;color:var(--ink)}
.mk .mkr{grid-column:2/11;position:relative;height:0;border-top:1px dashed var(--ink3)}
.mk .mkr span{position:absolute;top:-8px;left:10px;background:var(--panel);padding:0 8px;font-size:10px;
  letter-spacing:.06em;text-transform:uppercase;font-weight:700;color:var(--ink2)}
.mk.spot .mkr{border-top:2px solid var(--brass)}.mk.spot .mkl,.mk.spot .mkr span{color:var(--brass)}
/* LAST is where price IS; SETTLE is the anchor. They used to share a class and
   render identically, which collapsed them into one rule whenever the overnight
   round-tripped back to the settle. Ink, not brass, and heavier. */
.mk.last .mkr{border-top:3px solid var(--ink)}
.mk.last .mkl,.mk.last .mkr span{color:var(--ink)}
.mk.last .mkr span{background:var(--ink);color:var(--panel);border-radius:2px}
.mk.zero .mkr{border-top:2px dashed var(--warn)}.mk.zero .mkl,.mk.zero .mkr span{color:var(--warn)}
.mk.near .mkr,.mk.full .mkr{border-top:1px dashed var(--warn);opacity:.85}
.mk.near .mkl,.mk.full .mkl,.mk.near .mkr span,.mk.full .mkr span{color:var(--warn)}
.mk.rail .mkr{border-top:1px dashed var(--ink3)}
/* ladder views (2026-09-13): the radios sit before .sech and .lad in the section; CURRENT by default */
.lad .r:not(.cur)>*{display:none}
#lv-rel:checked~.lad .r:not(.rel)>*{display:none}
#lv-rel:checked~.lad .r.rel>*{display:block}
#lv-full:checked~.lad .r>*{display:block}
.lvr{position:absolute;opacity:0;width:0;height:0;pointer-events:none}
.lvs{display:inline-flex;border:1px solid var(--rule2);border-radius:3px;overflow:hidden;vertical-align:middle;margin-bottom:4px}
.lvs label{font:600 10.5px/1 Archivo,sans-serif;letter-spacing:.06em;text-transform:uppercase;padding:6px 9px;cursor:pointer;color:var(--ink3);border-right:1px solid var(--rule2);background:var(--panel)}
.lvs label:last-child{border-right:0}
#lv-cur:checked~.sech .lvs label[for=lv-cur],#lv-rel:checked~.sech .lvs label[for=lv-rel],#lv-full:checked~.sech .lvs label[for=lv-full]{background:var(--brassx);color:var(--brass)}
.b i.ov{outline:1px dashed var(--ink3);outline-offset:1px}
/* generic */
.note{font-size:13px;color:var(--ink2);background:var(--panel);border:1px solid var(--rule);border-left:3px solid var(--brass);border-radius:2px;padding:13px 16px}
.note.w{border-left-color:var(--neg)}
.note.v{border-left-color:var(--vio)}
.note b{color:var(--ink)}
ul.t{margin:6px 0 0;padding-left:19px;display:flex;flex-direction:column;gap:5px}
.plans{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.pl{background:var(--panel);border:1px solid var(--rule);border-radius:3px;display:flex;flex-direction:column}
.pl.pri{border-color:var(--pos)}
.pl .ph{padding:13px 16px;border-bottom:1px solid var(--rule);display:flex;align-items:flex-start;justify-content:space-between;gap:10px}
.pl h3{font-size:15px;font-weight:700}
.pl .tg{font-size:12px;color:var(--ink2);margin-top:4px}
.pl .cv{font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;font-weight:700;color:var(--ink3);white-space:nowrap;padding-top:2px}
.lv{display:grid;grid-template-columns:auto 1fr auto auto;gap:0 12px;padding:12px 16px;font-size:12.5px}
.lv div{padding:3.5px 0;border-bottom:1px solid var(--rule)}
.lv div:nth-last-child(-n+4){border-bottom:0}
.lv .a{color:var(--ink3);font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;font-weight:600;align-self:center}
.lv .b2{font-weight:600;text-align:left}
.lv .c,.lv .d{text-align:right;color:var(--ink2);font-size:12px}
.lv .d{font-weight:600;color:var(--ink)}
.pl .wy{padding:11px 16px 14px;border-top:1px solid var(--rule);font-size:12.5px;color:var(--ink2);background:var(--panel2);margin-top:auto}
.pl .wy b{color:var(--ink)}
.foot{font-size:12px;color:var(--ink3);border-top:1px solid var(--rule);padding-top:14px;line-height:1.7}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
/* ---- the 2026-09-16 sheet design -------------------------------------- */
/* The alerts band and the gate strip need a raised ground and a warning pair;
   the existing palette had neither. Defined here in all three theme blocks so
   they follow the page rather than sitting light-on-light in the dark theme. */
:root{--raised:#eef1f6;--wa:#854f0b;--wabg:#faeeda;--negbg:rgba(226,113,90,.10)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --raised:#1e2530;--wa:#efab27;--wabg:#3a2506;--negbg:rgba(240,153,123,.12)}}
:root[data-theme="dark"]{--raised:#1e2530;--wa:#efab27;--wabg:#3a2506;--negbg:rgba(240,153,123,.12)}
/* Ported from the artifact Mark iterated live. The card WRAPPERS (.plans, .pl,
   h3, .tg, .cv, .wy) and the data-* attributes are a parsing contract for
   setup_track and the command center -- restyled here, never renamed. */
.cond{font-family:"IBM Plex Sans Condensed","IBM Plex Sans",system-ui,sans-serif}
.call{font-family:Archivo,Helvetica,Arial,sans-serif;font-size:19px;font-weight:600;margin:16px 0 2px;letter-spacing:-.01em}
.call.up{color:var(--pos)}.call.down{color:var(--neg)}
.call+.sub{font-size:12.5px;color:var(--ink2);margin-bottom:14px}

.alerts{border:.5px solid var(--rule);border-radius:8px;overflow:hidden;margin:0 0 22px}
.ah{display:flex;gap:8px;align-items:baseline;padding:6px 13px 5px;border-bottom:.5px solid var(--rule);background:var(--raised)}
.ah .t{font-family:"IBM Plex Sans Condensed","IBM Plex Sans",sans-serif;font-size:10px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--ink3)}
.al{display:grid;grid-template-columns:86px 1fr;gap:0 12px;padding:6px 13px;border-bottom:.5px solid var(--rule2);align-items:baseline}
.al:last-child{border-bottom:0}
.al.red{background:var(--negbg,rgba(226,113,90,.10))}
.al .tm{font-family:"IBM Plex Mono",monospace;font-size:12.5px;font-weight:500;color:var(--ink);font-variant-numeric:tabular-nums}
.al.red .tm{color:var(--neg)}
.al .ct{font-family:"IBM Plex Mono",monospace;font-size:10px;color:var(--ink3);display:block;line-height:1.2}
.al .wt{font-size:12.5px;font-weight:500}
.al.red .wt{color:var(--neg)}
.al .nt{font-size:11.5px;color:var(--ink2)}

/* cards: the mock's cell grid, over the existing .pl wrapper */
/* Four cards read best two-up: 310px let four sit in a row at 1400px and the last one
   strand itself on a second row at a narrower window. min() keeps a narrow window to one
   column instead of overflowing. (2026-09-17) */
.plans{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr));gap:10px}
.pl{display:flex;flex-direction:column;border-top:3px solid var(--rule)}
.pl.pri{border-top-color:var(--pos)}
/* The title block must be allowed to shrink or it squeezes the rank badge into an ellipsis. */
.pl .ph{align-items:flex-start;gap:12px}
.pl .ph>div:first-child{min-width:0}
.pl .cv{flex:none}
.pl .gate{font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--wa,#854f0b);background:var(--wabg,rgba(239,171,39,.13));padding:4px 13px}
/* NO margin-top:auto HERE. .wy already carries one, and in a flex column the FIRST auto
   margin absorbs all the free space -- so on a card with a short tag the slack opened as a
   blank band between the header and the cells instead of at the bottom. The why-block is the
   one that should stretch, so it gets flex:1 and the cells sit under the header. (2026-09-17) */
.cells{display:grid;grid-template-columns:repeat(3,1fr);border-top:.5px solid var(--rule)}
.cell{padding:7px 12px;border-right:.5px solid var(--rule);border-bottom:.5px solid var(--rule)}
.cell:nth-child(3n){border-right:0}
.cell:nth-last-child(-n+3){border-bottom:0}
.cell .k{font-family:"IBM Plex Sans Condensed","IBM Plex Sans",sans-serif;font-size:9.5px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--ink3);display:block}
.cell .v{font-family:"IBM Plex Mono",monospace;font-size:15px;font-weight:500;display:block;line-height:1.3;font-variant-numeric:tabular-nums}
.cell .s{font-family:"IBM Plex Mono",monospace;font-size:10.5px;color:var(--ink3)}
.pl .wy{flex:1 1 auto;margin-top:0;line-height:1.62}
.held{font-size:12px;color:var(--ink3);font-family:"IBM Plex Mono",monospace;margin-top:10px}

/* VWAP bands against the book */
.bm{border:.5px solid var(--rule);border-radius:8px;overflow:hidden;margin-bottom:12px}
.bmr{display:grid;grid-template-columns:104px 84px 128px 1fr;gap:0 12px;padding:6px 13px;border-bottom:.5px solid var(--rule2);align-items:baseline;font-size:12px}
.bmr:last-child{border-bottom:0}
.bmr .l{font-weight:500}
.bmr .p{font-family:"IBM Plex Mono",monospace;font-size:12.5px;font-variant-numeric:tabular-nums}
.bmr .d{font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--ink3);font-variant-numeric:tabular-nums}
.bmr .r{color:var(--ink2);font-size:11.5px}
.bmr.positive .l,.bmr.positive .p{color:var(--pos)}
.bmr.negative .l,.bmr.negative .p{color:var(--neg)}
@media(max-width:620px){
  .bmr{grid-template-columns:92px 76px 1fr;gap:0 8px}
  .bmr .r{grid-column:1/-1;color:var(--ink3)}
  .al{grid-template-columns:72px 1fr;gap:0 8px}
}
</style>
'''

BODY = '''
<div class="wrap">
__BANNER__
<header class="mast">
  <div>
    __MAST__
    <div class="sub">One price axis, three books. Dealer gamma across the 0DTE, near-term and full expiry
    stacks, with vanna exposure as the regime read.</div>
  </div>
  <div class="stamp">
    __STAMP__
  </div>
</header>

<div class="strip">__STRIP__</div>
__CHANGES__
__READ__
__ALERTS__

<div class="note">
  __LEAD__
</div>
</div>

<section class="sec">
__CAVEAT__
  </div>
</section>
<section class="sec">
  <div class="sech"><h2>Gameplan</h2><div class="hint">structural levels, not entry signals</div></div>
    <div class="plans">

__CARDS__

  </div>
__HELD__

  </div>
</section>
__VWAPBOOK__
<section class="sec">
  <input class="lvr" type="radio" name="lv" id="lv-cur" checked><input class="lvr" type="radio" name="lv" id="lv-rel"><input class="lvr" type="radio" name="lv" id="lv-full">
  <div class="sech">
    <h2>Teddy Analysis</h2>
    <div class="hint"><span class="lvs"><label for="lv-cur">Current · __LVCUR__</label><label for="lv-rel">Relevant · __LVREL__</label><label for="lv-full">Full book · __LVFULL__</label></span><br>
    current = strikes carrying weight inside ±1.7 EM · relevant = strikes a structural stop can anchor on (≥ ¼ of a book's largest gamma within 1 EM) plus every wall at ≥ ¼ of its column's maximum within ±2 EM · full = every strike with exposure; __LVBEYOND__ of gross gamma sits beyond ±2 EM<br>
    bars scale within each column (dashed edge = clipped at the column) · ⬤ = material in 2 or 3 of the disjoint books (0DTE · 1–9 DTE · 10+ DTE)<br>
    solid dark rule = LAST, where price is · brass rule = SETTLE, what the book is priced on<br>
    gamma in futures per 1% move · VEX in futures per 1 vol point</div>
  </div>
  <div class="lad"><div class="grid">
    <div class="hd">
      <div class="lft">Strike</div><div class="sp2">0DTE · today</div><div class="sp2">Near · ≤9 DTE</div>
      <div class="sp2">Full book · __NEXP__ exp</div><div class="sp2">VEX · vanna</div><div>±</div>
    </div>
__LADDER__
  </div></div>
    <div class="note v">
    __VEX__
  </div>
  </div>
</section>
__STATEPANEL__
<footer class="foot">
  <b>Method.</b> Three books on one axis: 0DTE is the contract expiring today, Near is every expiry to 9 DTE,
Full is all listed expiries (the ⬤ score alone uses three disjoint books: 0DTE, 1–9 DTE, 10+ DTE). Open interest and settlement prices come from Databento GLBX.MDP3 (CME's licensed
redistributor) for the stated settle date. <b>Implied vol is solved per strike from those settlement prices</b>, so
the smile is measured rather than borrowed and nothing is integer-rounded — the gamma column is computed under
Black-76 at each contract's actual time to expiry, with each expiry priced against its own underlying future so the
NQU6/NQZ6 basis is preserved. Bars are net gamma, call minus put, and each column is scaled to its own maximum, so
lengths compare within a column and not across. Flips are the futures price where Σ Γᵢ(F)·(OI_call,i − OI_put,i)
crosses zero, reported against the reference future's own settle. VEX is Σ vanna·(OI_call,i − OI_put,i) per vol
point over the full book. Time is counted in <b>business time</b> — sessions, variance-weighted, weekends and holidays zero — for the vol solve and every Greek alike, so the one-session rails S × ATM IV ÷ √252 are consistent with the vol they use. Books settling into a different future (NQZ6 and later) are <b>basis-shifted onto the reference axis</b> before they are plotted, so a strike lands where its hedging flow reaches this contract's tape. <b>&Gamma; near price</b> is the same net gamma
summed only over strikes inside the band actually drawn (±1 expected move for a full day, scaled down for a
part-session) around the live price — the chain-wide totals are a property of
the book alone and so do not move when price does, while this one does, which is what makes it the figure to
watch across rebuilds on an unchanged book.<br><br>
<b>Caveats.</b> The dealer convention — long calls, short puts — is an assumption about positioning, not an
observation; every sign here inverts if it is wrong. The open interest is the prior settle, so the current session's
own build is missing, which matters most for the 0DTE book. The confluence dots describe which of three disjoint
books (0DTE / 1–9 DTE / 10+ DTE) a strike appears in, <b>not a ranking</b>, and the setups above are ordered by
gamma structure rather than by that score. This is positioning analysis, not advice.
</footer>
</div>
'''
# ---------------------------------------------------------------------------
# OPTIONAL market-state panel (2026-09-10). Renders ONLY when state.json exists.
# state_panel.render() returns "" for a missing file, malformed JSON, a missing
# key or any other problem, and never raises. That property is the whole point:
# this file runs unattended inside autopull four times a day, and an optional
# panel must not be able to take a sheet build down.
#
# With state.json absent -- the default -- the sheet is BYTE-IDENTICAL to what it
# was before this hook existed. Produce one with:  python engine.py --from-trinity
try:
    import state_panel
    STATEPANEL = state_panel.render(sheet_settle=SETTLE, sheet_report=RDATE)
except Exception:
    STATEPANEL = ""
if STATEPANEL:
    STATEPANEL = ('<section class="sec"><div class="sech">'
                  + STATEPANEL + '</div></section>')


# The session belongs in the title too. Two sheets a day both called
# 'Basilisk <date>' are indistinguishable in a browser tab or the gallery.
#
# AND SO DOES THE BUILD TIME on a mid-session sheet (2026-09-10). There can be a
# dozen intraday builds in one day and comparing them is the point, but they all
# carried the same 'Rest of session' label, so the browser tab, the gallery and
# the published artifact could not tell 09:02 from 12:40. The time comes from
# ASOF -- the moment the plan is PRICED to -- not from the clock, so a rebuild of
# an old plan keeps that plan's name. PT, like every other clock reading here.
_T_HHMM = _pt(ASOF, '%H%M', suffix='') if (KIND == 'intraday' and ASOF) else ''
TITLE = ('Basilisk ' + (RDATE or '')
         + ('' if RLABEL in ('1D', None) else ' ' + RLABEL)
         + (' ' + _T_HHMM if _T_HHMM and _T_HHMM != '—' else '')).strip() or 'Basilisk'
html = (HEAD.replace('<title>Basilisk</title>', f'<title>{TITLE}</title>')
        + BODY.replace('__LADDER__', lad).replace('__BANNER__', BANNER)
        .replace('__MAST__', MAST).replace('__STAMP__', STAMP)
        .replace('__STRIP__', STRIP).replace('__CHANGES__', CHANGES)
        .replace('__LEAD__', LEAD)
        .replace('__READ__', _read_head()).replace('__ALERTS__', _alerts())
        .replace('__HELD__', _held()).replace('__VWAPBOOK__', _vwapbook())
        .replace('__CARDS__', _cards()).replace('__VEX__', VEX_NOTE).replace('__CAVEAT__', _caveat())
        # The ladder header used to hardcode "16 exp"; the book has since grown to
        # 28 and the header quietly disagreed with the strip beside it.
        .replace('__NEXP__', _n(NEXP))
        .replace('__LVCUR__', f'{LVCUR}').replace('__LVREL__', f'{LVREL}')
        .replace('__LVFULL__', f'{LVFULL}').replace('__LVBEYOND__', LVBEYOND)
        # the newline goes with the token, so an absent panel leaves no blank line
        .replace('\n__STATEPANEL__\n', ('\n' + STATEPANEL + '\n\n') if STATEPANEL else '\n\n'))
for tok in ('__LADDER__', '__BANNER__', '__MAST__', '__STAMP__', '__STRIP__',
            '__CHANGES__', '__NEXP__', '__LEAD__', '__CARDS__', '__VEX__',
            '__READ__', '__ALERTS__', '__HELD__', '__VWAPBOOK__',
            '__CAVEAT__', '__STATEPANEL__', '__LVCUR__', '__LVREL__', '__LVFULL__', '__LVBEYOND__'):
    assert tok not in html, f"{tok} left unsubstituted"
open('trinity.html', 'w', encoding='utf-8').write(html)
print(f"built {len(html):,} chars   ref {_n(SPOT,1)}   settle {SETTLE}"
      f"   flips 0DTE {_n(_flip('0DTE'))} / NEAR {_n(_flip('NEAR'))} / FULL {_n(_flip('FULL'))}"
      + ("   [STALE NARRATIVE BANNER SHOWN]" if DRIFT else ""))
