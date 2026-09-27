# -*- coding: utf-8 -*-
"""
levels_live.py -- the sheet's open interest and smile, re-solved at the last print and the real
clock, written to levels_live.json every few seconds so the chart's levels move with the tape.

    python levels_live.py             a service: recompute when price moves or a minute passes
    python levels_live.py --once      one recompute, then exit
    python levels_live.py --check     reprice at the build's own spot and clock and diff against
                                      trinity.json's panels (must be ~0: the sheet is recovered
                                      exactly at shift 0 and plan time)

Mark, 2026-09-15: "Shouldn't we just poll throughout the day just so our charts stay updated?"
Open interest changes twice a day, but gamma at a strike depends on where price is and how much
time is left, so the same book gives a different ladder at 29,350 with three hours left than at
29,250 with six. The command center already reprices in the browser (LIVE-LADDER.md:
modelGamma / liveFlip in command_center.html); the chart read trinity.json, which changes only
when a build runs. This is that browser code ported to Python, line for line: every book's
underlying shifted by (last - F_ref), IV held at the build's smile evaluated at the new
moneyness, time in the business-clock basis chain_live.json exports (clock_knots), the flip
as the zero crossing of total gamma nearest the print. No options tape, no new OI: the book
is the last build's, only the price and the clock are live. teddychart/server.py prefers
this file over trinity.json's panels when it is fresh and for the same session.
"""
import sys, os, json, time, math, datetime, pathlib, argparse

ROOT = pathlib.Path(__file__).resolve().parent
CHAIN = ROOT / "chain_live.json"
OUT = ROOT / "levels_live.json"
BARS = ROOT / "data" / "bars"
YEAR_MS = 31557600000.0
PANELS = {"0DTE": lambda b: b["dte"] == 0, "NEAR": lambda b: b["dte"] <= 9, "FULL": lambda b: True}

try:
    import numpy as np
except ImportError:                                        # pure-Python fallback is far too slow for the flip sweep
    np = None


def say(msg):
    print(msg, flush=True)


# ------------------------------------------------------------ the model ---
# The JS: modelTau, modelT, modelIV, modelStrike, bsGamma, modelGamma, liveFlip.

def model_tau(C, now_ms):
    a = C["clock_knots"]
    if not a or len(a) < 2:
        raise ValueError("Rebuild chain_live: business clock is missing")
    if now_ms < a[0][0] or now_ms > a[-1][0]:
        raise ValueError("Rebuild chain_live: time outside exported clock")
    lo, hi = 0, len(a) - 1
    while hi - lo > 1:
        m = (lo + hi) >> 1
        if a[m][0] <= now_ms: lo = m
        else: hi = m
    return a[lo][1] + (a[hi][1] - a[lo][1]) * (now_ms - a[lo][0]) / (a[hi][0] - a[lo][0])


def parse_ms(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000.0


def model_T(C, b, now_ms):
    exp_ms = parse_ms(b["expiry"])
    if exp_ms <= now_ms:
        return 0.0
    if C.get("time_basis") == "sessions":
        return max(0.0, model_tau(C, exp_ms) - model_tau(C, now_ms)) / (C.get("sessions_per_year") or 252)
    return (exp_ms - now_ms) / YEAR_MS


def model_iv(b, K, F, iv):
    """Vectorised over K (numpy arrays); the JS interpolates inside the smile and extrapolates a
    fitted line from the last 2-5 knots outside it, clamped to [0.5, 2] x ATM."""
    a = b.get("smile")
    if not a:
        return iv
    xs = np.array([p[0] for p in a]); ys = np.array([p[1] for p in a])
    m = np.log(K / F)
    n = len(a)
    if n == 1:
        return np.full_like(m, ys[0])
    inside = np.interp(m, xs, ys)
    out = inside.copy()
    k = max(2, min(5, n // 4))
    for low, sel in ((True, m < xs[0]), (False, m > xs[-1])):
        if not sel.any():
            continue
        z_x, z_y = (xs[:k], ys[:k]) if low else (xs[-k:], ys[-k:])
        mx, my = z_x.mean(), z_y.mean()
        den = ((z_x - mx) ** 2).sum()
        slope = ((z_x - mx) * (z_y - my)).sum() / den if den else 0.0
        px, py = (xs[0], ys[0]) if low else (xs[-1], ys[-1])
        v = py + slope * (m[sel] - px)
        out[sel] = np.clip(v, 0.5 * b["atm_iv"], 2 * b["atm_iv"])
    return out


def round_even(x):
    l = np.floor(x); f = x - l
    tie = np.abs(f - 0.5) < 1e-12
    r = np.where(tie, np.where(l % 2 == 0, l, l + 1), np.round(x))
    return r


def model_strike(b, K):
    if b.get("basis"):
        grid = b.get("grid") or 25.0
        return round_even((K - b["basis"]) / grid) * grid
    return K


def bs_gamma(F, K, sig, T):
    sq = sig * np.sqrt(T)
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = (np.log(F / K) + 0.5 * sig * sig * T) / sq
        g = np.exp(-0.5 * d1 * d1) / 2.5066282746310002 / (F * sq)
    g = np.where((sig > 0) & (sq > 0), g, 0.0)
    return g


def book_arrays(b):
    S = np.array([[float(r[0]), float(r[1]), float(r[2])] for r in b["strikes"] if r[0] and r[1] is not None], dtype=float)
    return S[:, 0], S[:, 1], S[:, 2]


def model_gamma(C, b, arrs, shift, now_ms, T=None):
    """{z_strike: gamma} for one book at underlying b.F + shift and the clock now_ms."""
    K, n, iv = arrs
    F = b["F"] + shift
    T = model_T(C, b, now_ms) if T is None else T
    if T <= 0 or F <= 0 or not len(K):
        return {}
    cap = b.get("cap", 0.25) if b.get("cap") is not None else 0.25
    ok = (K > 0) & (np.abs(np.log(K / F)) <= cap)
    if not ok.any():
        return {}
    Kk, nn, ivv = K[ok], n[ok], iv[ok]
    g = nn * bs_gamma(F, Kk, model_iv(b, Kk, F, ivv), T) * 0.01 * F
    z = model_strike(b, Kk)
    out = {}
    for zk, gv in zip(z.tolist(), g.tolist()):
        out[zk] = out.get(zk, 0.0) + gv
    return out


def book_total(C, b, arrs, shift, T):
    K, n, iv = arrs
    F = b["F"] + shift
    if T <= 0 or F <= 0 or not len(K):
        return 0.0
    cap = b.get("cap", 0.25) if b.get("cap") is not None else 0.25
    ok = (K > 0) & (np.abs(np.log(K / F)) <= cap)
    if not ok.any():
        return 0.0
    Kk, nn, ivv = K[ok], n[ok], iv[ok]
    return float((nn * bs_gamma(F, Kk, model_iv(b, Kk, F, ivv), T) * 0.01 * F).sum())


def contract_ok(C):
    """
    (ok, why) -- is the feed's contract the one chain_live is referenced to?

    reprice() shifts every book by (print - F_ref). If the print is on another
    contract that shift silently carries the CALENDAR BASIS (+291.75 across the
    2026-09-18 roll), moving every wall and both flips by a whole roll and writing
    it to the chart as live levels, over the top of the sheet's own. Checked here
    because this file is the only writer of levels_live.json. A continuous feed
    symbol cannot be compared by name, so it passes and the chart keeps the
    sheet's levels if it is wrong.
    """
    try:
        st = json.loads((BARS / "feed_status.json").read_text(encoding="utf-8"))
    except Exception:
        return True, ""                                  # no feed status: last_print falls back to bars
    sym = st.get("symbol") or st.get("contract")
    ref = C.get("ref_symbol")
    if not sym or not ref or str(sym).startswith("NQ.") or sym == ref:
        return True, ""
    return False, f"the feed is on {sym} and chain_live is referenced to {ref}"


def reprice(C, last, now_ms):
    """The three panels at the print `last` and the clock `now_ms`: gamma per Z-axis strike, the
    flip nearest the print, the net."""
    books = [(b, book_arrays(b), model_T(C, b, now_ms)) for b in C["books"]]
    shift = last - C["F_ref"]
    panels = {}
    for name, pick in PANELS.items():
        sel = [(b, arrs, T) for b, arrs, T in books if pick(b)]
        g = {}
        for b, arrs, T in sel:
            for k, v in model_gamma(C, b, arrs, shift, now_ms, T).items():
                g[k] = g.get(k, 0.0) + v
        # the flip: zero crossings of the total over F_ref +/- 500 in 5-point steps, refined linearly
        xs = []
        prev = None
        for x in range(-500, 501, 5):
            t = sum(book_total(C, b, arrs, float(x), T) for b, arrs, T in sel)
            if prev is not None and ((prev[1] < 0 and t >= 0) or (prev[1] >= 0 and t < 0)):
                xs.append(C["F_ref"] + prev[0] + (0 - prev[1]) * (x - prev[0]) / (t - prev[1]))
            prev = (x, t)
        flip = min(xs, key=lambda v: abs(v - last)) if xs else None
        panels[name] = {
            "gamma": {f"{k:.1f}": int(round(v)) for k, v in sorted(g.items()) if abs(v) >= 0.5},
            "hvl": [round(flip, 1)] if flip is not None else [],
            "net_total": int(round(sum(g.values()))),
            "expiries": len(sel),
        }
    return panels


# ----------------------------------------------------------- the service ---

def last_print():
    """The newest print: feed_status.json when the feed is streaming, else the newest bar on disk."""
    try:
        st = json.loads((BARS / "feed_status.json").read_text(encoding="utf-8"))
        upd = datetime.datetime.fromisoformat(st["updated"])
        if (datetime.datetime.now(datetime.timezone.utc) - upd).total_seconds() < 120 and st.get("last_px"):
            return float(st["last_px"]), "feed"
    except Exception:
        pass
    today = datetime.datetime.now(datetime.timezone.utc).date()
    for d in (today, today - datetime.timedelta(days=1)):
        f = BARS / f"{d.isoformat()}.json"
        if f.exists():
            try:
                rows = json.loads(f.read_text(encoding="utf-8"))
                if rows:
                    return float(rows[-1]["c"]), f"bar {rows[-1]['ts'][11:16]}Z"
            except Exception:
                continue
    return None, None


def write_out(C, last, src, now_ms, panels):
    doc = {
        "t": int(now_ms), "asof": datetime.datetime.fromtimestamp(now_ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last": last, "last_source": src, "F_ref": C["F_ref"], "spot": C.get("spot"),
        "report_date": C.get("report_date"), "settle": C.get("settle"), "oi_vintage": C.get("oi_vintage"),
        "chain_asof": C.get("asof"), "panels": panels,
    }
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(tmp, OUT)
    return doc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--check", action="store_true", help="reprice at the build's spot and clock; diff against trinity.json")
    ap.add_argument("--every", type=float, default=5.0, help="seconds between looks at the print")
    ap.add_argument("--min-move", type=float, default=1.0, help="points the print must move to recompute (else every 60 s)")
    a = ap.parse_args()
    if np is None:
        say("numpy is required"); return 1

    if a.check:
        C = json.loads(CHAIN.read_text(encoding="utf-8"))
        T = json.loads((ROOT / "trinity.json").read_text(encoding="utf-8"))
        panels = reprice(C, float(C["spot"]), parse_ms(C["asof"]))
        worst = 0.0
        for name, p in panels.items():
            ref = (T.get("panels") or {}).get(name) or {}
            rg = {float(k): v for k, v in (ref.get("gamma") or {}).items()}
            lg = {float(k): v for k, v in p["gamma"].items()}
            keys = set(rg) | set(lg)
            d = max((abs(rg.get(k, 0) - lg.get(k, 0)) for k in keys), default=0)
            worst = max(worst, d)
            rf = (ref.get("hvl") or [None])[0]; lf = (p["hvl"] or [None])[0]
            say(f"  {name:5s} strikes {len(keys)}  max |gamma diff| {d:.1f}   flip sheet {rf} live {lf}   net sheet {ref.get('net_total')} live {p['net_total']}")
        say("check: " + ("the sheet is recovered" if worst <= 2 else "MISMATCH -- the port drifts from trinity_db"))
        return 0 if worst <= 2 else 1

    prev_last, prev_t, mtime, C = None, 0.0, None, None
    while True:
        try:
            m = CHAIN.stat().st_mtime
            if m != mtime:
                C = json.loads(CHAIN.read_text(encoding="utf-8")); mtime = m
                say(f"chain_live {C.get('report_date')} {C.get('rail_label', '')} spot {C.get('spot')} asof {C.get('asof')} ({len(C['books'])} books)")
                prev_last = None
            last, src = last_print()
            now = time.time()
            ok, why = contract_ok(C)
            if not ok:
                # Stale levels are recoverable; levels on the wrong axis are not, and the
                # chart cannot tell the difference. Said once per reason, not every 5 s.
                if getattr(main, "_warned", None) != why:
                    say(f"REFUSING to write: {why} -- the chart keeps the sheet's levels")
                    main._warned = why
            elif last is None:
                say("no print available")
            elif prev_last is None or abs(last - prev_last) >= a.min_move or now - prev_t >= 60:
                now_ms = now * 1000.0
                try:
                    panels = reprice(C, last, now_ms)
                except ValueError as ex:
                    say(f"{ex} -- writing nothing"); panels = None
                if panels:
                    doc = write_out(C, last, src, now_ms, panels)
                    z = panels["0DTE"]
                    say(f"{doc['asof'][11:19]}Z last {last:,.2f} ({src})  0DTE flip {z['hvl']} net {z['net_total']}  "
                        f"NEAR flip {panels['NEAR']['hvl']}  FULL flip {panels['FULL']['hvl']}")
                    prev_last, prev_t = last, now
        except Exception as ex:
            say(f"error: {type(ex).__name__}: {ex}")
        if a.once:
            return 0
        time.sleep(a.every)


if __name__ == "__main__":
    sys.exit(main())
