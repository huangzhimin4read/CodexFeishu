[CmdletBinding()]
param(
    [string]$Profile = "codex-feishu-owner",
    [switch]$NonInteractive,
    [switch]$RequireUserIdentity
)

$ErrorActionPreference = "Stop"

function Find-LarkCli {
    foreach ($name in @("lark-cli.cmd", "lark-cli")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            return $command.Source
        }
    }
    return $null
}

function Confirm-Step([string]$Prompt) {
    if ($NonInteractive) {
        return $false
    }
    $answer = Read-Host "$Prompt [Y/n]"
    return [string]::IsNullOrWhiteSpace($answer) -or $answer -match "^(y|yes|是)$"
}

if ([string]::IsNullOrWhiteSpace($Profile)) {
    throw "飞书 CLI profile 不能为空。"
}

Write-Host "CodexFeishu 可选能力：使用飞书官方 lark-cli 以已授权用户身份同步用户发言。"
Write-Host "CLI 会在本机管理 OAuth；安装器不会读取、显示或写入账号令牌。"

$larkCli = Find-LarkCli
if ($null -eq $larkCli) {
    Write-Warning "未检测到飞书官方 lark-cli。"
    Write-Host "官方安装命令：npm install -g @larksuite/cli"
    $install = Confirm-Step "现在安装飞书 CLI 吗？"
    if ($install) {
        $npm = Get-Command npm -ErrorAction SilentlyContinue
        if ($null -eq $npm) {
            throw "未检测到 npm。请先安装 Node.js，再重新运行本步骤。"
        }
        & $npm.Source install -g "@larksuite/cli"
        if ($LASTEXITCODE -ne 0) {
            throw "飞书 CLI 安装失败（npm exit $LASTEXITCODE）。"
        }
        $larkCli = Find-LarkCli
        if ($null -eq $larkCli) {
            throw "npm 已完成，但 PATH 中仍找不到 lark-cli；请重新打开终端后重试。"
        }
    } elseif ($RequireUserIdentity) {
        throw "用户身份同步已设为必需，但飞书 CLI 尚未安装。"
    } else {
        Write-Host "已跳过。机器人通知仍可使用；用户身份同步保持关闭。"
        return
    }
}

Write-Host "已检测到飞书 CLI：$larkCli"
& $larkCli --version

if ($NonInteractive) {
    Write-Host "请在交互终端依次完成："
    Write-Host "  lark-cli --profile $Profile config init --new"
    Write-Host "  lark-cli --profile $Profile auth login --recommend"
    Write-Host "  lark-cli --profile $Profile auth status --json --verify"
    return
}

if (Confirm-Step "为 profile '$Profile' 配置飞书开放平台应用吗？") {
    & $larkCli --profile $Profile config init --new
    if ($LASTEXITCODE -ne 0) {
        throw "飞书 CLI 应用配置失败（exit $LASTEXITCODE）。"
    }
}

if (Confirm-Step "现在登录需要同步发言的飞书账号吗？") {
    & $larkCli --profile $Profile auth login --recommend
    if ($LASTEXITCODE -ne 0) {
        throw "飞书 CLI 用户授权失败（exit $LASTEXITCODE）。"
    }
}

Write-Host "正在验证飞书 CLI 用户身份与令牌状态……"
& $larkCli --profile $Profile auth status --json --verify
if ($LASTEXITCODE -ne 0) {
    if ($RequireUserIdentity) {
        throw "飞书 CLI 身份验证未通过。"
    }
    Write-Warning "飞书 CLI 身份尚未就绪；机器人通知仍可使用。"
    return
}

Write-Host "飞书 CLI 已就绪。请在私有 runtime 配置中设置："
Write-Host '  user_message_identity = "lark_cli_user"'
Write-Host "  lark_cli_profile = `"$Profile`""
