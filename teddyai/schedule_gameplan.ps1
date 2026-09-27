<#
.SYNOPSIS
    Registers the TeddyAI schedule in Windows Task Scheduler: the data jobs, and the game
    plans that get pulled, interpreted and posted to Discord.

.DESCRIPTION
    Two kinds of entry, under the \TeddyAI\ folder:

      DATA JOBS      python autopull.py --job <name>
                     A pull or a build with no plan attached. Nothing is posted.
                       2:10 PM  close    the day's bars (open interest is not out yet)
                       4:00 PM  settle   the official settlement bulletin
                      10:15 PM  prelim   tonight's preliminary book
                       4:10 PM  opra     the equity books for today's settle (long: ~20 min,
                                         hundreds of MB -- its own entry on purpose)

      GAME PLANS     python gameplan_bot.py --job <name>
                     Pull, verify, build, ask the model to write the plan, then post it to
                     the channel.
                       6:05 AM  premarket  the RTH plan
                       5:15 PM  asia       the overnight plan
                      10:20 PM  tomorrow   tomorrow's RTH plan, on the preliminary book

    Weekdays only, local machine time (which this project treats as PT). Every task runs as
    YOU and only while you are logged on -- that is deliberate, not a limitation: the API keys
    live in your user environment, and a task running as SYSTEM would not see them.

.PARAMETER Intraday
    Also register mid-session refreshes at 7:30, 9:30 and 11:30 AM. Each one is a Databento
    pull AND an Anthropic call, so this is off by default -- it is a recurring cost, not a
    free refresh.

.PARAMETER Commentary
    Who writes the plan. 'cli' (default) is the Claude Code CLI on your subscription and bills
    nothing; 'api' is api.anthropic.com, billed per rewrite; 'cli-then-api' prefers the CLI and
    falls back to the API when it is missing, logged out or rate limited -- which CAN bill you.

.PARAMETER NoCommentary
    Register the game plans with --no-commentary: they pull and build on schedule but never
    call the Anthropic API, so nothing is billed. The sheet is then stale by definition and
    the post is refused, which is the holding state while the commentary comes from elsewhere.

.PARAMETER NoPost
    Register the game plans with --no-post: the whole pipeline runs and writes the sheet,
    nothing reaches Discord. Use this for a week of watching the output before it is public.

.PARAMETER Remove
    Unregister everything this script registers.

.PARAMETER List
    Show what is currently registered and when it next runs.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File schedule_gameplan.ps1 -List
    powershell -ExecutionPolicy Bypass -File schedule_gameplan.ps1 -NoPost
    powershell -ExecutionPolicy Bypass -File schedule_gameplan.ps1
    powershell -ExecutionPolicy Bypass -File schedule_gameplan.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Intraday,
    [switch]$NoPost,
    [switch]$NoCommentary,
    [ValidateSet('cli','api','cli-then-api')]
    [string]$Commentary = 'cli',
    [switch]$Remove,
    [switch]$List
)

$ErrorActionPreference = 'Stop'
$Root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Folder = '\TeddyAI\'

# pythonw runs without a console window; a scheduled plan should not throw one on screen every
# morning. Both bots log to notes\ regardless, so nothing is lost by hiding the window.
$Py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $Py) { throw "no python.exe on PATH -- open a new terminal, or install Python" }

# name, time (local), script, arguments
$DataJobs = @(
    @{ Name = 'close';  At = '14:10'; Script = 'autopull.py'; Args = '--job close'  },
    @{ Name = 'settle'; At = '16:00'; Script = 'autopull.py'; Args = '--job settle' },
    @{ Name = 'opra';   At = '16:10'; Script = 'autopull.py'; Args = '--job opra'   },
    # 19:30, not 22:15. Preliminary OI was measured landing 18:34-19:16 PT across
    # five weekdays on 2026-09-14 (see job_prelim); at 22:15 this pulled a book
    # that had been sitting there for three hours, and every Asia plan was built
    # at 17:15 on the PREVIOUS session's open interest as a result.
    # THE POLL (2026-09-15, Mark: "Yes lets do a poll"). From 18:30 the job checks the
    # feed every ten minutes for the open-interest burst (measured 18:34-19:16 PT over six
    # weekdays, one burst each), pulls it the moment it is there, and rebuilds the
    # overnight plan on it through gameplan_bot (commentary + post). It replaces the
    # fixed 19:30 pull and the 19:45 refresh; a fixed time waited up to an hour for a
    # book already on the feed. Its own time limit is three hours, not the default one.
    @{ Name = 'prelim-poll'; At = '18:30'; Script = 'autopull.py'; Args = '--job prelim-poll'; Limit = 3 },
    # the fallback if the poll gave up (a night the burst never came before 21:00)
    @{ Name = 'prelim';      At = '22:15'; Script = 'autopull.py'; Args = '--job prelim' }
)
$PlanJobs = @(
    @{ Name = 'gameplan-premarket'; At = '06:05'; Script = 'gameplan_bot.py'; Args = '--job premarket' },
    # TWO overnight plans a night, on purpose (2026-09-14).
    #
    # 17:15 is the plan that covers the overnight: 2.25h past the settle, rails
    # ~0.32x, so it describes most of the session. But preliminary open interest
    # does not land until ~19:00 PT (18:34-19:16 measured over five weekdays --
    # see job_prelim), so it is necessarily built on the LAST COMPLETE book.
    #
    # 19:45 runs after the prelim job at 19:30 and rebuilds on the session's OWN
    # open interest. It covers less -- rails ~0.25x, the tail rather than the
    # bulk -- but it is the current map. On 09-14 the two differed enough to move
    # structural confidence from 0.55 to 0.85 and shift every flip by hundreds of
    # points, so neither replaces the other. The 17:15 sheet keeps the stable
    # filename; the 19:45 one lands as <session>-asia-HHMM-teddyai.html.
    @{ Name = 'gameplan-asia';         At = '17:15'; Script = 'gameplan_bot.py'; Args = '--job asia'      },
    # the 19:45 refresh is now the poll's doing (prelim-poll, above), the moment the book lands
    @{ Name = 'gameplan-tomorrow';  At = '22:20'; Script = 'gameplan_bot.py'; Args = '--job tomorrow'  }
)
$IntradayJobs = @(
    @{ Name = 'gameplan-intraday-0730'; At = '07:30'; Script = 'gameplan_bot.py'; Args = '--job intraday' },
    @{ Name = 'gameplan-intraday-0930'; At = '09:30'; Script = 'gameplan_bot.py'; Args = '--job intraday' },
    @{ Name = 'gameplan-intraday-1130'; At = '11:30'; Script = 'gameplan_bot.py'; Args = '--job intraday' }
)

$Jobs = $DataJobs + $PlanJobs
if ($Intraday) { $Jobs = $Jobs + $IntradayJobs }

function Get-TeddyTasks {
    Get-ScheduledTask -TaskPath $Folder -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------- List
if ($List) {
    $tasks = Get-TeddyTasks
    if (-not $tasks) {
        Write-Host "nothing registered under $Folder" -ForegroundColor Yellow
        exit 0
    }
    $tasks | ForEach-Object {
        $info = Get-ScheduledTaskInfo $_
        $act  = ($_.Actions | Select-Object -First 1)
        [pscustomobject]@{
            Task     = $_.TaskName
            State    = $_.State
            NextRun  = $info.NextRunTime
            LastRun  = $info.LastRunTime
            LastCode = $info.LastTaskResult
            Command  = ($act.Arguments -replace '"', '')
        }
    } | Format-Table -AutoSize
    exit 0
}

# -------------------------------------------------------------------------- Remove
if ($Remove) {
    $tasks = Get-TeddyTasks
    if (-not $tasks) {
        Write-Host "nothing to remove under $Folder" -ForegroundColor Yellow
        exit 0
    }
    foreach ($t in $tasks) {
        Unregister-ScheduledTask -TaskName $t.TaskName -TaskPath $Folder -Confirm:$false
        Write-Host "removed  $($t.TaskName)" -ForegroundColor DarkGray
    }
    Write-Host "done." -ForegroundColor Green
    exit 0
}

# ------------------------------------------------------------------------- Install
Write-Host "project : $Root"
Write-Host "python  : $Py"
Write-Host ""

# The keys the schedule needs, checked here rather than discovered at 6am in a log file.
#
# Read the USER environment, not just this process's. setx writes to the registry and does not
# touch a window that is already open, so checking $env: alone warns "not set" about a key that
# was set correctly a minute ago -- and the tasks, being new processes, would have seen it fine.
function Get-UserOrProcessEnv([string]$name) {
    $v = [Environment]::GetEnvironmentVariable($name, 'User')
    if ($v) { return $v }
    return [Environment]::GetEnvironmentVariable($name, 'Process')
}
$HasDatabento = [bool](Get-UserOrProcessEnv 'DATABENTO_API_KEY')
$HasAnthropic = [bool](Get-UserOrProcessEnv 'ANTHROPIC_API_KEY')
$NeedsApi     = ($Commentary -ne 'cli')
$HasWebhook   = [bool](Get-UserOrProcessEnv 'DISCORD_WEBHOOK_URL')
$HasBot       = [bool]((Get-UserOrProcessEnv 'DISCORD_BOT_TOKEN') -and (Get-UserOrProcessEnv 'DISCORD_CHANNEL_ID'))

if (-not $HasDatabento) {
    Write-Host "WARNING  DATABENTO_API_KEY is not set -- every pull will fail." -ForegroundColor Yellow
    Write-Host "         setx DATABENTO_API_KEY `"db-...`"  then open a NEW terminal." -ForegroundColor Yellow
}
if (-not $HasAnthropic -and -not $NoCommentary -and $NeedsApi) {
    Write-Host "WARNING  ANTHROPIC_API_KEY is not set -- nothing can write the commentary," -ForegroundColor Yellow
    Write-Host "         so no plan will ever be posted." -ForegroundColor Yellow
}
if (-not $NoPost -and -not $NoCommentary -and -not $HasWebhook -and -not $HasBot) {
    Write-Host "WARNING  no Discord target -- set DISCORD_WEBHOOK_URL (or DISCORD_BOT_TOKEN" -ForegroundColor Yellow
    Write-Host "         and DISCORD_CHANNEL_ID). The pipeline will run and the post will fail." -ForegroundColor Yellow
}

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                                        -LogonType Interactive -RunLevel Limited
foreach ($j in $Jobs) {
    $name = $j.Name
    # per-job time limit: the poll waits up to three hours; everything else gets one
    $limitH = if ($j.Limit) { $j.Limit } else { 1 }
    $settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                                              -DontStopOnIdleEnd `
                                              -MultipleInstances IgnoreNew `
                                              -ExecutionTimeLimit (New-TimeSpan -Hours $limitH)
    $jobArgs = $j.Args
    if ($NoPost -and $j.Script -eq 'gameplan_bot.py') { $jobArgs = "$jobArgs --no-post" }
    # --no-commentary skips the Anthropic call entirely: the data still pulls and the sheet
    # still builds on schedule, at zero API cost. The sheet then carries fresh numbers under
    # older words, so the stale gate refuses to post it -- which is the intended holding state
    # while the commentary is written some other way.
    if ($NoCommentary -and $j.Script -eq 'gameplan_bot.py') { $jobArgs = "$jobArgs --no-commentary" }
    elseif ($j.Script -eq 'gameplan_bot.py') { $jobArgs = "$jobArgs --commentary $Commentary" }

    $action = New-ScheduledTaskAction -Execute $Py `
                                      -Argument "`"$Root\$($j.Script)`" $jobArgs" `
                                      -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Weekly `
                                        -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
                                        -At $j.At

    # Register-ScheduledTask -Force replaces an existing entry rather than erroring, so this
    # script is safe to re-run after changing a time or adding --no-post.
    Register-ScheduledTask -TaskName $name -TaskPath $Folder `
                           -Action $action -Trigger $trigger `
                           -Principal $principal -Settings $settings `
                           -Description "TeddyAI: $($j.Script) $jobArgs" -Force | Out-Null

    $tag = if ($j.Script -eq 'gameplan_bot.py') { 'plan' } else { 'data' }
    Write-Host ("  {0,-5}  {1,-5}  {2,-24}  {3}" -f $j.At, $tag, $name, $jobArgs) -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "registered $($Jobs.Count) tasks under $Folder (weekdays, local time)." -ForegroundColor Green
if ($NoPost) {
    Write-Host "--NoPost: the plans build but nothing reaches Discord. Re-run without it when ready." -ForegroundColor Cyan
}
if ($NoCommentary) {
    Write-Host "--NoCommentary: the data pulls and the sheets build on schedule, with NO Anthropic" -ForegroundColor Cyan
    Write-Host "call and NO post. Zero API spend. Write the commentary yourself, then post with:" -ForegroundColor Cyan
    Write-Host "    python gameplan_bot.py --post-only" -ForegroundColor Cyan
}
Write-Host ""
if (-not $NoCommentary -and $Commentary -eq 'cli') {
    Write-Host "commentary: the Claude Code CLI, on your subscription. Nothing is billed." -ForegroundColor Cyan
    $cli = Get-Command claude, claude.cmd -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cli) {
        Write-Host "  the CLI is NOT installed yet, so the plans will build and then stop at the" -ForegroundColor Yellow
        Write-Host "  commentary step -- nothing billed, nothing posted. To finish the setup:" -ForegroundColor Yellow
        Write-Host "      npm install -g @anthropic-ai/claude-code" -ForegroundColor Yellow
        Write-Host "      claude login" -ForegroundColor Yellow
        Write-Host "      python cli_commentary.py --check" -ForegroundColor Yellow
    } else {
        Write-Host ("  using " + $cli.Source) -ForegroundColor DarkGray
    }
    Write-Host ""
}
Write-Host "check it:   powershell -File schedule_gameplan.ps1 -List"
Write-Host "run one:    Start-ScheduledTask -TaskPath '$Folder' -TaskName 'gameplan-premarket'"
Write-Host "the log:    notes\gameplan.log  ·  notes\autopull.log  ·  notes\discord.log"
