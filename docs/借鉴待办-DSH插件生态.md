# 借鉴待办 · DSH 插件生态里灵枢可以学什么

> **调研日期**：2026-11-25 · 仓库 HEAD：1411136（v5.12）
> **来源**：npm / awesome-dsh-plugin / dsh.so / 凤凰网 / 36kr / 搜狐等公开资料
> **目标**：把 DSH 插件生态里已被验证有效的做法，对照灵枢（纯 Python 标准库、零外部依赖）的现状，拆成**可落地的代码改动**，不是"梦想清单"。

---

## 一、调研清单（每个插件做了什么）

### 1. 人格内核类

| 插件 | 公开信息 | 与用户描述的吻合度 |
|---|---|---|
| **dsh-agent-personality**（yimengsuper2432） | 0-100 情绪计量、世界观、每轮日记、关系网络、主动消息、**内心独白**（inner-voice line beside cost row） | 高——"内置身份特质+心情系统，主动搭话、表达态度"的精确实现 |
| **dsh-feiyu-mood**（Yuuyuko-uu） | 每轮心情判定（不是随机数/关键词硬猜），面板看心情值、情绪维度、时间线 | 高——"身份内核，带情绪权重"的精确实现 |
| **dsh-bili-scroll**（c1overzz） | 每天替用户刷一轮 B 站、读字幕挑出值得的、写日报、值得的 @ 用户 | 中——用户说"人格孵化"，但 dsh-bili-scroll 本身是**工具**而非人格孵化，真正的人格孵化是它背后的 dsh-fish-soul |
| **dsh-fish-soul**（npm 搜索） | 人格、按人记忆（可纠正、有印象、能导入旧聊天记录）、日程自走与主动开口、直连 QQ、抖音/抖音小号私信与自己刷视频、按功能选模型并统计用量、能听会说、收藏表情包、学群里的梗 | 高——用户说的"人格孵化：在信息碰撞中形成独立判断"的完整实现；**拒绝、复盘、沉淀专属性格**都在 |

### 2. 记忆增强类

| 插件 | 公开信息 | 与用户描述的吻合度 |
|---|---|---|
| **dsh-memory-evolve** | 五轨记忆（用户档案 / 全局事实 / 项目关键记忆 / 四轨待办 / 临时便签）、git 分支感知、回合内自我审查、技能自我进化与技能管理器、COI 调度、会话广播、会话搜索 | 高——"跨会话长期记忆偏好/约定，后台迭代避免失忆"的精确实现 |
| **dsh-feiyu-memory / 蓝色大肥鱼记忆** | 跨会话持久人格与日记。**每轮结束后固化模型只回答一句"你想记什么"**，写下的每条日记配一句十几二十字的索引；下一场对话注入人格、手记和最近几条，想知道更早的事它自己调 recall；**索引永远不进它的视野，所以不会被冗余信息淹** | 高——用户说的"记忆带情感标签，会记仇或惦记互动细节"的精确实现（索引法就是"惦记互动细节"的底层机制） |

### 3. 自主交互类

| 插件 | 公开信息 | 与用户描述的吻合度 |
|---|---|---|
| **dsh-speak-up**（Yuuyuko-uu） | 让 agent 在没人跟它说话的时候**主动搭话**——本地掷骰子，掷中就走 DSH 自己的一轮 | 高——"主动发起对话"的最小实现 |
| **WhaleMaid-Autopilot** | 搜不到公开资料，可能是未发布的内部插件或换了名字 | —— |
| **dsh-pet-evolve**（zoahdev） | **5 个进化阶段**（蛋 → 幼体 → 少年 → 成年 → 传奇），canvas 实时绘制；XP 从**真实 agent 信号**获取（已验证规则、完成的 session、工具调用、压缩记录）；一键生成 1200×630 成长分享卡 PNG；零依赖、数据全部留在本机 | 高——用户说的"分 5 阶段成长，进度由真实交互驱动，可导出成长记录"的精确实现 |
| **dsh-agentic-pet** | 桌面宠物（透明 always-on-top），随 agent 状态切换（idle/thinking/working/error），竞速回答批准请求，收集 per-pet touch-affection ledger，暴露表达工具 | 补充——桌面宠物形态，灵枢做面板版可以借鉴它的状态映射 |

---

## 二、灵枢现状（按上述插件对应）

| 能力 | 灵枢已有 | 缺口 |
|---|---|---|
| 情绪/心情 | emotion.py：七情事件、EMA 平滑、auto_appraise、面板心情徽章 | 缺：**内心独白**（token 计费旁的人格态度小字）、**心情时间线面板**（dsh-feiyu-mood 的时间线）、**索引法记忆**（feiyu 的"正文不进上下文，索引先行"） |
| 跨会话记忆 | memory.py：fact/preference/emotion/insight/habit/episode/identity 七种、海马体/皮层/淡忘/归档四层、recall 召回、注入水位 | 缺：**按人记忆**（dsh-fish-soul 的"可纠正、有印象"）、**git 分支感知**（dsh-memory-evolve 的项目隔离）、**技能自我进化**（dsh-memory-evolve 的 skill self-evolve）、**COI 调度 / 会话广播** |
| 主动行为 | cron 插件：dream/daily/backup/conflicts/selfreview/drift/autoarchive 天级任务 | 缺：**会话间概率触发的主动搭话**（dsh-speak-up 的掷骰子）、**外部信息摄入**（dsh-bili-scroll 的 B 站/抖音信息流）——灵枢目前只接收"用户说过的事"，没有"自己去看"的通道 |
| 可视化/成长 | panel.py：overview / persona / memories / knowledge / emotion / diagnostics / daily 共 8 个页签；doctor 13 项体检；failures 账本 | 缺：**agent 成长轨迹**（dsh-pet-evolve 的 5 阶段）——但灵枢已有面板，不需要 canvas，直接做个 **"灵枢自己的体检历史面板"** 就行 |

---

## 三、借鉴清单（按优先级）

### P0 · 改了立即有价值、改动小

#### P0-1 内心独白（借鉴：dsh-agent-personality）

**灵枢现状**：面板侧栏 footer 显示 token 计费、版本号、记忆库路径；没有人格层的实时反馈。
**借鉴点**：在 token 计费行旁加一行小字（灰色、斜体）——当轮对话后，灵枢对自己的简短态度评价（"有点累但很高兴" / "这句说得太冲了" / "好像没理解用户在说什么"）。每轮生成 1 句，上限 30 字。
**改法**：
1. `scripts/panel_server.py` `/api/overview` 新增 `inner_voice` 字段，从 `emotion.mood_now()["label"]` + `rationality.effective` + 最近 N 条记忆的 `emotion.intensity` 拼一条（纯模板 + 词表，不调 LLM，避免多一次开销）；
2. `panel_src/js/core.js` 侧栏 footer 新增一行，`span.inner-voice` 样式；
3. 面板里新增一个开关 `config set show_inner_voice true/false`（默认 false，不喜欢的用户可以关）。
**验收**：面板侧栏在有会话后出现灰色斜体小字；`config set show_inner_voice false` 后消失。

#### P0-2 心情时间线（借鉴：dsh-feiyu-mood）

**灵枢现状**：面板 emotion 页签有 mood meter（圆形情绪仪表）和 emotion arc（最近 N 天情绪波动折线）；但折线只显示 count，不显示具体情绪标签序列。
**借鉴点**：emotion arc 下方加一个"心情流水"——每轮对话的 mood label 按时间排列，hover 看 intensity。dsh-feiyu-mood 用的是面板展示，灵枢已有 emotion.py 的事件流（`events.jsonl`），直接读即可。
**改法**：
1. `scripts/panel_server.py` `/api/emotion` 新增 `timeline` 字段（从 `events.jsonl` 取最近 50 条 genesis 事件，按时间排序，返回 `[{time,label,intensity,category}]`）；
2. `panel_src/js/core.js` emotion 页签新增时间线组件（横向小卡片，按七情配色）；
3. 无配置项，默认显示。
**验收**：emotion 页签出现按时间排列的心情流水，hover 显示强度。

#### P0-3 测试文件入库（用户之前的约束已过时效）

**灵枢现状**：台账第五节第 5 条写"测试文件不入库"——那是 v3.55/v3.56 期间的临时决定（当时测试写得快、多是一次性用例）。现在项目已推进到 v5.12，测试文件数量从 3 个涨到了 40+。
**借鉴点**：dsh-memory-evolve 是零核心修改、零运行时依赖，但有完整测试；灵枢应该恢复测试文件入库——否则新版本的改动没有回归保障。
**改法**：
1. 删除 `.gitignore` 里可能的 tests 排除项（如果有的话）；
2. 台账第五节第 5 条改为"**已撤销**——v5.12 起恢复测试文件入库"。
**验收**：`git add tests/` 不再被 .gitignore 拦住。

### P1 · 结构性但可落地

#### P1-1 索引法记忆（借鉴：dsh-feiyu-memory / 蓝色大肥鱼）

**灵枢现状**：`extract` 把整段对话封存进 hippocampus.jsonl；下一轮 recall 用关键词 + 语义召回，召回到的记忆正文全部进注入 prompt。
**借鉴点**：肥鱼的索引法——每条封存的记忆配一句十几二十字的**摘要索引**（纯 LLM 或纯词表生成），召回时**先只把索引送进模型**，模型选中后才把正文进上下文。这样注入水位压力大幅降低，也避免冗余信息淹模型。
**改法**：
1. `scripts/memory.py` 的 `remember()` 新增可选 `index` 字段（纯词表生成，取首句 + 主题词；如果用户调了 LLM 就用 LLM）；
2. `scripts/retriever.py` 的召回流程拆两步：**先召回索引（最多 5 条，无正文）→ 模型勾选 → 再加载勾选的正文**；
3. 索引落盘在 hippocampus.jsonl 的 `_index` 列或单独的 `hippocampus.index.jsonl`（后者更好迁移）；
4. 过渡期 config `config set use_index_memory false`，默认 false，跑完真机巡检再开 true。
**验收**：注入水位在相同 recall 结果下 token 数下降 ≥40%；召回质量不退化（用 `usage --reply` 的命中率指标测）。

#### P1-2 灵枢体检历史（借鉴：dsh-pet-evolve）

**灵枢现状**：doctor 13 项体检是**一次性命令**；failures.jsonl 是被吞异常账本但只存 kind；self-review 是"自我总结"但不结构化。
**借鉴点**：dsh-pet-evolve 的"XP 从真实 agent 信号获取"——灵枢的真实信号就是 doctor 的每次体检结果 + failures 的 kind 聚合 + self-review 的输出。把这三者接起来，做一个"灵枢最近 30 天的体检历史面板"：
- 每跑一次 doctor，结果自动追加到 `doctor-history.jsonl`（时间戳 + 13 项的 PASS/WARN/FAIL）；
- failures.jsonl 自动聚合为每日 kind 分布；
- panel 新增 **"体检历史"页签**：折线图显示 13 项的 PASS/FAIL 随时间变化、failures 的 kind 饼图、self-review 的最近 3 条；
- 不需要 canvas，灵枢已有 panel，用 ECharts 或纯 CSS timeline 即可。
**改法**：
1. `scripts/cli.py` doctor 分支跑完后写 `<HOME>/doctor-history.jsonl`（追加模式）；
2. `scripts/panel_server.py` 新增 `/api/doctor-history` 接口；
3. `panel_src/js/core.js` 新增 tab "体检历史"，复用 emotion arc 的折线组件；
4. 可选：加"灵枢健康度分数"（0-100，doctor FAIL 扣 10、WARN 扣 3、failures 每 5 条扣 1，每日刷新）。
**验收**：panel 出现新页签；连续 3 天跑 doctor 后折线图有 3 个点；健康度分数与 doctor 结果同方向变化。

#### P1-3 会话间主动搭话（借鉴：dsh-speak-up）

**灵枢现状**：所有输出都是用户触发的（命令、插件 hook）；cron 插件有天级任务（dream/daily），但没有"会话间概率触发"。
**借鉴点**：dsh-speak-up 的"本地掷骰子"——在**每轮用户输入处理完后**，按概率（默认 10%）决定要不要让灵枢自己说一句（不进模型，纯模板 + 词表）。触发条件：
- 用户这一轮没有问问题（只是陈述）；
- 灵枢的 rationality.effective > 30（情绪不激动）；
- 距离上一次主动搭话 ≥ 15 分钟（防刷屏）；
- 模板库：`"你好像在忙{主题}，要不要聊聊？"`、`"刚才提到{关键词}，我有点感兴趣"`、`"嗨，{名字}，记得{某件小事}吗？"`（关键词从最近 N 条 memory 里取）。
**改法**：
1. `scripts/core.py` 的主循环末尾加一个 `_maybe_speak_up()`；
2. 模板库在 `scripts/templates/speak_up.py`；
3. config `config set speak_up_prob 0.15`（默认 0.10，0 关闭）；
4. 日志写入 failures.jsonl 的 `kind="speak_up.triggered"`，方便 doctor 观察。
**验收**：连续 20 轮纯陈述对话后，灵枢至少主动搭话 1 次；`speak_up_prob 0` 后绝不触发。

#### P1-4 Git 分支感知（借鉴：dsh-memory-evolve）

**灵枢现状**：memory.py 的记忆按 `project/session` 分，但没有感知 git 分支——同一个项目切了分支，之前的记忆还是会被召回（可能造成错误上下文）。
**借鉴点**：dsh-memory-evolve 的"git 分支感知"——当当前工作目录是 git 仓库时，自动探测 `git rev-parse --abbrev-ref HEAD`，把分支名作为 memory 的**附加隔离维度**。
**改法**：
1. `scripts/memory.py` 的 `remember()` 自动探测 `--branch`（从 CWD 的 git 仓库），存入 memory 的 `_branch` 字段；
2. `scripts/retriever.py` 的召回按 `project + branch` 过滤，同一分支优先、跨分支降权；
3. 非 git 目录不触发（`_branch=null`，不影响现状）；
4. config `config set git_branch_memory true`（默认 true，可关）。
**验收**：同一项目切到 feature-branch 后，召回的记忆里 main 分支的内容降权 ≥50%；非 git 目录行为不变。

### P2 · 长期

#### P2-1 外部信息摄入（借鉴：dsh-bili-scroll / dsh-fish-soul）

**灵枢现状**：灵枢的记忆来源**只有用户说过的话**（extract / remember / learn）。没有"自己去看 B 站、抖音、RSS"的通道。
**借鉴点**：dsh-bili-scroll 每天替用户刷 B 站、dsh-fish-soul 自己刷抖音——灵枢也可以加一个**定时信息摄入管道**：
1. 插件化：每个信息源一个 `feeder_*.py`（`feeder_bilibili.py` / `feeder_douyin.py` / `feeder_rss.py`），只负责拉取 + 产出 raw text；
2. 去重：拉到的文本先跟 memory 做相似度比对，重复度 ≥0.8 的直接丢弃；
3. 封存：新文本走 `memory.remember()`（kind=episode，importance 由 feeder 的可信度决定），自动打 tag `source=bilibili`；
4. 不做判断：灵枢不"评价"这些信息（那是人格孵化的事），只负责**记住**。
**改法**：
1. 新增 `scripts/feeders/` 目录（先只做 rss——无 API key 依赖，验证架构）；
2. cron 插件新增 `feeder` 任务（每天 08:00 跑，或 config `feeder.schedule`）；
3. doctor 新增第 14 项 `feeder-liveness`（跟 runtime-liveness 同结构）。
**验收**：`feeder_rss.py` 跑一次，memory 新增 ≥1 条带 `source=rss` tag 的记忆；doctor 第 14 项 PASS。

#### P2-2 技能自我进化（借鉴：dsh-memory-evolve）

**灵枢现状**：灵枢的 skill 系统（`skill install / skill scan / skill borrow / skill roots`）是**只读消费**——技能一旦装进来，不会自己演化。
**借鉴点**：dsh-memory-evolve 的 "skill self-evolve"——灵枢每天（或每周）self-review 后，**自动在 memory 里找过去 7 天关于某个 skill 的"踩坑"记忆**，然后**在 skill 的 SKILL.md 末尾追加一段 "灵枢给你的使用小贴士"**（纯模板 + 词表，不调 LLM）。
**改法**：
1. self-review 的输出增加一个分支：扫描 memory 里 `tags contains skill:<name>` 的记忆；
2. 对每个有 ≥2 条相关记忆的 skill，生成一段 150 字以内的贴士（"在 <场景> 下，这个 skill 可能会 <坑>；建议 <做法>"）；
3. 贴士文件：`<skill_dir>/.lingshu-tips.md`（追加模式，保留历史）；
4. doctor 新增第 15 项 `skill-evolve-liveness`。
**验收**：连续两周 self-review 后，至少 1 个 skill 的 `.lingshu-tips.md` 里有内容。

#### P2-3 情绪驱动的记忆权重（借鉴：dsh-feiyu-memory 的"记仇"）

**灵枢现状**：memory 的 `importance` 是 0-1 的主观评分，由用户显式指定或 importance.auto_calibrate 自动校准。没有**情绪维度的权重**——一条让用户"委屈"的记忆和一条"开心"的记忆，在 recall 里权重相同（只看 retention 和 importance）。
**借鉴点**：dsh-feiyu-memory 的"记仇"——当一条 emotion 事件的 `valence < -0.3 且 weight ≥0.1`（负面且有分量）时，对应的 memory 自动提升 importance ×1.2；当 `valence > 0.3 且 weight ≥0.1`（正面且有分量）时，importance ×1.1。
**改法**：
1. `scripts/emotion.py` 的 `_label_with_gate` 或 emotion event 落盘时，顺带调 `memory.touch_by_emotion(memory_id, valence, weight)`；
2. `memory.touch_by_emotion` 对对应 memory 的 importance 做 clamp（0-1），并在 `_touched_by_emotion` 里记下本次权重；
3. recall 时 importance × (1 + `_touched_by_emotion` 的均值 × 0.3)。
**验收**：extract 一段负面情绪对话后，对应 memory 的 importance 自动提升；同 importance 下，负面情绪相关记忆在 recall 结果里排名靠前。

#### P2-4 灵枢自己的宠物 / 成长阶段（借鉴：dsh-pet-evolve）

**灵枢现状**：doctor 13 项体检 + failures 账本 + self-review 都是**数据**，没有被打包成"灵枢自己的成长故事"呈现给用户。
**借鉴点**：dsh-pet-evolve 的 5 阶段——灵枢可以有自己的**体检健康度阶段**：
- 蛋：首次 install，doctor 只跑过 0 次；
- 幼体：doctor 跑过 ≥5 次、failures < 3 条；
- 少年：doctor 跑过 ≥30 次、failures < 10 条、self-review 跑过 ≥5 次；
- 成年：doctor 跑过 ≥100 次、failures < 20 条、self-review 跑过 ≥20 次、至少有 1 次 release-drift FAIL 被修复；
- 传奇：成年基础上 + 覆盖率 ≥45% + 至少有 1 次大版本升级记录在 CHANGELOG 里。
**改法**：
1. panel 首页或 persona 页签顶部加一行"灵枢健康度：少年（doctor 32 次 / failures 7 条）"；
2. 阶段判定逻辑在 `scripts/growth.py` 的 `_compute_stage()`；
3. 可选：阶段变化时 self-review 自动追加一段"灵枢成长日志"。
**验收**：doctor 跑过 ≥30 次后，panel 显示"少年"；跑过 ≥100 次后显示"成年"。

---

## 四、排除项（不建议借鉴）

| 插件 | 不借鉴的原因 |
|---|---|
| **WhaleMaid-Autopilot** | 搜不到公开资料，可能是未发布的内部插件。等它公开后再评估——"拆解目标、卡点求助"其实灵枢的 `plan.py` 已经在做，只是不叫 autopilot |
| **dsh-bili-scroll 的 B 站/抖音信息流**（作为人格孵化通道） | 灵枢是**本地纯标准库**技能，加 B 站/抖音爬取需要登录态、反爬处理、可能违反平台 ToS。**feeder_rss** 可以借鉴（无 API key 依赖），但 B 站/抖音先不动——要做也是**用户自己提供 feeder 插件**（灵枢作为宿主消费，不内置） |
| **dsh-fish-soul 的 QQ / 抖音小号 / 群插话** | 同上，需要外部 API key、反爬、合规问题；灵枢保持纯本地 |
| **dsh-agentic-pet 的桌面宠物形态** | 灵枢已有面板，跨平台一致；桌面宠物（透明 always-on-top）需要平台特定 API、维护成本高，收益不抵 |

---

## 五、优先级总览

| 优先级 | 编号 | 改法 | 影响模块 | 估计工时 |
|---|---|---|---|---|
| P0 | P0-1 内心独白 | panel + emotion.py 加字段 + core.js 加一行 | panel_server.py, emotion.py, core.js | 4h |
| P0 | P0-2 心情时间线 | panel 加 timeline 接口 + core.js 加组件 | panel_server.py, core.js | 6h |
| P0 | P0-3 测试文件入库 | 删 .gitignore 排除项 + 台账改口径 | .gitignore, 进展与待办.md | 0.5h |
| P1 | P1-1 索引法记忆 | memory.py 加 index + retriever.py 改两步召回 | memory.py, retriever.py, cli.py | 16h（需真机巡检） |
| P1 | P1-2 体检历史 | cli.py doctor 追加 history + panel 加新页签 | cli.py, panel_server.py, core.js, growth.py | 12h |
| P1 | P1-3 主动搭话 | core.py 主循环加 maybe_speak_up + 模板库 | core.py, 新建 templates/speak_up.py | 8h |
| P1 | P1-4 Git 分支感知 | memory.py 自动探测 git + retriever 按 branch 过滤 | memory.py, retriever.py | 6h |
| P2 | P2-1 外部信息摄入 | 新建 scripts/feeders/ + cron 新增 feeder 任务 | 新建 3 文件, cron.py, cli.py | 20h |
| P2 | P2-2 技能自我进化 | self-review 扫 memory + 追加 .lingshu-tips.md | cli.py, skill.py | 8h |
| P2 | P2-3 情绪驱动权重 | emotion.py 调 memory + memory 加 _touched_by_emotion | emotion.py, memory.py, retriever.py | 10h |
| P2 | P2-4 成长阶段 | growth.py 加 _compute_stage + panel 首页展示 | growth.py, core.js | 4h |

---

## 六、备注

1. 这条线是**新功能线**，跟现有 v5.12 主分支并行。建议开一个 feature 分支 `feature/dsh-inspired`，每条 P0/P1 一个 commit，P2 每条一个 PR。
2. 所有新增功能默认**有 config 开关、默认关闭或半关**，跑完真机巡检再开 true——避免"装了就默认变了"。
3. 测试文件入库（P0-3）是前提——不然这条线的改动没有回归保障。先做 P0-3，再做 P0-1/P0-2。
4. 灵枢的核心不变：**纯 Python 标准库、零外部依赖、跨会话长期记忆的可解释性**。dsh 插件里需要外部 API key 的部分（B 站/抖音/QQ）一律不内置，只留宿主接口让用户自己接。
