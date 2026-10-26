# 架构与开发说明

本文面向修改桥接代码的人，包括 AI 工具。功能和限制见 `docs/FEATURES.md`，Codex 会话细节见 `docs/APPSERVER-MIGRATION.md`。

## 总览

一个 asyncio 进程，用 WebSocket 连接 NapCat（OneBot 11），用 Unix socket 连接本机共享的 Codex App Server 守护进程。

```
NapCat ──WebSocket──▶ Bridge.session()  事件分发、过滤、去重、记录群聊
                         │ queue（容量 8）
                         ▼
                      Bridge.worker()    命令、附件、图片、长度检查（单个 worker，逐条处理）
                         │
                         ▼
                      Bridge.chat()
                         ├ instructions()  prompts.py + workspace/SOUL.md + workspace/OWNER.md
                         ├ thread_for()    恢复或新建 Codex 线程
                         ├ turn_text()     拼接旧历史、群聊上下文
                         └ run_turn()      AppServer.turn()
                               ├ 进度 → relay_progress() ─┐
                               └ 回复 → relay_answer()  ──┤（BubbleStream 切分）
                                                          ▼
NapCat ◀──WebSocket── Bridge.send()  全局锁，间隔随机 2～3 秒，每条 1600 字
```

## 模块

| 模块 | 职责 | 项目内依赖 |
| --- | --- | --- |
程序都在 `qq_chat/` 包里，入口是 `qq_chat/__main__.py`，用 `python3 -m qq_chat` 在项目根目录启动。包内模块之间用相对导入（`from .files import ...`）。

| 模块 | 职责 | 包内依赖 |
| --- | --- | --- |
| `bridge.py` | `main()` 入口与 `Bridge` 类：OneBot 收发、消息过滤、权限、命令、会话、调度 | 下面全部 |
| `codex_app.py` | `AppServer`：JSON-RPC 客户端、单轮对话事件处理；`progress_text` 生成进度文本 | 无 |
| `prompts.py` | 发给模型的全部提示词与拼接函数 | 无 |
| `reply.py` | `BubbleStream`：把流式回复切成 QQ 气泡 | 无 |
| `files.py` | 安全保存附件（不跟随符号链接、限制大小、不覆盖） | 无 |
| `images.py` | 获取并校验图片（域名白名单、文件头识别格式） | `files` |
| `moderation.py` | 群聊用户黑名单、明确滥用检测、群友输出限制、加锁原子保存配置 | 无 |
| `models.py` | 分页读取可见模型、解析 Codex 配置的默认型号 | 无 |

`bridge.py` 编排其他模块，`prompts.py` 从 `moderation.py` 引入内部拉黑标记。新功能如果不需要访问 `Bridge` 的状态，优先在包里写成独立模块；从 `bridge.py` 拆出功能时也放在包里。运行时文件（`config.json`、`chat.sqlite3`、`workspace/`）在项目根目录，由 `bridge.BASE` 指向。

## `bridge.py` 的结构

- **模块级纯函数**：`incoming_text()` 把 OneBot 事件转成 `Incoming` 或丢弃；`owner_request()` 判断权限；`conversation_key()` 决定会话键；`speaker_message()` 生成发言人标签。这几个函数不涉及 I/O，可以直接测试。
- **`Bridge` 的方法**，按职责分组：
  - 存储：`__init__`（建表与迁移）、`remember_group`、`group_reference`
  - OneBot 收发：`send` / `_send`、`onebot_call`、`session`、`run`
  - 媒体：`receive_attachments`、`receive_images`
  - Codex 编排：`instructions`、`thread_for`、`turn_text`、`run_turn`、`relay_progress`、`relay_answer`、`chat`
  - 调度：`worker`

## 权限模型

所有权限都来自 `owner_request(source, owner)`，它只比较数字 QQ 号。结果决定三件事：

1. Codex 的 sandbox：`danger-full-access` 或 `read-only`（`thread_for`）
2. 使用哪套指令：`prompts.OWNER` 或 `prompts.GUEST`（`prompts.instructions`）
3. 能否使用管理命令、发图片和附件（`worker`、`receive_*`、`incoming_text`）

主号在群里时，会话键加上 `:owner` 后缀，与群友的线程分开，避免群友的历史消息进入有操作权限的线程。

`private_whitelist` 只扩大文字私聊的接收范围；主号判断仍只认 `owner_qq`。白名单访客使用独立的 `private:<QQ>` 会话、非主号指令和只读 sandbox，无工具、媒体及管理权限。群聊拉黑协议仅用于群聊，不注入访客私聊。

修改权限相关代码时，必须同时更新 `tests/test_command_auth.py` 和 `tests/test_message_policy.py` 中的对应测试。

## 群聊用户动态拉黑

`moderation.blocked_group_sender()` 在接收、worker 和发送端检查 `group_user_blacklist`；主号按真实 QQ 号豁免。仅触发机器人的群友消息才交给 `obvious_abuse()` 预检，命中后直接由 `Bridge.auto_block()` 保存并丢弃，不调用模型、不进入群聊参考记录。后续 worker 再检查一次，防止之前排队的消息继续回复。

模型侧只收到一个固定的当前发言人拉黑标记协议；它返回该标记时，worker 从 `Source.sender_id` 获取拉黑对象，不解析模型或用户提供的 QQ 号，不发送标记。主号命令可显式添加和解除名单；群友不能使用这些命令，模型也没有获得配置写权限。

`moderation.update_blacklist()` 使用 `.group-blacklist.lock` 文件锁，重新读取配置、合并单个设置，再以 600 权限写临时文件并原子替换。不要提交配置或锁文件。配置错误/磁盘故障时，自动路径先在内存拉黑并记日志，保持静默；重启后若未落盘则不保留，应检查日志。

## 一轮对话的组装

发给 Codex 的内容分两部分：

- **developerInstructions**：`prompts.OWNER` / `GUEST` + `SPEAKER_LABELS` + `WORKSPACE_RULES` + `workspace/SOUL.md`、`workspace/OWNER.md` 中存在的文件全文（依次加载）。每次 `thread/start` 或 `thread/resume` 时重新读取并传入，缺失则跳过，下一轮生效。AGENTS.md 不由桥接读取或注入，保留 Codex 按 cwd 原生加载。
- **本轮输入**，依次为：
  1. 群聊参考记录（`group_reference`，仅群聊）
  2. 旧历史（`history_import`，仅线程第一次运行，且该会话有旧记录时）
  3. 发言人标签 + 正文（`speaker_message`）；带附件或图片时，还会附上保存路径的说明
  4. 图片本身，作为 `localImage` 单独传入

## 数据库（`chat.sqlite3`）

| 表 | 用途 |
| --- | --- |
| `seen` | 已处理的消息 ID，保留 7 天 |
| `threads` | 会话键 → Codex 线程 ID |
| `conversation_models` | 会话键 → 用户选择的模型和该模型默认推理强度，重启及 `/new` 时保留 |
| `old_threads` | `/new` 归档的旧映射 |
| `bootstrap` | 已导入过旧历史的线程 |
| `history` | 每轮的问答记录；现在只用于新线程导入（遗留） |
| `group_context` | 每群最近 100 条文字消息 |

建表和字段迁移写在 `Bridge.__init__`，用 `CREATE TABLE IF NOT EXISTS`。新增表或字段时沿用这个方式，旧数据库可以直接升级。

## 外部依赖

- Python 3（当前使用 3.14），第三方库只有 `websockets`
- NapCat（systemd 用户服务 `napcat.service`），在 `ws_url` 提供 OneBot 11 WebSocket
- Codex CLI（`codex_path`），由桥接运行 `codex app-server daemon start` 启动守护进程
- systemd 用户服务 `qq-bridge.service`：`ExecStart=/usr/bin/python3 -u -m qq_chat`，工作目录为项目根目录（`-m` 依赖它找到包）

## 常见改动

模型管理由 `Bridge.model_command()` 执行，worker 先检查主号权限，处理函数再次检查。命令验证目录后保存选择；`thread_for()` 在创建/恢复时传入选择，`run_turn()` 还会在每次 `turn/start` 明确传入所选模型及推理强度，避免已加载线程忽略恢复覆盖。`_thread_models` 仅缓存接口返回值，未设置偏好时用于状态显示；已设置时显示下一轮使用的型号，不把尚未生成的选择冒充为已完成的切换。空线程可能尚无 rollout，遇到该特定错误时重建映射，历史表不清空。

- **加一个命令**：在 `worker()` 的 if/elif 中加一个分支。默认只有主号可用；如果要对所有人开放，把命令加进开头的白名单元组 `('/ping', '/help')`。同时在 `/help` 文字和 `docs/FEATURES.md` 的命令表里补上，并在 `tests/test_command_auth.py` 加测试。
- **改提示词**：改 `prompts.py`。桥接注入的人设放入 `workspace/SOUL.md`，主人资料放入 `workspace/OWNER.md`；开发规则留在 Codex 原生加载的 `workspace/AGENTS.md`。文件不自动迁移，均属本地忽略文件。
- **加配置项**：用 `self.cfg.get('key', 默认值)` 读取，让旧配置文件也能工作；同时更新 `config.example.json` 和 README 的配置表。
- **加一种消息类型**：在 `incoming_text()` 中解析，在 `Incoming` 中加字段，在 `worker()` 中处理。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

测试全部离线运行，不连接 QQ，也不调用模型。常见写法：

- 用 `Bridge.__new__(Bridge)` 跳过 `__init__`，手动设置 `cfg`、`db`（`sqlite3.connect(':memory:')`）和需要的属性。
- 把 `bridge.app` 换成假的 App Server 对象，记录 `call` 和 `turn` 的参数，用来断言权限和提示词。
- 把 `bridge.send` 换成记录发送内容的函数。
- 需要文件系统时，用 `patch('qq_chat.bridge.BASE', 临时目录)`。

## 开发约定

- 仓库是公开的。不要提交 `config.json`、数据库、日志、`workspace/`，代码里也不要出现 QQ 号、token 或个人信息；按人设置的值放进配置。
- 改功能时，在同一个提交里更新 `docs/FEATURES.md` 和 `docs/CHANGELOG.md`。
- 改完先跑测试再提交；部署时运行 `python3 tools/restart_bridge.py`；出问题时用 `git revert` 回滚。

### 会话推理强度（仅主号）

`/effort` 查询当前模型、强度和支持档位；`/effort 档位` 设置；`/effort default` 恢复当前模型目录默认强度。按实时模型目录校验，不支持的档位或查询失败不更改原设置。沿用 `conversation_models` 保存，下一条消息生效，重启和 `/new` 保留，只影响当前主号会话。未选择模型时首次设置会固定当前会话模型；再次 `/model` 切换会重置为所选模型默认强度。
