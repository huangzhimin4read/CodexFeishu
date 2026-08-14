[CmdletBinding()]
param(
    [string]$PythonPath = 'python.exe',
    [string]$ApplicationRoot = 'C:\ProgramData\CodexFeishuBridge\app',
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [Parameter(Mandatory = $true)]
    [string]$RuntimeRoot
)

$ErrorActionPreference = 'Stop'
$PythonPath = (Get-Command -Name $PythonPath -ErrorAction Stop).Source
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$stdout = Join-Path $RuntimeRoot "remote-service-$stamp.stdout.log"
$stderr = Join-Path $RuntimeRoot "remote-service-$stamp.stderr.log"
$arguments = @(
    '-B', '-m', 'codex_feishu_bridge', 'run', '--config',
    [System.IO.Path]::GetFullPath($ConfigPath)
)
$process = Start-Process -FilePath $PythonPath -ArgumentList $arguments `
    -WorkingDirectory ([System.IO.Path]::GetFullPath($ApplicationRoot)) `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
    -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 5
if ($process.HasExited) {
    $details = if (Test-Path $stderr) {
        (Get-Content -LiteralPath $stderr -Raw -Encoding utf8).Trim()
    } else { '' }
    throw "remote service exited during startup with $($process.ExitCode): $details"
}
$result = [ordered]@{
    started = $true
    process_id = $process.Id
    started_at = (Get-Date).ToUniversalTime().ToString('o')
    config_path = [System.IO.Path]::GetFullPath($ConfigPath)
    application_root = [System.IO.Path]::GetFullPath($ApplicationRoot)
    stdout_log = $stdout
    stderr_log = $stderr
}
$json = $result | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText(
    (Join-Path $RuntimeRoot 'remote-service-launch.json'),
    $json + "`n",
    [System.Text.UTF8Encoding]::new($false)
)
[System.IO.File]::WriteAllText(
    (Join-Path $RuntimeRoot 'topic-group-service.pid'),
    [string]$process.Id + "`n",
    [System.Text.UTF8Encoding]::new($false)
)
$json
