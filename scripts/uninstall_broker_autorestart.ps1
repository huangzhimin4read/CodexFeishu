[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$TaskName = 'CodexFeishu-Broker-Owner',
    [string]$RuntimeRoot = ''
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
    $RuntimeRoot = Join-Path (Split-Path -Parent $PSScriptRoot) '.runtime'
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    if ($PSCmdlet.ShouldProcess($TaskName, 'stop and unregister Broker auto-restart task')) {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        $pidPath = Join-Path ([System.IO.Path]::GetFullPath($RuntimeRoot)) 'topic-group-service.pid'
        if (Test-Path -LiteralPath $pidPath) {
            $brokerPid = [int](Get-Content -Raw -Encoding utf8 -LiteralPath $pidPath).Trim()
            $process = Get-CimInstance Win32_Process -Filter "ProcessId=$brokerPid"
            if (
                $null -ne $process -and
                $process.CommandLine -match 'codex_feishu_bridge' -and
                $process.CommandLine -match 'live-remote\.toml'
            ) {
                Stop-Process -Id $brokerPid -Force
            }
        }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
}
