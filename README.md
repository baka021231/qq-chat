# QQ 聊天桥接

NapCat OneBot 11 与本机 Codex 会话之间的桥接。

## 目录

| 路径 | 用途 |
| --- | --- |
| `qq_chat/` | 桥接程序包，用 `python3 -m qq_chat` 启动 |
| `qq_chat/bridge.py` | 消息接收、权限、会话与回复调度 |
| `qq_chat/codex_app.py` | Codex App Server 客户端 |
| `qq_chat/agents/` | Codex/Claude 适配器、profile 校验和状态迁移 |
| `qq_chat/prompts.py` | 发给模型的提示词（权限指令、历史与群聊上下文、附件和图片说明） |
| `qq_chat/reply.py` | QQ 回复分段与流式发送 |
| `qq_chat/voice.py` | 独立语音队列、合成进程协议和音频校验 |
| `tools/voice_backend.py` | 隔离 Python 环境中的常驻 ONNX 合成适配器 |
| `qq_chat/files.py`、`qq_chat/images.py` | 文件和图片接收 |
| `tests/` | 离线回归测试 |
| `tools/` | 当前可用的连接检查、重启工具 |
| `docs/FEATURES.md` | 功能说明：能做什么、限制、不支持的功能、已知问题 |
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
| `show_tool_progress` | 进度中是否推送工具调用摘要，默认 `false` |
| `voice` | 可选本地语音回复，默认关闭；启用后正常回复先发文字，再补对应语音。配置见 `docs/VOICE.md` |
| `agents` | 可选单 agent profile 配置；省略保持 Codex，PR2 只允许空 back 列表；Codex profile 省略 path 时继承 codex_path |

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
