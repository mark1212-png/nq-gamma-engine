# -*- coding: utf-8 -*-
"""
stack_status.py -- is the stack actually running, and is the map still today's?

WHY THIS EXISTS. From 15:45 PT on 2026-09-16 until 13:23 PT on 2026-09-17 the
TeddyAI stack was not running -- no crash, it simply was not started. Twenty-two
hours: the 16:00 settle, 16:10 opra, 17:15 gameplan-asia, 18:30 prelim-poll,
22:15 prelim, 22:20 gameplan-tomorrow and the 06:05 premarket never fired, the
2026-09-16 book was never pulled, and the 09-17 RTH session was traded against a
sheet built twenty-two hours earlier on a two-session-old book -- anchored at
29,280 while price closed 486 points higher at a 29,743.00 settle.

Nothing degraded VISIBLY. The chart kept drawing, the sheet kept rendering,
levels_live.py kept repricing the saved book at the live print, so every surface
looked alive while the book behind it aged by sessions. That is the failure this
module is against: not the outage, the silence.

WHAT IT READS. Files the services already write; nothing here talks to a service,
so a dead one reads as down rather than hanging the caller.

  notes/scheduler-status.json   {updated, pid, jobs[{name, at, last_run, ...}]}
                                rewritten every 20 s by scheduler.py
  notes/autopull-status.json    {job: {ok, at, detail}} -- the last pull per job
  notes/services/*.log          per-service output; the mtime is the heartbeat

THE CHECKS.

  updated older than STALE_S   the scheduler stopped writing -> the stack is down
  pid not running              confirms it, and tells a stopped stack from a
                               slow tick (see pid_alive: never os.kill on
                               Windows, sig 0 there TERMINATES the process)
  missed jobs                  every weekday firing instant inside the hole --
                               between the last tick and now. What the outage
                               cost: seven, on 2026-09-17 at 13:23 PT. It goes
                               to zero the moment the stack comes back up, which
                               is why it is not the only check.
  not run today                jobs due earlier today with no run today. This
                               one SURVIVES the restart: start the stack at
                               13:23 and the 06:05 premarket is still missing,
                               all day, until it is run by hand.
  map behind                   trinity.json's report_date earlier than the
                               trading day. The same failure from the other end:
                               a map for a session that has already traded.
  book behind                  the book the map was built on, in weekdays. One
                               is normal (last night's preliminary). Two is the
                               22-hour hole, and it stays two until a rebuild.

Read-only and cheap, but state() is called from the chart's 1 Hz status pump, so
it caches for CACHE_S and every reader shares one answer.
"""
import os, json, time, datetime, pathlib

ROOT = pathlib.Path(__file__).resolve().parent
NOTES = ROOT / "notes"

STALE_S = 120          # a 20 s tick that has not written for two minutes is not ticking
GRACE_MIN = 30         # the scheduler's own late window is 25; nothing is "missed" inside it
CACHE_S = 5.0
WEEKDAYS = {0, 1, 2, 3, 4}

_cache = {}            # (root, report_date) -> (at, doc); the chart asks at 1 Hz


def pid_alive(pid):
    """
    Is that process still there? True / False / None when it cannot be told.

    NEVER os.kill(pid, 0) here. On Windows CPython implements os.kill by opening
    the process and calling TerminateProcess(handle, sig) -- with sig 0 that
    KILLS the process with exit code 0. The liveness check would take the
    scheduler down. OpenProcess with QUERY_LIMITED_INFORMATION only.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if k.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == 259             # STILL_ACTIVE
                return True
            finally:
                k.CloseHandle(h)
        except Exception:
            return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def _read(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _dt(iso):
    """The naive local datetime the services write, or None."""
    if not iso:
        return None
    try:
        t = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return t.replace(tzinfo=None) if t.tzinfo is None else t.astimezone().replace(tzinfo=None)
    except Exception:
        return None


def _date(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def trading_day(now=None):
    """Today if it is a weekday, else the Friday behind it -- the day the map should be for."""
    d = (now or datetime.datetime.now()).date()
    while d.weekday() not in WEEKDAYS:
        d -= datetime.timedelta(days=1)
    return d


def weekdays_between(a, b):
    """Weekday steps from date a to date b, negative when a is the later one."""
    if not a or not b:
        return None
    lo, hi, sign = (a, b, 1) if a <= b else (b, a, -1)
    n, d = 0, lo
    while d < hi:
        d += datetime.timedelta(days=1)
        if d.weekday() in WEEKDAYS:
            n += 1
    return n * sign


def _at(job):
    """A job's (hh, mm), or None when its time is unreadable."""
    at = str(job.get("at") or "")
    if ":" not in at:
        return None
    try:
        hh, mm = (int(x) for x in at.split(":")[:2])
    except ValueError:
        return None
    return hh, mm


def missed_jobs(jobs, now, since, days=7):
    """
    Every weekday firing instant that fell in the hole -- after `since` (the
    scheduler's last proven tick) and past the grace window -- newest first.

    This is what the outage cost, and it is only true WHILE the stack is down:
    come back up and `since` is seconds ago, so the count is zero again. The
    record of what was lost is not_run_today(), the book age and the map age.
    Bounded to `days` so a fortnight's gap does not enumerate a hundred rows.
    """
    out = []
    floor = max(since, now - datetime.timedelta(days=days)) if since else now - datetime.timedelta(days=days)
    for j in jobs or []:
        hm = _at(j)
        if not hm:
            continue
        for d in range(days, -1, -1):
            day = now.date() - datetime.timedelta(days=d)
            if day.weekday() not in WEEKDAYS:
                continue
            t = datetime.datetime.combine(day, datetime.time(*hm))
            if t <= floor:
                continue                                  # before the hole -- not this outage's loss
            if (now - t).total_seconds() / 60.0 <= GRACE_MIN:
                continue                                  # not due yet, or still inside the grace window
            out.append({"name": j.get("name"), "at": j.get("at"), "when": t.isoformat(timespec="minutes")})
    out.sort(key=lambda m: m["when"], reverse=True)
    return out


def not_run_today(jobs, now):
    """
    Jobs due earlier today that have no run today, in schedule order.

    The check that outlives the restart. On 2026-09-17 the stack came up at
    13:23 and every heartbeat went green, but the 06:05 premarket had not run
    and would not run again until tomorrow -- so the map on every screen was
    still the one built the night before last.
    """
    if now.weekday() not in WEEKDAYS:
        return []
    today = now.date().isoformat()
    out = []
    for j in jobs or []:
        hm = _at(j)
        if not hm:
            continue
        t = datetime.datetime.combine(now.date(), datetime.time(*hm))
        if (now - t).total_seconds() / 60.0 <= GRACE_MIN:
            continue                                      # not due yet, or still inside the grace window
        if j.get("running"):
            continue
        if str(j.get("last_run") or "")[:10] == today:
            continue
        out.append({"name": j.get("name"), "at": j.get("at")})
    out.sort(key=lambda m: m["at"] or "")
    return out


def ago(seconds):
    if seconds is None:
        return "-"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{round(s / 60)}m"
    if s < 86400:
        return f"{s / 3600:.0f}h"
    return f"{round(s / 86400)}d"


def state(root=None, report_date=None, now=None, fresh=False):
    """
    One answer for the whole stack, cached for CACHE_S.

    state: "up" | "stale" | "down" | "unknown"
    line:  the terse string the chart and the dashboard both print.

    report_date, when given, is the map the caller is showing; "behind" is then
    computed against it instead of against trinity.json on disk, so the chart
    reports on the document it actually drew.

    `now` is for the tests; passing it skips the cache, since a cached answer
    for one instant is not an answer for another.
    """
    base = pathlib.Path(root) if root else ROOT
    key = (str(base), report_date)
    if not fresh and now is None:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
    now = now or datetime.datetime.now()

    sc = _read(base / "notes" / "scheduler-status.json") or {}
    updated = _dt(sc.get("updated"))
    age = round((now - updated).total_seconds()) if updated else None
    pid = sc.get("pid")
    alive = pid_alive(pid)

    if not sc or updated is None:
        st, why = "unknown", "no scheduler status file -- the stack has never run here"
    elif age is not None and age > STALE_S:
        st, why = "down", f"last tick {ago(age)} ago"
    elif alive is False:
        st, why = "down", f"pid {pid} is gone"
    elif age is not None and age > 45:
        st, why = "stale", f"last tick {ago(age)} ago"
    else:
        st, why = "up", f"pid {pid}"

    missed = missed_jobs(sc.get("jobs"), now, updated)
    pending = not_run_today(sc.get("jobs"), now)

    T = _read(base / "trinity.json") or {}
    rd = _date(report_date if report_date is not None else T.get("report_date"))
    today = trading_day(now)
    behind = weekdays_between(rd, today) if rd else None      # positive: the map is older than today
    behind = behind if (behind or 0) > 0 else 0
    book = _date(T.get("settle"))
    book_behind = weekdays_between(book, today) if book else None

    # THE PULLS AND THE HEARTBEATS. One indicator for the stack, not for the
    # scheduler alone: a scheduler that ticks while autopull never lands, or a
    # service whose log stopped, is the same silence from another direction.
    pulls = _read(base / "notes" / "autopull-status.json") or {}
    newest_pull, newest_at = None, None
    for job, v in pulls.items():
        t = _dt((v or {}).get("at"))
        if t and (newest_at is None or t > newest_at):
            newest_pull, newest_at = job, t
    pull_age = round((now - newest_at).total_seconds()) if newest_at else None

    services = []
    logs = base / "notes" / "services"
    if logs.is_dir():
        for p in sorted(logs.glob("*.log")):
            if p.name.endswith(".err.log"):
                continue
            try:
                services.append({"name": p.stem, "age": round(time.time() - p.stat().st_mtime)})
            except OSError:
                pass

    # THE LINE. Terse, and the leading token is always printed: the point of all
    # of this is that "everything looks alive" stops being the only thing a
    # screen can say. Absence of an alarm is not the same as a green light.
    bits = []
    if st == "up":
        bits.append("scheduler ok")
    elif st == "stale":
        bits.append(f"scheduler stale {ago(age)}")
    elif st == "down":
        bits.append(f"stack down {ago(age)}" if age is not None else "stack down")
    else:
        bits.append("scheduler unknown")
    if missed:
        bits.append(f"{len(missed)} job{'s' if len(missed) != 1 else ''} missed")
    elif pending:
        bits.append((pending[0]["name"] if len(pending) == 1 else f"{len(pending)} jobs")
                    + " not run today")
    if behind:
        bits.append(f"map {behind} session{'s' if behind != 1 else ''} behind -- rebuild")
    if (book_behind or 0) >= 2:
        bits.append(f"book {book_behind} sessions old -- rebuild")

    doc = {
        "state": st, "why": why, "line": " · ".join(bits),
        "ok": st == "up" and not missed and not pending and not behind and (book_behind or 0) < 2,
        "updated": sc.get("updated"), "age": age, "pid": pid, "pid_alive": alive,
        "missed": missed[:12], "missed_n": len(missed),
        "pending": pending, "pending_n": len(pending),
        "report_date": rd.isoformat() if rd else None, "behind": behind,
        "book": book.isoformat() if book else None, "book_behind": book_behind,
        "last_pull": newest_pull, "last_pull_at": newest_at.isoformat(timespec="seconds") if newest_at else None,
        "last_pull_age": pull_age,
        "services": services,
    }
    _cache[key] = (time.time(), doc)
    if len(_cache) > 8:                                       # report_date changes once a session
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:4]:
            _cache.pop(k, None)
    return doc


if __name__ == "__main__":
    d = state(fresh=True)
    print(d["state"].upper(), "·", d["line"])
    print("  why        ", d["why"])
    print("  updated    ", d["updated"], f"({ago(d['age'])} ago)" if d["age"] is not None else "")
    print("  pid        ", d["pid"], "alive" if d["pid_alive"] else "GONE" if d["pid_alive"] is False else "unknown")
    print("  map        ", d["report_date"], f"({d['behind']} sessions behind)" if d["behind"] else "(current)")
    print("  book       ", d["book"], f"({d['book_behind']} weekdays old)" if d["book_behind"] is not None else "")
    print("  last pull  ", d["last_pull"], d["last_pull_at"], f"({ago(d['last_pull_age'])} ago)" if d["last_pull_age"] is not None else "")
    if d["missed"]:
        print(f"  MISSED IN THE HOLE ({d['missed_n']}):")
        for m in d["missed"]:
            print(f"    {m['when']}  {m['name']}")
    if d["pending"]:
        print(f"  NOT RUN TODAY ({d['pending_n']}):")
        for m in d["pending"]:
            print(f"    {m['at']}  {m['name']}")
    for s in d["services"]:
        print(f"  svc {s['name']:14s} {ago(s['age'])} ago")
