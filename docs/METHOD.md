# NQ Gamma Workflow — Method Spec & Corrections

Handoff notes for reproducing the NQ dealer-gamma analysis from CME QuikStrike exports.
Written after cross-checking an existing workflow against an independent parse of the same
source files. Corrections are marked **[FIX]**.

---

## 1. Source data

Two exports from the QuikStrike **OI Matrix** (CME Essentials), printed to PDF:

| File | Setting |
|---|---|
| Raw open interest | Greek: **None** |
| Gamma matrix | Greek: **Gamma (1 Pct)** |

Both carry all listed expiries as paired C/P columns (16 expiries = 32 columns), one row per strike.

### [FIX] Do not paste the matrix as text

Pasting the table into a chat window collapses empty cells. Because the leading Q1DU6 call
column is empty at most strikes below spot, the collapse shifts every value one cell left,
and **put gamma gets read as call gamma**. This silently flips the sign of the near-money
downside structure.

Observed instances (Sep 1 settle file):

| Strike | Read from paste | Actual |
|---|---|---|
| 28,800 | 3C / 7P | 0C / 3P |
| 28,825 | 1C / 10P | 0C / 1P |
| 28,900 | 9C / 3P → net **+6** | 0C / 9P → net **−9** |

This produced a phantom positive-gamma cluster at 28,850–28,925 in a region that is
entirely negative.

**Instead:** extract from the PDF by x-coordinate. Read the C/P header row, take each
header's center x, and bin every numeric token on a row to the nearest header center.
Validate by asserting that no token lands more than ~9pt from a column center and that
no cell is written twice. On the Sep 1 file this yields 488 strikes × 32 columns with zero
ambiguous assignments.

---

## 2. What the Gamma (1 Pct) column is

The displayed value is **OI × Δdelta per +1% move in the underlying** — already
open-interest weighted, rounded to an integer.

Verification: at any strike, call and put per-contract gamma must be identical (put-call
parity). At 28,000 on NQU6: 14/236 = 0.0593 for calls, 141/2,419 = 0.0583 for puts. Match.

Units are **futures contracts of delta per 1% move**. Multiply by `20 × S` for dollar notional.

Because values are rounded to integers, single-digit nodes carry real uncertainty. Only
treat double-digit nodes as structure.

---

## 3. Expiry dating

QuikStrike's DTE column counts from the **report generation date**, not from the OI settle
date. Anchor it by working backwards from a known expiry.

Worked example — file generated 9/2/26 10:59 PM PT, OI as of Tue Sep 1:

| Code | DTE | Expiry |
|---|---|---|
| Q1DU6 | 1 | Thu Sep 3 |
| QN1U6 | 2 | Fri Sep 4 |
| QN2U6 | 9 | Fri Sep 11 |
| NQU6 | 15 | Thu Sep 17 — quarterly, last trade Thu, AM-settled Fri |
| QN3U6 | 16 | Fri Sep 18 |

Note the NQU6/QN3U6 pair: the quarterly shows one day *earlier* than the Friday weekly
because it stops trading Thursday. That asymmetry is the check that confirms the anchor.

Code convention: `QN#` = Friday, `Q#A` = Monday, `Q#B` = Tuesday, `Q#C` = Wednesday,
`Q#D` = Thursday.

**Consequence:** a contract labelled "1 DTE" in an evening export expires the *next
session* — i.e. it is that session's 0DTE.

---

## 4. Expected move (1D Max / 1D Min)

```
EM = S × IV / √252
1D Max = S + EM
1D Min = S − EM
```

Sep 3 example: S = 29,181.5, IV = 16.96% → EM = 311.8 → **29,493 / 28,870**.

Use the ATM settle IV of the front expiry. √252 (trading days) not √365.

---

## 5. HVL / gamma flip

**The Gamma (1 Pct) column cannot be used for this.** It is frozen at today's spot. The
flip requires repricing at hypothetical futures prices, so it must be built from **raw OI**.

```
Γᵢ(F) = φ(d₁ᵢ) / (F·σ·√T)        d₁ᵢ = [ln(F/Kᵢ) + ½σ²T] / (σ√T)

G(F) = Σᵢ Γᵢ(F) · (OI_call,i − OI_put,i)

HVL solves G(F) = 0, interpolated between bracketing F.
```

Scan F on a fine grid (25pt or finer) and interpolate the sign change.

### [FIX] The HVL is nearly IV-invariant for a single expiry

A prior run attributed a ~36-point HVL move to IV dropping from 18.07% to 16.96%. That is
not what the formula does. Re-running it on Q1DU6 (Sep 1 settle OI, T = 1/365):

| IV | HVL |
|---|---|
| 16.96% | 29,180 |
| 18.07% | 29,183 |
| 20.00% | 29,188 |
| 26.62% | 29,206 |

**~3 points across that IV change, ~26 points across the entire plausible IV range.** The
zero-cross is set by the OI distribution, not by vol. If a recalculation moves the HVL by
tens of points on an IV change alone, it did not come from this formula.

Correct value for Q1DU6 on the Sep 1 file at 16.96%: **29,180**.
(Values of 29,164 / 29,150.5 / 29,114 previously reported for this file do not reproduce.)

Also: use a **flat IV** for a single expiry. Fitting a skew across one expiry over-models a
thin chain — a fitted surface pushed the same number to 29,235, which is no better.

---

## 6. Which expiries to include

For an intraday plan, weight the front, but do not use the front expiry alone.

Gamma per contract scales as 1/√T, so the nearest expiry has the highest gamma per lot —
but it usually has far less open interest. Share of gross gamma, Sep 3 board:

| | Q1DU6 (0DTE) | QN1U6 (1DTE) | NQU6 (15DTE) | 0–2 DTE share |
|---|---|---|---|---|
| Overnight | 605 | 1,543 | 1,931 | 37.7% |
| 9:30 ET | 1,163 | 1,936 | 1,980 | 45.9% |
| 2:00 PM ET | 2,096 | 2,097 | 1,993 | 53.2% |

The 0DTE roughly doubles by the open and triples by mid-afternoon, but even at its peak it
is ~21% of the board. It only draws level with the next expiry by 2 PM ET.

**Recommendation:** 0–2 DTE as primary basis; let 0DTE dominate the read after midday.
Keep the full book as secondary context — the HVL and the major call wall are stable across
every basis, so wide aggregation adds ballast without changing the shape.

### [FIX] Watch for single-position artifacts

On the full book, 29,500 showed +142 net gamma and read as a major call wall. Stripping to
≤9 DTE collapsed it to +21 — almost all of it was one 1,838-lot call line in QN4U6 at
23 DTE, irrelevant intraday. Always re-run a candidate level on the near-dated subset before
trusting it.

---

## 7. [FIX] Rank on raw OI as well as gamma

Gamma weighting discounts strikes far from spot, because a distant strike generates little
hedging flow *today*. But those strikes are still targets, and their gamma multiplies if
price travels to them. Rank both ways and take the union.

Q1DU6, Sep 1 settle:

| Strike | OI C / P | Net Γ | Comment |
|---|---|---|---|
| 28,600 | 0 / 217 | **−28** | largest node in the contract |
| 29,070 | 107 / 18 | +26 | the shelf |
| 29,090 | 80 / 9 | +21 | shelf upper |
| 28,760 | 0 / 100 | −19 | |
| 28,710 | 0 / 100 | −17 | |
| 29,000 | 7 / 71 | −17 | |
| 29,700 | 130 / 1 | +9 | **OI-heavy, gamma-light — real wall, just far** |
| 29,800 | 108 / 4 | +4 | same |
| 26,400 | 0 / 566 | −1 | **OI-heavy but tail hedge — gamma correctly kills it** |

The last two rows are why you need both metrics rather than either alone.

Also: **do not clip the display to the 1-day band.** 28,600 sits below −1σ and is the largest
node on the board. Scan at least ±2% and show anything material.

---

## 8. Verified output, Sep 3 2026 session (Q1DU6 basis)

Reference 29,181.5 · Settle IV 16.96% · OI as of Tue Sep 1 settle

```
29,800   OI wall 108C          +4
29,700   OI wall 130C          +9
29,600   call resistance      +13
29,593   1D max + buffer
29,493   1D MAX
29,450   on the rail           +6
29,340   call support          +7
29,200   balance               +1
29,181   REFERENCE
29,180   HVL  ← price opens on the flip
29,150                         −4
29,100   pocket floor          −8
29,090   shelf upper          +21
29,070   shelf core           +26
29,050   cliff                −12
29,000   put support          −17
28,900                         −9
28,870   1D MIN
28,760   put wall             −19
28,710   put wall             −17
28,650                         −9
28,600   LARGEST NODE         −28
28,500   lower anchor         −11
```

Structure: price opens on the flip with no directional gamma edge. The decision band is
**29,050–29,100** — the shelf either holds (sweep/reclaim long) or fails (short into an
air pocket with no positive gamma anywhere below, running to the 28,760–28,500 wall).

---

## 9. Standing caveats

- **OI staleness is the dominant error source.** CME settle OI is T+1. For a 15-DTE contract
  that is tolerable; for a same-day expiry it is not — 0DTE OI builds in the final 24–48
  hours. Re-pull before the open and re-check whether the key shelf still exists.
- **Dealer sign convention** (long calls / short puts) is an assumption about positioning,
  not an observation. Every sign on the sheet inverts if it is wrong.
- **NQZ6-settled expiries** (QN3U6, Q3AU6, QN4U6, NEU6, QN1V6) trade against a different
  underlying, ~287 points above NQU6. Compute their gamma at their own spot; their hedging
  flow lands in NQZ6 futures, not NQU6.
- Gamma describes where hedging flow concentrates. It is not a directional signal and it is
  one input among several.
