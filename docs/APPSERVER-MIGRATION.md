QQ 桥接使用 codex app-server 的 stdio 通道，每个桥接进程只有一个 App Server。
聊天映射保存在 chat.sqlite3 的 threads 表；/session 查看线程 ID，/new 归档旧映射并开始新会话。
旧聊天记录首次导入一次；不再每轮拼接16条历史，也不再按40条删除聊天日志。
主号私聊和群聊保持各自原来的权限约束。Windows 挂载文件仅允许读取。

检查：
  systemctl --user status qq-bridge.service
  journalctl --user -u qq-bridge.service -n 50 --no-pager

恢复旧桥接（不会删除 Codex 会话或聊天数据库）：
  python3 /home/baka/projects/qq-chat/switch_appserver.py --rollback

切换结果：appserver-switch-result.json
备份位置：appserver-backup-path
当前主号线程：owner-thread-id
终端接续：codex resume <owner-thread-id里的ID>
使用与桥接相同的 Linux 用户及 CODEX_HOME。不要同时在两个入口给同一线程运行任务。

验证：test_appserver.py 检验跨App Server进程的上下文恢复；
verify_migration.py 检验旧聊天中的仓库名称可回忆且持久会话文件存在；
verify_onebot.py 只读检查 QQ 在线和登录接口。不会主动发送测试消息。
