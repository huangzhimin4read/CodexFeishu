#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$TaskName = 'CodexFeishu-Worker-Owner',
    [string]$WorkerName = 'CodexFeishuWorker',
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
$output = [System.IO.Path]::GetFullPath($OutputPath)
$policyPath = [System.IO.Path]::ChangeExtension($output, '.policy.inf')
try {
    $started = Get-Date
    wevtutil.exe sl Microsoft-Windows-TaskScheduler/Operational /e:true | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 5
    secedit.exe /export /cfg $policyPath /areas USER_RIGHTS | Out-Null
    $policy = Get-Content -LiteralPath $policyPath -Encoding Unicode | Where-Object {
        $_ -match '^Se(BatchLogonRight|DenyBatchLogonRight)'
    }
    $taskEvents = Get-WinEvent -FilterHashtable @{
        LogName = 'Microsoft-Windows-TaskScheduler/Operational'
        StartTime = $started.AddSeconds(-5)
    } -ErrorAction SilentlyContinue | Where-Object {
        $_.Message -like "*$TaskName*"
    } | Select-Object TimeCreated, Id, LevelDisplayName, Message
    $securityEvents = Get-WinEvent -FilterHashtable @{
        LogName = 'Security'
        Id = 4625
        StartTime = $started.AddSeconds(-5)
    } -ErrorAction SilentlyContinue | Where-Object {
        $_.Message -like "*$WorkerName*"
    } | Select-Object TimeCreated, Id, LevelDisplayName, Message
    $task = Get-ScheduledTask -TaskName $TaskName
    $taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName
    $result = [ordered]@{
        captured_at = (Get-Date).ToUniversalTime().ToString('o')
        succeeded = $true
        task_state = [string]$task.State
        last_run_time = $taskInfo.LastRunTime.ToUniversalTime().ToString('o')
        last_task_result = $taskInfo.LastTaskResult
        principal_user = $task.Principal.UserId
        principal_logon_type = [string]$task.Principal.LogonType
        principal_run_level = [string]$task.Principal.RunLevel
        allow_demand_start = $task.Settings.AllowDemandStart
        disallow_battery = $task.Settings.DisallowStartIfOnBatteries
        user_rights = @($policy)
        task_events = @($taskEvents)
        security_logon_failures = @($securityEvents)
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
$result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $output -Encoding utf8
