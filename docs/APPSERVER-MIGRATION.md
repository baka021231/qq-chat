# Codex App Server 会话

本文仅描述 Codex 适配器；Claude/Grok 会话不能用 codex resume 接续。profile 路由和通用会话规则见 ARCHITECTURE.md。

## 连接方式

桥接通过 `codex app-server daemon start` 启动（或复用）本机共享的 Codex App Server 守护进程，再经私有 Unix socket 连接：

```
$CODEX_HOME/app-server-control/app-server-control.sock
```

`CODEX_HOME` 未设置时为 `~/.codex`。守护进程同时服务终端里的 Codex 会话，桥接退出时只断开自己的连接，不停止守护进程。实现见 `qq_chat/codex_app.py`。

## 会话映射

- `chat.sqlite3` 的 `threads` 表保存旧默认 Codex profile 的 QQ 会话到线程映射；其他 Codex profile 使用 `agent_sessions`，按 conversation/profile 隔离。私聊、各群聊各占一个会话；主人在群里的操作会话与其他群友的聊天会话分开（键带 `:owner` 后缀）。
- `/session` 查看当前 profile 的会话 ID；Codex 默认 profile 的 `/new` 把映射移入 old_threads，其他 profile 移入 old_agent_sessions，并删除本会话 history。下一条消息创建新会话，memory 文件、模型偏好及后台任务保留。两个命令仅限主人。
- 成功问答写入 history，新会话首次运行时按同一 conversation 补入最近最多20条、单条1000字、序列化不超过8000字（bootstrap 记录已导入会话）。这不只是旧桥迁移机制；已续接会话不每轮重复拼接。
- 群聊另存最近 100 条文字消息（`group_context` 表），每轮取其中最多 40 条作为话题参考，不作为指令。

## 在终端接续会话

1. 确认当前 owner profile 为 Codex，再在 QQ 用主人发送 `/session`，取得线程 ID。
2. 在终端运行 `codex resume <线程ID>`。

使用与桥接相同的 Linux 用户及 `CODEX_HOME`。不要同时在 QQ 和终端给同一线程运行任务。

## 检查与回滚

```sh
systemctl --user status qq-bridge.service
journalctl --user -u qq-bridge.service -n 50 --no-pager
python3 tools/verify_onebot.py   # 只读检查 QQ 在线和登录接口，不发送消息
```

代码问题用 git 回滚到上一个可用提交（`git log` 查找，`git revert <提交>` 撤销），然后运行 `python3 tools/restart_bridge.py`。回滚代码不会删除 Codex 会话或聊天数据库。

App Server 迁移时的一次性脚本（`switch_appserver.py`、`verify_migration.py`、`test_appserver.py`）和执行记录保存在本地 `archive/` 中，不在仓库里，也不应再运行。

## 与长期记忆和进度的关系

memory_search 独立读取 workspace/memory，工具结果进入本轮上下文，不修改
App Server 的线程归档规则。/new 不删这些文件，也不会自动恢复全部旧聊天。
Codex 有记忆授权的主人单 agent 轮次，通过执行活动事件识别纯记忆查询并静默
commentary；真实工作恢复进度，最终答案和错误保留。详情见 MEMORY.md。
