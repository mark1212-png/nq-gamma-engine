#!/usr/bin/env python3
"""
verify_pull.py -- is the book the sheet is about to be built on trustworthy?

    python verify_pull.py            exit 0 = fine, 1 = do not build on this

Three checks, each a thing that has actually gone wrong once:
  chain      the chain file trinity.json names (data/<settle>-chain.json, or the
             prelim one) is at least 750 KB -- a tiny file is a failed pull wearing
             a filename (autopull.chain_ok uses 500 KB; this is a little stricter)
  reference  the reference in trinity.json sits inside the settle day's 19:59 UTC
             bar (the settle bar), or -- when the plan anchors on a last print --
             inside the newest bar we hold. Warns only: on 2026-09-03 the OFFICIAL
             settle sat outside the 19:59 bar entirely and was still right.
  oi         total open interest vs the previous business day's chain. A delta
             over 40% means a different product slipped in or a window was empty.
             Skipped (not failed) when there is no prior chain to compare against.
Prints one line per check; the command center shows the last line on failure.
"""
import sys, json, datetime, pathlib

ROOT = pathlib.Path(__file__).parent.resolve()
DATA = ROOT / "data"
MIN_CHAIN = 750_000      # a real chain is 0.9-1.1 MB; the 09-03 preliminary was 0.94 MB and was fine
OI_MAX_DELTA = 0.40
fails, out = 0, []


def say(ok, msg):
    global fails
    out.append(("ok   " if ok else "FAIL ") + msg)
    if not ok:
        fails += 1


def total_oi(p):
    ch = json.loads(p.read_text(encoding="utf-8"))["chain"]
    return sum((r[c].get("oi") or 0) for v in ch.values() for r in v.values() for c in r)


def prev_business_day(d):
    import nqvol
    return nqvol.prev_session(d)


T = json.loads((ROOT / "trinity.json").read_text(encoding="utf-8"))
settle = T.get("settle")
prelim = T.get("oi_vintage") == "preliminary"
chain = (DATA / "prelim" / f"{settle}-chain.json") if prelim else (DATA / f"{settle}-chain.json")
if not chain.exists():                       # vintage label and location can disagree
    alt = (DATA / f"{settle}-chain.json") if prelim else (DATA / "prelim" / f"{settle}-chain.json")
    chain = alt if alt.exists() else chain

# 1. chain size
if not chain.exists():
    say(False, f"chain: {chain.relative_to(ROOT)} missing")
else:
    sz = chain.stat().st_size
    say(sz >= MIN_CHAIN, f"chain: {chain.name} {sz/1e6:.2f} MB ({T.get('oi_vintage','final')}, {T.get('expiries')} expiries)")

# 2. reference inside a bar
# The reference is the LATEST settle at build time, which is not always the
# book's settle date (a Tuesday plan on Thursday's preliminary book anchors on
# Friday's settle). Check the 19:59 UTC bar of every session from the book's
# date up to the report date and pass if it sits inside any of them.
ref = T.get("last_print") or T.get("spot")
try:
    import nqvol
    if T.get("last_print"):
        rows = json.loads((DATA / "bars" / f"{settle}.json").read_text(encoding="utf-8"))
        bar = rows[-1]                                        # anchored on the tape
        inside = bar["l"] - 0.01 <= float(ref) <= bar["h"] + 0.01
        out.append(("ok   " if inside else "warn ") + f"reference {ref:,.2f} {'inside' if inside else 'OUTSIDE'} "
                   f"newest bar {str(bar['ts'])[:16]} [{bar['l']:,.2f}, {bar['h']:,.2f}]"
                   + ("" if inside else " -- check it is not a stale print"))
    else:
        d, rd = datetime.date.fromisoformat(settle), datetime.date.fromisoformat(T.get("report_date") or settle)
        checked, hit = [], None
        while d < rd and len(checked) < 4:
            bf = DATA / "bars" / f"{d}.json"
            if bf.exists():
                rows = json.loads(bf.read_text(encoding="utf-8"))
                bar = next((r for r in rows if str(r["ts"])[11:16] == "19:59"), None)
                if bar:
                    checked.append(d)
                    if bar["l"] - 0.01 <= float(ref) <= bar["h"] + 0.01:
                        hit = (d, bar); break
            d = nqvol.next_session(d)
        if hit:
            out.append(f"ok   reference {ref:,.2f} inside the {hit[0]} 19:59 UTC bar [{hit[1]['l']:,.2f}, {hit[1]['h']:,.2f}]")
        elif checked:
            out.append(f"warn reference {ref:,.2f} OUTSIDE the 19:59 UTC bar on {', '.join(map(str, checked))}"
                       f" -- the official settle can sit outside the bar; check it is not a stale print")
        else:
            out.append(f"warn reference: no 19:59 bar file to check {ref:,.2f} against")
except Exception as ex:
    out.append(f"warn reference: could not check -- {type(ex).__name__}: {ex}")

# 3. OI delta vs the prior day's chain
if chain.exists():
    try:
        cur = total_oi(chain)
        d = datetime.date.fromisoformat(settle)
        prev = None
        for _ in range(4):
            d = prev_business_day(d)
            for cand in (DATA / f"{d}-chain.json", DATA / "prelim" / f"{d}-chain.json"):
                if cand.exists() and cand.stat().st_size > 500_000:
                    prev = cand; break
            if prev: break
        if prev is None:
            out.append(f"skip oi: {cur:,} total; no prior chain within 4 business days to compare")
        else:
            po = total_oi(prev)
            delta = (cur - po) / po if po else 0
            say(abs(delta) <= OI_MAX_DELTA, f"oi: {cur:,} vs {po:,} on {prev.name[:10]} ({delta:+.1%})")
    except Exception as ex:
        say(False, f"oi: could not total -- {type(ex).__name__}: {ex}")

print("\n".join(out))
sys.exit(1 if fails else 0)
