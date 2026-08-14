#Requires -RunAsAdministrator
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,

    [Parameter(Mandatory = $true)]
    [string]$CodexExecutable,

    [Parameter(Mandatory = $true)]
    [string]$CodexHome,

    [Parameter(Mandatory = $true)]
    [string[]]$ProjectRoots,

    [string]$WorkerName = 'CodexFeishuWorker',
    [string]$TaskName = 'CodexFeishu-Worker-Owner',
    [string]$ResultPath,
    [string]$DiagnosticPath,
    [string]$InstallRoot = (Join-Path $env:ProgramData 'CodexFeishuBridge')
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'windows_lsa_rights.ps1')
$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runtimeRoot = Join-Path $workspace '.runtime'
$launchFile = Join-Path $runtimeRoot 'isolated-worker-launch.json'
$pythonPath = (Resolve-Path -LiteralPath $PythonExecutable).Path
$codexPath = (Resolve-Path -LiteralPath $CodexExecutable).Path
$codexHomePath = (Resolve-Path -LiteralPath $CodexHome).Path
$resolvedProjects = @($ProjectRoots | ForEach-Object { (Resolve-Path -LiteralPath $_).Path })
$resolvedInstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$programDataRoot = [System.IO.Path]::GetFullPath($env:ProgramData).TrimEnd('\') + '\'
if (-not ($resolvedInstallRoot.TrimEnd('\') + '\').StartsWith(
    $programDataRoot,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw 'InstallRoot must stay below ProgramData.'
}
$applicationRoot = Join-Path $resolvedInstallRoot 'app'
$diagnosticRoot = Join-Path $resolvedInstallRoot 'worker-diagnostics'
$diagnosticFile = Join-Path $diagnosticRoot 'last-error.json'
$resolvedResultPath = if ($ResultPath) {
    [System.IO.Path]::GetFullPath($ResultPath)
} else {
    Join-Path $runtimeRoot 'worker-install-result.json'
}
$resolvedDiagnosticPath = if ($DiagnosticPath) {
    [System.IO.Path]::GetFullPath($DiagnosticPath)
} else {
    Join-Path $runtimeRoot 'worker-install-error.txt'
}

function Invoke-Icacls {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & icacls.exe @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "icacls failed with exit code $LASTEXITCODE for: $($Arguments -join ' ')"
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $workspace 'pyproject.toml') -PathType Leaf)) {
    throw 'The resolved workspace is not the CodexFeishu project.'
}
if (-not $TaskName.StartsWith('CodexFeishu-Worker-', [System.StringComparison]::Ordinal)) {
    throw 'TaskName must use the CodexFeishu-Worker- prefix.'
}
if (Get-LocalUser -Name $WorkerName -ErrorAction SilentlyContinue) {
    throw "Local account $WorkerName already exists; this installer will not rotate or reuse an unknown password."
}
if (Test-Path -LiteralPath $applicationRoot) {
    throw "Application staging root already exists: $applicationRoot"
}

if (-not $PSCmdlet.ShouldProcess(
    "$env:COMPUTERNAME\$WorkerName",
    'create a non-administrator worker account, apply ACLs, and register a hidden task'
)) {
    return
}

$random = $null
$passwordText = $null
$securePassword = $null
$workerSid = $null
$aclTargets = @()
try {
    $random = New-Object byte[] 36
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($random)
    }
    finally {
        $rng.Dispose()
    }
    $passwordText = [Convert]::ToBase64String($random) + 'aA1!'
    $securePassword = ConvertTo-SecureString $passwordText -AsPlainText -Force
    $worker = New-LocalUser -Name $WorkerName -Password $securePassword `
        -AccountNeverExpires -PasswordNeverExpires -UserMayNotChangePassword `
        -Description 'Codex App Server worker; no Feishu secret'
    $workerSid = $worker.Sid.Value
    Grant-CodexFeishuAccountRight -Sid $workerSid -Right 'SeBatchLogonRight'
    $brokerSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $qualifiedAccount = "$env:COMPUTERNAME\$WorkerName"
    $aclTargets = @(
        $codexHomePath,
        $resolvedProjects,
        (Split-Path -Parent $pythonPath),
        (Split-Path -Parent $codexPath)
    ) | ForEach-Object { $_ } | Select-Object -Unique

    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $applicationRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $diagnosticRoot -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $workspace 'codex_feishu_bridge') `
        -Destination $applicationRoot -Recurse
    & $pythonPath (Join-Path $workspace 'scripts\stage_runtime_dependencies.py') `
        --target $applicationRoot `
        --lock (Join-Path $workspace 'requirements-runtime.lock')
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to stage exact installed worker runtime dependencies.'
    }

    # The worker must be able to resume current Codex Desktop tasks and access
    # only explicitly selected project roots. It receives no ACL on bridge
    # runtime state, where provider credentials, approval keys and audit DB
    # references live.
    Invoke-Icacls $codexHomePath /grant:r "*$workerSid`:(OI)(CI)(M)" /T /C
    foreach ($project in $resolvedProjects) {
        Invoke-Icacls $project /grant:r "*$workerSid`:(OI)(CI)(M)" /T /C
    }
    Invoke-Icacls (Split-Path -Parent $pythonPath) /grant:r "*$workerSid`:(OI)(CI)(RX)" /T /C
    Invoke-Icacls (Split-Path -Parent $codexPath) /grant:r "*$workerSid`:(OI)(CI)(RX)" /T /C
    # Project-root recursion above also touches .runtime. Remove every worker
    # ACE below the control-plane root before establishing the broker-only ACL.
    Invoke-Icacls $runtimeRoot /remove:g "*$workerSid" /T /C
    Invoke-Icacls $runtimeRoot /inheritance:r
    Invoke-Icacls $runtimeRoot /grant:r `
        "*$brokerSid`:(OI)(CI)(F)" `
        '*S-1-5-18:(OI)(CI)(F)' `
        '*S-1-5-32-544:(OI)(CI)(F)'
    Invoke-Icacls $runtimeRoot /grant "*$workerSid`:(RX)"
    Invoke-Icacls $resolvedInstallRoot /inheritance:r
    Invoke-Icacls $resolvedInstallRoot /grant:r `
        "*$brokerSid`:(OI)(CI)(F)" `
        '*S-1-5-18:(OI)(CI)(F)' `
        '*S-1-5-32-544:(OI)(CI)(F)'
    Invoke-Icacls $resolvedInstallRoot /grant "*$workerSid`:(RX)"
    Invoke-Icacls $applicationRoot /grant:r "*$workerSid`:(OI)(CI)(RX)" /T /C
    Invoke-Icacls $diagnosticRoot /inheritance:r
    Invoke-Icacls $diagnosticRoot /grant:r `
        "*$brokerSid`:(OI)(CI)(F)" `
        "*$workerSid`:(OI)(CI)(M)" `
        '*S-1-5-18:(OI)(CI)(F)' `
        '*S-1-5-32-544:(OI)(CI)(F)'

    $xmlPath = Join-Path $runtimeRoot 'isolated-worker-task.xml'
    Push-Location -LiteralPath $workspace
    try {
        & $pythonPath -m codex_feishu_bridge render-worker-task `
            --launch-file $launchFile --account $qualifiedAccount `
            --working-directory $applicationRoot --diagnostic-file $diagnosticFile `
            --output $xmlPath
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to render the worker Scheduled Task definition.'
        }
    }
    finally {
        Pop-Location
    }
    $xml = Get-Content -LiteralPath $xmlPath -Raw -Encoding Unicode
    Register-ScheduledTask -TaskName $TaskName -Xml $xml `
        -User $qualifiedAccount -Password $passwordText -Force | Out-Null
    # Registration defaults do not allow the unelevated broker to query/run a
    # password-logon task. Grant only GenericRead + GenericExecute; task
    # modification and credential access remain Administrator/SYSTEM only.
    $scheduler = New-Object -ComObject 'Schedule.Service'
    $scheduler.Connect()
    $registeredTask = $scheduler.GetFolder('\').GetTask($TaskName)
    $taskSddl = "D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;GRGX;;;$workerSid)(A;;GRGX;;;$brokerSid)"
    $registeredTask.SetSecurityDescriptor($taskSddl, 0)

    $administratorsGroup = Get-LocalGroup -SID 'S-1-5-32-544'
    $administratorSids = @(Get-LocalGroupMember -Group $administratorsGroup | ForEach-Object {
        $_.SID.Value
    })
    if ($administratorSids -contains $workerSid) {
        throw 'The worker account unexpectedly belongs to the local Administrators group.'
    }
    $result = [pscustomobject]@{
        installed = $true
        worker_account = $qualifiedAccount
        worker_sid = $workerSid
        broker_sid = $brokerSid
        worker_is_administrator = $false
        scheduled_task_name = $TaskName
        launch_file = $launchFile
        worker_codex_home = $codexHomePath
        application_root = $applicationRoot
        password_persisted_only_by_task_scheduler = $true
    } | ConvertTo-Json -Compress
    $resultDirectory = Split-Path -Parent $resolvedResultPath
    New-Item -ItemType Directory -Path $resultDirectory -Force | Out-Null
    Set-Content -LiteralPath $resolvedResultPath -Value $result -Encoding utf8
    $result
}
catch {
    $diagnostic = $_ | Format-List * -Force | Out-String
    Set-Content -LiteralPath $resolvedDiagnosticPath -Value $diagnostic -Encoding utf8 `
        -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    if ($workerSid) {
        foreach ($target in $aclTargets) {
            & icacls.exe $target /remove "*$workerSid" /T /C 2>$null | Out-Null
        }
    }
    Remove-LocalUser -Name $WorkerName -ErrorAction SilentlyContinue
    if ((Test-Path -LiteralPath $applicationRoot) -and
        ($applicationRoot.StartsWith($programDataRoot, [System.StringComparison]::OrdinalIgnoreCase))) {
        Remove-Item -LiteralPath $applicationRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    throw
}
finally {
    $passwordText = $null
    $securePassword = $null
    if ($null -ne $random) {
        [Array]::Clear($random, 0, $random.Length)
    }
}
