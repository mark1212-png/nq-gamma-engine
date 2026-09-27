# stop_teddy.ps1 -- stop EVERYTHING TeddyAI runs, by what it runs, with its children.
#
# Mark, 2026-09-15: "make sure the stop teddy stops everything too so i dont crowd my
# network traffic". The services (dashboard, live feed, tape ledger, live levels, scheduler,
# teddychart) and any job in flight (a pull, a gameplan run, an equity fetch, the Databento
# pull scripts) -- and each one's PROCESS TREE, because a scheduled gameplan spawns the
# Claude Code CLI and a pull spawns databento, and Stop-Process on the parent alone leaves
# those children streaming. taskkill /T /F takes the tree. Then any console the -Consoles
# launcher opened. Nothing is written on the way down.
$patterns = @('teddy\.py', 'live_feed\.py', 'tape_ledger\.py', 'levels_live\.py', 'scheduler\.py',
              'autopull\.py', 'gameplan_bot\.py', 'databento_pull\.py', 'opra_pull\.py', 'opra_compare\.py',
              'cli_commentary\.py', 'discord_post\.py', 'teddy_app\.py')
$n = 0
foreach ($p in Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'py.exe'") {
    $c = [string]$p.CommandLine
    $ours = $false
    foreach ($pat in $patterns) { if ($c -match $pat) { $ours = $true; break } }
    if (-not $ours -and $c -match 'server\.py' -and $c -notmatch '--probe') {
        # server.py is teddychart's; make sure it is ours and not some other project's
        if ($c -match 'teddychart') { $ours = $true }
        else {
            try { $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($p.ParentProcessId)"; if ($parent.CommandLine -match 'TeddyAI') { $ours = $true } } catch { }
            if (-not $ours) { try { if ($p.ExecutablePath -and (Get-Item $p.ExecutablePath).Directory -and $c -match '^\S*python(w)?(\.exe)?"?\s+server\.py') { $ours = $true } } catch { } }
        }
    }
    if (-not $ours) { continue }
    Write-Host ("  stopping  " + ($c -replace '^.*?python(w)?(\.exe)?"?\s+', '') + "  (tree of pid $($p.ProcessId))")
    & taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null
    $n++
}
# a CLI round left behind by a gameplan that was already gone
foreach ($p in Get-CimInstance Win32_Process -Filter "Name = 'claude.exe' OR Name = 'node.exe'") {
    $c = [string]$p.CommandLine
    if ($c -match 'claude' -and ($c -match ' -p ' -or $c -match '--print' -or $c -match '--output-format')) {
        Write-Host ("  stopping  CLI round pid $($p.ProcessId)")
        & taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null
        $n++
    }
}
# and the console windows the -Consoles launcher opened for them
Get-Process cmd -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -like 'TeddyAI - *' } | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
Write-Host ("  " + $n + " process tree(s) stopped")
