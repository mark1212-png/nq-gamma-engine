# teddychart — the TeddyAI live chart

NQ, streamed from Databento, drawn at the display's refresh rate, with the
gamma map from the current report, order flow and TPO on the same price axis.

    cd teddyai\teddychart
    run.bat                      →  http://127.0.0.1:5138

`run.bat` reads `DATABENTO_API_KEY` from the environment like `live_feed.py`
does. The key never leaves the machine; the browser only ever talks to
127.0.0.1. Try `run-sim.bat` first — a synthetic tape, no key, every feature.

## What is on the screen

**Top bar.** Timeframes (`1s`…`1h` time bars, `250t`…`2000t` tick bars,
`5r`…`20r` range bars — all built in the browser from the same prints, so a
switch is instant), candles or footprint, and one-click toggles for every
overlay. `⚙` opens the settings drawer: **everything is toggleable there**, and
the numeric knobs (imbalance ratio, big-lot threshold, TPO row size, heatmap
gain…) live beside them. Settings persist in the browser.

**Follow.** The `Follow` toggle on the top bar (or `L`) keeps the newest bar at
the right edge *and* the last price at the middle of the price pane, with every
visible bar kept on screen around it, so the candles never walk off the screen.
The centre re-targets once price has drifted a fifth of the half-height from it
and eases there ("Re-centre once price drifts" in the drawer; 0 re-centres on
every tick, and "Follow keeps the last price centred" off gives the plain
auto-fit of the visible bars instead). It is on when the chart opens. Panning away from the
live edge, dragging the price axis or shift-wheeling turns it off and the button
dims; dragging back to the live edge, `End`, double-click, `F` or the button
turns it on again. The command center's chart toolbar has the same button.

**The chart.**

- *Gamma map* — read from `trinity.json` and `trinity.html`, the same files the
  report is built from. Nothing is written back. Flips per book (0DTE amber,
  NEAR violet, FULL grey, dashed), expected-move rails (dotted), the reference
  price (brass), the gamma profile as a histogram on the right edge (teal
  positive, red negative, selectable book), gamma nodes as bands across the
  tape, and the call wall / put wall — the largest positive node above and
  negative node below the reference; every level's label sits in one column
  beside the price axis on the right ("Level labels on the right" in the
  drawer puts them back on the left). The four gameplan setups draw entry
  (solid), stop (dashed) and targets (dotted); when `teddy.py` is running their
  states are mirrored (in play brass, target hit teal, stopped red, missed
  violet) and dormant setups fade, exactly as in the console.
- *Footprint* — bid × ask per price row inside every bar (row size in ticks),
  cells shaded by volume, diagonal imbalances highlighted (ask vs the bid one
  row below, and vice versa), the bar's POC outlined, delta and volume in the
  footer. Below the minimum bar width it falls back to candles automatically.
- *Order flow* — CVD line and per-bar delta histogram in the lower pane;
  large-lot bubbles at the print's price sized by contracts (teal lifted the
  ask, red hit the bid), with a scrolling tape of the largest lots on the
  right; the ten-level depth ladder from `mbp-10`, resting size bars, walls
  highlighted, book imbalance in the spread row.
- *Liquidity heatmap* — resting size by price over time from the same book,
  folded to one column per second, log-scaled so a 400-lot wall reads without
  washing out the 40-lot rows. Drawn under everything else.
- *TPO* — RTH (06:30–13:00 PT) and ETH (13:00 → next 06:30, across the halt)
  as separate profiles, 30-minute letters from the session start, POC, 70%
  value area, initial-balance bracket, single prints in red. Prior sessions
  come from the historical API (1-minute bars); today's from the tape.
- *Power trades* — the Bookmap/Quantower idea: a zone where at least N
  contracts execute inside a short window (default 200 in 5 s) within a narrow
  price range (default 8 ticks). Drawn as a diamond (or hexagon / box) at the
  burst's price, sized by volume, with a dotted bracket over the price range,
  labelled "480 in 2.7s" and "B 310 · S 170 · +29%" — buyers and sellers
  counted separately, not netted. Colour blends from brass (two-sided,
  absorption) toward teal or red as the delta dominance grows; one-sided
  bursts (|delta| ≥ 60%) get a halo. Each burst is its own event unless
  "merge overlapping bursts" is on. Optional sound. Volume, seconds, zone
  height, delta threshold, shape, size and labels are all in the drawer.
- *Session VWAP* (resets at 06:30 and 13:00) and a *volume profile* of the
  visible range with bid/ask split, POC and value area, on the left edge.

**Status line.** Feed state, trading day, prints received, prints per second,
socket latency, frames per second and the cost of the last frame in ms.

## Keyboard

`←` `→` pan · `+` `−` zoom · `L` follow · `End` live · `Home` start · `F` fit today ·
`1`…`9` timeframes · `C` candles · `P` footprint · `H` heat · `T` TPO ·
`G` gamma · `S` setups · `B` bubbles · `X` power · `W` VWAP · `V` CVD · `D` depth ·
`,` settings · `?` help. Wheel zooms time; shift-wheel or wheel over the price
axis zooms price; drag the price axis to stretch it; double-click resets.

## How it stays smooth

The server sends prints, not bars — each one with its aggressor side — and
replays the trading day from the 17:00 Chicago open on connect (Databento's
intraday replay), so a chart opened at 09:40 has the whole session's
footprint, delta and TPO structure. The browser folds the tape into every
view incrementally in typed arrays: a print costs O(1) per builder.

The renderer stacks eight transparent canvases. A print repaints only the
`live` layer (the forming bar, the last-price line and tag) and the crosshair;
a bar close repaints the series; only a viewport change repaints everything.
Invalidations coalesce into one `requestAnimationFrame`. Measured on the
simulator: 60 fps at ~0.5 ms per frame with a few hundred prints a second.

The book arrives as at most twenty snapshots a second; the DOM ladder updates
at most once per frame; the heatmap is an offscreen image that only grows by
the newest columns until you pan.

## Replaying a real day (the pre-Monday test)

    python server.py --replay 2026-09-04                       # today's RTH at real speed, from 06:20 PT
    python server.py --replay 2026-09-04 --speed 10            # ten times faster
    python server.py --replay 2026-09-04 --from 09:30 --to 11:00 --speed 3
    python server.py --replay 2026-09-04 --replay-book mbp-1   # with the top of book (DOM + heatmap)

The day's prints are pulled once from the historical API (the log prints
Databento's cost estimate first; a day of NQ trades is tens of MB) and cached
under `cache/trades-<day>.npz`, so every replay after the first is free and
instant, with or without a key. Everything before `--from` loads as history —
full TPO, footprint, CVD from the open — and from there the prints are released
as a virtual clock passes them, at `--speed` × real time. The status line reads
`REPLAY <day> ×<speed>` and the clock at the right shows the replayed time.
The chart cannot tell it from a live session, which is the point. A speed
control appears on the top bar during a replay (⏸ 1× 2× 5× 10× 30× 100×);
`Space` pauses and resumes, `[` and `]` step the speed. Changing speed never
jumps the clock — it re-anchors from the current replayed moment.

`--replay-book mbp-10` replays the full depth too, folded to ten snapshots a
second; historical L2 is large (the log prints the size and the estimate
before pulling) and may be billed on the Standard plan, so `mbp-1` is the
cheap way to see the ladder move. Prior sessions' 1-minute bars are pulled
for the days before the replayed day, as in live mode.

## Options

    python server.py                 # live
    python server.py --sim           # synthetic tape, no key
    python server.py --probe         # entitlements and replay check, then exit
    python server.py --no-book       # skip mbp-10 (no DOM / heatmap)
    python server.py --book-schema mbp-1   # top of book only, far fewer messages
    python server.py --port 5140
    python server.py --replay YYYY-MM-DD [--from HH:MM] [--to HH:MM] [--speed N] [--replay-book mbp-1|mbp-10]

`--probe` is worth running once: it lists which schemas this key streams and
whether the gateway accepts a replay from the day's open.

## Embedding in the console

One line in `teddy.py`'s console, wherever the chart band goes:

    <iframe src="http://127.0.0.1:5138/" style="width:100%;height:720px;border:0;border-radius:6px"></iframe>

Both servers must be running. Run `python check_ui.py` after the edit, as
always. The chart also stands alone in a browser tab, which is how it is
meant to be used while trading.

## Files

    server.py          the service: Databento live + replay, book, history, levels, WebSocket
    static/index.html  layout and the TeddyAI palette
    static/wire.js     binary frame decoder (twin of the formats in server.py)
    static/store.js    tape, bar builders (time/tick/range), footprint, CVD, VWAP, TPO, book history
    static/chart.js    the layered renderer
    static/panels.js   depth ladder, tape, legend, status
    static/settings.js every setting, the drawer, persistence
    static/main.js     socket, wiring, keyboard
    test/shot.py       headless check (needs playwright): screenshots + console + frame cost
    cache/             prior sessions' 1-minute bars, one file per trading day

## Things to know

- **Prices from Databento are fixed-point** (1 unit = 1e-9). The scale is
  applied in exactly one place, `px()` in `server.py`.
- **Side is the aggressor.** Databento `B` = buy aggressor = lifted the ask →
  the *ask* column of the footprint. `A` = sell aggressor = hit the bid → the
  *bid* column. Code 1 and 2 on the wire.
- **The replay overlaps the live fan-out by up to one flush.** Every trades
  frame carries the day-sequence number of its first print; the browser drops
  what it already holds. Nothing is lost, nothing is doubled.
- **The trading day is named for the day it settles**, so an evening session
  files under tomorrow's date, the same way the report's sheets are named for
  the session they are FOR.
- **Time bars snap to the Pacific clock**, so a 30m bar starts at 06:30.
  Tick and range bars start at the print that opened them.
- If the machine falls behind on `mbp-10` (the book is thousands of messages a
  second at the open), `--book-schema mbp-1` keeps the ladder's top level and
  the tape while dropping the depth.
- The gamma map is re-read whenever `trinity.json` or `trinity.html` changes
  on disk, so a Generate in the console shows on the chart within seconds.
