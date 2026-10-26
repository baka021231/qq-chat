# 本地爱弥斯语音

启用后，正常聊天的最终回复先发文字，再补同内容的语音。进度、命令和错误提示仍只发文字。语音输出是桥接功能，群友没有因此获得调用工具或操作电脑的权限。

## 安装记录

2026-10-27 安装于 Linux 用户目录 `~/.local/share/aemeath-voice/`，未使用 root 或 Windows 程序。

- 模型来源：`https://huggingface.co/Mark42IRP/Aemeath_onnx_GSV_model`。
- 文件：`Aemeath_ONNX_GSV_Saver_INT8.rar`，1,113,345,756 字节。
- 压缩包 SHA256：`0b8cd647a9c2f4e5f6b158437275e57d05fc89cf7b75a77f9c6bf5e82816552c`。下载后对照仓库 `SHA256SUMS.txt`，解压后再次验证内部清单中的 49 个文件。
- 模型目录：`~/.local/share/aemeath-voice/model/ONNX_aimisiV2/`。GPT-SoVITS V2Pro，运行时修订 8，CPU ONNX 混合精度档。
- 隔离环境：`~/.local/share/aemeath-voice/venv/`，CPython 3.11.17。使用 uv 创建，不改变系统 Python；依赖版本见 `requirements-voice.lock`。
- 上游 `requirements.txt` 中的 `jieba-fast` 需要编译。此机没有系统 C 编译器，安装了用户目录内的 `ziglang==0.13.0`，用 `tools/cc` 包装器去掉 Zig 不接受的 `-Wl,--exclude-libs,ALL` 后完成编译；这个 `tools/cc` 在语音运行目录，不是项目的 `tools/`。重新安装时可使用系统编译器，或复用此包装器设置 `CC`。
- 压缩包使用官方 7-Zip Linux 版解压。编译器、解压工具、下载包、模型和虚拟环境不进入 Git。

安装依赖可使用 uv：

```sh
uv venv --python 3.11 ~/.local/share/aemeath-voice/venv
uv pip install --python ~/.local/share/aemeath-voice/venv/bin/python -r docs/requirements-voice.lock
```

无系统编译器时需先单独安装 Zig 并配置上述 `CC` 包装器。不要直接运行未核对的第三方安装脚本。模型包包含作者提供的 `infer.py` 和中文前端，必须与权重一起保留；更换模型包时重新核对校验和、依赖和接口。模型仓库的许可标签为 `other`；本项目没有把该社区模型称为官方配音或保证其授权范围。

## 模块边界

```text
Codex 最终文字 → 原有审核和文字发送 → VoiceReplies 有界队列
                                     ↓
                      ProcessSynthesizer（异步子进程协议）
                                     ↓
                tools/voice_backend.py（独立环境、常驻 ONNX）
                                     ↓
                 PCM16 WAV → Bridge.send_audio → OneBot record
```

`voice.py` 不依赖 QQ 和 Codex，以回调发送音频和提示。`voice_backend.py` 不知道会话、QQ 目标、凭据或聊天历史，只收到待合成文字。`Bridge` 保留目标与权限判断，负责 Base64 音频和 QQ 限速。更换引擎只需替换配置中的进程命令并遵守协议。

输入是 UTF-8 JSON 行 `{"text":"你好"}`；输出是单行 `{"wav":"<Base64 WAV>"}`。失败输出 `{"error":"synthesis_failed"}`。stdout 只用于协议，诊断写 stderr；生产适配器不把后端日志或文字载荷转发到 QQ。一次最多 140 字，后端拒绝超过 160 字；进程常驻，仅加载一次模型。协议异常、超时或取消时终止进程，下次请求新建，避免错配上次输出。

代码块用“代码见文字消息”、链接用“链接见文字消息”代替，Markdown 格式简化；其余正文完整按序切分。语音会晚于文字，长回复可能对应多条语音。全局聊天仍按原队列串行处理，只有合成和语音发送拆为独立队列；不等于已实现前后台聊天分离。

## 配置与运行

参考 `config.example.json` 的 `voice` 对象，将绝对路径替换为本机路径。配置不含额外 API 密钥。默认不启用；`voice.enabled=true` 后重启加载。关闭时设为 `false` 再重启，模型和环境可保留。

语音与文字共用 2～3 秒发送限速，语音发送确认最多等 90 秒；合成默认每段最多等 120 秒。最多排队 8 轮；满时保留文字并提示。断线取消正在进行的合成和等待中的语音，不自动重发旧语音。已经被 QQ 接收而尚未确认的发送不能保证可撤回。

离线测试不发送 QQ 消息：

```sh
python3 -m unittest discover -s tests -q
```

以下命令会真实发送一条测试文字及对应语音，必须由主人明确指定目标：

```sh
python3 tools/test_voice_delivery.py --group <当前授权群号>
python3 tools/test_voice_delivery.py --private-owner
```

`tools/activate_voice.py` 必须从独立 systemd 用户单元启动，不能留在桥接 cgroup 中。它等待指定会话的新一对 user/assistant 历史提交后，核对 Git 提交与干净工作区，重启桥接、检查新进程的连接日志与 QQ 登录状态，并发送启用结果和对应语音。失败时关闭语音；源码仍是预期提交且工作区干净时用 `git revert` 回滚，再重启并检查文字连接。等待超时或源码变化则取消启用。目标仅来自主人本轮明确授权的群，不从历史任意挑选。

## 已执行验证

- 模型压缩包及内部文件校验通过。
- Linux 本地生成单声道 32 kHz PCM16 WAV：5.14 秒、329,004 字节；首次加载与合成共约 10.13 秒。此数字仅代表这次短句测试。
- 使用同一桥接发送接口向主人本轮授权的群发送测试文字与语音，两次 QQ 接口均成功确认。
- 尚未人工试听确认音色相似度；接口成功确认也不等于验证所有客户端的播放效果。
- 不包含语音接收、识别、唱歌或任意 QQ 文件发送。
