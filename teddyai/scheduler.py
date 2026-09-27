# -*- coding: utf-8 -*-
"""
scheduler.py -- the TeddyAI schedule, inside the stack instead of Windows Task Scheduler.

    python scheduler.py               the service: fire each job at its time, weekdays, PT
    python scheduler.py --list        the table with the next fire time of every job
    python scheduler.py --run NAME    fire one job now (the "Run now" button)
    python scheduler.py --once        one tick, then exit

Mark, 2026-09-15: "can we just put everything the scheduling the tracking and everything
into start teddy.bat so that there are no terminal or powershell windows coming in". Until
tonight the jobs were Task Scheduler entries registered by schedule_gameplan.ps1 -- and two
generations of them, the 09-03 root-level set and the 09-10 folder, fired every data job
twice at the same second. Now the launcher starts this one process, hidden, and it runs the
table below. Nothing is registered in Windows; when the stack is down nothing runs, which
is also what "runs only while logged on" meant before.

THE TABLE is the one schedule_gameplan.ps1 carried, with the poll:

  06:05  gameplan-premarket   the RTH plan on the final book, CLI commentary, posted
  14:10  close                bars only; settlement still disseminating
  16:00  settle               the official settlement bulletin
  16:10  opra                 the equity books with open interest
  17:15  gameplan-asia        the overnight plan on the previous session's book
  18:30  prelim-poll          waits for tonight's book (18:34-19:16 PT measured), pulls it,
                              rewrites the overnight on it; three-hour limit
  22:15  prelim               the fallback pull if the poll gave up
  22:20  gameplan-tomorrow    tomorrow's RTH plan on tonight's book, CLI commentary, posted

The commentary is `--commentary cli`: the Claude Code CLI on Mark's subscription, never the
API (cli_commentary.py strips the key from that process). Each job runs hidden with its
output in notes/services/jobs/<name>-<date>.log, one instance at a time, killed at its time
limit. A job whose time passed while the stack was down is run on start if it is less than
GRACE_MIN late (the 06:05 premarket at 06:20 still matters; at 09:00 it does not). State
(what ran today, with what result) is notes/scheduler-state.json; the status the
dashboard's Operations list reads is notes/scheduler-status.json, refreshed every tick.
"""
import sys, os, json, time, datetime, pathlib, argparse, subprocess

ROOT = pathlib.Path(__file__).resolve().parent
os.chdir(ROOT)
NOTES = ROOT / "notes"
JOBLOGS = NOTES / "services" / "jobs"
STATE = NOTES / "scheduler-state.json"
STATUS = NOTES / "scheduler-status.json"
LOG = NOTES / "scheduler.log"
PY = sys.executable
TICK_S = 20
GRACE_MIN = 25
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

JOBS = [
    dict(name="gameplan-premarket", at="06:05", cmd=["gameplan_bot.py", "--job", "premarket", "--commentary", "cli"], limit_h=1, what="the RTH plan on the final book; CLI commentary; posted"),
    dict(name="close",              at="14:10", cmd=["autopull.py", "--job", "close"],                                  limit_h=1, what="bars only; settlement still disseminating"),
    dict(name="settle",             at="16:00", cmd=["autopull.py", "--job", "settle"],                                 limit_h=1, what="the official settlement bulletin"),
    dict(name="opra",               at="16:10", cmd=["autopull.py", "--job", "opra"],                                   limit_h=1, what="the equity books with open interest"),
    # --no-post from 2026-09-17, Mark's call: he wants to see the machine's plan and a
    # hand-written one side by side before either goes to Discord. The job still runs the whole
    # pipeline and publishes the sheet -- only the post is withheld. Post by hand with
    # `python gameplan_bot.py --job asia --post-only` once a version is picked, or drop
    # "--no-post" here and restart the scheduler to go back to posting automatically.
    dict(name="gameplan-asia",      at="17:15", cmd=["gameplan_bot.py", "--job", "asia", "--commentary", "cli", "--no-post"], limit_h=1, what="the overnight plan on the previous session's book; NOT posted -- compare, then post by hand"),
    # --no-gameplan from 2026-09-17: this job rewrites THE OVERNIGHT -- the same sheet the
    # 17:15 job is now withholding -- and by default it writes CLI commentary and posts it. It
    # still waits for tonight's book, pulls it and rebuilds the numbers; only the auto-prose and
    # the post are withheld, so nothing ships before the two versions are compared.
    dict(name="prelim-poll",        at="18:30", cmd=["autopull.py", "--job", "prelim-poll", "--no-gameplan"],            limit_h=3, what="waits for tonight's book, pulls it, rebuilds the overnight -- no prose, NOT posted"),
    dict(name="prelim",             at="22:15", cmd=["autopull.py", "--job", "prelim"],                                 limit_h=1, what="the fallback pull if the poll gave up"),
    # --no-post from 2026-09-17, Mark's call -- the last automatic post in the table. Every
    # scheduled job now builds and publishes but ships nothing: 17:15 asia, 18:30 prelim-poll
    # (--no-gameplan) and this one. Post by hand once a version is picked:
    #   python gameplan_bot.py --job tomorrow --post-only
    # To go back to posting automatically, drop "--no-post" here and restart the scheduler.
    # HELD 2026-09-17 at Mark's request ("dont let the 22:20 job overwrite it"): the 22:02
    # hand-written RTH sheet is the published one and this job would re-render the stable
    # sheet name and narrative.json over it. Nothing else is affected -- 22:15 prelim still
    # pulls the book. TO RESTORE: uncomment the dict below, delete these five lines, and
    # restart the scheduler. To run it once by hand instead:
    #   python gameplan_bot.py --job tomorrow --commentary cli --no-post
    # dict(name="gameplan-tomorrow",  at="22:20", cmd=["gameplan_bot.py", "--job", "tomorrow", "--commentary", "cli", "--no-post"], limit_h=1, what="tomorrow's RTH plan on tonight's book; NOT posted -- compare, then post by hand"),
]
WEEKDAYS = {0, 1, 2, 3, 4}


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        NOTES.mkdir(exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(st):
    STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")


def next_fire(job, now):
    """The next datetime this job fires, weekdays only."""
    hh, mm = map(int, job["at"].split(":"))
    for d in range(0, 8):
        day = now.date() + datetime.timedelta(days=d)
        if day.weekday() not in WEEKDAYS:
            continue
        t = datetime.datetime.combine(day, datetime.time(hh, mm))
        if t > now:
            return t
    return None


def write_status(running, st, now):
    doc = {"updated": now.isoformat(timespec="seconds"), "pid": os.getpid(), "jobs": []}
    for j in JOBS:
        nf = next_fire(j, now)
        last = st.get(j["name"]) or {}
        r = running.get(j["name"])
        doc["jobs"].append({
            "name": j["name"], "at": j["at"], "what": j["what"], "cmd": " ".join(j["cmd"]),
            "next": nf.isoformat(timespec="minutes") if nf else None,
            "running": bool(r), "started": (r or {}).get("started"),
            "last_run": last.get("started"), "last_rc": last.get("rc"), "last_seconds": last.get("seconds"),
            "last_log": last.get("log"),
        })
    # The tmp name carries the pid and the replace retries: a fixed "scheduler-status.tmp"
    # races every reader of the status file (the command center's Operations panel polls it),
    # and on Windows os.replace then fails outright -- 70 minutes of "tick error: PermissionError"
    # on 2026-09-16, one per tick. It never stopped the loop, because write_status is the last
    # call in the tick's try and the jobs are dispatched above it, but it blinded the one panel
    # that would have shown the stack was down. (2026-09-17)
    tmp = STATUS.with_name(f"scheduler-status.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    for _ in range(5):
        try:
            os.replace(tmp, STATUS)
            return
        except PermissionError:
            time.sleep(0.08)
    try:                                                   # a reader held it for 400 ms: the
        tmp.unlink()                                       # status is one tick stale, which is
    except OSError:                                        # cosmetic. Drop it, do not raise.
        pass


def start_job(job, now, running, why):
    JOBLOGS.mkdir(parents=True, exist_ok=True)
    logf = JOBLOGS / f"{job['name']}-{now:%Y-%m-%d-%H%M}.log"
    fh = open(logf, "w", encoding="utf-8")
    p = subprocess.Popen([PY, "-X", "utf8"] + job["cmd"], cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                         creationflags=CREATE_NO_WINDOW)
    running[job["name"]] = {"proc": p, "fh": fh, "started": now.isoformat(timespec="seconds"), "log": str(logf),
                            "deadline": time.time() + job["limit_h"] * 3600, "t0": time.time()}
    log(f"{job['name']}: started ({why}) pid {p.pid} -> {logf.name}")


def reap(running, st):
    done = []
    for name, r in running.items():
        rc = r["proc"].poll()
        if rc is None and time.time() > r["deadline"]:
            log(f"{name}: over its time limit -- killing pid {r['proc'].pid}")
            try:
                r["proc"].kill()
            except Exception:
                pass
            rc = -9
        if rc is not None:
            r["fh"].close()
            st[name] = {"started": r["started"], "rc": rc, "seconds": round(time.time() - r["t0"]), "log": r["log"],
                        "date": r["started"][:10]}
            log(f"{name}: finished rc={rc} in {st[name]['seconds']}s")
            done.append(name)
    for name in done:
        del running[name]
    if done:
        save_state(st)


def due(job, now, st):
    """Fire at its minute, or within GRACE_MIN after it if it has not run today."""
    if now.weekday() not in WEEKDAYS:
        return None
    hh, mm = map(int, job["at"].split(":"))
    t = datetime.datetime.combine(now.date(), datetime.time(hh, mm))
    last = st.get(job["name"]) or {}
    if last.get("date") == now.date().isoformat() and last.get("started", "")[11:16] >= job["at"]:
        # already ran at or after its time today -- unless it never finished: a job the previous
        # scheduler started and a restart of the stack killed with it (2026-09-15 22:21 PT, the
        # gameplan-tomorrow run). rc None with no live process is "interrupted", and inside the
        # grace window it runs again.
        if last.get("rc") is not None or last.get("interrupted"):
            return None
        late_i = (now - t).total_seconds() / 60.0
        if late_i <= GRACE_MIN:
            return f"interrupted by a restart at {last.get('started', '')[11:16]}; running again"
        return None
    late = (now - t).total_seconds() / 60.0
    if 0 <= late < 1:
        return "on time"
    if 1 <= late <= GRACE_MIN:
        return f"{late:.0f} min late: the stack was not up at {job['at']}"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--run", default=None, help="fire one job now")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    now = datetime.datetime.now()
    st = load_state()
    if a.list:
        for j in JOBS:
            nf = next_fire(j, now); last = st.get(j["name"]) or {}
            print(f"  {j['at']}  {j['name']:20s} next {nf:%a %H:%M}   last {last.get('started', '-')[:16]} rc={last.get('rc', '-')}   {j['what']}")
        return 0
    running = {}
    if a.run:
        job = next((j for j in JOBS if j["name"] == a.run), None)
        if not job:
            print("no such job:", a.run); return 1
        start_job(job, now, running, "run now")
        while running:
            time.sleep(2); reap(running, st)
        print("done:", st.get(a.run))
        return 0
    log(f"scheduler up, pid {os.getpid()}: {len(JOBS)} jobs, weekdays, PT; grace {GRACE_MIN} min")
    while True:
        now = datetime.datetime.now()
        try:
            reap(running, st)
            for j in JOBS:
                if j["name"] in running:
                    continue
                why = due(j, now, st)
                if why:
                    # mark the start in state at once so a restart inside the minute does not fire it twice
                    st[j["name"]] = {"started": now.isoformat(timespec="seconds"), "rc": None, "date": now.date().isoformat()}
                    save_state(st)
                    start_job(j, now, running, why)
            write_status(running, st, now)
        except Exception as ex:
            log(f"tick error: {type(ex).__name__}: {ex}")
        if a.once:
            return 0
        time.sleep(TICK_S)


if __name__ == "__main__":
    sys.exit(main())
