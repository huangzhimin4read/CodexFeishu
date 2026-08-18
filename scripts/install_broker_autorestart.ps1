[CmdletBinding()]
param(
    [string]$TaskName = 'CodexFeishu-Broker-Owner',
    [string]$Workspace = '',
    [string]$InstallRoot = 'C:\ProgramData\CodexFeishuBridge',
    [string]$PythonPath = 'python.exe',
    [string]$ConfigPath = '',
    [string]$RuntimeRoot = '',
    [string]$LarkCliProfile = 'codex-feishu-owner',
    [switch]$SkipLarkCliSetup,
    [switch]$RequireUserIdentity
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($Workspace)) {
    $Workspace = Split-Path -Parent $PSScriptRoot
}
if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
    $RuntimeRoot = Join-Path $Workspace '.runtime'
}
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $RuntimeRoot 'live-remote.toml'
}
$pythonCommand = Get-Command -Name $PythonPath -ErrorAction Stop
$PythonPath = $pythonCommand.Source
$workspacePath = [System.IO.Path]::GetFullPath($Workspace)
$installPath = [System.IO.Path]::GetFullPath($InstallRoot)
$programData = [System.IO.Path]::GetFullPath($env:ProgramData).TrimEnd('\') + '\'
if (-not $installPath.StartsWith($programData, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'InstallRoot must stay below ProgramData.'
}

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$brokerSid = $identity.User.Value
$qualifiedUser = $identity.Name
$brokerRoot = Join-Path $installPath 'broker'
$runnerSource = Join-Path $workspacePath 'scripts\run_supervised_remote_service.ps1'
$runner = Join-Path $brokerRoot 'run_supervised_remote_service.ps1'
$applicationRoot = Join-Path $installPath 'app'
$larkCliSetup = Join-Path $workspacePath 'scripts\setup_lark_cli.ps1'
$resultPath = Join-Path ([System.IO.Path]::GetFullPath($RuntimeRoot)) 'broker-autorestart-install.json'
$xmlPath = Join-Path ([System.IO.Path]::GetFullPath($RuntimeRoot)) 'broker-autorestart-task.xml'

foreach ($required in @($runnerSource, $PythonPath, $ConfigPath, $applicationRoot)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required path is missing: $required"
    }
}

if (-not $SkipLarkCliSetup) {
    if (-not (Test-Path -LiteralPath $larkCliSetup)) {
        throw "Required Feishu CLI setup guide is missing: $larkCliSetup"
    }
    Write-Host '正在进入飞书 CLI 安装与账号授权引导。若只使用机器人通知，可以在引导中跳过。'
    if ($RequireUserIdentity) {
        & $larkCliSetup -Profile $LarkCliProfile -RequireUserIdentity
    } else {
        & $larkCliSetup -Profile $LarkCliProfile
    }
}

New-Item -ItemType Directory -Path $brokerRoot -Force | Out-Null
Copy-Item -LiteralPath $runnerSource -Destination $runner -Force

function Quote-TaskArgument([string]$Value) {
    return '"' + $Value.Replace('"', '""') + '"'
}

$powerShell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $powerShell -PathType Leaf)) {
    throw "Stable Windows PowerShell executable is missing: $powerShell"
}
$arguments = @(
    '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-WindowStyle', 'Hidden', '-File', (Quote-TaskArgument $runner),
    '-PythonPath', (Quote-TaskArgument ([System.IO.Path]::GetFullPath($PythonPath))),
    '-ApplicationRoot', (Quote-TaskArgument $applicationRoot),
    '-ConfigPath', (Quote-TaskArgument ([System.IO.Path]::GetFullPath($ConfigPath))),
    '-RuntimeRoot', (Quote-TaskArgument ([System.IO.Path]::GetFullPath($RuntimeRoot))),
    '-TaskName', (Quote-TaskArgument $TaskName)
) -join ' '

$action = New-ScheduledTaskAction -Execute $powerShell -Argument $arguments `
    -WorkingDirectory $brokerRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $qualifiedUser
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId $qualifiedUser `
    -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -Hidden
$definition = New-ScheduledTask -Action $action -Trigger @($trigger, $watchdogTrigger) `
    -Principal $principal -Settings $settings `
    -Description 'Single-user Codex Feishu/Lark bridge; restart after exit or stale health.'
Register-ScheduledTask -TaskName $TaskName -InputObject $definition -Force | Out-Null

$xml = Export-ScheduledTask -TaskName $TaskName
[System.IO.File]::WriteAllText($xmlPath, $xml, [System.Text.UTF8Encoding]::new($false))
$sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $runnerSource).Hash
$installedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $runner).Hash
if ($sourceHash -ne $installedHash) {
    throw 'Installed supervisor hash differs from the workspace source.'
}
$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
$result = [ordered]@{
    installed_at = (Get-Date).ToUniversalTime().ToString('o')
    task_name = $TaskName
    task_state = [string]$task.State
    task_user = $task.Principal.UserId
    task_logon_type = [string]$task.Principal.LogonType
    task_run_level = [string]$task.Principal.RunLevel
    restart_count = $task.Settings.RestartCount
    restart_interval = [string]$task.Settings.RestartInterval
    start_at_logon = $true
    periodic_watchdog_minutes = 1
    start_when_available = [bool]$task.Settings.StartWhenAvailable
    multiple_instances = [string]$task.Settings.MultipleInstances
    last_task_result = $info.LastTaskResult
    runner_path = $runner
    runner_sha256 = $installedHash
    broker_sid = $brokerSid
    lark_cli_setup = if ($SkipLarkCliSetup) { 'skipped' } else { 'guided' }
    lark_cli_profile = if ($SkipLarkCliSetup) { $null } else { $LarkCliProfile }
}
[System.IO.File]::WriteAllText(
    $resultPath,
    ($result | ConvertTo-Json -Compress) + "`n",
    [System.Text.UTF8Encoding]::new($false)
)
$result | ConvertTo-Json -Compress
