# How to think when writing and ranking a TeddyAI plan

The single source of truth for the commentary. `command_center.PROMPT` reads this file at
import and sends it verbatim to the generator, so a scheduled job and a hand-written `/build`
think the same way. Edit this file, not the prompt.

Pinned 2026-09-17 at Mark's request — *"I want the future commentaries outputted like yours
moving forward, can we temporarily hard wire it unless something changes"*, then *"just make it
so how you would think when writing and ranking the prose"* — from the 2026-09-18 Asia sheets.
It is a convention, not a measured result. Change it deliberately and date the change.

**What renders:** the **lead**, the **cards**, the **VEX note**. `reading` is accepted by
`build.py` and thrown away (2026-09-16, "the reading goes"). Nothing put there reaches the
page, so anything that must be read goes in the lead or on a card.

---

## 1. Read before deciding anything

In this order, because each one changes what the next one means:

1. **The candidate list** (`precommentary.py --candidates`). This is the board. It has already
   computed every entry zone, every structural stop, T1 and R, and graded each row `CARD` or
   `level, not a card`. Do not re-derive any of it; you will disagree with the page.
2. **The read** at the top of that list — up, down or none, and the floors or ceilings it is
   counted from. This is the single most consequential line on the page, and §4 ranks on it.
3. **The level states** — first touch, reclaimed, wicked, broken. A wick is not a failure. A
   close beyond a wall that stayed beyond is a *break*, and its continuation side then grades
   *chases a move already made*.
4. **The engine's paths and gate** — the base case, the alternatives, how many carry each way,
   and the gate verdict.
5. **The previous sheet**, and the tape ledger on a rebuild inside a session. Diff against the
   previous build deliberately: the strongest part of a lead is usually what changed since it.
   A level already triggered since the session opened is written for its next visit or withheld.

## 2. Find what the page is about

**First, the salience question: what is the single most consequential fact on this board
tonight?** Find it, then build the page around it and let everything else be support. This is
the judgement that separates a sheet that reads as a plan from one that reads as a list of
levels, and it is not reducible to a rule — but it is the first thing to do, before any
structure work. On the 2026-09-18 14:54 sheet it was three things: the read had flipped to up,
four walls had gone from wicked to broken, and the quarterly expires mid-plan. The lead opened
on all three and the cards followed from them.

**Then the mechanical question: did the book, the clock or the tape change?** That answer is
the lead's second beat and it usually decides the ranking too.

- The **book** changed → the flips and net gamma move on their own. Say so before reading any
  level as news.
- The **clock** changed → the rails moved and nothing else did. That is the band, not the
  market.
- The **tape** changed → the read and the level states move, and the card order moves with
  them. This is the one that re-orders the page.

## 3. Turn candidates into cards

**A LIVE CARD IS CARRIED, NOT REPLACED.** Before selecting anything, look at what the previous
sheet's cards are doing — the tape ledger in the payload grades every one of them. **A card that
is in play and not invalidated stays on the page, unchanged: same entry, same stop, same
targets.** A rebuild is a new map, not a new opinion about an open trade. Do not retire it, do not
re-price it onto a nearer rung, and do not manufacture a different setup in its place — fill the
remaining slots around it.

(2026-09-17, and this is the rule's whole reason: the 29,650–29,675 hold was written at 14:54,
carried at 16:20, then **retired by the 23:17 rebuild** which replaced it with a 29,625 hold. It
was live and uninvalidated at the moment it was dropped. It filled at 01:16 on the 29,648.00
touch and ran +31, 2.58R on its 12-point stop; the 29,625 that replaced it never came within 17
points of its entry. Mark: *"the 650 call was the correct one and the most accurate one I would
have taken"* — and the sheet had thrown it away four hours earlier.)

**A level the tape has held keeps its card even when the engine grades it out.** The candidate
list scores geometry from the current reference; it does not know that price has defended a floor
three times. If the tape is holding a level, that level is carded and the card says the tape is
the reason, not the book. This is the one place a `level, not a card` row is overridden — say so
explicitly when you do it.

**Otherwise, take only rows graded `CARD`.** A row graded `level, not a card` has already had its stop
and target priced and found not to pay. Name it in the lead; never card it.

**Merge before you rank, and let the zone span every rung you merged.** Adjacent same-side rungs
that share one invalidation are ONE card, written as a zone — and the zone runs from the
outermost rung to the innermost one. **Take the engine's zone from the candidate list and do not
narrow it.** If the why names a rung, the entry has to include it. (2026-09-17: the engine
offered 29,652–29,675 on the afternoon floor; the card was written 29,650–29,670 while its own
why called 29,675 the first of its three rungs. The session low was 29,674.00 — it missed the
fill by four points, on five points that had been trimmed off the top for no reason. 29,675 was
a real strike carrying +35 on the 0DTE book and session VWAP −1.) Three rungs stopping one point under the same swing is one zone, not three
cards competing for the top of the sheet. A hold and a break on the same strike are one zone —
the break enters where the hold stops — never two entries a point either side of it.

**Aim for four, two a side — and notice when you are filling a quota.** Picking the best long
and the best short is structure; reaching for a second of each to balance the page can
manufacture a card. If the structure genuinely offers only one side, write that and say so in
the lead.

## 4. Rank: the read sorts first

**When the read is up or down, it is the primary sort.** Cards with the read come first, and
structure orders them within that. When the read is **none**, structure sorts, in this order, because the
two preferences routinely disagree: **near-wall first** — **measured from the edge of the zone
price reaches FIRST, not from the quoted entry** — then node size, near-wall scaled to the
session (`rail_scale` times the 40-point near-wall).

**Stop width does not rank** (2026-09-25, Mark: *"we dont need to favor tighter stops just more
directional accuracy"*). A tighter stop is not a better trade and never moves a card up; the
width is printed on the card against the session band and that is all. This was the tiebreak
that put fades at fresh highs first on trend nights (the 2026-09-24 Asia Primary, a 36-point
short against a 45-point one, stopped at 05:31 while the engine's paths ran up). What replaces
it is a direction input that has been measured, not one that sounds directional — until one
is, the read is the only directional sort on this page.

A zone card has two prices. `entry` is the far edge, the worst fill, the one R is quoted from; the
touch edge is the near one, the price the tape actually arrives at. Near-wall asks how close the
card sits to the strike it leans on, so it is a question about the touch edge. Measured at the
quoted entry it answers a question nobody asked. (2026-09-17, the afternoon floor: touch edge
29,675 sits ON a +37 strike, distance 0; the quoted entry 29,650 has nothing material within 25
points. Ranked from the entry the card came third and a fade took Primary; the fade never traded
and the floor filled and paid TP1. Measured at the touch edge it ranks first, and stop-band fit
and reach both agree — three of four inputs pointed at it and only the mismeasured one did not.) An entry sitting ON a heavy strike
outranks one with a tidier stop; that card names its stop width and says it is wider than the
band rather than being demoted for it. (Set 2026-09-17: the same board ranked a fade first on
near-wall and a hold first on stop-band, and nothing in the file said which won.) Node size alone never promotes a card — the biggest
strike on the board is where hedging is concentrated, which is not where price turns. Never
rank by confluence dots.

This is written from what the sheets actually did, not from what the old prompt said. The two
2026-09-18 Asia builds, 83 minutes apart on the same book with identical stops:

| | 13:31, read **NONE** | 14:54, read **UP** |
|---|---|---|
| 1 | 29,762–790 rejection · 19 pts | 29,650–670 hold · 12 pts |
| 2 | 29,650–675 hold · 12 pts | 29,793–801 acceptance · 86 pts |
| 3 | 29,800–867 acceptance · 73 pts | 29,762–790 rejection · 19 pts |
| 4 | 29,612–630 break · 169 pts | 29,593–600 break · 130 pts |

Nothing structural moved. The read flipped and the long side took the top two slots. Rank that
way on purpose.

**A note learned from review, not produced by the method** (2026-09-17, after Mark asked why
the page was leading with a fade on a day that closed +486): ranking on stop quality alone
systematically favours fades on a trend day, because a fade at a fresh high always has a swing
a few points away while a breakout's most recent swing beyond the entry is by construction far
below. Sorting on the read first mostly fixes this. When a card still lands lower than its
thesis deserves, **say so on the card** — that its stop is why it is ranked there, not its
thesis — so the reader can take it knowingly.

**The read goes blind on a one-way leg** (2026-09-21 — read all of this before using any of
it). `swings.bias()` returns **none** when there are not enough confirmed swings to say, and a
leg that does not pull back prints no pivots. So "read NONE" on a trending session is a fact
about the detector, not about the tape: it does not mean the floors and ceilings disagree, it
means there are too few of either to ask. The same session leaves every stop on the board on
the fallback construction, **on both sides** — a page where not one card leans on a place the
tape actually turned. Both belong in the lead. Neither re-orders anything, and the rest of this
block is why.

**What does not follow — and sat in this file for eight hours before the tape killed it.** On
the morning of 2026-09-21 this section said that when the read is none, no card carries a swing
stop, and the session is more than one band from its open and within half a band of its
extreme, then the **leg direction** sorts in place of the read and the counter-leg card cannot
be Primary. By the close:

- **That day it would have been right, and enormously.** Same board, same minute, every shape
  graded from publication: **all five rejections stopped for −1.00R** — 30,525, 30,550, 30,600,
  30,650, 30,700 — and **all four acceptances paid TP1 or better** — 30,525 +2.56R, 30,550
  +2.93R, 30,600 +2.11R, 30,650 +3.56R. Both holds never filled: the post-publication low was
  30,506.50 against a 30,501 entry, **missed by 5.5 points on the engine's own unnarrowed
  zone**.
- **Those same three conditions fire on 4 of the last 34 sessions** when evaluated at a build
  time an hour into RTH — 2026-08-04, 08-06, 08-13 and 09-21 — and **on 2026-08-13 the fade was
  the winner.** The leg was +324 at the cut, the next level up was 30,250, it was touched at
  14:32, it never traded 23 points against the short, it paid at 14:55 and the session closed
  at 30,194.75. The with-leg acceptance stopped.
- **The "a pullback will not fill" half does not survive either.** Two of those four days
  retraced **more than 130 points** after the signal. Only 09-21 (17.5 points) and 08-04 (41.8)
  did not.

**Two wins, one tie, one loss for the leg, on four days.** The conditions describe a session
that has been one-way *so far* and carry nothing about the rest of it. **Do not sort on the
leg. Do not demote a card for being counter-leg or promote one for being with-leg.** Rank as
this section already says — near-wall, then node size (stop width stopped ranking on
2026-09-25) — and if the counter-leg card sits nearest its wall it takes Primary, which is
what happened on 2026-09-21 and what lost. That is the cost of not having an edge, not a fault in the ranking.

**What to do instead is bookkeeping, and it is worth doing.** When a session is one-way and the
read has gone none, the lead says three things: which kind of none it is; that every stop on
the page is geometry, on both sides; and **that the with-leg pullback cards are limit orders
the session has not come back for**, with the distance to each. On 2026-09-21 that reads: the
two holds sit 11 and 36 points under price and the tape has retraced 17.5 points all morning.
Then the reader decides. The page does not decide for him.

**And it is still not the gamma sign.** Mark, on that sheet: *"we were long gamma shouldnt we
only look for longs theres no negative exposure at all."* The book was positive at all 37 of
its 0DTE strikes, which says price is above the strikes and nothing else; `state.json`'s own
inversion test leaves the flips, the rails, the node ranking and the formations identical and
moves only the regime label. §8 stands.

**Primary.** Exactly one card is Primary when the gate reads `look`; none under `thin` or
`stand aside`. Primary is an ORDER — the best-structured card, the one to take first if only
one is taken — and it makes no claim about direction. **Do not withhold it because direction is
unknown: direction is always unknown here, and a rank that can never be reached is not a rank.**
A Primary long and a Primary short are the same claim about structure. The top card carries the
`pri` border, so labelling it anything else contradicts the page.

## 5. Write each card

- **Stop**: **one point beyond the most recent swing beyond the entry** — and the swings are in
  the payload, under `SWINGS THIS SESSION`, with their times. Read that list, pick the swing, name
  it and its time on the card. Name the swing and its time on the card. Never
  invented, never rounded to taste, never capped. There is exactly one other construction and it
  applies only when the tape has made no swing beyond the entry — the nearest material strike
  plus 0.15x the drawn band — and a card using it says so. On 2026-09-17 the two were both live
  in the prompt with nothing ranking them, and the same 29,750 rejection came out with a 44-point
  stop from one and a 97.75-point stop from the other: 1.70R against 1.24R on the same trade. Put the width in points on
  the card and say what it is against the session-scaled band. A wide stop is a fact about the
  board, not a reason to delete the level.
- **Targets**: exactly three, in order away from the entry, each a price that exists — a strike,
  a flip or a rail. TP1 pays at least 1.2R from the named entry. If only a far target clears
  that, use it and say why the near one does not pay.
- **R from where**: a zone entry has two R figures. Quote the one from the worst fill as the
  number to size on, and say which end each came from.
- **Void condition**: what kills the card — the swing that must not print, and what the next
  rebuild's rails do to it if price is beyond them.
- **Level history**: first touch, second visit, reclaimed, wicked, broken — and write the card
  for its NEXT visit.
- **Pairing**: cards that cannot both be live say so on both sides.
- **The counter-argument, on every card, opening with a BOLDED lead-in.** The bold is the
  requirement — it is what makes the block scannable and what the script looks for. The
  WORDING is free and should name what actually argues against *this* card: `Against it:`,
  `What is wrong with it:`, `Why it is third and not first:`, `...which is the whole
  trade-off:` are all right when they are true. A stock phrase on four cards is worse than
  four openers that each say what the objection is. End with what argues against the card,
  in the structure and
  the levels on this page: the wall sitting under a short entry, the base case running through a
  long entry, a stop wider than the session band, an entry at the wrong edge of the range.
  **Never in statistics** — no hit rates, no win rates, no sample sizes, no p-values, no
  placebos, no "the record says", no "it may not hold". This narrows the 2026-09-13 "do not
  argue with the page" rule rather than reversing it: argue in levels, never in statistics. A
  card that defends itself with a significance test is not a card; a card that names the
  structure against it is honest.

## 6. Write the lead

Long is fine — it is the only prose block on the page. In this order:

1. What the plan is for: session, build time, hours, the book and vintage, the reference and
   where it came from (settle vs print, and the print's time).
2. The salience answer from §2, then what changed since the last sheet and whether it was the
   book, the clock or the tape. Name the previous build; say which of its cards are void.
3. The read, and that it re-orders the cards when it has changed.
4. Level states that changed — wicked → broken especially.
5. Location: the three flips, where the reference sits against them, net gamma at spot per book,
   and whether the chain-wide sums agree in sign. Report both, labelled.
6. **The open-location branch.** Which cards are live from where price opens: the live window,
   each side of it, what a reopen beyond the band does. This used to live in `reading` and
   therefore never rendered — it belongs here.
7. Any tension you are leaving unresolved — the read against the base case, for instance. Name
   it and say you are not resolving it. Do not quietly pick a side.
8. The gate verdict verbatim, which card is Primary, the red-folder line, and what expires
   mid-plan.

This order is reverse-engineered from sheets that worked, not a template to fill. If the
salience answer wants a different order, take it — but everything in the list belongs on the
page somewhere.

## 7. Run the checkers and fix what they say

**This step does as much for the sheet as anything above it, and it is not optional.** Run
`publish_plan.py --prose <file> --build HHMM --dry`, read every line, fix, and re-run until it
comes back clean. Then publish without `--dry` and read the post-publish gate too.

A rejection means the card is **wrong, not unlucky**. Fix the card; never argue with the
checker and never soften the prose to get past it. What they actually caught on 2026-09-17:

- `interpret.check_card_levels` — *"target 29,972 is not a level, the nearest is 2 points
  away"*. Two targets I had taken from the candidate list's rounded print did not exist on the
  board. Moved to 29,970 and 29,450.
- `precommentary.structure_problems` — *"TP1 is 0.37R, under 1.0R: this side grades a LEVEL, not
  a card"*. The breakdown card was targeting the lower rail, which does not pay on a 169-point
  stop. Retargeted to the full flip at 1.6R, which is what made it a card at all.
- The same checker, twice more — *"ABSURD: stop 29,723 is not structural, the structure puts it
  at 29,729.50"*. The tape had moved past the swing the card was written on. Both times the
  right answer was to take the engine's number, not to keep mine.
- `verify_commentary` — blocking vs advisory. Zero blocking is the bar; advisories on tape
  prices and rail-at-hour figures are expected and are not a reason to change anything.

If a card cannot be made to pass, it is not a card. Drop it and name the level in the lead.

Then run **`python check_prose.py --sheet sheets/<the sheet>`**, which tests the published
prose against the mechanical rules in this file — one Primary, three real targets, TP1 at
1.2R, a void condition and a counter-argument on every card, and all eight beats in the
lead including the branch block actually rendering. It exists because asserting that the
prose follows this file turned out to be worth nothing: the first run came back 25/29.
It cannot see selection, merging, ranking or salience, so a clean run means nothing was
dropped, not that the page is good.

## 8. Never claimed

- **Direction from the gamma sign** — it describes how hedging behaves once price moves, not
  which way it moves.
- **A range from the gamma sign** — the flip's range claim measured p = 0.46.
- **A ranking from confluence** — 3/3 held no better than 1/3.
- **NEAR or FULL driving levels, cards or the read** — score 0DTE rungs against the 0DTE book;
  the other two are context and are named as such.

## 9. Voice

Every sentence carries a number or a level. No disclaimers, no restating the method, no hedging
boilerplate. State structure and location plainly; state regime tentatively or not at all.
Write for a discretionary trader who knows the method and is deciding what to do in the next
hour.

Worked examples: `notes/prose/2026-09-18-asia-1331.py` (read NONE) and `-1454.py` (read UP).
The second is the better one, and the table in §4 is the two of them side by side.

---

## Before you return it — the nine that get dropped

Every one of these has been missed by a real run. Check them against what you have written, in
order, and fix rather than explain:

1. **Exactly one Primary, and it is card one.** `build.py` gives card one the green border
   whatever you label it, so a Primary anywhere else makes the page contradict itself.
2. **Every entry is a zone** — `zone_lo` and `zone_hi` — taken from the candidate list and **not
   narrowed**. If the why names a rung, the entry includes it.
3. **Three targets per card, every one a price on the board**, TP1 paying at least 1.2R from the
   quoted entry.
4. **`VOID IF` in every tag**, naming the swing that kills the card.
5. **A BOLDED counter-argument on every card.** The bold is the requirement — it is what makes it
   scannable. `<b>Against it:</b>`, `<b>What is wrong with it:</b>`, `<b>Why it is third and not
   first:</b>` — whichever is true. Substance without the bold does not count.
6. **The open-location branch, in the LEAD.** Which cards are live from where price opens: the
   live window, each side of it, what a reopen beyond the band does. `reading` is discarded by
   `build.py`, so this is the only place it renders.
7. **Fewer than four cards? The lead says why** — which levels did not pay and what they are
   instead.
8. **The stop is the swing.** One point beyond the most recent swing beyond the entry. Only if the
   tape has made no such swing does the fallback apply, and then the card says so.
9. **Read NONE — say which kind.** "Not enough swings to ask", which is what a one-way leg
   produces, is not the same as "floors and ceilings disagree", and the lead must tell them
   apart. If no card carries a swing stop, say that too, and give the distance to any pullback
   card the session has not come back for. None of it re-orders the page.

5 and 6 are the two that fall out first when this document gets long. They are not optional.
9 is the one no checker can catch for you.
