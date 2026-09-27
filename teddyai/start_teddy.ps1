# start_teddy.ps1 -- the one-click start. Run via "Start TeddyAI.bat" (double-click).
#
# Order, and why:
#   1. teddy.py        the dashboard on :5137 -- everything else reads its /api/bars,
#                      so it goes first and the rest wait for it to answer
#   2. live_feed.py    keeps data\bars\<utc-day>.json current from Databento Live;
#                      skipped without a key (the dashboard then refreshes bars itself)
#   2b. tape_ledger.py writes notes\tape\<session>.md as the night happens, for the
#                      next regeneration to read; no key needed, rolls over sessions
#   2c. levels_live.py reprices the sheet's book at the last print every few seconds so
#                      the chart's walls and flips follow the tape; no key needed
#   3. teddychart      the chart on :5138 (footprint, large lots, TPO, VP, CVD, depth);
#                      the command center embeds it in its chart row. Skipped without
#                      a key unless -Sim (synthetic tape)
#   4. equity OI       autopull.py --job opra-live -- the last complete session's
#                      equity books with open interest replayed off the OPRA live
#                      gateway, which is the only place this morning's OI can be
#                      read. Minutes, not seconds; nothing waits on it
#   5. the browser     http://127.0.0.1:5137/cc -- opening the page runs the
#                      pipeline (pull, verify, ladder, build). teddy.py's own print
#                      feed (cc_ticks.py) drives the strip and the setup tracker
# Each service gets its own console window titled "TeddyAI - <name>" so you can see
# its log and close it on its own. Re-running this script never starts a second copy:
# a port that already answers is left alone. "Stop TeddyAI.bat" closes them all.
#
#   start_teddy.ps1            normal
#   start_teddy.ps1 -Sim       tick feed and teddychart on synthetic tapes (no key)
#   start_teddy.ps1 -NoChart   without teddychart (built-in chart)
#   start_teddy.ps1 -NoOpra    without the equity open-interest fetch
#   start_teddy.ps1 -NoBrowser
#   start_teddy.ps1 -Consoles    the old visible console per service (for debugging)
#   start_teddy.ps1 -NoScheduler without the in-stack job schedule

param([switch]$Sim, [switch]$NoChart, [switch]$NoFeed, [switch]$NoOpra, [switch]$NoBrowser, [switch]$Consoles, [switch]$NoScheduler)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Host.UI.RawUI.WindowTitle = 'TeddyAI - launcher'

function Say($msg, $color = 'Gray') { Write-Host ("  " + $msg) -ForegroundColor $color }
function PortOpen($port) {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', $port); $c.Close(); return $true } catch { return $false }
}
function WaitFor($port, $name, $seconds = 40) {
    $t = 0
    while (-not (PortOpen $port)) {
        Start-Sleep -Milliseconds 500; $t += 0.5
        if ($t -ge $seconds) { return $false }
    }
    return $true
}
function Launch($title, $cmd, $workdir = $Root) {
    # HIDDEN BY DEFAULT (2026-09-15, Mark: "no terminal or powershell windows coming in").
    # Each service runs without a window; its output goes to notes\services\<name>.log,
    # which the Operations list on the command center tails. -Consoles brings the old
    # visible windows back for debugging: /k keeps a window open if the script dies.
    if ($Consoles) {
        Start-Process -FilePath 'cmd.exe' -ArgumentList "/k title TeddyAI - $title && $cmd" -WorkingDirectory $workdir | Out-Null
        return
    }
    $logdir = Join-Path $Root 'notes\services'
    if (-not (Test-Path $logdir)) { New-Item -ItemType Directory $logdir | Out-Null }
    $slug = ($title -replace '[^a-z0-9]+', '-').ToLower().Trim('-')
    $parts = $cmd -split ' ', 2
    $exe = $parts[0]; $args = if ($parts.Count -gt 1) { $parts[1] } else { '' }
    if ($exe -eq 'python') { $exe = $py.Source }
    Start-Process -FilePath $exe -ArgumentList $args -WorkingDirectory $workdir -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logdir "$slug.log") -RedirectStandardError (Join-Path $logdir "$slug.err.log") | Out-Null
}

Write-Host ''
Write-Host '  TeddyAI' -ForegroundColor White -NoNewline; Write-Host '  one-click start' -ForegroundColor DarkGray
Write-Host ''

# ---- preflight ----------------------------------------------------------------
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Say 'python is not on PATH. Install Python 3 or add it to PATH.' 'Red'; exit 1 }
$hasDb = [bool]$env:DATABENTO_API_KEY
$hasAi = [bool]$env:ANTHROPIC_API_KEY
Say ("python        " + $py.Source) 'DarkGray'
Say ("Databento key " + $(if ($hasDb) { 'found' } else { 'NOT SET -- pulls and the live feed will not run; the dashboard still opens' })) $(if ($hasDb) { 'DarkGray' } else { 'Yellow' })
Say ("Anthropic key " + $(if ($hasAi) { 'found' } else { 'not set -- "Rewrite commentary" will fail until it is (see COMMANDS.txt)' })) $(if ($hasAi) { 'DarkGray' } else { 'Yellow' })
foreach ($d in 'data', 'data\bars', 'notes', 'sheets') { if (-not (Test-Path $d)) { New-Item -ItemType Directory $d | Out-Null } }
Write-Host ''

# ---- 1. dashboard -----------------------------------------------------------------
if (PortOpen 5137) { Say 'dashboard      already running on :5137' 'DarkGray' }
else {
    if ($Sim) { $env:CC_TICKS = 'sim'; Say 'dashboard      starting teddy.py on :5137 (tick feed: SIMULATED, -Sim)' 'White' }
    else { Say 'dashboard      starting teddy.py on :5137' 'White' }
    Launch 'dashboard' 'python teddy.py'
    if (-not (WaitFor 5137 'dashboard' 45)) { Say 'teddy.py did not answer on :5137 within 45 s -- look at its window' 'Red'; exit 1 }
    Say 'dashboard      up' 'Green'
}

# ---- 2. live bar feed -------------------------------------------------------------
if ($NoFeed) { Say 'live feed      skipped (-NoFeed)' 'DarkGray' }
elseif (-not $hasDb) { Say 'live feed      skipped (no DATABENTO_API_KEY); the dashboard tops the cache up itself' 'Yellow' }
else {
    $feedAlive = $false
    $fs = Join-Path $Root 'data\bars\feed_status.json'
    if (Test-Path $fs) {
        try {
            $doc = Get-Content $fs -Raw | ConvertFrom-Json
            $age = ((Get-Date).ToUniversalTime() - [datetime]::Parse($doc.updated).ToUniversalTime()).TotalSeconds
            # ...and only if that pid is still alive: a Stop followed by a Start inside 90 s
            # otherwise trusts the heartbeat of a feeder that was just killed and starts
            # nothing (2026-09-13 -- the stack came up without its feed).
            if ($age -lt 90 -and $doc.state -in 'streaming', 'connecting' -and $doc.pid -and
                (Get-Process -Id $doc.pid -ErrorAction SilentlyContinue)) { $feedAlive = $true }
        } catch { }
    }
    if ($feedAlive) { Say 'live feed      already streaming (feed_status.json is fresh)' 'DarkGray' }
    else { Say 'live feed      starting live_feed.py' 'White'; Launch 'live feed' 'python live_feed.py' }
}

# ---- 2b. tape ledger ----------------------------------------------------------------
# tape_ledger.py writes notes\tape\<session>.md as the night happens -- one line per
# event: cards triggered / stopped / spent, first touches, the opens with the sheet's
# verdict on each card, feed gaps, rebuilds -- and the next regeneration reads it, so a
# trade that already happened is written for its next visit rather than generated
# again (2026-09-15). It reads the bar files the feed writes; no key, no Databento
# calls. It rolls from one session to the next by itself, so it starts once.
$ledgerUp = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
              Where-Object { $_.CommandLine -and $_.CommandLine -match 'tape_ledger\.py' })
if ($ledgerUp.Count -gt 0) { Say 'tape ledger    already running' 'DarkGray' }
else { Say 'tape ledger    starting tape_ledger.py (writes notes\tape\<session>.md)' 'White'; Launch 'tape ledger' 'python -X utf8 tape_ledger.py' }

# ---- 2c. live levels ----------------------------------------------------------------
# levels_live.py re-solves the sheet's book at the last print and the real clock every few
# seconds and writes levels_live.json; teddychart draws those walls and flips when the file
# is fresh, so the chart's levels move with the tape between builds (2026-09-15).
$liveUp = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match 'levels_live\.py' })
if ($liveUp.Count -gt 0) { Say 'live levels    already running' 'DarkGray' }
else { Say 'live levels    starting levels_live.py (writes levels_live.json for the chart)' 'White'; Launch 'live levels' 'python -X utf8 levels_live.py' }

# ---- 3. teddychart ------------------------------------------------------------------
$chartDir = Join-Path $Root 'teddychart'
if ($NoChart) { Say 'teddychart     skipped (-NoChart)' 'DarkGray' }
elseif (-not (Test-Path (Join-Path $chartDir 'server.py'))) { Say 'teddychart     not installed (teddychart\server.py missing)' 'DarkGray' }
elseif (PortOpen 5138) { Say 'teddychart     already running on :5138' 'DarkGray' }
elseif ($Sim) { Say 'teddychart     starting on the SIMULATOR (-Sim)' 'White'; Launch 'teddychart (sim)' 'python server.py --sim' $chartDir }
elseif (-not $hasDb) { Say 'teddychart     skipped (no DATABENTO_API_KEY; -Sim shows it on a synthetic tape)' 'Yellow' }
else { Say 'teddychart     starting server.py on :5138' 'White'; Launch 'teddychart' 'python server.py' $chartDir }
if (-not $NoChart -and (Test-Path (Join-Path $chartDir 'server.py')) -and ($Sim -or $hasDb) -and -not (PortOpen 5138)) {
    if (WaitFor 5138 'teddychart' 30) { Say 'teddychart     up -- the command center puts it in the chart row' 'Green' }
    else { Say 'teddychart     not answering yet; the command center uses its built-in chart and switches when it appears' 'Yellow' }
}

# ---- 4. equity open interest, off the OPRA live gateway ----------------------------
# The morning refresh of the equity cross-reference: the last COMPLETE session's
# quotes paired with open interest replayed off the live gateway. That gateway is the
# only place this morning's OI can be read -- Databento's historical endpoint serves
# complete days only, so at this hour it would hand back the previous close and the
# Cross-Reference panel would carry its STALE stamp all day.
#
# It is a real fetch (~320 MB across the four roots, 20+ minutes with the American
# pass on QQQ/SPY), so it gets its own window and NOTHING WAITS ON IT: the page reads
# the chains whenever they land. Re-running the launcher is safe -- the job skips any
# root whose OI is already current for that settle, so a second start is four file
# reads. autopull.py --job opra-live has the reasoning; notesutopull.log has the run.
#
# OPRA disseminates open interest ONCE, at 10:30Z, and the gateway refuses a replay
# start in the future or older than 24 h -- so the window is 10:30Z..00:00Z (03:30 PT
# to 17:00 PT) and outside it there is simply nothing to replay. The job re-checks
# this itself; the point of checking here is to say so instead of opening a window
# that immediately declines.
if ($NoOpra) { Say 'equity OI      skipped (-NoOpra)' 'DarkGray' }
elseif (-not $hasDb) { Say 'equity OI      skipped (no DATABENTO_API_KEY)' 'Yellow' }
else {
    $busy = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
              Where-Object { $_.CommandLine -and $_.CommandLine -match 'opra_pull|opra-live' })
    $utc = (Get-Date).ToUniversalTime()
    $utcH = $utc.Hour + $utc.Minute / 60.0
    if ($busy.Count -gt 0) {
        Say 'equity OI      already fetching (an opra pull is in flight)' 'DarkGray'
    } elseif ($utcH -lt 10.5) {
        Say ('equity OI      skipped: ' + $utc.ToString('HH:mm') + 'Z is before the 10:30Z open-interest wave') 'DarkGray'
    } else {
        Say 'equity OI      starting autopull.py --job opra-live (its own window; minutes, not seconds)' 'White'
        Launch 'equity OI' 'python autopull.py --job opra-live'
    }
}


# ---- 4b. the schedule ---------------------------------------------------------------
# scheduler.py runs the job table (premarket, close, settle, opra, asia, the prelim poll,
# the fallback prelim, tomorrow) inside the stack, weekdays PT, hidden, one instance per
# job with a time limit. Nothing is registered in Windows any more; when the stack is
# down nothing runs. -NoScheduler leaves it off. (2026-09-15)
if ($NoScheduler) { Say 'schedule       skipped (-NoScheduler)' 'DarkGray' }
else {
    $schedUp = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
                 Where-Object { $_.CommandLine -and $_.CommandLine -match 'scheduler\.py' })
    if ($schedUp.Count -gt 0) { Say 'schedule       already running' 'DarkGray' }
    else { Say 'schedule       starting scheduler.py (the job table, weekdays PT)' 'White'; Launch 'scheduler' 'python -X utf8 scheduler.py' }
}

# ---- 5. browser --------------------------------------------------------------------
Write-Host ''
if ($NoBrowser) { Say 'browser        skipped (-NoBrowser). Open http://127.0.0.1:5137/cc' 'DarkGray' }
else {
    Say 'opening        http://127.0.0.1:5137/cc  (the page runs the pipeline on open)' 'White'
    Start-Process 'http://127.0.0.1:5137/cc'
}
Write-Host ''
Say $(if ($Consoles) { 'Done. Each service has its own window; "Stop TeddyAI.bat" closes them all.' } else { 'Done. The services run hidden; their logs are in notes\services, the Operations list on the page shows them, and "Stop TeddyAI.bat" closes them all.' }) 'DarkGray'
Start-Sleep -Seconds 3
exit 0
