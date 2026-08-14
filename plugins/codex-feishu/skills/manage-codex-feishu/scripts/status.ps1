param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryRoot,
    [string]$TaskName = "CodexFeishu-Broker-Owner",
    [int]$MaximumHealthAgeSeconds = 30
)

$ErrorActionPreference = "Stop"

function Add-Check {
    param(
        [System.Collections.Generic.List[object]]$Checks,
        [string]$Name,
        [bool]$Ok,
        [string]$Detail
    )
    $Checks.Add([ordered]@{ name = $Name; ok = $Ok; detail = $Detail })
}

$root = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$projectFile = Join-Path $root "pyproject.toml"
if (-not (Test-Path -LiteralPath $projectFile -PathType Leaf)) {
    throw "Repository does not contain pyproject.toml."
}
$projectText = Get-Content -LiteralPath $projectFile -Raw -Encoding utf8
if ($projectText -notmatch '(?m)^name\s*=\s*"codex-feishu-bridge"\s*$') {
    throw "Repository is not the codex-feishu-bridge project."
}

$checks = [System.Collections.Generic.List[object]]::new()

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$taskRunning = $null -ne $task -and [string]$task.State -eq "Running"
Add-Check $checks "scheduled_task" $taskRunning $(if ($null -eq $task) { "missing" } else { [string]$task.State })

$pidPath = Join-Path $root ".runtime\topic-group-service.pid"
$servicePid = $null
if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    $candidate = (Get-Content -LiteralPath $pidPath -Raw -Encoding utf8).Trim()
    if ($candidate -match '^\d+$') {
        $servicePid = [int]$candidate
    }
}
$pidAlive = $null -ne $servicePid -and $null -ne (Get-Process -Id $servicePid -ErrorAction SilentlyContinue)
Add-Check $checks "service_pid" $pidAlive $(if ($pidAlive) { "alive" } elseif ($null -eq $servicePid) { "missing" } else { "not_running" })

$healthPath = Join-Path $root ".runtime\topic-group-status.json"
$health = $null
if (Test-Path -LiteralPath $healthPath -PathType Leaf) {
    try {
        $health = Get-Content -LiteralPath $healthPath -Raw -Encoding utf8 | ConvertFrom-Json
    } catch {
        $health = $null
    }
}
Add-Check $checks "health_file" ($null -ne $health) $(if ($null -eq $health) { "missing_or_invalid" } else { "valid" })

$healthAge = $null
if ($null -ne $health -and $null -ne $health.generated_at) {
    try {
        $generatedAt = [DateTimeOffset]::Parse([string]$health.generated_at)
        $healthAge = [math]::Max(0, [int]([DateTimeOffset]::UtcNow - $generatedAt).TotalSeconds)
    } catch {
        $healthAge = $null
    }
}
$healthFresh = $null -ne $healthAge -and $healthAge -le $MaximumHealthAgeSeconds
Add-Check $checks "health_fresh" $healthFresh $(if ($null -eq $healthAge) { "unknown" } else { "age_seconds=$healthAge" })

$processRunning = $null -ne $health -and [string]$health.process_state -eq "running"
Add-Check $checks "process_state" $processRunning $(if ($null -eq $health) { "unknown" } else { [string]$health.process_state })

$connected = $null -ne $health -and [string]$health.remote_connection_state -eq "connected"
Add-Check $checks "remote_connection" $connected $(if ($null -eq $health) { "unknown" } else { [string]$health.remote_connection_state })

$breakerCount = if ($null -eq $health -or $null -eq $health.open_breakers) { $null } else { @($health.open_breakers).Count }
$breakersClear = $breakerCount -eq 0
Add-Check $checks "circuit_breakers" $breakersClear $(if ($null -eq $breakerCount) { "unknown" } else { "open=$breakerCount" })

$deadLetters = if ($null -eq $health -or $null -eq $health.dead_letters) { $null } else { [int]$health.dead_letters }
$deadLettersClear = $deadLetters -eq 0
Add-Check $checks "dead_letters" $deadLettersClear $(if ($null -eq $deadLetters) { "unknown" } else { "count=$deadLetters" })

$healthy = -not ($checks | Where-Object { -not $_.ok })
[ordered]@{
    healthy = $healthy
    task_name = $TaskName
    process_id = $servicePid
    checks = $checks
} | ConvertTo-Json -Depth 5

if (-not $healthy) {
    exit 1
}
