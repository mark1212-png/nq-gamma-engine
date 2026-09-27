#!/usr/bin/env python3
"""
gen.py -- ladder_frag.html from trinity.json.

Everything here USED to be hardcoded: the strike list, and the marker lines for
the three flips, the reference and the two rails. That meant a fresh data pull
updated trinity.json and the sheet did not move -- the reference price on the
page stayed at whatever it was the day the literals were typed. So the strikes
and the markers are both derived now, and the only hand-written thing left on
the page is the prose (which build.py stamps and checks).
"""
import json

d = json.load(open('trinity.json', encoding='utf-8'))
g0 = d['panels']['0DTE']['gamma']
gn = d['panels']['NEAR']['gamma']
gf = d['panels']['FULL']['gamma']
vex = d.get('vex', {})
conf = d.get('confluence', {})
spot = d['spot']
em = d.get('em') or spot * (d.get('iv_atm') or 0.17) / 252 ** 0.5


def num(m, k):
    return float(m.get(str(float(k)), 0) or 0)


# ---- which strikes get a row -------------------------------------------------
# A row earns its place by carrying weight in some book, or by being a round
# number close enough to price that its absence would look like an omission.
# The window is set by the expected move, so a quiet day shows a tight ladder
# and a wild one shows a wide one, instead of a fixed list that is sometimes
# far too narrow and sometimes mostly zeros.
HALF = max(1.7 * em, 380)
lo, hi = spot - HALF, spot + HALF
# Keep the live print inside the frame. The window is centred on the SETTLE,
# which is right -- the rails and the solved vol both belong to that instant --
# but it means a large overnight move walks price toward the edge of the ladder,
# and a big enough one would put it off the end entirely, leaving the marker
# floating above rows it has nothing to do with. Extend (never shrink) so the
# print always keeps a block of strikes on both sides of it.
_lp = d.get('last_print')
if _lp:
    lo = min(lo, _lp - 0.6 * HALF)
    hi = max(hi, _lp + 0.6 * HALF)

cand = {float(k) for k in set(g0) | set(gn) | set(gf) | set(vex)}
ALLK = sorted(cand)                      # every strike carrying exposure -- the FULL view
cand = {k for k in cand if lo <= k <= hi}
cand |= {float(k) for k in range(int(lo // 100 * 100), int(hi // 100 * 100) + 100, 100)
         if lo <= k <= hi}


def weight(k):
    return max(abs(num(g0, k)) / 5.0, abs(num(gn, k)) / 12.0,
               abs(num(gf, k)) / 25.0, abs(num(vex, k)) / 8.0,
               1.01 if k % 100 == 0 and abs(k - spot) <= 1.2 * em else 0.0)


KEYS = sorted(k for k in cand if weight(k) >= 1.0)
if len(KEYS) > 40:                       # keep the sheet readable
    KEYS = sorted(sorted(KEYS, key=weight, reverse=True)[:40])
if not KEYS:
    KEYS = sorted({float(round((spot + o) / 50) * 50) for o in range(-400, 401, 50)})

M0 = max((abs(num(g0, k)) for k in KEYS), default=0) or 1
MN = max((abs(num(gn, k)) for k in KEYS), default=0) or 1
MF = max((abs(num(gf, k)) for k in KEYS), default=0) or 1
MV = max((abs(num(vex, k)) for k in KEYS), default=0) or 1

# ---- marker lines, all read from the data ------------------------------------
marks = {}


def mark(v, cls, label):
    if v is None:
        return
    k = int(round(v))
    # Format the label HERE, one placeholder per label. Two levels within a point of
    # each other are merged into one line below, and formatting the merged label
    # afterwards with a single argument raised IndexError (2026-09-15 book: the NEAR
    # flip 29,431.4 and the FULL flip 29,430.9 both round to 29,431).
    if '{' in label:
        label = label.format(k)
    if k in marks:                       # two levels within a point of each other
        cls0, lab0 = marks[k]
        marks[k] = (cls0, f"{lab0} · {label}")
    else:
        marks[k] = (cls, label)


def flip(name):
    h = ((d.get('panels') or {}).get(name) or {}).get('hvl') or []
    return h[0] if h else None


mark(flip('NEAR'), 'near', 'NEAR-BOOK FLIP {:,.0f}')
mark(flip('FULL'), 'full', 'FULL-BOOK FLIP {:,.0f}')
mark(flip('0DTE'), 'zero', '0DTE FLIP {:,.0f}')
RL = d.get('rail_label') or '1D'
mark(d.get('rail_hi'), 'rail', RL + ' MAX {:,.0f}')
mark(d.get('rail_lo'), 'rail', RL + ' MIN {:,.0f}')
# Same rule as build.SPOTLAB: on an intraday plan `spot` IS the live print --
# it is passed as --spot with no separate --last -- so labelling that rule
# SETTLE puts the wrong name on the most important line of the ladder. When the
# reference is the live price it also takes the LAST styling, because that is
# what it is.
_lastp = d.get('last_print')
_isref = _lastp is None and RL not in ('1D', None) and 'Globex' not in RL   # a holiday plan anchors on the settle
mark(spot, 'last' if _isref else 'spot',
     ('REFERENCE {:,.0f}' if _isref else 'SETTLE {:,.0f}'))
# Where price is now, when that is a different place from the settle. Without
# this line the ladder reads as though price were sitting on the settle, which
# after an overnight move it is not.
# ...and it gets its OWN class. It used to share 'spot' with the settle, so both
# drew as the same solid brass rule. That is fine when they are 100 points apart
# and unreadable when they are 20: on 2026-09-04 price came all the way back to
# the settle and the single most important fact on the sheet -- that it had --
# rendered as one thick line instead of two.
_last = d.get('last_print')
if _last and abs(_last - spot) >= 10:
    mark(_last, 'last', 'LAST {:,.0f}')
marks = {k: (c, l.format(k) if '{' in l else l) for k, (c, l) in marks.items()}

# ---- the three views (2026-09-13) --------------------------------------------
# CURRENT is KEYS, the selection above. RELEVANT is the strikes a structural stop
# can anchor on -- at least a quarter of a book's largest |gamma| within one EM, on
# any book, the same MATERIAL test command_center.py applies -- plus every wall at
# a quarter of its column's maximum anywhere in the book (30,000 on the 09-10 book
# is 1.6 EM out and the largest strike on the ladder). FULL is every strike carrying
# exposure. Every row is written and tagged; the radios build.py places above the
# ladder pick one view through CSS, CURRENT by default, so the Discord image and the
# command center's verbatim copy of this fragment do not change.
def _material():
    out = set()
    for m in (g0, gn, gf):
        w = {k: num(m, k) for k in ALLK if abs(k - spot) <= em}
        mx = max((abs(v) for v in w.values()), default=0) or 1
        out |= {k for k, v in w.items() if abs(v) >= 0.25 * mx}
    return out


def _walls():
    # Within two EM: a LEAP line 7 EM out can be the largest strike in the full book and
    # still have nothing to do with the session (32,200 and 27,700 on the 09-10 book).
    out = set()
    near = [k for k in ALLK if abs(k - spot) <= 2 * em]
    for m in (g0, gn, gf):
        mx = max((abs(num(m, k)) for k in near), default=0) or 1
        out |= {k for k in near if abs(num(m, k)) >= 0.25 * mx}
    return out


REL = _material() | _walls()
CUR, ALLSET = set(KEYS), set(ALLK)

rows = []
allk = sorted(set(int(k) for k in ALLK) | set(marks), reverse=True)
for k in allk:
    if k in marks:
        cls, lab = marks[k]
        rows.append(f'      <div class="mk {cls}"><div class="mkl mono">{k:,}</div>'
                    f'<div class="mkr"><span>{lab}</span></div></div>')
        if float(k) not in ALLSET:
            continue
    v0, vn = int(num(g0, k)), int(num(gn, k))
    vf, vv = int(num(gf, k)), num(vex, k)
    sc = int(conf.get(str(float(k)), 0) or 0)

    def bar(v, mx, cl=""):
        if not v:
            return '<i class="z"></i>'
        # The maxima are the CURRENT view's, so its bars are unchanged; a strike outside
        # it can out-size the column (VEX 35 at 31,700 against 20 at 30,000). Clamp at
        # the column edge and mark it, rather than let the bar run into the next column.
        w = abs(v) / mx * 50
        ov = " ov" if w > 50 else ""
        return f'<i class="{"p" if v > 0 else "n"}{cl}{ov}" style="width:{min(w, 50):.1f}%"></i>'

    dot = ('<b class="c3">3</b>' if sc == 3 else
           '<b class="c2">2</b>' if sc == 2 else '<b class="c0"></b>')
    sgn = lambda v: "p" if v > 0 else "n" if v < 0 else "z"
    view = (" cur" if float(k) in CUR else "") + (" rel" if float(k) in REL else "")
    rows.append(
        f'      <div class="r{" hi" if sc == 3 else ""}{view}"><div class="k mono">{k:,}</div>'
        f'<div class="b">{bar(v0, M0)}</div><div class="v mono {sgn(v0)}">{v0:+d}</div>'
        f'<div class="b">{bar(vn, MN)}</div><div class="v mono {sgn(vn)}">{vn:+d}</div>'
        f'<div class="b">{bar(vf, MF)}</div><div class="v mono {sgn(vf)}">{vf:+d}</div>'
        f'<div class="b vx">{bar(vv, MV)}</div><div class="v mono {sgn(vv)}">{vv:+.0f}</div>'
        f'<div class="cf">{dot}</div></div>')

open('ladder_frag.html', 'w', encoding='utf-8').write('\n'.join(rows))
print(f"{len(rows)} rows  ({len(KEYS)} current / {len(REL)} relevant / {len(ALLK)} full strikes, "
      f"{len(marks)} markers)  maxima {M0:.0f} {MN:.0f} {MF:.0f} {MV:.1f}")
