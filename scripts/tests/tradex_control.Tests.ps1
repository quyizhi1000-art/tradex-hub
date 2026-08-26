$controlScript = Join-Path (Split-Path $PSScriptRoot -Parent) "tradex_control.ps1"
. $controlScript

Describe "tradex dashboard launcher compatibility" {
    BeforeEach {
        $script:listenerPid = 4100
        $script:launcherPid = 4200
        $script:testStartTime = [DateTime]::Parse("2026-08-24T01:00:00Z").ToLocalTime()
        $script:capturedState = $null
        $script:testProcesses = @{
            4100 = [pscustomobject]@{
                ProcessId = 4100
                ParentProcessId = 4200
                ExecutablePath = "C:\Python312\python.exe"
                CommandLine = '"C:\Python312\python.exe" -m tradex.dashboard'
            }
            4200 = [pscustomobject]@{
                ProcessId = 4200
                ParentProcessId = 1
                ExecutablePath = $PythonPath
                CommandLine = ('"{0}" -m tradex.dashboard' -f $PythonPath)
            }
        }

        Mock Get-CimInstance {
            param($ClassName, $Filter)
            if ([string]$Filter -match "ProcessId=(\d+)") {
                return $script:testProcesses[[int]$Matches[1]]
            }
            return $null
        }
        Mock Get-Process {
            param($Id)
            $processId = [int](@($Id)[0])
            return [pscustomobject]@{ Id = $processId; StartTime = $script:testStartTime }
        }
        Mock Get-PortOwner { return $script:listenerPid }
        Mock Test-DashboardHealth { return $true }
    }

    It "accepts a listener descended from this project's exact virtual environment" {
        Test-CompatibleDashboardProcess 8765 $script:listenerPid | Should Be $true
        Assert-MockCalled Test-DashboardHealth -Times 1 -Exactly -Scope It
    }

    It "rejects the same module launched from another virtual environment" {
        $script:testProcesses[$script:launcherPid].ExecutablePath = "C:\other\money\.venv\Scripts\python.exe"

        Test-CompatibleDashboardProcess 8765 $script:listenerPid | Should Be $false
        Assert-MockCalled Test-DashboardHealth -Times 0 -Exactly -Scope It
    }

    It "rejects an unhealthy or non-matching dashboard response" {
        Mock Test-DashboardHealth { return $false }

        Test-CompatibleDashboardProcess 8765 $script:listenerPid | Should Be $false
    }

    It "adopts a compatible unmanaged dashboard so the stop script can manage it" {
        Mock Read-ComponentState { return $null }
        Mock Get-PortOwner { return $script:listenerPid }
        Mock Test-CompatibleDashboardProcess { return $true }
        Mock Get-LatestDashboardSource {
            return [pscustomobject]@{ LastWriteTimeUtc = $script:testStartTime.ToUniversalTime().AddMinutes(-1) }
        }
        Mock Write-ComponentState {
            param($Name, $State)
            $script:capturedState = $State
        }
        Mock Start-Process { throw "must not start or stop a process under -NoBrowser" }
        $script:NoBrowser = $true

        Start-Component "dashboard" 8765 @("-m", "tradex.dashboard") -OpenBrowser

        Assert-MockCalled Write-ComponentState -Times 1 -Exactly -Scope It
        Assert-MockCalled Start-Process -Times 0 -Exactly -Scope It
        $script:capturedState.process_pid | Should Be $script:listenerPid
        $script:capturedState.launcher_pid | Should Be $script:launcherPid
        $script:capturedState.process_start_utc | Should Be $script:testStartTime.ToUniversalTime().ToString("o")
        $script:capturedState.launcher_start_utc | Should Be $script:testStartTime.ToUniversalTime().ToString("o")
        Test-ComponentState "dashboard" $script:capturedState | Should Be $true
    }

    It "adopts and stops a compatible unmanaged dashboard directly" {
        $script:stopped = $false
        Mock Read-ComponentState { return $null }
        Mock Get-PortOwner {
            if ($script:stopped) { return $null }
            return $script:listenerPid
        }
        Mock Test-CompatibleDashboardProcess { return $true }
        Mock Test-ExpectedProcess { return (-not $script:stopped) }
        Mock Write-ComponentState {
            param($Name, $State)
            $script:capturedState = $State
        }
        Mock Stop-ExpectedProcessTree {
            $script:stopped = $true
            return $true
        }
        Mock Remove-ComponentState { return $null }

        Stop-Component "dashboard" 8765

        Assert-MockCalled Write-ComponentState -Times 1 -Exactly -Scope It
        Assert-MockCalled Stop-ExpectedProcessTree -Times 2 -Exactly -Scope It
        Assert-MockCalled Remove-ComponentState -Times 1 -Exactly -Scope It
        $script:capturedState.process_pid | Should Be $script:listenerPid
        $script:capturedState.launcher_pid | Should Be $script:launcherPid
    }

    It "refuses to take over a compatible but stale unmanaged dashboard" {
        Mock Read-ComponentState { return $null }
        Mock Get-PortOwner { return $script:listenerPid }
        Mock Test-CompatibleDashboardProcess { return $true }
        Mock Get-LatestDashboardSource {
            return [pscustomobject]@{ LastWriteTimeUtc = $script:testStartTime.ToUniversalTime().AddMinutes(1) }
        }
        Mock Write-ComponentState { throw "must not adopt an unmanaged process" }
        Mock Stop-Component { throw "must not stop an unmanaged process" }

        $thrown = $false
        try {
            Start-Component "dashboard" 8765 @("-m", "tradex.dashboard") -OpenBrowser
        }
        catch {
            $thrown = $true
        }
        $thrown | Should Be $true
        Assert-MockCalled Write-ComponentState -Times 0 -Exactly -Scope It
        Assert-MockCalled Stop-Component -Times 0 -Exactly -Scope It
    }

    It "opens a compatible unmanaged dashboard without granting management rights" {
        Mock Read-ComponentState { return $null }
        Mock Test-CompatibleDashboardProcess { return $true }
        Mock Get-LatestDashboardSource {
            return [pscustomobject]@{ LastWriteTimeUtc = $script:testStartTime.ToUniversalTime().AddMinutes(-1) }
        }
        Mock Write-ComponentState { throw "must not adopt an unmanaged process" }
        Mock Start-Process { return $null }

        Open-Dashboard 8765

        Assert-MockCalled Start-Process -Times 1 -Exactly -Scope It
        Assert-MockCalled Write-ComponentState -Times 0 -Exactly -Scope It
    }

    It "fails closed when an occupied port is not the verified project dashboard" {
        Mock Read-ComponentState { return $null }
        Mock Test-CompatibleDashboardProcess { return $false }
        Mock Write-ComponentState { throw "must not adopt an unmanaged process" }
        Mock Start-Process { throw "must not start over an occupied port" }

        $thrown = $false
        try {
            Start-Component "dashboard" 8765 @("-m", "tradex.dashboard") -OpenBrowser
        }
        catch {
            $thrown = $true
        }
        $thrown | Should Be $true

        Assert-MockCalled Write-ComponentState -Times 0 -Exactly -Scope It
        Assert-MockCalled Start-Process -Times 0 -Exactly -Scope It
    }
}

Describe "tradex dashboard HTTP identity" {
    BeforeEach {
        $script:originalProjectRoot = $ProjectRoot
        $script:ProjectRoot = $TestDrive
        $script:expectedHtml = "<!doctype html><title>Tradex</title>"
        $assetDirectory = Join-Path $ProjectRoot "tradex\src\tradex\dashboard\watch"
        [IO.Directory]::CreateDirectory($assetDirectory) | Out-Null
        [IO.File]::WriteAllText(
            (Join-Path $assetDirectory "index.html"),
            $script:expectedHtml,
            [Text.Encoding]::UTF8
        )
    }

    AfterEach {
        $script:ProjectRoot = $script:originalProjectRoot
    }

    It "accepts only the exact current workspace root page" {
        Mock Invoke-WebRequest {
            return [pscustomobject]@{
                StatusCode = 200
                Headers = @{ "Content-Type" = "text/html; charset=utf-8" }
                Content = $script:expectedHtml
            }
        }

        Test-DashboardHealth 8765 | Should Be $true
    }

    It "rejects a lookalike Tradex page whose content differs" {
        Mock Invoke-WebRequest {
            return [pscustomobject]@{
                StatusCode = 200
                Headers = @{ "Content-Type" = "text/html; charset=utf-8" }
                Content = "<!doctype html><title>Tradex</title><p>other checkout</p>"
            }
        }

        Test-DashboardHealth 8765 | Should Be $false
    }
}

Describe "tradex launcher state files" {
    BeforeEach {
        $script:originalRuntimeDir = $RuntimeDir
        $script:RuntimeDir = $TestDrive
    }

    AfterEach {
        $script:RuntimeDir = $script:originalRuntimeDir
    }

    It "writes state atomically and reads it back" {
        $state = [ordered]@{ name = "dashboard"; port = 8765; process_pid = 4100 }

        Write-ComponentState "dashboard" $state
        $actual = Read-ComponentState "dashboard"

        $actual.process_pid | Should Be 4100
        @(Get-ChildItem -LiteralPath $RuntimeDir -Filter "*.tmp").Count | Should Be 0
    }

    It "treats a damaged state file as unmanaged instead of trusting it" {
        [IO.File]::WriteAllText((Get-StatePath "dashboard"), "{broken", [Text.Encoding]::UTF8)

        Read-ComponentState "dashboard" | Should BeNullOrEmpty
    }
}

Describe "tradex multi-component error aggregation" {
    It "leaves failure reporting to the single aggregate error" {
        Mock Write-Host { return $null }
        $steps = @(
            { throw "dashboard failed" }
            { return }
        )

        $thrown = $false
        try {
            Invoke-AllSteps $steps
        }
        catch {
            $thrown = $true
        }
        $thrown | Should Be $true

        Assert-MockCalled Write-Host -Times 0 -Exactly -Scope It
    }
}

Describe "tradex independent market-watch collector lifecycle" {
    BeforeEach {
        $script:originalRuntimeDir = $RuntimeDir
        $script:RuntimeDir = $TestDrive
        $script:capturedCollectorState = $null
        $script:collectorStartTime = [DateTime]::Parse("2026-08-24T01:00:00Z").ToLocalTime()
        $script:collectorProcess = [pscustomobject]@{
            Id = 4300
            StartTime = $script:collectorStartTime
            HasExited = $false
        }
        $script:collectorProcess | Add-Member -MemberType ScriptMethod -Name Refresh -Value { }
    }

    AfterEach {
        $script:RuntimeDir = $script:originalRuntimeDir
    }

    It "starts one hidden portless collector and records its exact process identity" {
        Mock Test-Path { return $true }
        Mock Read-ComponentState { return $null }
        Mock Test-ExpectedProcess { return $true }
        Mock Test-CollectorRuntimeReady { return $true }
        Mock Start-Process { return $script:collectorProcess }
        Mock Write-ComponentState {
            param($Name, $State)
            $script:capturedCollectorState = $State
        }
        Mock Write-Host { return $null }

        Start-CollectorWorker

        Assert-MockCalled Start-Process -Times 1 -Exactly -Scope It
        Assert-MockCalled Write-ComponentState -Times 1 -Exactly -Scope It
        $script:capturedCollectorState.name | Should Be "collector"
        $script:capturedCollectorState.process_pid | Should Be 4300
        $script:capturedCollectorState.process_start_utc | Should Be (
            $script:collectorStartTime.ToUniversalTime().ToString("o")
        )
    }

    It "wires dashboard stop independently while stop-all owns collector shutdown" {
        $source = Get-Content -LiteralPath $controlScript -Raw -Encoding UTF8

        $source | Should Match '(?s)"start-dashboard"\s*\{\s*Start-CollectorWorker\s*Start-Component'
        $source | Should Match '(?s)"stop-dashboard"\s*\{\s*Stop-Component\s+"dashboard"\s+\$DashboardPort\s*\}'
        $source | Should Match '(?s)"stop-all"\s*\{.*Stop-CollectorWorker.*\}'
        Get-ProcessPattern "collector" | Should Match 'collector_worker'
    }

    It "accepts only a running worker heartbeat from the started process window" {
        $started = "2026-08-24T01:00:10Z"
        $fresh = [pscustomobject]@{
            collector_state = "running"
            collector_heartbeat_at = "2026-08-24T01:00:10Z"
        }
        $stale = [pscustomobject]@{
            collector_state = "running"
            collector_heartbeat_at = "2026-08-24T00:59:00Z"
        }
        $stopped = [pscustomobject]@{
            collector_state = "stopped"
            collector_heartbeat_at = "2026-08-24T01:00:10Z"
        }

        Test-CollectorEnvelopeReady $fresh $started | Should Be $true
        Test-CollectorEnvelopeReady $stale $started | Should Be $false
        Test-CollectorEnvelopeReady $stopped $started | Should Be $false
    }
}
