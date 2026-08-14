# Codex 飞书桥接

简体中文 | [English](README.md)

Codex 飞书桥接是一个由单一 owner 在 Windows 本机运行的 OpenAI Codex 与飞书连接服务。它可以把 Codex 任务输出同步到飞书私有项目群的话题中；在逐项显式授权后，也可以把 owner 从飞书发送的文本、图片、文件、审批操作和控制命令准确送回对应的 Codex 任务。

项目采用 fail-closed 控制面：身份、群、回复树、任务绑定、消息投递结果、Codex 可执行文件、协议 schema 或审批结果只要不能确定，就拒绝、暂停或进入对账，不自行猜测。

> **Alpha 阶段：**当前代码适合开发和受控的单 owner 本机试运行，不是托管服务、多用户机器人平台，也不代表已经通过生产认证。

## 主要能力

- **按项目和任务组织话题：**项目活动后按需创建私有项目群，每个 Codex 任务对应一个飞书话题。
- **可靠出站：**SQLite 持久 outbox、稳定 UUID、重试分类、投递对账、死信和熔断器。
- **双向消息镜像：**Codex 中由 owner 输入的文本、图片和文件标签同步到对应飞书话题；飞书注入 Codex 的同一用户 item 按持久 `thread + turn + item` 身份抑制回流，不会重复提交或重复显示。
- **适合手机阅读：**同步过程消息和 final；发送项目内 Markdown 图片及 Codex 可见图像；文件引用只显示 `🔗【文件名】`，不暴露本地路径；普通链接只显示可读标签。
- **严格入站路由：**派发前核对 owner、tenant、app、群、话题根、回复后代、任务 epoch、项目根目录和能力授权。
- **远程输入：**文本、图片和文件分别授权。默认桌面投递路径先定位精确任务，再通过 Codex 应用自身的辅助功能输入面提交，桌面始终是唯一写入者。附件受大小与格式限制，校验并哈希后写入选定项目的 inbox，不自动执行或解压。
- **真实提交状态：**Codex 桌面输入器确认提交后，飞书显示“已提交 Codex”；如 steer 需要等待当前工具边界，桥接器会在精确 rollout user item 稍后出现时完成认领。桌面输入结果本身不明确时才保持未确认状态，不虚构人类式“已读”。
- **精确去重：**来源去重不依赖正文相同；rollout item、dispatch 记录、provider outbox 和飞书 UUID 共同保证重启及重试后的至多一次可见投递。
- **远程审批和控制：**短时、单次审批动作，以及受约束的状态、任务、profile、append、stop 和 hard-stop 命令。
- **Windows 身份隔离：**飞书凭据只属于 Broker 身份；Codex App Server 可通过另一个非管理员 worker 身份运行，并使用 ACL 与 Job Object 建立边界。
- **版本 Gate：**固定 Codex 可执行文件及稳定版/实验版 App Server schema，并校验哈希。

## 架构

```text
Codex Desktop 状态 / rollout 文件
              |
              v
   规范化 + 绑定核对 ----------> SQLite items/outbox/audit
                                             |
                                             v
                                    飞书 REST + WebSocket
                                             |
                                             v
                                      私有项目任务话题

飞书 owner 输入
      |
      v
身份/群/root/epoch/能力核对
      |
      v
Codex 桌面辅助功能 -> 桌面持有的写入者 -> 精确任务
```

桥接器只在本机运行，不开放公共 webhook，也不扫描任意项目目录。项目和任务身份来自已配置的 Codex 状态，所有可写项目根目录仍必须在 allowlist 内。

## 运行要求

- Windows 10/11 或 Windows Server，具备 PowerShell 和任务计划程序
- Python 3.11 或更高版本
- 已安装的 Codex CLI/App Server 可执行文件
- 已创建并发布的飞书自建应用和机器人，以及租户管理员批准的所需权限
- 使用 Windows 凭据管理器保存飞书 App Secret
- 使用 `delivery = "desktop"` 时保持 Codex 桌面处于已登录的交互会话
- 只有兼容模式 `delivery = "app_server"` 需要另一个非管理员 Windows 账户

飞书权限、回调订阅、限流和响应合同可能变化。示例合同只能作为模板，实际启用前必须根据当前租户开发者后台导出结果重新核对。

## 快速开始

```powershell
git clone https://github.com/huangzhimin4read/CodexFeishu.git
Set-Location CodexFeishu

py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[test]"

python -m pytest -q
python -m codex_feishu_bridge verify-config --config config/offline.example.toml
```

配置真实本机实例：

1. 把 `config/runtime.example.toml` 或 `config/runtime.topic-group.example.toml` 复制到已被 Git 忽略的 `.runtime` 目录。
2. 替换所有 `REPLACE_*` 字段；未经批准的端点和能力继续保持关闭。
3. 以对应的 `*.example.json` 为模板，导出并复核当前租户合同。
4. 在 Windows 凭据管理器中创建通用凭据，目标名称必须与 `credential_target` 一致。App Secret 不得写入 TOML、JSON、日志或证据。
5. 针对实际运行的 Codex 可执行文件生成本机协议基线：

   ```powershell
   python scripts/generate_codex_baseline.py --codex-executable "C:\path\to\codex.exe"
   ```

6. 启动前先验证配置和租户预检：

   ```powershell
   python -m codex_feishu_bridge verify-config --config .runtime/runtime.toml
   python -m codex_feishu_bridge preflight --config .runtime/runtime.toml --live
   python -m codex_feishu_bridge run --config .runtime/runtime.toml
   ```

远程文本、图片、文件、审批和控制是五个独立开关，只有明确配置后才会启用。飞书消息需要出现在 Codex 桌面任务时使用 `delivery = "desktop"`；App Server 兼容模式仍要求独立 Windows worker 身份。

## 目录说明

| 路径 | 内容 |
| --- | --- |
| `codex_feishu_bridge/` | 运行服务、协议适配、存储、安全控制和运维模块 |
| `config/*.example.*` | 默认 fail-closed 的配置与租户合同模板 |
| `generated/codex/` | 固定版本 Codex App Server schema 和兼容矩阵 |
| `plugins/codex-feishu/` | 可选 Codex 插件，用于受控的状态检查、验证、部署和诊断 |
| `scripts/` | 协议基线、证据、Windows 隔离和服务辅助脚本 |
| `tests/` | 单元、协议、路由、存储故障、图片和服务测试 |
| `SECURITY.md` | 信任边界与安全问题报告规则 |

## Codex 插件

本仓库同时也是一个公开的 Codex 插件 marketplace。可选的 `codex-feishu` 插件为 Codex 提供经过校验的管理 skill 和不暴露路径的只读健康检查；常驻消息传输仍由 Windows 计划任务服务负责。

可以直接把下面这句话交给 Codex：

```text
请从 GitHub 仓库 huangzhimin4read/CodexFeishu 添加插件 marketplace，安装并启用 codex-feishu 插件，验证安装状态后告诉我结果。
```

也可以在 PowerShell 中一行安装：

```powershell
codex plugin marketplace add huangzhimin4read/CodexFeishu --ref main; if ($LASTEXITCODE -eq 0) { codex plugin add codex-feishu@codex-feishu }
```

安装后新建一个 Codex 任务，使插件被加载。插件安装的是 Codex 管理工作流；桥接服务本身仍需按照下文完成仓库部署，并配置私有飞书凭据。

插件本身不包含凭据、租户 ID、真实运行配置或运行数据库。

## 不得公开的文件

`.gitignore` 已排除运行数据库、WAL、日志、证据、live TOML、租户后台导出、真实租户合同和本机内部记录。发布 fork 前还应扫描完整 Git 历史，不能只检查当前工作区。

## 开发与测试

```powershell
python -m pip install -e ".[test]"
python -m pytest -q
python -m compileall -q codex_feishu_bridge tests scripts
```

升级 Codex 或协议时，必须重新生成稳定版与实验版 schema，复核兼容矩阵，运行完整测试，并把新的可执行文件哈希作为新的发布 Gate。

提交改动前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题按 [SECURITY.md](SECURITY.md) 处理，不得在公开 issue 中放入真实 prompt、消息正文、token、租户导出或审批 payload。

## 许可证

[MIT](LICENSE)
