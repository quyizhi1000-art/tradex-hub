param([switch]$HealthOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = 'G:\money'
$taskRoot = Join-Path $projectRoot 'work\smart_sector_library_20260918'
$runRoot = Join-Path $taskRoot 'scheduled'
New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
Set-Location -LiteralPath $projectRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:CODEX_HOME = 'F:\CodexHome'
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Read-Progress([string]$FilePath) {
    if (-not (Test-Path -LiteralPath $FilePath)) { return @{ status = 'missing' } }
    $data = Get-Content -LiteralPath $FilePath -Raw -Encoding utf8 | ConvertFrom-Json
    return $data | Select-Object status,total,processed,read,collected,unavailable,failed,last,updated_at
}
function Write-AtomicJson([string]$FilePath, $Value) {
    $temporary = $FilePath + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $FilePath -Force
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$processes = @(Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -in @('node.exe','python.exe')) -and ($_.CommandLine -match 'collect-ths\.cjs|smart_sector_library\.audit')
} | Select-Object ProcessId,Name,CommandLine)
$health = [ordered]@{
    checked_at = (Get-Date).ToString('o')
    ths = Read-Progress (Join-Path $taskRoot 'ths-progress.json')
    business = Read-Progress (Join-Path $taskRoot 'full_market\progress.json')
    processes = $processes
}
Write-AtomicJson (Join-Path $runRoot 'heartbeat.json') $health
if ($HealthOnly) { $health | ConvertTo-Json -Depth 6; exit 0 }

# Each ten-minute tick checks health; only one analysis may run at a time.
$mutex = [System.Threading.Mutex]::new($false, 'Local\TradexSmartSectorReview')
$ownsMutex = $false
try {
    try { $ownsMutex = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { exit 0 }
    $logPath = Join-Path $runRoot ($stamp + '.log')
    $resultPath = Join-Path $runRoot ($stamp + '-result.md')
    Write-AtomicJson (Join-Path $runRoot 'analysis-status.json') @{ started_at = (Get-Date).ToString('o'); status = 'running'; result = $resultPath; log = $logPath }
    $promptText = Get-Content -LiteralPath (Join-Path $taskRoot 'scheduled-review-prompt.md') -Raw -Encoding utf8
    $promptText | & 'C:\Users\Admin\AppData\Roaming\npm\codex.cmd' exec --ephemeral --sandbox workspace-write -C $projectRoot --json -o $resultPath - *> $logPath
    $resultCode = $LASTEXITCODE
    Write-AtomicJson (Join-Path $runRoot 'analysis-status.json') @{ finished_at = (Get-Date).ToString('o'); status = $(if ($resultCode -eq 0) {'completed'} else {'failed'}); exit_code = $resultCode; result = $resultPath; log = $logPath }
    exit $resultCode
} catch {
    Write-AtomicJson (Join-Path $runRoot 'analysis-status.json') @{ finished_at = (Get-Date).ToString('o'); status = 'failed'; error = $_.Exception.Message }
    throw
} finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
