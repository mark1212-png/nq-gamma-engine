# -*- coding: utf-8 -*-
"""
tape_ledger.py -- the tape written down AS IT HAPPENS, for the next plan to read.

    python tape_ledger.py                 a service: this session, then the next, and so on
    python tape_ledger.py --once          one pass: write the current state, exit
    python tape_ledger.py --session 2026-09-16    that session only, then exit

Why this exists (Mark, 2026-09-15): "what if you monitor for context when
regenerated a new game plan ... we are missing what actually occurred during
plan generation ... a trade already happened but we will still generate it".
The regeneration reads the bars at build time -- `command_center._tape_summary`
gives the model the overnight, the session's turns and a per-level ledger, and
`_pre_plan` folds each card over the day. What that cannot give is the ORDER
things happened in, the previous sheet's own verdict on each card at each open
(live / not live / void / spent), a plan rebuilt mid-session, or a feed that went
blind for twenty minutes. This writes those down, one line per event, to
notes/tape/<session>.md (and .jsonl for machines); `regenerate_commentary` and
`precommentary.py --candidates` put the file in front of whoever writes the
next commentary.

It reads data/bars/<utc-day>.json, which live_feed.py writes every few seconds,
so it makes no Databento calls; and it prints almost nothing -- "armed", a feed
that has gone stale or come back, a plan rebuilt, "done", and "waiting" -- so it
can run under a Claude Code Monitor without waking the session per tick, and as
one of the stack's consoles under start_teddy.ps1 (Mark: "I would not want to
depend on having a claude session open"). A session's ledger closes at the
20:00 UTC front expiry; the service then waits for the next 22:00 UTC reopen and
starts that session's file. Cards are graded with setup_track.track /
track_close, the dashboard's grader.
"""
import sys, os, json, time, datetime, pathlib, argparse

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import setup_track as ST                                   # noqa: E402
import nqvol                                               # noqa: E402

BARS = ROOT / "data" / "bars"
TAPE = ROOT / "notes" / "tape"
UTC = datetime.timezone.utc
OPENS = [("00:00", "Tokyo open"), ("07:00", "London open"), ("13:30", "New York open")]
EXTREME_STEP = 25.0            # a new session high/low is an event once it extends by this much
STALE_S = 180                  # feed silence that counts as blind


def say(msg):
    print(msg, flush=True)


def read_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None                                        # mid-write; next pass


def session_for(now):
    """The session this tape belongs to: after the 22:00 UTC reopen it is the next one, and
    after the 20:00 UTC close (the halt) there is nothing until that reopen."""
    d = now.date()
    if now.hour >= 22:
        return nqvol.next_session(d)
    if nqvol.is_session(d) and now.hour < 20:
        return d
    return nqvol.next_session(d)


def reopen_of(session):
    return datetime.datetime.combine(session - datetime.timedelta(days=1), datetime.time(22, 0), UTC)


def close_of(session):
    return datetime.datetime.combine(session, datetime.time(20, 0), UTC)


class Plan:
    """One sheet's cards, re-read whenever the file changes (the 22:20 and 06:05 jobs rewrite it)."""
    def __init__(self, label, path):
        self.label, self.path, self.mtime, self.cards = label, path, None, []
        self.previous = []           # the cards the last rebuild dropped (same id+geometry = kept, not dropped)

    def refresh(self):
        if not self.path.exists():
            return False
        m = self.path.stat().st_mtime
        if m == self.mtime:
            return False
        try:
            cards = ST.parse_setups(self.path.read_text(encoding="utf-8"))
        except Exception:
            return False
        changed = self.mtime is not None and [c["id"] for c in cards] != [c["id"] for c in self.cards]
        if changed:
            self.previous = [c for c in self.cards
                             if not any(c["id"] == n["id"] and c["entry"] == n["entry"] and c["stop"] == n["stop"] for n in cards)]
        self.mtime, self.cards = m, cards
        return changed


def verdict(card, px):
    """The sheet's own rule on a card, read from its geometry at a print: live / not live / void."""
    e, s, side = card["entry"], card["stop"], card["side"]
    if card.get("exec") == "close":
        level, risk = round(e / 25.0) * 25.0, abs(e - s) or 10.0
        beyond = (px - level) > risk if side == "long" else (level - px) > risk
        return "beyond the level -- live on a retest that holds" if beyond else "waiting for a 5-minute close beyond the level"
    if side == "short":
        if px > s: return "VOID -- past the stop"
        if px > e: return "not live -- above the entry, needs a return from below"
        return f"live on the approach, {e - px:,.2f} away"
    if px < s: return "VOID -- past the stop"
    if px < e: return "not live -- under the entry, needs a return from above"
    return f"live on the approach, {px - e:,.2f} away"


def run_session(session, once=False, every=20.0):
    """Write one session's ledger until its 20:00 UTC close. Returns when it is closed."""
    reopen = reopen_of(session)
    start_key = reopen.strftime("%Y-%m-%d %H:%M")
    rth_key, end_key = f"{session} 13:30", f"{session} 20:00"
    days = [reopen.date().isoformat(), session.isoformat()]
    TAPE.mkdir(parents=True, exist_ok=True)
    md, jl = TAPE / f"{session}.md", TAPE / f"{session}.jsonl"
    if not md.exists():
        md.write_text(f"# Tape ledger -- session {session} (UTC, from the live feed; one line per event)\n", encoding="utf-8")

    def event(kind, ts, text, **fields):
        line = f"{ts} {text}"
        with md.open("a", encoding="utf-8") as f: f.write(line + "\n")
        with jl.open("a", encoding="utf-8") as f: f.write(json.dumps({"ts": ts, "kind": kind, "text": text, **fields}) + "\n")

    # WHAT IS ALREADY WRITTEN. The ledger is the bars' sequence, so a restart -- the console
    # closed for an hour, or the stack started at 06:00 after a whole overnight -- walks the
    # bars from the reopen and writes what it finds, minus what an earlier run already wrote.
    # Nothing is lost for having been closed; what is lost is only real-time.
    prior_cards, prior_levels, prior_opens, restart = {}, set(), set(), False
    prior_defs, last_seen = {}, {}      # skey -> the card's geometry / the last time a record named it
    retired = []                 # [{label, since, card}] -- cards a rebuild dropped, graded to the close
    new_on_restart = []          # ...of which this start derived from the file rather than saw happen
    if jl.exists():
        for ln in jl.read_text(encoding="utf-8").splitlines():
            try: rec = json.loads(ln)
            except Exception: continue
            k = rec.get("kind")
            if k == "armed":
                restart = True; prior_cards.update(rec.get("cards") or {}); prior_defs.update(rec.get("defs") or {})
                for kk in (rec.get("cards") or {}): last_seen[kk] = rec.get("ts")
            elif k == "card" and rec.get("card"):
                cd = rec["card"]; prior_cards[cd["key"]] = cd["state"]; last_seen[cd["key"]] = rec.get("ts")
                if cd.get("def"): prior_defs[cd["key"]] = cd["def"]
            elif k == "level" and rec.get("level") is not None: prior_levels.add(float(rec["level"]))
            elif k == "open" and rec.get("open"): prior_opens.add(rec["open"])
            elif k == "plan" and rec.get("retired"): retired.extend(rec["retired"])
    recovered = 0

    def hhmm_now():
        return datetime.datetime.now(UTC).strftime("%H:%M")

    plans = [Plan("ASIA", ROOT / "sheets" / f"{session}-asia-teddyai.html"),
             Plan("RTH", ROOT / "sheets" / f"{session}-teddyai.html")]
    for p in plans: p.refresh()
    for skey, c in prior_defs.items():
        if "~retired|" in skey or "|" not in skey: continue
        label = skey.split("|", 1)[0]
        plan = next((q for q in plans if q.label == label), None)
        if plan is None: continue
        live = any(n["id"] == c["id"] and n["entry"] == c["entry"] and n["stop"] == c["stop"] for n in plan.cards)
        already = any(r["label"] == label and r["card"]["id"] == c["id"] and r["card"]["entry"] == c["entry"] for r in retired)
        if not live and not already:
            since = (last_seen.get(skey) or "restart")
            rc = {"label": label, "since": since, "card": c}
            retired.append(rc); new_on_restart.append(rc)
            prior_cards.setdefault(f"{label}~retired|{c['id']}", prior_cards.get(skey, "not in play yet"))
    T = read_json(ROOT / "trinity.json") or {}
    band = (T.get("rail_lo"), T.get("rail_hi")) if T.get("report_date") == session.isoformat() else (None, None)

    seen, touched, opens_done = {}, set(), set()
    hi = lo = None
    watch = None
    stale = False
    last_ts = None
    first = True

    while True:
        rows, ok = [], True
        for d in days:
            f = BARS / f"{d}.json"
            if f.exists():
                b = read_json(f)
                if b is None: ok = False; break
                rows.extend(x for x in b if start_key <= x["ts"][:16] <= end_key)
        if not ok:
            time.sleep(every); continue
        rows.sort(key=lambda x: x["ts"])
        bars = rows
        st = read_json(BARS / "feed_status.json") or {}
        try:
            age = (datetime.datetime.now(UTC) - datetime.datetime.fromisoformat(st.get("updated"))).total_seconds()
        except Exception:
            age = None
        if age is not None and age > STALE_S and not stale:
            stale = True
            msg = f"FEED stale: live_feed {st.get('state', '?')}, last update {age/60:.0f} min ago" + (f"; bars stop at {bars[-1]['ts'][11:16]}Z" if bars else "")
            event("feed", hhmm_now(), msg); say(msg)
        elif age is not None and age <= STALE_S and stale:
            stale = False
            msg = "FEED back: streaming again" + (f", last bar {bars[-1]['ts'][11:16]}Z {bars[-1]['c']:,.2f}" if bars else "")
            event("feed", hhmm_now(), msg); say(msg)

        for p in plans:
            if p.refresh() and not first:
                now_h = hhmm_now()
                gone = [{"label": p.label, "since": now_h, "card": c} for c in p.previous]
                retired.extend(gone)
                msg = (f"{p.label} plan rebuilt: cards now " + ", ".join(f"{c['id']} ({c['side']} {c['entry']:,.0f}/{c['stop']:,.0f})" for c in p.cards)
                       + (("; retired " + ", ".join(c["id"] for c in p.previous) + " -- still graded to the close") if p.previous else ""))
                event("plan", now_h, msg, retired=gone); say(msg)

        if bars and (first or bars[-1]["ts"] != last_ts):
            last_ts = bars[-1]["ts"]; last = bars[-1]; t = last["ts"][11:16]
            shi, slo = max(b["h"] for b in bars), min(b["l"] for b in bars)
            if watch is None:                                  # the 50-point rungs around the reopen print
                base = round(bars[0]["o"] / 50) * 50
                watch = sorted({base + k * 50 for k in range(-8, 9)} | {c["entry"] for p in plans for c in p.cards} | {c["stop"] for p in plans for c in p.cards})
            if hi is None:
                hi, lo = shi, slo
            else:
                if shi >= hi + EXTREME_STEP: event("extreme", t, f"new session high {shi:,.2f} (+{shi-hi:.0f}), last {last['c']:,.2f}"); hi = shi
                if slo <= lo - EXTREME_STEP: event("extreme", t, f"new session low {slo:,.2f} (-{lo-slo:.0f}), last {last['c']:,.2f}"); lo = slo
            for L in watch:
                if L in touched: continue
                for b in bars:
                    if b["l"] <= L <= b["h"]:
                        touched.add(L)
                        if not first or L not in prior_levels:
                            gap = ("  (recovered on restart)" if restart else "  (before this run started)") if first else ""
                            if first: recovered += 1
                            event("level", b["ts"][11:16], f"first touch of {L:,.2f} (bar {b['o']:,.2f}/{b['h']:,.2f}/{b['l']:,.2f}/{b['c']:,.2f}), last {last['c']:,.2f}{gap}", level=L)
                        break
            def segment(label):
                # the RTH plan is graded from the reopen until the open (an entry printed overnight is
                # spent) and from the open afterwards, which is how the dashboard grades it
                return bars if (label == "ASIA" or last["ts"][:16] < rth_key) else [b for b in bars if b["ts"][:16] >= rth_key]

            def grade(label, c, shown, skey):
                """Track one card and write a line when its state changes (or, on the first pass, when it
                differs from what an earlier run had written)."""
                nonlocal recovered
                try:
                    r = ST.track(c, segment(label))
                except Exception as ex:
                    event("error", t, f"tracker error on {shown} {c['id']}: {ex}"); return
                if r["state"] == "expired" and last["ts"][:16] < end_key:
                    approached = "never came within" not in r["detail"]
                    r["state"], r["detail"] = "not in play yet", f"entry {c['entry']:,.0f} not reached" + ("  -- approached (within a stop-width)" if approached else "")
                key, cur = (shown, c["id"]), (r["state"], r["detail"])
                if seen.get(key) != cur:
                    known = prior_cards.get(skey, "not in play yet")
                    if not first or r["state"] != known:
                        extra = f"  mfe {r['mfe']:+.2f} ({r.get('r_mfe')}R) mae {r['mae']:+.2f}" if r.get("mfe") is not None else ""
                        spent = "  -- an RTH entry printed before the open: SPENT" if (label == "RTH" and last["ts"][:16] < rth_key and r["state"] not in ("not in play yet",)) else ""
                        gap = ("  (recovered on restart)" if restart else "  (before this run started)") if first else ""
                        if first: recovered += 1
                        event("card", t, f"{shown} {c['id']}: {r['state']} -- {r['detail']}{extra}{spent}; last {last['c']:,.2f}{gap}",
                              card={"key": skey, "label": label, "id": c["id"], "state": r["state"], "detail": r["detail"], "def": c})
                    seen[key] = cur

            for p in plans:
                for c in p.cards:
                    grade(p.label, c, p.label, f"{p.label}|{c['id']}")
            # A CARD A REBUILD RETIRED IS STILL A CARD (Mark, 2026-09-15: the 29,250 hold filled at
            # 01:07Z, forty minutes after the 17:28 regeneration had replaced it, and nothing wrote
            # down how it went). It keeps its grade to the close so the next plan knows how the last
            # one's calls resolved, marked with the time it was retired.
            for rc in retired:
                c = rc["card"]
                grade(rc["label"], c, f"{rc['label']} {c['id']} [retired {rc['since']}Z, previous sheet]".replace(f" {c['id']} [", " ["), f"{rc['label']}~retired|{c['id']}")
            for hhmm, name in OPENS:
                key = f"{session} {hhmm}"
                if hhmm in opens_done or last["ts"][:16] < key: continue
                opens_done.add(hhmm)
                if hhmm in prior_opens: continue
                b0 = next(b for b in bars if b["ts"][:16] >= key)
                px = b0["o"]
                inb = "" if band[0] is None else (" inside the drawn band" if band[0] <= px <= band[1] else f" OUTSIDE the drawn band {band[0]:,.0f}-{band[1]:,.0f}: every card void")
                parts = [f"{name} printed {px:,.2f}; since the reopen {slo:,.2f}-{shi:,.2f}{inb}"]
                for p in plans:
                    for c in p.cards:
                        stt = seen.get((p.label, c["id"]), ("", ""))[0]
                        parts.append(f"{p.label} {c['id']}: " + (stt if stt not in ("", "not in play yet") else verdict(c, px)))
                for rc in retired:
                    c = rc["card"]; shown = f"{rc['label']} [retired {rc['since']}Z, previous sheet]"
                    stt = seen.get((shown, c["id"]), ("", ""))[0]
                    parts.append(f"{shown} {c['id']}: " + (stt if stt not in ("", "not in play yet") else verdict(c, px)))
                event("open", hhmm, " | ".join(parts), open=hhmm)
            if first:
                first = False
                if new_on_restart:
                    named = [f"{rc['label']} {rc['card']['id']} (last seen {rc['since']}Z)" for rc in new_on_restart]
                    event("plan", t, "on restart: no longer on its sheet, still graded to the close -- " + ", ".join(named),
                          retired=new_on_restart)
                what = f"restarted (recovered {recovered} event(s) from the bars)" if restart else "armed"
                msg = (f"{what} {t}Z: last {last['c']:,.2f}, since the reopen {slo:,.2f}-{shi:,.2f}, touched {[f'{x:,.0f}' for x in sorted(touched)]}; "
                       f"feed {st.get('state', '?')}; " + "; ".join(f"{l} {i} {v[0]}" for (l, i), v in seen.items()))
                event("armed", t, msg, cards={f"{l}|{i}": v[0] for (l, i), v in seen.items()},
                      defs={f"{p.label}|{c['id']}": c for p in plans for c in p.cards}); say(msg)
            if last["ts"][:16] >= end_key:
                event("done", t, "front expiry: ledger closed"); say(f"done: {session} closed at 20:00 UTC")
                return
        # a heartbeat for the dashboard's Operations list: alive, which session, how far the
        # tape it has read goes, how many lines the ledger holds
        try:
            hb = {"updated": datetime.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "pid": os.getpid(),
                  "session": session.isoformat(), "last_bar": (bars[-1]["ts"][11:16] + "Z") if bars else None,
                  "last_px": bars[-1]["c"] if bars else None, "events": sum(1 for _ in md.open(encoding="utf-8")) - 1,
                  "cards": {f"{l} {i}": v[0] for (l, i), v in seen.items()}, "feed_stale": stale}
            tmp = TAPE / "ledger-status.tmp"; tmp.write_text(json.dumps(hb), encoding="utf-8"); os.replace(tmp, TAPE / "ledger-status.json")
        except Exception:
            pass
        if once:
            return
        time.sleep(every)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--session", default=None, help="YYYY-MM-DD: that session only, then exit")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--every", type=float, default=20.0)
    a = ap.parse_args()
    if a.session:
        run_session(datetime.date.fromisoformat(a.session), a.once, a.every)
        return 0
    while True:
        now = datetime.datetime.now(UTC)
        session = session_for(now)
        reopen = reopen_of(session)
        if now < reopen:
            say(f"waiting for the {session} session: reopen {reopen:%Y-%m-%d %H:%M}Z")
            while datetime.datetime.now(UTC) < reopen:
                if a.once: return 0
                time.sleep(60)
        run_session(session, a.once, a.every)
        if a.once:
            return 0
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
