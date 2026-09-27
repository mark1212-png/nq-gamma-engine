"""
xladder.py -- four ladders (NQ, QQQ, SPY, SPX complex) on ONE axis, styled like the
TeddyAI sheet's ladder. Read-only over trinity outputs; touches nothing in the pipeline.

Axis: the NQ strike grid (25 pts). Every equity strike is placed at its NQ-equivalent
price  K_eq / F_eq * F_NQ  (moneyness-preserving -- the same mapping the cross-reference
uses; "moneyness needs no mapping") and bucketed to the nearest NQ grid line, gamma
summed. Bars scale WITHIN each instrument's column, exactly as the sheet's ladder does.
The numbers are that instrument's own contracts per 1% (dealer sign, C - P); the only
unit in which the four are comparable is $mm per 1%, printed in the footer from the
same books. No cross-instrument statement is made in the contract columns.
"""
import json, html, datetime as dt, sys, argparse, os
from pathlib import Path

#   python xladder.py --eq-date 2026-09-03 --session 2026-09-08 --out sheets/2026-09-08-xladder.html
# --eq-date is the QUOTE date of the equity trinity files (data/opra/<date>-<root>-trinity.json,
# written by opra_ladder.py); the NQ column is whatever trinity.json currently holds.
ap = argparse.ArgumentParser()
ap.add_argument("--eq-date", required=True)
ap.add_argument("--session", required=True, help="the RTH session the page is for (title only)")
ap.add_argument("--nq", default="trinity.json")
ap.add_argument("--out", default=None)
A = ap.parse_args()
ROOT = Path(os.environ.get("TEDDY_ROOT") or Path(__file__).resolve().parent)
NQ   = json.load(open(ROOT / A.nq, encoding="utf-8"))
EQ   = {n: json.load(open(ROOT / "data/opra" / f"{A.eq_date}-{n}-trinity.json", encoding="utf-8"))
        for n in ("QQQ", "SPY", "SPX+SPXW")}
MULT = {"NQ": 20.0, "QQQ": 100.0, "SPY": 100.0, "SPX+SPXW": 100.0}
BOOKS = ("0DTE", "NEAR", "FULL")
GRID = 25.0
LABEL = {"NQ": "NQ", "QQQ": "QQQ", "SPY": "SPY", "SPX+SPXW": "SPX"}

fnq = NQ["spot"]
def nq_axis(k, f): return round((k / f) * fnq / GRID) * GRID

# ---- bucket every book onto the NQ axis ------------------------------------------------
data = {}          # inst -> book -> {axis_strike: gamma}
src  = {}          # inst -> axis_strike -> list of own strikes that landed there
refs = {"NQ": fnq}
for inst, T in [("NQ", NQ)] + list(EQ.items()):
    f = T["spot"]; refs[inst] = f
    data[inst] = {}; src[inst] = {}
    for b in BOOKS:
        g = T["panels"][b]["gamma"]; agg = {}
        for ks, v in g.items():
            k = float(ks); a = k if inst == "NQ" else nq_axis(k, f)
            agg[a] = agg.get(a, 0) + v
            if v: src[inst].setdefault(a, set()).add(k)
        data[inst][b] = agg

lo, hi = fnq * 0.97, fnq * 1.03
axis = [k for k in sorted({a for i in data for b in data[i] for a in data[i][b]}, reverse=True) if lo <= k <= hi]

def strike_label(inst, a):
    ks = sorted(src[inst].get(a, ()))
    if not ks: return ""
    if inst == "NQ": return f"{a:,.0f}"
    if len(ks) == 1: return f"{ks[0]:,.0f}" if inst == "SPX+SPXW" else f"{ks[0]:,.0f}"
    return f"{ks[0]:,.0f}–{ks[-1]:,.0f}"

# markers on the NQ axis: NQ last/settle/flips; equity flips mapped through the same ratio
marks = [("last", "LAST", NQ["last_print"]), ("spot", "SETTLE", fnq)]
for b, cls in (("0DTE", "zero"), ("NEAR", "near"), ("FULL", "full")):
    marks.append((cls, f"NQ {b} FLIP", NQ["panels"][b]["hvl"][0]))
for inst, T in EQ.items():
    fl = T["panels"]["FULL"]["hvl"]
    if fl: marks.append(("eq", f"{LABEL[inst]} FULL FLIP → {fl[0]:,.1f}", fl[0] / T["spot"] * fnq))

# ---- render --------------------------------------------------------------------------------
def bar(v, mx):
    if not v or not mx: return '<div class="b"><i class="z"></i></div>'
    w = min(100, abs(v) / mx * 100) / 2
    return f'<div class="b"><i class="{"p" if v > 0 else "n"}" style="width:{w:.1f}%"></i></div>'
def val(v):
    if not v: return '<div class="v mono z">·</div>'
    return f'<div class="v mono {"p" if v > 0 else "n"}">{v:+,.0f}</div>'

payload = {}
for b in BOOKS:
    rows = []
    mx = {i: max((abs(v) for a, v in data[i][b].items() if lo <= a <= hi), default=0) for i in data}
    pending = sorted(marks, key=lambda m: -m[2]); pi = 0
    for a in axis:
        while pi < len(pending) and pending[pi][2] > a:
            cls, lab, px = pending[pi]; pi += 1
            rows.append(f'<div class="mk {cls}" data-px="{px:g}"><div class="mkl mono">{px:,.0f}</div><div class="mkr"><span>{html.escape(lab)} {px:,.2f}</span></div></div>')
        cells = [f'<div class="k mono">{a:,.0f}</div>']
        hot = any(abs(data[i][b].get(a, 0)) >= 0.5 * mx[i] for i in data if mx[i])
        for i in data:
            v = data[i][b].get(a, 0)
            cells.append(f'<div class="s mono">{strike_label(i, a) if i != "NQ" else ""}</div>' if i != "NQ" else "")
            cells.append(bar(v, mx[i])); cells.append(val(v))
        rows.append(f'<div class="r{" hi" if hot else ""}" data-px="{a:g}" data-g25="{1 if a % 25 == 0 else 0}">{"".join(cells)}</div>')
    payload[b] = "\n".join(rows)

def musd(inst, T, b):
    f = T["spot"]; em = T["em"]; g = T["panels"][b]["gamma"]
    at = sum(v for k, v in g.items() if f - em <= float(k) <= f + em)
    return at * f * MULT[inst] / 100 / 1e6, T["panels"][b]["net_total"] * f * MULT[inst] / 100 / 1e6

foot = []
for inst, T in [("NQ", NQ)] + list(EQ.items()):
    a0, c0 = musd(inst, T, "0DTE"); a1, c1 = musd(inst, T, "NEAR"); a2, c2 = musd(inst, T, "FULL")
    foot.append(f'<tr><td class="mono">{LABEL[inst]}</td><td class="mono">{T["spot"]:,.2f}</td><td class="mono">{T["em"]:,.2f}</td>'
                f'<td class="mono">{a0:+,.0f}</td><td class="mono">{a1:+,.0f}</td><td class="mono">{a2:+,.0f}</td><td class="mono dim">{c2:+,.0f}</td>'
                f'<td class="mono">{T["panels"]["0DTE"]["hvl"][0] if T["panels"]["0DTE"]["hvl"] else "—"}</td>'
                f'<td class="mono">{T["panels"]["NEAR"]["hvl"][0] if T["panels"]["NEAR"]["hvl"] else "—"}</td>'
                f'<td class="mono">{T["panels"]["FULL"]["hvl"][0] if T["panels"]["FULL"]["hvl"] else "—"}</td></tr>')

built = dt.datetime.now(dt.timezone(dt.timedelta(hours=-7))).strftime("%H%M")
title = f"TeddyAI {A.session} x-ladder {built}"
page = f"""<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root{{--rh:15px;--rp:2px;--lh:1.1;--fk:12.5px;--fv:11.5px;--fs:10.5px;--ground:#F2F3F1;--panel:#FBFBFA;--panel2:#E9EBE7;--ink:#14181A;--ink2:#4A5350;--ink3:#7C8783;--rule:#D2D6D0;--rule2:#BFC5BE;--brass:#9A6E1F;--brassx:#EFE2C6;--neg:#A8412C;--negx:#F0D8D1;--pos:#1F6E62;--posx:#CFE3DE;--warn:#8A6A12;--vio:#5B4B8A}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{--ground:#15181A;--panel:#1D2124;--panel2:#262B2F;--ink:#ECEEEA;--ink2:#B9C0BB;--ink3:#8A938E;--rule:#33393D;--rule2:#454C51;--brassx:#3A3120;--negx:#3E2620;--posx:#1E3330}}}}
:root[data-theme="dark"]{{--ground:#15181A;--panel:#1D2124;--panel2:#262B2F;--ink:#ECEEEA;--ink2:#B9C0BB;--ink3:#8A938E;--rule:#33393D;--rule2:#454C51;--brassx:#3A3120;--negx:#3E2620;--posx:#1E3330}}
*{{box-sizing:border-box}} body{{background:var(--ground);color:var(--ink);font-family:"IBM Plex Sans",-apple-system,"Segoe UI",sans-serif;font-size:15px;line-height:1.5;margin:0}}
.mono{{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}}
.wrap{{max-width:1500px;margin:0 auto;padding:22px 16px 56px;display:flex;flex-direction:column;gap:18px}}
h1{{font-family:Archivo,"IBM Plex Sans",sans-serif;font-size:22px;letter-spacing:-.01em;margin:0}}
.sech{{display:flex;align-items:baseline;gap:12px;border-bottom:1px solid var(--rule2);padding-bottom:7px;flex-wrap:wrap}}
.sech .hint{{font-size:12px;color:var(--ink3);margin-left:auto;text-align:right}}
.tools{{display:flex;gap:6px;align-items:center}} .tl{{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);font-weight:700;margin-right:2px}} .tools button{{font:inherit;font-size:11px;letter-spacing:.06em;text-transform:uppercase;font-weight:700;padding:5px 10px;border:1px solid var(--rule2);background:var(--panel);color:var(--ink2);border-radius:2px;cursor:pointer}}
.tools button.on{{background:var(--brassx);color:var(--brass);border-color:var(--brass)}}
.lad{{border:1px solid var(--rule);background:var(--panel);border-radius:3px;padding:0 0 8px;overflow-x:auto}}
.grid{{min-width:1180px;display:grid;grid-template-columns:70px 1fr 54px 56px 1fr 60px 56px 1fr 60px 62px 1fr 64px;align-items:center}}
#rows{{display:contents}} .hd{{display:contents}} .hd>div{{position:sticky;top:0;background:var(--panel2);border-bottom:1px solid var(--rule2);padding:8px 0 7px;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);font-weight:700;z-index:1}}
.hd .lft{{text-align:right;padding-right:10px}} .hd .sp2{{grid-column:span 2}} .hd .sp3{{grid-column:span 3}}
.r{{display:contents}} .r>*{{padding:var(--rp) 0;line-height:var(--lh)}} .mk .mkl{{line-height:1.1}} .r[hidden]>*{{display:none}} .mk[hidden]>*{{display:none}} .r.hi .k{{color:var(--ink);font-weight:700}}
.k{{font-size:var(--fk);color:var(--ink2);text-align:right;padding-right:10px!important;font-weight:500}}
.s{{font-size:var(--fs);color:var(--ink3);text-align:right;padding-right:6px!important}}
.b{{position:relative;height:var(--rh);background:linear-gradient(var(--rule),var(--rule)) center/1px 100% no-repeat;margin:0 4px}}
.b i{{position:absolute;top:2px;height:calc(var(--rh) - 4px);display:block;border-radius:1px}} .b i.n{{right:50%;background:var(--neg)}} .b i.p{{left:50%;background:var(--pos)}} .b i.z{{display:none}}
.v{{font-size:var(--fv);text-align:right;padding-right:6px!important;font-weight:500}} .v.p{{color:var(--pos)}} .v.n{{color:var(--neg)}} .v.z{{color:var(--ink3);opacity:.4}}
.mk{{display:contents}} .mk .mkl{{grid-column:1/2;text-align:right;padding-right:10px!important;font-size:10px;font-weight:700;color:var(--ink)}}
.mk .mkr{{grid-column:2/13;position:relative;height:0;border-top:1px dashed var(--ink3)}}
.mk .mkr span{{position:absolute;top:-8px;left:10px;background:var(--panel);padding:0 8px;font-size:10px;letter-spacing:.06em;text-transform:uppercase;font-weight:700}}
.mk.spot .mkr{{border-top:2px solid var(--brass)}} .mk.spot .mkl,.mk.spot .mkr span{{color:var(--brass)}}
.mk.last .mkr{{border-top:3px solid var(--ink)}} .mk.last .mkl{{color:var(--ink)}} .mk.last .mkr span{{background:var(--ink);color:var(--panel);border-radius:2px}}
.mk.zero .mkr{{border-top:2px dashed var(--warn)}} .mk.zero .mkl,.mk.zero .mkr span{{color:var(--warn)}}
.mk.near .mkr,.mk.full .mkr{{border-top:1px dashed var(--warn);opacity:.85}} .mk.near .mkl,.mk.full .mkl,.mk.near .mkr span,.mk.full .mkr span{{color:var(--warn)}}
.mk.eq .mkr{{border-top:1px dotted var(--vio)}} .mk.eq .mkl,.mk.eq .mkr span{{color:var(--vio)}}
table{{border-collapse:collapse;font-size:12.5px;background:var(--panel);border:1px solid var(--rule);border-radius:3px}} th,td{{padding:6px 12px;text-align:right;border-bottom:1px solid var(--rule)}} th{{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3)}} td:first-child,th:first-child{{text-align:left}} .dim{{color:var(--ink3)}}
.note{{font-size:12.5px;color:var(--ink2);border-left:3px solid var(--brass);padding:8px 12px;background:var(--panel)}}
.note.w{{border-left-color:var(--neg)}}
</style>
<div class="wrap">
<div class="sech"><h1>NQ · QQQ · SPY · SPX — one axis</h1><div class="hint">{A.session} RTH plan · axis = NQ strikes, 25-pt grid · equity strikes placed at K/F<sub>eq</sub>·F<sub>NQ</sub> (moneyness-preserving) and bucketed · built {built} PT</div></div>
<div class="note w"><b>Vintage.</b> NQ is today's build on the 2026-09-04 final book (ref = Friday settle 29,565.25). QQQ, SPY and the SPX complex are the latest complete equity set on disk: <b>{A.eq_date} quotes for the {EQ["QQQ"].get("report_date","?")} session</b> (NQ book settle {NQ.get("settle","?")}). When the two dates differ the equity columns are a session older than the NQ column and their reference forwards are from the earlier day — <code>python opra_pull.py --estimate --date &lt;settle&gt; --roots SPX,SPXW,QQQ,SPY</code>, then <code>--fetch</code>, then <code>opra_ladder.py</code> per root brings them level.</div>
<div class="sech" style="gap:18px"><div class="tools"><span class="tl">Book</span><button data-book="0DTE">0DTE</button><button data-book="NEAR">Near · ≤9 DTE</button><button data-book="FULL" class="on">Full book</button></div>
<div class="tools"><span class="tl">Range</span><button data-range="0.01">±1%</button><button data-range="0.02">±2%</button><button data-range="0.03" class="on">±3%</button></div>
<div class="tools"><span class="tl">Rows</span><button data-g25="0" class="on">all NQ strikes</button><button data-g25="1">25-pt grid only</button></div>
<div class="tools"><span class="tl">Size</span><button data-size="fit">fit screen</button><button data-size="s">S</button><button data-size="m" class="on">M</button><button data-size="l">L</button></div>
<div class="hint">bars scale within each instrument's column — shapes compare, lengths do not · numbers are each instrument's own contracts per 1% move (dealer sign, C − P) · the one comparable unit, $mm per 1%, is in the table below<br>solid dark rule = LAST · brass = NQ SETTLE · amber dashes = NQ flips · violet dots = equity full-book flips on the NQ axis · small grey figure = the equity's own strike(s) in that row</div></div>
<div class="lad"><div class="grid" id="grid">
<div class="hd"><div class="lft">NQ axis</div><div class="sp2">NQ</div><div class="sp3">QQQ · strike · gamma</div><div class="sp3">SPY · strike · gamma</div><div class="sp3">SPX complex · strike · gamma</div></div>
<div id="rows"></div>
</div></div>
<div class="sech"><h1 style="font-size:16px">The comparable unit — $mm of dealer gamma per 1% move</h1><div class="hint">within ±1 EM of each reference, by book · column = chain-wide full-book sum · flips in each instrument's own price</div></div>
<table><tr><th>book</th><th>reference</th><th>EM</th><th>0DTE at spot</th><th>Near at spot</th><th>Full at spot</th><th>Full column</th><th>0DTE flip</th><th>Near flip</th><th>Full flip</th></tr>{''.join(foot)}</table>
<div class="note">Reading it: a row is one NQ price. The QQQ / SPY / SPX cells show the strike(s) of that instrument sitting at the same moneyness, and that instrument's net gamma there. Long bars in the same rows across columns mean the four books agree on where the weight is; that is a statement about structure, not about direction — the standing finding is that these levels do not predict (p = 0.73), and the 60-session confluence test found no evidence that agreement across books improves a level.</div>
</div>
<script>
const ROWS={json.dumps(payload)}, F={fnq};
const S={{book:'FULL',range:0.03,g25:0,size:'m'}};
const on=(sel,val,attr)=>document.querySelectorAll(sel).forEach(x=>x.classList.toggle('on',x.dataset[attr]===String(val)));
function show(){{
  const el=document.getElementById('rows'); el.innerHTML=ROWS[S.book];
  const lo=F*(1-S.range), hi=F*(1+S.range); let n=0, m=0;
  el.querySelectorAll('.r,.mk').forEach(r=>{{const px=+r.dataset.px; const hide=px<lo||px>hi||(S.g25&&r.classList.contains('r')&&r.dataset.g25!=='1'); r.hidden=hide; if(!hide){{if(r.classList.contains('mk'))m++; else n++}}}});
  on('[data-book]',S.book,'book'); on('[data-range]',S.range,'range'); on('[data-g25]',S.g25,'g25'); on('[data-size]',S.size,'size');
  size(n,m);
}}
function size(n,m){{
  const root=document.documentElement.style;
  const preset={{s:[11,1,10.5,9.5,9],m:[15,2,12.5,11.5,10.5],l:[20,3,14,13,12]}};
  let v=preset[S.size]||preset.m;
  if(S.size==='fit'){{
    const top=document.getElementById('grid').getBoundingClientRect().top+window.scrollY+34;   // header row
    const avail=window.innerHeight-top-24-m*12, per=Math.max(6, Math.floor(avail/Math.max(n,1)));
    const f=Math.max(6, Math.min(12.5, per-1)), rh=Math.max(6, Math.min(per, 15));
    v=[rh, 0, f, Math.max(6,f-1), Math.max(6,f-2)];
  }}
  root.setProperty('--rh',v[0]+'px'); root.setProperty('--rp',v[1]+'px'); root.setProperty('--fk',v[2]+'px'); root.setProperty('--fv',v[3]+'px'); root.setProperty('--fs',v[4]+'px'); root.setProperty('--lh', S.size==='fit'?'1':'1.1');
}}
document.querySelectorAll('[data-book]').forEach(x=>x.addEventListener('click',()=>{{S.book=x.dataset.book;show()}}));
document.querySelectorAll('[data-range]').forEach(x=>x.addEventListener('click',()=>{{S.range=+x.dataset.range;show()}}));
document.querySelectorAll('[data-g25]').forEach(x=>x.addEventListener('click',()=>{{S.g25=+x.dataset.g25;show()}}));
document.querySelectorAll('[data-size]').forEach(x=>x.addEventListener('click',()=>{{S.size=x.dataset.size;show()}}));
window.addEventListener('resize',()=>{{if(S.size==='fit')show()}});
show();
</script>"""
out = Path(A.out) if A.out else ROOT / "sheets" / f"{A.session}-xladder.html"
out.write_text(page, encoding="utf-8")
print(out, len(page), "chars;", len(axis), "axis rows;", {i: refs[i] for i in refs})
