# 爱弥斯：随机种子 × temperature 扫描

30 条真实生成 WAV，种子 0–9 × temperature 0.7 / 1.0 / 1.3。全部使用 CUDA 完整模型、同一已确认参考音频及修正后的参考文案。日常语音开关保持关闭。

原始测试文字：

> 同桌，你好~我是爱弥斯。今天完成了3项测试……准备好了吗？Ow< 😊

实际送入合成的文字：

> 同桌，你好！我是爱弥斯。今天完成了三项测试，准备好了吗？

除 seed 与 temperature 外，固定 cut3、0.35 秒静音、15 毫秒淡入淡出、top_k=15、top_p=1.0、repetition_penalty=1.35、speed_factor=1.0、max_steps=500。参考文案为：

> 同桌，你好！我是拉贝尔学部的爱弥斯，和你一样。

## 按参数试听

每个链接对应一个 WAV，括号为音频时长。助手未试听或指定“最好”的组合。

| seed | temperature 0.7 | temperature 1.0 | temperature 1.3 |
| --- | --- | --- | --- |
| 0 | [seed-00_temp-0p7.wav](seed-00_temp-0p7.wav)（5.47 秒） | [seed-00_temp-1.wav](seed-00_temp-1.wav)（5.59 秒） | [seed-00_temp-1p3.wav](seed-00_temp-1p3.wav)（6.07 秒） |
| 1 | [seed-01_temp-0p7.wav](seed-01_temp-0p7.wav)（6.39 秒） | [seed-01_temp-1.wav](seed-01_temp-1.wav)（5.55 秒） | [seed-01_temp-1p3.wav](seed-01_temp-1p3.wav)（5.55 秒） |
| 2 | [seed-02_temp-0p7.wav](seed-02_temp-0p7.wav)（6.23 秒） | [seed-02_temp-1.wav](seed-02_temp-1.wav)（5.47 秒） | [seed-02_temp-1p3.wav](seed-02_temp-1p3.wav)（5.55 秒） |
| 3 | [seed-03_temp-0p7.wav](seed-03_temp-0p7.wav)（6.15 秒） | [seed-03_temp-1.wav](seed-03_temp-1.wav)（5.95 秒） | [seed-03_temp-1p3.wav](seed-03_temp-1p3.wav)（5.67 秒） |
| 4 | [seed-04_temp-0p7.wav](seed-04_temp-0p7.wav)（5.63 秒） | [seed-04_temp-1.wav](seed-04_temp-1.wav)（4.95 秒） | [seed-04_temp-1p3.wav](seed-04_temp-1p3.wav)（6.23 秒） |
| 5 | [seed-05_temp-0p7.wav](seed-05_temp-0p7.wav)（6.07 秒） | [seed-05_temp-1.wav](seed-05_temp-1.wav)（6.19 秒） | [seed-05_temp-1p3.wav](seed-05_temp-1p3.wav)（6.19 秒） |
| 6 | [seed-06_temp-0p7.wav](seed-06_temp-0p7.wav)（6.19 秒） | [seed-06_temp-1.wav](seed-06_temp-1.wav)（6.27 秒） | [seed-06_temp-1p3.wav](seed-06_temp-1p3.wav)（6.11 秒） |
| 7 | [seed-07_temp-0p7.wav](seed-07_temp-0p7.wav)（5.43 秒） | [seed-07_temp-1.wav](seed-07_temp-1.wav)（6.03 秒） | [seed-07_temp-1p3.wav](seed-07_temp-1p3.wav)（6.11 秒） |
| 8 | [seed-08_temp-0p7.wav](seed-08_temp-0p7.wav)（5.43 秒） | [seed-08_temp-1.wav](seed-08_temp-1.wav)（4.83 秒） | [seed-08_temp-1p3.wav](seed-08_temp-1p3.wav)（5.11 秒） |
| 9 | [seed-09_temp-0p7.wav](seed-09_temp-0p7.wav)（6.39 秒） | [seed-09_temp-1.wav](seed-09_temp-1.wav)（5.51 秒） | [seed-09_temp-1p3.wav](seed-09_temp-1p3.wav)（6.55 秒） |

## 记录与复现

`manifest.json` 保存每条的实际 seed、temperature、模型版本、参考文案、纯合成耗时、请求往返耗时和 SHA256。首条往返耗时含模型加载，其余条目复用同一进程。全部为单声道 32 kHz、PCM16 WAV。

30 条共 11,192,360 字节；纯合成耗时合计 65.17 秒，中位数 2.13 秒。单次扫描只用于本批对比，不能代表一般性能。

复现命令（拒绝覆盖已有目录）：

```sh
python3 tools/batch_voice.py --output-dir <新目录> --seeds 0 1 2 3 4 5 6 7 8 9 --temperatures 0.7 1.0 1.3
```

仅改变种子和采样温度并不保证更像参考音频；请按同一句话试听选择，后续可按文件名准确复现。
