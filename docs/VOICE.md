# 本地爱弥斯语音

## 2026-11-01 调整：主参考、采样解码、语速与 speak 风格

10-28 接入时的设置让输出偏慢、偏平，三四个字的短句会念坏。对照源码与本地对照样本（`workspace/voice-tests/20261101-claude-diagnosis/`，经主人试听）定位到三处：

- IndexTTS2 的 `spk_audio_prompt` 不只决定音色。GPT 每步以它为条件，s2mel 用它的频谱作模板，情绪合并公式为 `基础 + alpha × (情绪参考 − 基础)`，基础情绪也取自它。原来以 `音色.wav`（自我介绍）为主参考，换情绪参考只能小幅改变语气。现在各风格以 `跟读日常平和原声.wav` 为主参考，不再单独传情绪参考。
- 原先用贪心单 beam 解码，现改为 IndexTTS2 默认采样：top_p 0.8、top_k 30、temperature 0.8、3 beam、repetition_penalty 10。种子仍默认 42，同一文字同一风格的结果可复现。
- 补上发行包参考说明里的 1.15 倍速。实现方式是在启动时把 s2mel 的长度换算 `code_lens × 1.72` 乘以 1/1.15，与 IndexTTS-2.5 的 duration_factor 相同，不变调。引擎这一行若有变化，后端拒绝启动，不会悄悄忽略。

协议新增可选的 `style`（日常／开心／安慰／激动／压着怒气／哽咽，默认日常）和 `intensity`（低／中／高，默认中），未知值拒绝，不接受路径。风格对应的参考音频与情绪向量只写在 `tools/indextts_backend.py` 的 PRESETS 中：日常不加向量；开心为 happy 0.5、surprised 0.1；安慰为 sad 0.1、melancholic 0.15、calm 0.3。强度按 0.6／1.0／1.4 缩放，再经引擎自带的偏置和总和 0.8 上限。激动、压着怒气、哽咽三种直接以对应原声为主参考（`发声状态/激动.wav`、`终极版/压住情绪.wav`、`发声状态/哽咽.wav`），不加向量，也不受强度影响；它们说得慢、停顿长，是原声本身的节奏。但模型会照搬原声里的多秒空白（压着怒气实测一处 3.3 秒），所以后端对所有风格把句中超过 1 秒、低于峰值 40 dB 的空白从中间截短到 1 秒，开头结尾不动。三种的含义先按主人试听确定，再对照剧情：三段原声都出自远航星场景 106（爱弥斯向漂泊者说出故乡切断通讯的真相）。激动（`106_27`）是替漂泊者抱不平、越说越激动带哭腔，主人补充也可用于喜极而泣；压住情绪（`106_67`）是平静地说出“我也看到了……他们的拒绝”，压着怒火和难过陈述事实；哽咽（发声状态版是 `106_31` 的剪辑）是承认“我不知道怎么才能帮上你”，即她自己的痛苦与愧疚。规则按这些处境描述各风格，并点明与日常、安慰的区别。前后文见本地素材库 `workspace/voice-library/`。文件名里的“压住情绪”不是安慰，这三段都不用于哄人。`终极版/自信.wav` 听起来像电话里的声音，原声也是，不采用；根目录各片段和 `跟读连贯原声.wav` 底噪高、垫有背景音，不适合作主参考。候选对比见本地 `workspace/voice-tests/20261101-style-references/`。只发 `text` 的旧请求按日常处理，所以 reply 模式的朗读也换成新设置。回退到 ONNX 后端时，它会忽略 style 和 intensity，speak 仍可用，但没有风格差异。

试听结论：主人认可开心风格与 1.15 倍速，D／E 组的长句。孤立短句在全部五组配置下仍不行，原因未确认，因此 speak 工具拒绝少于 6 字的口语稿，规则要求把它和前后句合起来说。安慰风格、强度三档以及 `20261101-speak-styles/` 的经生产链路样本尚待试听。经 `ProcessSynthesizer` 实测，首条请求含模型加载耗时 75 秒，之后每条 2.6～4.4 秒；首条离 120 秒的默认超时较近。

`voice.mode=tool` 后，由主人单 agent 调用 `speak` 自己决定何时说、用哪种风格，见 FEATURES。更多风格（如撒娇、认真）需先挑合适的原声，不能只调向量。

## 2026-11-02：连上 QQ 时预热模型

`voice.preload` 默认开启：每次连上 QQ，桥接在后台给语音后端发一次 `{"warmup": true}`，IndexTTS 后端只加载模型、回 `{"ready": true}`，不合成。断线会关掉语音进程（避免重连后重播旧语音），所以每次重连都会再预热。预热期间到来的语音排在它后面，最慢也只等于原来的冷启动。旧 ONNX 后端不认识预热、回错误时保留进程，只有不回应或退出才关闭。代价是显存常驻约 5 GB，设为 `false` 可恢复首次说话才加载。

## 2026-11-02：按情境从原声库选参考（scene）

固定风格要人工挑参考、写描述，覆盖不了多少情境。现在 speak 可以多传一句 `scene`，只写“想怎么说”（对谁、什么处境、什么语气），后端从本地原声库里找她在相似处境下说过的话，拿那段当主参考合成，不加情绪向量，用原声自己的节奏（1.0 倍速）。匹配分低于 0.45（描述完全不着边）或没配原声库时，按 style 合成，行为和以前一样。门槛起初是 0.6，盲测时“嗔怪熬夜”“晚安”因此退回了安慰；主人认为应当模仿风格最接近的原声，于是放宽。

原声库在本地 `workspace/voice-library/yhx/`，不进 Git，建法见该目录 README：主人解包的远航星 1,324 条语音，本地解码、Whisper 转写，和剧情文本对齐，用声纹认出爱弥斯，共 478 句。其中 302 段干净、确定是她、3–15 秒的原声，由助手按前后文各写了一句语气说明（对谁、什么处境、怎么说），作为检索的主要依据。另有 30 段疑似别的角色借她的声音、或说话人不确定，标为受限，永不选用。

- 检索：bge-small-zh-v1.5（本地文件，约 96 MB）把语气说明、台词加前文转成向量；得分 = 0.75 × scene 与语气说明的相似度 + 0.25 × 要说的话与台词的相似度。8 个典型情境试搜，第一名都对得上（哄睡→“那就睡吧”，撒娇→“可以吗？可以吗？”，抱不平→“是他们主动切断了和你的通讯”等）。
- 质检：每段原声当主参考合成两句固定测试话，Whisper 转写后按拼音比对（同音字不算错），要求两句拼音错误率 ≤10%、合成音色与她的声纹相似度 ≥0.6、语速每秒 2.5–7 字、截短前没有超过 2.5 秒的卡顿。未通过的原声不进入候选，结果在 `qc.json`。
- IndexTTS 自带情绪编码器的 8 类情绪分数也记在 `qc.json`，实测能分清平静与难过、哭腔，但认不出开心、调皮和愤怒，所以目前不参与排序。
- 后端每次检索会在原声库目录追加一行 `usage.jsonl`，只记时间、选中的原声编号和得分，不记 scene 和说的内容，用于事后查“刚才那条用的哪段”。
- 盲测（2026-11-02）：10 个情境各合成固定风格与检索两版，随机标 A/B，主人不知道哪版是哪版。有差别的 8 组里检索版赢 7 组（哄睡、报喜、撒娇、叮嘱、抱不平、被夸、闲聊）；“调皮”一组（选中“这是我们的秘密旅途”）盲测时选了固定风格，主人事后更正为差不多，即 7 胜 1 平。闲聊一组两版同一原声、只差语速，主人选了原速 1.0。样本与答案在本地 `workspace/voice-tests/20261102-scene-ab/`。
- 角色语音库（2026-11-02 加入）：游戏 `RoleVoice/role_lang_aimisi_zh` 包内 194 条，其中 191 条只加密未压缩，用系统 openssl 按已解包大包的同一密钥解密；另 3 条用 Oodle 压缩，未处理。内容是她的角色资料语音：长独白（生日、雷雨夜讲故事、陆医生、莫宁、爱好、美食等）和战斗喊话。15 秒以上的独白按 Whisper 词级时间戳在句末、其次逗号处切成 4–14 秒的段，共 56 个候选，各写一句语气说明；战斗喊话标为慎用不选。质检 53 段通过，放在本地 `workspace/voice-library/role/`。两库合计约 297 段可选，过生日、害羞地说担心、聊吃的等情境改由角色库的原声应答。
- 重情绪粗筛（2026-11-02）：建索引时按语气说明标 `heavy`（哽咽、哭腔、愤怒、冷笑、阴冷、低沉叙述等，人工排除两段只是台词带“沉重”的原声），style 为日常、开心、安慰时检索排除这些原声，避免“轻声安慰”配到哭腔。同时规则要求 scene 只写情绪、态度和关系，不写话题：实测“轻声安慰学统计机器学习遇到困难的漂泊者”因话题词配到了“感叹技术进步挺可悲”，去掉话题后候选回到安慰类。
- 启用：在 `voice.command` 末尾加 `--library <原声库目录> --embedder <bge-small-zh 目录>`，两项必须同时给；`--library` 可重复，多个库一起检索，原声编号须跨库唯一，使用记录写在第一个库目录。不加时 scene 被忽略。ONNX 后端同样忽略 scene。

## 当前后端：V5 IndexTTS（2026-10-28）

本机 `voice.command` 已改为 `tools/indextts_backend.py`，日常语音仍为关闭状态；该后端接入阶段的验证当时未重启桥接、未发送 QQ 测试消息；这是历史验证边界，不表示之后其他功能没有重启服务。启用时沿用原有 `VoiceReplies` 队列与发送链路，不改变文字、权限或会话逻辑。ONNX 后端及模型保留作手动回退，下方旧实验记录仅描述 ONNX。

主人提供的 `小爱配音_完整包_V5_20261018.zip` 为 Windows 完整发行包。本次仅提取 Linux 推理所需的 IndexTTS 引擎、V5 和共用声学模型、参考音频及许可证，共 293 个文件、5,959,883,760 字节。没有提取 Windows Python、EXE/DLL、界面与安装器、V3 权重；原 ZIP 在真实合成与保留文件校验成功后删除。保留文件及原 ZIP 的 SHA256、各文件大小记录于模型目录 `manifest.json`。V5 GPT 权重另与包内发布记录核对，SHA256 为 `ee5c19bf7c421d6b9ae74f8ab75411d6a57e220ffe515bd131a80d33d3508ebc`。

- 运行目录：`~/.local/share/aemeath-voice/indextts-v5/`，其中 `engine/`、`models/`、`references/`、`licenses/` 分开保存。
- 隔离 Python：`~/.local/share/aemeath-voice/indextts-venv/bin/python`，CPython 3.11，PyTorch/torchaudio 2.8.0+cu128。系统 Python、原 ONNX 环境及 Windows 挂载盘未改动。
- 后端默认 CUDA，FP16 GPT；只读取本地权重，关闭 QwenEmotion、DeepSpeed、FlashAttention 和自定义 CUDA 编译。模型构造沿用包内内存映射加载方式，严格核对 GPT 权重结构，避免额外的整份 FP32 主内存拷贝。
- （10-28 原设置，已由上节替换）参考为 `references/音色.wav` 及 `references/跟读日常平和原声.wav`，情绪参考强度 0.55。使用贪心解码、单 beam、种子 42、repetition_penalty=5；未移植 Windows 跟读加速与 1.15 倍变速，不承诺与 Windows 输出逐样本一致。
- 原生输出 22,050 Hz 整数幅度音频，用多相滤波重采样为桥接接受的单声道 32 kHz PCM16 WAV，保持时长和音高；每段执行原有 120 秒、8 MiB 上限与完整性检查。
- JSONL 接受 `text` 和可选 `seed`、`style`、`intensity`（后两项见上节），默认种子 42；不接受请求提供的模型或参考音频路径、参考文案及 temperature。stdout 仅输出协议，诊断写 stderr；合成失败保留文字。CUDA 不可用时明确失败，不会悄悄换模型；若需回退，恢复原 ONNX 配置。

本机命令如下；`--device cpu` 仅为显式可选项，本次未做 CPU 大模型实测：

```sh
~/.local/share/aemeath-voice/indextts-venv/bin/python -u \
  /home/baka/projects/qq-chat/tools/indextts_backend.py \
  --package-dir ~/.local/share/aemeath-voice/indextts-v5 --device cuda
```

环境复建需先用 CPython 3.11 创建独立 venv，再安装 `docs/requirements-voice-indextts.lock`；无需执行包内 Windows 程序或第三方安装脚本。该锁定文件包含实际推理导入所需的依赖及其传递依赖，不等同于上游训练/WebUI 的完整环境。

实测样本保存在本地 `workspace/voice-tests/20261028-indextts-v5/`，未发布到远程：

- `first.wav`：5.492 秒；冷启动总耗时 23.722 秒，其中模型构造 12.873 秒、合成及转换 7.408 秒，PyTorch 峰值显存分配约 5,392 MiB。总耗时还含导入等开销，此显存指标也不等于驱动报告的进程总占用。
- `bridge-short.wav`、`bridge-long.wav`：通过生产 `ProcessSynthesizer` 启动新适配器连续请求，同一常驻进程依次生成 4.133 秒和 32.273 秒音频；首条含启动的往返耗时 17.612 秒，134 字长句的热请求往返耗时 16.500 秒。验证了 WAV、非静音、文本清洗和进程关闭，不据此声称音色或发音已人工验收。
- 全量测试运行 182 项，180 项通过，系统环境缺少音频科学库而跳过 2 项重采样测试；在独立语音环境运行新适配器全部 5 项测试通过，包括重采样的频率、幅度、时长校验。`pip check` 通过。

回退仅需将本地 `indextts-v5/bridge-voice-before.json` 的内容恢复到配置的 `voice` 字段，保留其余字段。该快照不含其他配置或凭据。原语音为关闭状态，变更后仍关闭，因此本次无需重启；后续启用或回退运行中的语音须按桥接重启规则操作。

## 原 ONNX 默认与参考原句复刻（历史记录）

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

输入是 UTF-8 JSON 行 `{"text":"你好"}`；输出是单行 `{"wav":"<Base64 WAV>"}`。失败输出 `{"error":"synthesis_failed"}`。stdout 只用于协议，诊断写 stderr；生产适配器不把后端日志或文字载荷转发到 QQ。桥接一次最多发 35 字（约 10 秒，见下），后端拒绝超过 160 字；进程常驻，仅加载一次模型。协议异常、超时或取消时终止进程，下次请求新建，避免错配上次输出。

代码块用“代码见文字消息”、链接用“链接见文字消息”代替，Markdown 格式简化；其余正文完整按序切分。语音会晚于文字，长回复可能对应多条语音。全局聊天仍按原队列串行处理，只有合成和语音发送拆为独立队列；不等于已实现前后台聊天分离。

## 群聊原生语音工具

若希望群聊默认只发语音，设置 `voice.group_voice_only=true`（默认 `false`）。
群聊不发送回复文字或群内流式文字。工具模式下，此开关同时向群里所有人的轮次发放
`speak`（不必另开 `group_enabled`），本轮说明要求她每轮都用 `speak` 说出回复、
自己选 style、intensity 和 scene，这样群聊语音和私聊一样按情景挑原声。
本轮调用过 `speak`，最终正文只记入历史，不再朗读；没调用（如不到 6 字的短句、
忘记调用、工具服务不可用）时，程序把最终正文按默认风格放入语音队列。
合成或 QQ 音频发送失败时向原群发送原文：`speak` 那段发它自己的 text，
自动朗读发原回复，队列满时直接发送原文。私聊与管理命令仍沿用原路径。

工具模式下，`voice.group_enabled=true` 为群聊访客开放唯一的 `speak` 能力。
Codex 通过 App Server 的 `dynamicTools` / `item/tool/call` 调用桥接；访客保持
`read-only` 沙箱和禁网，不再执行 `tools/speak.py`。主人原有终端入口不变。
两条入口复用 `ToolServer.call` 的本轮授权、参数校验及 `VoiceReplies` 队列；
工具不能选择 QQ 目标，群友触发的一轮最多接受四段语音，`queued` 只代表排队。

单条语音控制在 10 秒内：原声库质检的 700 句合成里，日常、开心、安慰中位约 3.9 字/秒，最慢一成约 3.1；激动、压着怒气、哽咽中位约 3.6，最慢一成约 2.8。因此 `speak` 每段最多 35 字（含标点，约 30 个汉字），自动朗读也按 35 字分段；话长就拆成几段依次发送。

当前本机协议只允许在 `thread/start` 注册动态工具。旧访客会话首次启用时，
保留旧会话绑定到归档表，创建带工具的新会话；原有历史导入逻辑带入最近聊天，
不复制完整模型上下文。注册失败保留原绑定；成功后的注册标记跨桥接重启保存，
避免每轮重建会话。旧会话中尚未带入的上下文仍保存在原会话中。

`codex_app.py` 只转发当前 thread/turn 的工具请求，拒绝其他审批请求；
工具内容和权限留在 `tool_server.py`，合成与 QQ 发送仍使用原模块。
重复的同一工具调用编号复用回执，不重复排队；缺失、过期或撤销的授权均拒绝。

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
