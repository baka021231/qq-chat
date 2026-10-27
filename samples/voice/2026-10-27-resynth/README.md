# 原参考句复刻测试

目标句与模型包参考原文逐字相同：

> 同桌，你好~我是拉贝尔学部的爱弥斯，和你一样。

参考音频与前几轮相同，可对照 [原始参考录音](../2026-10-27/reference-original.wav)。这三条是模型重新合成的结果，没有复制或拼接原始录音。

- 运行命令已去掉 `--reference-text-file`，没有在请求中传 `prompt_text`。
- 特意绕过聊天文字清洗，保留目标句中的“~”；模型内部正常文本前端仍会处理输入。
- temperature=1.0，其余为 cut3、0.35 秒片段静音、15 毫秒首尾淡入淡出、top_k=15、top_p=1.0、repetition_penalty=1.35、speed_factor=1.0、max_steps=500。
- 日常默认种子现为 7；本实验另外显式生成种子 9 和 1。日常语音保持关闭。

| 种子 | 生成样本 | 音频时长 | 纯合成耗时 |
| --- | --- | --- | --- |
| 7 | [resynth-7.wav](resynth-7.wav) | 5.60 秒 | 2.18 秒 |
| 9 | [resynth-9.wav](resynth-9.wav) | 4.56 秒 | 1.70 秒 |
| 1 | [resynth-1.wav](resynth-1.wav) | 3.96 秒 | 1.48 秒 |

三条均实际使用 CUDA。`manifest.json` 保存实际种子、参考文案哈希、覆盖状态、目标文字及计时；`SHA256SUMS.txt` 可校验文件。首条请求耗时包含模型加载，纯合成耗时另列。助手没有试听评分，保留给主人分析。

```sh
python3 tools/resynth_reference.py --output-dir <新的样本目录>
```
