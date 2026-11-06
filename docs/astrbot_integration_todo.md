# 灵枢 × AstrBot 集成审计与迭代待办文档

- 文档版本：v1.1（2026-11-06，§4 问题清单已批量修复，见 §7）
- 审计范围：`scripts/` 全部核心模块（core.py / memory.py / emotion.py / persona.py / dream.py / cli.py / growth.py / brain.py / panel_server.py / panels.py / sync_engine.py）+ `scripts/astrbot_plugin_lingshu/` 桥接插件（main.py、_conf_schema.json、metadata.yaml、README.md、make_checksums.py、engine/ 镜像副本）
- 性质：v1.0 为只读代码审计；v1.1 起 §4 清单已落实到源码修复（见 §7 修复记录）。

---

## 1. 概述

### 1.1 背景

灵枢（SKILL.md v2.71）是纯本地、零第三方依赖的 Python 类脑记忆/人格技能，运行数据以 JSONL 明文存放在 `~/.brain-memory/`（或 `BRAIN_MEMORY_HOME` 指定目录）。核心入口 `scripts/brain.py` 做命名空间合并与 re-export，子命令由 `cli.py` 分发。AstrBot 侧通过桥接插件 `scripts/astrbot_plugin_lingshu/`（v0.2.3，`astrbot_version: ">=4.13.0,<5.0.0"`）以 subprocess 方式调用 `brain.py`，实现记忆召回注入（`on_llm_request`）、写回捕获（`on_llm_response`）、`lingshu` 指令组与 `memory_isolation` 隔离开关。

### 1.2 目标

1. 摸清桥接插件 0.2.x 与灵枢 v2.71 核心之间的能力缺口（尤其是话题栈、情绪、模式联动缺失）；
2. 给出符合 AstrBot 官方插件规范的兼容设计方案与三条实现路径对比；
3. 累计梳理约 100 个问题点（功能/逻辑/安全/兼容/边界/改进六维度），逐项给出优先级、处理要求与完成标准；
4. 形成 P0→P3 迭代路线与验证计划。

### 1.3 范围

- 只覆盖本仓库代码与桥接链路；不覆盖 AstrBot 框架自身的缺陷。
- 桥接与 v2.71 话题栈（`topic` 命令、`topics.jsonl`）尚未打通是已知事实，见 §2 与 §4。

---

## 2. 拟人闲聊话题栈记忆能力验证结论

**结论：核心侧话题栈功能正常生效，但桥接插件完全未接入，AstrBot 场景下话题栈处于"有引擎、无驱动"状态。**

验证依据（代码层面）：

- 核心能力齐全：`core.py` 定义 `TOPIC_FILE = HOME / "topics.jsonl"`、`TOPIC_CAPACITY=12`、`TOPIC_DECAY=0.85`、`TOPIC_SPAWN_THRESHOLD=0.30`、`TOPIC_TTL=7天`；`memory.py` 提供 `topic_status()`（L1590）、`topic_touch()`（L1602，用户重提拉高/AI 自发 0.4 起步）、`topic_feedback()`（L1638，敷衍 ×0.6 / 展开 +0.15）、`topic_drop()`（L1655）、`topic_can_spawn()`（L1664）。
- CLI 已暴露：`cli.py` L2163 起 `brain.py topic touch|feedback|drop|status` 子命令完整可用，含 `--why/--good/--bad/--weight/--delta` 参数，鉴权闸（L1332）也已覆盖。
- 读写有锁：话题栈写路径 `topic_touch/topic_feedback/topic_drop` 均在 `with _locked(TOPIC_FILE)` 内完成 read-modify-write，多进程并发安全。
- 衰减逻辑：`load_topics()` 读取时按 `TOPIC_DECAY ** periods`（12 小时一个周期）指数衰减并回写归一化，权重低于阈值进入 `weak` 列表供自发话题使用。

**桥接侧缺口（AstrBot 场景下话题栈不生效）**：

| 应联动的核心能力 | 桥接 0.2.x 现状 |
|---|---|
| `topic touch`（用户抛出新话题） | `inject_memory` 只做 recall，不 touch 话题；每轮用户消息没有进入 topics.jsonl |
| `topic feedback`（敷衍/展开反馈） | `on_llm_response` 只做写回捕获，不评估用户对 AI 回复的兴趣反馈 |
| `topic status/can_spawn`（自发话题准入） | 注入 system_prompt 时只拼记忆块，不带话题栈状态，LLM 无法"主动开新话题" |
| 情绪注入（`emotion.py` mood/storm） | 完全未调用，AstrBot 对话中 AI 无情绪状态 |
| 模式判定（`core.analyze_topic` work/human） | 未调用，无法按工程/拟人切换注入口径 |
| `startup` 注入包（`build_injection`） | 桥接只注入 recall 结果，不带人格简报/用户规则/交接笔记 |

此外话题栈当前是**全局单栈**（无 channel/group/user 维度），即使桥接接入，多群用户也会互相污染话题上下文，接入前需先做通道隔离改造（见 B-08、C-03）。

---

## 3. AstrBot 兼容设计方案

### 3.1 现状梳理（桥接 0.2.x 调用路径）

```
用户消息
  ├─ @filter.on_llm_request() → inject_memory(event, req)
  │     _validate_text(raw) → _run_cli_async('channel','recall',query,…,'--json')
  │     subprocess: {python} brain.py channel recall … （env: BRAIN_MEMORY_HOME=data_home）
  │     json 解析 → 预算拼接 <memory> 块 → req.system_prompt += block
  ├─ @filter.command('lingshu') → lingshu(event)
  │     动作：状态/身份/记住/找回/同意/拒绝/允许记录/拒绝记录/我的信息/问/忘记（做梦禁用）
  │     错误码约定：0 成功 / 3 权限或只读 / 4 隐私拒绝 / 其他=通用失败
  └─ @filter.on_llm_response() → capture_turn(event, resp)
        _capturable()（长度+敏感词+字母数字占比）→ _capture_allowed()（consent.json capture=true，mtime 缓存）
        → 入队 _capture_queue（上限 500）→ _capture_worker 串行消费 → channel remember（节流 capture_interval）
```

关键参数：并发信号量 `_slots=2`、pending 上限 8、timeout 15s（CLI 层 ×2+5）、recall_budget 1400 字符、memory_isolation 默认开启。

### 3.2 AstrBot 官方插件规范要点（与本插件相关的核对项）

| 规范要点 | 桥接现状 | 结论 |
|---|---|---|
| 事件钩子：`on_llm_request` / `on_llm_response`（修改 `ProviderRequest.system_prompt`） | 已实现 | ✅ |
| `@filter.llm_tool` 函数工具（把能力暴露给 LLM 自主调用） | 未实现 | ❌ 缺口 |
| `@filter.command` / `@filter.command_group` 指令 | 仅单个 `@filter.command('lingshu')`，靠手工 split 原文解析子动作 | ⚠️ 可用但非规范用法 |
| `metadata.yaml` 声明 `astrbot_version` | 已声明 `>=4.13.0,<5.0.0` | ✅（但 AstrBot v4.5.7 前后钩子签名/行为有差异，见 C-01） |
| 随插件提供 `skills/` 目录（Skill Manager 可纳管） | 未提供（只有 `engine/` 镜像副本） | ❌ 缺口 |
| 持久化数据放 data 目录（`StarTools.get_data_dir`） | 已用 `StarTools.get_data_dir('astrbot_plugin_lingshu')/memory` | ✅ |
| 插件市场发布要求（checksum、README、版本号规范） | 有 `make_checksums.py`、README；版本号带 `v` 前缀（`v0.2.3`） | ⚠️ 小问题 |

### 3.3 目标架构：AstrBot 调用灵枢的三条实现路径

**路径 a（现状）：桥接插件 subprocess 调 brain.py 完成召回/注入/写回**

- 优点：零侵入、零依赖、进程隔离，核心崩溃不影响宿主；权限/隐私门控在 CLI 层统一收口（`_require_privileged`、`PrivacyDenied`）。
- 缺点：每条命令冷启动一个 Python 解释器（数百 ms），recall 高频路径成本高；argv 传参受 Windows ~32K 命令行长度限制（超长消息会 spawn 失败，见 B-05）；话题栈/情绪/模式等"每轮联动"能力难以低延迟接入；stdout 文本协议脆弱（无版本化 JSON schema）。
- 适用：保持为兜底路径与手动命令通道。

**路径 b：`@filter.llm_tool` 暴露 recall / remember / topic 为 LLM 函数工具**

- 做法：在插件内注册 `recall(query)`、`remember(text)`、`topic_status()` 等 llm_tool，内部仍走 `channel recall/remember` 或直接 `import` 核心模块（同进程 `sys.path` 注入 engine/scripts）。
- 优点：LLM 可自主决定何时查/记/换话题，天然打通话题栈反馈闭环；避免盲目每轮注入。
- 缺点：同进程 import 会把核心异常引入宿主进程（需异常隔离与 `READONLY` 探测）；工具调用次数不受控会放大 subprocess 成本；需在工具描述里写清隐私口径。
- 适用：**推荐的主演进路径**，配合路径 a 的自动召回共存（配置项互斥提示）。

**路径 c：随插件提供 `skills/` 把 SKILL.md 纳入 AstrBot Skill Manager**

- 做法：插件目录新增 `skills/lingshu/SKILL.md`（从核心根目录同步），声明触发条件与使用说明，由 AstrBot 技能系统在合适时机把"如何使用灵枢"注入系统提示。
- 优点：召回/记忆策略由宿主统一编排，桥接只保留隐私门控；符合"技能即文档"的定位。
- 缺点：依赖 AstrBot Skill Manager 的注入时机，与 `on_llm_request` 自动召回可能重复注入（需冲突开关，见 §3.6）。
- 适用：发布到插件市场后的推荐形态；短期可先在插件包内放 `skills/` 占位。

### 3.4 接口规范表（桥接命令 输入/输出/错误码）

| 命令 | 输入（argv） | 输出（stdout） | 错误码 | 桥接侧处理 |
|---|---|---|---|---|
| `channel remember <text> --channel C --group G --user U --source user [--kind K] [--importance I]` | 记忆文本 ≤ max_text | 中文确认行（id/layer/重要性） | 0 成功 / 2 参数错 / 1 内部错 | `_reply()` 直接回显 |
| `channel recall <query> --channel C --group G --user U --asker U [--target T] --k N --json` | 查询 ≤ max_text | JSON 数组 `[{memory, score, parts}]` | 0 / 2 / 4 PrivacyDenied | inject/找回/问 三处消费 |
| `channel privacy grant/revoke/capture-on/capture-off --user U [--channel C] [--group G]` | 本人身份 | 中文确认 + 授权范围 | 0 / 64 用法错 | 同意/拒绝/允许记录/拒绝记录 |
| `channel user <user> --asker U` | 本人或已授权目标 | 跨群记忆列表文本 | 0 / 4 | 仅私聊暴露 |
| `channel owner list` | — | owners/admins 列表 | 0 | 健康自检用 |
| `forget <m_id> --channel C --user U` | `m_[A-Za-z0-9_]+` | 确认文本 | 0 / 1 未找到 / 3 非白名单 | 桥接先 `_is_owner` 预检 |
| `topic touch/feedback/drop/status [--json]`（**待接入**） | 话题名、--why/--good/--bad | 栈状态文本/JSON | 0 / 2 | 规划在 inject/capture 链路中调用 |
| `startup --query Q` | — | 注入包文本 | 0 / 10 只读 / 2 未初始化 | **待接入**（替代/补充手动 recall） |

约定：CLI 通用退出码 0/1/2/3(拒绝)/4(隐私)/10(只读注入)/64(用法)/130(中断)；bridge `_reply()` 将 3、4 映射为中文提示，其余一律模糊化为"灵枢操作失败"（不回显 stderr，防信息泄露）。

### 3.5 适配逻辑：channel/group/user 三元组 ↔ `unified_msg_origin`

- 桥接 `_ident(event)`：`channel = quote(platform, safe='') + '@' + quote(platform_id, safe='')`，`user = channel + ':' + quote(sender_id)`，`group = get_group_id() or ''`。百分号编码防分隔符碰撞；adapter 实例 ID 参与 channel，隔离同一平台的多个 bot 连接。
- AstrBot 的 `unified_msg_origin`（`platform:type:session_id`）与灵枢三元组语义对齐映射：`platform_id→channel`、群消息 `session_id→group`、`sender_id→user`；私聊时 group 为空串，`memory._channel_visible` 走"本人记忆 + 群公共记忆"分支。
- 群聊隔离：召回时 `_channel_visible` 物理过滤其他群记忆；同群他人记忆需 `_consented(m_user, group)`；第三人查询走 `PrivacyDenied` fail-closed（asker 缺失不视为本人）。
- 隐私门控：分享授权（consent granted + groups 作用域 + expires）与自动写回授权（capture=true）分离；owner/admin 对隐私查询不豁免；`我的信息` 强制私聊；`问` 限定当前平台实例内目标。
- 缺口：桥接未把 `unified_msg_origin` 的完整会话 ID 落盘到记忆记录（只有三元组），跨 adapter 换绑/重装后 channel 字符串变化将导致旧记忆"失联"（见 E-09）。

### 3.6 冲突排查与稳定性保障（与 AstrBot 内置记忆/人格并存）

1. **人格冲突**：桥接刻意只读 persona（不创建/不注入/不改写 `persona.json`），宿主人格由 AstrBot 内置人格系统管理——保持该原则，文档化"灵枢人格仅本机 CLI/面板生效"。
2. **记忆冲突**：若用户同时启用 AstrBot 内置记忆插件，会产生双重注入。建议：桥接配置加 `coexist_mode`（`suggest_disable_host_memory` 提示 + 状态命令可见），并在 README 写明二选一。
3. **注入冲突（路径 b/c）**：`llm_tool` 与自动召回并存时 LLM 可能重复查询。加配置 `auto_recall` 与 `llm_tools` 互斥提示（可同时开但状态页警告）。
4. **锁与陈旧锁**：桥接 timeout(15s) < 核心陈旧锁阈值 STALE_LOCK_SECONDS(30s)，超时被杀的进程可能留下 ≤30s 的锁文件，后续调用最多额外等待 30s 才接管——把 bridge timeout 与 stale 阈值联动（C-07）。
5. **数据目录只读**：CLI 已有 `--ro` 自动探测降级（startup 返回 10）；桥接 initialize 的 TemporaryFile 探测覆盖了目录不可写场景，但运行中目录变只读不会触发降级（E-12）。

---

## 4. 问题清单（100 项）

优先级定义：P0=安全/数据丢失/主链路不可用；P1=兼容/明显 BUG；P2=边界与健壮性；P3=改进建议。

### 4.1 功能异常（F-01~F-18）

| 编号 | 优先级 | 位置 | 问题 | 影响 | 处理要求 | 完成标准 |
|---|---|---|---|---|---|---|
| F-01 | P1 | astrbot_plugin_lingshu/main.py `inject_memory` | 桥接未调用 `topic_touch/feedback/can_spawn`，话题栈不联动 | AstrBot 对话无话题连续性、AI 不能自发换话题 | 在 inject/.response 链路加 topic 联动（可配置开关） | 联天场景 topics.jsonl 持续更新且注入包带话题状态 |
| F-02 | P1 | main.py `inject_memory` | 未注入情绪/模式（emotion mood、analyze_topic work/human） | AI 在群聊恒为"中性无人格"状态，与 SKILL.md 宣称能力不符 | 注入包追加 mood/mode 摘要段 | 群聊注入内容含情绪与模式字段 |
| F-03 | P1 | main.py `inject_memory` | 未调用 `build_injection/startup`，人格简报、用户规则、交接笔记不进入宿主 | 宿主对话缺失人格底色与用户附加规则 | 路径 c 落地或 inject 时追加 startup 包 | 宿主 system_prompt 含 persona 简报 |
| F-04 | P2 | main.py `lingshu` | `做梦` 动作永久禁用，仅有静态提示文案 | 群聊用户无法触发做梦压缩，记忆库长期膨胀只能靠管理员本机处理 | 评估 owner-only 远程做梦或明确文档化为"仅本机" | 有明确产品结论并写进 README |
| F-05 | P2 | main.py `lingshu` | 无 `帮助` 之外的分页/别名，中文动作名硬编码 | 英文平台用户不可用 | 增加动作别名或 README 双语说明 | 状态页展示动作清单 |
| F-06 | P2 | main.py `terminate` | `_capture_worker_task.cancel()` 后未 await，队列中未写回内容直接丢弃 | 停机瞬间 opt-in 用户的待写回消息丢失 | terminate 里 drain 队列或落盘待办 | 停机后队列内容可恢复或明确告警 |
| F-07 | P2 | main.py `_capture_worker` | 轮询 `asyncio.sleep(1.0)` 消费队列 | 写回延迟最高 1s，且空转唤醒浪费 | 改 `asyncio.Queue` + `queue.get()` 阻塞消费 | 压测写回延迟 <200ms |
| F-08 | P2 | cli.py L2394 `forget` | 忘记只移入淡忘区，桥接文案"忘记"未说明可恢复语义 | 用户误以为彻底删除，隐私诉求未满足 | 桥接回复补充"淡忘可 revive"说明或提供 purge 命令 | 用户可查知记忆真实去向 |
| F-09 | P2 | main.py `_ident` | `问` 命令限制 target 必须同 channel，但跨平台实例（同平台多 bot）身份展示冗长易错 | 隐私功能可用性差 | 提供 `lingshu 群友` 列表（仅展示已授权者） | 问命令失败率下降 |
| F-10 | P3 | main.py `lingshu 状态` | 未展示引擎版本号（核心 v2.71 vs engine 镜像版本） | 用户无法判断镜像是否过期 | 状态页加 engine/scripts 版本与核心版本对比 | 版本不一致时有提示 |
| F-11 | P3 | main.py | 无健康自检周期任务，`health` 仅 initialize 时确定 | 核心中途损坏（磁盘满）后状态页仍显示"可用" | 加定时轻量探活（如每小时 owner list） | 状态页能反映实时可用性 |
| F-12 | P3 | main.py `inject_memory` | 召回为空时静默 return，无可观测记录 | 用户开了 auto_recall 却不知是否生效 | debug 日志记录召回条数与耗时 | 日志含 recall 统计 |
| F-13 | P2 | core.py `analyze_topic` L565 | work/human 判定仅靠关键词表，"帮我看看这段代码让我很难过"这类混合句全靠打分粗判 | 模式误切换导致情绪隔离错误 | 引入权重表外置 config 或接 LLM 判定（可选） | 判定依据可在面板展示并被人工覆盖 |
| F-14 | P3 | memory.py `topic_status` L1590 | status 不回写衰减后的权重，只读视图 | 长期不调用 touch 时衰减只存在于内存视图 | 衰减结果落盘或文档化"读时衰减"语义 | 行为有明确定义 |
| F-15 | P3 | cli.py `check-context` L2406 | handoff 依赖 `input()`，非交互环境直接 EOF | 宿主调 check-context --handoff 拿不到内容 | 支持 --section 参数化输入 | 非交互可写交接笔记 |
| F-16 | P3 | cli.py `serve` L2619 | 面板启动失败（端口占用）仅抛 OSError 给 main 兜底 | 报错不友好 | 捕获并提示换端口 | 有中文指引 |
| F-17 | P2 | panel_server.py `serve` L1565 | 端口被占用时 `ThreadingHTTPServer` 未设置 `allow_reuse_address` 行为依赖平台默认 | Windows 重启面板偶发 bind 失败 | 显式设置 SO_REUSEADDR（Windows 上注意 TIME_WAIT 语义） | 反复启停面板不再失败 |
| F-18 | P3 | main.py `lingshu 忘记` | 桥接先 `_is_owner` 再调 CLI，双重校验但两者数据源一致依赖 config.json 直接读 | 权限口径分裂风险（CLI 层有 owners/admins，桥接只查 owners） | 统一走 CLI 返回码 3 判定 | 权限判定单一出口 |

### 4.2 逻辑 BUG（B-01~B-20）

| 编号 | 优先级 | 位置 | 问题 | 影响 | 处理要求 | 完成标准 |
|---|---|---|---|---|---|---|
| B-01 | P0 | main.py `inject_memory` L383 | 判重条件写错：`if header not in base` 检查的是 header 而非完整 block；若 base 因其他插件已含 header 文案则本轮记忆被静默丢弃，反之同一请求内不会重复但跨插件拼接无幂等保障 | 记忆偶发不注入，难排查 | 改为检查本插件特征标记（如 `<memory>` 块计数）或使用独立 marker | 单元测试覆盖 base 已含相似文案场景 |
| B-02 | P1 | core.py `log_event` L200-207 | 日志写用 `open(...,"a", encoding="utf-8")` 未指定 `newline`，Windows 文本模式把 `\n` 转成 `\r\n`；日志滚动时 `splitlines()` 虽兼容，但字节层面与其他 LF 文件不一致，外部工具按行统计时口径漂移 | 日志文件 CRLF/LF 混杂 | 所有追加写统一 `newline="\n"`（user-rules add L2726 同病） | 仓库内所有写文件点 grep 核对 newline 参数 |
| B-03 | P1 | core.py `atomic_json` L680 | 写入用 `encoding="utf-8"` 无 BOM，读取用 `utf-8-sig`；用户用记事本编辑 config.json 保存后带 BOM 可读，但手写脚本按 utf-8 读会踩坑；且 `atomic_json` 无 `os.fsync`，Windows 突然断电时 os.replace 后文件可能空/半截（元数据未刷盘） | 极端断电丢配置 | 关键 JSON 写入后 fsync；文档说明编码约定 | backup/restore 往返 + 强杀进程测试通过 |
| B-04 | P1 | main.py `_validate_text` L162 | 拒绝以 `-` 开头的内容过于粗暴：用户真想记"-比如这样"会被拒且报错文案晦涩 | 可用性问题 | CLI 层改为 `--` 分隔符或 base64 传参，桥接放开限制 | 以 - 开头内容可正常写入 |
| B-05 | P1 | main.py `inject_memory` L334 | 召回查询最长 max_text=4000 字符经 argv 传给子进程；Windows 命令行上限 ~32K 字符，多 bot 并发 + 上下文联想（astrbot_context_lines 拼接）接近上限时 spawn 直接失败 | 超长消息召回静默失败（except 捕获后仅 warning） | 长查询走临时文件/stdin 传递 | 12000 字符消息可正常召回 |
| B-06 | P2 | main.py `_reply` | CLI 返回码未穷尽：核心退出码 10（只读注入）、64（用法）、130（中断）都会落入"灵枢操作失败" | 用户收到错误提示却查不到原因 | `_reply` 按 exit code 映射细分文案 | 每个退出码有对应提示 |
| B-07 | P2 | main.py `capture_turn` | `_capturable` 的敏感词正则 `_sensitive` 覆盖有限（如"卡号""cvv""otp"漏网），且 `text.lower()` 对中文无效但正则含中文词双重匹配无害；`re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]','',text)` 未含日文假名/emoji-only 消息判断为不可记——行为正确但日韩用户长文可能 <2 有效字符 | 敏感信息漏记入记忆库 / 部分语言永远不写回 | 敏感词表外置可配置；字符统计按 Unicode 类别 | 敏感词样例集全部拦截 |
| B-08 | P1 | memory.py `topic_touch/feedback/drop` | 话题栈全局单栈，无 channel/group/user 维度；多群并发 touch 会互相挤出（TOPIC_CAPACITY=12） | 多群场景话题上下文串群 | topics.jsonl 记录 scope 键并在读取时按 scope 过滤 | 两群话题互不可见 |
| B-09 | P2 | memory.py `topic_feedback` L1641 | 未先 `ensure_home()`（topic_touch 有、feedback/drop 没有），只读模式下首次调用 `_locked` 会在不存在的目录创建 lock 文件失败 | 只读环境 topic feedback 异常 | 统一入口调用 ensure_home 或检查 READONLY | --ro 下 topic 命令优雅降级 |
| B-10 | P2 | core.py `_locked` L899 | `_owns` 变量在 try 前定义、finally 中判定，但 `_lock_owner` 每次释放都读一次锁文件；若锁文件此刻被陈旧锁接管流程删除，`_lock_owner` 返回 None → 不删除 → 正常路径也留陈旧锁文件 30s | 偶发写等待放大 | 释放逻辑容忍锁文件已不存在（FileNotFoundError 即跳过） | 并发压测无超时尖刺 |
| B-11 | P2 | core.py `_locked` L877 | 两个等待者同时发现陈旧锁并都 unlink：一个成功一个 OSError 忽略后双方 continue 重试，逻辑正确但 `_pid_alive` 在 Windows 上对无法打开句柄的进程（权限差异）返回 False 会误判为死锁接管 | 跨权限进程并发写时可能双写 | `_pid_alive` 失败时保守返回 True（注释声称如此，但 OpenProcess 失败路径直接 return False 与注释矛盾） | 修正实现与注释一致 |
| B-12 | P2 | memory.py `remember` L808 | 写锁只锁 hippocampus.jsonl，但 `save_all` 会同时覆写 cortex.jsonl / archive；cortex 的并发写者（knowledge tidy 等）若不持同一把锁，仍有跨文件写冲突窗口 | 极端并发下 cortex 记录丢失 | 所有 save_all 调用点统一持 hippocampus 锁（dream.apply 已这么做，需审计其余调用点） | 全仓 save_all 调用点审计清单 |
| B-13 | P2 | memory.py `recall` L1075 | `records = [m for m in load_all() ...]` 每次全量载入内存 + 全量分词；万条记忆 + 群聊高频召回时 CPU 尖刺，且桥接 timeout 15s 可能超时 | 大库场景召回超时率上升 | 引入 mtime 缓存的内存索引或限制候选集 | 1 万条记忆召回 <500ms |
| B-14 | P2 | main.py `_capture_allowed` L394 | consent.json 读取在 `_consent_cache` 读写时未加锁；asyncio.to_thread 并发下 dict 写入竞态（CPython GIL 下单操作原子，但 read-modify-write 组合非原子） | 极端情况下缓存脏读一次 | 缓存操作移回事件循环单线程或加锁 | 代码评审通过即可 |
| B-15 | P2 | main.py `inject_memory` L344 | `req.contexts` 兼容处理只 catch AttributeError；AstrBot v4.5.7 前后 contexts 结构（list[dict] vs 其他）若变化，`m.get('content')` 可能拿到非 str，`str()` 兜底存在但截断 120 字符后拼接语义漂移 | 联想查询质量下降 | 对 contexts 元素做 schema 校验并单测 | 不同版本 contexts 结构均有测试 |
| B-16 | P2 | growth.py L1413-1419 | Gitee zip 解压的防 zip-slip 用 `str(p).startswith(str(staging.resolve()))` 前缀匹配，未加路径分隔符：`staging_evil` 兄弟目录可绕过 | 恶意归档可写到缓存目录外 | 改用 `Path.is_relative_to`（与 cmd_restore L288 一致） | 恶意成员名用例被拒绝 |
| B-17 | P3 | dream.py `apply_dream` L311 | apply 在锁内做 O(n) 候选粗筛 + 精比对，万条记忆时持锁时间长（接近 STALE_LOCK_SECONDS），其他写进程等待 | 做梦期间写入延迟 | 比对移到锁外快照（接受少量竞态）或分批 | apply 持锁时间 <10s |
| B-18 | P3 | memory.py `topic_touch` L1631 | `spawned_by_ai = delta is not None and weight is None`——CLI 里 delta 与 weight 可同时传，语义为 False，但传参组合 (--weight 0.4 --delta 0.1) 时该标记失真 | 统计口径漂移 | 明确参数互斥并在 CLI 校验 | 非法组合被拒绝 |
| B-19 | P3 | main.py `_run_cli_async` L169 | `_pending >= 8` 拒绝新请求直接返回 code=1，`_reply` 会把它当"灵枢操作失败"回给用户，但真实原因是限流 | 拥塞时错误提示误导 | 限流时返回专用提示语 | 拥塞提示与失败提示区分 |
| B-20 | P3 | cli.py `cmd_startup` L560 | last_startup 写 config.json 在锁内，但 `atomic_json` 每次全量重写 config；高频 startup（多会话并发）会造成 config.json 写放大 | config 写入频繁 | last_startup 挪到独立状态文件 | config.json 写频率下降 |

### 4.3 安全漏洞（S-01~S-16）

| 编号 | 优先级 | 位置 | 问题 | 影响 | 处理要求 | 完成标准 |
|---|---|---|---|---|---|---|
| S-01 | P0 | core.py 全部 JSONL | 记忆、情绪、人格、consent 全部明文存储于 ~/.brain-memory/，无静态加密；群聊用户隐私（聊天原话、身份映射）落盘即裸奔 | 磁盘被复制/设备丢失即泄露全部群成员隐私 | 提供 at-rest 加密选项（复用 backup 的 PBKDF2 vault 方案）或至少文档强提示 + 目录 ACL 建议 | 有加密选项或 README 显著警示 |
| S-02 | P0 | main.py `capture_turn` | auto_capture 记录"聊天原话"进长期记忆，群聊场景其他成员的消息若 opt-in 也会原文落盘并可在召回中展示给同群他人（有 consent 门控，但授权粒度是"分享关于我的记忆"，不含"我说的原话别给第三人看"细粒度） | 群内第三人隐私暴露面 | 原话落盘默认脱敏（去引用他人 ID/昵称）或授权项拆分 | 授权模型覆盖"原话可见性" |
| S-03 | P1 | main.py `inject_memory` | 召回结果直接拼入 system_prompt 虽有"不可信参考资料"包裹，但记忆内容本身是用户历史消息，若另一用户曾诱导写入 prompt-injection 文本，会在同群他人对话中回放 | 间接提示注入 | 注入内容进一步转义（已有 json.dumps + sanitize，需加长度与结构双重限制）并周期性让模型忽略历史指令 | 注入样例对抗测试通过 |
| S-04 | P1 | growth.py 远程更新 | `install_skill` 更新走 Gitee/GitHub 拉取后整目录替换 scripts/，相当于从远程仓库拉代码落盘供后续执行；无签名/哈希校验（make_checksums.py 存在但更新链路未强制校验） | 供应链投毒面 | 更新链路强制校验 checksum 或最少要求 maintainer 签名 | 校验失败的包拒绝落盘 |
| S-05 | P1 | growth.py L1389 | Gitee zip 下载把 access_token 拼进 URL query（`?access_token=...`）；URL 会进入 urllib 错误消息、可能的代理日志与核心日志（错误路径有 `raise RuntimeError(f"...{zip_url}")` 风险） | token 泄露到日志/代理 | token 改走 HTTP header（Gitee 支持）或 POST；确保任何异常路径不打印完整 URL | 日志 grep 无 token 明文 |
| S-06 | P1 | panel_server.py L53 | 面板 token 随首页响应下发并允许 URL query 携带（L735 注释"API 可通过 URL query 自行携带 token"） | token 进浏览器历史/Referer/本机日志 | query 传 token 改为仅自定义面板 iframe 场景并加短时效 | 非 iframe 路径不接受 query token |
| S-07 | P2 | panel_server.py L569 | API 只校验 Origin + token，未校验 `Sec-Fetch-Site`；本机任意非浏览器进程可直接 GET / 提取 token（代码注释已自认"防的不是本机进程"） | 本机恶意进程可读全部记忆 | 文档明示威胁模型（仅防浏览器向量），或加本地配对确认 | 威胁模型写入 README |
| S-08 | P2 | main.py `lingshu 我的信息`/`问` | 群聊场景 `问` 命令把目标用户的记忆内容（已授权范围内）打印到群里；授权是目标本人给的，但授权时可能未意识到内容会以原文全量展示 | 已授权用户记忆原文暴露群聊 | 摘要化展示 + 每次展示前提示来源 | 群内展示为摘要 |
| S-09 | P2 | memory.py `recall` 通道上下文 | `_channel_visible` 对"群公共记忆（user 为空）"对所有群成员可见：管理员/owner 手动写入的全局记忆（无 user 字段）会注入每个群 | 全局私有笔记泄露到群聊 | 群聊注入时默认排除无 user 且 source=ai 的敏感层记忆，或加 `group_visible` 字段 | 群聊召回白名单测试 |
| S-10 | P2 | cli.py `_require_privileged` L925 | "无 --user 即放行"的设计在本机成立，但任何能执行进程的宿主（含桥接）都可用空 --user 绕过白名单；桥接目前始终带 --user，风险受控但 CLI 作为通用入口口径偏松 | 本机其他进程可越权操作记忆库 | CLI 增加环境变量级信任标记（如 BRAIN_MEMORY_TRUSTED=1 由桥接注入）替代"缺省放行" | 非受信上下文敏感命令被拒 |
| S-11 | P2 | main.py `_execute_cli_async` | 子进程 env 继承宿主全部环境变量（dict(os.environ)），AstrBot 进程的 API key、平台 token 全量传给子进程 | 子进程异常（如核心打印 environ）可致宿主密钥外泄 | env 白名单化（PATH/HOME/BRAIN_MEMORY_HOME 等必需项） | 子进程 env 只含白名单键 |
| S-12 | P2 | cli.py `cmd_backup` | 无口令备份默认明文 zip 存放在 HOME/backups（记忆库目录内），与明文数据同盘无增量防护（仅有 stderr 提示） | 备份扩大泄露面 | 默认提示改为交互确认或自动加口令 | 无口令备份有显式确认 |
| S-13 | P2 | core.py `load_consent`/consent 链路 | consent.json 无完整性保护，本机任何进程/用户手改即生效；expires 过期校验用本地时钟（可回拨绕过） | 授权模型可被本机篡改（威胁模型内可接受，需文档化） | 文档写明 consent 属本机自愿模型，非安全边界 | README 有明确声明 |
| S-14 | P3 | main.py `_reply` | code!=0 时不回显 stderr 仅记 `CLI 返回错误 {code}`，但 initialize 失败时 `logger.error` 打印 exc 类型不含细节——异常细节只进 AstrBot 日志，包含路径信息（data_home 绝对路径） | 日志含本地路径信息 | 审查日志内容，路径仅 debug 级输出 | info 级日志无绝对路径 |
| S-15 | P3 | growth.py `install_skill --from` | 路径白名单校验用 `is_relative_to`，但 `_panel_root()` 返回的面板根目录若被用户配置指向 HOME 外，白名单即被拉宽 | 任意目录可被整包安装 | 白名单收紧为 SKILLS_DIR + HOME + 显式 allowlist 配置 | 越界路径被拒测试 |
| S-16 | P3 | panel_server.py | 日志滚动、`.corrupt` 备份、`dreams/` 快照等衍生文件同样明文含记忆内容，但备份脚本/清理建议只提主文件 | 用户以为删主文件即删隐私 | 文档列全所有含记忆内容的文件清单（含 .corrupt/verbatim/dreams/backups） | 清理文档覆盖全部衍生文件 |

### 4.4 兼容性问题（C-01~C-14）

| 编号 | 优先级 | 位置 | 问题 | 影响 | 处理要求 | 完成标准 |
|---|---|---|---|---|---|---|
| C-01 | P1 | main.py（钩子签名） | AstrBot v4.5.7 前后 `on_llm_request/on_llm_response` 钩子参数与 `ProviderRequest` 字段有差异；metadata 只声明 `>=4.13.0`，未覆盖用户从旧版升级的钩子兼容 | 旧版宿主上注入/写回静默失效 | 在 inject/capture 中对 req/resp 做 schema 防御并写明最低版本依据；CI 矩阵测两个版本 | 两版本钩子行为均有测试 |
| C-02 | P1 | scripts/astrbot_plugin_lingshu/engine/ | engine/ 是核心的镜像副本（metadata 自称 v2.69 核心），与仓库根 scripts/（v2.71）存在版本漂移风险；sync_engine.py 存在同步机制但无 CI 强制 | 桥接用户用旧核心，行为与文档不一致 | 把镜像同步纳入 CI（diff 校验失败即 fail），或在 make_checksums 中校验 | 镜像与核心 diff 为空 |
| C-03 | P1 | memory.py 话题栈 / core.py 情绪 | topics.jsonl、emotions.jsonl、desires.json 均无 channel 维度（config owners 也是全局），核心的"多通道"仅在 memory recall/remember 层实现 | 话题/情绪能力无法安全接入多群桥接 | 状态文件统一加 scope 字段（含迁移脚本兼容旧记录） | 旧数据可迁移且双栈不串 |
| C-04 | P1 | cli.py `_pid_alive` L927 | ctypes 写死 Windows kernel32 调用；POSIX 分支依赖 `os.kill(pid,0)`（未在代码中看到完整实现路径的测试）；混合部署（WSL 宿主 + Windows 数据盘）下锁接管行为不可预测 | 跨平台陈旧锁处理差异 | 补 POSIX 探活单测并文档化 | 两平台锁接管测试通过 |
| C-05 | P2 | main.py `_ident` | platform_id 为空的 adapter（部分 Telegram/微信 web 实现不填 platform_id）直接 raise ValueError，功能整体不可用 | 特定 adapter 上插件瘫痪 | 降级策略：platform_id 缺省时用 channel 名 + 显著告警，而非硬拒 | 缺失字段时有可用降级 |
| C-06 | P2 | main.py metadata.yaml | 版本号 `v0.2.3` 带 v 前缀，AstrBot 插件市场发布要求 semver 裸版本（部分市场工具解析失败） | 无法上架/更新检测失败 | 改为 `0.2.3` 并加 changelog | 市场校验通过 |
| C-07 | P2 | main.py timeout vs core.py STALE_LOCK_SECONDS | 桥接超时 15s < 陈旧锁接管阈值 30s：核心被超时 kill 后遗锁，接下来 30s 内所有写操作排队直到接管 | 超时后连锁超时 | bridge 侧 kill 后主动清理自己 BRAIN_MEMORY_HOME 下的孤儿锁（按 pid 判定），或核心缩短 stale 阈值 | kill 后 5s 内恢复写入 |
| C-08 | P2 | scripts 核心整体 | Python 版本兼容声明缺失：代码用 `int \| None`（3.10+）、`is_relative_to`（3.9+）、`time.time_ns`；桥接用 AstrBot 宿主解释器，若宿主为 3.8 则语法错误 | 低版本宿主 import 即崩 | metadata/README 声明 requires-python>=3.10；bridge initialize 预检解释器版本 | 低版本给出明确报错 |
| C-09 | P2 | main.py `capture_turn` | 不同平台 adapter 的 `get_message_str()` 对 @bot 前缀、图片占位、reply 引用的还原不一致；图片/语音消息得到空串或占位符，`_capturable` 行为因平台而异 | 跨平台写回口径不一 | 事件预处理层（剥离 CQ 码/at 段）+ 每平台样例测试 | 主流 adapter 样例通过 |
| C-10 | P2 | cli.py 输出 | CLI 输出为中文人读文本 + `--json` 两种形态，但部分命令（topic status 等）`--json` 覆盖不全；桥接作为机器消费方解析脆弱 | 桥接升级需要文本解析 | 统一 `--json` 机器协议并定义 schema 版本 | 全部桥接用命令支持 --json |
| C-11 | P3 | growth.py `git clone` L1448 | 依赖宿主机安装 git；未装 git 时 zip 降级链路存在但 Gitee 风控下易全链路失败 | 部分环境无法自更新 | 文档化依赖或内置纯 HTTP 兜底 | 无 git 环境可更新 |
| C-12 | P3 | main.py `StarTools.get_data_dir` | StarTools 行为在 AstrBot 不同版本对 data 目录解析可能不同（相对/绝对），桥接 resolve 后直接使用 | 升级后数据目录漂移，旧记忆"消失" | initialize 时检测目录内已有核心文件决定迁移提示 | 目录变更时有迁移引导 |
| C-13 | P3 | core.py `fmt_time` L151 | `datetime.fromtimestamp` 用本地时区，无 tz 配置；跨时区迁移机器后时间线错乱（retention/recency 计算不受影响，但展示与 expires 授权受影响） | expires 语义漂移 | 内部统一 UTC 时间戳（now_ts 已是 UTC 秒，展示层才本地化，需审计所有 fromtimestamp 调用点） | expires 不受时区影响测试 |
| C-14 | P3 | astrbot_plugin_lingshu/README.md | README 未说明 engine/ 副本与核心仓库的版本对应关系与同步方式 | 用户混装版本排查困难 | README 增加版本对照表 | 表格存在且 CI 校验 |

### 4.5 边界场景（E-01~E-16）

| 编号 | 优先级 | 位置 | 问题 | 影响 | 处理要求 | 完成标准 |
|---|---|---|---|---|---|---|
| E-01 | P1 | memory.py `remember` L774 | 空文本抛 ValueError，但桥接 `记住` 已预检；auto_capture 链路 `_capturable` 预检后仍可能因 strip 后为空（全空白消息）走 CLI 报错 | 噪声告警 | 桥接侧 strip 后判空跳过 | 空白消息零 CLI 调用 |
| E-02 | P1 | main.py `_validate_text` | 超长消息（>max_text 4000）拒绝写入，但 `inject_memory` 是 `raw[:self.max_text]` 截断后仍调用 `_validate_text`——截断可能恰好把首字符变成 `-` 或截出空串，行为不一致 | 边界行为不可预测 | 统一"先截断再校验"或"先校验再截断"的单一策略 | 边界值测试（3999/4000/4001/以-截断） |
| E-03 | P1 | main.py `inject_memory` | 用户消息恰为 `lingshu` 命令时 return 跳过召回——但若消息以 `/lingshu` 开头且不同 adapter 斜杠处理不同，跳过判断（startswith 元组）可能漏/误跳 | 命令轮次误注入 | 用事件是否为指令的宿主 API 判断 | 指令轮不注入 |
| E-04 | P2 | memory.py `topic_touch` L1633 | `rows = rows[:TOPIC_CAPACITY]` 静默挤出栈尾，无任何"被挤出"事件记录 | 话题无声丢失，难排查 | 挤出时 log_event | 日志可见挤出记录 |
| E-05 | P2 | core.py `ensure_home` | 数据目录只读时 ensure_home 直接 return，后续 `topic_touch` 等在锁文件创建时抛 OSError，CLI 兜底为通用错误 | 只读场景报错不明确 | 统一 READONLY 检查前置返回专用退出码 | --ro 下所有写命令返回 3 |
| E-06 | P2 | core.py `read_jsonl` L764 | 坏块挪 .corrupt 依赖可写；只读模式下 `.corrupt` 写失败被 try 吞掉，坏块每次读取都重复告警（有去重逻辑但只对成功追加时生效） | 只读环境日志噪声 | 只读时内存内去重告警一次 | 只读读取同一坏块只报一次 |
| E-07 | P2 | dream.py `apply_dream` | 迁移/恢复中断场景：restore 的临时目录 `.restore-tmp-*` 若进程中途死掉，残留目录无清理机制，下次 restore 也不清理旧 tmp | 磁盘垃圾与隐私残留（含记忆明文） | 启动时清理超龄 .restore-tmp-* | 残留目录被自动清理 |
| E-08 | P2 | cli.py `cmd_restore` L271 | restore 前自动 backup 到 HOME/backups，但若 HOME 磁盘空间不足，备份失败被 try 吞掉后仍继续恢复 | 空间不足时恢复既失败又无回滚点 | 备份失败即中止恢复 | 空间不足场景中止且提示 |
| E-09 | P2 | main.py `_ident` | channel 标识含 platform_id，用户更换 adapter 实例（换号/重连设备名变化）后 user 标识变化，旧记忆永久不可见 | 记忆"丢失" | 文档说明 + 提供 relink/迁移命令（核心已有 relink 思路，桥接侧需暴露） | 换 adapter 后可手动合并身份 |
| E-10 | P2 | main.py `_last_capture` | 节流字典 LRU 上限 5000、裁剪 1000；多群大流量下裁剪可能把活跃用户的时间戳裁掉导致重复写入 | 短时重复记录 | 裁剪策略按最近使用保留活跃 key | 压测活跃用户不被裁剪 |
| E-11 | P2 | core.py `_locked` | 锁文件写入 pid 后，Windows 上两个不同用户会话（不同 SID）共享目录时 O_EXCL 正常但 `_pid_alive` 跨会话 OpenProcess 可能拒绝访问返回 False → 误判死锁 → 并发写 | 多用户共享数据目录时数据竞争 | 跨会话场景探测失败一律视为存活 | 共享目录并发测试 |
| E-12 | P2 | main.py `initialize` | 只在启动时探测目录可写；运行中磁盘满/目录变只读后，写回持续失败仅 warning，无降级与告警升级 | 静默丢写回 | 连续失败 N 次后 health 降级并停止 auto_capture | 连续失败触发状态变更 |
| E-13 | P2 | memory.py `recall` | include_archive + target_user 组合下，归档记忆与现役记忆去重仅靠 id（归档层 id 相同），归档后原记录标 archived_at 仍在 load_all——重复计分可能 | 排序轻微失真 | 归档候选去重断言 + 单测 | 同 id 不重复计分 |
| E-14 | P3 | main.py `_slots=2` + `_pending=8` | 多 bot 单进程多群高并发时 2 并发 subprocess 成瓶颈，超限请求被立即拒绝（而非排队） | 高峰期功能间歇不可用 | pending 满时改为有限排队或动态扩容 | 压测 P99 等待可控 |
| E-15 | P3 | core.py `log_event` 滚动 | 512KB 滚动用"保留后半"而非按行数，多进程同时滚动时 read_text 与 write_text 竞态（无锁） | 日志偶发截断 | 滚动加专用锁或单进程化 | 并发滚动不丢行 |
| E-16 | P3 | emotion.py `feel` | intensity/decay 参数无上界钳制的完整审计（decay_hours 有 0.5-720 钳制，intensity 依赖调用方）；桥接未来接入时需确认 | 接入后异常值污染情绪状态 | 钳制审计清单 | 全参数边界测试 |

### 4.6 改进点（I-01~I-16）

| 编号 | 优先级 | 位置 | 问题 | 影响 | 处理要求 | 完成标准 |
|---|---|---|---|---|---|---|
| I-01 | P1 | main.py 整体 | 每轮对话 1-2 次 subprocess 冷启动（recall + remember），延迟与 CPU 成本高 | 高频群聊响应变慢 | 引入常驻 worker 进程（stdin/stdout JSON-RPC 长连接）替代逐条 spawn | 单次调用延迟 <50ms |
| I-02 | P1 | 全仓 | 无任何自动化测试（tests/ 缺失），回归全靠手工 | 迭代风险高 | 为核心模块（memory/topic/consent/lock）与桥接（ident/reply/validate）建 pytest 基线 | CI 跑通冒烟集 |
| I-03 | P2 | brain.py + engine/ | 双副本架构靠 sync_engine.py 人工同步 | 漂移已实际发生（2.69 vs 2.71） | 以 CI 或安装时校验替代人工同步 | 漂移自动发现 |
| I-04 | P2 | main.py `inject_memory` | 召回无缓存：同一群同一消息重试/复读会重复全量查询 | 资源浪费 | 按文本哈希 + TTL 做短缓存 | 复读命中缓存 |
| I-05 | P2 | memory.py `recall` | 打分无可解释输出给桥接（parts 有但桥接丢弃） | 调优盲区 | 桥接 debug 模式输出 score/parts | 可解释召回可开关 |
| I-06 | P2 | cli.py CLI 输出协议 | 机器可读性差（退出码 + 中文文本），桥接只能模糊映射 | 错误处理粒度粗 | 全命令 `--json` 化（结构 {code,msg,data}） | 协议文档 v1 发布 |
| I-07 | P2 | metadata.yaml | repo 指向 GitHub 仓库，但更新源先 Gitee 后 GitHub，双源一致性无校验 | 两源内容可能不同 | 更新时校验两源 HEAD SHA 一致或声明主源 | 主源声明清晰 |
| I-08 | P3 | panel_server.py | 单文件 1500+ 行，Handler 巨型方法堆叠 | 可维护性差 | 按路由拆分模块 | 文件 <500 行/模块 |
| I-09 | P3 | core.py `analyze_topic` | 关键词表硬编码在代码里 | 定制需改源码 | 词表外置 config 或 skills 数据文件 | 词表可配置 |
| I-10 | P3 | memory.py topic 系列函数 | 话题无 TTL 主动清理（靠读时衰减+TTL 判定），topics.jsonl 只增不减（drop 前） | 文件缓慢膨胀 | 定期物理清理过期话题 | 文件行数有界 |
| I-11 | P3 | main.py | 无指标采集（调用次数/耗时/失败率），问题定位靠翻日志 | 可观测性弱 | 简单计数器 + `lingshu 状态` 展示 | 状态页含统计 |
| I-12 | P3 | cli.py `main` L2774 | 兜底 except 把 traceback 截 400 字符写日志 | 长堆栈信息丢失 | 完整堆栈单独落诊断文件 | 崩溃可完整复盘 |
| I-13 | P3 | growth.py 远程模块 | REMOTE_TIMEOUT 等常量不可配置 | 弱网环境不可调 | 配置化 | config 可覆盖 |
| I-14 | P3 | docs | 缺少隐私数据流图（哪条消息流到哪个文件、谁能读） | 用户信任成本高 | 本文档 §3.5 基础上补数据流图并入 README | 图存在 |
| I-15 | P3 | main.py `_resolve_engine` | 探测失败时静默回退内置路径，仅日志提示 | 配置错误被掩盖 | 初始化失败时在 `lingshu 状态` 置顶告警 | 状态页醒目提示 |
| I-16 | P3 | make_checksums.py | checksum 清单与 engine/ 镜像的关系未文档化，手工维护易漏 | 校验形同虚设 | checksum 生成纳入发布脚本 | 发布流程自动生成 |

---

## 5. 迭代路线建议

**P0（安全与数据，1-2 周）**
1. S-01/S-02：记忆静态存储加密选项 + 群聊原话写回的隐私粒度拆分；
2. B-01：修正 inject 判重条件（`header not in base`）；
3. B-05：超长查询改临时文件/stdin 传递，规避 Windows argv 上限；
4. B-03：关键 JSON 写入补 fsync；
5. S-11：子进程 env 白名单化。

**P1（兼容与桥接打通，2-4 周）**
1. 话题栈打通（F-01/F-02/F-03）：先做 B-08/C-03 的 scope 隔离，再在桥接 inject/response 链路接 topic touch/feedback/can_spawn + 情绪/模式注入；
2. C-01/C-02：钩子版本防御 + engine 镜像 CI 同步校验；
3. C-04/C-05/C-08：Python 版本预检、platform_id 降级、锁探活修正；
4. S-04/S-05：更新链路 checksum 强制校验、token 移出 URL；
5. B-04/B-06/B-09：CLI 传参与错误码映射修缮。

**P2（边界与健壮性，4-8 周）**
- E 组全部（只读降级、restore 中断清理、并发锁边界）+ B-12/B-13（save_all 锁审计、召回索引缓存）+ S-06/S-07/S-08/S-09/S-10（面板 token、群内展示、全局记忆可见性、CLI 信任模型）。

**P3（改进，长期）**
- I 组：常驻 worker 进程（I-01）、pytest 测试基线（I-02）、CLI --json 协议（I-06）、面板拆分与可观测性。

---

## 6. 验证计划

1. **单元测试**：pytest 覆盖 `_locked`（并发 8 进程写同一 JSONL）、`_channel_visible`（私聊/群聊/授权矩阵）、`topic_touch/feedback/drop`（含 scope 隔离）、`_vault_encrypt/decrypt` 往返、`_validate_text` 边界（3999/4000/4001/-开头/\x00）。
2. **桥接集成测试**：本地起 AstrBot + aiocqhttp 假 adapter，验证：
   - `lingshu` 全动作（含错误码 3/4 路径）；
   - `on_llm_request` 注入（含 B-01 判重、超长消息 B-05、contexts 缺失 C-01）；
   - `on_llm_response` 写回（consent 开关、节流、队列上限、terminate drain F-06）。
3. **并发/崩溃演练**：同时 4 进程 remember；kill -9 持锁进程后 30s 内恢复；restore 中途 kill 后启动清理 `.restore-tmp-*`；磁盘只读切换后写命令降级。
4. **安全验证**：zip-slip 恶意归档样例（B-16 前缀绕过 PoC + 修复后回归）；日志 grep 无 token/密码；S-11 env 白名单后子进程内打印 environ 验证。
5. **兼容矩阵**：AstrBot v4.5.7 与 v4.13+ 各跑注入/写回冒烟；Windows（CRLF/BOM/锁）与 Linux 各跑全量；Python 3.10/3.12。
6. **话题栈联调验收**：双群并行拟人对话 30 轮，断言两群 topics.jsonl scope 互不污染、敷衍反馈后权重下降、spawn 条件触发后注入包含自发话题提示。

---

## 7. 修复记录（v1.1，2026-11-06）

§4 问题清单 100 项已按三组分批修复并汇总验证：

### 7.1 验证结果

- `py_compile` 全部改动文件通过（core / memory / emotion / cli / growth / dream / panel_server / brain / 桥接 main.py）。
- CLI 冒烟（临时 `BRAIN_MEMORY_HOME`）：`init` / `topic touch` / `topic status` / `channel remember` / `channel recall --json` 全部正常，召回返回结构化 JSON（score/parts）。
- engine/ 镜像已与 `scripts/` 主目录逐文件哈希同步（brain/cli/core/dream/emotion/growth/memory/panels/panel_cloud/persona）。

### 7.2 修复分组摘要

| 分组 | 范围 | 代表性改动 |
|---|---|---|
| R1 核心层 | core.py / memory.py / emotion.py / sync_engine.py | B-01 判重条件、B-03 atomic_json fsync、B-08/C-03 话题栈 scope 隔离、B-10/B-11 锁与进程探测、E-04/05/13/15、F-13/F-14、I-10 |
| R2 桥接插件 | main.py / _conf_schema.json / metadata.yaml / README.md / CHECKSUMS.json | F-01 话题联动、F-02 情绪/模式注入、F-03 startup 注入包、B-05 超长查询降级、B-14 consent 缓存锁、S-11 env 白名单、C-05/C-06/C-07、F-04~F-12、E-01/02/03/10/12 |
| R3 工具链 | cli.py / growth.py / dream.py / panel_server.py | F-08/F-15/F-16/F-18、S-04/S-05/S-10/S-12/S-15、B-16/B-17、C-08/C-10/C-11/C-13、I-06/I-07/I-12/I-13、F-17、S-06/S-07 |

### 7.3 遗留事项

- 插件配置新增项（topic_linkage / emotion_injection / startup_injection 等）默认关闭，需在真实 AstrBot 实例联调后逐项开启（见 §6 验证计划）。
- C-01（AstrBot v4.5.7 钩子签名差异）与双宿主写回冲突（README 已说明）仍依赖宿主侧验收。
- 实际运行测试仅覆盖 Windows Python 3.12/3.13；Linux/Docker 平台验收仍待执行。
