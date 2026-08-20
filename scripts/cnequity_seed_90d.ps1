[CmdletBinding()]
param(
    [string]$CneRepo = 'G:\CNEquity',
    [string]$ConfigPath = 'G:\CNEquity\configs\cnequity.toml',
    [string]$DataRoot = 'G:\CNEquity\data\cnequity',
    [datetime]$TradeDate = (Get-Date),
    [ValidateRange(1, 3650)]
    [int]$CalendarDays = 90,
    [string]$ResumeRunId = '',
    [int]$WaitForProcessId = 0,
    [ValidateRange(1, 5)]
    [int]$NetworkAttempts = 3,
    [string]$LogPath = '',
    [string]$StatePath = ''
)

$ErrorActionPreference = 'Stop'
$resolvedRepo = (Resolve-Path -LiteralPath $CneRepo).Path
$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$cneExe = Join-Path $resolvedRepo '.venv\Scripts\cne.exe'
$pythonExe = Join-Path $resolvedRepo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $cneExe -PathType Leaf)) {
    throw "CNEquity CLI not found at $cneExe"
}
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "CNEquity Python not found at $pythonExe"
}

$resolvedExe = (Resolve-Path -LiteralPath $cneExe).Path
$resolvedPython = (Resolve-Path -LiteralPath $pythonExe).Path
$endDate = $TradeDate.Date
$startDate = $endDate.AddDays(-($CalendarDays - 1))
$startText = $startDate.ToString('yyyy-MM-dd')
$endText = $endDate.ToString('yyyy-MM-dd')
$breadthLookbackStartText = $startDate.AddDays(-7).ToString('yyyy-MM-dd')
$breadthLookbackEndText = $startDate.AddDays(-1).ToString('yyyy-MM-dd')
$automationRoot = Join-Path $DataRoot 'meta\automation'
$logRoot = Join-Path $DataRoot 'meta\logs'
New-Item -ItemType Directory -Force -Path $automationRoot, $logRoot | Out-Null
if (-not $LogPath) {
    $LogPath = Join-Path $logRoot "seed_90d_${startText}_${endText}.log"
}
if (-not $StatePath) {
    $StatePath = Join-Path $automationRoot 'seed_90d_status.json'
}

$mutex = [System.Threading.Mutex]::new($false, 'Local\CNEquitySeed90d')
if (-not $mutex.WaitOne(0)) {
    throw 'Another CNEquity 90-day seed controller is already running.'
}

function Save-State {
    param(
        [string]$Status,
        [string]$Step,
        [string]$Message = ''
    )
    $payload = [ordered]@{
        status = $Status
        step = $Step
        message = $Message
        start_date = $startText
        end_date = $endText
        resume_run_id = $ResumeRunId
        controller_pid = $PID
        updated_at = [DateTimeOffset]::Now.ToString('o')
        log_path = $LogPath
    }
    $tmpPath = "$StatePath.tmp"
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $tmpPath -Encoding UTF8
    Move-Item -LiteralPath $tmpPath -Destination $StatePath -Force
}

function Get-RunStatus {
    param([string]$RunId)
    if (-not $RunId) { return '' }
    $manifestPath = Join-Path $DataRoot 'meta\manifest.db'
    # Windows PowerShell 5.1 strips embedded double quotes when forwarding a
    # variable to a native executable.  Keep the Python program free of double
    # quotes so ``python -c`` receives one intact argument.
    $code = "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); r=c.execute('select status from ingestion_runs where run_id=?',(sys.argv[2],)).fetchone(); print(r[0] if r else 'missing')"
    $value = & $resolvedPython -c $code $manifestPath $RunId
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect manifest status for run $RunId"
    }
    return "$value".Trim()
}

function Invoke-CneStep {
    param(
        [string]$Name,
        [string[]]$CneArguments,
        [int]$Attempts = 1
    )
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        Save-State -Status 'running' -Step $Name -Message "attempt $attempt/$Attempts"
        Write-Host "[$([DateTimeOffset]::Now.ToString('o'))] START $Name attempt $attempt/$Attempts"
        & $resolvedExe @CneArguments
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0) {
            Write-Host "[$([DateTimeOffset]::Now.ToString('o'))] DONE  $Name"
            return
        }
        Write-Warning "$Name failed with exit code $exitCode (attempt $attempt/$Attempts)"
        if ($attempt -lt $Attempts) {
            Start-Sleep -Seconds ([Math]::Min(60, 10 * $attempt))
        }
    }
    throw "$Name failed after $Attempts attempt(s)"
}

function Invoke-CneAdvisory {
    param(
        [string]$Name,
        [string[]]$CneArguments
    )
    Save-State -Status 'running' -Step $Name -Message 'advisory check'
    Write-Host "[$([DateTimeOffset]::Now.ToString('o'))] START $Name (advisory)"
    & $resolvedExe @CneArguments
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "$Name reported findings (exit $LASTEXITCODE); core dataset verification continues."
    }
    else {
        Write-Host "[$([DateTimeOffset]::Now.ToString('o'))] DONE  $Name"
    }
}

$transcriptStarted = $false
Push-Location $resolvedRepo
try {
    Start-Transcript -LiteralPath $LogPath -Append | Out-Null
    $transcriptStarted = $true
    Save-State -Status 'running' -Step 'startup'

    if ($WaitForProcessId -gt 0) {
        $existing = Get-Process -Id $WaitForProcessId -ErrorAction SilentlyContinue
        if ($null -ne $existing) {
            Save-State -Status 'waiting' -Step 'existing_process' -Message "waiting for PID $WaitForProcessId"
            Write-Host "Waiting for existing CNEquity process PID $WaitForProcessId"
            Wait-Process -Id $WaitForProcessId
        }
    }

    Invoke-CneStep -Name 'config_validate' -CneArguments @('config', 'validate', '--config', $resolvedConfig)
    Invoke-CneStep -Name 'doctor' -CneArguments @('doctor', '--config', $resolvedConfig)
    Invoke-CneStep -Name 'server_probe' -Attempts $NetworkAttempts -CneArguments @('servers', 'test', '--config', $resolvedConfig)

    # The ownership receipt must cover the exact daily-bar window, including
    # names that delisted after the lake's previous watermark.
    Invoke-CneStep -Name 'delisted_receipt' -Attempts $NetworkAttempts -CneArguments @(
        'delisted', 'backfill', '--config', $resolvedConfig, '--since', $startText, '--end', $endText
    )

    if ($ResumeRunId) {
        $runStatus = Get-RunStatus -RunId $ResumeRunId
        if ($runStatus -ne 'success') {
            Invoke-CneStep -Name 'daily_bars_resume' -Attempts $NetworkAttempts -CneArguments @(
                'retry', '--config', $resolvedConfig, '--run-id', $ResumeRunId
            )
        }
        $runStatus = Get-RunStatus -RunId $ResumeRunId
        if ($runStatus -ne 'success') {
            throw "daily_bars run $ResumeRunId ended with manifest status '$runStatus'"
        }
    }
    else {
        Invoke-CneStep -Name 'daily_bars' -Attempts $NetworkAttempts -CneArguments @(
            'backfill', 'daily_bars', '--config', $resolvedConfig, '--start', $startText, '--end', $endText
        )
    }

    Invoke-CneStep -Name 'index_bars' -Attempts $NetworkAttempts -CneArguments @(
        'backfill', 'index_bars', '--config', $resolvedConfig, '--start', $startText, '--end', $endText
    )

    # Breadth for the first requested session needs the prior session's close.
    # Seed a bounded one-week lookback so weekends/holidays are covered while
    # the published breadth window remains exactly startText..endText.
    Invoke-CneStep -Name 'breadth_daily_lookback' -Attempts $NetworkAttempts -CneArguments @(
        'backfill', 'daily_bars', '--config', $resolvedConfig,
        '--start', $breadthLookbackStartText, '--end', $breadthLookbackEndText
    )
    Invoke-CneStep -Name 'market_breadth' -CneArguments @(
        'backfill', 'market_breadth', '--config', $resolvedConfig, '--start', $startText, '--end', $endText
    )

    try {
        Invoke-CneStep -Name 'sector_bars' -CneArguments @(
            'backfill', 'sector_bars', '--config', $resolvedConfig, '--start', $startText, '--end', $endText
        )
    }
    catch {
        Invoke-CneStep -Name 'sector_bars_retry' -Attempts $NetworkAttempts -CneArguments @(
            'backfill', 'sector_bars', '--config', $resolvedConfig, '--start', $startText, '--end', $endText, '--retry-failed'
        )
    }

    Invoke-CneStep -Name 'verify_core' -CneArguments @(
        'verify', '--config', $resolvedConfig, '--dataset', 'daily_bars,index_bars,market_breadth,sector_bars'
    )
    # Whole-lake audit includes snapshot-only/history scopes outside this seed;
    # retain its findings without letting them mask the explicit core verifier.
    Invoke-CneAdvisory -Name 'audit' -CneArguments @('audit', '--config', $resolvedConfig)
    Invoke-CneStep -Name 'stats_rebuild' -CneArguments @('stats', 'rebuild', '--config', $resolvedConfig)
    Invoke-CneStep -Name 'daily_coverage' -CneArguments @(
        'query', '--config', $resolvedConfig, '--sql', 'SELECT min(trade_date) AS min_date, max(trade_date) AS max_date, count(*) AS rows, count(DISTINCT symbol) AS symbols FROM daily_bars'
    )
    Invoke-CneStep -Name 'index_coverage' -CneArguments @(
        'query', '--config', $resolvedConfig, '--sql', 'SELECT min(trade_date) AS min_date, max(trade_date) AS max_date, count(*) AS rows, count(DISTINCT symbol) AS symbols FROM index_bars'
    )
    Invoke-CneStep -Name 'breadth_coverage' -CneArguments @(
        'query', '--config', $resolvedConfig, '--sql', 'SELECT min(trade_date) AS min_date, max(trade_date) AS max_date, count(*) AS rows FROM market_breadth'
    )
    Invoke-CneStep -Name 'sector_coverage' -CneArguments @(
        'query', '--config', $resolvedConfig, '--sql', 'SELECT min(trade_date) AS min_date, max(trade_date) AS max_date, count(*) AS rows, count(DISTINCT symbol) AS symbols FROM sector_bars'
    )

    Save-State -Status 'complete' -Step 'complete' -Message 'Core 90-day lake seed and verification completed.'
    Write-Host "CNEquity baseline completed for $startText .. $endText at $DataRoot"
    Write-Host 'Scope: SH/SZ baseline; historical snapshot-only datasets and BJ discovery are not fabricated.'
}
catch {
    Save-State -Status 'failed' -Step 'failed' -Message $_.Exception.Message
    Write-Error $_
    exit 1
}
finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
    Pop-Location
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
