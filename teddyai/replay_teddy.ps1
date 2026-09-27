# replay_teddy.ps1 -- run a recorded day through the whole stack at chosen speed.
#
#   replay_teddy.ps1 [-Day 2026-09-04] [-From 06:20] [-Speed 5] [-Kind auto|rth|rthprelim|intraday-0658]
#
# What it does, in order:
#   1. stops a running dashboard / teddychart (their windows), so nothing is mid-flight
#   2. backs up today's trinity.json / trinity.html / ladder_frag.html / chain_live.json to notes\replay-backup\
#   3. puts the replayed day's archived sheet in their place (archive\<day>\<kind>-trinity.json
#      and <kind>-sheet.html) and regenerates the ladder fragment from it; puts that day's
#      chain_live.json in place too (archive\<day>\<kind>-chain_live.json, or rebuilt from
#      data\<settle>-chain.json with the sheet's own arguments when the day was archived before
#      archive.py kept it) -- otherwise the live NQ re-price, the Gamma Ladders and the QQQ
#      factor run on TODAY's book against the replayed tape
#   4. starts teddy.py with CC_TICKS=replay:<day>:<from>:<speed> and CC_REPLAY=<day>
#      (the page will NOT run the pipeline or rebuild -- the sheet stays that day's)
#   5. starts teddychart with --replay <day> --from <from> --speed <speed>
#   6. opens /cc
#   7. waits for Enter, then stops both and restores today's files
# Needs teddychart\cache\trades-<day>.npz. If it is missing, teddychart pulls it on the first
# --replay of that day (a day of NQ trades is tens of MB; the log shows the estimate first).
param([string]$Day = '2026-09-04', [string]$From = '06:20', [double]$Speed = 5, [string]$Kind = 'auto')
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Host.UI.RawUI.WindowTitle = "TeddyAI - replay $Day"
function Say($m, $c = 'Gray') { Write-Host ("  " + $m) -ForegroundColor $c }
function PortOpen($port) { try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', $port); $c.Close(); $true } catch { $false } }
function Launch($title, $cmd, $wd = $Root) { Start-Process -FilePath 'cmd.exe' -ArgumentList "/k title TeddyAI - $title && $cmd" -WorkingDirectory $wd | Out-Null }

Write-Host ''; Write-Host "  TeddyAI  replay $Day from $From PT at ${Speed}x" -ForegroundColor White; Write-Host ''
$arch = Join-Path $Root "archive\$Day"
if ($Kind -eq 'auto') {
    # the RTH plan that was current at the open: the newest rth / rthprelim sheet archived for that day
    $cand = Get-ChildItem $arch -Filter '*-sheet.html' -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^(rthprelim|rth)-sheet\.html$' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $cand) { Say "no rth sheet archived for $Day" 'Red'; exit 1 }
    $Kind = $cand.Name -replace '-sheet\.html$', ''
    Say "sheet          using the newest RTH plan archived for $Day ($Kind, built $($cand.LastWriteTime.ToString('HH:mm')))" 'DarkGray'
}
$tj = Join-Path $arch "$Kind-trinity.json"; $th = Join-Path $arch "$Kind-sheet.html"
if (-not (Test-Path $tj) -or -not (Test-Path $th)) { Say "no archived $Kind sheet for $Day in archive\$Day (need $Kind-trinity.json and $Kind-sheet.html)" 'Red'; exit 1 }
$tape = Join-Path $Root "teddychart\cache\trades-$Day.npz"
if (-not (Test-Path $tape)) { Say "no teddychart\cache\trades-$Day.npz yet -- teddychart will pull it on this first replay (see its window); the command center's tracker joins once the file exists (restart the replay then)" 'Yellow' }

# 1. stop what is running
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'stop_teddy.ps1') | Out-Null
Start-Sleep -Seconds 1

# 2-3. swap the sheet
$bk = Join-Path $Root 'notes\replay-backup'; New-Item -ItemType Directory -Force $bk | Out-Null
foreach ($f in 'trinity.json', 'trinity.html', 'ladder_frag.html', 'chain_live.json') { if (Test-Path $f) { Copy-Item $f (Join-Path $bk $f) -Force } }
Copy-Item $tj 'trinity.json' -Force; Copy-Item $th 'trinity.html' -Force
& python gen.py | Out-Null
Say "sheet          $Day $Kind plan in place (today's backed up to notes\replay-backup)" 'DarkGray'
# the day's own chain_live.json: archived beside the sheet from 2026-09-07 on; before that, rebuilt from the chain file
$cl = Join-Path $arch "$Kind-chain_live.json"
if (Test-Path $cl) {
    Copy-Item $cl 'chain_live.json' -Force
    Say "chain_live     $Day $Kind book in place (archived)" 'DarkGray'
} else {
    $T = Get-Content $tj -Raw -Encoding UTF8 | ConvertFrom-Json
    $chain = Join-Path $Root "data\$($T.settle)-chain.json"
    if ($T.settle -and (Test-Path $chain)) {
        $tdb = @('trinity_db.py', '--chain', $chain, '--report-date', $T.report_date, '--out', (Join-Path $bk 'replay-trinity.json'))
        if ($T.spot) { $tdb += @('--spot', $T.spot) }
        if ($T.asof) { $tdb += @('--asof', $T.asof) } elseif ($T.hours) { $tdb += @('--hours', $T.hours) }
        if ($T.ref_symbol) { $tdb += @('--ref', $T.ref_symbol) }
        if ($T.rail_scale) { $tdb += @('--rail-scale', $T.rail_scale) }
        if ($T.rail_label) { $tdb += @('--rail-label', $T.rail_label) }
        if ($T.last_print) { $tdb += @('--last', $T.last_print) }
        if ($T.last_at) { $tdb += @('--last-at', $T.last_at) }
        if ($T.oi_vintage) { $tdb += @('--oi-vintage', $T.oi_vintage) }
        & python @tdb | Out-Null
        $built = Join-Path $bk 'chain_live.json'
        if (Test-Path $built) {
            Copy-Item $built 'chain_live.json' -Force
            Say "chain_live     rebuilt for $Day from data\$($T.settle)-chain.json with the sheet's arguments (the chain file as it is today, $($T.oi_vintage))" 'DarkGray'
        } else { Say "chain_live     rebuild failed -- today's book stays in place; the NQ column and QQQ factor are NOT the replayed day's" 'Yellow' }
    } else { Say "chain_live     no data\<settle>-chain.json for this sheet -- today's book stays in place; the NQ column and QQQ factor are NOT the replayed day's" 'Yellow' }
}

# 4. dashboard in replay mode
$env:CC_TICKS = "replay:${Day}:${From}:${Speed}"; $env:CC_REPLAY = $Day
Launch 'dashboard (replay)' 'python teddy.py'
$t = 0; while (-not (PortOpen 5137) -and $t -lt 45) { Start-Sleep -Milliseconds 500; $t += 0.5 }
if (-not (PortOpen 5137)) { Say 'teddy.py did not start -- see its window' 'Red' } else { Say 'dashboard      up, tick feed replaying' 'Green' }

# 5. teddychart replay
$chartDir = Join-Path $Root 'teddychart'
if (Test-Path (Join-Path $chartDir 'server.py')) {
    Launch "teddychart (replay $Day)" "python server.py --replay $Day --from $From --speed $Speed" $chartDir
    $t = 0; while (-not (PortOpen 5138) -and $t -lt 30) { Start-Sleep -Milliseconds 500; $t += 0.5 }
    Say ("teddychart     " + $(if (PortOpen 5138) { 'up, replaying' } else { 'not answering yet (pulling the day? see its window)' })) $(if (PortOpen 5138) { 'Green' } else { 'Yellow' })
}

# 6. browser
Start-Sleep -Seconds 1
Start-Process 'http://127.0.0.1:5137/cc'
Write-Host ''
Say 'Replay running. Space / [ ] in teddychart pause and change its speed; the command center follows its own clock.' 'DarkGray'
Say "Press Enter here to END the replay and restore today's sheet." 'White'
[void](Read-Host)

# 7. stop and restore
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'stop_teddy.ps1') | Out-Null
foreach ($f in 'trinity.json', 'trinity.html', 'ladder_frag.html', 'chain_live.json') { $b = Join-Path $bk $f; if (Test-Path $b) { Copy-Item $b $f -Force } }
Remove-Item Env:CC_TICKS -ErrorAction SilentlyContinue; Remove-Item Env:CC_REPLAY -ErrorAction SilentlyContinue
Say "today's sheet restored. Start TeddyAI.bat brings the live stack back." 'Green'
Start-Sleep -Seconds 2
