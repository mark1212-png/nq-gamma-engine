# -*- coding: utf-8 -*-
"""
read_tape.py -- everything a tape read needs, printed once, so the narration is about
what happened and not about assembling it.

    python read_tape.py                       this session, from the 22:00 UTC reopen
    python read_tape.py --since 01:18         and what changed since that UTC time (the last read)
    python read_tape.py --session 2026-09-16  a session other than the clock's
    python read_tape.py --bars 20             more one-minute bars at the end

Reads data/bars/<utc-day>.json (live_feed.py's cache; nothing is fetched), the session's
sheets (cards, via setup_track.parse_setups), trinity.json (the drawn band, if it is this
session's) and notes/tape/<session>.md (the ledger). Cards are graded with setup_track, the
dashboard's grader, over the same windows tape_ledger.py uses: the overnight plan from the
reopen, the RTH plan from the reopen until the open (an entry printed overnight is spent)
and from the open afterwards. Cards a rebuild retired are read back from the ledger and
graded too. The 20:00 UTC front expiry closes the session; 21:00-22:00 UTC is the halt.
"""
import sys, json, datetime, pathlib, argparse

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import setup_track as ST                                   # noqa: E402
import tape_ledger as TL                                   # noqa: E402

UTC = datetime.timezone.utc
PT = datetime.timezone(datetime.timedelta(hours=-7), "PT")   # PDT; the sheets and Mark's clock


def pt(hhmm):
    """'01:57' UTC -> '01:57Z (18:57 PT)'."""
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    return f"{hhmm}Z ({(h - 7) % 24:02d}:{m:02d} PT)"


def fmt(x):
    return f"{x:,.2f}"


def main():
    ap = argparse.ArgumentParser(description="print the tape since the reopen, the cards' fates and the ledger")
    ap.add_argument("--session", default=None)
    ap.add_argument("--since", default=None, help="UTC HH:MM of the previous read; adds a 'since then' block")
    ap.add_argument("--bars", type=int, default=12)
    a = ap.parse_args()

    now = datetime.datetime.now(UTC)
    session = datetime.date.fromisoformat(a.session) if a.session else TL.session_for(now)
    reopen = TL.reopen_of(session)
    start_key, rth_key, end_key = reopen.strftime("%Y-%m-%d %H:%M"), f"{session} 13:30", f"{session} 20:00"
    print(f"session {session}   reopen {reopen:%Y-%m-%d %H:%M}Z   now {now:%H:%M}Z ({now.astimezone(PT):%H:%M} PT)")

    # ---- feed --------------------------------------------------------------------
    st = TL.read_json(TL.BARS / "feed_status.json") or {}
    try:
        age = (now - datetime.datetime.fromisoformat(st["updated"])).total_seconds()
    except Exception:
        age = None
    print(f"feed    {st.get('state', '?')}, last bar {str(st.get('last_bar_ts', '?'))[11:16]}Z px {st.get('last_px')}"
          + (f", status {age:.0f}s old" if age is not None else "") + ("   <-- STALE, read your own tape" if age and age > 180 else ""))

    # ---- bars --------------------------------------------------------------------
    rows = []
    for d in (reopen.date().isoformat(), session.isoformat()):
        f = TL.BARS / f"{d}.json"
        if f.exists():
            b = TL.read_json(f) or []
            rows.extend(x for x in b if start_key <= x["ts"][:16] <= end_key)
    rows.sort(key=lambda x: x["ts"])
    if not rows:
        print("no bars for this session yet"); return 0
    bars = rows
    hi, lo = max(bars, key=lambda x: x["h"]), min(bars, key=lambda x: x["l"])
    last = bars[-1]
    print(f"\nSINCE THE REOPEN  open {fmt(bars[0]['o'])}  high {fmt(hi['h'])} at {pt(hi['ts'][11:16])}  "
          f"low {fmt(lo['l'])} at {pt(lo['ts'][11:16])}  last {fmt(last['c'])} at {pt(last['ts'][11:16])}  "
          f"range {hi['h'] - lo['l']:,.2f}  bars {len(bars)}")
    if a.since:
        seg = [b for b in bars if b["ts"][11:16] > a.since] if a.since > bars[0]["ts"][11:16] else \
              [b for b in bars if b["ts"][:10] == session.isoformat() and b["ts"][11:16] > a.since]
        if seg:
            h2, l2 = max(seg, key=lambda x: x["h"]), min(seg, key=lambda x: x["l"])
            print(f"SINCE {a.since}Z       high {fmt(h2['h'])} at {pt(h2['ts'][11:16])}  low {fmt(l2['l'])} at {pt(l2['ts'][11:16])}  "
                  f"last {fmt(seg[-1]['c'])}  net {seg[-1]['c'] - seg[0]['o']:+,.2f}")

    print("\n30-MINUTE PATH (UTC)")
    buckets = {}
    for b in bars:
        k = b["ts"][:10] + " " + b["ts"][11:13] + (":00" if int(b["ts"][14:16]) < 30 else ":30")
        buckets.setdefault(k, []).append(b)
    for k, bs in buckets.items():
        print(f"  {k[11:]}Z  o {fmt(bs[0]['o'])}  h {fmt(max(x['h'] for x in bs))}  l {fmt(min(x['l'] for x in bs))}  c {fmt(bs[-1]['c'])}")
    print(f"\nLAST {a.bars} BARS")
    for b in bars[-a.bars:]:
        print(f"  {b['ts'][11:16]}Z  {fmt(b['o'])} {fmt(b['h'])} {fmt(b['l'])} {fmt(b['c'])}")

    # ---- the plans and their cards --------------------------------------------------
    T = TL.read_json(ROOT / "trinity.json") or {}
    band = None
    if T.get("report_date") == session.isoformat() and T.get("rail_lo"):
        band = (float(T["rail_lo"]), float(T["rail_hi"]))
        em = float(T.get("em") or 0) * float(T.get("rail_scale") or 1)
        ref = float(T.get("last_print") or T.get("spot") or 0)
        print(f"\nLIVE BUILD  {T.get('rail_label')}  reference {fmt(ref)}  band {fmt(band[0])}-{fmt(band[1])} (±{em:,.0f}, reach ±{em / 2:,.0f})  "
              f"last {fmt(last['c'])} is {last['c'] - ref:+,.2f} from the reference, "
              + ("inside the band" if band[0] <= last["c"] <= band[1] else "OUTSIDE THE BAND"))
    plans = [TL.Plan("ASIA", ROOT / "sheets" / f"{session}-asia-teddyai.html"),
             TL.Plan("RTH", ROOT / "sheets" / f"{session}-teddyai.html")]
    for p in plans:
        p.refresh()
    retired = []
    jl = TL.TAPE / f"{session}.jsonl"
    if jl.exists():
        for ln in jl.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            if rec.get("kind") == "plan" and rec.get("retired"):
                retired.extend(rec["retired"])

    def segment(label):
        return bars if (label == "ASIA" or last["ts"][:16] < rth_key) else [b for b in bars if b["ts"][:16] >= rth_key]

    def grade(label, c):
        r = ST.track(c, segment(label))
        if r["state"] == "expired" and last["ts"][:16] < end_key:
            approached = "never came within" not in r["detail"]
            r["state"], r["detail"] = "not in play yet", f"entry {c['entry']:,.0f} not reached" + ("  -- approached (within a stop-width)" if approached else "")
        return r

    print("\nCARDS")
    watch = set()
    for p in plans:
        if not p.cards:
            print(f"  {p.label}: no sheet for this session"); continue
        for c in p.cards:
            r = grade(p.label, c)
            ex = f"  mfe {r['mfe']:+.2f} ({r.get('r_mfe')}R) mae {r['mae']:+.2f}" if r.get("mfe") is not None else ""
            spent = "  SPENT (printed before the open)" if (p.label == "RTH" and last["ts"][:16] < rth_key and r["state"] != "not in play yet") else ""
            print(f"  {p.label:5s} {c['id']:24s} {c['side']:5s} E {c['entry']:>10,.2f}  SL {c['stop']:>10,.2f}  TP {c['tps']}  "
                  f"-> {r['state']}: {r['detail']}{ex}{spent}")
            if r["state"] == "not in play yet":
                print(f"        at {fmt(last['c'])}: {TL.verdict(c, last['c'])}")
            watch.update([c["entry"], c["stop"]] + list(c["tps"]))
    for rc in retired:
        c = rc["card"]; r = grade(rc["label"], c)
        ex = f"  mfe {r['mfe']:+.2f} ({r.get('r_mfe')}R) mae {r['mae']:+.2f}" if r.get("mfe") is not None else ""
        print(f"  {rc['label']:5s} {c['id']:24s} {c['side']:5s} E {c['entry']:>10,.2f}  SL {c['stop']:>10,.2f}  TP {c['tps']}  "
              f"-> {r['state']}: {r['detail']}{ex}   [retired {rc['since']}Z, previous sheet]")
        watch.update([c["entry"], c["stop"]] + list(c["tps"]))

    # ---- levels touched ------------------------------------------------------------
    base = round(bars[0]["o"] / 50) * 50
    watch.update(base + k * 50 for k in range(-6, 7))
    print("\nLEVELS (first touch since the reopen)")
    for L in sorted(watch, reverse=True):
        t = next((b for b in bars if b["l"] <= L <= b["h"]), None)
        side = "above" if last["c"] > L else "below"
        print(f"  {L:>10,.2f}  {('touched ' + pt(t['ts'][11:16])) if t else 'untouched':28s}  price {side}, {abs(last['c'] - L):,.2f} away")

    # ---- the ledger ----------------------------------------------------------------
    md = TL.TAPE / f"{session}.md"
    print("\nLEDGER" + (f" (since {a.since}Z)" if a.since else "") + f"  {md}")
    if md.exists():
        lines = [l for l in md.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
        if a.since:
            # ledger times are UTC HH:MM; 22:00-23:59 belong to the reopen day, 00:00-20:00 to the session day
            def after(t):
                prev_day, since_prev = t >= "22:00", a.since >= "22:00"
                if since_prev:
                    return (prev_day and t > a.since) or not prev_day
                return (not prev_day) and t > a.since
            lines = [l for l in lines if after(l[:5])]
        for l in lines[-40:]:
            print("  " + l[:230])
        if not lines:
            print("  (nothing yet)")
    else:
        print("  no ledger for this session -- is tape_ledger.py running?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
