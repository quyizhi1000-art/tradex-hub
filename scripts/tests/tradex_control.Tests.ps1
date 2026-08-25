$controlScript = Join-Path (Split-Path $PSScriptRoot -Parent) "tradex_control.ps1"
. $controlScript

Describe "tradex dashboard launcher compatibility" {
    BeforeEach {
        $script:listenerPid = 4100
        $script:launcherPid = 4200
        $script:testStartTime = [DateTime]::Parse("2026-08-24T01:00:00Z").ToLocalTime()
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

    It "reuses but does not adopt a compatible unmanaged dashboard" {
        Mock Read-ComponentState { return $null }
        Mock Get-PortOwner { return $script:listenerPid }
        Mock Test-CompatibleDashboardProcess { return $true }
        Mock Get-LatestDashboardSource {
            return [pscustomobject]@{ LastWriteTimeUtc = $script:testStartTime.ToUniversalTime().AddMinutes(-1) }
        }
        Mock Write-ComponentState { throw "must not adopt an unmanaged process" }
        Mock Start-Process { throw "must not start or stop a process under -NoBrowser" }
        $script:NoBrowser = $true

        Start-Component "dashboard" 8765 @("-m", "tradex.dashboard") -OpenBrowser

        Assert-MockCalled Write-ComponentState -Times 0 -Exactly -Scope It
        Assert-MockCalled Start-Process -Times 0 -Exactly -Scope It
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

        { Start-Component "dashboard" 8765 @("-m", "tradex.dashboard") -OpenBrowser } | Should Throw
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

        { Start-Component "dashboard" 8765 @("-m", "tradex.dashboard") -OpenBrowser } | Should Throw

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
