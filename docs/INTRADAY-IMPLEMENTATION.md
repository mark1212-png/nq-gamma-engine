# Intraday implementation — 2026-09-14

Mark approved the proposal and requested a heatmap repair. This is the first
implementation of that approved project; the remaining work below is still open.

## What Mark can see now

Refresh the chart / command center. The chart sidebar shows server-computed
10s/60s/300s trade delta, buy/sell/unknown volume, classification coverage, print
count, 60s displacement, and CVD. CVD reports its first observed trade, because a
partial replay must not imply complete session coverage. Window calculations use
completed event-time seconds; CVD includes the latest accepted print.

The embedded chart also shares its 60s measurements with the live ladder panels.
The parent accepts only its actual chart frame and configured origin, a matching
contract/mode, a feature timestamp within 10s, and a trade age below 30s. Current
pressure is displayed separately from historical price confirmations. It does
not retrospectively confirm an earlier event. Sparse samples stay visible with
their counts; no confidence score or entry recommendation is attached.

## Heatmap: confirmed cause and repair

Read-only inspection of the running WebSocket showed trades but zero BOOK frames.
After adding error observation and restarting only teddychart, the live client
reported **`BentoError: Not authorized for mbp-10 schema`**. This is the cause of
the all-day blank depth display. Successful `subscribe()`/`start()` calls were
previously logged as streaming before the gateway's asynchronous rejection.

The chart now shows depth schema, waiting/disabled/stale/receiving/error status
and the actual gateway error. `watch_book()` observes `block_for_close()` failures.
Mark subsequently authorized using the existing included MBP-1 feed. The server
constructor and CLI now default to `mbp-1`; the chart was restarted with that
schema. A live WebSocket check verified `receiving`, no depth error, and 21 BOOK
frames in 4.5 seconds. Browser inspection confirmed live best-bid/ask prices and
sizes. The panel explicitly labels MBP-1 and hides unavailable deeper rows; the
heatmap records displayed liquidity only at the best bid and ask. Ten-level
MBP-10 still requires a different live entitlement. No subscription change or
historical depth download was made.

Two additional rendering defects were corrected: shifting ladder slots no longer
preserve unrelated old prices/sizes, and size-scale / evolving-bar changes
invalidate the cached raster. Heat columns now mean the latest received snapshot
in each second, not maximum size per slot or every historical order event.
Zero sizes clear slots; out-of-order snapshots cannot corrupt the newest column.
Raster updates follow the one-second sample clock, and history is capped at
86,400 sampled columns, trimming an hour when full. Browser history starts when
that page receives depth; past candles do not contain historical depth.

## Feature source and limitations

`teddychart/flow.py` is deterministic and has no external I/O. `Tape.append()` feeds
the same trade aggregation in live, simulation, and replay. Features are broadcast
once per second. The raw live depth callback computes BBO OFI before UI coalescing.
OFI is unavailable without usable recent raw BBO, and sampled depth replay does
not pretend to reproduce raw OFI. Missing or stale depth makes OFI unavailable.
Unknown trade side is never converted into buying/selling in this feature stream.
Out-of-order records are counted and excluded; reconnect/session reset clears the
accumulator. No feature archive, historical normalization, or event-level flow
confirmation is implemented yet. This is observation mode.

## Commentary publication changes

`publication.py` hashes the complete trinity input, requires the original snapshot
at publication, uses an exclusive process lock for publication, renders in an
isolated directory, and rechecks the inputs after rendering. Prose goes into
`narrative.json`; normal commentary generation no longer rewrites executable
`build.py`. The builder prefers this structured prose and checks its exact input
identity. Legacy embedded prose remains a migration fallback.

Each new version is retained under `notes/commentary-revisions/<snapshot>/<time>/`.
Writes use atomic replacement. `publication.json` is written last as a commit
marker and binds input, rendered HTML, and prose hashes. Discord fails closed
when this evidence is absent/mismatched, even if no warning sentence is present.
The dashboard labels an unverified saved report. Multi-file replacement is not
one filesystem transaction: interrupted/mixed writes fail validation.

Matching state (including asof) is required before invoking a commentary sender;
a required narrative checker exception blocks publication. Stand-aside allows
zero cards only; thin allows at most two and no more than carried scenarios;
normal allows zero to four valid cards. Count rules override the old four-card
prompt. Existing geometry checks remain. Replay renders in isolation and swaps
only the temporary HTML, preserving the legacy replay restore contract.

This is not yet complete provenance across every chain/state/ladder writer:
chain, ladder fragment, sizing, and state do not share the same immutable bundle
identifier. Upstream writers do not yet share the publication lock. Do not claim
all phase-1 race criteria are satisfied. Existing historical prose was not
retroactively marked validated or regenerated. On migration, posting remains
blocked until a replacement passes the new publication path.

## Historical studies and tests

`session_bars.py` replaces fixed UTC RTH filters in histval, tradeval, backtest,
and extract_levels. It reuses nqvol's existing calendar and DST rules, excludes
bar starts at the close, and honors listed half-days/closures. extract_levels
now analyzes the next session rather than the settlement date. Old study outputs
must be regenerated before being used for calibration. Calendar coverage still
depends on the existing maintained year tables.

`requirements-core.txt` records installed direct package versions; it is not a
complete transitive lock or a verified clean installation. Tests block socket
connections and subprocesses by default. A staging-render test explicitly enables
only its copied local renderer. Existing test fixtures were updated to reflect
the required state/validation and close-exclusive window contracts.

Validation: 178 affected Python tests passed; 26 ladder Node tests passed; both
offline browser checks passed. The actual live chart was also inspected: current
flow measurements and the depth entitlement error were visible with no JS errors.
Synthetic depth was decoded and painted by the actual browser renderer, with
price-shift, zero-size, out-of-order and scale checks. No real depth pixels could
be verified because the gateway rejects this key's depth access.

Commands:

```
python -m pytest tests/test_intraday_reliability.py tests/test_cli_commentary.py tests/test_histval.py tests/test_discord_post.py tests/test_nqvol_math.py -q -p no:cacheprovider
node --test tests/test_live_ladder_explain.cjs
node tests/check_heatmap_browser.cjs
node tests/check_live_ladder_browser.cjs
```

Browser checks require Playwright/Chromium, as described in LIVE-LADDER.md.
The chart service was restarted. After /cc/api/state confirmed running=false,
the main teddy.py service was also restarted to activate the Python safeguards.
The saved report remains viewable; its unverified status was checked through the
report API. Refresh the browser for static changes. No live commentary was regenerated or posted.

## Remaining approved work

1. Complete chain/state/ladder provenance and coordinated upstream publication;
   verify clean dependency installation and wider regression coverage.
2. Enable the appropriate depth entitlement after reviewing its cost; then verify
   real depth, add bounded server history/capture, and test raw OFI replay parity.
3. Persist features and deduplicated event evidence, add point-in-time flow/price
   confirmation annotations, and test significance by session and activity.
4. Add realized-volatility/efficiency/VWAP context and historical session baselines.
5. Specify and price nearby/near-dated live options quote access; implement quality
   filters and shadow IV surfaces before allowing them to alter the gamma ladder.
   Existing OPRA live statistics remain daily OI, not live quote/position updates.
6. Source point-in-time breadth, rates and release surprises; perform held-out
   ablation/calibration studies before interpreting these observations as signals.

The proposal remains the acceptance checklist. Future data costs still require
the concrete symbols, schemas, coverage, estimate, and purpose review described there.


## 2026-09-14 — four-asset ladder startup repair

Mark reported the four-asset live ladder breaking. All four chain endpoints were
healthy. Reproduced a startup stall: after the preceding service restart the
pipeline was idle with pending step records, but boot() waited for completed
steps for up to 15 minutes before loadReport()/TAPE.connect(). It now exits the
wait when running=false and opens the saved report. Active jobs still wait.
This corrects startup only; it does not change gamma math or data subscriptions.

Read-only browser verification against the live service showed NQ, QQQ, SPX and
SPY columns and connected NQ trades with no JavaScript errors. New offline test
`node tests/check_four_asset_ladder.cjs` exercises actual boot() (not bypassed),
idle/pending state, all four repricings after NQ/ES price changes, and 0DTE/near/full
in both strike and band modes. All requests are intercepted; no model, data pull,
or posting operation ran. Refresh the command center; no service restart needed.
