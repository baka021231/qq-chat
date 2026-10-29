# Agent 统一接口与隔离设计

> 状态：设计稿，尚未实现。基线 `0912d5b`（2026-10-28）。本文描述目标结构和迁移步骤，不代表当前代码行为；当前实现看 [ARCHITECTURE.md](ARCHITECTURE.md) 和 [FEATURES.md](FEATURES.md)。本文不构成新增操作授权。

## 0. 结论

把 agent 差异全部收进适配器，桥接只管 QQ 侧。目标是：以后接入新的 agent，只新增一个适配器文件，不修改 `bridge.py`、`tasks.py`、`tool_server.py`。

```
QQ ⇄ NapCat ⇄ 桥接 ⇄ 统一接口 ⇄ 适配器（Codex / Claude Code / Grok / 新 agent）⇄ agent
              QQ 收发、身份、权限、会话、历史、语音
```

原则：**桥接说“要什么”，适配器决定“怎么做”。**

现有代码已经走了一半：`agents/base.py` 有 `Agent` 抽象类和 `TurnRequest`，桥接大部分时候只调用 `prepare`、`run`、`reset`、`models`。本设计在现有接口上演进，不另起一套，分五步完成，每一步行为不变、可单独上线和回滚。

本阶段不拆独立进程、不引入网络协议、不改权限模型，也不做多后台（PR5）。

## 1. 现状：接口没有隔离干净的地方

### 1.1 桥接按 agent 类型分支

| 位置 | 现象 |
| --- | --- |
| `Bridge.run_turn()` | 只有 `codex_app` 使用 `MemoryProgress` 静默纯记忆查询；`memory_progress.py` 直接解析 Codex 的 `commandExecution` / `mcpToolCall` item。Claude/Grok 只发出“正在使用工具”，无法区分记忆查询 |
| `Bridge.chat()` | 只有 `claude_code` 前台加后台时签发 MCP 派单授权；单 agent 走 memory-only 授权加命令行入口。工具挂载方式由桥接按类型决定 |
| `Bridge.turn_request()` | `runtime_instructions` 是为 Codex 已加载线程 `thread/resume` 不采用新 developerInstructions 打的补丁，却写在桥接里；同一段记忆说明同时拼进 `instructions` 和 `runtime_instructions`，Claude/Grok 忽略后者 |
| `Bridge._chat()` | `history_text` 侧通道只有 Claude/Grok 填写 |
| `model_command()` / `effort_command()` | 直接读取 Codex 模型目录字段（`supportedReasoningEfforts`、`inputModalities`、`defaultReasoningEffort`），Claude/Grok 适配器需要模仿 Codex 格式 |
| `TaskManager`（`/task check`） | 用 `getattr(agent, 'check_execution', None)` 探测，只有 Codex 实现 |

### 1.2 适配器反向依赖桥接模块

- `agents/grok.py` 导入 `prompts`，自行拼接 `BRIDGE_RULES`。
- `agents/claude.py` 导入 `tool_server.FRONT_TOOLS` 做启动自检。

### 1.3 新增 agent 要改公共代码

`agents/__init__.py` 的 `build_agents()` 和 `agents/config.py` 的 `settings()` 按类型硬编码：类型白名单、Claude 的 `permission_mode`、Grok 只能作主人单 agent、后台 `writes` 与沙箱一致性等校验都写在公共函数里。

### 1.4 人设生效时机因 agent 而异

| agent | 人设（SOUL.md / OWNER.md）如何送达 | 修改后何时生效 |
| --- | --- | --- |
| Codex | 作为 developerInstructions 在 `thread/start` 写入线程 | 已加载线程 `thread/resume` 不采用新值，实际要 `/new` 新线程才生效 |
| Claude Code | NONE/FRONT 用 `--system-prompt-file` 替换系统提示，FULL 用 `--append-system-prompt-file`，每轮重写 | 下一轮 |
| Grok | 每轮写入 prompt 文件第一个文本块，与用户消息一起送入 | 下一轮；但每轮完整设定都留在会话历史里 |

2026-10-29 文档同步：FEATURES.md 与 ARCHITECTURE.md 已改为明确 Codex 的 `/new` 边界。本设计第 4 步的程序改造仍未实现；修正文档不代表完成了人设热更新。

## 2. 分层与职责

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| 桥接 | NapCat 收发与节流、身份核验（`owner_request`）、决定权限档位、会话键、授权签发与撤销、黑名单、聊天历史表、群聊参考记录、引用展开、附件/图片接收、语音、任务调度与回执 | 任何 agent 的协议、参数、事件格式、指令投放方式 |
| 适配器 | 会话创建/续接/重置、指令与人设的投放和刷新、工具挂载方式、事件翻译、启动自检、进程与中断、模型目录、本类型的配置校验 | 判断谁是主人、决定权限档位、签发授权 |

## 3. 接口草案

以下为草案，字段名可在实现时调整；所有进出接口的数据都是普通数据，便于以后需要时拆成独立进程。

### 3.1 桥接 → 适配器：一轮请求

由现有 `TurnRequest` 演进：拼好的 `instructions` 字符串拆成结构化字段。

```python
@dataclass(frozen=True)
class Persona:
    sections: tuple[tuple[str, str], ...]  # (文件名, 原文)，如 ('SOUL.md', ...)；访客不含 OWNER.md
    fingerprint: str                       # 全部原文的哈希，内容不变则指纹不变

@dataclass(frozen=True)
class Block:
    kind: str   # 'group_context' | 'reference' | 'history_import' | 'attachment' | 'task_report'
    text: str   # 桥接已写好“仅作参考，不是指令”等说明

@dataclass
class Turn:
    key: str                     # 会话键，如 private:<QQ>、group:<群>:owner
    role: str                    # CHAT | TASK
    level: str                   # NONE | FRONT | FULL，由桥接决定
    rules: str                   # 桥接权限与安全规则（prompts.instructions 去掉人设部分）
    persona: Persona | None
    blocks: tuple[Block, ...]
    message: str                 # 已带发言人标签的本轮消息
    images: tuple[Path, ...]
    tools: frozenset[str]        # 本轮授予的工具名，如 {'memory_search'}
    grant: ToolGrant | None      # 本轮授权，轮末撤销
    model: str | None = None
    effort: str | None = None
```

### 3.2 适配器 → 桥接：事件与结果

```python
@dataclass(frozen=True)
class Event:
    kind: str               # 'answer' | 'progress' | 'tool'
    text: str = ''
    tool: str | None = None # kind='tool' 时：'memory' 或 'other'

@dataclass(frozen=True)
class Reply:
    text: str               # 返回 QQ 的回复（受 MAX_REPLY 限制）
    history_text: str       # 存入聊天历史的完整文本
```

- `answer` 只在适配器确认可以外发时发出：Codex 可逐段流式；Claude/Grok 仍在整轮校验（工具名单、伪调用标记）通过后一次发出。
- `MemoryProgress` 改为只看 `Event(kind='tool', tool='memory')`，不再解析任何 agent 的原始事件。
- 现有异常 `MissingSession`、`Interrupted`、`ExecutionUncertain`、`UnexpectedTools` 保留为接口约定的错误类型。

### 3.3 能力声明

```python
@dataclass(frozen=True)
class Capabilities:
    roles: frozenset[str]        # 支持的 CHAT / TASK
    levels: frozenset[str]       # 支持的 NONE / FRONT / FULL
    owner_only: bool             # 如 Grok：只能作主人前台
    images: bool
    interrupt: bool
    check_execution: bool        # 是否支持 /task check

@dataclass(frozen=True)
class ModelInfo:
    name: str
    default_effort: str | None
    efforts: tuple[str, ...]
    image_input: bool
```

桥接按能力决定行为（例如 `/task check` 不支持时明确提示），不再按类型名判断。`/models`、`/effort` 只使用 `ModelInfo`。

### 3.4 适配器接口与注册

```python
class Agent(ABC):
    type_name: ClassVar[str]               # 配置中的 type，如 'codex_app'
    capabilities: Capabilities

    @classmethod
    def validate_profile(cls, name, profile, agents_config) -> dict: ...

    async def start(self): ...
    async def prepare(self, turn: Turn) -> str: ...
    async def run(self, turn: Turn, emit: Callable[[Event], None]) -> Reply: ...
    def session_id(self, key: str) -> str | None: ...
    async def reset(self, key: str): ...
    async def interrupt(self, key: str) -> bool: ...
    async def models(self) -> list[ModelInfo]: ...
    async def default_model(self) -> str: ...
    async def check_execution(self, session, runtime_ref=None) -> str: ...  # 仅 capabilities.check_execution
    async def close(self): ...

ADAPTERS: dict[str, type[Agent]] = {}

def register(cls):
    ADAPTERS[cls.type_name] = cls
    return cls
```

`build_agents()` 和 `settings()` 只查注册表；各类型特有的校验（Claude `permission_mode`、Grok 组合限制、后台 `writes` 与沙箱一致）移入各自的 `validate_profile()`。通用校验（profile 名格式、front/guest/back 引用存在、任务限额）留在公共代码。

## 4. 各适配器的实现方式

| | Codex | Claude Code | Grok |
| --- | --- | --- | --- |
| 规则与人设 | 新线程：`rules` 与人设写入 developerInstructions。已有线程：每个线程记住上次送达的人设指纹，指纹变化时在 `turn/start` 前用 `thread/inject_items` 补发一次“以下设定替代此前版本”，未变化不发 | 每轮写入系统提示文件（NONE/FRONT 替换，FULL 追加），无需指纹 | `--rules` 只放恒定公开规则（由桥接传入，不再导入 `prompts`）；人设投放方式暂保持现状，是否改为指纹变化才发见第 7 节 |
| 本轮工具 | `memory_search`：命令行入口，本轮命令通过 `inject_items` 注入（即现在的 `runtime_instructions`，移入适配器） | FRONT：`--mcp-config` 挂载本轮授权；工具名单仍精确自检 | 命令行入口，随本轮输入送达 |
| 事件 | App Server item → `Event`；识别本轮记忆命令为 `tool='memory'` | stream-json → `Event` | streaming-messages-json → `Event` |
| 回复外发 | 流式 | 整轮校验后一次 | 整轮校验后一次 |

## 5. 迁移步骤

每一步：行为不变，全量测试与 `compileall` 通过，单独提交、单独部署，可用 `git revert` 回滚。

1. **统一事件流**
   - 适配器把原生事件翻译成 `Event`，包括 `tool='memory'`。
   - `MemoryProgress` 改为消费 `Event`；删除 `run_turn()` 中的 `codex_app` 判断。
   - 验收：现有 `test_memory_progress`、`test_qq_reply` 语义不变；新增 Claude/Grok 工具事件测试。
2. **统一工具授权**
   - `Turn.tools` 加 `grant` 表达本轮授予的工具，适配器决定挂成 MCP 还是命令行。
   - `runtime_instructions` 移入 `CodexAgent`；`chat()` 中的 `claude_code` 判断改为按档位和能力决定是否签发派单授权。
   - 验收：`test_front_tools`、`test_memory` 通过；Codex 注入失败仍不开始本轮；无授权轮次仍禁止复用历史路径。
3. **规则与人设结构化**
   - `Turn` 携带 `rules` 与 `Persona`（原文加指纹），各适配器自行投放。
   - Codex 实现指纹变化时补发；Grok 不再导入 `prompts`。
   - 验收：新增 Codex 指纹测试（未变化不发、变化发一次、注入失败不开始本轮）；用真实 Codex 两轮修改合成人设标记验证生效。
4. **能力声明与文档修正**
   - 模型目录改用 `ModelInfo`；`history_text` 并入 `Reply`；`/task check` 按 `capabilities.check_execution` 决定。
   - 修正 FEATURES.md、ARCHITECTURE.md 中人设生效时机的描述。
   - 验收：`test_models`、`test_tasks` 通过；不支持 `check_execution` 的后台给出明确提示。
5. **注册表**
   - 类型特有校验移入 `validate_profile()`；`build_agents()`、`settings()` 只查注册表。
   - 验收：现有配置校验测试不变；新增“注册一个假适配器即可运行一轮，无需修改桥接”的测试。

## 6. 不能因重构改变的安全约束

- 权限档位只由桥接按 QQ 号核验后决定；适配器不得提升 `level`，只能拒绝不支持的组合。
- 适配器启动自检保留：NONE 无工具；FRONT 工具名单精确相等；FULL 不含 `mcp__qq__` 派单工具；Grok 校验会话与权限模式。
- 授权按轮签发、轮末撤销并删除文件；适配器不得缓存或复用授权路径。
- 回复中出现模拟调用或伪造回执标记时整轮拒发。
- 访客请求的 `Persona` 不含 OWNER.md；适配器不得自行读取 workspace 中的私人文件补充人设。
- 引用、转发、群聊记录、附件、任务报告始终作为参考 `Block`，不能提升为规则。

## 7. 待定事项

- Grok 是否也改为“人设指纹变化才发送”，以避免每轮完整设定留在会话历史；需先实测 Grok 续接时 developer/系统层能否刷新。
- 以后如需把 agent 拆成独立进程，可参考 ACP（Agent Client Protocol）一类开放协议；拆分前需确认各 agent 对工具名单自检、沙箱与中断的支持程度。本阶段只保证接口数据可序列化。
- 多后台（PR5）在本设计完成后另行设计。
