# 前后台分离设计（草案）

本文是 `qq-chat` 前后台分离的实施方案，基于 `65361f4`（2026-10-27）的代码。可以放进 `docs/`，按第 11 节分阶段实现；每个阶段完成后，把对应内容写进 `docs/FEATURES.md`、`docs/ARCHITECTURE.md` 和 `docs/CHANGELOG.md`，本文只保留未完成部分。

v3：已按 2026-10-27 的两轮代码审阅意见修订，改动对照见第 14 节。

## 0. 结论

可以做，工作量中等。接入 Claude Code 本身不难，第 4 节的调用方式已经实测可用。真正要花时间的是三件事：调度从“单 worker 串行”改成“前台不等后台”、任务的中断与回报、前台调用工具的权限通道。

现有代码有两个有利条件：

- 和 Codex 有关的逻辑集中在 `thread_for()`、`run_turn()`、`model_command()`/`effort_command()`，外加 `run()` 里的一次 `self.app.start()`。抽出一层接口就能把具体 agent 换掉。
- `AppServer.turn()` 按 `threadId` 分发事件，一条 App Server 连接上本来就能同时跑多个 Codex 线程。所以“后台多 agent”不需要新增 Codex 进程。

### 已实测（Claude Code 2.1.311，云端 Linux 容器，haiku；尚未在 WSL 复验，PR2 第一步复验）

- 用 `--tools ""` 关掉全部内置工具，再挂一个自己写的 MCP 服务器（附录 A），前台能正确调用 `dispatch_task`，`goal` 写得完整，`result` 只包含最终那段话。
- `--session-id <uuid>` 新建会话、`--resume <uuid>` 续接，都能接上上下文。`--setting-sources project` 下订阅登录照常可用。
- 冷启动到 `system/init` 约 0.7 秒，首字约 1.6 秒，全程约 2.3 秒。WSL 上的数字要另测。
- 不带工具、不带 MCP、替换系统提示的纯聊天会话，首轮输入约 1.7k token。
- **只加 `--strict-mcp-config` 拦不住所有 MCP 服务器。** 测试环境里 Chrome 集成（`source: dynamic`）照样加载，多出 22 个工具，那一轮的提示约 14.7k token（不带它的纯聊天会话约 1.7k）；加了 `--no-chrome` 才去掉。你的 WSL 上还可能有 claude.ai 连接器之类的动态来源，所以桥接必须检查 `system/init` 里的 `tools` 列表，出现白名单以外的工具就中止这一轮（fail closed，见 4.3）。
- **续接会话时沿用首轮记录的系统提示。** 第二轮去掉了 Chrome，`cache_read_input_tokens` 仍是 14702，说明系统提示是首轮录下的那份。要保持现在“改了 SOUL.md 下一条就生效”，必须加 `--system-prompt-snapshot off`（v2.1.257+）。

## 1. 目标与非目标

**目标**

1. 前台负责聊天和分派，后台负责干活，前台不等后台完成。
2. 前台和后台都由配置决定，不写死在代码里。支持以下组合：

   | `front` | `back` | 效果 |
   | --- | --- | --- |
   | `codex` | `[]` | 现状：Codex 单 agent |
   | `claude` | `[]` | Claude Code 单 agent（主人会话直接带工具干活） |
   | `claude` | `["codex"]` | 本次目标：Claude 前台，Codex 后台 |
   | `claude` | `["codex", "codex-ro"]` | 多后台：一个能写，一个只读、可并行 |
   | `codex` | `["claude-worker"]` | 反过来也行 |

3. 后台任务有编号和状态，可以查询、中断，结果回到发起任务的那个会话。
4. 没有 `agents` 配置的旧 `config.json` 行为与现在完全一致。

**非目标（这一轮不做）**

- 后台向主人提问后暂停等待（`ask_user`），先在报告里写明“需要你提供什么”。
- 多个写任务在同一仓库并行时的隔离（git worktree），见 13 节第 6 条。
- DeepSeek 等直接调 API 的前台。接口留好，以后加 `type: "openai_chat"` 即可。

## 2. 现状与改动点

| 位置 | 现在 | 改成 |
| --- | --- | --- |
| `Bridge.__init__` | `self.app = AppServer(cfg["codex_path"])` | `self.agents = build_agents(cfg)`；Codex profile 内部持有 AppServer |
| `thread_for()` | 拼 sandbox/指令，恢复或新建 Codex 线程 | 移入 `CodexAgent`；Bridge 只给出权限级别和指令 |
| `run_turn()` | 直接调用 `self.app.turn()` | 调用 `agent.run(TurnRequest)`，进度与流式回复的回调不变 |
| `chat()` | 取 owner 权限 → Codex | 先选 profile（前台或访客），再加会话锁运行 |
| `worker()` | 单 worker 串行；所有命令都排队 | 仍是单 worker 跑前台；`/stop`、`/tasks` 在 `session()` 里直接处理，不排队 |
| `model_command()` 等 | `available_models(self.app)` | 交给当前前台 profile：Codex 读 `model/list`，Claude 读配置里的 `models` |
| `threads` 表 | 会话键 → Codex 线程 | 保留给 codex 前台；新表 `agent_sessions` 存其他 profile |
| `AppServer.turn()` | `turn_id` 是局部变量 | 记到 `self.active_turns[thread]`，供 `interrupt()` 使用 |

一条原则：**agent 只产出文字。** 面向 QQ 的策略（群友 500 字截断、数字过多拒绝、拉黑标记、语音、发送节奏）都留在 Bridge，换 agent 不影响。唯一的例外是返回值上限：`run()` 返回的完整回复最多 12000 字（`agents/base.py` 的 `MAX_REPLY`），由各适配器负责。这个截断现在在 `AppServer.turn()` 里，PR1 原样留在 CodexAgent，不改语义；流式发送不受这个上限影响。

## 3. 总体结构

```
NapCat ─▶ Bridge.session() ──(/stop、/tasks 直接处理)
             │ queue（容量 8）
             ▼
          worker ─▶ Bridge.chat()  加会话锁；选 profile（主人→front，其他→guest）
                        ▼
                  前台 Agent（claude_code / codex_app）
                        │ MCP 工具：dispatch_task / task_status / stop_task / send_to_task
                        ▼
   qq_chat.front_tools（Claude Code 每轮拉起的 stdio MCP）
                        │ run/tools.sock（一次性令牌）
                        ▼
                  Bridge.tool_server ─▶ TaskManager ─▶ 后台 Agent 池（每个 profile 有并发上限）
                                          │ 进度（节流）───────────────▶ QQ【#12 进度】
                                          └ 完成/失败 ─▶ report_task() 加会话锁，前台再跑一轮 ─▶ QQ
```

## 4. Agent 接口

### 4.1 接口

```python
# qq_chat/agents/base.py
from dataclasses import dataclass
from typing import Callable

NONE, FRONT, FULL = 'none', 'front', 'full'   # 权限级别：只决定工具和 sandbox
CHAT, TASK = 'chat', 'task'                   # 角色：只决定用哪套指令
MAX_REPLY = 12000                             # run() 返回值上限

class Interrupted(Exception):                 # 本轮因为我们发出的中断而结束（4.2、4.3）
    pass

@dataclass
class TurnRequest:
    key: str                  # 会话键，如 "private:<QQ>"、"group:<群>:owner"，或 "task:12"
    text: str                 # turn_text() 拼好的本轮输入
    role: str                 # CHAT 前台对话 / TASK 后台任务
    level: str                # NONE / FRONT / FULL，由 Bridge 决定
    instructions: str         # Bridge 按 role 组装好的指令全文
    images: tuple = ()
    model: str | None = None
    effort: str | None = None
    on_progress: Callable[[str], None] | None = None
    on_answer: Callable[[str], None] | None = None
    grant: 'ToolGrant | None' = None   # FRONT 级别时由 Bridge 生成

class Agent:
    name: str                                             # profile 名
    async def start(self): ...
    async def run(self, req: TurnRequest) -> str: ...     # 返回完整回复；失败抛异常
    async def interrupt(self, key: str) -> bool: ...      # 中断请求送达执行端返回 True；结局看 run()
    async def reset(self, key: str): ...                  # /new：归档旧会话
    async def models(self) -> list[dict]: ...             # /model 用
    async def close(self): ...
```

权限和角色是两个独立的维度：

| 场景 | role | level | 指令 |
| --- | --- | --- | --- |
| 群友、白名单访客 | CHAT | NONE | `GUEST`（群聊再加 `GROUP_MODERATION`） |
| 主人，单 agent 模式 | CHAT | FULL | `OWNER` + SOUL.md + OWNER.md |
| 主人，分离模式，PR4 之前 | CHAT | NONE | 同上，再加手动版 `FRONT_DESK`（提醒主人用 `/task`） |
| 主人，分离模式，PR4 起 | CHAT | FRONT | 同上，再加 `FRONT_DESK` |
| 任务回报轮次（6.3） | CHAT | NONE | 同上一行，但不能分派 |
| 后台任务 | TASK | FULL | `WORKER` |

level 和“是不是主人”也是两回事：回报轮次属于主人会话，用主人人设，但 level 是 NONE。Bridge 的发送策略只看 `owner_request()`，不看 level。

`build_agents(cfg)` 按 `profiles[*].type` 实例化：`codex_app` → `CodexAgent`，`claude_code` → `ClaudeCodeAgent`。多个 `codex_app` profile 共用一个 `AppServer` 实例，因为连的是同一个守护进程。

### 4.2 CodexAgent

基本就是把 `thread_for()` 和 `run_turn()` 里调 `self.app` 的部分搬过来：

| 级别 | sandbox | 说明 |
| --- | --- | --- |
| NONE | `read-only` | 与现在相同。**注意 read-only 仍能执行只读命令、读文件**（之前提过的 P0），所以访客最好走 Claude 无工具前台，见 9 节 |
| FRONT | `read-only` + MCP | 只有 Codex 在分离模式下当前台时才需要。要给线程挂 MCP，可试 `thread/start` 的 `config` 覆盖 `mcp_servers`，需实测 |
| FULL | profile 的 `sandbox`，默认 `danger-full-access` | 主人单 agent 模式或后台任务 |

后台任务每个任务开一条新线程（`ephemeral: False`，需要时可以在终端 `codex resume` 接着看），记录在 `tasks.session_id`，不写 `threads` 表。

`interrupt(key)`：找到该 key 当前线程的 `active_turns[thread]`，调用 `turn/interrupt`。这个 RPC 报错就是“停止失败”，任务回到 running。RPC 成功只算“已请求”，最终结局看这一轮的 `turn/completed`：

| `turn.status` | 结局 | 任务状态 |
| --- | --- | --- |
| `interrupted` | 已确认停止 | stopped |
| `completed` | 停止前已完成 | done，照常回报 |
| `failed` | 停止前已失败 | failed，报告里写错误 |
| 30 秒内没收到 | 停止未确认 | 保持 stopping，告诉主人还在等；之后收到事件，再按上面三行结算 |

`run()` 按结局区分：`interrupted` 抛 `Interrupted`，`failed` 抛普通异常，`completed` 正常返回。现在 `AppServer.turn()` 把所有非 completed 状态都抛成同一个 `RuntimeError`；PR1 不动，PR3 把 interrupted 单独区分出来。状态取值以本机 App Server 实际返回为准（13 节第 7 条）。

### 4.3 ClaudeCodeAgent（参数已实测）

每轮启动一个 `claude -p` 进程，写入一条 stream-json 用户消息后关闭 stdin，再读 stdout 直到 `result`。冷启动只有不到 1 秒，先不做常驻进程；需要时再改成长驻并保持 stdin 打开，接口不用变。

```python
BASE_ARGS = ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
             '--verbose', '--include-partial-messages',
             '--system-prompt-snapshot', 'off',        # 让 SOUL.md 修改下一轮生效
             '--setting-sources', 'project',           # cwd 是桥接专用空目录，相当于不加载任何设置
             '--disable-slash-commands', '--no-chrome', '--permission-prompts', 'none']

def level_args(level, grant, profile):
    """返回 (参数, init.tools 校验函数)。"""
    if level == NONE:
        return ['--tools', '', '--strict-mcp-config', '--disallowedTools', 'mcp__*',
                '--permission-mode', 'dontAsk'], lambda tools: not tools
    if level == FRONT:
        return ['--tools', '', '--strict-mcp-config', '--mcp-config', grant.config_path,
                '--allowedTools', 'mcp__qq__*', '--permission-mode', 'dontAsk'], \
               lambda tools: set(tools) <= FRONT_TOOLS
    # FULL：permission_mode 必须在 profile 里显式写（启动时校验，缺了拒绝启动）；
    # 永远不挂 qq 工具，防止工人自己再派任务
    return ['--tools', profile.get('tools', 'default'),
            '--permission-mode', profile['permission_mode']], \
           lambda tools: not any(t.startswith('mcp__qq__') for t in tools)

def user_message(text, images):
    content = [{'type': 'text', 'text': text}]
    for path, media_type in images:          # 图片块格式来自官方文档，尚未实测
        content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': media_type,
                        'data': base64.b64encode(path.read_bytes()).decode()}})
    return {'type': 'user', 'message': {'role': 'user', 'content': content}}
```

`run()` 的要点：

1. **会话 ID**：首轮由桥接生成 `uuid4`，先写入 `agent_sessions`，再用 `--session-id` 启动；之后用 `--resume`。续接失败（会话文件丢失）时清掉映射重开，同现在处理 `no rollout found` 的方式一样。
2. **系统提示**：怎么写看 level，写什么看 role（4.1）。NONE 和 FRONT 没有内置工具，用 `--system-prompt-file` 整个替换默认提示，提示更短，也没有“我是 Claude Code”的身份。FULL 要用工具，不管是单 agent 的主人对话还是后台任务，都用 `--append-system-prompt-file` 追加，保留 Claude Code 自带的工具用法说明。提示文件写在 `run/` 下，权限 600。**不要用 `--system-prompt` 直接传文字**：命令行参数在 `ps` 里所有本机用户都能看到，而 OWNER.md 里有个人资料。
3. **工作目录**：用桥接专用的空目录（如 `run/front/`）。不加 `--bare` 时，`-p` 会执行 cwd 里 `.claude/settings.json` 的 hooks，连接 `.mcp.json`，而且没有信任提示。所以前台不能用 `workspace/` 或任何仓库当 cwd。
4. **子进程**：`asyncio.create_subprocess_exec(..., limit=16 * 1024 * 1024, start_new_session=True)`。默认 64KB 的行长上限遇到大段工具结果会报错。
5. **事件处理**：

   | 事件 | 处理 |
   | --- | --- |
   | `system` / `init` | 用 `level_args` 返回的函数校验 `tools`：NONE 必须为空，FRONT 必须是 `FRONT_TOOLS` 的子集，FULL 不能含 `mcp__qq__*`。不通过就发 SIGINT 并抛 `UnexpectedTools`（fail closed） |
   | `stream_event`，`content_block_delta` 且 `delta.type == 'text_delta'` | `on_answer(delta.text)`，只取 `parent_tool_use_id` 为空的事件 |
   | `stream_event`，`content_block_start` 且块类型为 `tool_use` | 可选：`on_progress('⏳ 派发任务')` 之类 |
   | `assistant` 中的 `thinking` 块 | 忽略 |
   | `rate_limit_event` | 写日志；接近上限时可以提醒主人 |
   | `result` | `is_error` 为真或 `subtype != 'success'` 时抛异常；否则返回 `result`，`usage` 和 `total_cost_usd` 写日志（和现在记 Codex usage 一样） |

   `result` 只包含最后一段文字。如果前台先说一句再调工具，流式发出去的文字会比 `result` 多，所以写 `history` 用流式拼出的全文，不要用 Codex 那种“前缀必须一致”的校验。

6. **中断**：发 SIGINT（官方文档写明这样会结束当前轮）；5 秒内没退出就发 SIGTERM，再不行 kill 整个进程组。结局判定：收到 `subtype == 'success'` 的 `result`，算停止前已完成；我们发过 SIGINT 后，进程以其他结果或没有结果退出，算已确认停止，原始 subtype 和退出码记进 `error`；没发过 SIGINT 却失败，算失败。SIGINT 之后 `result` 具体长什么样，PR2 复验时记录下来，必要时修正这条规则（13 节第 7 条）。
7. **同一会话不能并发**：同一个 session ID 同时跑两个 `--resume` 会把会话写乱，所以会话锁必不可少（第 7 节）。

## 5. 配置

```json
"agents": {
  "front": "claude",
  "guest": "claude-chat",
  "back": ["codex", "codex-ro"],
  "profiles": {
    "claude":      {"type": "claude_code", "path": "claude", "model": "sonnet", "effort": "low",
                    "max_turns": 6, "models": ["haiku", "sonnet", "opus"]},
    "claude-chat": {"type": "claude_code", "path": "claude", "model": "haiku", "max_turns": 2},
    "codex":       {"type": "codex_app", "path": "/home/<user>/.local/bin/codex",
                    "sandbox": "danger-full-access", "writes": true, "max_parallel": 1,
                    "description": "能改文件、跑命令的主力"},
    "codex-ro":    {"type": "codex_app", "path": "/home/<user>/.local/bin/codex",
                    "sandbox": "read-only", "writes": false, "max_parallel": 3,
                    "description": "只读调研、读代码、查资料，可并行"}
  }
},
"tasks": {
  "max_running": 4,
  "max_writers": 1,
  "max_per_conversation": 3,
  "timeout_minutes": 60,
  "progress": "direct",
  "report_chars": 4000
}
```

规则：

- 没有 `agents` 时，等价于 `{"front": "codex", "back": [], "profiles": {"codex": {"type": "codex_app", "path": codex_path}}}`，也就是现状。
- `back` 为空即单 agent 模式：主人会话用 FULL 级别，前台自己干活。`back` 非空即分离模式：主人前台不自己干活。PR4 之前还没有工具通道，主人前台用 NONE，只聊天，干活由主人用 `/task` 手动分派（`FRONT_DESK` 用手动版，提醒主人用 `/task`）；PR4 起改用 FRONT。用哪一级由代码里工具通道是否可用决定，不另设配置项。
- 过渡期不用 FULL，是因为前台自己改文件会绕开全局写锁，和后台任务撞车。PR4 之前想让她像现在一样直接干活，就先保持 `back: []`。
- `guest` 默认等于 `front`。访客永远是 NONE 级别。
- `back` 里的 `description` 会写进 `dispatch_task` 的工具说明，前台据此选 agent；`agent` 参数是 `back` 列表构成的枚举。
- `tasks.progress`：`direct` 把后台进度节流后直接发到 QQ；`silent` 不推送，只在前台调用 `task_status` 时返回。主人在群里的任务建议用 `silent`。
- 启动时校验：`front`、`guest` 和 `back` 引用的 profile 必须存在，`type` 必须已知，否则拒绝启动并打印原因。
- 改了配置用 `tools/restart_bridge.py` 重启。运行时切换（`/front claude`）可以以后再加，写回配置沿用 `moderation.update_blacklist()` 的加锁原子替换方式。
- 后台 profile 必须显式写 `writes`。Codex 的 `sandbox` 不是 `read-only` 时，`writes` 必须为 true；Claude 的 FULL 工具里有 Bash、Edit 或 Write 时也必须为 true。声明与实际不符就拒绝启动。所有 `writes: true` 的 profile 共用一个全局写锁（`tasks.max_writers`，默认 1），和各自的 `max_parallel` 同时满足才能开工。等以后做了按任务开 git worktree，再放开这个限制。
- 切换前台以后，新 profile 没有旧对话的上下文。新会话第一轮从 `history` 表带上最近的记录：从新往旧取，最多 20 条、总共 8000 字，单条超过 1000 字截断（`bootstrap` 表按新的 session ID 记录）。注意现在的 `history_import` 导入的是全部历史：PR1 不动它，PR2 改成上面的预算，对所有 profile 一致（包括 Codex 新开的线程，属于行为变化，要写进 CHANGELOG）。
- 模型选择按“会话 + profile”保存（6.1 的 `profile_models`），一个 profile 永远拿不到另一个 profile 的模型名。

同时更新 `config.example.json` 和 README 的配置表。

## 6. 任务管理

### 6.1 数据表（在 `Bridge.__init__` 里用 `CREATE TABLE IF NOT EXISTS`）

```sql
CREATE TABLE IF NOT EXISTS agent_sessions (
  conversation TEXT, profile TEXT, session_id TEXT,
  PRIMARY KEY (conversation, profile));
CREATE TABLE IF NOT EXISTS profile_models (
  conversation TEXT, profile TEXT, model TEXT, effort TEXT,
  PRIMARY KEY (conversation, profile));
-- PR2 迁移：conversation_models 的旧记录复制进 profile_models，profile 记为默认的 codex profile
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation TEXT NOT NULL,     -- 发起任务的会话键，结果只回这里
  source TEXT NOT NULL,           -- Source 的 JSON，回报时用来发送
  agent TEXT NOT NULL, session_id TEXT,
  runtime_ref TEXT,               -- Codex 的 turn_id 或 Claude 进程的 pid，重启后善后用
  goal TEXT NOT NULL,
  status TEXT NOT NULL,           -- queued / running / stopping / done / failed / stopped / interrupted
                                  -- 终态：done / failed / stopped / interrupted
  created REAL, started REAL, finished REAL,
  result TEXT, error TEXT,
  report_text TEXT,               -- 要发给 QQ 的回报文字（6.3）
  reported INTEGER DEFAULT 0);
```

### 6.2 `TaskManager`（`qq_chat/tasks.py`）

```python
class TaskManager:
    async def dispatch(self, conversation, source, goal, agent=None) -> dict:
        # 1. owner_request(source) 必须为真；agent 必须在 back 列表里（缺省取第一个）
        # 2. 检查 max_running / max_per_conversation，超出就返回错误文字给前台，不排队
        # 3. 写表（queued），asyncio.create_task(self._run(id))，立即返回 {"id", "agent", "status"}
    async def _run(self, task_id):
        writer = self.write_lock if profile_writes(agent) else contextlib.nullcontext()
        async with self.semaphores[agent], writer:   # profile 的 max_parallel + 全局写锁
            # 标记 running；agent.run(TurnRequest(key=f'task:{id}', role=TASK, level=FULL,
            #     instructions=prompts.WORKER, on_progress=节流后发 QQ 或只记录))
            # asyncio.timeout(timeout_minutes)；记录 result 或 error
        # finally: await self.bridge.report_task(task_id)
    def status(self, conversation, task_id) -> dict   # 状态、耗时、最近 10 条进度、计划
    async def stop(self, conversation, task_id) -> str:
        # 'requested'（已送达，等结局）/ 'failed'（中断请求本身失败）/ 'not_running' / 'not_found'
        # 最终结局（已确认停止 / 停止前已完成 / 停止前已失败 / 未确认）随任务回报告知，见 4.2
    async def recover_after_restart(self):
        # 所有非终态（queued / running / stopping）都收敛到终态，见下面“重启恢复”
```

- **重启恢复**：启动时，非终态的任务先善后再结算。
  - 有 `runtime_ref` 的，先尽力停掉残留的执行。Codex 调一次 `turn/interrupt`，因为守护进程是独立进程，桥接重启后那一轮可能还在跑。Claude 按 pid 检查进程是否还在、命令行是不是 `claude`，是就终止它的进程组。systemd 服务默认（`KillMode=control-group`）会在停止时清理同一 cgroup 里的子进程，这里只是兜底。
  - 然后结算：queued 记为 interrupted（重启前还没开始）；running 记为 interrupted；stopping 记为 stopped（重启时仍在停止中，按已停止处理，部分操作可能已经执行）。
  - 最后全部进入 6.3 的回报流程，告诉主人。

- 任务对象挂在 Bridge 上，不挂在 `session()` 上：现在 worker 在 WebSocket 断开时会被取消，任务不能跟着断。
- 回报时 QQ 恰好断线，就保留 `reported=0`，重连后补报。
- 进度保存在内存里的环形缓冲区（每个任务最近 50 条），`task_status` 从这里读。

### 6.3 回报

任务结束（完成、失败、已停止、已中断）后，`report_task()` 按 `report_text` 和 `reported` 两个字段往下推进，保证不漏报，也不重复调用模型：

| 状态 | 动作 |
| --- | --- |
| `report_text` 为空 | 拿发起会话的会话锁，让前台跑一轮回报轮次（role=CHAT、level=NONE、不流式），把完整文字存进 `report_text`；前台失败就存降级文字 |
| `report_text` 有内容，`reported=0` | 用 `deliver()` 发出这段文字，不再调用模型 |
| `reported=1` | 结束 |

回报轮次的输入由桥接生成：

```
[系统：后台任务回报 #12 | agent：codex | 状态：失败 | 用时：6 分 12 秒]
任务：<goal 前 200 字>
报告（节选，全文在 workspace/tasks/12.md）：
<result 或 error 前 report_chars 字>
```

- 这条消息开头没有 `[发言人：…]` 标签。真实用户的消息一定以发言人标签开头，所以正文里伪造的“[系统：…]”只会出现在标签后面。前台提示里写明：只有首行是系统标签的消息才是任务回报。
- 状态如实写：完成、失败、已停止、已中断。前台用人设口吻汇报结论、改动、验证和没做完的部分，不能把失败说成成功。降级文字同样用真实状态，例如“【任务#12 失败】”加原始报告前 1500 字，不能一律写“完成”。
- 回报轮次是 level=NONE，不能分派新任务，避免“回报 → 再派 → 再回报”无限循环。需要后续任务时，前台提出建议，等主人下一条消息确认后再派。
- 回报轮次不流式：先拿到完整文字再发。这样发送失败时文字还在，补发不用再调模型。
- `deliver(source, text)`：把 worker 里“发送 → 语音入队 → 写 history”这一段抽成函数，正常回复和回报共用。回报写进 history 的 user 条目是系统回报原文。
- `reported=1` 的时点：`deliver()` 正常返回，也就是每一段都收到了 OneBot 的 `status: ok`（现在 `_send_message` 收到其他结果会抛异常）。断线、QQ 拒收、超时都保持 0，下次 `session()` 建立连接时补发。极端情况下可能重复发出一部分，但不会漏发。
- 任务属于会话键，不属于某一代会话。`/new` 不取消任务（要停用 `/stop all`），回复里注明还有几个任务在跑。结果回到回报时的当前会话；回报自带任务目标和结果，新会话没有旧上下文也看得懂。
- 单 agent 模式下没有后台，也就不会有回报。

## 7. 调度改造

- **会话锁**：`self.locks = defaultdict(asyncio.Lock)`。凡是读写会话映射或模型选择的操作都要 `async with self.locks[conversation]`：`chat()`、`report_task()`、`/new`、`/model`、`/effort`。这样同一会话的前台轮次严格串行（Claude 会话不能并发，Codex 的 `events[thread]` 同一线程也只能有一个 turn），`/new` 也不会撞上正在进行的回报。`/stop` 和 `/tasks` 不拿锁，它们必须在锁被占用时也能执行，只发中断信号或读状态。
- **worker 维持单个**：分离模式下前台一轮只要几秒，单 worker 够用。以后需要的话再改成按会话分 worker。
- **`/stop`、`/tasks` 不排队**：worker 正在跑前台那一轮时，`/stop` 进队列就要等这一轮结束，中断就失去意义。在 `session()` 里识别主人的这两个命令，用 `asyncio.create_task` 直接处理（权限检查和 worker 用同一个 `owner_request()`、同一套命令规范化）。
- **工具通道和前台并行**：前台调用 `dispatch_task` 时，Bridge 正卡在 `await agent.run()` 上；工具请求由独立的协程（`tool_server`）处理，不需要会话锁。

## 8. 前台工具通道

### 8.1 链路

```
Claude Code ──stdio──▶ python3 -m qq_chat.front_tools     （每轮由 Claude Code 拉起）
                         │ asyncio.open_unix_connection(run/tools.sock)，一行一个 JSON
                         ▼
                      Bridge.tool_server（asyncio.start_unix_server）→ 校验令牌 → TaskManager
```

不引入新依赖：shim 用标准库手写 MCP 的 stdio JSON-RPC（附录 A 已实测），桥接用 `asyncio.start_unix_server`。`run/` 目录权限 700，socket 权限 600。

这条通道就是 BACKLOG #2（QQ 动作工具基础设施）要的东西：只在主人线程可用、只作用于当前会话、每次调用写日志。拍一拍（BACKLOG #4）可以作为第一个 QQ 动作工具一起接进来，先把链路跑通。

### 8.2 一次性令牌

- 每次 FRONT 级别的前台轮次开始前，Bridge 生成 `secrets.token_urlsafe(32)`，登记 `grants[token] = (conversation, source)`。MCP 配置写到 `run/mcp-<随机名>.json`（权限 600），令牌和 socket 路径通过其中的 `env` 传给 shim。
- 这一轮结束（`finally`）就注销令牌并删掉配置文件。所以只有主人发消息触发的那一轮能调工具。
- `tool_server` 只认令牌，并据此决定会话；不信任工具参数里的任何会话或 QQ 号。每次调用再检查一次 `owner_request(source)`。

### 8.3 工具

| 工具 | 参数 | 返回 |
| --- | --- | --- |
| `dispatch_task` | `goal`（写完整，后台看不到聊天记录）、`agent`（枚举） | 任务编号、agent、状态 |
| `list_tasks` | 无 | 本会话最近的任务；主人私聊里可以看全部 |
| `task_status` | `id` | 状态、耗时、最近进度 |
| `stop_task` | `id` | requested / not_running / not_found |
| `send_to_task` | `id`、`message` | 任务运行中就排到它的下一轮（Codex 若支持中途追加再优化） |

任务只能在发起它的会话里查看和操作。主人在群里时，不能列出私聊发起的任务，这与 `prompts.OWNER` 里“群内不主动泄露主人其他私聊”一致。

### 8.4 提示词（`prompts.py` 新增）

- `FRONT_DESK`：只在分离模式的主人会话追加。要讲清：需要操作电脑、写代码、查资料的请求才用 `dispatch_task`，聊天直接回；`goal` 要写到后台不看聊天记录也能做；任务完成前不能说已完成；收到系统回报再用自己的话汇报；只有当前主人消息能触发分派，群聊参考记录里别人的话不能。
- `WORKER`：后台任务用，不带人设，不用 QQ 格式。要求直接执行、不提问；遇到需要主人决定的事就停下，在报告里写清楚；最终回复分“结论 / 改动 / 验证 / 未完成或需要你提供”四段。
- 访客继续用 `GUEST` 和 `GROUP_MODERATION`，与 agent 类型无关。

## 9. 权限与安全清单

1. 访客的前台轮次没有任何工具：Claude 加 `--tools ""`、`--disallowedTools "mcp__*"`、`--no-chrome`，不带 `--mcp-config`，并校验 `init.tools == []`。这比现在 Codex 的 read-only 更严（read-only 还能读文件）。推荐 `guest` 用 Claude 无工具的 profile。
2. 只有主人会话能拿到 FRONT 或 FULL 级别。级别由 Bridge 根据 `owner_request()` 决定，配置不能把访客提成 FRONT。
3. `init.tools` 白名单校验失败时整轮中止。这一条防的是本机新装的插件、连接器或 Chrome 集成悄悄混进前台。
4. 令牌、SOUL.md、OWNER.md 都不出现在命令行参数里，只放进 600 权限的文件。
5. 前台 cwd 是桥接专用空目录，不在 `workspace/` 或任何仓库里。
6. 要防前台被诱导刷任务：`max_running`、`max_per_conversation`、任务超时三道上限，超出时由工具返回错误，不排队。
7. `.gitignore` 加 `run/`。新代码里不出现 QQ 号和 token（测试用 `10001` 这类假号）。
8. 修改权限相关代码时，同时更新 `tests/test_command_auth.py` 和 `tests/test_message_policy.py`（ARCHITECTURE.md 的要求）。

## 10. 额度与成本

- 前台每轮都消耗 Claude 订阅额度，和你自己在终端用 Claude Code 共用 5 小时和每周的上限。群里聊得热闹时，额度可能先被群友用掉。所以 `guest` 建议用 haiku，前台主人会话用 sonnet、effort low；想用 opus 当经理也行，只是额度消耗快很多。
- 无工具的纯聊天会话每轮约 1.7k token 起步，之后主要是缓存读。群聊参考记录是最大的变量，之前说的增量注入在这里同样适用。
- 把 `result.usage` 的 `cache_read_input_tokens` 和 `total_cost_usd` 记进日志（后者是客户端估算，订阅下只看相对大小），方便对比 Codex 和 Claude 前台的消耗。
- 后台 Codex 走 ChatGPT 订阅，和前台额度互不影响，这也是“Claude 前台 + Codex 后台”这种搭配划算的地方。

## 11. 分阶段实施

每个阶段一个提交或 PR：先跑测试，同一提交里更新 FEATURES/CHANGELOG，部署用 `tools/restart_bridge.py`。

**PR1：抽接口，不改行为**
- 新增 `qq_chat/agents/`（`base.py`、`codex.py`、`build_agents()`），`thread_for`/`run_turn` 的 Codex 逻辑移入 `CodexAgent`。为 `/model`、`/effort`、`/session` 补上需要的接口（准备线程、查询模型状态）。
- 权限、人设组装、历史、流式发送、拉黑和语音留在 Bridge。以下语义原样保留：12000 字截断在 CodexAgent 里；`history_import` 仍导入全部历史；`conversation_models` 表和空线程的恢复处理不变。
- 验收：现有测试全部通过（测试里替换 `bridge.app` 的写法，改成替换 `bridge.agents['codex']` 或保留 `app` 属性作兼容）；不加 `agents` 配置时行为一致；`self.app` 只在 `CodexAgent` 和模型目录里出现。

**PR2：Claude Code 前台（纯聊天，单 agent）**
- 第一步：在 WSL 上复现 4.3 和附录 A，内容包括 `init.tools`、`--no-chrome` 是否必要、耗时、系统提示快照，以及 13 节的 CLAUDE.md 暗号测试。结果写进第 0 节，和文档不一致的以本机为准。
- `ClaudeCodeAgent`，支持 NONE 和 FULL 两个级别；`agent_sessions` 表；`profile_models` 表和迁移；历史导入预算（5 节）；`/new` 和 `/model` 适配。
- 验收：`front: "claude"` 时，主人和访客都能聊；访客 `init.tools` 非空即中止；改 SOUL.md 下一条生效；重启后会话续上；流式回复照常分气泡发送。

**PR3：任务与命令（先手动分派）**
- `tasks.py`、`tasks` 表、`report_task()` 回报状态机、`deliver()` 抽取、会话锁（7 节的范围）、全局写锁、重启恢复（6.2），以及主人命令 `/task <目标>`、`/tasks`、`/stop [编号|all]`（BACKLOG #6）。
- 分离模式下主人前台用 NONE 和手动版 `FRONT_DESK`（5 节）。
- `AppServer.turn()` 把 interrupted 和 failed 区分开（4.2）。
- `/stop` 的语义（顺带回答 BACKLOG #6 的待定项）：不带参数停当前会话正在跑的前台轮次；`/stop 12` 停单个任务；`/stop all` 停本会话所有运行中的任务，并取消排队中的任务。只作用于发起命令的会话。
- 验收：任务运行期间前台照常回话；分离模式下主人前台不会自己改文件；任务结果只回发起会话；`/stop` 不排队，先回“已请求”，结局能区分已确认停止、停止前已完成、停止前已失败、未确认和停止失败；重启后 queued、running、stopping 都收敛到终态并通知主人；非主人用不了这些命令（静默）。
- 做到这一步，前后台分离已经能从 QQ 用起来了，PR4 只是把“主人手动分派”换成“前台自己判断”。

**PR4：前台工具通道**
- `front_tools.py`（MCP shim）、`tool_server`、一次性令牌、`FRONT_DESK` 提示；分离模式的主人前台从 NONE 换成 FRONT。
- 验收：分离模式下，主人说“帮我修个 bug”时前台会分派；访客的消息永远不带 MCP；群里主人分派的任务回报到该群；三道上限生效。

**PR5：多后台**
- `back` 支持多个 profile、`description` 进工具说明、按 profile 的 `max_parallel`。
- 验收：`codex-ro` 能同时跑 3 个只读任务；`codex` 写任务同时只跑 1 个。

## 12. 测试清单

全部离线，沿用 `Bridge.__new__(Bridge)` 的写法，另加一个 `FakeAgent`：记录收到的 `TurnRequest`，用 `asyncio.Event` 控制何时返回。

- `build_agents`：无 `agents` 配置等价现状；引用不存在的 profile 时拒绝启动。
- 级别：访客永远 NONE；单 agent 主人是 FULL；分离模式主人是 FRONT；昵称、正文里自称主人都不改变级别。
- `ClaudeCodeAgent`：用录制好的 jsonl（本次实测输出可以直接存成测试数据，记得去掉会话 ID）喂一个假子进程，断言参数里有 `--tools ""`、`--no-chrome`、`--system-prompt-snapshot off`，访客没有 `--mcp-config`；`init.tools` 出现多余工具时抛异常并发出 SIGINT。
- 并发：后台 FakeAgent 卡住时，前台仍能完成新一轮；同一会话两轮严格串行。
- 工具通道：过期令牌、伪造令牌、访客会话来源一律拒绝；`dispatch_task` 参数里塞别的会话键不起作用。
- 回报：任务结果只发到发起会话；失败任务的降级文字写“失败”而不是“完成”；`report_text` 已有时补发不调用模型；断线时 `reported=0`，重连后补报；`/new` 之后的回报进入新会话；回报轮次拿不到分派工具。
- 级别：FULL 的 `init.tools` 出现 `mcp__qq__*` 时中止；FULL 的 profile 没写 `permission_mode` 时拒绝启动。
- 写锁：两个不同 profile 的写任务不会同时运行；只读 profile 不受影响。`writes` 声明与 sandbox 不符时拒绝启动。
- 中断结局：假 App Server 分别返回 interrupted、completed、failed 和超时，任务分别落到 stopped、done、failed 和 stopping；中断 RPC 报错时回到 running，`/stop` 回复“停止失败”。
- 重启恢复：库里预置 queued、running、stopping 各一条，启动后分别变成 interrupted、interrupted、stopped，有 `runtime_ref` 的收到一次善后调用，三条都进入回报流程。
- 阶段衔接：分离模式且工具通道不可用时，主人前台的 level 是 NONE。
- 模型隔离：Codex 的模型选择不会传给 Claude profile，反之亦然。
- `/stop`：在 `session()` 里处理，不进队列；非主人静默。

## 13. 待实测与未决

1. `--setting-sources project` 下，`~/.claude/CLAUDE.md`（你自己做开发用的个人指令）还会不会注入前台。测法：在里面写一句暗号，问前台暗号是什么，应该答不出来。
2. stream-json 输入里的 base64 图片块（格式来自官方文档，未实测）。
3. WSL 上的冷启动耗时；如果明显变慢，再考虑每个会话一个长驻进程。
4. Codex App Server 有没有中途追加消息的接口（决定 `send_to_task` 的实现）；Codex 在分离模式下当前台时，能否通过 `thread/start` 的 `config` 挂 MCP。
5. 换成 Claude 前台后，人设的表现要重新调：被认真问到“你是不是 AI”时的反应、语气、长度控制都会和 Codex 不同。
6. 多个写任务同时改同一仓库：目前靠第 5 节的全局写锁（`tasks.max_writers`，默认 1）串行执行，以后按任务开 git worktree 再放开。
7. 中断的实际表现：Claude 被 SIGINT 后 `result` 的 subtype 和退出码（决定 4.3 第 6 条的判定规则），以及本机 App Server 的 turn 状态取值（4.2 的表）。

## 附录 A：实测用的最小 MCP 服务器

这是测试时用的版本：`tools/call` 返回固定文字。实现 `front_tools.py` 时，把 `tools/call` 改成连接 `QQ_TOOL_SOCKET`、带上 `QQ_TOOL_TOKEN` 转发给桥接，再把结果原样返回。

```python
"""Minimal MCP stdio server (newline-delimited JSON-RPC), no dependencies."""
import json, sys

TOOLS = [{
    'name': 'dispatch_task',
    'description': '把需要操作电脑、写代码、查资料的工作交给后台 agent。立即返回任务编号，不等待完成。',
    'inputSchema': {'type': 'object', 'properties': {
        'goal': {'type': 'string', 'description': '给后台的完整任务说明'},
        'agent': {'type': 'string', 'enum': ['codex'], 'description': '后台 agent'}},
        'required': ['goal']}}]

def reply(ident, result):
    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': ident, 'result': result}, ensure_ascii=False) + '\n')
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method, ident = msg.get('method'), msg.get('id')
    if ident is None:                      # notifications/initialized 等通知不回
        continue
    if method == 'initialize':
        reply(ident, {'protocolVersion': msg['params'].get('protocolVersion', '2025-06-18'),
                      'capabilities': {'tools': {}}, 'serverInfo': {'name': 'qq', 'version': '0.1'}})
    elif method == 'tools/list':
        reply(ident, {'tools': TOOLS})
    elif method == 'tools/call':
        reply(ident, {'content': [{'type': 'text', 'text': '已创建任务 #7，后台 codex 正在处理。'}]})
    else:
        reply(ident, {})
```

对应的 MCP 配置（文件权限 600）：

```json
{"mcpServers": {"qq": {"command": "/usr/bin/python3", "args": ["-I", "<项目>/qq_chat/front_tools.py"],
  "env": {"QQ_TOOL_SOCKET": "<项目>/run/tools.sock", "QQ_TOOL_TOKEN": "<每轮生成>"}}}}
```

`-I` 隔离模式不读 `PYTHONPATH`、不把当前目录加入导入路径，所以 shim 要按绝对路径运行，并且只用标准库、不写包内相对导入。`command` 用 `sys.executable`，保证和桥接是同一个 Python。

## 14. 审阅修订记录（2026-10-27）

### 第一轮（v2）

v1 交给 Codex 对照代码审阅后，以下意见全部采纳：

| 审阅意见 | 结论 | 改在哪里 |
| --- | --- | --- |
| 12000 字截断实际在 `AppServer.turn`，不在 Bridge | 成立。定为适配器的返回值上限 `MAX_REPLY`，PR1 原样保留 | 2 节、4.1、11 节 PR1 |
| `history_import` 导入的是全部历史，不是最近几轮；模型设置要按 profile 隔离 | 成立。PR2 起改为 20 条、8000 字的预算；新增 `profile_models` | 5 节、6.1、11 节 PR2 |
| 会话锁要覆盖 `/new` 和模型切换；`/new` 之后结果回哪一代会话 | 成立。锁的范围写明；任务属于会话键，结果回当前会话 | 6.3、7 节 |
| 降级回报不能一律写“完成”；要复用历史、语音和补报；`reported` 的确认时点 | 成立。如实写状态；新增 `deliver()` 和 `report_text` 状态机 | 6.1、6.3 |
| 每个 profile 限一个写任务，挡不住不同 profile 同时写 | 成立。新增全局写锁和 `writes` 声明 | 5 节、6.2 |
| FULL 同时指单 agent 主人和后台任务；工具白名单与回报轮次的分派权限要明确 | 成立。拆成 role 和 level 两个维度；回报轮次 level=NONE | 4.1、4.3、6.3 |
| Claude 参数和性能是外部实测，本机未复验 | 成立。PR2 第一步在 WSL 复验 | 0 节、11 节 PR2 |

### 第二轮（v3）

| 审阅意见 | 结论 | 改在哪里 |
| --- | --- | --- |
| 分离模式要求 FRONT，但 FRONT 依赖 PR4 的工具通道；PR3 期间主人前台用什么级别 | 成立。PR4 之前用 NONE 加手动版 `FRONT_DESK`，不用 FULL 过渡（会绕开写锁）；想直接干活就先保持 `back: []` | 4.1、5 节、11 节 PR3/PR4 |
| “状态不是 completed”就算停止，会把 failed 也算进去 | 成立。按 interrupted / completed / failed / 超时分别结算；新增 `Interrupted` 异常 | 4.1、4.2、4.3、6.2 |
| 重启恢复漏了 stopping | 成立。所有非终态都收敛到终态，并先按 `runtime_ref` 善后 | 6.1、6.2 |
| 13 节第 6 条还写着靠 `max_parallel: 1` 防写冲突 | 成立。改为全局写锁 | 13 节 |
