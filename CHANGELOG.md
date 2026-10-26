# 变更记录

## 2026-10-26

### 配置与文档整理

- `config.json` 显式写入 `"allow_all_groups": true`，与实际行为一致（代码在缺省时也按 `true` 处理，此时 `allowed_groups` 不生效）。
- 新增 `config.example.json` 配置模板，README 新增配置项说明。
- 重写 `docs/APPSERVER-MIGRATION.md`：改为描述当前的共享守护进程与 Unix socket 连接，回滚方式改为 git；删去指向已归档脚本的路径。
- README 区分仓库内文件与仅在本机的文件，补充版本管理约定。
- 删除 `qq_bridge.py` 中未使用的 `os`、`tempfile` 导入；清理根目录残留的 `__pycache__/`。

### 初始提交

- 引入 git，推送到 GitHub。`.gitignore` 排除配置、聊天数据库、日志、`archive/` 和 `workspace/`。
- 群聊自动 @ 的对象从硬编码 QQ 号改为配置项 `mention_qq`。
