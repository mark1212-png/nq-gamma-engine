#!/usr/bin/env python3
"""
feedcheck.py -- is the live price on this machine actually live, and does every
layer agree with every other one?

Written 2026-09-14 after the feed died twice in one session and announced itself
as "streaming" both times. Nothing here is new information; the point is that it
is all in ONE place, because every failure so far has been a DISAGREEMENT between
layers rather than any single layer reporting an error:

  * 17:49 UTC -- live_feed died. feed_status.json froze with state "streaming"
    and last_px 29,289.50 while the bar file went on to 29,190.50. No component
    complained: the dashboard's own feed_status() correctly returned None on the
    heartbeat age, but the bar file was still being topped up by the dashboard's
    Databento backfill, so `close` reported "1,250 bars" and exit 0.
  * 21:07 UTC -- the close job ran 11 minutes short of the session because the
    ohlcv-1m schema trails real time by 10-15 minutes and _clamp_end honoured it.
    Again: no error, just a number that was quietly not the close.
  * 2026-09-11 -- 29,468.75 reached the published sheet as the reference. It was
    the 08:51 PT print of a feed that had already died.

Read the VERDICT line. Everything above it is the evidence for it.

    python feedcheck.py                     check the live feed (whatever contract
                                            live_feed.py is on -- it says which)
    python feedcheck.py --basis 289.25      only while the feed is on the continuous
                                            NQ.c.0: add the calendar basis so the
                                            number can be eyeballed against
                                            TopstepX / TradingView directly. With
                                            the feed pinned to the traded contract
                                            (2026-09-14, NQZ6) the raw number IS
                                            the platform's and no basis is needed.
"""
import argparse, datetime, json, pathlib, sys, urllib.request

UTC = datetime.timezone.utc
ROOT = pathlib.Path(__file__).parent.resolve()
STALE_HEARTBEAT_S = 90          # the same rule teddy.feed_status uses
STALE_BAR_MIN = 3.0             # a live session should not be older than this
AGREE_TOL = 0.25                # feed last_px vs bar close, in points

ok = lambda s: f"  ok    {s}"
warn = lambda s: f"  WARN  {s}"
bad = lambda s: f"  FAIL  {s}"


def _get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _port_open(port):
    import socket
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT), help="project root holding data/ (default: this file's folder)")
    ap.add_argument("--basis", type=float, default=None,
                    help="continuous feed only: add the calendar basis to show the traded contract "
                         "(NQZ6 = NQU6 + basis); ignored with a note when the feed is already pinned")
    a = ap.parse_args()
    root = pathlib.Path(a.root)
    data = root / "data" / "bars"
    now = datetime.datetime.now(UTC)
    problems, notes = [], []

    print(f"\nfeedcheck  {now:%Y-%m-%d %H:%M:%S}Z   ({now.astimezone():%H:%M:%S %Z})   root {root}")

    # ---- 1. is the market even open? ------------------------------------
    # Globex: 22:00 UTC reopen -> 21:00 UTC close, with the 21:00-22:00 break.
    h = now.hour + now.minute / 60
    if 21.0 <= h < 22.0:
        session = "MAINTENANCE BREAK (21:00-22:00 UTC) -- no prints expected"
    elif now.weekday() == 5 or (now.weekday() == 4 and h >= 21) or (now.weekday() == 6 and h < 22):
        session = "WEEKEND -- feed dark until the Sunday 22:00 UTC reopen"
    else:
        session = "session open"
    live_expected = session == "session open"
    print(f"\nclock\n  {session}")

    # ---- 2. the feeder's own heartbeat ----------------------------------
    print("\nfeed_status.json")
    fs, fs_px = None, None
    p = data / "feed_status.json"
    if not p.exists():
        print(bad("missing -- live_feed.py has never run here"))
        problems.append("no feed_status.json")
    else:
        try:
            fs = json.loads(p.read_text(encoding="utf-8"))
            age = (now - datetime.datetime.fromisoformat(fs["updated"])).total_seconds()
            fs_px = fs.get("last_px")
            print(f"  state {fs.get('state')!r}  contract {fs.get('contract')}  last_px {fs_px}  "
                  f"quiet_s {fs.get('quiet_s')}")
            print(f"  heartbeat written {age:,.0f}s ago")
            # THE STATE FIELD IS NOT EVIDENCE. A killed process leaves "streaming"
            # behind it forever; only the age of the write says anything.
            if fs.get("state") not in ("streaming", "connecting"):
                print(bad(f"state is {fs.get('state')!r} -- the feeder stopped"))
                problems.append(f"feeder state {fs.get('state')!r}")
            elif age > STALE_HEARTBEAT_S:
                print(bad(f"claims {fs.get('state')!r} but has not written for {age:,.0f}s "
                          f"(> {STALE_HEARTBEAT_S}s) -- DEAD PROCESS, stale file"))
                problems.append("heartbeat cold while claiming to stream")
            elif live_expected:
                print(ok("heartbeat fresh"))
            else:
                print(ok("heartbeat fresh (market closed, so this is a bonus)"))
        except Exception as ex:
            print(bad(f"unreadable -- {type(ex).__name__}: {ex}"))
            problems.append("feed_status.json unreadable")

    # ---- 3. the bar file, which is what every build actually reads -------
    print(f"\nbar file  data/bars/{now:%Y-%m-%d}.json")
    bars, bar_px, bar_age = None, None, None
    bp = data / f"{now:%Y-%m-%d}.json"
    if not bp.exists():
        print(bad("missing for today"))
        problems.append("no bar file for today")
    else:
        try:
            bars = json.loads(bp.read_text(encoding="utf-8"))
            last = bars[-1]
            lt = datetime.datetime.fromisoformat(str(last["ts"]).replace(" ", "T"))
            bar_age = (now - lt).total_seconds() / 60
            bar_px = last["c"]
            print(f"  {len(bars):,} bars   last {lt:%H:%M}Z close {bar_px:,.2f}   {bar_age:,.1f} min old")
            if live_expected and bar_age > STALE_BAR_MIN:
                print(bad(f"last bar is {bar_age:,.1f} min old in an open session -- not advancing"))
                problems.append(f"bars {bar_age:.0f} min stale")
            elif live_expected:
                print(ok("advancing"))
            else:
                print(ok("not expected to advance right now"))
            # The 21:00-22:00 UTC maintenance break is not a gap; a UTC bar day
            # spans it, so every weekday file has exactly one and flagging it
            # trains the eye to ignore the warning that matters.
            gaps = []
            prev = None
            for r in bars:
                t = datetime.datetime.fromisoformat(str(r["ts"]).replace(" ", "T"))
                if prev and (t - prev).total_seconds() > 60:
                    straddles_break = prev.hour == 20 and t.hour == 22
                    if not straddles_break:
                        gaps.append((prev, t))
                prev = t
            if gaps:
                print(warn(f"{len(gaps)} gap(s); newest {gaps[-1][0]:%H:%M}Z -> {gaps[-1][1]:%H:%M}Z"))
                notes.append(f"{len(gaps)} bar gap(s)")
            else:
                print(ok("no gaps"))
        except Exception as ex:
            print(bad(f"unreadable -- {type(ex).__name__}: {ex}"))
            problems.append("bar file unreadable")

    # ---- 4. DO THE TWO AGREE? the check that would have caught 09-11 -----
    print("\ncross-check  feeder vs bar file")
    if fs_px is None or bar_px is None:
        print(warn("cannot compare -- one side missing"))
    elif abs(fs_px - bar_px) <= AGREE_TOL:
        print(ok(f"agree at {bar_px:,.2f}"))
    else:
        print(bad(f"DISAGREE: feeder says {fs_px:,.2f}, bar file says {bar_px:,.2f} "
                  f"({fs_px - bar_px:+,.2f}) -- one of them is stale"))
        problems.append("feeder and bar file disagree")

    # ---- 5. the dashboard, which is the path autopull reads --------------
    print("\ndashboard :5137")
    if not _port_open(5137):
        print(bad("not listening -- start_teddy.ps1"))
        problems.append("dashboard down")
    else:
        try:
            d = _get(f"http://127.0.0.1:5137/api/bars?date={now:%Y-%m-%d}")
            rows = d.get("bars") or []
            err = d.get("stale_error")
            print(f"  {len(rows):,} bars   stale_error {err!r}")
            if err:
                print(bad(f"dashboard reports: {str(err)[:110]}"))
                problems.append("dashboard stale_error")
            elif rows and bars is not None and len(rows) != len(bars):
                print(warn(f"dashboard has {len(rows):,} bars, file has {len(bars):,}"))
                notes.append("dashboard/file bar count differs")
            else:
                print(ok("serving and agrees with the file"))
        except Exception as ex:
            print(warn(f"/api/bars failed -- {type(ex).__name__}: {ex}"))
            notes.append("/api/bars failed")

    # ---- 6. the chart ----------------------------------------------------
    print("\nteddychart :5138")
    print(ok("listening") if _port_open(5138) else warn("not listening (-NoChart, or it died)"))
    if not _port_open(5138):
        notes.append("teddychart down")

    # ---- 7. the number to eyeball against the broker ---------------------
    # WHICH CONTRACT THE FEED IS ON IS DECIDED IN live_feed.py. Pinned to a raw
    # symbol (2026-09-14, NQZ6 for the roll window) the number IS what the
    # platform shows. On NQ.c.0, a CALENDAR-roll symbol, it stays on the expiring
    # front month until expiry while volume (and TopstepX) moved to the next
    # quarter days earlier, and the raw number looks ~290 points wrong when
    # nothing is: that is what --basis is for, and only then.
    if bar_px is not None:
        symbol = (fs or {}).get("symbol") or "NQ.c.0"
        contract = (fs or {}).get("contract") or symbol
        pinned = not symbol.startswith("NQ.")
        print("\ncompare against your platform")
        print(f"  feed symbol {symbol}   contract {contract}   last {bar_px:,.2f}")
        if pinned:
            print(ok(f"pinned to {contract}: this is the number TopstepX / TradingView should show"))
            if a.basis:
                print(warn(f"--basis {a.basis:+,.2f} ignored -- the feed is already on the traded contract; "
                           f"a basis would double-count the roll"))
        elif a.basis:
            print(f"  traded contract at basis {a.basis:+,.2f}   ->  {bar_px + a.basis:,.2f}   "
                  f"<-- this is the number TopstepX / TradingView should show")
        else:
            print(warn(f"{symbol} is the calendar-roll front ({contract}); in roll week pass --basis to convert "
                       f"to the contract your platform trades"))

    # ---- verdict ---------------------------------------------------------
    print("\n" + "-" * 68)
    if problems:
        print(f"VERDICT: NOT TRUSTWORTHY -- {'; '.join(problems)}")
        print("Do not build a plan on this price. Restart with start_teddy.ps1, then re-run.")
    elif notes:
        print(f"VERDICT: usable, with caveats -- {'; '.join(notes)}")
    else:
        print("VERDICT: live and self-consistent.")
    print()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
