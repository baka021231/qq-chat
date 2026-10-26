# Codex App Server 会话

## 连接方式

桥接通过 `codex app-server daemon start` 启动（或复用）本机共享的 Codex App Server 守护进程，再经私有 Unix socket 连接：

```
$CODEX_HOME/app-server-control/app-server-control.sock
```

`CODEX_HOME` 未设置时为 `~/.codex`。守护进程同时服务终端里的 Codex 会话，桥接退出时只断开自己的连接，不停止守护进程。实现见 `qq_chat/codex_app.py`。

## 会话映射

- `chat.sqlite3` 的 `threads` 表保存 QQ 会话到 Codex 线程的映射。私聊、各群聊各占一个会话；主号在群里的操作会话与其他群友的聊天会话分开（键带 `:owner` 后缀）。
- `/session` 查看当前会话的线程 ID；`/new` 把旧映射移入 `old_threads` 并开始新会话。两个命令仅限主号。
- 旧桥接留下的 `history` 聊天记录只在线程第一次使用时导入一次（`bootstrap` 表记录已导入的线程），不再每轮拼接历史。
- 群聊另存最近 100 条文字消息（`group_context` 表），每轮取其中最多 40 条作为话题参考，不作为指令。

## 在终端接续会话

1. 在 QQ 用主号发送 `/session`，取得线程 ID。
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
