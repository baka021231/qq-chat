# 架构与开发说明

本文面向修改桥接代码的人，包括 AI 工具。功能和限制见 `docs/FEATURES.md`，需求待办与验收标准见 `docs/BACKLOG.md`，Codex 会话细节见 `docs/APPSERVER-MIGRATION.md`。

## 总览

一个 asyncio 进程，用 WebSocket 连接 NapCat（OneBot 11），默认用 Unix socket 连接本机共享的 Codex App Server；也可按 profile 使用 Claude Code 或主人单 agent Grok 子进程。后台是否执行由 agents.back 决定，代码支持不等于所有部署都开启分离模式。

```
NapCat ──WebSocket──▶ Bridge.session()  事件分发、过滤、去重、记录群聊
                         ├ /stop、/tasks、/front → 控制协程（不排队）
                         │ queue（容量 8）
                         ▼
                      Bridge.worker()    命令、附件、图片、长度检查（单个 worker，逐条处理）
                         │
                         ▼
                      Bridge.chat()
                         ├ instructions()  prompts.py + workspace/SOUL.md + workspace/OWNER.md
                         ├ agent.prepare() 恢复或新建会话
                         ├ turn_text()     拼接旧历史、群聊上下文
                         ├ /task → TaskManager → 后台 Agent → report_task()
                         └ run_turn()      agent.run(TurnRequest) → Codex AppServer / Claude 或 Grok JSONL
                               ├ 进度 → Codex memory 门控（适用时）→ relay_progress() ─┐
                               └ 回复 → relay_answer()  ──┤（BubbleStream 切分）
                                                          ▼
NapCat ◀──WebSocket── Bridge.send()  全局锁，间隔随机 2～3 秒，每条 1600 字
```

## 模块

程序都在 `qq_chat/` 包里，入口是 `qq_chat/__main__.py`，用 `python3 -m qq_chat` 在项目根目录启动。包内模块之间用相对导入（`from .files import ...`）。

| 模块 | 职责 | 包内依赖 |
| --- | --- | --- |
| `bridge.py` | `main()` 入口与 `Bridge` 类：OneBot 收发、消息过滤、权限、命令、会话、调度 | 下面全部 |
| `codex_app.py` | `AppServer`：JSON-RPC 客户端、单轮对话事件处理；`progress_text` 生成进度文本、传递执行活动事件 | `agents.base`、`memory_progress` |
| `agents/base.py` | `Agent`、`TurnRequest`、角色/权限常量和返回值上限；不负责 QQ 策略 | 无 |
| `agents/codex.py` | `CodexAgent`：线程准备/恢复、状态查询、归档、模型目录与底层 turn 调用 | `agents.base`、`codex_app`、`models` |
| `agents/__init__.py` | 构建 Codex/Claude/Grok profile，Codex profile 共用 transport | `agents.codex`、`agents.claude`、`agents.grok`、`agents.config` |
| `agents/grok.py` | 主人单 agent Grok JSONL、私有 ACP 输入、会话/模型/中断；仅 CHAT/FULL | `agents.base`、`agents.claude`（MIME 与进程清理工具）、`prompts` |
| `agents/claude.py` | Claude JSONL、NONE/FRONT/FULL 启动、工具列表检查、回复校验、进程清理 | `agents.base` |
| `agents/config.py` | profile 引用、权限模式、模型目录及阶段范围校验 | 无 |
| `agents/state.py` | 模型偏好迁移与隔离、历史导入预算 | 无 |
| `tool_server.py` | 每轮授权、Unix socket、工具 schema/执行、范围校验与真实回执 | `bridge`（运行时校验）、`memory` |
| `diary_migration.py` | 离线一次性迁移固定日记来源，保留原件并校验冲突；不参与查询 | `memory`、标准库 |
| `memory.py` | 在可信范围目录内做有预算的关键词检索，直接返回来源片段 | 标准库 |
| `memory_progress.py` | 识别直接记忆查询，按真实 Codex 活动事件缓冲/释放进度 | 标准库 |
| `front_tools.py` | 标准库 MCP stdio shim，经令牌转发工具调用 | 标准库 |
| `tasks.py` | TaskManager：持久任务、接纳上限、后台执行、写锁、中断与保守重启善后 | `agents.base`、`prompts` |
| `task_commands.py` | TaskCommands：会话锁、手动命令、可补发回报与共享 deliver | `tasks`、`agents.base`、`prompts` |
| `prompts.py` | 公共权限/角色提示词与历史拼接；本轮记忆命令说明由 Bridge 补入 | `moderation` |
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
  - Agent 编排：`instructions`、`turn_request`、`thread_for`、`turn_text`、`run_turn`、`relay_progress`、`relay_answer`、`chat`
  - 调度：`worker`

## 权限模型

所有权限都来自 `owner_request(source, owner)`，它只比较数字 QQ 号。结果决定三件事：

1. 请求的权限级别：单 agent 主人 `FULL`，分离模式 Claude 主人前台 `FRONT`、Codex 主人前台 `NONE`（手动分派）；访客及回报轮次 `NONE`；`CodexAgent.prepare()` 维持原 sandbox：`danger-full-access` 或 `read-only`
2. 使用哪套指令：`prompts.OWNER` 或 `prompts.GUEST`（`prompts.instructions`）
3. 能否使用管理命令、发图片和附件（`worker`、`receive_*`、`incoming_text`）

主人在群里时，会话键加上 `:owner` 后缀，与群友的线程分开，避免群友的历史消息进入有操作权限的线程。

`private_whitelist` 只扩大文字私聊的接收范围；主人判断仍只认 `owner_qq`。白名单访客使用独立的 `private:<QQ>` 会话、非主人指令和只读 sandbox，无工具、媒体及管理权限。群聊拉黑协议仅用于群聊，不注入访客私聊。

修改权限相关代码时，必须同时更新 `tests/test_command_auth.py` 和 `tests/test_message_policy.py` 中的对应测试。

## 群聊用户动态拉黑

`moderation.blocked_group_sender()` 在接收、worker 和发送端检查 `group_user_blacklist`；主人按真实 QQ 号豁免。仅触发机器人的群友消息才交给 `obvious_abuse()` 预检，命中后直接由 `Bridge.auto_block()` 保存并丢弃，不调用模型、不进入群聊参考记录。后续 worker 再检查一次，防止之前排队的消息继续回复。

模型侧只收到一个固定的当前发言人拉黑标记协议；它返回该标记时，worker 从 `Source.sender_id` 获取拉黑对象，不解析模型或用户提供的 QQ 号，不发送标记。主人命令可显式添加和解除名单；群友不能使用这些命令，模型也没有获得配置写权限。

`moderation.update_blacklist()` 使用 `.group-blacklist.lock` 文件锁，重新读取配置、合并单个设置，再以 600 权限写临时文件并原子替换。不要提交配置或锁文件。配置错误/磁盘故障时，自动路径先在内存拉黑并记日志，保持静默；重启后若未落盘则不保留，应检查日志。

## 一轮对话的组装

发给 Codex 的内容分两部分：

- **developerInstructions**：`prompts.OWNER` / `GUEST` + `SPEAKER_LABELS` + `WORKSPACE_RULES` + `workspace/SOUL.md`、`workspace/OWNER.md` 中存在的文件全文（依次加载，但 OWNER.md 只在 authorized=True 时读取）。每次 `thread/start` 或 `thread/resume` 时重新读取并传入，缺失则跳过，下一轮生效。AGENTS.md 不由桥接读取或注入，保留 Codex 按 cwd 原生加载。
- **本轮输入**，依次为：
  1. 群聊参考记录（`group_reference`，仅群聊）
  2. 旧历史（`history_import`，仅线程第一次运行，且该会话有旧记录时）
  3. 发言人标签 + 正文（`speaker_message`）；带附件或图片时，还会附上保存路径的说明
  4. 图片本身，作为 `localImage` 单独传入

## 数据库（`chat.sqlite3`）

| 表 | 用途 |
| --- | --- |
| `tasks` | 任务来源、目标、执行与回报状态；writes 冻结，orphaned 写任务继续占锁；receipt_sent 记录真实回执 |
| `task_inputs` | 运行任务的追加要求，下一轮消费标记；重启后不自动执行 |
| `seen` | 已处理的消息 ID，保留 7 天 |
| `threads` | 会话键 → Codex 线程 ID |
| `conversation_models` | 旧 Codex 偏好的兼容表，迁移后仍同步默认 Codex 的选择 |
| `profile_models` | 会话键 + profile → 模型及强度；Claude 不读取 Codex 偏好 |
| `agent_sessions` / `old_agent_sessions` | 非旧默认 Codex 的会话映射和 `/new` 归档，按 profile 隔离 |
| `agent_metadata` / `agent_profile_types` | 固定旧 Codex 迁移身份并记录 profile 类型，防止配置切换后误迁移或复用异类会话 |
| `old_threads` | `/new` 归档的旧 Codex 映射 |
| `bootstrap` | 已导入过旧历史的线程 |
| `history` | 成功交付的问答记录；新会话补入有预算的最近历史，/new 删除本会话记录；不是长期记忆档案 |
| `group_context` | 每群最近 100 条文字消息 |

建表和字段迁移写在 `Bridge.__init__`，用 `CREATE TABLE IF NOT EXISTS`。新增表或字段时沿用这个方式，旧数据库可以直接升级。

## 外部依赖

- Python 3（当前使用 3.14），第三方库只有 `websockets`
- NapCat（systemd 用户服务 `napcat.service`），在 `ws_url` 提供 OneBot 11 WebSocket
- Codex CLI（`codex_path`），由桥接运行 `codex app-server daemon start` 启动守护进程
- systemd 用户服务 `qq-bridge.service`：`ExecStart=/usr/bin/python3 -u -m qq_chat`，工作目录为项目根目录（`-m` 依赖它找到包）

## 常见改动

当前边界：`Bridge.agents` 持有各 profile，`Bridge.agent` 是主人前台便捷入口，`agent_for(source)` 按真实身份选择前台或访客 profile；Bridge 不再依赖 `self.app`。`TurnRequest` 的 `role=CHAT/TASK`、`level=FULL/FRONT/NONE`、人设全文和模型选择由 Bridge 组装。先 `prepare(req)` 得到线程，再拼接旧历史与群聊上下文，最后通过 `run(req)` 执行。请求额外包含 `session_id`（已准备的线程）、`show_tools`（进度显示开关）及 `on_activity`（Codex 执行活动回调），避免 run 再次恢复线程或重复读取人设。

`session_id(key)` 只查已有映射，供 `/session` 使用；`model_state(thread)` 提供接口返回的实际模型与强度；`models()` 和 `default_model()` 封装原模型目录及临时线程解析。`reset(key)` 仅在当前数据库事务中归档、移除当前 profile 的会话映射，Bridge 继续清除 history 并统一提交，模型选择不清除。适配器借用 Bridge 的数据库，不负责关闭它。

当前支持 back 为空的单 agent，或一个后台 profile；分离模式与工具通道见下文 PR3/PR4。PR2 当时仅允许空 back，是历史阶段限制，不是现行校验规则。`AppServer.turn()` 的事件处理、前缀一致性及异常语义保留，Codex 返回上限仍在适配器。新会话历史统一由 `recent_history()` 从最近 20 条向前取，每条截到 1000 字，JSON 序列化后不超过 8000 字，再恢复时间顺序；数据库原记录不截断。

Claude NONE 在桥接的 `run/claude/<profile>/` 空目录运行，safe-mode 加两个禁用记忆变量，不携带 MCP；FULL 使用实际 workspace 工作目录，正常加载项目规则，不添加隔离开关。人设写入 600 权限临时提示文件，NONE 替换系统提示、FULL 追加；每轮 snapshot=off，结束删除文件。子进程以新进程组启动，stdout 上限 16 MiB、stderr 并行排空，异常或取消依次 SIGINT/SIGTERM/KILL 清理；同一会话由适配器锁串行运行。显式停止与保守重启恢复已经实现，见下文后台调度章节。

Claude 工具列表未校验通过前不接受回复；完整聚合顶层 assistant 文本，忽略子 agent 文本和 thinking，检查 `<invoke` / `<system-reminder` 等标记后才回放到气泡回调。`result` 失败、缺失、异常退出或可疑标记均不发缓冲文字。`TurnRequest.history_text` 承载验证后的全文，Bridge 仅为主人正常成功回复暂存该值、在 worker 落库时取出；访客仍保存实际发送的受限文本。模型返回值上限与全文历史是两个不同用途。

`agent_sessions` 在 prepare 时持久化 UUID；CLI 明确表示找不到该会话且没有初始化执行时，抛 `MissingSession`，Bridge 只重试一次：归档旧映射、分配新会话、重新导入预算内历史。其他错误不自动重试，防止重复执行工具。

启动时把旧模型记录幂等复制到固定的 Codex profile。该身份写入 agent_metadata 后不因更换 front 改变；同一个 profile 名禁止更换后端类型，需使用新名字，避免复用旧会话或偏好。旧表保留并同步默认 Codex 写入，其他 profile 不回写旧表。

模型管理由 `Bridge.model_command()` 执行，worker 先检查主人权限，处理函数再次检查。命令验证 agent 返回的目录后保存选择；`CodexAgent.prepare()` 在创建/恢复时传入选择，`run_turn()` 还会通过请求在每次 `turn/start` 明确传入所选模型及推理强度，避免已加载线程忽略恢复覆盖。Codex 适配器缓存接口返回的实际模型状态；Claude 在首次 init 前仅有配置别名，之后缓存实际返回型号，未设置偏好时用于显示；已设置时显示下一轮使用的型号，不把尚未生成的选择冒充为已完成的切换。空线程可能尚无 rollout，遇到该特定错误时重建映射，历史表不清空。

- **加一个命令**：在 `worker()` 的 if/elif 中加一个分支。默认只有主人可用；如果要对所有人开放，把命令加进开头的白名单元组 `('/ping', '/help')`。同时在 `/help` 文字和 `docs/FEATURES.md` 的命令表里补上，并在 `tests/test_command_auth.py` 加测试。
- **改提示词**：改 `prompts.py`。桥接注入的人设放入 `workspace/SOUL.md`，主人资料放入 `workspace/OWNER.md`；开发规则留在 Codex 原生加载的 `workspace/AGENTS.md`。文件不自动迁移，均属本地忽略文件。
- **加配置项**：用 `self.cfg.get('key', 默认值)` 读取，让旧配置文件也能工作；同时更新 `config.example.json` 和 README 的配置表。
- **加一种消息类型**：在 `incoming_text()` 中解析，在 `Incoming` 中加字段，在 `worker()` 中处理。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

测试全部离线运行，不连接 QQ，也不调用模型。常见写法：

- 用 `Bridge.__new__(Bridge)` 跳过 `__init__`，手动设置 `cfg`、`db`（`sqlite3.connect(':memory:')`）和需要的属性。
- 把 `bridge.agents['codex']` 换成 `FakeAgent`，记录 `TurnRequest` 并用事件控制运行/失败；需要检查底层 JSON-RPC 参数时，把 `bridge.agents['codex'].app` 换成假的 App Server 对象。
- 把 `bridge.send` 换成记录发送内容的函数。
- 需要文件系统时，用 `patch('qq_chat.bridge.BASE', 临时目录)`。

## 开发约定

- 仓库是公开的。不要提交 `config.json`、数据库、日志、`workspace/`，代码里也不要出现 QQ 号、token 或个人信息；按人设置的值放进配置。
- 改功能时，在同一个提交里更新 `docs/FEATURES.md` 和 `docs/CHANGELOG.md`。
- 改完先跑测试再提交；部署时运行 `python3 tools/restart_bridge.py`；出问题时用 `git revert` 回滚。

### 会话推理强度（仅主人）

`/effort` 查询当前模型、强度和支持档位；`/effort 档位` 设置；`/effort default` 恢复当前模型目录默认强度。Codex 按实时目录、Claude 按配置目录校验，不支持的档位或查询失败不更改原设置。按会话和 profile 存入 `profile_models`，旧 Codex 表保留兼容，下一条消息生效，重启和 `/new` 保留，只影响当前主人会话。未选择模型时首次设置会固定当前会话模型；再次 `/model` 切换会重置为所选模型默认强度。

## PR3：后台调度与回报

`Bridge` 继承 `TaskCommands`，构造时创建 `TaskManager`。前台仍由一个 worker 串行消费容量 8 的队列；session 对主人 `/stop`、`/tasks` 直接创建控制协程，非主人静默。后台 runner 属于 TaskManager，不随 QQ session 断开而取消。`back: []` 不创建后台执行。

每个任务以 `task:编号` 为执行键，使用 TASK/FULL、WORKER、自足目标、profile 默认模型，不携带 SOUL/OWNER 或聊天历史。Codex task 用独立持久线程，仅内存映射和 tasks.session_id，不覆盖 threads 中的聊天映射。配置只开放一个后台且并发为 1，多后台放到 PR5。

调度用 Condition 检查 tasks 中 running/stopping/orphaned 的 profile 及持久 writes 占用，再原子标记 running。各 profile 占用和全局写占用同时满足才执行。接纳上限同时计入 queued，容量满立即拒绝。正常完成、已确认失败/中断释放容量；超时先请求中断再等待实际结局，RPC 报错回 running；Codex 连接失去或其他无法确认的执行保守进入 orphaned。

重启恢复仅执行数据库状态转换：queued → interrupted，running/stopping → orphaned，不访问旧线程或进程、不发信号、不轮询。写 orphaned 占用按创建时的 writes 判定，防止改配置绕过锁；只有原会话 `/task release` 在主人确认后可释放。旧 runtime_ref 自动核对方案保留在 FRONT-BACK 第 13 节，不在当前代码中。

会话锁按 conversation key 建立，对同一 asyncio task 可重入；chat、模型/强度命令、/new、回报均持锁，worker 持锁直到交付和历史落库。前台与回报不会并发操作同一线程。控制命令和后台调度不取会话锁。

回报还有独立的 task id 锁，避免完成与重连补报同时重复生成/发送。先在会话锁内用前台 CHAT/NONE 生成非流式文字并持久化 report_text；失败使用真实状态的降级摘要；orphaned 始终由桥接固定警告。再由 deliver 走原发送/语音/历史路径，所有 OneBot 气泡确认成功才记 reported=1；已有文字补发不调用模型。/new 不取消任务，回报时重新取当前线程；系统回报首行先于历史参考，防止被导入包装遮住。部分气泡成功后断线可能补发重复，采用至少一次交付。

PR4 前置小修复：TaskManager.check 按原会话校验，再调用 CodexAgent.check_execution，只发一次 thread/read(includeTurns=true)，10 秒超时不重试。PR3 每个任务仅一轮；缺少精确 turn_id 且返回多轮时未知，不误判其他轮次。查询不改变状态或锁。orphaned 固定通知说明 check、终端核实及人工 release。排队取消直接说明未执行。

## PR4：前台工具授权

tool_server 绑定 run/tools.sock，目录 700、socket 600，拒绝替换普通文件或活 socket。Bridge.chat 对已核验主人分两路生成逐轮 ToolGrant：back 非空的 Claude FRONT 可用完整七工具；back 为空且 memory 启用的 FULL 主人只取得 memory_search 授权（通过命令入口，不改变内置工具集）。工具服务在配置后台或启用 memory 时启动；MCP 配置包含 socket 和随机令牌，权限 600，令牌绑定 Source/会话并限时，finally 撤销。工具请求独立协程处理，不取会话锁，避免前台 await agent.run 时死锁。访客、报告轮次不取得 grant。

Claude FRONT 不加 safe-mode，设置两个禁用记忆环境变量、空 cwd、空内置工具、显式 MCP 和 dontAsk；init.tools 精确等于 FRONT_TOOLS，工具缺失和多出都失败，运行中也检查白名单。CHAT 全轮缓冲检查伪调用及固定回执标记后才交付。NONE/FULL 原规则保留；本机版本隔离证据见 FRONT-BACK 14 节，不由 tools 为空推断 hooks 或插件关闭。

shim 用 python -I 的绝对路径启动，不导入包；tools/list 向桥接取动态后台枚举与 schema。动作参数不接受来源、QQ 或会话字段，来源只认令牌。任务工具仅查当前会话，返回有长度限制的摘要；poke 根据绑定 Source 调用本机 NapCat send_poke，不能指定第三人。任务/动作日志记录工具名、来源类型及结局，不记录授权串或任意正文；memory_search 直接走只读检索，不记录查询及结果正文。

创建任务与 receipt_sent=0 同步落库，桥接发送真实回执并确认后置 1，失败不丢任务，工具仍返回真实编号和 pending 回执状态；重连先补回执。旧任务迁移默认 1。send_to_task 写 task_inputs，任务完成一轮后在原 profile/写占用下续接同一 session，使用创建时的总执行期限。Codex 启动每轮时记录 runtime_ref；人工 check 使用精确 turn_id，防止续轮后拿别的轮次推断。

## Grok 单 agent 接入

主人明确选择与当前 Codex 相同的操作权限、关闭后台的最简模式。配置类型为 grok_build，仅准用于 owner front；guest 不可引用，back 不可包含 Grok，Grok 前台时 back 必须为空。每个 profile 明确 permission_mode=bypassPermissions 和 sandbox=off，避免配置名义与实际命令不一致；Bridge 的身份校验不改，访客仍使用既有 Claude NONE。

GrokAgent 使用现有 agent_sessions/old_agent_sessions/profile_models 存储，旧 Codex 迁移身份不变。每轮 `--session-id` 或 `--resume`，输出 streaming-messages-json，关闭子 agent 自动派生。启动校验 session、权限模式、核心文件/终端工具，以及不加载 qq 派单工具；不支持 NONE/FRONT/TASK 的请求在启动前拒绝。

Grok 续接时会缓存首次系统规则，单纯更换 --agent 文件未能刷新旧人设。因此命令行只传恒定公开 BRIDGE_RULES；动态 SOUL/OWNER、工作台规则、用户输入和图片内容块每轮存入 600 权限 prompt JSON，CLI --prompt-file 读入，finally 删除。每轮公开系统约定采用 current_bridge_rules，已用两轮合成设定及公开历史词验证更新与续接同时生效。图片为原生 ACP image/data/mimeType 格式，不把 base64 或个人文本放进 argv。

只解析已校验 init 之后的顶层 assistant text；忽略 thinking、签名和子 agent 数据，不对原始 stderr 做日志输出。整轮成功且未含伪调用/回执才交给现有回调，保留最大返回值与完整历史侧通道。原生 result.cancelled 或本轮发过中断且进程未成功结束时抛 Interrupted；普通失败不重试，防止重复执行。取消/超时清理本轮进程组及提示文件。模型目录使用明确配置，本机 models 实测当前默认且可用 grok-4.7；不继承旧 Codex 的模型/effort。

## 独立于模型的前台切换

`Bridge.session()` 直接分发 `/front` 至 `front_switch.command()`，按真实 Source 校验主人，不获取会话锁、不进入 worker。`/front` 列出通过现有 `settings()` 校验的 profile；切换只修改 `agents.front`，旧配置隐含的 guest 会先固定为原值，避免访客跟随切换。每个 profile 的线程和模型状态仍由现有数据库保存。

命令先生成 0600 私有任务记录、发送 QQ 确认，再经 systemd-run 创建独立用户服务（2 秒后执行），避免桥接停止时连同重启任务一起被杀死。`python -m qq_chat.front_switch --job UUID` 重新读取和校验最新配置，与黑名单写入共享文件锁，合并前台字段，原子保存两份私有备份及新配置。重复切换被持久任务状态和进程内锁拦截；记录超过 300 秒可再次操作，服务最长执行 240 秒。

独立任务调用 `tools/restart_bridge.py`，检查服务 PID 变化、NapCat 重连日志和持续存活，然后直接私聊回报，不依赖前台模型。失败时仅在当前配置仍与本任务写入值完全一致时恢复快照并复验，避免覆盖并发人工修改。若恢复或通知失败，私有任务文件保留状态供诊断。需要可用的 systemd 用户服务和原有 OneBot 连接。

## 手动记忆与会话上下文的边界

- `history` 与 `group_context` 是聊天续接/参考；`memory` 是独立本地文件，聊天history不会自动变成记忆。日记和详细日志直接写入 memory/private/diary，查询直接搜索 memory，不复制或刷新副本。
- `ToolServer.call` 按真实 Source 决定 root 下的 shared/private/当前群目录，模型只能传 query。memory-only 授权的 tools/list 也只含 memory_search，不能借它派单或拍一拍。
- `tools/memory_search.py` 只读取本轮 grant 并请求 Unix 服务；token 不进入 argv、日志或返回值，轮次结束撤销并删除授权文件。Codex 在 turn/start 前用 thread/inject_items 注入当前轮记忆命令，避免已加载线程恢复时沿用旧授权路径；注入失败不开始该轮。FULL 仍保留其他文件工具，不能将此范围检查说成系统沙箱。
- `memory.search` 以目录描述符逐层打开，NOFOLLOW 并排除硬链接/特殊文件；每文件最多3个不同片段，含有限相邻上下文，同分时正文优先于Markdown标题；最多5条、单条500字、正文共2000字。关键词检索不等于语义检索，扫描预算与 partial 语义见 MEMORY.md。
- Codex 主人单 agent 有 memory grant 时，Bridge.run_turn 安装 MemoryProgress；AppServer 在转发进度前传递真实 item 活动事件。纯记忆调用丢弃缓冲，其他或未知工具释放缓冲；最终回答、失败和停止不经过该过滤。
- 日期记录由主人会话按本地规则手动维护，现有夜间整理器只整理日记、详细日志和索引，不自动监听全部聊天或提取事实。主题更新规则已保存，但主题写入、纠正/遗忘流程尚未实现。本机已完成正式目录切换，旧原件及两类旧副本已按主人授权删除；服务保留旧副本排除逻辑，详情见 MEMORY.md。

当前检索协议与验收边界以 [MEMORY.md](MEMORY.md) 为准，代码变更历史看 CHANGELOG。

## 引用与合并转发

`message_references.py`识别reply/forward数组段，在worker内用已有OneBot通道获取get_msg/get_forward_msg及有界内联/嵌套节点。Incoming新增references，事件接收与入队保留这些段，但不在WebSocket接收循环await获取，否则无法处理对应echo回执。普通群消息仍沿用触发规则；静默卡片只留占位，不主动展开。

引用结果按当前来源校验get_msg范围，并以JSON参考区块置于真实speaker_message之前，署名不授予身份权限；参考图片沿用现有主人图片下载路径，群友仅文字，文件/语音/视频不下载。管理命令检查用当前直接文字，不看参考正文。节点、调用数、深度、文本与超时预算见FEATURES；日志不写引用正文。参考获取失败以提示进入当前轮，原文字请求仍可处理。

## 群聊参考增量位置

`group_context_cursors`以(conversation, profile, session_id)为键保存last_id。group_reference读取id大于已处理位置、小于当前触发context_id的最近有限消息，保留40条/12000字上限。成功完成模型轮次后与bootstrap同事务推进到触发context_id，不用生成结束时的群消息最大ID，因此生成期间收到的消息不会漏掉；失败不推进。主人与群友会话、不同群/profile/session各自隔离，数据库持久化支持重启续用。

首次无位置使用原有近期参考；显式/new清除当前会话/profile的位置，并写入空session_id的重置起点（/new消息的context_id，没有消息ID的内部调用才取当前群最大ID），之后新session不重放重置前记录。旧群上下文和其他会话位置不删除。
