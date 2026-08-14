#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$TaskName = 'CodexFeishu-Worker-Owner',
    [string]$WorkerName = 'CodexFeishuWorker',
    [int]$DiagnosticProcessId = 0,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
$output = [System.IO.Path]::GetFullPath($OutputPath)

$lsaSource = @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;

public static class CodexFeishuLsaRights
{
    [StructLayout(LayoutKind.Sequential)]
    private struct LSA_OBJECT_ATTRIBUTES
    {
        public int Length;
        public IntPtr RootDirectory;
        public IntPtr ObjectName;
        public uint Attributes;
        public IntPtr SecurityDescriptor;
        public IntPtr SecurityQualityOfService;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct LSA_UNICODE_STRING
    {
        public ushort Length;
        public ushort MaximumLength;
        public IntPtr Buffer;
    }

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern uint LsaOpenPolicy(
        IntPtr systemName,
        ref LSA_OBJECT_ATTRIBUTES objectAttributes,
        uint desiredAccess,
        out IntPtr policyHandle);

    [DllImport("advapi32.dll")]
    private static extern uint LsaAddAccountRights(
        IntPtr policyHandle,
        byte[] accountSid,
        LSA_UNICODE_STRING[] userRights,
        uint countOfRights);

    [DllImport("advapi32.dll")]
    private static extern uint LsaClose(IntPtr policyHandle);

    [DllImport("advapi32.dll")]
    private static extern uint LsaNtStatusToWinError(uint status);

    public static void AddRight(string sidValue, string rightName)
    {
        const uint POLICY_CREATE_ACCOUNT = 0x00000010;
        const uint POLICY_LOOKUP_NAMES = 0x00000800;
        var attributes = new LSA_OBJECT_ATTRIBUTES();
        attributes.Length = Marshal.SizeOf(typeof(LSA_OBJECT_ATTRIBUTES));
        IntPtr policy;
        uint status = LsaOpenPolicy(IntPtr.Zero, ref attributes,
            POLICY_CREATE_ACCOUNT | POLICY_LOOKUP_NAMES, out policy);
        if (status != 0)
            throw new Win32Exception((int)LsaNtStatusToWinError(status));

        IntPtr rightBuffer = IntPtr.Zero;
        try
        {
            var sid = new SecurityIdentifier(sidValue);
            var sidBytes = new byte[sid.BinaryLength];
            sid.GetBinaryForm(sidBytes, 0);
            rightBuffer = Marshal.StringToHGlobalUni(rightName);
            var right = new LSA_UNICODE_STRING
            {
                Buffer = rightBuffer,
                Length = checked((ushort)(rightName.Length * 2)),
                MaximumLength = checked((ushort)((rightName.Length + 1) * 2))
            };
            status = LsaAddAccountRights(policy, sidBytes,
                new[] { right }, 1);
            if (status != 0)
                throw new Win32Exception((int)LsaNtStatusToWinError(status));
        }
        finally
        {
            if (rightBuffer != IntPtr.Zero)
                Marshal.FreeHGlobal(rightBuffer);
            LsaClose(policy);
        }
    }
}
'@

try {
    if ($DiagnosticProcessId -gt 0) {
        Stop-Process -Id $DiagnosticProcessId -Force -ErrorAction SilentlyContinue
    }

    $worker = Get-LocalUser -Name $WorkerName
    if (-not $worker.Enabled) {
        throw "Worker account is disabled: $WorkerName"
    }
    $adminGroup = [ADSI]("WinNT://{0}/Administrators,group" -f $env:COMPUTERNAME)
    $workerPath = "WinNT://$env:COMPUTERNAME/$WorkerName,user"
    if ([bool]$adminGroup.psbase.Invoke('IsMember', $workerPath)) {
        throw "Worker account must not be an administrator: $WorkerName"
    }

    if (-not ('CodexFeishuLsaRights' -as [type])) {
        Add-Type -TypeDefinition $lsaSource -Language CSharp
    }
    [CodexFeishuLsaRights]::AddRight($worker.SID.Value, 'SeBatchLogonRight')

    $started = Get-Date
    wevtutil.exe sl Microsoft-Windows-TaskScheduler/Operational /e:true | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 8
    $task = Get-ScheduledTask -TaskName $TaskName
    $taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName
    $taskEvents = Get-WinEvent -FilterHashtable @{
        LogName = 'Microsoft-Windows-TaskScheduler/Operational'
        StartTime = $started.AddSeconds(-3)
    } -ErrorAction SilentlyContinue | Where-Object {
        $_.Message -like "*$TaskName*"
    } | Select-Object TimeCreated, Id, LevelDisplayName, Message
    $result = [ordered]@{
        captured_at = (Get-Date).ToUniversalTime().ToString('o')
        succeeded = $true
        worker_sid = $worker.SID.Value
        worker_is_administrator = $false
        granted_right = 'SeBatchLogonRight'
        task_state = [string]$task.State
        last_run_time = $taskInfo.LastRunTime.ToUniversalTime().ToString('o')
        last_task_result = $taskInfo.LastTaskResult
        task_events = @($taskEvents)
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
if (-not $result.succeeded) { exit 1 }
