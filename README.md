# QQ 聊天桥接

NapCat OneBot 11 与本机 Agent 会话之间的桥接，支持 Codex、Claude Code 和主人单 agent Grok。

## 目录

| 路径 | 用途 |
| --- | --- |
| `qq_chat/` | 桥接程序包，用 `python3 -m qq_chat` 启动 |
| `qq_chat/bridge.py` | 消息接收、权限、会话与回复调度 |
| `qq_chat/codex_app.py` | Codex App Server 客户端 |
| `qq_chat/agents/` | Codex/Claude/Grok 适配器、profile 校验和状态迁移 |
| `qq_chat/prompts.py` | 发给模型的提示词（权限指令、历史与群聊上下文、附件和图片说明） |
| `qq_chat/memory.py`、`qq_chat/memory_progress.py` | 有范围限制的关键词检索、Codex 纯记忆查询静默进度 |
| `tools/memory_search.py` | 主人单 agent 经本轮授权调用记忆工具的命令入口 |
| `qq_chat/reply.py` | QQ 回复分段与流式发送 |
| `qq_chat/voice.py` | 独立语音队列、合成进程协议和音频校验 |
| `tools/voice_backend.py` | 隔离 Python 环境中的常驻 ONNX 合成适配器 |
| `tools/indextts_backend.py` | 独立 Linux CUDA 环境中的 V5 IndexTTS 合成适配器 |
| `qq_chat/files.py`、`qq_chat/images.py` | 文件和图片接收 |
| `tests/` | 离线回归测试 |
| `tools/` | 当前可用的连接检查、重启工具 |
| `docs/FEATURES.md` | 功能说明：能做什么、限制、不支持的功能、已知问题 |
| `docs/MEMORY.md` | 手动记忆检索：目录范围、授权、结果限制与验收边界 |
| `docs/FRONT-BACK.md` | 分阶段设计和历史实测记录；当前实现看 FEATURES/ARCHITECTURE |
| `docs/VOICE.md` | 本地角色语音安装、解耦接口、实测与启用方法 |
| `docs/BACKLOG.md` | 需求待办：目标、验收标准和待定事项 |
| `docs/ROADMAP.md` | 远景小地图：语音、前后台协作与希望实现的体验 |
| `docs/ARCHITECTURE.md` | 架构与开发说明：模块、权限模型、数据库、常见改动、测试写法 |
| `docs/APPSERVER-MIGRATION.md` | Codex 会话映射与终端接续 |
| `docs/CHANGELOG.md` | 变更记录 |
| `setup/` | NapCat 安装脚本 |
| `config.example.json` | 配置模板 |

以下内容只在本机，已被 `.gitignore` 排除：

| 路径 | 用途 |
| --- | --- |
| `config.json`、`chat.sqlite3`、`qq_bridge.log`、`.bridge-restart.lock` | 运行时配置、聊天数据和日志 |
| `archive/` | 引入 git 前的备份、一次性脚本和执行记录 |
| `workspace/` | QQ 工作台：Codex 原生规则 `AGENTS.md`、桥接注入 `SOUL.md` / `OWNER.md` 与用户项目 |

配置和聊天数据属于私人数据，不应提交或公开。

## 配置

复制 `config.example.json` 为 `config.json` 后填写：

| 键 | 说明 |
| --- | --- |
| `ws_url`、`onebot_token` | NapCat OneBot WebSocket 地址和 token |
| `codex_path` | `codex` 可执行文件路径 |
| `owner_qq` | 主人 QQ，只有它可以授权操作 |
| `private_whitelist` | 允许文字私聊的其他 QQ 号，默认空；不授予工具、文件、图片或管理命令权限，主人始终允许 |
| `allow_all_groups` | `true` 时响应机器人加入的所有群，此时忽略 `allowed_groups`；省略时也按 `true` 处理 |
| `allowed_groups` | `allow_all_groups` 为 `false` 时才生效的群号白名单 |
| `mention_qq` | 群聊回复这些 QQ 号的消息时自动 @ 对方 |
| `group_user_blacklist` | 群聊用户 QQ 号列表，默认空；跨群静默忽略，动态拉黑会保存，主人豁免。主人用 `/blacklist` 查看、`/block QQ号` 添加、`/unblock QQ号` 解除 |
| `show_tool_progress` | 是否推送普通工具调用摘要，默认 false；已识别的纯记忆命令摘要不显示 |
| `memory.enabled` | 手动记忆检索开关，默认 true；false 禁止查询并不为 FULL 轮次发放记忆授权，详见 docs/MEMORY.md |
| `voice` | 可选本地语音回复，默认关闭；启用后正常回复先发文字，再补对应语音。配置见 `docs/VOICE.md` |
| `agents` | 可选 profile 配置；省略保持 Codex；back 可空或指定一个后台；Codex profile 省略 path 时继承 codex_path |

不论哪种群设置，群聊都需要 @ 机器人或喊触发词才会回复。

## Claude 单 agent 配置

本机已验证 Claude Code 2.1.311；需先用官方 `claude auth login` 登录，不把凭据写入桥接配置。以下是 `agents` 字段示例，路径按本机修改：

```json
"agents": {
  "front": "claude",
  "guest": "claude-chat",
  "back": [],
  "profiles": {
    "claude": {
      "type": "claude_code",
      "path": "/home/<user>/.local/bin/claude",
      "model": "sonnet",
      "models": ["haiku", "sonnet", "opus"],
      "permission_mode": "dontAsk",
      "max_turns": 6
    },
    "claude-chat": {
      "type": "claude_code",
      "path": "/home/<user>/.local/bin/claude",
      "model": "haiku",
      "models": ["haiku"],
      "max_turns": 2
    }
  }
}
```

主人前台为 FULL，必须显式设置权限模式；`dontAsk` 会拒绝需要额外许可的操作，不能保证所有编码工具请求都能执行。访客无论 profile 怎么配置都为 NONE，safe-mode、禁用 CLAUDE.md/自动记忆及空工具校验共同生效，不取得主人的权限。FULL 不启用这些隔离开关，正常读取项目规则。

Claude 回复完整校验后才按气泡发出，因此没有即时首字；Codex 流式保留。`/models` 对 Claude 读取配置目录，model 项也可用对象形式声明 `defaultReasoningEffort` 和 `supportedReasoningEfforts`；未声明档位时 `/effort` 不允许切换，不从 CLI 通用列表推断模型支持情况。新 profile 不继承其他 profile 的模型名。更多边界见 `docs/FRONT-BACK.md` 和 `docs/ARCHITECTURE.md`。

### 前后台分离配置（含 PR3 手动入口）

在已有 profiles 中选择一个后台，增加以下配置（示例将 `codex` 作为后台；保留自己的 front/guest 选择）：

```json
{
  "agents": {
    "front": "claude",
    "guest": "claude-guest",
    "back": ["codex"],
    "profiles": {
      "claude": {"type": "claude_code", "path": "claude", "model": "sonnet", "models": ["sonnet"], "permission_mode": "dontAsk"},
      "claude-guest": {"type": "claude_code", "path": "claude", "model": "haiku", "models": ["haiku"]},
      "codex": {"type": "codex_app", "sandbox": "danger-full-access", "writes": true, "max_parallel": 1}
    }
  },
  "tasks": {"max_running": 4, "max_writers": 1, "max_per_conversation": 3, "timeout_minutes": 60, "progress": "direct", "report_chars": 4000}
}
```

配置与顶层 codex_path 等原配置合并；没有 agents 或 back 为空时维持单 agent 行为。当前只开放一个后台 profile；Claude 工具前台可通过 MCP 派发，Codex 前台仍使用 `/task 完整目标` 手动入口。后台看不到聊天记录，需要把要求写全。用 `/tasks` 查看当前会话任务、`/stop` 停前台、`/stop 编号` 或 `/stop all` 停后台。群任务可设置 tasks.progress=silent。

重启后未开始的任务记为 interrupted，运行过的记为 orphaned 并通知；写 orphaned 持续占锁。自行确认旧执行确实结束后，在原会话用 `/task release 编号` 释放。不自动探测旧进程，不根据重启本身认定已停止。报告位于 workspace/tasks/编号.md，QQ 断线后可补发。

## 记忆检索

已有只读 `memory_search`，直接返回关键词匹配的片段、相对文件名和行号。
只查 `workspace/memory/`：主人私聊可读 shared/private，群内主人可读
shared/当前群目录；访客不取得检索授权。记忆文件属于本地私人数据，不提交仓库。

FULL 单 agent 每轮由桥接提供带限时授权的命令入口；Claude FRONT 使用
qq MCP。Codex 原生 MCP 配置不变。单 agent Codex 只查记忆时默认静默，
开始执行非记忆工作后恢复进度；Claude/Grok 的输出分流未做同样改造。

检索不会自动保存事实、同步旧日记或每轮自动调用；按主题维护“当前有效偏好/决定”
仍是后续设计。`/new` 清当前聊天上下文和 history，不删除 memory 文件。
手工复制的旧记录只是快照。工具权限不是 FULL 文件工具的操作系统沙箱。
详见 [记忆检索说明](docs/MEMORY.md)。

## 常用操作

在项目根目录运行离线测试（不会发送 QQ 消息或请求模型）：

```sh
python3 -m unittest discover -s tests -v
```

检查 QQ 连接（使用现有配置登录，不发送聊天消息）：

```sh
python3 tools/verify_onebot.py
```

查看服务状态：

```sh
systemctl --user status qq-bridge.service
```

手动在前台运行（调试用；先停止服务，避免两个实例同时回复）：

```sh
systemctl --user stop qq-bridge.service
python3 -m qq_chat
```

需要重启时，在当前回复发送完成后运行：

```sh
python3 tools/restart_bridge.py
```

## 模型选择（仅主人）

- `/models`：读取当前账号的可选模型，包含必要的图片能力提示。
- `/model`：查询当前会话模型设置；未设置时查询接口的当前型号。
- `/model 模型名`：校验可选列表并保存当前会话选择，下一条消息生效，保留聊天记录。
- `/model default`：解析并切换到 Codex 当前配置的默认模型。

模型选择按会话及 profile 保存在 `chat.sqlite3` 的 `profile_models` 表，旧 Codex 的 `conversation_models` 表保留兼容；重启和 `/new` 后保留，只影响当前主人会话。群友和私聊白名单访客不能查询或切换模型。切换时采用该模型目录中给出的默认推理强度，不修改全局 Codex 配置。若默认模型不在可选目录中，命令会拒绝切换。

## 版本管理

代码用 git 管理。修改前确认工作区干净，改完运行测试再提交；改功能时在同一个提交里更新 `docs/FEATURES.md` 和 `docs/CHANGELOG.md`；出问题时用 `git revert` 回滚后重启。不再新建 `archive/backups/` 备份。

新增测试放入 `tests/`，维护工具放入 `tools/`，避免在根目录堆积临时文件。

## 历史文件

本机的 `archive/` 保留引入 git 前的旧文件；其中脚本的相对路径和报告中的绝对路径反映整理前的布局，不作为当前维护入口。不要直接运行旧激活脚本，以免恢复过时版本。移动清单在 `archive/organization-manifest.json`。

### 会话推理强度（仅主人）

`/effort` 查询当前模型、强度和支持档位；`/effort 档位` 设置；`/effort default` 恢复当前模型目录默认强度。Codex 按实时目录、Claude 按配置目录校验，不支持的档位或查询失败不更改原设置。按会话和 profile 存入 `profile_models`，旧 Codex 表保留兼容，下一条消息生效，重启和 `/new` 保留，只影响当前主人会话。未选择模型时首次设置会固定当前会话模型；再次 `/model` 切换会重置为所选模型默认强度。

## 工作台设定加载

桥接每轮按顺序读取 `workspace/SOUL.md`、`workspace/OWNER.md`，将存在的文件注入 developerInstructions；缺失文件跳过，不回退读取 AGENTS.md，也不自动创建或迁移文件。SOUL.md 对所有会话注入；OWNER.md 仅对桥接核验的主人私聊和群内主人会话注入，访客不会读取该文件。`workspace/AGENTS.md` 仅由 Codex 按工作目录原生加载，桥接不再手动注入。

### Claude 工具前台（PR4）

完整示例见 `config.front-back.example.json`：主人前台 `claude_front` 使用 sonnet，访客 `claude_guest` 使用 haiku，后台 codex。合并现有私密配置并校验，主人确认部署后才重启；不要把示例的占位参数当真实登录信息。

分离模式下 Claude 主人前台经 MCP 使用七个工具：派发、任务列表、状态、停止、追加要求、拍一拍当前主人，以及有范围限制的记忆检索；任务真实建立后桥接直接发编号回执。访客无工具。`/task` 手动入口保留，Codex 前台仍用手动入口。`/task check 编号` 只读查询一次、不自动放锁；确认结束后 `/task release 编号`。FRONT 本机隔离证据和启动耗时调查见设计第 14 节，两个性能实验开关未写入配置。

### Grok 主人单 agent

Grok Build 已登录后，可在 profiles 新增 `grok`，并设 front 为 grok、guest 仍为 claude_guest、back 为空：

```json
{"type":"grok_build","path":"/home/<user>/.local/bin/grok","model":"grok-4.7","models":["grok-4.7"],"permission_mode":"bypassPermissions","sandbox":"off","max_turns":100,"timeout_seconds":3600}
```

该模式让主人前台直接读写文件和执行命令，权限范围与原 Codex 主人单 agent 相同；不赋予 root 或 Windows 写权限。模型与会话独立保存，/new、/model、/stop 保留；访客不可选择 Grok。本机已验证续接、每轮人设更新、图片与正在运行命令的停止。回复完整校验后再发送，不即时逐 token 输出。缺失会话不自动重试，必要时用 /new 重建。配置修改前备份，校验后按授权重启。
