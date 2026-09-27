# Proposal: order flow, intraday context, and reliability

**Status: Mark approved implementation on 2026-09-14. First implementation delivered;
this is a staged project, not a claim that every acceptance criterion below is met.**
See `INTRADAY-IMPLEMENTATION.md` for exact shipped scope, verification, and remaining work.
Live depth is currently blocked by Databento's confirmed `mbp-10` entitlement rejection.
No new subscription was purchased. Live option quote/IV updates are not connected yet.

## The intended result

Use the gamma ladder to identify exposure concentrations, observed price behavior
to describe what is happening there, and order flow/liquidity to measure the
pressure accompanying that behavior. Keep these as separate pieces of evidence.
An increasingly detailed explanation is not proof of a profitable entry.

The chart already has footprints, CVD, VWAP, a DOM, and a heatmap. The first order-
flow project should make those measurements available consistently to the decision
panel and historical testing, rather than add another independent chart.

## Phase 1 — reliability before more actionable output

1. One immutable snapshot identifier tying together chain, inputs, report, state,
   and commentary; reject mixed versions, including same-session stale state.
2. Store prose as structured data rather than rewriting executable `build.py`.
   Use a process-wide lock and atomic publication of a complete version. Preserve
   the original plan and append explicitly timestamped revisions.
3. Required checks must block publication when unavailable or failing. A last
   successful report remains viewable, clearly labeled; an unchecked replacement
   must not appear approved. Discord should inspect structured validation status,
   not search for a warning sentence in HTML.
4. Apply the trade gate throughout: zero cards for stand-aside, limited cards for
   thin books. This resolves the current requirement for exactly four cards.
5. Fix DST/holiday session windows and snapshot dating in historical studies before
   using them to calibrate new rules. Pin runtime dependencies and make network/
   model/process calls disabled by default in tests.

Acceptance: race tests cannot mix versions; stale/missing validators cannot publish;
stand-aside works end to end; winter and early-close studies select the correct
bars; tests cannot spend quota or post to a channel.

## Phase 2 — order flow from data the chart already consumes

### Shared, deterministic measurements

Build one timestamped server-side feature stream from the existing futures trade
and depth inputs. Both chart and explanation panel consume it. Preserve event time,
receive time, instrument ID, contract mapping, session, source, and gap status.

| Feature | Definition/use | Important limitation |
|---|---|---|
| Trade delta | Buyer-initiated volume minus seller-initiated volume over 10s/60s/5m; measure aggressor pressure near a level | Unknown-side volume stays unknown; never counted as buying or selling |
| Delta fraction | Signed classified volume / total classified volume, with classification coverage shown | Sparse samples can give extreme ratios; require minimum coverage/activity |
| CVD | Accumulated classified trade delta from an explicit session anchor | Reset consistently across chart, replay, and server; it is not dealer inventory |
| Price response | Signed displacement and range alongside aggressive volume | Heavy buying with limited upward progress is an observation, not proof of a named participant absorbing |
| Spread/depth | Spread in ticks, near-touch depth, changes in displayed depth | Displayed liquidity can disappear; it is not a promise of a fill |
| Order-flow imbalance | Changes at best bid/ask accounting for price/queue changes | Compute on the raw ordered feed before UI coalescing; trade delta alone is not OFI |

The existing chart streams coalesced depth snapshots for display. Those alone may
not reconstruct every book event needed for historical OFI. Audit the raw ingest
and archive before claiming an accurate OFI replay. A new capture/storage path may
be required even if no new subscription is needed.

### What Mark would see

At selected gamma levels, add a compact row with:

* last-minute aggressive buying/selling and net delta;
* observed price progress over the same interval;
* depth/spread relative to the normal level for that session/time;
* classification coverage, sample size, and data freshness;
* a concise interpretation such as “selling increased while price progressed
  below the level” or “aggressive buying increased with limited upward progress.”

Chart annotations would mark the timestamp and level of confirmed observations.
Clicking an annotation would reveal exact inputs and rule, not a generated rationale.
The original plan's entries/stops/targets would remain fixed. Optional sound should
be off initially and enabled only for confirmed, deduplicated events.

### How this would enter trading

Start in observation mode. Proposed conditions to test include:

* a price-confirmed loss of a saved gamma level with persistent negative trade delta;
* a reclaim supported by positive delta and upward price progress;
* a failed reclaim with renewed selling after the retest;
* a high-volume test with unusually little price progress, followed by an actual
  reversal — not a preemptive fade just because a large print appeared.

Do not call these entries until tested with executable timing, spread, slippage,
commissions, and stop/target sequencing. A confirmation available at the end of a
minute cannot receive an earlier price from inside that minute as its fill.
Keep pressure, price confirmation, and gamma context visible individually rather
than combine them into an uncalibrated “confidence 87%” score.

## Phase 3 — regimes and commentary context

**Price/volatility:** time-of-day-normalized realized volatility, movement efficiency,
range overlap, and VWAP location/slope. Describe movement, volatility, liquidity,
and participation separately. Avoid assigning trend direction from gamma sign.

**Asia/Europe/RTH:** benchmark each session against its own historical distribution.
Include overnight gaps and release windows. Assess both wall-clock and activity-
based confirmation windows; a low-volume minute must not be treated as equivalent
to an active RTH minute without evidence. Keep a fresh-feed requirement throughout.

**Breadth:** point-in-time Nasdaq constituent participation, weighted contribution,
percentage above VWAP, and breadth beyond the largest names. This requires additional
equity data and constituent/weight history; a current constituent list is unsuitable
for historical testing.

**Catalysts/rates:** extend the existing release calendar with actual-versus-consensus
surprises and timestamped market response. Add rates and NQ/ES relative behavior as
context; avoid a fixed causal rule such as yields-up means equities-down.

**Live options quotes, later:** separately sourced quotes could update the IV surface.
Treat quote age, spread, liquidity, and surface failures explicitly. Options flow can
provide a positioning estimate, but aggressor identity does not reveal customer/
dealer identity or opening/closing intent. It must remain separate from official OI.

## How significance would be established

The shipped display thresholds are initial filters, not statistical findings.
Calibrate them on a development period, freeze them, and test on later untouched
sessions. Define each prediction/outcome and horizon BEFORE looking at performance.
Use session-level resampling or clustering to account for many related observations
on the same day, and account for testing multiple rules. Report uncertainty, sample
size, and failures as well as positive results.

Compare price-only, gamma-only, order-flow-only, price+flow, and price+flow+gamma.
That tests whether gamma contributes information beyond the tape, not merely whether
a sophisticated combined rule can fit the sample. Separate Asia, Europe, RTH,
release days, roll periods, and volatility conditions without mining tiny subgroups.

Measure two outcomes independently:

1. **Interpretation:** whether a confirmed change persists, reverses, or is invalidated
   over a specified future horizon; false alerts and delayed detection.
2. **Execution:** subsequent executable entry, adverse/favorable excursion, expectancy
   after costs, tail losses, and how often a setup is actually available to trade.

Persist event inputs, thresholds/version, confirmation time, subsequent outcomes,
and plan version. Replay must use exactly the live feature/confirmation implementation.
Do not select the day's winning interpretation after seeing the close.

## Data, cost, and rollout

First audit existing subscriptions and retained fields. Reuse the current NQ trade
and depth streams where appropriate; avoid duplicate paid subscriptions. Before
any new download/subscription, prepare the symbols, schemas, history length,
estimated bill, storage, and purpose for review. No new data purchase is part of the
current live-explanation implementation.

Suggested sequence: reliability fixes → shared order-flow measurements → replay
and shadow logging → validation → optional chart alerts → breadth/rates/options-IV
extensions. No automated execution or Discord distribution is proposed by default.

**Decision requested after review:** approve a phase or revise its scope. Until then,
only the documented live ladder explanations and price confirmations are applied.
