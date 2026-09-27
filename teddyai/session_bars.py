"""Cash-session bar selection using the project's existing session calendar.

Input timestamps without offsets are explicitly interpreted as UTC, as in the
historical bar cache. Bar starts at the close are excluded.
"""
import datetime as dt
import nqvol


def rth_bars(rows, day):
    d=dt.date.fromisoformat(str(day))
    if not nqvol.is_session(d): return []
    start,end=nqvol.rth_open_utc(d),nqvol.close_utc(d)
    result=[]
    for row in rows:
        t=dt.datetime.fromisoformat(str(row['ts']).replace('Z','+00:00'))
        if t.tzinfo is None: t=t.replace(tzinfo=dt.timezone.utc)
        if start <= t < end: result.append(row)
    return result
