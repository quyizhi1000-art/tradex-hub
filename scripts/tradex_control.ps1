[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet(
        "start-dashboard",
        "stop-dashboard",
        "start-service",
        "stop-service",
        "start-all",
        "stop-all",
        "status",
        "open-dashboard"
    )]
    [string]$Action = "status",

    [ValidateRange(1, 65535)]
    [int]$DashboardPort = 8765,

    [ValidateRange(1, 65535)]
    [int]$ServicePort = 8000,

    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$RuntimeDir = Join-Path $ProjectRoot ".tradex-run"

# These variables only affect child processes launched by this script.
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONUTF8 = "1"
# The optional WebSocket server also defaults to port 8765. Keep it disabled
# for these two-process launchers so it cannot collide with the dashboard.
$env:WS_SERVER_ENABLED = "false"

function Get-ComponentLabel {
    param([string]$Name)
    if ($Name -eq "dashboard") { return "网页看板" }
    return "MCP 服务"
}

function Get-StatePath {
    param([string]$Name)
    return Join-Path $RuntimeDir ("{0}.json" -f $Name)
}

function Read-ComponentState {
    param([string]$Name)
    $path = Get-StatePath $Name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        Write-Host "[警告] 运行状态文件损坏或不可读，按未受管实例处理：$path" -ForegroundColor Yellow
        return $null
    }
}

function Write-ComponentState {
    param([string]$Name, [object]$State)
    if (-not (Test-Path -LiteralPath $RuntimeDir -PathType Container)) {
        New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    }
    $path = Get-StatePath $Name
    $temporaryPath = "{0}.{1}.tmp" -f $path, [Guid]::NewGuid().ToString("N")
    try {
        $json = $State | ConvertTo-Json
        [IO.File]::WriteAllText($temporaryPath, $json, [Text.UTF8Encoding]::new($true))
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            [IO.File]::Replace($temporaryPath, $path, $null)
        }
        else {
            [IO.File]::Move($temporaryPath, $path)
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}

function Remove-ComponentState {
    param([string]$Name)
    $path = Get-StatePath $Name
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        Remove-Item -LiteralPath $path -Force
    }
}

function Get-PortOwner {
    param([int]$Port)
    $listeners = @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -in @("127.0.0.1", "0.0.0.0", "::1", "::") }
    )
    if ($listeners.Count -eq 0) { return $null }
    return [int]$listeners[0].OwningProcess
}

function Get-ProcessPattern {
    param([string]$Name)
    if ($Name -eq "dashboard") {
        return "(?i)(^|\s)-m\s+tradex\.dashboard(\s|$)"
    }
    return "(?i)(^|\s)-m\s+tradex(\s|$)"
}

function Test-ExpectedProcess {
    param(
        [string]$Name,
        [int]$ProcessId,
        [AllowNull()][object]$ExpectedStartUtc
    )

    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $processInfo) { return $false }
    if ([string]::IsNullOrWhiteSpace([string]$processInfo.CommandLine)) { return $false }
    if ([string]$processInfo.CommandLine -notmatch (Get-ProcessPattern $Name)) { return $false }
    if ($Name -eq "service" -and [string]$processInfo.CommandLine -notmatch "(?i)(^|\s)--(?:http|streamable-http)(\s|$)") {
        return $false
    }

    if (-not [string]::IsNullOrWhiteSpace($ExpectedStartUtc)) {
        try {
            $actual = (Get-Process -Id $ProcessId -ErrorAction Stop).StartTime.ToUniversalTime()
            if ($ExpectedStartUtc -is [DateTimeOffset]) {
                $expected = $ExpectedStartUtc.UtcDateTime
            }
            elseif ($ExpectedStartUtc -is [DateTime]) {
                $expected = $ExpectedStartUtc.ToUniversalTime()
            }
            else {
                $expected = [DateTimeOffset]::Parse(
                    [string]$ExpectedStartUtc,
                    [Globalization.CultureInfo]::InvariantCulture,
                    [Globalization.DateTimeStyles]::RoundtripKind
                ).UtcDateTime
            }
            if ([Math]::Abs(($actual - $expected).TotalSeconds) -gt 3) { return $false }
        }
        catch {
            return $false
        }
    }
    return $true
}

function Get-OptionalStateValue {
    param([object]$State, [string]$PropertyName)
    if ($null -eq $State) { return $null }
    $property = $State.PSObject.Properties[$PropertyName]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Test-IsDescendantProcess {
    param([int]$ChildProcessId, [int]$AncestorProcessId)
    if ($ChildProcessId -eq $AncestorProcessId) { return $true }

    $currentId = $ChildProcessId
    $visited = @{}
    while ($currentId -gt 0) {
        if ($visited.ContainsKey($currentId)) { return $false }
        $visited[$currentId] = $true
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$currentId" -ErrorAction SilentlyContinue
        if ($null -eq $processInfo) { return $false }
        $parentId = [int]$processInfo.ParentProcessId
        if ($parentId -eq $AncestorProcessId) { return $true }
        $currentId = $parentId
    }
    return $false
}

function Get-DescendantProcessIds {
    param([int]$RootProcessId)
    $allProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $frontier = @($RootProcessId)
    $descendants = @()

    while ($frontier.Count -gt 0) {
        $next = @()
        foreach ($processInfo in $allProcesses) {
            if ($frontier -contains [int]$processInfo.ParentProcessId) {
                $candidateId = [int]$processInfo.ProcessId
                if ($descendants -notcontains $candidateId) {
                    $descendants += $candidateId
                    $next += $candidateId
                }
            }
        }
        $frontier = $next
    }
    return @($descendants)
}

function Stop-ExpectedProcessTree {
    param(
        [string]$Name,
        [int]$RootProcessId,
        [AllowNull()][object]$ExpectedStartUtc
    )
    if (-not (Test-ExpectedProcess $Name $RootProcessId $ExpectedStartUtc)) {
        return $false
    }

    $descendants = @(Get-DescendantProcessIds $RootProcessId)
    [Array]::Reverse($descendants)
    foreach ($descendantId in $descendants) {
        Stop-Process -Id $descendantId -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $RootProcessId -Force -ErrorAction SilentlyContinue
    return $true
}

function Test-ComponentState {
    param([string]$Name, [object]$State)
    if ($null -eq $State) { return $false }
    $owner = Get-PortOwner ([int]$State.port)
    if ($null -eq $owner -or $owner -ne [int]$State.process_pid) { return $false }
    return Test-ExpectedProcess $Name ([int]$State.process_pid) $State.process_start_utc
}

function Get-ProjectLauncherProcess {
    param(
        [string]$Name,
        [int]$ProcessId
    )

    $currentId = $ProcessId
    $visited = @{}
    while ($currentId -gt 0) {
        if ($visited.ContainsKey($currentId)) { return $null }
        $visited[$currentId] = $true

        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$currentId" -ErrorAction SilentlyContinue
        if ($null -eq $processInfo) { return $null }

        $executablePath = [string]$processInfo.ExecutablePath
        if (-not [string]::IsNullOrWhiteSpace($executablePath)) {
            try {
                $isProjectPython = [string]::Equals(
                    [IO.Path]::GetFullPath($executablePath),
                    [IO.Path]::GetFullPath($PythonPath),
                    [StringComparison]::OrdinalIgnoreCase
                )
            }
            catch {
                $isProjectPython = $false
            }
            if ($isProjectPython -and (Test-ExpectedProcess $Name $currentId $null)) {
                return $processInfo
            }
        }

        $currentId = [int]$processInfo.ParentProcessId
    }
    return $null
}

function Test-DashboardHealth {
    param([int]$Port)

    try {
        $expectedHtmlPath = Join-Path $ProjectRoot "tradex\src\tradex\dashboard\watch\index.html"
        if (-not (Test-Path -LiteralPath $expectedHtmlPath -PathType Leaf)) { return $false }
        $expectedHtml = [IO.File]::ReadAllText($expectedHtmlPath, [Text.Encoding]::UTF8)
        $response = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri ("http://127.0.0.1:{0}/" -f $Port) `
            -TimeoutSec 5
        $contentType = [string]$response.Headers["Content-Type"]
        return (
            [int]$response.StatusCode -eq 200 -and
            $contentType -match "(?i)^text/html(?:;|$)" -and
            [string]$response.Content -match "(?is)<title>[^<]*Tradex[^<]*</title>" -and
            [string]$response.Content -ceq $expectedHtml
        )
    }
    catch {
        return $false
    }
}

function Test-CompatibleDashboardProcess {
    param(
        [int]$Port,
        [int]$ProcessId
    )

    # A matching module name alone is not sufficient: another checkout can run
    # the same module. Require the listener to descend from this project's exact
    # virtual-environment executable and verify the live Tradex HTML response.
    if (-not (Test-ExpectedProcess "dashboard" $ProcessId $null)) { return $false }
    $launcherInfo = Get-ProjectLauncherProcess "dashboard" $ProcessId
    if ($null -eq $launcherInfo) { return $false }
    try {
        $processStartUtc = (Get-Process -Id $ProcessId -ErrorAction Stop).StartTime.ToUniversalTime()
        $launcherStartUtc = (
            Get-Process -Id ([int]$launcherInfo.ProcessId) -ErrorAction Stop
        ).StartTime.ToUniversalTime()
    }
    catch {
        return $false
    }
    if (-not (Test-DashboardHealth $Port)) { return $false }

    # Recheck identity after HTTP I/O so a listener swap cannot be mistaken for
    # the process that was inspected before the request.
    $confirmedOwner = Get-PortOwner $Port
    return (
        $null -ne $confirmedOwner -and
        [int]$confirmedOwner -eq $ProcessId -and
        (Test-ExpectedProcess "dashboard" $ProcessId $processStartUtc) -and
        (Test-ExpectedProcess "dashboard" ([int]$launcherInfo.ProcessId) $launcherStartUtc)
    )
}

function Get-LatestDashboardSource {
    $sourceRoot = Join-Path $ProjectRoot "tradex\src\tradex\dashboard"
    if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) {
        throw "未找到网页看板源码目录：$sourceRoot"
    }

    $latestSource = Get-ChildItem -LiteralPath $sourceRoot -Recurse -File -ErrorAction Stop |
        Where-Object {
            $_.Extension -in @(".py", ".html", ".css", ".js") -and
            $_.FullName -notmatch "[\\/]__pycache__[\\/]"
        } |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
    if ($null -eq $latestSource) {
        throw "网页看板源码目录中未找到可检查的源码文件：$sourceRoot"
    }
    if ($latestSource.LastWriteTimeUtc -gt [DateTime]::UtcNow.AddMinutes(5)) {
        throw "网页看板源码时间晚于系统时间，未自动重启：$($latestSource.FullName)"
    }
    return $latestSource
}

function Get-LogTail {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "" }
    return ((Get-Content -LiteralPath $Path -Tail 12 -ErrorAction SilentlyContinue) -join [Environment]::NewLine).Trim()
}

function Start-Component {
    param(
        [string]$Name,
        [int]$Port,
        [string[]]$Arguments,
        [switch]$OpenBrowser
    )

    $label = Get-ComponentLabel $Name
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw "未找到项目 Python 环境：$PythonPath。请先创建 .venv 并安装项目依赖。"
    }

    $state = Read-ComponentState $Name
    if (Test-ComponentState $Name $state) {
        $restartForSourceUpdate = $false
        if ($Name -eq "dashboard") {
            $latestSource = Get-LatestDashboardSource
            $runningProcess = Get-Process -Id ([int]$state.process_pid) -ErrorAction Stop
            $restartForSourceUpdate = (
                $latestSource.LastWriteTimeUtc -gt $runningProcess.StartTime.ToUniversalTime()
            )
            if (-not $restartForSourceUpdate -and (
                -not (Test-DashboardHealth ([int]$state.port)) -or
                -not (Test-ComponentState $Name $state)
            )) {
                throw "$label 的受管进程仍占用端口，但未通过当前页面健康校验；未自动停止或重启。"
            }
        }

        if ($restartForSourceUpdate) {
            $managedPort = [int]$state.port
            Write-Host "[更新中] $label 源码已有更新，安全重启..." -ForegroundColor Yellow
            Stop-Component $Name $managedPort
            $Port = $managedPort
            $state = $null
        }
        else {
            Write-Host "[已运行] $label，端口 $($state.port)，PID $($state.process_pid)" -ForegroundColor Green
            if ($OpenBrowser -and -not $NoBrowser) {
                Start-Process ("http://127.0.0.1:{0}/" -f [int]$state.port)
            }
            return
        }
    }

    if ($null -ne $state) {
        $recordedProcessAlive = Test-ExpectedProcess $Name ([int]$state.process_pid) $state.process_start_utc
        $launcherStart = Get-OptionalStateValue $state "launcher_start_utc"
        $recordedLauncherAlive = $false
        if (-not [string]::IsNullOrWhiteSpace($launcherStart)) {
            $recordedLauncherAlive = Test-ExpectedProcess $Name ([int]$state.launcher_pid) $launcherStart
        }
        if ($recordedProcessAlive -or $recordedLauncherAlive) {
            Write-Host "[清理中] $label 存在未就绪的受管进程，先安全停止..." -ForegroundColor Yellow
            Stop-Component $Name ([int]$state.port)
        }
        else {
            Remove-ComponentState $Name
        }
    }

    $existingOwner = Get-PortOwner $Port
    if ($null -ne $existingOwner) {
        $compatibleDashboard = (
            $Name -eq "dashboard" -and
            (Test-CompatibleDashboardProcess $Port $existingOwner)
        )
        if ($compatibleDashboard) {
            $latestSource = Get-LatestDashboardSource
            $runningProcess = Get-Process -Id $existingOwner -ErrorAction Stop
            if ($latestSource.LastWriteTimeUtc -gt $runningProcess.StartTime.ToUniversalTime()) {
                throw "端口 $Port 上是本项目的旧版 $label，但它未受启动器管理；请先手动停止该实例，再重新启动。"
            }
            else {
                Write-Host "[已运行] $label 已由本项目进程提供，端口 $Port，PID $existingOwner（未接管）" -ForegroundColor Green
                if ($OpenBrowser -and -not $NoBrowser) {
                    Start-Process ("http://127.0.0.1:{0}/" -f $Port)
                }
                return
            }
        }
        else {
            throw "端口 $Port 已被 PID $existingOwner 占用；未能确认它是本项目的健康 $label，为避免误操作，未启动。"
        }
    }

    if (-not (Test-Path -LiteralPath $RuntimeDir -PathType Container)) {
        New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    }
    $stdoutPath = Join-Path $RuntimeDir ("{0}.out.log" -f $Name)
    $stderrPath = Join-Path $RuntimeDir ("{0}.err.log" -f $Name)

    if ($Name -eq "dashboard") {
        $env:TRADEX_DASHBOARD_PORT = [string]$Port
    }

    Write-Host "[启动中] $label..." -ForegroundColor Cyan
    $launcher = $null
    $launcherStartUtc = $null
    $owner = $null
    $ownedProcessStartUtc = $null
    $stateWritten = $false
    try {
        $launcher = Start-Process `
            -FilePath $PythonPath `
            -ArgumentList $Arguments `
            -WorkingDirectory $ProjectRoot `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -WindowStyle Hidden `
            -PassThru
        $launcherStartUtc = $launcher.StartTime.ToUniversalTime().ToString("o")

        $deadline = [DateTime]::UtcNow.AddSeconds(45)
        while ([DateTime]::UtcNow -lt $deadline) {
            $owner = Get-PortOwner $Port
            if ($null -ne $owner) { break }
            $launcher.Refresh()
            if ($launcher.HasExited) { break }
            Start-Sleep -Milliseconds 300
        }

        if ($null -eq $owner) {
            $details = Get-LogTail $stderrPath
            if ([string]::IsNullOrWhiteSpace($details)) {
                $details = Get-LogTail $stdoutPath
            }
            if ([string]::IsNullOrWhiteSpace($details)) {
                $details = "未产生启动日志。"
            }
            throw "$label 未能在 45 秒内监听端口 $Port。`n$details"
        }

        if (-not (Test-IsDescendantProcess $owner $launcher.Id) -or -not (Test-ExpectedProcess $Name $owner $null)) {
            throw "端口 $Port 在启动过程中被其他进程占用（PID $owner）；未接管该进程。"
        }

        $runningProcess = Get-Process -Id $owner -ErrorAction Stop
        $ownedProcessStartUtc = $runningProcess.StartTime.ToUniversalTime().ToString("o")
        if ($Name -eq "dashboard" -and (
            -not (Test-DashboardHealth $Port) -or
            (Get-PortOwner $Port) -ne $owner -or
            -not (Test-ExpectedProcess $Name $owner $ownedProcessStartUtc)
        )) {
            throw "$label 已监听端口，但未通过当前页面健康校验；已放弃本次启动。"
        }
        $componentState = [ordered]@{
            name = $Name
            port = $Port
            process_pid = $owner
            process_start_utc = $ownedProcessStartUtc
            launcher_pid = $launcher.Id
            launcher_start_utc = $launcherStartUtc
            started_at = (Get-Date).ToString("o")
            stdout_log = $stdoutPath
            stderr_log = $stderrPath
        }
        Write-ComponentState $Name $componentState
        $stateWritten = $true
    }
    catch {
        if (-not $stateWritten) {
            if ($null -ne $owner -and -not [string]::IsNullOrWhiteSpace($ownedProcessStartUtc)) {
                Stop-ExpectedProcessTree $Name $owner $ownedProcessStartUtc | Out-Null
            }
            if ($null -ne $launcher -and -not [string]::IsNullOrWhiteSpace($launcherStartUtc)) {
                Stop-ExpectedProcessTree $Name $launcher.Id $launcherStartUtc | Out-Null
            }
        }
        throw
    }

    Write-Host "[已启动] $label，端口 $Port，PID $owner" -ForegroundColor Green
    if ($Name -eq "dashboard") {
        $url = "http://127.0.0.1:$Port/"
        Write-Host "          $url"
        if ($OpenBrowser -and -not $NoBrowser) {
            Start-Process $url
        }
    }
    else {
        Write-Host "          MCP: http://127.0.0.1:$Port/mcp"
    }
}

function Stop-Component {
    param([string]$Name, [int]$DefaultPort)

    $label = Get-ComponentLabel $Name
    $state = Read-ComponentState $Name
    if ($null -eq $state) {
        $owner = Get-PortOwner $DefaultPort
        if ($null -eq $owner) {
            Write-Host "[已停止] $label 当前未运行。" -ForegroundColor DarkGray
            return
        }
        throw "端口 $DefaultPort 由未受本工具管理的 PID $owner 占用；为避免误杀，未停止任何进程。"
    }

    $port = [int]$state.port
    $processId = [int]$state.process_pid
    $processStart = $state.process_start_utc
    $launcherPid = [int]$state.launcher_pid
    $launcherStart = Get-OptionalStateValue $state "launcher_start_utc"
    $owner = Get-PortOwner $port
    $recordedProcessAlive = Test-ExpectedProcess $Name $processId $processStart
    $recordedLauncherAlive = $false
    if (-not [string]::IsNullOrWhiteSpace($launcherStart)) {
        $recordedLauncherAlive = Test-ExpectedProcess $Name $launcherPid $launcherStart
    }

    if ($null -ne $owner -and $owner -eq $processId -and -not $recordedProcessAlive) {
        throw "端口 $port 的 PID 已被复用且与启动记录不匹配；为避免误杀，未停止任何进程。"
    }

    if (-not $recordedProcessAlive -and -not $recordedLauncherAlive) {
        Remove-ComponentState $Name
        if ($null -ne $owner) {
            Write-Host "[已停止] $label 的受管进程已不存在；端口 $port 当前由外部 PID $owner 占用。" -ForegroundColor Yellow
        }
        else {
            Write-Host "[已停止] $label 当前未运行，已清理过期状态。" -ForegroundColor DarkGray
        }
        return
    }

    Write-Host "[停止中] $label（记录 PID $processId）..." -ForegroundColor Yellow
    if ($recordedProcessAlive) {
        Stop-ExpectedProcessTree $Name $processId $processStart | Out-Null
    }
    if ($recordedLauncherAlive) {
        Stop-ExpectedProcessTree $Name $launcherPid $launcherStart | Out-Null
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ([DateTime]::UtcNow -lt $deadline) {
        $processStillAlive = Test-ExpectedProcess $Name $processId $processStart
        $launcherStillAlive = $false
        if (-not [string]::IsNullOrWhiteSpace($launcherStart)) {
            $launcherStillAlive = Test-ExpectedProcess $Name $launcherPid $launcherStart
        }
        if (-not $processStillAlive -and -not $launcherStillAlive) { break }
        Start-Sleep -Milliseconds 200
    }
    if ((Test-ExpectedProcess $Name $processId $processStart) -or
        (-not [string]::IsNullOrWhiteSpace($launcherStart) -and (Test-ExpectedProcess $Name $launcherPid $launcherStart))) {
        throw "$label 的受管进程未能在 10 秒内停止；状态记录已保留。"
    }

    Remove-ComponentState $Name
    $remainingOwner = Get-PortOwner $port
    if ($null -ne $remainingOwner) {
        Write-Host "[已停止] $label；端口 $port 仍由外部 PID $remainingOwner 占用。" -ForegroundColor Yellow
    }
    else {
        Write-Host "[已停止] $label。" -ForegroundColor Green
    }
}

function Open-Dashboard {
    param([int]$DefaultPort)

    $state = Read-ComponentState "dashboard"
    if (Test-ComponentState "dashboard" $state) {
        if (-not (Test-DashboardHealth ([int]$state.port)) -or
            -not (Test-ComponentState "dashboard" $state)) {
            throw "网页看板的受管进程未通过当前页面健康校验。"
        }
        Start-Process ("http://127.0.0.1:{0}/" -f [int]$state.port)
        return
    }

    $owner = Get-PortOwner $DefaultPort
    if ($null -eq $owner -or -not (Test-CompatibleDashboardProcess $DefaultPort $owner)) {
        throw "网页看板尚未通过本工具启动，也未发现可安全复用的本项目实例。"
    }

    $latestSource = Get-LatestDashboardSource
    $runningProcess = Get-Process -Id $owner -ErrorAction Stop
    if ($latestSource.LastWriteTimeUtc -gt $runningProcess.StartTime.ToUniversalTime()) {
        throw "端口 $DefaultPort 上是本项目的旧版网页看板，但它未受启动器管理；请先手动停止该实例，再重新启动。"
    }
    Start-Process ("http://127.0.0.1:{0}/" -f $DefaultPort)
}

function Show-ComponentStatus {
    param([string]$Name, [int]$DefaultPort)

    $label = Get-ComponentLabel $Name
    $state = Read-ComponentState $Name
    if (Test-ComponentState $Name $state) {
        Write-Host ("{0,-10} 运行中   端口 {1}   PID {2}" -f $label, $state.port, $state.process_pid) -ForegroundColor Green
        return
    }

    $port = $DefaultPort
    if ($null -ne $state) { $port = [int]$state.port }
    $owner = Get-PortOwner $port
    $recordedProcessAlive = $false
    if ($null -ne $state) {
        $recordedProcessAlive = Test-ExpectedProcess $Name ([int]$state.process_pid) $state.process_start_utc
    }
    if ($recordedProcessAlive) {
        Write-Host ("{0,-10} 异常     PID {1} 仍在，但端口 {2} 未由它监听" -f $label, $state.process_pid, $port) -ForegroundColor Red
    }
    elseif ($null -ne $owner) {
        if ($Name -eq "dashboard" -and (Test-CompatibleDashboardProcess $port $owner)) {
            Write-Host ("{0,-10} 项目实例 端口 {1}   PID {2}（未接管）" -f $label, $port, $owner) -ForegroundColor Green
        }
        else {
            Write-Host ("{0,-10} 未受管理 端口 {1}   PID {2}" -f $label, $port, $owner) -ForegroundColor Yellow
        }
    }
    elseif ($null -ne $state) {
        Write-Host ("{0,-10} 已停止   （存在过期状态记录）" -f $label) -ForegroundColor DarkYellow
    }
    else {
        Write-Host ("{0,-10} 已停止" -f $label) -ForegroundColor DarkGray
    }
}

function Invoke-AllSteps {
    param([scriptblock[]]$Steps)
    $errors = @()
    foreach ($step in $Steps) {
        try {
            & $step
        }
        catch {
            $errors += $_.Exception.Message
            Write-Host ("[失败] " + $_.Exception.Message) -ForegroundColor Red
        }
    }
    if ($errors.Count -gt 0) {
        throw ("部分操作失败：{0}" -f ($errors -join " | "))
    }
}

# Dot-sourcing loads the functions for focused tests without executing an
# action or calling exit. Normal .cmd and -File invocation paths are unchanged.
if ($MyInvocation.InvocationName -eq ".") {
    return
}

$mutexBytes = [Text.Encoding]::UTF8.GetBytes($ProjectRoot.ToLowerInvariant())
$mutexHasher = [Security.Cryptography.SHA256]::Create()
try {
    $mutexHash = [BitConverter]::ToString($mutexHasher.ComputeHash($mutexBytes)).Replace("-", "")
}
finally {
    $mutexHasher.Dispose()
}
$controlMutex = [Threading.Mutex]::new($false, "Local\TradexControl_$mutexHash")
$controlMutexAcquired = $false
$exitCode = 0

try {
    try {
        $controlMutexAcquired = $controlMutex.WaitOne([TimeSpan]::FromSeconds(60))
    }
    catch [Threading.AbandonedMutexException] {
        $controlMutexAcquired = $true
    }
    if (-not $controlMutexAcquired) {
        throw "等待 tradex 控制锁超时；另一个启动或停止操作可能仍在进行。"
    }

    switch ($Action) {
        "start-dashboard" {
            Start-Component "dashboard" $DashboardPort @("-m", "tradex.dashboard") -OpenBrowser
        }
        "stop-dashboard" {
            Stop-Component "dashboard" $DashboardPort
        }
        "start-service" {
            Start-Component "service" $ServicePort @("-m", "tradex", "--streamable-http", "--host", "127.0.0.1", "--port", [string]$ServicePort)
        }
        "stop-service" {
            Stop-Component "service" $ServicePort
        }
        "start-all" {
            Invoke-AllSteps @(
                { Start-Component "service" $ServicePort @("-m", "tradex", "--streamable-http", "--host", "127.0.0.1", "--port", [string]$ServicePort) },
                { Start-Component "dashboard" $DashboardPort @("-m", "tradex.dashboard") -OpenBrowser }
            )
        }
        "stop-all" {
            Invoke-AllSteps @(
                { Stop-Component "dashboard" $DashboardPort },
                { Stop-Component "service" $ServicePort }
            )
        }
        "status" {
            Write-Host "tradex 本地运行状态" -ForegroundColor Cyan
            Write-Host "--------------------"
            Show-ComponentStatus "dashboard" $DashboardPort
            Show-ComponentStatus "service" $ServicePort
            Write-Host ""
            Write-Host "日志目录：$RuntimeDir"
        }
        "open-dashboard" {
            Open-Dashboard $DashboardPort
        }
    }
}
catch {
    Write-Host ("[失败] " + $_.Exception.Message) -ForegroundColor Red
    $exitCode = 1
}
finally {
    if ($controlMutexAcquired) {
        $controlMutex.ReleaseMutex()
    }
    $controlMutex.Dispose()
}

exit $exitCode
