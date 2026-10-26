# QQ 聊天桥接

NapCat OneBot 11 与本机 Codex 会话之间的桥接。

## 目录

| 路径 | 用途 |
| --- | --- |
| `qq_bridge.py` | 消息接收、权限、会话与回复调度 |
| `codex_app.py` | Codex App Server 客户端 |
| `qq_reply.py` | QQ 回复分段与流式发送 |
| `qq_files.py`、`qq_images.py` | 文件和图片接收 |
| `tests/` | 离线回归测试 |
| `tools/` | 当前可用的连接检查、重启工具 |
| `docs/` | 项目文档和迁移说明 |
| `archive/backups/` | 原有版本及会话备份 |
| `archive/scripts/` | 一次性更新、迁移和诊断脚本原件 |
| `archive/records/` | 历史执行报告、日志和路径记录 |
| `setup/` | 原有安装材料 |
| `workspace/` | QQ 工作台规则与用户项目 |

运行时继续使用根目录的 `config.json`、`chat.sqlite3`、`qq_bridge.log` 和 `.bridge-restart.lock`。配置和聊天数据属于私人数据，不应提交或公开。

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

需要重启时，在当前回复发送完成后运行：

```sh
python3 tools/restart_bridge.py
```

## 历史文件

`archive/` 保留旧文件以便恢复；其中脚本的相对路径和报告中的绝对路径反映整理前的布局，不作为当前维护入口。不要直接运行旧激活脚本，以免恢复过时版本。

本次移动清单在 `archive/organization-manifest.json`，记录了每项原位置与新位置。两个维护工具整理前的原件保存在 `archive/maintenance-originals/`；需要撤销整理时，结合清单恢复文件位置及工具原件。

以后新增测试放入 `tests/`，维护工具放入 `tools/`，备份放入 `archive/backups/`，避免在根目录堆积临时文件。
