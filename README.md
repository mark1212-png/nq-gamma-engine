# TeddyAI — NQ dealer-gamma engine

An end-to-end system that turns the Nasdaq-100 futures (NQ) options chain into a
nightly trading plan: where dealer hedging is likely to damp or amplify price,
which levels matter, and the handful of trade cards worth watching in the next
session. It runs unattended on a scheduler, publishes to a local command center
and Discord, and updates a live chart as the tape prints.

Built and run daily as a working tool, not a demo.

**Python · NumPy / pandas · Flask · vanilla JS · Databento market data · pytest (~660 tests)**

---

## What it does

```
Databento settlement chain ─► trinity_db.py ─► engine.py ─► build.py ─► plan (HTML, PNG, Discord)
        (NQ options OI)        gamma by strike   market state    render       │
                                                   + cards                     ▼
live trades & bars (live_feed.py) ───────────────► levels_live.py ─► teddychart (live chart)
```

1. **Pull.** `databento_pull.py` fetches the CME settlement chain for every NQ
   option expiry; `verify_pull.py` checks it before anything is built on it.
2. **Model.** `nqvol.py` solves each strike's implied vol from settlement
   prices (Black-76, business-day time); `trinity_db.py` turns that into
   per-strike dealer gamma, basis-shifting other contracts' books onto the
   traded one. `engine.py` then derives the market state: gamma flip / HVL,
   walls, nodes (`nodescore.py`), regime (`regime.py`), topology of the profile
   (`topology.py`), and scenarios keyed to where the session opens (`scenario.py`).
3. **Plan.** `build.py` renders the sheet: the map, a ranked roster of trade
   cards with entries, structural stops and void conditions, and the prose.
   `precommentary.py` is a structural gate that must pass before any commentary
   is written; `publication.py` hashes the inputs and fails closed if the prose
   and the numbers don't belong to the same snapshot.
4. **Operate.** `scheduler.py` fires the Asia, overnight and RTH builds from
   inside the stack; `stack_status.py` detects a silently dead scheduler;
   `gameplan_bot.py` + `discord_post.py` publish.
5. **Live.** `live_feed.py` streams bars and trades; `levels_live.py` re-solves
   the levels at the last print; `teddychart/` is a canvas chart with the levels,
   flow panels and a replay simulator; `command_center.py` serves the `/cc` page.

See [`examples/`](examples/) for two real rendered plans (open the HTML in a browser).

## Engineering notes

- **Tests first for the maths.** Small, hand-checkable fixtures for the gamma,
  vol and scenario code; a regression baseline that stores flips, totals and a
  hash of each gamma map so a diff says *which* number moved.
- **Fail closed.** Publication refuses to post when the input identity, manifest
  or structure check doesn't match — a stale plan is worse than no plan.
- **Measured, not assumed.** Every rule that ranks or filters a card was put
  through a backtest with a proper control. Several popular ideas didn't survive
  (below), and they were taken out of the engine rather than left in as decoration.
- **Operational scars are encoded.** Timezone/DST clocks, contract rolls
  (quarterly expiry borrowing strikes across contracts), late-landing open
  interest, and scheduler double-fires each have a guard and a test.

## What the research found

Studies are in the repo (`backtest.py`, `backtest_opra.py`, `gapstudy.py`,
`hammerstudy.py`, `mm_*.py`); the short version:

- **Gamma is a variance signal, not a direction signal.** Afternoon range /
  expected move is ~2.1 under negative gamma vs ~1.6 under positive. No
  directional futures rule survived re-running on an offset (:15/:45) bar grid.
- **Levels are real but weak.** Only the barrier effect beats a
  distance-matched control — useful for stop placement, not entries or targets.
- **Base rates hide in geometry.** The weekend-gap "92% fill rate" matches a
  distance-matched control almost exactly.
- **Backtest bugs flatter.** Filling and taking profit on the same bar turned a
  ~0R strategy into +120R on coarse bars; on 1-minute bars it was ~0R again.

## Layout

```
teddyai/            the engine, dashboard, scheduler and live chart (run from here)
  tests/            pytest suite (+ a few browser checks in .cjs)
  teddychart/       live chart server and front end
  CARD_RULES.md     the house rules the plan's cards are written and ranked by
docs/               method, operations, Discord bot, live ladder, command list
examples/           rendered plans
```

## Running it

```bash
pip install -r requirements-test.txt
cd teddyai
python -m pytest -q
```

Building a plan needs a Databento API key in `DATABENTO_API_KEY`; no market data
is included in this repository. The launchers (`*.bat`, `*.ps1`) target the
Windows machine it runs on. [`docs/OPERATIONS.md`](docs/OPERATIONS.md) and
[`docs/COMMANDS.txt`](docs/COMMANDS.txt) cover every job and switch.

## Credits

The dealer-flow, release and rail-sensitivity models (`dealer_flow.py`,
`releases.py`, parts of `engine.py` and `precommentary.py`) were ported from
[Nicky24B](https://github.com/Nicky24B)'s engine and are used with permission.

This public repository is a curated snapshot of a private working repo; the
nightly plans, research notes and development journal stay private.
