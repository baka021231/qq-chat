# 本地爱弥斯语音

## 当前默认与参考原句复刻

按主人最新要求，日常语音仍关闭，后端默认种子现为 7，temperature=1.0。运行命令已移除 `--reference-text-file`，因此参考文案使用模型包原始 `reference.txt`：“同桌，你好~我是拉贝尔学部的爱弥斯，和你一样。”。先前的 `reference-confirmed.txt` 和所有实验样本作为历史保留，不再由日常命令加载。

```sh
python3 tools/resynth_reference.py --output-dir samples/voice/2026-10-27-resynth
```

该实验使用同一参考音频，目标文字也是上述原句；专门绕过聊天文字清洗，保留“~”。请求只显式传入种子 7/9/1 和 temperature=1.0，不传 `prompt_text`，同时验证实际参考文案 SHA256 与覆盖状态；CPU 回退也必须使用同一参考音频与原文。输出为 `resynth-7.wav`、`resynth-9.wav`、`resynth-1.wav`。其余切分、淡入淡出与采样参数保持不变。

原先 `benchmark_voice.py` 的 before/after 实验仍固定种子 42，工具现在显式传入 42，不受日常默认种子改变影响。需要回退本次配置时，仅恢复本地 `voice-config-before-resynth.json` 的 voice 字段；源码独立提交可回滚。

## 优化步骤与回退（2026-10-27）

原始样本、每步 WAV、指标、GPU 检查和原语音配置快照保存在本地 `workspace/voice-tests/20261027-optimization/`。同一原文、种子 42；耗时同时标注冷启动总耗时和后端可报告的加载/合成耗时，不将不同口径混为速度提升。

GPU 环境为 `~/.local/share/aemeath-voice/gpu-venv/`，完整模型为 `~/.local/share/aemeath-voice/model-fp32/ONNX_aimisiV2/`；原 CPU 环境和节约包不变。完整包校验和为 `06738ab0a0c9e875cee85f3fc25b06b65380081e6041120591e37ebfb709b8ed`。它是非整数权重量化的完整档，上游 RoBERTa 本身仍为 FP16。

后端参数 `--provider auto` 优先 CUDA；`--cpu-model-dir` 指定 CPU 回退模型。主要 ONNX 会话未实际启用 CUDA、初始化错误或推理异常时，释放引擎并加载 CPU 模型，同一请求最多回退一次。响应 `metrics` 给出实际 provider、回退异常类型、加载和合成耗时；桥接只取音频，不把这些指标念出来。`--provider cpu` 强制用 `--model-dir` 的 CPU 推理。

GPU 环境依赖记录于 `requirements-voice-gpu.lock`。`genie-tts` 会依赖 CPU 版 `onnxruntime`；两个发行包共享 Python 模块，普通安装可能覆盖 GPU 二进制。须在最后重新安装 `onnxruntime-gpu==1.22.0 --no-deps --reinstall`，并实际核对 `get_available_providers()` 和模型会话；仅安装了 GPU 包不代表 GPU 已生效。CUDA/cuDNN 库通过 GPU 包的 extras 安装并由 `preload_dlls(directory='')` 加载，WSL 驱动来自系统已有接口。

回退配置：从样本目录的 `voice-config-before.json` 仅恢复 `voice` 字段，保留其他配置；代码按各步骤 Git 提交单独 `git revert`。配置变更在回复结束后重启加载。没有重新开启日常语音。

切分优化使用 cut3、0.35 秒静音及每个内部片段的 15 毫秒淡入淡出，不对静音做交叉淡化。文本清洗在桥接层确定性处理波浪号、颜文字、emoji、省略号与中文数字读法，不追加口语稿生成调用。

仅回退切分与淡入淡出时，在后端命令中加 `--text-split-method cut5 --fragment-interval 0.3 --fade-ms 0`；0 表示关闭淡入淡出，启用时仅允许 10–20 毫秒。模型及清洗不用一并回退。

留样工具示例（不发 QQ、拒绝覆盖）：

```sh
python3 tools/benchmark_voice.py --output-dir workspace/voice-tests/20261027-optimization --name new-comparison
```

`before.wav` 在修改前使用 CPU 节约包生成；`step2-gpu-full.wav`、`step4-segments.wav`、`step5-cleaning.wav` 分别保留模型、切分和清洗阶段。`after.wav` 单独运行，避免与其他性能测试争用资源；一次并行验证的样本另存为 `after-concurrent-check.*`，不用于最终计时对比。每次均使用相同原文及种子 42；清洗后送入模型的文本按步骤改变，操作系统文件缓存也不同，不能仅凭单次冷启动数值推断普遍性能提升。

`cpu-fallback.wav` 在 `CUDA_VISIBLE_DEVICES=-1` 下真实生成，报告 provider=cpu，回退原因 RuntimeError；冷启动 20.620 秒（该次与 GPU 校验并行，不能用于性能优劣结论）。CPU 完整包的条件测试仅在 GPU 无法使用时才必要；本机 CUDA 完整包已跑通，因此未额外跑 CPU 完整包计时。

批量扫描默认用同一句测试原文、种子 0–9、temperature 0.7/1.0/1.3，共 30 条。后端协议接受 `seed` 和 `temperature`，当前缺省为 7 和 1.0；后端回传实际值，批量工具会逐条核对，避免实际忽略参数却声称完成扫描。使用单个常驻进程，首条 roundtrip 包含冷启动，其余不含；清单另有每条纯合成时间。清单包含有效输入与公开模型信息，不包含本机凭据或完整命令。工具不发送 QQ 消息且拒绝覆盖现有输出目录。

```sh
python3 tools/batch_voice.py --output-dir samples/voice/2026-10-27-grid
```

主人试听上一批后认为 temperature 影响较小，种子 7 最接近原声，其次为 9 和 1。这是主人的试听反馈，不是程序计算出的评分。下一轮固定 temperature=1.0，只测试这三个种子与两版参考文案。`tools/reference_voice_grid.py` 生成 5 句不同内容语气（开心、平淡、问句、安慰、认真），每句话在两版中完全相同；参考音频与模型不变，参考文案仅将“~”替换成“！”。后端对每个请求单独使用 `prompt_text`，回传 SHA256 供批量工具核对，不修改已确认的日常参考配置。

```sh
python3 tools/reference_voice_grid.py --output-dir samples/voice/2026-10-27-reference-grid
```

输出如 `07-tilde-01.wav`、`07-exclamation-01.wav`，依次表示种子、参考文案版本、句子编号。tilde 为“~”，exclamation 为“！”。tone 字段只标明测试句的内容语气，不代表额外情感控制。工具要求日常语音处于关闭状态，只生成本地 WAV，不发 QQ 消息。

参考音频 `reference-original.wav`、原文及状态单独保存。主人随后明确确认参考音频并要求修改文本，因此新增 `samples/voice/2026-10-27/reference-confirmed.txt`，只将参考文案中的波浪号替换成感叹号。使用后端参数 `--reference-text-file` 指定该文件，作为 `prompt_text` 同时用于 GPU 与 CPU 回退。原模型包未改写，历史 `after.wav` 仍代表修改参考文案之前的结果；新增 `step3-reference.wav` 表示确认后最后补上的步骤。移除该参数即可回退原模型包文案，不影响其他优化。

当前执行环境没有本地播放器或 PulseAudio/PortAudio 客户端库，助手也没有音频试听工具。可以发送 A/B 语音并核对 QQ 接口确认，但不能据此声称已完成本地与 QQ 的听感对比，需人工试听。

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
