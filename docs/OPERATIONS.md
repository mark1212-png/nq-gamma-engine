# Operations

Intraday NQ plans built from dealer gamma. The numbers come from Databento
settlement chains. The prose — the lead, "Reading the day", the four cards,
the VEX note — is written by hand through the `teddyai-narrative` skill. A data
pull updates every figure and leaves the words alone. The map is context, not a
signal; it is traded discretionarily.

## Read these first

| | |
|---|---|
| `COMMANDS.txt` | Every command, job and switch. |
| `METHOD.md` | The method and the corrections behind it. |
| `DISCORD.md` | The game plan bot. |
| `CC_BUILD_CHECKLIST.md` | The command center's checklist. |
| `../teddyai/CARD_RULES.md` | How the plan's cards are written and ranked. |
| `../teddyai/teddychart/README.md` | The live chart. |

Paths below are relative to `teddyai/`, where every command is run.

## What changed most recently

Production commentary now goes through `publication.py`: the prose is written to
`narrative.json` with a hashed input identity and a validated manifest, and
Discord fails closed without that evidence. Rewriting the `CC:` blocks in
`build.py` is the legacy fallback, not the production path. The live chart
broadcasts trade-flow measurements into the command center's live ladder
explanation panels, and the depth panel runs on the included MBP-1 feed.
Read `INTRADAY-IMPLEMENTATION.md` before editing or generating commentary;
`LIVE-LADDER.md` covers the explanation panels and their tests, and
`INTRADAY-IMPROVEMENTS-PROPOSAL.md` is the approved scope the implementation
is working through.

## The pipeline

```
databento_pull.py --fetch  →  trinity_db.py  →  gen.py  →  build.py  →  sheets/
```

`autopull.py` runs it unattended for each session job. `schedule_gameplan.ps1`
registers the jobs and the game plans in Task Scheduler. `gameplan_bot.py`
rewrites the commentary through `cli_commentary.py` and `discord_post.py`
posts it. `archive.py` keeps every build under `archive/`. `teddy.py` is the
dashboard on 127.0.0.1:5137 and `command_center.py` is the `/cc` page on it.

`engine.py`, `scenario.py`, `nodescore.py`, `topology.py`, `regime.py`,
`confidence.py`, `dealer_flow.py` and `releases.py` compute the market state
the sheet's panel and the cards read. `opra_pull.py` and `opra_compare.py`
carry the SPX / SPXW / QQQ / SPY cross-reference. `quikstrike_oi.py` reads
Friday's open interest off the QuikStrike OI PDF for the Sunday-open plan; it
is the only PDF reader left. The old PDF route (`nqgamma.py` → `trinity_pdf.py`)
was removed on 2026-09-13, Databento having been the only source since the
2026-09-05 audit.

Never automate CME or QuikStrike retrieval. Databento is a licensed
redistributor; their sites are not. The API key never leaves this machine.

## Launchers (double-click)

| | |
|---|---|
| `Start TeddyAI.bat` | `teddy.py` on :5137, `live_feed.py`, teddychart on :5138, then the command center at `/cc`. |
| `Start TeddyAI (App).bat` | The same stack in one native window. |
| `Stop TeddyAI.bat` | Closes them. |
| `Pull TeddyAI.bat` | One pull-and-build for whatever session the clock says. |
| `Replay TeddyAI.bat` | Re-run a recorded day at speed. |
| `Backtest OPRA.bat` | The pre-registered cross-asset test. |
| `Probe *.bat` | The market-maker footprint probes. |

## Setup

```
pip install --user numpy pandas flask databento
```

`DATABENTO_API_KEY` must be in the environment. Optional extras: `pdfplumber`
for `quikstrike_oi.py`; `playwright` for `print_sheet.py` and the chart
screenshot test; `Pillow` for `sheet_image.py`; `pywebview`, `pystray` and
`Pillow` for the desktop window.

Tests: `python -m pytest tests -q`. Read `tests/conftest.py` before running
with an API key in the environment.

## Records

| | |
|---|---|
| `sheets/` | Every published plan with its commentary, named for the session the plan is FOR. Tracked in git. |
| `archive/` | Every build. Local only. |
| `data/` | Chains, bars, OPRA books, definitions. Local only, ~11 GB, rebuilt by the pull scripts. |
| `notes/` | Logs, API replies, status files. Local only. |
