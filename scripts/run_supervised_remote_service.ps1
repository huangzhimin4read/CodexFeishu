[CmdletBinding()]
param(
    [string]$PythonPath = 'python.exe',
    [string]$ApplicationRoot = 'C:\ProgramData\CodexFeishuBridge\app',
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [Parameter(Mandatory = $true)]
    [string]$RuntimeRoot,
    [string]$TaskName = 'CodexFeishu-Broker-Owner',
    [ValidateRange(5, 3600)]
    [int]$RestartDelaySeconds = 60,
    [ValidateRange(1, 9999)]
    [int]$MaxRestartAttempts = 999,
    [ValidateRange(1, 3600)]
    [int]$StartupGraceSeconds = 120,
    [ValidateRange(1, 3600)]
    [int]$HealthStaleSeconds = 120,
    [ValidateRange(1, 60)]
    [int]$HealthPollSeconds = 5
)

$ErrorActionPreference = 'Stop'
$python = (Get-Command -Name $PythonPath -ErrorAction Stop).Source
$application = [System.IO.Path]::GetFullPath($ApplicationRoot)
$config = [System.IO.Path]::GetFullPath($ConfigPath)
$runtime = [System.IO.Path]::GetFullPath($RuntimeRoot)

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python executable is missing: $python"
}
if (-not (Test-Path -LiteralPath $application -PathType Container)) {
    throw "Frozen application root is missing: $application"
}
if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
    throw "Live configuration is missing: $config"
}
if (-not (Test-Path -LiteralPath $runtime -PathType Container)) {
    throw "Runtime root is missing: $runtime"
}

$launchPath = Join-Path $runtime 'remote-service-launch.json'
$pidPath = Join-Path $runtime 'topic-group-service.pid'
$healthPath = Join-Path $runtime 'topic-group-status.json'
$watchdogPath = Join-Path $runtime 'broker-supervisor-last-watchdog.json'
$restartAttempt = 0

while ($true) {
    $child = $null
    $watchdogReason = $null
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')
    $stdout = Join-Path $runtime "remote-service-supervised-$stamp.stdout.log"
    $stderr = Join-Path $runtime "remote-service-supervised-$stamp.stderr.log"
    try {
        $arguments = @(
            '-B', '-m', 'codex_feishu_bridge', 'run', '--config', $config
        )
        $child = Start-Process -FilePath $python -ArgumentList $arguments `
            -WorkingDirectory $application `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
            -WindowStyle Hidden -PassThru

        $launch = [ordered]@{
            started = $true
            supervised = $true
            supervisor_task = $TaskName
            supervisor_process_id = $PID
            process_id = $child.Id
            restart_attempt = $restartAttempt
            started_at = (Get-Date).ToUniversalTime().ToString('o')
            config_path = $config
            application_root = $application
            stdout_log = $stdout
            stderr_log = $stderr
        }
        [System.IO.File]::WriteAllText(
            $launchPath,
            ($launch | ConvertTo-Json -Compress) + "`n",
            [System.Text.UTF8Encoding]::new($false)
        )
        [System.IO.File]::WriteAllText(
            $pidPath,
            [string]$child.Id + "`n",
            [System.Text.UTF8Encoding]::new($false)
        )

        $childStartedAt = [DateTime]::UtcNow
        while (-not $child.HasExited) {
            Start-Sleep -Seconds $HealthPollSeconds
            if ($child.HasExited) { break }
            $now = [DateTime]::UtcNow
            if (($now - $childStartedAt).TotalSeconds -lt $StartupGraceSeconds) {
                continue
            }
            $health = Get-Item -LiteralPath $healthPath -ErrorAction SilentlyContinue
            $healthAgeSeconds = $null
            if ($null -eq $health) {
                $watchdogReason = 'health_missing'
            } else {
                $healthAgeSeconds = [Math]::Round(($now - $health.LastWriteTimeUtc).TotalSeconds, 3)
                if ($healthAgeSeconds -gt $HealthStaleSeconds) {
                    $watchdogReason = 'health_stale'
                }
            }
            if ($null -eq $watchdogReason) { continue }

            $watchdog = [ordered]@{
                detected_at = $now.ToString('o')
                supervisor_task = $TaskName
                process_id = $child.Id
                reason = $watchdogReason
                startup_grace_seconds = $StartupGraceSeconds
                health_stale_seconds = $HealthStaleSeconds
                health_age_seconds = $healthAgeSeconds
                last_health_write_at = if ($null -ne $health) { $health.LastWriteTimeUtc.ToString('o') } else { $null }
            }
            [System.IO.File]::WriteAllText(
                $watchdogPath,
                ($watchdog | ConvertTo-Json -Compress) + "`n",
                [System.Text.UTF8Encoding]::new($false)
            )
            try {
                Stop-Process -Id $child.Id -Force -ErrorAction Stop
            }
            catch {
                if (-not $child.HasExited) { throw }
            }
            break
        }
        $child.WaitForExit()
        $exitCode = $child.ExitCode
        $launch.ended_at = (Get-Date).ToUniversalTime().ToString('o')
        $launch.exit_code = $exitCode
        if ($null -ne $watchdogReason) {
            $launch.watchdog_reason = $watchdogReason
        }
        [System.IO.File]::WriteAllText(
            $launchPath,
            ($launch | ConvertTo-Json -Compress) + "`n",
            [System.Text.UTF8Encoding]::new($false)
        )
        if ($exitCode -eq 0 -and $null -eq $watchdogReason) { exit 0 }
    }
    catch {
        $failure = [ordered]@{
            failed_at = (Get-Date).ToUniversalTime().ToString('o')
            supervisor_task = $TaskName
            restart_attempt = $restartAttempt
            error_type = $_.Exception.GetType().FullName
            error_message = $_.Exception.Message
        }
        [System.IO.File]::WriteAllText(
            (Join-Path $runtime 'broker-supervisor-last-error.json'),
            ($failure | ConvertTo-Json -Compress) + "`n",
            [System.Text.UTF8Encoding]::new($false)
        )
    }

    $restartAttempt += 1
    $restart = [ordered]@{
        recorded_at = (Get-Date).ToUniversalTime().ToString('o')
        supervisor_task = $TaskName
        restart_attempt = $restartAttempt
        restart_delay_seconds = $RestartDelaySeconds
        previous_process_id = if ($null -ne $child) { $child.Id } else { $null }
        previous_exit_code = if ($null -ne $child -and $child.HasExited) { $child.ExitCode } else { $null }
        watchdog_reason = $watchdogReason
    }
    [System.IO.File]::WriteAllText(
        (Join-Path $runtime 'broker-supervisor-last-restart.json'),
        ($restart | ConvertTo-Json -Compress) + "`n",
        [System.Text.UTF8Encoding]::new($false)
    )
    if ($restartAttempt -ge $MaxRestartAttempts) { exit 1 }
    Start-Sleep -Seconds $RestartDelaySeconds
}
