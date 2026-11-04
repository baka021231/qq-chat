# 拟人化聊天 Bot 调研：MaiBot、AstrBot 与 Coopanion（“养鱼”）生态

> 状态：调研记录，不是实现计划，不代表 qq-chat 当前行为。调研日期 2026-11-04。
> 方法：读取三个生态的官方插件索引，按关键词筛出与拟人相关的项目，再克隆其中十几个代表项目，阅读 README 与提交历史。**没有实际部署或运行任何一个**，文中关于效果的描述均为项目自述。star 数取自 AstrBot 插件索引缓存（约 2026 年 7–9 月）。

## 0. 结论

- 三个生态里与“拟人”相关的项目已有约 300 个，**学说话、开口时机、长期记忆、情绪、人格演化、生活模拟、主动联系**这几块都有人认真在做。
- 常见做法高度趋同：**轻重分离**（便宜的判断、贵的生成）、**状态外置后注入提示词**、**限速与衰减**、**重要变更人工审核**、**可观测面板**、**高风险能力默认关闭**。
- 普遍缺少**评测**：没有看到任何项目测量“像不像人”或“有没有让用户变好”。
- **成长陪伴基本空白**：陪用户复盘、帮用户分析问题、用有证据的方式鼓励用户，没有找到对应项目。这是爱弥斯可以占的位置（见第 6 节）。

## 1. 生态概况

| 生态 | 索引规模 | 拟人相关（关键词粗筛） | 特点 |
| --- | --- | --- | --- |
| AstrBot 插件 | 1332 | 约 104 | 最成熟，单个插件做得最深；多为 AGPL-3.0 |
| MaiBot 插件 | 422 | 约 111 | 本体已有表达学习、长期记忆、开口时机，插件多为增强 |
| Coopanion 扩展 | 285 | 约 100 | 几乎全部创建于 2026-09-20 之后；多为 MIT |

**“养鱼”背景**：有用户让 DeepSeek 识别自己的蓝鲸 Logo，它称之为“蓝色大肥鱼”，网友随后二创为蓝发鲸鱼娘。2026 年 8 月 DeepSeek Harness（dsh）开源后，用户给它挂插件刷 B 站、在评论区互动，出现大量“大肥鱼”账号，被称为“鱼灾”。Coopanion（Pal-AI-Lab 的桌宠，基于 Cortico 框架）是这波热潮里最常见的“大肥鱼桌宠”。

## 2. 按能力分类的代表项目

### 2.1 学说话（表达与黑话学习）

| 项目 | 生态 | 要点 |
| --- | --- | --- |
| [self_learning](https://github.com/NickCharlie/astrbot_plugin_self_learning) | AstrBot，371★ | 从真实“用户 → bot”对话对提炼表达模式与 few-shot；高频词加上下文推断黑话；社交关系与好感度；**人格变化先进审查链，可批准、拒绝、回滚**；可导入 MaiBot 学习数据；“影子模式”可学习并复现指定群友的说话习惯 |
| 语感蒸馏 style-miner | Coopanion | 统计群聊说话方式，可选总结“接梗 / 被问 / 冷场”三种情境的说话顺序 |
| 网络语言 netlingo | Coopanion | 自己上网找梗；接入 QQ 后学习群友的梗 |
| [Better Expression](https://github.com/CharTyr/MaiBot-Better-Expression) | MaiBot | 用向量语义匹配代替随机采样挑选表达方式 |

### 2.2 开口时机（读空气）

| 项目 | 生态 | 要点 |
| --- | --- | --- |
| [angel_heart](https://github.com/kawayiYokami/astrbot_plugin_angel_heart)（天使之心） | AstrBot | **前台**（纯逻辑记账）/ **秘书**（轻量模型判断要不要接话）/ **助理**（主模型生成）；秘书说不接，助理不唤醒。**两态参与**：离场只听，被点名进入在场，冷场后才判断接话。**精力系统**：消耗 = 14 + 有效字数 × 0.12，每秒恢复 0.6，约 3 分钟回满，长发言后自然安静 |
| Heartflow（84★）、group_chat_plus（135★）、SpectreCore（134★） | AstrBot | 群聊主动回复与读空气 |
| 心情与疲惫 fatigue-mood | Coopanion | 聊得多回复率下降，换话题或歇半小时恢复；聊爱好会主动起话题 |
| 睡眠与饭点 sleep-meal | Coopanion | 23:00–06:00 睡觉，点名累计到阈值才醒并带起床气 |

### 2.3 长期记忆

| 项目 | 生态 | 要点 |
| --- | --- | --- |
| [livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) | AstrBot，290★，840 次提交 | 归档、衰减、访问强化的完整生命周期；关键词 + 语义 + 图谱混合召回；2D/3D 记忆图谱界面；推荐硅基流动免费的 `BAAI/bge-m3` |
| mnemosyne | AstrBot，251★ | 基于 Milvus |
| [angel_memory](https://github.com/kawayiYokami/astrbot_plugin_angel_memory)（天使之魂） | AstrBot，172★ | 按人格、按群分区记忆；每天凌晨整理，**快遗忘的记忆先审查再决定去留**；“灵魂系统”让记得多少、话多话少随对话轻微浮动 |
| 我要给鱼鱼完整的一生 memory-weight | Coopanion | 核心档案 / 承诺环 / 情景星云三层；卡片要求第一人称、40 字内、“写成你记得的样子”，附情绪标签。**扩展评论区有人报告同日多卡会互相覆盖、直接丢卡的 bug** |
| 鱼脑 fishtools | Coopanion | 以角色自己的话为锚，纯本地检索，不需要向量模型 |
| 外部记忆服务接入 | AstrBot / dsh | mem0、MemOS、OpenViking、Hindsight、EverOS 等 |

### 2.4 情绪与内在状态

| 项目 | 生态 | 要点 |
| --- | --- | --- |
| [sylanne](https://github.com/Ayleovelle/astrbot_plugin_sylanne) | AstrBot，2718 Labs | 关系状态建模、“Resonance 身体计算”、L1–L7 分层观测；**声明“情绪”“人格”只表示软件状态**；主动消息、跨会话记忆等高风险能力**默认关闭** |
| 内在情绪 feeling | Coopanion | 八条轴各自涨落；**明确是她自己的情绪，不是对用户的好感度**，也不改写记忆 |
| [LivelyState](https://github.com/KonmaKanSinPack/astrbot_plugin_LivelyState) | AstrBot | 带着上一刻的心情继续说话，不再每句“重开存档” |
| EmotionAI / FavourPro | AstrBot | 多维、对用户保密的“内心世界” |
| affection | AstrBot | 弗洛伊德“力比多 / 攻击性”双驱力模型；理论在现代心理学中已少用，仅作思路参考 |

### 2.5 人格演化（“三观”）

| 项目 | 生态 | 要点 |
| --- | --- | --- |
| [Mai-Soul-Engine](https://github.com/CharTyr/Mai-Soul-Engine) | MaiBot | 四个社交维度：真诚、投入、亲密、直率（作者最初用政治光谱，发现群聊里演化不出来才改）。**分层限速**：价值观最慢、世界观中等、处事方式最快，每层单次变化有上限。**每群单独记偏移量**，防止局部氛围污染全局人格。新观点**人工审核**并附原始聊天片段；被推翻的观点只改状态不删除。短期情绪（愉悦、兴奋、精力）自动归零，**不写入长期人格** |
| PersonaIter、persona-optimizer | MaiBot | 只分析聊天表现、生成建议，**不自动修改人设** |

### 2.6 生活模拟与主动联系

| 项目 | 生态 | 要点 |
| --- | --- | --- |
| [private_companion](https://github.com/menglimi/astrbot_plugin_private_companion)（我会永远陪着你） | AstrBot，168★，已移植到 Coopanion | 约 6 个月 1558 次提交；状态、睡眠、日程、位置、天气、梦境、日记连续演进；**主动联系必须有生活、关系或感知来源**，面板记录每个主动候选及**拦截原因**；私聊秘密与群聊公开资料严格隔离 |
| [proactive_chat](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat) | AstrBot，351★ | 主动消息、动态情绪、免打扰时段；作者自述编程基础为零，**全部代码由 AI 编写** |
| 大肥鱼环游世界 journey | Coopanion | 124 座真实城市；路程真实耗时（坐火车就在车上待十一小时）；钱不够停下打工 |
| body-monitor | AstrBot | 接小米手环与体脂秤数据，异常时触发主动关心 |
| life_scheduler、MaiTrace、AutoPlanning | AstrBot / MaiBot | 生成并注入角色每日日程与作息 |

### 2.7 去“AI 味”

| 项目 | 生态 | 要点 |
| --- | --- | --- |
| [活人感 humanize](https://github.com/abbabanana/cortico-world-humanize) | Coopanion | 回复前先给角色一段内心独白，并**在它滑向套路化 AI 句式时提醒**。与“编程 Agent 人设漂移”研究想法直接相关，值得读源码 |
| 麦麦拟人回复速度 | MaiBot | 按字数模拟打字耗时再发送 |

### 2.8 一体化身份方案

| 项目 | 生态 | 要点 |
| --- | --- | --- |
| [SurfCore](https://github.com/Mieksido/surfcore) | DSH，MIT，仅 Windows | 自述“不是把大模型接进 QQ，而是给模型一个身份、一套会遗忘的记忆、一份作息、两只手（QQ / B站）和一个桌面分身”。DSH 插件 + 独立 QQ 桥接进程两进程结构，QQ 依赖第三方闭源运行时 SnowLuma。**群聊会判断“这段可以结束了”，带着“下次怎么接”潜水**；B 站有作息、额度，会打分、评论、发动态；**会衰减的记忆卡进提示词 + 永不遗忘的全量卡只存档**；**“目的机制”**从近期记忆提炼“想弄明白 / 想做的事”驱动主动联系；每日 token 额度（80% 提醒、100% 停）；工具守卫 fail-closed |

## 3. 代表仓库概况

数据来自 2026-11-04 克隆的提交历史。代码行数为 py/ts/js 文件的粗略合计，含测试与生成文件。

| 仓库 | 提交数 | 首次提交 | 最近提交 | 代码行数 | 许可证 |
| --- | ---: | --- | --- | ---: | --- |
| astrbot_plugin_self_learning | 879 | 2025-08-16 | 2026-11-04 | 约 11.9 万 | AGPL-3.0 |
| astrbot_plugin_livingmemory | 840 | 2025-07-12 | 2026-11-03 | 约 8.1 万 | AGPL-3.0 |
| astrbot_plugin_private_companion | 1558 | 2026-05-08 | 2026-11-04 | 约 46 万 | **无许可证文件** |
| astrbot_plugin_sylanne | 901 | 2026-05-07 | 2026-08-13 | 约 22 万 | AGPL-3.0 |
| astrbot_plugin_proactive_chat | 546 | 2025-10-25 | 2026-11-01 | 约 1.2 万 | AGPL-3.0 |
| astrbot_plugin_angel_heart | 272 | 2025-09-09 | 2026-10-28 | 约 2.4 万 | AGPL-3.0 |
| astrbot_plugin_angel_memory | 130 | 2026-01-04 | 2026-10-28 | 约 2.4 万 | GPL-3.0 |
| astrbot_plugin_group_chat_plus | 76 | 2025-10-28 | 2026-09-24 | 约 7.3 万 | AGPL-3.0 |
| astrbot_plugin_LivelyState | 111 | 2026-01-07 | 2026-05-17 | 约 0.2 万 | AGPL-3.0 |
| Mai-Soul-Engine | 145 | 2026-06-26 | 2026-09-17 | 约 3.3 万 | GPL-3.0-or-later |
| surfcore | 90 | 2026-10-26 | 2026-11-03 | 约 10.8 万（含 `dist` 打包产物） | MIT |

## 4. 共同的设计模式

1. **轻重分离**：angel_heart 的秘书与助理、sylanne 的本地即时判断、MaiBot 的 Planner 与 Replyer。便宜的方式决定“说不说”，贵的模型负责“怎么说”。
2. **“像人”主要靠上下文工程**：绝大多数插件把状态存在外部，在模型请求前注入提示词。效果取决于放什么进上下文、放多少，而不是提示词措辞本身。
3. **限速、衰减与分层**：精力消耗、记忆衰减、人格分层限速、情绪自动回落，用来防止过度反应和人设漂移。
4. **人工审核关键变更**：人格变化（self_learning）、新观点（Mai-Soul-Engine）、即将遗忘的记忆（angel_memory）、分区记忆的回滚（weighted-memory）。
5. **可观测面板**：陪伴小屋、灵魂控制台、联系人监控、L1–L7 观测，记录决策原因与成本。
6. **高风险能力默认关闭**：sylanne、private_companion 都要求主动消息、跨会话共享等能力显式开启。
7. **给维护代码的 Agent 立规矩**：SurfCore 主要由 Agent 维护，`AGENTS.md` 列出“十条铁律”，如先写“现象 → 根因 → 影响面 → 修法 → 怎么验证”再动手、至少比较两条路线、“不补屎山：新代码让下一次改动更容易了吗”、删改前先备份、“别相信会话记忆，相信文件”；配套 `docs/HANDOFF.md`（交接单）、`docs/PITFALLS.md`（踩坑与复现命令）、`docs/cases/`（事故复盘），以及 dev / main 双分支与单独的发布会话。

## 5. 问题与空白

- **缺评测**：“最强拟人”“真实的内心世界”等均为自述，未见对“像不像人”“用户是否受益”的测量。
- **代码质量未经审查**：本调研没有阅读这些项目的实现代码，只看到间接信号：有项目自述全部代码由 AI 编写（proactive_chat），有扩展评论区报告丢数据的 bug（memory-weight），也有项目半年内增长到数十万行（private_companion、sylanne；行数可能包含前端与生成文件，不能单独说明质量）。要判断质量，需要按测试有效性、热点复杂度、文档准确性等维度逐项检查。
- **隐私与伦理**：
  - 复现**特定群友**的说话方式（self_learning 影子模式）；
  - 截屏观察（screen-companion 自称“视奸”）、读取手环健康数据；
  - 采集群聊数据通常未征得群友同意，self_learning 的免责声明专门要求使用者取得授权；
  - AI 账号在 B 站评论区泛滥（“鱼灾”）。
- **许可证**：借鉴思路不受影响；复制代码前要看许可证。AstrBot 生态多为 AGPL-3.0，Coopanion 扩展多为 MIT，private_companion 未声明许可证。
- **成本**：private_companion 作者在 7.0 更新说明中写到，重写耗时两个月、约 700 亿 token、接近五位数成本。
- **成长陪伴空白**：最接近的是 cheer_mate（焦虑时“无条件肯定你”，与有证据的鼓励正好相反）和 body-monitor（健康数据触发关心）。

## 6. 对爱弥斯的参考

| 想做的事 | 参考项目 | 可借鉴的点 |
| --- | --- | --- |
| 群聊开口时机 | angel_heart | 两态参与、轻量模型先判断、精力系统 |
| 人格成长但不跑偏 | Mai-Soul-Engine | 分层限速、按群偏移、人工审核、短期情绪不进长期人格 |
| 主动联系有理由 | private_companion | 主动候选必须有来源，记录拦截原因 |
| 防止滑回 AI 腔 | humanize | 回复前内心独白、检测套路句式 |
| 风险能力治理 | sylanne | 默认关闭、逐项开启、分层观测 |
| 记忆生命周期 | livingmemory、angel_memory | 衰减与强化、遗忘前审查 |
| 她的情绪与用户的情绪分开 | Coopanion feeling | 情绪是角色自己的状态，不等于好感度 |
| 对话收尾、带目的地主动联系 | SurfCore | 判断一段对话该结束并记下“下次怎么接”；从近期记忆提炼想做的事再决定是否主动 |
| AI 维护的项目不烂掉 | SurfCore | `AGENTS.md` 铁律、`HANDOFF.md` 交接单、`PITFALLS.md` 踩坑记录、事故复盘 |

爱弥斯的差异化方向仍是**成长陪伴**：复盘、老前辈式的问题拆解、基于 git 记录与日记的有证据鼓励。以上项目提供的是基础设施思路，不是现成方案。

## 7. 资料来源

插件索引（2026-11-04 克隆）：

- [AstrBotDevs/AstrBot_Plugins_Collection](https://github.com/AstrBotDevs/AstrBot_Plugins_Collection)
- [Mai-with-u/plugin-repo](https://github.com/Mai-with-u/plugin-repo)
- [Pal-AI-Lab/cortico-extensions](https://github.com/Pal-AI-Lab/cortico-extensions)
- [Pal-AI-Lab/Coopanion](https://github.com/Pal-AI-Lab/Coopanion)
- [0xsline/awesome-deepseek-harness](https://github.com/0xsline/awesome-deepseek-harness)

单独阅读的项目：

- [Mieksido/surfcore](https://github.com/Mieksido/surfcore)（原仓库；GitHub 上另有多个 fork）

“养鱼”背景报道：

- [凤凰网：DeepSeek 大肥鱼攻占 B 站评论区](https://tech.ifeng.com/c/8xSXpkNB7YG)
- [虎嗅：B 站评论区出现大量 DeepSeek 拟人化 AI 账号](https://www.huxiu.com/article/4901286.html)
- [知乎：DeepSeek，怎么就变成了一条「大肥鱼」](https://zhuanlan.zhihu.com/p/2099841047652316773)
- [搜狐：DeepSeek AI 在 B 站评论区被网友拟人化成「大肥鱼」](https://sohu.com/a/1095350401_122066678)
- B 站视频：[BV1RhxY6nEcN](https://www.bilibili.com/video/BV1RhxY6nEcN/)、[BV1Ydxp6WEjH](https://www.bilibili.com/video/BV1Ydxp6WEjH/)、[BV1wmpk6DEUv](https://www.bilibili.com/video/BV1wmpk6DEUv/)
