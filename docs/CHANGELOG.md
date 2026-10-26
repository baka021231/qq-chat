# 变更记录

## 2026-10-26

### 程序移入 `qq_chat/` 包

- 6 个模块移入 `qq_chat/`，去掉重复的 `qq_` 前缀：`qq_bridge.py` → `qq_chat/bridge.py`，`qq_reply.py` → `reply.py`，`qq_files.py` → `files.py`，`qq_images.py` → `images.py`；`codex_app.py`、`prompts.py` 名称不变。包内改用相对导入。
- 启动方式改为在项目根目录运行 `python3 -m qq_chat`。本机 systemd 服务的 `ExecStart` 已同步修改（服务文件不在仓库里）。
- `tools/restart_bridge.py` 用新方式启动，并能同时识别新旧两种启动方式的进程，从旧版本切换时不会漏掉旧实例。
- 行为不变，测试只更新了导入路径和 patch 目标。

### 新增功能说明与开发文档

- 新增 `docs/FEATURES.md`：角色与权限、收发规则、图片与附件、命令、不支持的功能和已知问题。
- 新增 `docs/ARCHITECTURE.md`：消息流程、模块职责、权限模型、提示词组装、数据库、常见改动和测试写法。
- `CHANGELOG.md` 移入 `docs/`。

### 拆分 `chat()`，提示词移入 `prompts.py`

- 新增 `prompts.py`，集中存放发给模型的提示词：主号与群友的权限指令、发言人标签说明、工作台规则引导语、旧历史导入、群聊参考记录、附件与图片说明。
- `Bridge.chat()` 拆为 `instructions()`（构造指令并读取 `workspace/AGENTS.md`）、`thread_for()`（恢复或创建线程）、`turn_text()`（拼接旧历史与群聊上下文）、`run_turn()`（执行回合）和 `relay_progress()` / `relay_answer()`（转发进度与流式回复）。
- 行为不变：在同一组场景下比对新旧版本发给模型的指令、本轮文本和 QQ 发送内容，结果逐字一致。

### 配置与文档整理

- `config.json` 显式写入 `"allow_all_groups": true`，与实际行为一致（代码在缺省时也按 `true` 处理，此时 `allowed_groups` 不生效）。
- 新增 `config.example.json` 配置模板，README 新增配置项说明。
- 重写 `docs/APPSERVER-MIGRATION.md`：改为描述当前的共享守护进程与 Unix socket 连接，回滚方式改为 git；删去指向已归档脚本的路径。
- README 区分仓库内文件与仅在本机的文件，补充版本管理约定。
- 删除 `qq_bridge.py` 中未使用的 `os`、`tempfile` 导入；清理根目录残留的 `__pycache__/`。

### 初始提交

- 引入 git，推送到 GitHub。`.gitignore` 排除配置、聊天数据库、日志、`archive/` 和 `workspace/`。
- 群聊自动 @ 的对象从硬编码 QQ 号改为配置项 `mention_qq`。
