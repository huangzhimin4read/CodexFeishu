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
    [int]$MaxRestartAttempts = 999
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
$restartAttempt = 0

while ($true) {
    $child = $null
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

        $child.WaitForExit()
        $exitCode = $child.ExitCode
        $launch.ended_at = (Get-Date).ToUniversalTime().ToString('o')
        $launch.exit_code = $exitCode
        [System.IO.File]::WriteAllText(
            $launchPath,
            ($launch | ConvertTo-Json -Compress) + "`n",
            [System.Text.UTF8Encoding]::new($false)
        )
        if ($exitCode -eq 0) { exit 0 }
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
    }
    [System.IO.File]::WriteAllText(
        (Join-Path $runtime 'broker-supervisor-last-restart.json'),
        ($restart | ConvertTo-Json -Compress) + "`n",
        [System.Text.UTF8Encoding]::new($false)
    )
    if ($restartAttempt -ge $MaxRestartAttempts) { exit 1 }
    Start-Sleep -Seconds $RestartDelaySeconds
}
