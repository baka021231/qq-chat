# QQ 聊天桥接

NapCat OneBot 11 与本机 Codex 会话之间的桥接。

## 目录

| 路径 | 用途 |
| --- | --- |
| `qq_chat/` | 桥接程序包，用 `python3 -m qq_chat` 启动 |
| `qq_chat/bridge.py` | 消息接收、权限、会话与回复调度 |
| `qq_chat/codex_app.py` | Codex App Server 客户端 |
| `qq_chat/prompts.py` | 发给模型的提示词（权限指令、历史与群聊上下文、附件和图片说明） |
| `qq_chat/reply.py` | QQ 回复分段与流式发送 |
| `qq_chat/files.py`、`qq_chat/images.py` | 文件和图片接收 |
| `tests/` | 离线回归测试 |
| `tools/` | 当前可用的连接检查、重启工具 |
| `docs/FEATURES.md` | 功能说明：能做什么、限制、不支持的功能、已知问题 |
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
| `workspace/` | QQ 工作台规则（`AGENTS.md`）与用户项目 |

配置和聊天数据属于私人数据，不应提交或公开。

## 配置

复制 `config.example.json` 为 `config.json` 后填写：

| 键 | 说明 |
| --- | --- |
| `ws_url`、`onebot_token` | NapCat OneBot WebSocket 地址和 token |
| `codex_path` | `codex` 可执行文件路径 |
| `owner_qq` | 主号 QQ，只有它可以授权操作 |
| `allow_all_groups` | `true` 时响应机器人加入的所有群，此时忽略 `allowed_groups`；省略时也按 `true` 处理 |
| `allowed_groups` | `allow_all_groups` 为 `false` 时才生效的群号白名单 |
| `mention_qq` | 群聊回复这些 QQ 号的消息时自动 @ 对方 |
| `group_user_blacklist` | 群聊用户 QQ 号列表，默认空；跨群静默忽略，动态拉黑会保存，主号豁免。主号用 `/blacklist` 查看、`/block QQ号` 添加、`/unblock QQ号` 解除 |
| `show_tool_progress` | 进度中是否推送工具调用摘要，默认 `false` |

不论哪种群设置，群聊都需要 @ 机器人或喊触发词才会回复。

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

## 版本管理

代码用 git 管理。修改前确认工作区干净，改完运行测试再提交；改功能时在同一个提交里更新 `docs/FEATURES.md` 和 `docs/CHANGELOG.md`；出问题时用 `git revert` 回滚后重启。不再新建 `archive/backups/` 备份。

新增测试放入 `tests/`，维护工具放入 `tools/`，避免在根目录堆积临时文件。

## 历史文件

本机的 `archive/` 保留引入 git 前的旧文件；其中脚本的相对路径和报告中的绝对路径反映整理前的布局，不作为当前维护入口。不要直接运行旧激活脚本，以免恢复过时版本。移动清单在 `archive/organization-manifest.json`。
