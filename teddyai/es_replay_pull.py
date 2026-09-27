"""
es_replay_pull.py -- ES 1-minute closes for one trading day, so a REPLAY moves the SPX and SPY
columns of the Gamma Ladders panel. (Live needs nothing: cc_ticks.py carries ES.c.0 itself.)

    python es_replay_pull.py --day 2026-09-04             # estimate only -- always first, it is free
    python es_replay_pull.py --day 2026-09-04 --fetch     # writes teddychart/cache/es-2026-09-04.npz
    python es_replay_pull.py --day 2026-09-04 --res 1m    # the 1-minute version (es-1m-<day>.npz)

Window: the CME trading day, 17:00 Chicago the evening before to 16:00 Chicago on --day, from
GLBX.MDP3 ES.c.0 schema ohlcv-1s by default (one close per second with a trade, ~50-70k bars,
a few MB) or ohlcv-1m (--res 1m, ~1,380 bars). Databento serves complete UTC days only, so the
day must be fully in the past. metadata.get_billable_size / get_cost run before the fetch and
the fetch refuses above --max-mb (default 20). The key is DATABENTO_API_KEY, never printed.

The file: ts (ms, the bar's CLOSE time = ts_event + the bar length), close (points), res ('1s'
or '1m'), and the contract the continuous symbol resolved to (ESU6 ...), which the page needs
for the SPX/ES basis factor. cc_ticks.py's replay releases each close on the virtual clock as
status()['es'] with src = res, preferring es-<day>.npz (1 s) over es-1m-<day>.npz; the panel
then re-prices SPX and SPY every couple of seconds, the same cadence as NQ off its prints.
teddychart's own trades cache is untouched: this is a second file beside it.
"""
from __future__ import annotations
import os, sys, argparse, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DATASET, SYMBOL, STYPE = "GLBX.MDP3", "ES.c.0", "continuous"
SCHEMAS = {"1s": ("ohlcv-1s", 1_000), "1m": ("ohlcv-1m", 60_000)}
CT, UTC = ZoneInfo("America/Chicago"), datetime.timezone.utc
ROOT = Path(__file__).resolve().parent


def window(day: datetime.date):
    """17:00 CT the calendar day before -> 16:00 CT on `day`, as aware UTC datetimes."""
    o = datetime.datetime.combine(day - datetime.timedelta(days=1), datetime.time(17, 0), CT)
    c = datetime.datetime.combine(day, datetime.time(16, 0), CT)
    return o.astimezone(UTC), c.astimezone(UTC)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="the trading day, YYYY-MM-DD (the RTH date)")
    ap.add_argument("--fetch", action="store_true", help="pull and write; without it, estimate only")
    ap.add_argument("--res", choices=list(SCHEMAS), default="1s", help="bar length: 1s (default) or 1m")
    ap.add_argument("--max-mb", type=float, default=20.0)
    a = ap.parse_args()
    SCHEMA, bar_ms = SCHEMAS[a.res]
    day = datetime.date.fromisoformat(a.day)
    if day.weekday() >= 5:
        sys.exit(f"{day} is a weekend")
    s, e = window(day)
    try:
        import databento as db
    except ImportError:
        sys.exit("pip install --user databento")
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        sys.exit("set DATABENTO_API_KEY first")
    c = db.Historical(key)
    rng = c.metadata.get_dataset_range(DATASET)
    end_avail = rng.get("end") if isinstance(rng, dict) else getattr(rng, "end", None)
    print(f"  {DATASET} {SYMBOL} {SCHEMA}  {s:%Y-%m-%d %H:%M}Z -> {e:%Y-%m-%d %H:%M}Z   (dataset end {end_avail})")
    if end_avail and str(end_avail)[:19] < e.strftime("%Y-%m-%dT%H:%M:%S"):
        sys.exit("  PAST DATASET END -- the day is not complete on the historical service yet")
    mb = c.metadata.get_billable_size(dataset=DATASET, symbols=[SYMBOL], stype_in=STYPE, schema=SCHEMA, start=s, end=e) / 1e6
    usd = c.metadata.get_cost(dataset=DATASET, symbols=[SYMBOL], stype_in=STYPE, schema=SCHEMA, start=s, end=e)
    print(f"  estimate {mb:.3f} MB  ${usd:.4f}")
    if not a.fetch:
        print("  (estimate only; add --fetch to write the file)")
        return
    if mb > a.max_mb:
        sys.exit(f"  estimate {mb:.2f} MB exceeds --max-mb {a.max_mb:g}; not fetching blind.")
    import numpy as np
    store = c.timeseries.get_range(dataset=DATASET, symbols=[SYMBOL], stype_in=STYPE, schema=SCHEMA, start=s, end=e)
    df = store.to_df(map_symbols=True)
    if df.empty:
        sys.exit("  no bars returned")
    df = df.sort_index()
    ts_ms = (df.index.astype("int64") // 1_000_000 + bar_ms).to_numpy()      # close time
    close = df["close"].to_numpy(dtype="float64")
    # the contract the continuous symbol resolved to (ESU6), from the store's symbology; to_df's
    # `symbol` column carries the INPUT symbol for continuous requests, which the page cannot use
    contracts = []
    try:
        res = (store.symbology or {}).get("result", {})
        contracts = sorted({m.get("s") for lst in res.values() for m in lst if m.get("s")})
    except Exception:
        pass
    if not contracts and "symbol" in df:
        contracts = sorted(s for s in set(df["symbol"].astype(str)) if not s.endswith(".c.0"))
    contract = contracts[-1] if contracts else SYMBOL
    out = ROOT / "teddychart" / "cache"
    out.mkdir(parents=True, exist_ok=True)
    p = out / (f"es-{day}.npz" if a.res == "1s" else f"es-1m-{day}.npz")
    np.savez(p, ts=ts_ms.astype("int64"), close=close, contract=np.array(contract), res=np.array(a.res))
    print(f"  wrote {p.relative_to(ROOT)}: {len(close):,} {a.res} bars, {contract or '?'}, "
          f"{close[0]:,.2f} -> {close[-1]:,.2f}, first close {datetime.datetime.fromtimestamp(ts_ms[0]/1000, UTC):%H:%M}Z "
          f"last {datetime.datetime.fromtimestamp(ts_ms[-1]/1000, UTC):%H:%M}Z"
          + (f"  (roll inside the day: {contracts})" if len(contracts) > 1 else ""))


if __name__ == "__main__":
    main()
