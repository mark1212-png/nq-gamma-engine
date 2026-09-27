# Live ladder explanations and price confirmations

Implemented for the command center, September 2026. This document describes the
shipped feature. `INTRADAY-IMPROVEMENTS-PROPOSAL.md` describes additional work that
has NOT been applied and still needs Mark's review.

## Where it appears

Open the existing command center and reload it after the files have been updated.
The **Live Ladder Explanation** panel sits immediately below Gamma Ladders in the
Terminal view and below Strike Exposure in the Ladder view, including its detached
window. There is no new service, model call, or options subscription. Flask's
existing `/static` route serves `static/live_ladder_explain.js`.

The panel follows the selected NQ book and expiry chips. Equity-derived ladders
are not interpreted in this first version. The selected book and dates are named
in the panel. Original report prose, entries, targets, and stops are not changed.

## What is calculated

`command_center.html` provides `ladderExplanationSnapshot()`. It reuses the SAME
`liveGamma`, `liveFlip`, and `liveState` functions used by the displayed ladder.
There is no second option-pricing implementation in the explanation module.

* Baseline: `chain_live.json` at its `spot` and `asof`, with the selected books.
* Current: the same saved OI and smile, repriced at the trade price and market clock.
* Fixed comparison window: baseline spot plus/minus `R.session.em` (the FULL-session
  expected move, not the narrower Asia/intraday rail width). Holding the window
  fixed avoids treating a strike's entry into a moving window as exposure growth.
* Rows: signed net gamma in that window, nearest flip, largest absolute strike
  change in the window, and the strongest current strike if different.
* Every comparable row shows baseline, current, signed change, and its materiality
  threshold. Gamma percentages use delta / ABS(baseline); these are not growth in
  OI. They are omitted below 10 contracts of absolute baseline gamma. Flip changes
  are expressed in points only.
* The strongest-strike share uses SUM(ABS(NET gamma per displayed strike)) as its
  denominator. It is not gross call-plus-put exposure or dealer market share.

The narrative names sizable polarity changes, price's position relative to the
flip, flip movement, and node magnitude changes. It never attributes them to new
buying/selling, actual dealer identities, or a directional forecast. The saved
smile is evaluated at new moneyness; no fresh IV observation is implied.

## What “material” means

These are PROVISIONAL display rules, not statistically calibrated significance:

| Measurement | Absolute change required |
|---|---|
| Window/strike gamma | max(10 contracts per 1% move, 20% of absolute comparison value) |
| Nearest flip | max(5 NQ points, 2% of full-session EM) |
| Clear gamma polarity change | Material above, with both endpoint magnitudes at least 10 |

Rows compare with the saved snapshot and show material SIZE immediately. A log
entry requires material change in the same direction for 30 seconds of observed
market time. After an entry, its next threshold is measured from that entry's
value; the row still compares with the saved snapshot. The log has 12 entries.
It starts when the page observes the condition, not retrospectively at the pull.
Both row and log disclose their comparison basis. No p-value or probability is
generated. Changing the model's selected nearest crossing can move the flip even
without one specific zero crossing traveling; the method text states this.

## Price confirmations (explicit follow-up authorization)

The **Price confirmations at saved levels** section uses one-minute bars built
incrementally from `TAPE.ts` / `TAPE.px`; changing the chart timeframe does not
change these bars. It considers up to four nearby saved strikes with absolute
baseline gamma >=10, plus the saved flip when distinct.

Levels are frozen at the snapshot. The CURRENT moving flip is deliberately NOT
applied backwards to earlier bars. Bars before the snapshot, the first partial
observed minute, forming bars, and triples spanning a missing minute cannot
confirm. The newest complete minute must also be available. After a feed pause,
only new complete bars after that pause can confirm.

Buffer = max(1 point, 0.25% of full-session EM), rounded UP to an NQ quarter-point.
This buffer is provisional too. For consecutive completed bars P, A, B:

* Reclaim: P closes below the level-buffer; A and B close above level+buffer.
* Loss: P closes above level+buffer; A and B close below level-buffer.
* Failed reclaim from below: P closes below; A trades above level+buffer but
  closes below level-buffer; B also closes below level-buffer.
* Retest held from above: the symmetric price behavior from above.
* Two closes above/below: P is within the buffer; A and B close beyond the same
  outer edge. Two closes already on an established side do not repeatedly fire.

The six newest qualifying events from approximately the last 30 minutes are shown
with confirmation time, level, two actual closes, buffer, and where price is NOW.
This is a reconstruction of observed price behavior, NOT a fill, signal backtest,
or automatic entry recommendation. The rules do not identify two distinct failed
attempts just because two bars close below a level.

## Feed, replay, and Asia

Runs across sessions, including Asia; no options trade-flow input is required.
There is no claim of fresh intraday OI or live options quotes. Asia and RTH use the
same provisional rules today; separate calibration belongs in the proposal.

The panel pauses if disconnected, catching up, last trade >90 seconds old,
timestamps are inconsistent, chain/report do not match, no usable selected book
exists, all selected expiries have expired, or model clock evaluation fails.
For real feeds it also rejects a different futures contract from `ref_symbol`,
preventing roll basis from masquerading as a price/exposure change.

Replay uses the virtual market clock and is marked REPLAY. Simulation is marked
SIMULATION; neither is called live evidence. Rewind, new saved chain contents,
book/expiry selection, mode, or session change resets the comparison log. A feed
pause clears persistence candidates and starts fresh confirmation history.

The browser log is in memory, separate per page, and is not written into the
report/archive. Reloading clears it. Chain reload follows the existing report
reload path. This feature does not add independent polling for changed chain files.

## Files and validation

* `static/live_ladder_explain.js`: pure comparisons, thresholds, event tracker,
  independent minute-bar accumulator, price-confirmation rules, rendering.
* `command_center.html`: two panels, small CSS, script include, adapter.
* `tests/test_live_ladder_explain.cjs`: offline deterministic unit tests.
* `tests/check_live_ladder_browser.cjs`: offline actual-page test with a saved chain.
  Disables `boot()` ONLY in its in-memory copy, intercepts every request, and
  asserts zero POSTs. Never point a UI test at the production app: its existing
  boot behavior may start a paid pipeline.

Run `node --test tests/test_live_ladder_explain.cjs`.
For the browser test install Playwright/Chromium in a test environment, then run
`node tests/check_live_ladder_browser.cjs`. `TEDDY_PLAYWRIGHT` may specify an absolute
Playwright module path; `PLAYWRIGHT_BROWSERS_PATH` may specify its browser cache.
Screenshots are written only to ignored `notes/live-ladder-review/`.

## Handoff to Claude

Do not merge the proposed order-flow/regime/validation refactors into this feature
without Mark's review. Do not run a pull, commentary rewrite, or Discord post to
verify this UI. Preserve the pre-existing uncommitted changes in the workspace.
Do not advertise the provisional materiality or confirmation rules as validated
trading signals. Read the proposal for the empirical work needed before that claim.


## Approved extension: current futures pressure

The embedded chart now shares its server-computed 60s flow window with both ladder
explanation panels. Only matching fresh contract/mode data is accepted from the
actual configured chart frame. It describes current pressure separately; it does
not retroactively confirm a past price event or estimate option positioning.
See INTRADAY-IMPLEMENTATION.md for source, coverage, limitations and remaining work.


## 2026-09-14 — live commentary replaces Terminal sidebar widgets

At Mark's request, the old widgets to the left of the live chart were removed
from the Terminal layout and their customization controls retired. The live
commentary panel moved from below Gamma Ladders into that sidebar (340px desktop,
310px at the intermediate breakpoint), with independent scrolling. On smaller
screens it stacks above the chart. The Gamma Ladder view retains its explanation
panel. Shared book/QQQ controls still synchronize; no model or feed changes.
The existing browser check passed for both explanation locations, expanded
sections, reconnect behavior and narrow layout. Refresh the page to apply.


## 2026-09-14 — expanded live explanations

Mark requested more verbose live updates. static/live_ladder_explain.js now adds
plain-language paragraphs covering exact change/threshold, limits of price/time/
smile attribution under fixed OI, flip movement, confirmation versus invalidation,
and contemporaneous trade pressure versus price response. Missing or sparse
evidence is explicit; no dealer identity, absorption, probability or entry is
inferred. Confirmed-event log entries explain their threshold and persistence.
The existing 2-second local refresh remains; no LLM calls or extra data requests.
Sidebar scroll position and expanded details are preserved during refresh.
29 offline Node tests passed; the existing browser integration check also covers
the expanded renderer. Refresh the page to see the additional prose.

### Compact display (2026-09-14)
Default commentary now uses one-line summaries for gamma, flip, flow, price confirmation, and persisted ladder changes. Expand Full explanation and measurements for the longer interpretation. Amounts and significance remain visible; calculation and pause rules are unchanged.


### Layout and highlight (2026-09-14)
Gamma Ladders sits above Price Action. Prices align in the asset headers and preserve two decimal places. Equity prices remain futures-derived estimates and are labeled accordingly. Gold highlights the entire asset row for the largest absolute exposure node; bands highlight the band containing that node.


### Reading layout (2026-09-14)
Ladders span the Terminal width above the chart. Live Commentary and compact setups share the right sidebar. View settings expands ladder controls and full strike range; default shows seven strikes each side of spot. Key Levels is removed; use chart overlays for levels. Completed setups and session commentary are collapsed.


### Reorganization reverted (2026-09-14)
At Mark's request, restored the prior three-column dashboard, original ladder controls, full setup cards, Key Levels and Reading the Day. Ladders remain above chart; prior exposure highlighting and live explanation improvements remain.

