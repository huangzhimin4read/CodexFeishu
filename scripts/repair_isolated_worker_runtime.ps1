#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$Workspace = '',
    [string]$InstallRoot = 'C:\ProgramData\CodexFeishuBridge',
    [string]$PythonPath = 'python.exe',
    [string]$WorkerName = 'CodexFeishuWorker',
    [string]$TaskName = 'CodexFeishu-Worker-Owner',
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($Workspace)) {
    $Workspace = Split-Path -Parent $PSScriptRoot
}
$PythonPath = (Get-Command -Name $PythonPath -ErrorAction Stop).Source
$workspacePath = [System.IO.Path]::GetFullPath($Workspace)
$installPath = [System.IO.Path]::GetFullPath($InstallRoot)
$runtimePath = Join-Path $workspacePath '.runtime'
$launchFile = Join-Path $runtimePath 'isolated-worker-launch.json'
$applicationRoot = Join-Path $installPath 'app'
$diagnosticRoot = Join-Path $installPath 'worker-diagnostics'
$diagnosticFile = Join-Path $diagnosticRoot 'last-error.json'
$output = [System.IO.Path]::GetFullPath($OutputPath)

function Invoke-Icacls {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & icacls.exe @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "icacls failed with exit code $LASTEXITCODE for: $($Arguments -join ' ')"
    }
}

try {
    $worker = Get-LocalUser -Name $WorkerName
    if (-not $worker.Enabled) { throw "Worker account is disabled: $WorkerName" }
    $workerSid = $worker.SID.Value
    $brokerSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $adminGroup = [ADSI]("WinNT://{0}/Administrators,group" -f $env:COMPUTERNAME)
    if ([bool]$adminGroup.psbase.Invoke(
        'IsMember', "WinNT://$env:COMPUTERNAME/$WorkerName,user")) {
        throw "Worker account must not be an administrator: $WorkerName"
    }

    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
    New-Item -ItemType Directory -Path $applicationRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $diagnosticRoot -Force | Out-Null

    Copy-Item -LiteralPath (Join-Path $workspacePath 'codex_feishu_bridge') `
        -Destination $applicationRoot -Recurse -Force
    & $PythonPath (Join-Path $workspacePath 'scripts\stage_runtime_dependencies.py') `
        --target $applicationRoot `
        --lock (Join-Path $workspacePath 'requirements-runtime.lock')
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to stage exact installed worker runtime dependencies.'
    }

    # Revoke the recursive project-root grant from all control-plane state.
    Invoke-Icacls $runtimePath /remove:g "*$workerSid" /T /C
    Invoke-Icacls $runtimePath /inheritance:r
    Invoke-Icacls $runtimePath /grant:r `
        "*$brokerSid`:(OI)(CI)(F)" `
        '*S-1-5-18:(OI)(CI)(F)' `
        '*S-1-5-32-544:(OI)(CI)(F)'
    Invoke-Icacls $runtimePath /grant "*$workerSid`:(RX)"

    Invoke-Icacls $installPath /inheritance:r
    Invoke-Icacls $installPath /grant:r `
        "*$brokerSid`:(OI)(CI)(F)" `
        '*S-1-5-18:(OI)(CI)(F)' `
        '*S-1-5-32-544:(OI)(CI)(F)'
    Invoke-Icacls $installPath /grant "*$workerSid`:(RX)"
    Invoke-Icacls $applicationRoot /grant:r "*$workerSid`:(OI)(CI)(RX)" /T /C
    Invoke-Icacls $diagnosticRoot /inheritance:r
    Invoke-Icacls $diagnosticRoot /grant:r `
        "*$brokerSid`:(OI)(CI)(F)" `
        "*$workerSid`:(OI)(CI)(M)" `
        '*S-1-5-18:(OI)(CI)(F)' `
        '*S-1-5-32-544:(OI)(CI)(F)'

    $scheduler = New-Object -ComObject 'Schedule.Service'
    $scheduler.Connect()
    $registeredTask = $scheduler.GetFolder('\').GetTask($TaskName)
    $taskSddl = "D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;GRGX;;;$workerSid)(A;;GRGX;;;$brokerSid)"
    $registeredTask.SetSecurityDescriptor($taskSddl, 0)

    $result = [ordered]@{
        captured_at = (Get-Date).ToUniversalTime().ToString('o')
        succeeded = $true
        worker_sid = $workerSid
        worker_is_administrator = $false
        runtime_worker_ace_removed = $true
        application_root = $applicationRoot
        diagnostic_file = $diagnosticFile
        task_name = $TaskName
    }
}
catch {
    $result = [ordered]@{
        captured_at = (Get-Date).ToUniversalTime().ToString('o')
        succeeded = $false
        error_type = $_.Exception.GetType().FullName
        error_message = $_.Exception.Message
        error_details = ($_ | Format-List * -Force | Out-String)
    }
}
$result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $output -Encoding utf8
if (-not $result.succeeded) { exit 1 }
