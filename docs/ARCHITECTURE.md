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
                         ├ instructions()  prompts.py + workspace/AGENTS.md
                         ├ thread_for()    恢复或新建 Codex 线程
                         ├ turn_text()     拼接旧历史、群聊上下文
                         └ run_turn()      AppServer.turn()
                               ├ 进度 → relay_progress() ─┐
                               └ 回复 → relay_answer()  ──┤（BubbleStream 切分）
                                                          ▼
NapCat ◀──WebSocket── Bridge.send()  全局锁，间隔 1.5 秒，每条 1600 字
```

## 模块

| 模块 | 职责 | 项目内依赖 |
| --- | --- | --- |
| `qq_bridge.py` | 入口与 `Bridge` 类：OneBot 收发、消息过滤、权限、命令、会话、调度 | 下面全部 |
| `codex_app.py` | `AppServer`：JSON-RPC 客户端、单轮对话事件处理；`progress_text` 生成进度文本 | 无 |
| `prompts.py` | 发给模型的全部提示词与拼接函数 | 无 |
| `qq_reply.py` | `BubbleStream`：把流式回复切成 QQ 气泡 | 无 |
| `qq_files.py` | 安全保存附件（不跟随符号链接、限制大小、不覆盖） | 无 |
| `qq_images.py` | 获取并校验图片（域名白名单、文件头识别格式） | `qq_files` |

依赖只从 `qq_bridge.py` 指向其他模块。新功能如果不需要访问 `Bridge` 的状态，优先写成独立模块。

## `qq_bridge.py` 的结构

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

修改权限相关代码时，必须同时更新 `tests/test_command_auth.py` 和 `tests/test_message_policy.py` 中的对应测试。

## 一轮对话的组装

发给 Codex 的内容分两部分：

- **developerInstructions**：`prompts.OWNER` / `GUEST` + `SPEAKER_LABELS` + `WORKSPACE_RULES` + `workspace/AGENTS.md` 全文。每次 `thread/start` 或 `thread/resume` 时传入，所以修改提示词或 AGENTS.md 后，下一轮就生效。
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
| `old_threads` | `/new` 归档的旧映射 |
| `bootstrap` | 已导入过旧历史的线程 |
| `history` | 每轮的问答记录；现在只用于新线程导入（遗留） |
| `group_context` | 每群最近 100 条文字消息 |

建表和字段迁移写在 `Bridge.__init__`，用 `CREATE TABLE IF NOT EXISTS`。新增表或字段时沿用这个方式，旧数据库可以直接升级。

## 外部依赖

- Python 3（当前使用 3.14），第三方库只有 `websockets`
- NapCat（systemd 用户服务 `napcat.service`），在 `ws_url` 提供 OneBot 11 WebSocket
- Codex CLI（`codex_path`），由桥接运行 `codex app-server daemon start` 启动守护进程
- systemd 用户服务 `qq-bridge.service`，工作目录为项目根目录

## 常见改动

- **加一个命令**：在 `worker()` 的 if/elif 中加一个分支。默认只有主号可用；如果要对所有人开放，把命令加进开头的白名单元组 `('/ping', '/help')`。同时在 `/help` 文字和 `docs/FEATURES.md` 的命令表里补上，并在 `tests/test_command_auth.py` 加测试。
- **改提示词**：改 `prompts.py`。机器人的人设和回复格式属于 `workspace/AGENTS.md`，不要写进代码。
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
- 需要文件系统时，用 `patch('qq_bridge.BASE', 临时目录)`。

## 开发约定

- 仓库是公开的。不要提交 `config.json`、数据库、日志、`workspace/`，代码里也不要出现 QQ 号、token 或个人信息；按人设置的值放进配置。
- 改功能时，在同一个提交里更新 `docs/FEATURES.md` 和 `docs/CHANGELOG.md`。
- 改完先跑测试再提交；部署时运行 `python3 tools/restart_bridge.py`；出问题时用 `git revert` 回滚。
