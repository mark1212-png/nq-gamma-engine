# The Basilisk game plan bot

Databento → the sheet → the model → a channel, on a schedule, with nobody in the loop.

```
 6:05 AM   gameplan_bot.py --job premarket      the RTH plan
 5:15 PM   gameplan_bot.py --job asia           the overnight plan
10:20 PM   gameplan_bot.py --job tomorrow       tomorrow's RTH plan
```

Each run does the whole thing: pulls the CME chain and open interest, compares the OPRA
equity books beside it, verifies, builds, asks the model to write the plan, publishes the
dated sheet, and posts the four cards to the channel as an image.

---

## 1. Make the webhook

In Discord, on the channel the plan should land in:

> **Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL**

That URL is the entire configuration. No bot in the server, no gateway process, nothing to
keep running. **It is also a bearer credential** — anyone holding it can post to that channel
as Basilisk, so treat it like the Databento key: user environment only, never in the repo.

```powershell
setx DISCORD_WEBHOOK_URL "https://discord.com/api/webhooks/<id>/<token>"
```

Then **close the terminal and open a new one** — `setx` does not reach into windows that are
already open.

Optional:

```powershell
setx DISCORD_MENTION  "<@&1234567890>"    # ping a role on the first message
setx DISCORD_USERNAME "Basilisk"          # webhook display name
```

<details>
<summary>If it has to come from a real bot user instead</summary>

A webhook posts as a channel integration. If the post must come from an actual bot account
(so it has a profile, can be mentioned, shows in the member list):

1. https://discord.com/developers/applications → New Application → Bot → Reset Token, copy it
2. OAuth2 → URL Generator → scope `bot`, permissions **View Channel**, **Send Messages**
   (and **Attach Files** if you want `--attach-sheet`) → open the URL, add it to the server
3. Right-click the channel → Copy Channel ID (Developer Mode must be on in Advanced settings)

```powershell
setx DISCORD_BOT_TOKEN  "..."
setx DISCORD_CHANNEL_ID "123456789012345678"
```

Same REST endpoint either way, so still no `discord.py` and still nothing to keep alive.
If both a webhook and a bot token are set, the webhook wins.
</details>

---

## 2. See what it would post

```powershell
cd "$HOME\Claude Gamma Analysis"
python gameplan_bot.py --post-only --dry-run
```

Free and instant: it renders the sheet already on disk and prints what the channel would see.
Nothing is sent. The full render also lands in `notes\discord-preview.json`.

Then the real pipeline, still without sending:

```powershell
python gameplan_bot.py --job premarket --dry-run
```

This one costs a Databento pull and one Anthropic call (~15–25¢ on Opus), because it runs the
whole thing — it just stops before the channel.

---

## 3. Post once, by hand

```powershell
python gameplan_bot.py --job premarket
```

Two messages:

1. a one-line header — date, reference, flips, rails, net gamma, book — and the **gameplan**
   image: the four cards in a 2×2, entries, structural stops, T1–T3 with their R multiples,
   and each card's before-this-plan line
2. the **equity cross-reference** — SPX / SPXW / SPY / QQQ / NQ, dealer gamma within 1% and
   each book's 0DTE flip, from the OPRA compare. A text embed; there is no sheet section for it

The cards alone, because that is what the channel opens the post for. The levels that would
have been in a masthead image — reference, flips, rails, net gamma, the book — are in the
header **text** on the same message, which also makes them searchable, visible in a phone's
notification preview, and copyable. An image is none of those things.

Message 2 is skipped when there is no `data\opra\<settle>-compare.json`, which is the normal
state until the 4:10 PM `opra` entry has run for that session. `--no-opra` drops it for good.

### More of the sheet, or none of it

```powershell
python gameplan_bot.py --post-only --parts overview,gameplan,reading   # the prose too
python gameplan_bot.py --post-only --parts all         # every section, ladder included
python gameplan_bot.py --post-only --format embeds     # Discord embeds, no pictures
python gameplan_bot.py --post-only --format both       # embeds, then the pictures
python sheet_image.py --list                           # what sections the sheet has
```

Rendering is headless Chrome or Edge, already on the machine — nothing to install. If it fails
for any reason the post **falls back to embeds** rather than not going out.

One consequence of posting the cards alone: the sheet's **caveat block** lives in the overview
section, so it does not reach the channel. The staleness caveat is already covered — a stale
sheet is refused outright, below — but a build that draws a caveat for another reason (a stale
reference, a preliminary book) carries it only on the sheet itself.

---

## 4. Put it on the clock

Register everything, with the plans building but **not** posting:

```powershell
powershell -ExecutionPolicy Bypass -File schedule_gameplan.ps1 -NoPost
```

Run a week like that and read `notes\gameplan.log`. When the output is what you want:

```powershell
powershell -ExecutionPolicy Bypass -File schedule_gameplan.ps1
```

`-List` shows what is registered and when it next fires, `-Remove` unregisters the lot, and
`-Intraday` adds mid-session refreshes at 7:30, 9:30 and 11:30 — off by default, because each
one is another pull and another API call.

The tasks run **as you, only while you are logged on**. That is deliberate: the API keys live
in your user environment and a task running as SYSTEM would not see them.

---

## What stops a post

The channel reads the output before any human does, so every gate is on the way in.

| | |
|---|---|
| autopull did nothing | outside its window, or no new book — nothing to say |
| `verify_pull.py` failed | the chain is short, or open interest moved more than 40% |
| the commentary could not be rewritten | **the important one** — see below |
| this exact plan already went out | the fingerprint matched |

**The stale gate.** After a fresh pull the sheet carries new numbers under the *previous*
plan's words — `build.py` draws its red banner and says so. That sheet is fine on a screen,
where the banner is right there. It is not fine in a channel, where four cards read as a plan
for today and were written for a different book. So `discord_post.py` refuses it, and
`gameplan_bot.py` stops the run rather than posting one. `--allow-stale` overrides that; read
the caveat block first and decide you meant it.

A half-sent post (message 1 landed, message 2 failed) **resumes** on the next run instead of
duplicating. The ledger is `notes\discord-posted.json`.

Nothing retries a paid Databento pull. A failed run is a failed run, the log says which step
stopped it, and the next scheduled slot picks it up.

---

## Where to look when it goes wrong

```
notes\gameplan.log            every step, ok or FAILED, with the reason
notes\gameplan-status.json    the last run, step by step
notes\discord.log             the posting leg alone
notes\autopull.log            the pull itself
notes\cc-last-reply.txt       the model's raw reply, when the commentary step failed
```

| what you see | what it is |
|---|---|
| `[pull] FAILED -- autopull did nothing this run` | outside the job window, or the book has not published yet |
| `[comment] FAILED -- ANTHROPIC_API_KEY not set` | no key, so nothing can write the plan, so nothing is posted |
| `[comment] FAILED -- reply rejected 3 times` | the model's cards failed the structural checks — `notes\cc-last-reply.txt` has what it sent |
| `[post] FAILED -- no Discord target` | `DISCORD_WEBHOOK_URL` is not visible in this terminal; `setx` needs a new one |
| `Discord HTTP 401` / `403` | the webhook was deleted, or the bot cannot see the channel |
| `not posted (duplicate)` | this plan already went out; `--force` reposts it |
| `not posted (stale)` | the commentary step did not run or did not succeed |

---

## The pieces

| | |
|---|---|
| `gameplan_bot.py` | the orchestrator: pull → equities → verify → ladder → state → build → comment → publish → post |
| `discord_post.py` | formatting and transport. Runs alone: `python discord_post.py --dry-run` |
| `sheet_image.py` | the sheet's sections as PNGs, via headless Chrome. Runs alone: `--list`, `--parts` |
| `schedule_gameplan.ps1` | registers, lists and removes the Task Scheduler entries |
| `tests\test_discord_post.py` | formatting, Discord's limits and the gates. No network |
| `tests\test_gameplan_bot.py` | the orchestrator's decisions. No subprocesses |

The numbers and the prose both come from `command_center.normalize()` — the same function the
`/cc` page renders from. There is deliberately no second parser: two of them would eventually
disagree about what the plan says, and the channel would carry a plan nobody could reproduce.
