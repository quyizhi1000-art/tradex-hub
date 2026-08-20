[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$ServicePort = 8000,

    [switch]$Worker
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ControlScript = Join-Path $PSScriptRoot "tradex_control.ps1"
$RuntimeDir = Join-Path $ProjectRoot ".tradex-run"

function Test-ServicePort {
    param([int]$Port)

    $client = [Net.Sockets.TcpClient]::new()
    try {
        $connectTask = $client.ConnectAsync("127.0.0.1", $Port)
        if (-not $connectTask.Wait(250)) {
            return $false
        }
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Get-BootstrapMutexName {
    $bytes = [Text.Encoding]::UTF8.GetBytes($ProjectRoot.ToLowerInvariant())
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = [BitConverter]::ToString($hasher.ComputeHash($bytes)).Replace("-", "")
    }
    finally {
        $hasher.Dispose()
    }
    return "Local\TradexSessionBootstrap_$hash"
}

if ($Worker) {
    $bootstrapMutex = [Threading.Mutex]::new($false, (Get-BootstrapMutexName))
    $bootstrapMutexAcquired = $false
    $workerExitCode = 0
    try {
        try {
            $bootstrapMutexAcquired = $bootstrapMutex.WaitOne(0)
        }
        catch [Threading.AbandonedMutexException] {
            $bootstrapMutexAcquired = $true
        }

        if (-not $bootstrapMutexAcquired -or (Test-ServicePort $ServicePort)) {
            exit 0
        }

        & $ControlScript start-service -ServicePort $ServicePort
        $workerExitCode = $LASTEXITCODE
    }
    catch {
        Write-Warning ("Tradex MCP background startup failed: " + $_.Exception.Message)
        $workerExitCode = 1
    }
    finally {
        if ($bootstrapMutexAcquired) {
            $bootstrapMutex.ReleaseMutex()
        }
        $bootstrapMutex.Dispose()
    }
    exit $workerExitCode
}

try {
    if (Test-ServicePort $ServicePort) {
        exit 0
    }

    if (-not (Test-Path -LiteralPath $ControlScript -PathType Leaf)) {
        throw "Missing Tradex control script: $ControlScript"
    }
    if (-not (Test-Path -LiteralPath $RuntimeDir -PathType Container)) {
        New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    }

    $stdoutPath = Join-Path $RuntimeDir "session-start.out.log"
    $stderrPath = Join-Path $RuntimeDir "session-start.err.log"
    $pwshPath = (Get-Command pwsh -ErrorAction Stop).Source
    $workerArguments = @(
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-File", ('"{0}"' -f $PSCommandPath),
        "-ServicePort", [string]$ServicePort,
        "-Worker"
    )

    Start-Process `
        -FilePath $pwshPath `
        -ArgumentList $workerArguments `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -WindowStyle Hidden | Out-Null

    # A normal cold start is about three to four seconds. Wait briefly so the
    # same new conversation sees every Tradex tool, but never inherit the
    # control script's 45/60-second blocking budget.
    $readyDeadline = [DateTime]::UtcNow.AddSeconds(5)
    while ([DateTime]::UtcNow -lt $readyDeadline) {
        if (Test-ServicePort $ServicePort) {
            break
        }
        Start-Sleep -Milliseconds 100
    }
}
catch {
    # The MCP server is optional. Never block or abort conversation creation;
    # the detached worker log preserves the startup error for diagnosis.
    Write-Warning ("Tradex MCP startup was deferred: " + $_.Exception.Message)
}

exit 0
