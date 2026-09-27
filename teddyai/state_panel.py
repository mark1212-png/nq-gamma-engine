#!/usr/bin/env python3
"""
state_panel.py -- render the market-state layers as an HTML fragment for the sheet.

    import state_panel
    html = state_panel.render()          # "" when state.json is absent

    python state_panel.py --out panel.html

ENHANCES, NEVER REPLACES. Mark's brief was explicit that the compact ladder and
the existing level output stay. This adds a block ABOVE nothing and BELOW
nothing -- `build.py` decides where to place it, and if it decides not to, the
sheet is exactly what it was.

FAIL-SAFE BY CONSTRUCTION. `render()` returns an empty string on any problem: no
`state.json`, malformed JSON, a missing key, a stale document. It never raises.
That property is what makes it safe to call from `build.py`, which runs
unattended inside `autopull` four times a day and must not be able to fail
because an optional panel could not be drawn.

    HANDOFF, "Traps that have already cost a day each": a truncated cache and a
    quiet tape look identical to every reader. An optional panel that throws
    would take the whole sheet down for a feature nobody asked to be critical.

STALENESS. The panel stamps the settle and report date the state was computed
for. If they disagree with the sheet's own, the panel says so rather than
quietly showing yesterday's structure beside today's ladder -- the same failure
`build.staleness()` exists to catch for the prose.

The markup reuses the sheet's existing classes (`note`, `mono`, `st`, `k`, `v`,
`n`) so it inherits the page's styling and needs no CSS of its own.

Written 2026-09-10, Phase H of the market-state upgrade.
"""

import json
import html as _html
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state.json"

MAX_LEVELS = 6          # the sheet already has a full ladder; this is the shortlist
MAX_SCENARIOS = 3


def _esc(s):
    return _html.escape(str(s), quote=True)


def _n(v, dp=0):
    return "&mdash;" if v is None else f"{v:,.{dp}f}"


def _pct(v):
    return "&mdash;" if v is None else f"{v * 100:.0f}%"


def _conf_cell(c):
    v = (c or {}).get("value")
    return "n/a" if v is None else f"{v:.2f}"


def render(path=None, sheet_settle=None, sheet_report=None):
    """
    The fragment, or "" if it cannot be drawn. NEVER RAISES.

    `sheet_settle` / `sheet_report`, when supplied, are checked against the
    document so a state computed for a different session is labelled rather than
    silently shown.
    """
    try:
        p = Path(path or STATE)
        if not p.exists():
            return ""
        doc = json.loads(p.read_text(encoding="utf-8"))
        src = doc.get("source") or {}
        rep = doc.get("report") or {}
        if not rep:
            return ""

        warn = ""
        if sheet_settle and src.get("settle") and src["settle"] != sheet_settle:
            warn = (f'<div class="note w"><b>This panel was computed on a different book.</b> '
                    f'State settle <b class="mono">{_esc(src["settle"])}</b> against the sheet’s '
                    f'<b class="mono">{_esc(sheet_settle)}</b>. Re-run '
                    f'<span class="mono">engine.py</span> before reading it.</div>')
        elif sheet_report and src.get("report_date") and src["report_date"] != sheet_report:
            warn = (f'<div class="note w"><b>This panel is for a different session.</b> '
                    f'State report date <b class="mono">{_esc(src["report_date"])}</b> against the '
                    f'sheet’s <b class="mono">{_esc(sheet_report)}</b>.</div>')

        st = rep.get("state") or {}
        parts = [warn, '<h2>Market state</h2>',
                 '<div class="strip">']

        for key, label in (("gamma", "Dealer gamma"), ("concentration", "Concentration"),
                           ("flip", "Price vs flips"), ("expiry", "0DTE share"),
                           ("vanna", "Vanna"), ("vol", "Volatility")):
            d = st.get(key) or {}
            if not d:
                continue
            val = d.get("label", "—")
            if key == "expiry" and d.get("gross_share") is not None:
                val = f"{_pct(d['gross_share'])} <span class=\"n\">{_esc(d.get('label',''))}</span>"
            elif key == "vol" and d.get("percentile") is not None:
                val = f"{_esc(d['label'])} <span class=\"n\">p{d['percentile'] * 100:.0f}</span>"
            note = f"confidence {d['confidence']:.2f}" if d.get("confidence") is not None else ""
            if key == "gamma" and d.get("measures_agree") is False:
                note += " · <b>at-spot and chain-wide DISAGREE</b>"
            if key == "gamma":
                # 2026-09-12: the slope of the aggregate gamma curve at spot, the
                # size of the hedging response rather than its sign alone
                gp = ((rep.get("gamma_profile") or {}).get(src.get("panel_used") or "NEAR")) or {}
                if gp.get("slope_at_spot") is not None:
                    note += f" · slope {gp['slope_at_spot']:+.2f}/pt"
            if key == "flip" and d.get("fragmented"):
                note += " · <b>FRAGMENTED</b> " + _esc(", ".join(
                    f"{k} {v}" for k, v in (d.get("crossings") or {}).items()))
            parts.append(f'<div class="st"><div class="k">{_esc(label)}</div>'
                         f'<div class="v mono">{val if key in ("expiry", "vol") else _esc(val)}</div>'
                         f'<div class="n">{note}</div></div>')
        parts.append('</div>')

        # 2026-09-12: the session's scheduled releases as time gates, and the
        # sign-convention check. Both optional; both drawn only when present.
        rel = rep.get("releases") or {}
        if rel.get("summary"):
            parts.append(f'<div class="note{" w" if rel.get("n_in_plan") else ""}"><b>Releases.</b> '
                         f'{_esc(rel["summary"])}</div>')
        sc = rep.get("sign_convention") or {}
        inv = sc.get("invariant_under_inversion") or {}
        if sc:
            src_txt = sc.get("source")
            src_txt = src_txt if isinstance(src_txt, str) else "flow-measured"
            parts.append(f'<div class="note n"><b>Dealer sign.</b> {_esc(src_txt)}; an assumption, not an '
                         f'observation. If it is wrong the gamma regime reads '
                         f'<b>{_esc(sc.get("gamma_regime_if_inverted") or "?")}</b>; the flips, the rails'
                         f'{", the node ranking" if inv.get("node_ranking_identical") else ""}'
                         f'{" and the formations" if inv.get("formations_identical") else ""} do not move.</div>')

        levels = (rep.get("levels") or [])[:MAX_LEVELS]
        if levels:
            parts.append('<h3>Ranked nodes</h3><table class="lv"><tr>'
                         '<th>Price</th><th>Quality</th><th>&Gamma; 1%</th><th>OI</th>'
                         '<th>Dist</th><th>Structure</th></tr>')
            for l in levels:
                lab = l.get("topology") or "—"
                if l.get("labels"):
                    lab += " (" + ", ".join(_esc(x) for x in l["labels"]) + ")"
                parts.append(
                    f'<tr><td class="mono">{_n(l.get("price"))}</td>'
                    f'<td class="mono">{l.get("quality", 0):.2f}</td>'
                    f'<td class="mono">{l.get("gamma", 0):+,.0f}</td>'
                    f'<td class="mono">{_n(l.get("oi"))}</td>'
                    f'<td class="mono">{l.get("distance", 0):+,.0f}</td>'
                    f'<td>{lab}{" · <i>rail call fragile</i>" if l.get("rail_call_fragile") else ""}</td></tr>')
            parts.append('</table>')

        # 2026-09-12: the trade gate leads the scenario block. On a stand-aside
        # book the report carries no scenarios and the panel says why.
        tg = rep.get("trade_gate") or {}
        if tg.get("verdict"):
            cls = "note" if tg["verdict"] == "look" else "note w"
            why = ("<br><span class=\"n\">" + "; ".join(_esc(x) for x in (tg.get("reasons") or [])[:4]) + "</span>"
                   if tg.get("reasons") else "")
            played = ""
            if tg.get("scenarios_played"):
                played = (f' <span class="n">{tg["scenarios_played"]} path(s) already played by the live print '
                          f'{tg["live_print"]:,.2f} and withheld.</span>')
            parts.append(f'<div class="{cls}"><b>Trade gate: {_esc(str(tg["verdict"]).upper())}.</b> '
                         f'{_esc(tg.get("text") or "")}{played}{why}</div>')

        scen = (rep.get("scenarios") or [])[:MAX_SCENARIOS]
        if scen:
            parts.append('<h3>Scenarios</h3><ul>')
            for s in scen:
                tg = ", ".join(f"{t:,.0f}" for t in (s.get("targets") or [])) or "—"
                parts.append(
                    f'<li><b>{_esc(s.get("label", ""))}</b> · '
                    f'{_esc(s.get("trigger") or "no trigger")} → <span class="mono">{tg}</span>'
                    f'<br><span class="n">invalidation: '
                    f'{_esc((s.get("invalidation") or "none"))}</span></li>')
            parts.append('</ul>')

        cf = rep.get("confidence") or {}
        if cf:
            cells = " · ".join(f"{_esc(k)} {_conf_cell(v)}" for k, v in cf.items())
            parts.append(f'<div class="note"><b>Confidence.</b> {cells}. '
                         f'<b>None of these is a probability</b> and none has been calibrated '
                         f'against outcomes.</div>')

        parts.append(
            f'<div class="note n">Computed by <span class="mono">engine.py</span> on the '
            f'<b class="mono">{_esc(src.get("settle", "?"))}</b> book for '
            f'<b class="mono">{_esc(src.get("report_date", "?"))}</b>, panel '
            f'<b class="mono">{_esc(src.get("panel", "?"))}</b>. Structure and location only — '
            f'the 60-session backtest found no support for a gamma sign implying a range '
            f'(p=0.46). <b>These nodes mark where hedging concentrates, not where price '
            f'turns</b>: over 500 sessions and 4,556 touches they reacted 56.8% against 57.0% '
            f'for a distance-matched round-number placebo, and the quality buckets are not '
            f'monotone. Rank them for attention, not for a hold rate.</div>')
        return "\n".join(x for x in parts if x)
    except Exception:
        # Deliberately silent. See the module docstring: this panel must never be
        # able to take down an unattended sheet build.
        return ""


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=str(STATE))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    h = render(a.state)
    if not h:
        print("nothing rendered (no state.json, or it could not be read)")
        return
    if a.out:
        Path(a.out).write_text(h, encoding="utf-8")
        print(f"wrote {a.out} ({len(h):,} chars)")
    else:
        print(h)


if __name__ == "__main__":
    main()
