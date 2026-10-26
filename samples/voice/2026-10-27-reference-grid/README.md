# 参考文案标点 × 种子 × 不同句子

3 个种子（7、9、1）× 两版参考文案 × 5 句，共 30 条实际生成 WAV，全部使用 CUDA。temperature 固定为 1.0。

## 固定与变化的条件

- 模型、参考音频及其余采样/拼接参数保持不变；参考音频 SHA256 见 manifest.json。
- 两版参考文案只在同一个位置使用“~”或“！”。每个请求都核对实际文案的 SHA256。
- `tilde`：同桌，你好~我是拉贝尔学部的爱弥斯，和你一样。
- `exclamation`：同桌，你好！我是拉贝尔学部的爱弥斯，和你一样。
- 文件命名：`种子-参考文案版本-句子编号.wav`，例如 `07-tilde-01.wav` 与 `07-exclamation-01.wav`。
- tone 只是句子内容语气的标签，没有添加模型情感参数；未开启日常语音，也未根据未试听结果替换默认种子。

## 按句子对比

每组中两版使用完全相同的目标句子。链接后标明音频时长；请按原声相似度、自然度及不同语气的一致性试听分析。

### 01 · 开心

> 太好啦！我们终于把这件事做成了，今天一定要好好庆祝。

| 种子 | 参考文案 ~ | 参考文案 ！ |
| --- | --- | --- |
| 7 | [07-tilde-01.wav](07-tilde-01.wav)（5.08 秒） | [07-exclamation-01.wav](07-exclamation-01.wav)（5.88 秒） |
| 9 | [09-tilde-01.wav](09-tilde-01.wav)（5.00 秒） | [09-exclamation-01.wav](09-exclamation-01.wav)（5.32 秒） |
| 1 | [01-tilde-01.wav](01-tilde-01.wav)（5.16 秒） | [01-exclamation-01.wav](01-exclamation-01.wav)（5.16 秒） |

### 02 · 平淡

> 我把资料放在桌上了，等你有空的时候再看就好。

| 种子 | 参考文案 ~ | 参考文案 ！ |
| --- | --- | --- |
| 7 | [07-tilde-02.wav](07-tilde-02.wav)（3.76 秒） | [07-exclamation-02.wav](07-exclamation-02.wav)（3.56 秒） |
| 9 | [09-tilde-02.wav](09-tilde-02.wav)（3.72 秒） | [09-exclamation-02.wav](09-exclamation-02.wav)（3.80 秒） |
| 1 | [01-tilde-02.wav](01-tilde-02.wav)（3.84 秒） | [01-exclamation-02.wav](01-exclamation-02.wav)（4.08 秒） |

### 03 · 问句

> 你今天过得怎么样？有没有什么有趣的事情想和我说？

| 种子 | 参考文案 ~ | 参考文案 ！ |
| --- | --- | --- |
| 7 | [07-tilde-03.wav](07-tilde-03.wav)（3.96 秒） | [07-exclamation-03.wav](07-exclamation-03.wav)（3.92 秒） |
| 9 | [09-tilde-03.wav](09-tilde-03.wav)（4.28 秒） | [09-exclamation-03.wav](09-exclamation-03.wav)（4.04 秒） |
| 1 | [01-tilde-03.wav](01-tilde-03.wav)（3.80 秒） | [01-exclamation-03.wav](01-exclamation-03.wav)（3.84 秒） |

### 04 · 安慰

> 没关系，慢慢来。我在这里陪着你，我们一起想办法。

| 种子 | 参考文案 ~ | 参考文案 ！ |
| --- | --- | --- |
| 7 | [07-tilde-04.wav](07-tilde-04.wav)（4.71 秒） | [07-exclamation-04.wav](07-exclamation-04.wav)（4.39 秒） |
| 9 | [09-tilde-04.wav](09-tilde-04.wav)（4.19 秒） | [09-exclamation-04.wav](09-exclamation-04.wav)（3.91 秒） |
| 1 | [01-tilde-04.wav](01-tilde-04.wav)（4.87 秒） | [01-exclamation-04.wav](01-exclamation-04.wav)（4.19 秒） |

### 05 · 认真

> 先确认每一步的结果，再决定下一步怎么做。这一次，我会认真完成。

| 种子 | 参考文案 ~ | 参考文案 ！ |
| --- | --- | --- |
| 7 | [07-tilde-05.wav](07-tilde-05.wav)（5.59 秒） | [07-exclamation-05.wav](07-exclamation-05.wav)（5.67 秒） |
| 9 | [09-tilde-05.wav](09-tilde-05.wav)（5.71 秒） | [09-exclamation-05.wav](09-exclamation-05.wav)（5.43 秒） |
| 1 | [01-tilde-05.wav](01-tilde-05.wav)（5.75 秒） | [01-exclamation-05.wav](01-exclamation-05.wav)（5.79 秒） |

## 清单与复现

`manifest.json` 保存每条原文、实际送入文字、参考文案、参数、时长、纯合成耗时及校验和；`SHA256SUMS.txt` 可独立验证文件。所有 WAV 均为 32 kHz、单声道、PCM16。

30 条 WAV 共 8,858,920 字节，纯合成耗时合计 53.72 秒。首条往返耗时包含模型加载，其余复用同一进程，不可直接混作推理性能比较。

```sh
python3 tools/reference_voice_grid.py --output-dir <新的输出目录>
```

复现时须使用相同模型、参考音频和命令参数；工具拒绝覆盖旧目录，并要求日常语音关闭。原先“种子 7 最接近，其次 9 和 1”的排序来自主人上一轮试听，本批没有程序生成的主观评分或优选结论。
