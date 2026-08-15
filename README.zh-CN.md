# Codex 飞书（Lark）桥接

简体中文 | [English](README.md)

Codex 飞书（Lark）桥接是一个由单一 owner 在 Windows 本机运行的 OpenAI Codex 与飞书（Lark）连接服务。它可以把 Codex 任务输出同步到飞书/Lark 私有项目群的话题中；在逐项显式授权后，也可以把 owner 从飞书发送的文本、图片、文件、审批操作和控制命令准确送回对应的 Codex 任务。

项目采用 fail-closed 控制面：身份、群、回复树、任务绑定、消息投递结果、Codex 可执行文件、协议 schema 或审批结果只要不能确定，就拒绝、暂停或进入对账，不自行猜测。

> **Alpha 阶段：**当前代码适合开发和受控的单 owner 本机试运行，不是托管服务、多用户机器人平台，也不代表已经通过生产认证。

## 主要能力

- **按项目和任务组织话题：**项目活动后按需创建私有项目群，每个 Codex 任务对应一个飞书话题。
- **任务生命周期与标题同步：**Codex 任务改名后，在飞书仍允许编辑根消息时，直接更新原飞书/Lark 话题（`任务名称|项目名称`），不会新建重复话题。Codex 任务归档后立即停止该任务的双向桥接、撤销远程权限；根消息仍可编辑时，把同一话题标记为 `【已归档】`；任务重新激活后恢复实时绑定。飞书因编辑时间窗或编辑次数拒绝更新时，桥接器会持久记录为标题投影受阻，不会循环重试。
- **可靠出站：**SQLite 持久 outbox、稳定 UUID、重试分类、投递对账、死信和熔断器。
- **双向消息镜像：**Codex 中由 owner 输入的文本、图片和文件标签同步到对应飞书话题。文本可通过官方 `lark-cli` 以已授权的飞书 owner 身份发出；启动时强制核对 CLI 用户 Open ID 与 `owner_open_id`，不一致即拒绝启用。桥接器按飞书消息 ancestry 抑制代发回调，并覆盖发送与接收的竞态窗口，代发内容不会重新进入 Codex 成为新指令。回退为机器人发送时会使用配置的 owner 显示名明确标注用户发言。飞书注入 Codex 的同一用户 item 按持久 `thread + turn + item` 身份抑制回流，不会重复提交或重复显示。
- **适合手机阅读：**同步过程消息和 final；发送项目内 Markdown 图片及 Codex 可见图像；文件引用只显示 `🔗【文件名】`，不暴露本地路径；普通链接只显示可读标签。
- **明确区分等待状态：**Codex 自动继续运行时，过程消息保持普通样式；当前回合结束后，单独发送 `🔔【等待你的回应】`，在飞书/Lark 话题末尾和手机预览中都能直接看出任务已经停下。
- **严格入站路由：**派发前核对 owner、tenant、app、群、话题根、回复后代、任务 epoch、项目根目录和能力授权。
- **远程输入：**文本、图片和文件分别授权。推荐路径先使用 Codex CLI；若 Codex Desktop 已经持有任务 writer，则改由该桌面 writer 提交，并继续核验持久化的 rollout turn ID 与 user-item ID 后才报告成功。附件受大小与格式限制，校验并哈希后写入选定项目的 inbox，不自动执行或解压。
- **真实提交状态：**只有核验到精确的 Codex 用户回合后，飞书/Lark 才显示“已提交 Codex”；两条 writer 路径都不能确认时，消息保持排队或未确认，不虚报送达。飞书消息后的空心圈属于客户端原生已读状态，桥接器和普通飞书开放接口都不能将它消除。
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
Codex CLI resume -> 持久化用户回合 -> 精确任务
```

桥接器只在本机运行，不开放公共 webhook，也不扫描任意项目目录。项目和任务身份来自已配置的 Codex 状态，所有可写项目根目录仍必须在 allowlist 内。

## 运行要求

- Windows 10/11 或 Windows Server，具备 PowerShell 和任务计划程序
- Python 3.11 或更高版本
- 已安装的 Codex CLI/App Server 可执行文件
- 如需以本人身份同步 Codex 用户发言：安装飞书官方 `lark-cli` 并完成对应账号授权；安装引导会检测该依赖，并依次提示安装、配置、登录和验证
- 已创建并发布的飞书自建应用和机器人，以及租户管理员批准的所需权限
- 使用 Windows 凭据管理器保存飞书 App Secret
- 使用 `delivery = "desktop"` 时保持 Codex 桌面处于已登录的交互会话
- 只有兼容模式 `delivery = "app_server"` 需要另一个非管理员 Windows 账户

飞书权限、回调订阅、限流和响应合同可能变化。示例合同只能作为模板，实际启用前必须根据当前租户开发者后台导出结果重新核对。

飞书/Lark 目前没有向机器人或官方 `lark-cli` 开放“让某个用户订阅/取消订阅某一个话题”的受支持操作。用户在话题中以本人身份实际回复后，飞书会自然订阅该话题；因此，新话题根消息确认后，桥接器会通过已核验的 owner `lark-cli` 身份发送一条可见的 `🔔 已订阅任务更新` 回复，以受支持的方式完成自动订阅，并在进入 Codex 前对这条回调去重。Codex 任务归档时，桥接器能够可靠执行的是停止流量、撤销权限和标记已归档；如需让该话题从飞书的订阅列表中消失，仍需在飞书客户端点击“取消订阅”。

飞书还把消息编辑限制在企业管理员设定的时间窗内，并规定每条消息最多编辑 20 次。话题标题就是根消息，因此较老或频繁改名的话题可能已经无法通过受支持的接口改名。任务绑定和归档撤权仍会正常生效；桥接器会记录飞书拒绝，不会另建一个重复话题替代。

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

远程文本、图片、文件、审批和控制是五个独立开关，只有明确配置后才会启用。推荐使用 `delivery = "cli"`：无人持有任务时通过 `codex exec resume` 写入；Codex Desktop 已经持有 writer 时，仅针对这个明确冲突改走桌面输入面，并在 rollout 中核验 turn ID 与 user-item ID 后才确认。`desktop` 仍可作为显式选择的 UI 自动化方案，App Server 兼容模式仍要求独立 Windows worker 身份。

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

Broker 安装程序默认会进入飞书 CLI 安装与授权引导（只有明确不启用本人身份同步时才使用 `-SkipLarkCliSetup` 跳过）。同一引导也可单独运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_lark_cli.ps1 -Profile codex-feishu-owner
```

它只安装官方 `@larksuite/cli`，并调用 `config init --new`、`auth login --recommend` 与 `auth status --json --verify`。OAuth 令牌由飞书 CLI 在本机管理，不写入本仓库。若跳过该步骤，机器人通知仍可使用，但不能以飞书本人身份同步 Codex 中的用户发言。

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
