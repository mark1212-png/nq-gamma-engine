"""
Unit tests for stack_status.py -- the staleness signal.

The case under test is the real one: the stack was stopped at 15:45 PT on
2026-09-16 and not started again until 13:23 PT on 2026-09-17. Nothing crashed.
Every surface kept drawing, so the only way to see it was to ask whether the
scheduler was still ticking -- which nothing did.

These tests pin the two halves of that:

  * WHILE DOWN, the hole is counted. Seven firing instants were lost between
    15:45 on the 16th and 13:23 on the 17th, and the state has to say "down"
    with that number on it.
  * AFTER THE RESTART, the hole closes -- every heartbeat goes green again --
    but the 06:05 premarket still has not run, the map is still the one built
    the night before last, and the book behind it is two sessions old. Those
    three must survive the restart, or the signal is worse than none: it would
    have gone quiet at exactly the moment Mark started trading the stale sheet.

No file is read: every input here is a literal status document.
"""
import datetime
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import stack_status as S


# The eight-job table scheduler.py carries, with last_run as it stood when the
# stack was stopped on 2026-09-16 at 15:45 PT: the 06:05 premarket and the 14:10
# close had run that day, nothing after them ever did.
def _jobs(last=None):
    last = last or {}
    table = [
        ("gameplan-premarket", "06:05"),
        ("close", "14:10"),
        ("settle", "16:00"),
        ("opra", "16:10"),
        ("gameplan-asia", "17:15"),
        ("prelim-poll", "18:30"),
        ("prelim", "22:15"),
        ("gameplan-tomorrow", "22:20"),
    ]
    return [{"name": n, "at": at, "last_run": last.get(n), "running": False} for n, at in table]


STOPPED = {
    "gameplan-premarket": "2026-09-16T06:05:15",
    "close": "2026-09-16T14:10:01",
    "settle": "2026-09-15T16:00:04",
    "opra": "2026-09-15T16:10:02",
    "gameplan-asia": "2026-09-15T17:15:03",
    "prelim-poll": "2026-09-15T18:30:02",
    "prelim": "2026-09-15T22:15:11",
    "gameplan-tomorrow": "2026-09-15T22:22:39",
}

DIED = datetime.datetime(2026, 9, 16, 15, 45)
DURING = datetime.datetime(2026, 9, 17, 13, 0)      # mid-outage, 21 hours in
AFTER = datetime.datetime(2026, 9, 17, 13, 40)      # 17 minutes after the restart


# --------------------------------------------------------------- the hole ---

def test_the_22_hour_hole_is_seven_jobs():
    """settle, opra, asia, prelim-poll, prelim, tomorrow on the 16th; premarket on the 17th."""
    missed = S.missed_jobs(_jobs(STOPPED), DURING, DIED)
    assert [m["name"] for m in missed] == [
        "gameplan-premarket",       # 09-17 06:05
        "gameplan-tomorrow",        # 09-16 22:20
        "prelim",                   # 09-16 22:15
        "prelim-poll",              # 09-16 18:30
        "gameplan-asia",            # 09-16 17:15
        "opra",                     # 09-16 16:10
        "settle",                   # 09-16 16:00
    ]
    assert missed[0]["when"].startswith("2026-09-17")
    assert missed[-1]["when"].startswith("2026-09-16")


def test_the_hole_is_bounded_by_the_last_tick_not_by_last_run():
    """
    A job that never ran under this scheduler is not seven days of failures.

    The first cut of this walked back from each job's last_run and reported 23
    misses on a healthy stack -- archaeology from before scheduler.py existed.
    Anything at or before the last proven tick belongs to some earlier story.
    """
    missed = S.missed_jobs(_jobs(last={}), DURING, DIED)
    assert len(missed) == 7
    assert all(m["when"] > DIED.isoformat(timespec="minutes") for m in missed)


def test_a_live_stack_has_no_hole():
    now = datetime.datetime(2026, 9, 17, 13, 0)
    assert S.missed_jobs(_jobs(STOPPED), now, now - datetime.timedelta(seconds=20)) == []


def test_the_grace_window_is_not_a_miss():
    """The scheduler itself still runs a job up to 25 minutes late; do not call it lost."""
    died = datetime.datetime(2026, 9, 17, 5, 0)
    now = datetime.datetime(2026, 9, 17, 6, 20)             # 15 min past the 06:05
    assert [m["name"] for m in S.missed_jobs(_jobs(STOPPED), now, died)] == []
    later = datetime.datetime(2026, 9, 17, 6, 40)           # 35 min past it
    assert [m["name"] for m in S.missed_jobs(_jobs(STOPPED), later, died)] == ["gameplan-premarket"]


def test_the_weekend_fires_nothing():
    fri = datetime.datetime(2026, 9, 18, 15, 45)
    mon = datetime.datetime(2026, 9, 21, 5, 0)              # before Monday's first job
    assert [m["name"] for m in S.missed_jobs(_jobs(STOPPED), mon, fri)] == [
        "gameplan-tomorrow", "prelim", "prelim-poll", "gameplan-asia", "opra", "settle",
    ]                                                       # Friday evening only -- no Saturday, no Sunday


# ----------------------------------------------- what survives the restart ---

def test_premarket_is_still_missing_after_the_restart():
    """
    13:23 PT: the stack comes back, the hole closes, every light goes green.
    The 06:05 premarket still never ran, and will not until tomorrow.
    """
    assert S.missed_jobs(_jobs(STOPPED), AFTER, AFTER - datetime.timedelta(seconds=20)) == []
    pending = S.not_run_today(_jobs(STOPPED), AFTER)
    assert [p["name"] for p in pending] == ["gameplan-premarket"]


def test_not_run_today_ignores_what_is_not_due_yet():
    pending = S.not_run_today(_jobs(STOPPED), datetime.datetime(2026, 9, 17, 6, 20))
    assert pending == []                                    # inside the 06:05 grace window
    pending = S.not_run_today(_jobs(STOPPED), datetime.datetime(2026, 9, 17, 23, 0))
    assert [p["name"] for p in pending] == [
        "gameplan-premarket", "close", "settle", "opra",
        "gameplan-asia", "prelim-poll", "prelim", "gameplan-tomorrow",
    ]                                                       # in schedule order


def test_a_running_job_is_not_missing():
    jobs = _jobs(STOPPED)
    jobs[0]["running"] = True
    assert S.not_run_today(jobs, AFTER) == []


def test_not_run_today_is_quiet_at_the_weekend():
    assert S.not_run_today(_jobs(STOPPED), datetime.datetime(2026, 9, 19, 23, 0)) == []


# --------------------------------------------------- the map and the book ---

def test_the_trading_day_steps_back_over_the_weekend():
    assert S.trading_day(datetime.datetime(2026, 9, 17, 9, 0)) == datetime.date(2026, 9, 17)
    assert S.trading_day(datetime.datetime(2026, 9, 19, 9, 0)) == datetime.date(2026, 9, 18)   # Sat -> Fri
    assert S.trading_day(datetime.datetime(2026, 9, 20, 9, 0)) == datetime.date(2026, 9, 18)   # Sun -> Fri


def test_weekdays_between_skips_the_weekend():
    assert S.weekdays_between(datetime.date(2026, 9, 16), datetime.date(2026, 9, 17)) == 1
    assert S.weekdays_between(datetime.date(2026, 9, 15), datetime.date(2026, 9, 17)) == 2
    assert S.weekdays_between(datetime.date(2026, 9, 18), datetime.date(2026, 9, 21)) == 1   # Fri -> Mon
    assert S.weekdays_between(datetime.date(2026, 9, 18), datetime.date(2026, 9, 17)) == -1
    assert S.weekdays_between(None, datetime.date(2026, 9, 17)) is None


# ------------------------------------------------------------ the whole doc ---

def _state(tmp_path, updated, jobs, report_date, settle, now, pid=None):
    """A project folder with just the files stack_status reads."""
    import json
    (tmp_path / "notes").mkdir(exist_ok=True)
    (tmp_path / "notes" / "scheduler-status.json").write_text(
        json.dumps({"updated": updated, "pid": pid, "jobs": jobs}), encoding="utf-8")
    (tmp_path / "trinity.json").write_text(
        json.dumps({"report_date": report_date, "settle": settle}), encoding="utf-8")
    return S.state(root=tmp_path, now=now, fresh=True)


def test_mid_outage_the_state_says_down_with_the_count(tmp_path):
    d = _state(tmp_path, DIED.isoformat(timespec="seconds"), _jobs(STOPPED),
               "2026-09-16", "2026-09-15", DURING)
    assert d["state"] == "down"
    assert d["missed_n"] == 7
    assert d["behind"] == 1                                 # a map for a session already traded
    assert d["book_behind"] == 2                            # the 09-15 book on the 17th
    assert not d["ok"]
    assert d["line"] == ("stack down 21h · 7 jobs missed"
                         " · map 1 session behind -- rebuild"
                         " · book 2 sessions old -- rebuild")


def test_after_the_restart_the_stale_map_still_speaks(tmp_path):
    """
    The heartbeat is green and the hole is closed, but the sheet on the screen
    is still the one built 22 hours ago on a two-session-old book. If this line
    came back empty here, the signal would go quiet at the worst moment.
    """
    d = _state(tmp_path, AFTER.isoformat(timespec="seconds"), _jobs(STOPPED),
               "2026-09-16", "2026-09-15", AFTER, pid=-1)
    assert d["state"] == "up"
    assert d["missed_n"] == 0
    assert not d["ok"]
    assert d["line"] == ("scheduler ok · gameplan-premarket not run today"
                         " · map 1 session behind -- rebuild"
                         " · book 2 sessions old -- rebuild")


def test_a_healthy_stack_says_so_and_nothing_more(tmp_path):
    now = datetime.datetime(2026, 9, 17, 9, 0)
    jobs = _jobs({"gameplan-premarket": "2026-09-17T06:05:12"})
    d = _state(tmp_path, now.isoformat(timespec="seconds"), jobs, "2026-09-17", "2026-09-16", now, pid=-1)
    assert d["state"] == "up" and d["ok"]
    assert d["line"] == "scheduler ok"                      # the map is today's, the book is last night's


def test_tomorrows_map_is_not_behind(tmp_path):
    """After the 17:15 asia build the map is dated tomorrow. That is correct, not stale."""
    now = datetime.datetime(2026, 9, 17, 18, 0)
    jobs = _jobs({n: f"2026-09-17T{at}:05" for n, at in
                  [("gameplan-premarket", "06:05"), ("close", "14:10"), ("settle", "16:00"),
                   ("opra", "16:10"), ("gameplan-asia", "17:15")]})
    d = _state(tmp_path, now.isoformat(timespec="seconds"), jobs, "2026-09-18", "2026-09-16", now, pid=-1)
    assert d["behind"] == 0
    assert d["line"] == "scheduler ok"


def test_no_status_file_is_unknown_not_healthy(tmp_path):
    import json
    (tmp_path / "trinity.json").write_text(json.dumps({"report_date": "2026-09-17"}), encoding="utf-8")
    d = S.state(root=tmp_path, now=DURING, fresh=True)
    assert d["state"] == "unknown" and not d["ok"]
    assert d["line"].startswith("scheduler unknown")


def test_pid_alive_never_kills_anything():
    """
    The guard that matters on Windows: os.kill(pid, 0) there calls
    TerminateProcess, so the liveness check would stop the scheduler it asked
    about. Our own pid must read alive, and a nonsense pid must not raise.
    """
    import os
    assert S.pid_alive(os.getpid()) is True
    assert S.pid_alive(None) is None
    assert S.pid_alive("not a pid") is None
    assert S.pid_alive(-1) is None
    assert S.pid_alive(0) is None
