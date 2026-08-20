# Codex 飞书（Lark）桥接

简体中文 | [English](README.md)

Codex 飞书（Lark）桥接是一个供单一用户在 Windows 本机运行的 OpenAI Codex 与飞书（Lark）连接服务。它把 Codex 任务输出同步到飞书/Lark 私有项目群的话题中，也可以把映射话题中的文本、图片、文件、审批操作和控制命令准确送回对应的 Codex 任务。

公开项目只提供这一种宽松本地模式，并直接使用当前 Windows 用户运行。路由以 tenant、app、项目、任务、群、消息和回复关系的稳定 ID 为准；机器人、项目、任务、群和用户显示名称改变都不会让服务停机。

> **Alpha 阶段：**当前代码面向一台 Windows 电脑上的单一可信用户，不是托管服务或多用户机器人平台。

## 主要能力

- **按项目和任务组织话题：**项目活动后按需创建私有项目群，每个 Codex 任务对应一个飞书话题。
- **任务生命周期与标题同步：**Codex 任务改名后，在飞书仍允许编辑根消息时，直接更新原飞书/Lark 话题（`任务名称|项目名称`），不会新建重复话题。Codex 任务归档后立即停止该任务的双向桥接、撤销远程权限；根消息仍可编辑时，把同一话题标记为 `【已归档】`；任务重新激活后恢复实时绑定。飞书因编辑时间窗或编辑次数拒绝更新时，桥接器会持久记录为标题投影受阻，不会循环重试。
- **可靠出站：**SQLite 持久 outbox、稳定 UUID、重试分类、投递对账、死信和熔断器。
- **双向消息镜像：**Codex 中的用户文本、图片和文件标签同步到对应飞书话题。官方 `lark-cli` 可以使用当前已就绪的任意授权用户配置发送文本，启动时不再把该账号与配置中的 Open ID 比较。桥接器通过消息 ancestry 和持久 `thread + turn + item` 身份阻止回调和飞书来源 item 循环或重复显示。
- **适合手机阅读：**同步过程消息和 final；发送项目内 Markdown 图片及 Codex 可见图像；文件引用只显示 `🔗【文件名】`，不暴露本地路径；普通链接只显示可读标签。项目内图片如果嵌在两段文字之间，会先完成上传，再按原位置组成一条飞书/Lark 富文本消息，不再拆成单独图片消息。
- **明确区分等待状态：**Codex 自动继续运行时，过程消息保持普通样式；当前回合结束后，单独发送 `🔔【等待你的回应】`，在飞书/Lark 话题末尾和手机预览中都能直接看出任务已经停下。
- **稳定 ID 入站路由：**映射私有话题中的任意真人用户都可提交输入；tenant、app、群、话题根、回复关系和任务 ID 决定目标，显示名称和发送者 Open ID 不作为授权门槛。
- **远程输入：**文本、图片和文件分别授权。推荐的 `cli` 模式只使用 Codex CLI；上行消息若在 60 秒内仍未形成已确认的 Codex 用户回合，就会被丢弃并停止重试，同时在原飞书/Lark 话题提示投递超时、请用户重新发送。该模式不会操作 Codex Desktop 输入框。附件受大小与格式限制，校验并哈希后写入选定项目的 inbox，不自动执行或解压。
- **真实提交状态：**只有核验到精确的 Codex 用户回合后，飞书/Lark 才显示“已提交 Codex”；60 秒期限内 writer 不可用时显示排队或未确认，期限届满则明确提示上行已丢弃，不虚报送达。飞书消息后的空心圈属于客户端原生已读状态，桥接器和普通飞书开放接口都不能将它消除。
- **精确去重：**来源去重不依赖正文相同；rollout item、dispatch 记录、provider outbox 和飞书 UUID 共同保证重启及重试后的至多一次可见投递。
- **远程审批和控制：**短时、单次审批动作，以及受约束的状态、任务、profile、append、stop 和 hard-stop 命令。
- **单一本机身份：**桥接器与 Codex writer 都使用当前 Windows 用户；`dangerFullAccess`、网络访问和 `approval_policy = "never"` 不再要求独立 worker 账号。

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

飞书/Lark 话题输入
      |
      v
tenant/app/群/root/任务 ID 路由
      |
      v
Codex CLI resume -> 持久化用户回合 -> 精确任务
```

桥接器只在本机运行，不开放公共 webhook。项目、任务、群和消息 ID 是权威身份，显示名称和项目路径作为可变元数据自动刷新。allowlist 只限定路由范围。

## 运行要求

- Windows 10/11 或 Windows Server，具备 PowerShell 和任务计划程序
- Python 3.11 或更高版本
- 已安装的 Codex CLI/App Server 可执行文件
- 如需以用户身份同步 Codex 用户发言：安装飞书官方 `lark-cli` 并让任意用户配置处于已授权、可用状态；安装引导会检测该依赖，并依次提示安装、配置、登录和验证
- 已创建并发布的飞书自建应用和机器人，以及租户管理员批准的所需权限
- 使用 Windows 凭据管理器保存飞书 App Secret
- 使用 `delivery = "desktop"` 或 `delivery = "desktop_relay"` 时保持 Codex 桌面处于已登录的交互会话

飞书权限、回调订阅、限流和响应合同可能变化。示例合同只能作为模板，实际启用前必须根据当前租户开发者后台导出结果重新核对。

飞书/Lark 目前没有向机器人或官方 `lark-cli` 开放“让某个用户订阅/取消订阅某一个话题”的受支持操作。用户在话题中以用户身份实际回复后，飞书会自然订阅该话题；因此，新话题根消息确认后，桥接器会通过当前已就绪的 `lark-cli` 用户配置发送一条可见的 `🔔 已订阅任务更新` 回复，并在进入 Codex 前对这条回调去重。Codex 任务归档时会停止流量并标记已归档；如需让该话题从订阅列表中消失，仍需在飞书客户端点击“取消订阅”。

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
2. 替换所有 `REPLACE_*` 字段。话题群示例默认启用宽松本地功能集；不需要的单项功能可自行关闭。
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

远程文本、图片、文件、审批和控制仍是五个独立功能开关；宽松话题群示例默认全部启用并自动批准。`delivery = "cli"` 通过 `codex exec resume` 写入；遇到 writer 锁时保留在持久队列。`delivery = "desktop_relay"` 把输入交给专用中转任务，只使用 `desktop_relay_thread_id` 标识，任务改名不会影响路由。桥接器通过本地任务 prompt 深链接预填该任务，并用本次唯一提示文字识别输入框，不使用全局键盘、剪贴板或前台激活；只有中转和目标 rollout item 都出现后才确认投递。`desktop` 保留为直接 UI 自动化方式，`app_server` 也直接使用当前 Windows 用户。

## 目录说明

| 路径 | 内容 |
| --- | --- |
| `codex_feishu_bridge/` | 运行服务、协议适配、存储、安全控制和运维模块 |
| `config/*.example.*` | 宽松单用户配置与租户合同模板 |
| `generated/codex/` | 固定版本 Codex App Server schema 和兼容矩阵 |
| `plugins/codex-feishu/` | 可选 Codex 插件，用于状态检查、验证、部署和诊断 |
| `scripts/` | 协议基线、证据、安装和服务辅助脚本 |
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
