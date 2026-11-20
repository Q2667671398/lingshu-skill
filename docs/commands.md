# 灵枢 · 命令速查与排障（按需查阅）

> 这是 [SKILL.md](../SKILL.md) 的完整命令表 + 故障排查。主文件只留高频命令索引；**不确定某命令的确切参数时，用 Read 读这里。**

## 完整命令速查

所有命令前缀认准 `python scripts/brain.py`（Windows 双击 `start-panel.bat` 只启动面板）。

| 目的 | 命令 |
|---|---|
| 初始化 | `init`（`--force` 重置人格，覆盖前自动留一份 `persona.json.pre-init-<时间戳>`） |
| 会话开始钩子 | `startup --query "关键词"`（返回码 0/10/2/64） |
| 注入记忆 | `inject "关键词" [--budget 1400]` |
| 只读降级 | `--ro inject "关键词"`（或 `BRAIN_MEMORY_READONLY=1`） |
| 换数据目录 | `--home <目录> ...` / `home move <目录>` / `home verify` |
| 备份 / 恢复 | `backup [--out 目录] [--keep 20]` / `restore <zip> [--yes]` |
| 工作记忆 | `work [status\|list]`（看一眼）→ `work add "..."` → `work done <序号>` / `work flush [--skip-done]` / `work clear` |
| 写入 | `remember "内容" --kind fact --importance 7 --tags a,b`（kind 可用 mistake=错误教训 / relationship=人际关系，注入时独立成块） |
| 回忆 | `recall "关键词" --k 8` |
| 遇到不会的 | `learn ask "问题"` / `learn loop` |
| 做梦 | `dream` / `dream --apply apply.json` / `dream --auto [--dry-run]` |
| 与 DSH 同步 | `sync dsh [--dir 目录] [--days 1] [--force] [--dry-run]` |
| 上下文水位 | `check-context --used 68`（60 清点 / 75 做梦 / 90 封存，可加 `--unsaved 3000`） |
| 原样封存 | `capture "<原文>" [--tag 逃生] [--raw-only]` |
| 人格 | `persona [show\|sync\|path\|set traits.幽默=70\|lock 名字\|unlock 名字]` |
| 人格历史/回滚 | `persona history [--limit N]` → 看来源与依据 → `persona rollback <序号>` |
| 人格快照/漂移 | `persona snapshot --label "基准"` / `persona snapshots` / `persona restore <名字>` / `persona drift [--days 7] [--json]` |
| 人格自检 | `persona check [--json]`（校验 traits 溯源结构/数值越界/history 溯源；FAIL 退出码 1） |
| 画像归纳 | `persona suggest [list\|scan\|adopt <序号>\|discard <序号>]`（从最近记忆归纳候选，采纳时写证据链） |
| 人格审计链 | `persona audit [--days 30] [--json]`（时间线回看谁改了什么、证据是哪条记忆） |
| 变动已读 | `persona viewed`（把「先生效后告知」的未读人格变动标为已读） |
| 遗忘纠正 | `memory boost <记忆id>`（恢复被遗忘/低稳定度的记忆，稳定度×1.5） |
| 人格编辑 | `persona set traits.幽默=70`（逐字段改，写进历史可回滚） |
| 初识 | `persona interview [--name ... --traits ...\|--unsure]` → `persona self [--name ... --traits ... --likes ... --dislikes ... --habits ...]` |
| 理智/好奇 | `mind [state\|weigh "事" --benefit 7 --cost 3\|tune --curiosity +5 --reason -3 --why "..."]` |
| 会话模式 | `mode show\|work\|human\|auto\|explain "一句话话题"`（工程会话自动隔离情绪；explain 打印 auto 判定依据） |
| 审核闸 / 自查 | `review [--json]` → `review confirm <id>` / `review discard <id>`（记忆 m_* 与知识 k_* 通用）；`audit [--json]` |
| 心情（七情六欲） | `emotion [now\|arc\|list\|feel 情绪名 --intensity 0.7 --why "..."\|desire 欲望名 --amount 20]` |
| 情绪事件/微调 | `emotion events`（带序号）→ `emotion adjust <序号> [--intensity 0.3] [--emotion-name 委屈] [--why "..."]` / `emotion del <序号>` / `emotion cool` |
| 事件→情绪（认知评价） | `emotion evaluate "事件" [--polarity auto\|positive\|negative\|neutral] [--intensity 0.8]` |
| 情绪-输出一致性 | `emotion check "回复文本"`（内部情绪 vs 回复语气是否背离，重度背离时告警） |
| 共情 / 情感设置 | `emotion mirror "用户情绪" [--intensity 0.7]` / `emotion config [--empathy mirror\|mild\|off] [--gain 0.5] [--max-intensity 0.8] [--suppress "怒,恶"] [--notes "..."]` |
| 记忆关联 | `assoc [link <idA> <idB> --relation 因果 --weight 1\|unlink <idA> <idB>\|of <id>\|auto [--dry-run]\|graph --limit 300]` |
| 每日例行 | `daily [--dry-run] [--force] [--feel "情绪:强度:原因"]`（写日记 + 沉淀知识 + 过期的梦自动做掉；`--feel` 可重复，顺手把当天对话里的情绪事件记进情绪流） |
| 回忆使用反馈 | `usage --reply "回复文本" [--json]`（核对上一轮 recall/注入的记忆有没有出现在回复里，按关键词重叠粗判；命中的记忆会加一次使用强化） |
| 自我总结（每2-3天） | `skill self-review [--force] [--days 3]` |
| 知识库 | `knowledge add\|list\|search\|show\|pin\|rm` |
| 整理知识 | `knowledge tidy` → 填 apply.json → `knowledge tidy --apply apply.json` |
| 知识审核 | `knowledge review` / `knowledge confirm <id>` / `knowledge discard <id>` |
| 笔记 | `note write "标题" --type journal\|log\|growth --body ...` / `note list [--type ...]` / `note show <id>` |
| 笔记检索 / 素材 | `note search "词"` / `note digest --days 7` |
| 技能 | `skill new\|edit\|list\|show\|check\|install\|uninstall\|rm\|find\|use\|borrow\|roots\|scan` |
| 技能安装 | `skill install <名字> [--force] [--from 外部目录]`（整目录 + 自动生成 AGENTS.md；装灵枢本体需先过风险确认 `--yes`，中文名/英文名 `lingshu` 都认；自动装到 DSH 的 `~/.dsh/skills`，无则回落 Trae 的 `~/.trae-cn/skills`） |
| 技能根目录（跨宿主） | `skill roots`（列出全部根目录与技能数）/ `skill root-add <路径>` / `skill root-rm <路径>`（手动增删自定义根目录，写入 config.json `skill_roots`；也支持环境变量 `LINGSHU_GLOBAL_SKILLS`，多个用 `; ,` 分隔）。内置 15 个宿主候选：DSH / Trae CN / Trae / Agents / Claude Code / Codex / Cursor / Gemini / Antigravity / Amp / Qwen / Windsurf / OpenCode / Cline / GitHub |
| 跨宿主扫描 | `skill scan`（只读扫描所有根目录，列出其他宿主里现成可复用的技能；`skill use <名字>` 可直接读正文照做，`skill borrow <名字>` 可复制为本地技能自由修改） |
| 已装技能自查 | `skill list --global`（列出全局技能目录已装的技能，核对目录名是否为 kebab-case；DSH/Trae 两目录都会扫，旧版中文目录会被标 `✗`） |
| 技能卸载 | `skill uninstall <名字>`（`lingshu` 会连旧版中文目录 `灵枢` 一起删掉，两个全局目录都清，避免 DSH 仍去扫空壳） |
| 技能自动全局化 | `skill use <名字> --auto` |
| DSH 插件（只读） | `skill plugins [--json]` |
| 用户附加规则 | `user-rules show\|add <规则>\|clear`（数据目录 lingshu.user.md，更新永不覆盖，每会话自动注入；`clear` 前自动备份） |
| 扩展模块 | `ext new\|edit\|show\|list\|run\|check\|rm`（可执行钩子，数据目录，更新永不覆盖） |
| 成长 | `growth status` / `growth review --days 7` |
| 进化日志 | `evolve log "标题" --kind persona\|skill\|knowledge\|thinking\|self` / `evolve list` |
| 统计 | `stats` |
| 运行日志 | `logs [--limit 20] [--level warn\|error] [--all]`（锁冲突/异常/解析报错，面板「日志」页同款） |
| 列表 / 忘记 | `list` / `forget <id>` |
| 配置 | `config show` / `config set <key> <value>` / `config unset <key>` |
| 记忆冲突检查 | `detect-conflicts [--json] [--limit 50]` |
| 聊天平台通道 | `channel remember "内容" --channel qq --group 群号 --user qq:10001 [--kind fact] [--importance 7] [--tags a,b] [--source user\|ai\|dream] [--layer hippo\|cortex]` |
| 通道召回 | `channel recall "关键词" --channel qq --group 群号 --user qq:10001 [--k 8] [--json] [--asker 发起者 --target 目标]` |
| 跨群画像 | `channel user qq:10001 [--k 20] [--asker 发起者]`（asker == 目标 → 全量；否则要求目标已 grant） |
| 群友隐私 | `channel privacy grant\|revoke\|status\|capture-on\|capture-off --user qq:10001 [--expires-days N]`（只能操作自己的授权；群内 grant 只对该群可见，`--expires-days` 0/不填为永久，到期自动失效） |
| 主人白名单 | `channel owner add\|remove\|list [qq:10001] [--caller 身份]`（owner add/remove 仅限本机或已有 owner；通道内必须带 `--caller` 且其为 owner） |
| 体检 / 水位 | `doctor [--json]`（11 项，见下节）/ `status [--used 68] [--json]`（各层条数 + 距上次做梦/交接 + 阈值，唯一水位数据源） |
| 上下文账单 | `context-stats [--budget 1400]`（注入包按段算字符/token 占预算比例） |
| 五段式装配 | `context-plan [--budget ...]`（按预算试排 stable/summary/retrieved/recent/current）/ `summarize`（模型优先，失败回退离线抽取）/ `session-summary [status\|make\|show\|rollback]` |
| 检索服务（RAG） | `rag init` / `rag ingest <目录或文件>` / `rag search "词"` / `rag stats` / `rag serve`（SQLite + FTS5；`retrieve "词"` 优先走它，失败回退本地召回） |
| 失败可见化 | `failures [--limit 20] [--json]`（被吞异常按类聚合，每 512KB 自动轮转）/ `ab-stats [--days 7]`（上下文装配 A/B 节省比例） |
| 注入自检 | `inject-drift`（连渲染几次注入包，看稳定前缀是否稳，prompt 缓存友好度）/ `session-state [--clear]`（会话级注入快照，需 `config session_state_cache=true`） |
| 解引用 | `deref <记忆id>`（取一条记忆的证据原文；注入时只给要点，全文按需取） |
| 收尾清点 | `checkpoint [--repo 路径] [--json]`（用 git 证据写一条收尾记忆，幂等）/ `replay`（只读回放人格偏移与拐点）/ `saturation`（traits 距 0/100 余量，≥0.8 告警） |
| 跨对话摄取 | `ingest-dialogues <文件…> [--dry-run]`（其他会话导出的 JSONL/MD/TXT 增量读成记忆）/ `extract <文件>`（静默即记即存，供调度插件后台调用） |
| 教训库 | `lesson "教训" [--tags ...]`（一条命令三写：mistake 记忆 + pending 知识 + 笔记）/ `lessons [list\|add\|check]` |
| 话题栈 | `topic [list\|add "话题"]`（拟人闲聊用，记聊过的话题与权重） |
| 归档 | `archive [run\|list\|restore <id>]`（冷热归档，原文保留可回捞）/ `auto-archive`（pending 记忆按事实/事件沉淀进知识库或日记） |
| 联想与向量 | `relink [--dry-run]`（按词面相似度补挂 links）/ `rebuild-embeds`（换模型/缓存坏/缺向量时重建）/ `drift [--days N]`（按近期情感倾向静默微调 mood_score） |
| 记忆操作 | `memory dust fold\|unfold\|revive <id>`（淡忘可逆）/ `memory diagnose` / `memory fix-links` / `memory freeze\|unfreeze <id>` / `memory purge <id>`（物理删除，需 dust 满 7 天 + 手输 id 前 8 位） |
| 审计 / 导出 | `auditlog [--limit N]`（敏感写/删/导出的最近记录）/ `export [--out 文件] [--layer ...] [--md]` |
| 可视化面板 | `serve [--port 8765] [--no-browser]` → http://127.0.0.1:8765（面板细节见下节） |

### channel 召回的可见性规则（通道三元组）

`--channel/--group/--user` 任一给出即进入通道上下文，召回范围被物理过滤：

- 其他群 / 其他 channel 的记忆**不可见**（即使已授权）；
- 同群记忆：本人的全可见；他人的需其 `consent`（群内授权只对该群，`*` 全域授权在所有群生效）；
- `user` 为空的全局/群公共记忆：可见；
- 私聊（`--group` 留空）：本人记忆 + 全局记忆可见。

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 正常 |
| 2 | 未初始化 / 命令错误（startup 只认 `--query`） |
| 3 | 鉴权拒绝（通道上下文执行 owner/forget/dream/backup/restore/sync 等敏感命令，非 owner/admin） |
| 4 | 隐私拒绝（`recall --asker/--target` 或 `channel user --asker`：目标未 grant；消息不回显目标完整标识） |
| 10 | 只读降级 |

### 破坏性操作的确认口径（v3.52 统一）

会**覆盖或删除数据**的命令不再各写一套确认，统一走 `safety.confirm_destructive()`：

| 规则 | 行为 |
|---|---|
| 影响面 | 无论是否交互，先打印"动哪个目录 / 会不会删文件"（`restore` 会按 `--exact` 区分"备份之后新增的文件删不删"） |
| `--yes` | 直接放行（脚本、宿主、CI 用这个） |
| 非交互 + 没有 `--yes` | **拒绝**，退出码 1，并提示必须加 `--yes`（管道、AI 调用、计划任务都属于这一类） |
| 交互 | 问 `y/N`，**默认 N**；回车 / 其它输入都算取消（退出码 0） |

覆盖的命令：`restore`（含 `--exact`）、`memory purge`（令牌 `--confirm <id前8位>` 仍是身份确认，影响面清单由同一函数打印）。

例外：`backup` 不加口令时只**提示**不拦截——它不销毁数据，且 `restore` 前的自动备份内部传 `assume_yes=True`，不能被这条规则挡住。

### 自动写回（AstrBot 桥接 `auto_capture`）

默认**关闭**。开启后群友消息写回 `channel remember --source user`（用户原话立即取代旧说法），但有三道闸：敏感词拦截 → 用户 consent 门控（`lingshu 拒绝` 即停）→ 节流。详见[插件 README](../scripts/astrbot_plugin_lingshu/README.md)。

## 体检 doctor：11 项与处置

`python scripts/brain.py doctor [--json]` 一次跑完下列检查。退出码：**0 = 没有 FAIL**、**1 = 只有 WARN**、**2 = 有 FAIL**（CI 用 `rc==2` 判红，warn 允许）。
不带 `--json` 是给人看的逐行结果；`--json` 给脚本（`{"checks":[{name,status,detail}],"summary":{...}}`）。

| # | 检查 | 在查什么 | 报 WARN/FAIL 时怎么办 |
|---|---|---|---|
| 1 | `audit` | 待审记忆 / 待审知识 / AI 改人格 / 疑似冲突 | `review` 过一遍；冲突用 `detect-conflicts` 看明细 |
| 2 | `persona` | 人格完整性（traits 溯源结构、数值越界、history 溯源） | `persona check` 看具体问题；坏了从 `persona snapshots` 回滚 |
| 3 | `diagnose` | 记忆库结构（死链、折叠态、来源归属完整率） | `memory fix-links` 清死链；其余按提示逐条处理 |
| 4 | `writable` | 数据目录可写 | 权限/磁盘问题；只读降级下所有写命令返回码 10 |
| 5 | `backup-sha` | `backups/` 里的副本与当前库 sha 是否一致 | 不一致通常是正常更新；长期不一致说明备份没跑成 |
| 6 | `log-anomalies` | `lingshu.log` 里累计 warn/error 条数（≥10 告警） | 先 `logs --level error`；持续增长看 `failures` |
| 7 | `version-matrix` | `SKILL.md` / `package.json` / 插件 `metadata.yaml` 三处版本互账 | 升版时三处一起改；`metadata.yaml` 是 AstrBot 独立版本线，不参与相等比较 |
| 8 | `staging` | 被强杀的更新留下的临时目录（>1h 自动清） | 一般自愈；反复出现说明更新过程被杀 |
| 9 | `writers` | 当前持锁的写入者（多机共享记忆库时能看见另一台） | 是别的会话在写就等；"已退出"仍占锁 → 删对应 `.lock` |
| 10 | `engine-sync` | `scripts/` 与 AstrBot 插件里的 `engine/` 副本是否一致（33 脚本 + 11 文档，含 `assets/panel.html`） | `python scripts/sync_engine.py` 同步；漏跑会发出旧核心 |
| 11 | `release-drift` | **本机已安装副本的版本 vs 源码版本**（DSH 各 profile 的 `node_modules/lingshu-skill` + 各宿主全局技能目录里的 `lingshu/`） | 落后 → 面板「检查更新 → 修复 DSH 副本」或 `powershell -ExecutionPolicy Bypass -File scripts\update_dsh_copy.ps1`；全局技能副本落后 → `skill install lingshu --from <当前目录> --force --yes`。**改完记得重启宿主/把插件关一次再开一次**，改文件不会自动重载 |

> 第 11 项是 v3.47 新增的：它要防的就是「代码里已经修好、机器上跑的还是旧版」——v3.45 的护栏写好了，运行时却还是 v3.44，一路没人发现，因为旧版 doctor 只查包内自洽。

## 可视化面板

```bash
python scripts/brain.py serve
```

浏览器打开 http://127.0.0.1:8765 ：记忆时间线与保留率、人格档案与好奇心/理智、**此刻理智状态（被情绪压住多少）**、知识库、笔记日志、技能自编辑、成长指标与复盘素材、**每日例行与自我总结**、进化轨迹、手动做梦与梦报告。仅监听本机。v2.3 起侧栏还显示**会话模式**（可点击切换）、**待审核项计数**、**人格漂移告警**；记忆列表里的待审核条目可直接 ✓/✗ 确认或废弃。

新增（v2.11 后）：

- **概览 · 会话模式判定卡**：当前模式 + 置信度 + 工程/情绪两侧命中分数与命中词；输入一句话点"试判"，看 auto 会怎么切、为什么切。
- **记忆页 · 批量条**：勾选（含全选）后批量 confirm/discard 待审核、批量设重要性、批量加/减标签；每条卡片底部显示 `id xxx` 方便复制去做关联。
- **梦页 · 草稿编辑**：pending 草稿自动落盘 `dreams/apply_draft.json`，文本框里直接改 JSON 再 apply；apply 成功自动清草稿，旧任务草稿不会串台。
- **心情页 · 情绪-输出一致性核对**：输入一段回复点"核对"，判定语气与内部情绪状态是否严重背离，并列出最近告警。
- **联想页 · 手动建立关联**：填 A/B 记忆 id + 关系名 + 为什么，一键连线，配合 3D 星图看网络。
- **日志页**：按 warn/error/全部过滤查看运行日志（锁冲突/异常/解析报错），概览显示近期 warn/error 计数。

新增两块：

- **心情页**：七情占比、六欲、"事件 → 情绪"认知评价（输入一句话，看它怎么判断、冒出什么情绪、理智被压多少、大事是否交给你）、共情试一下、情感设置（共情等级/比例/上限/压下去的情绪，随时改）。
- **联想页（3D 空间）**：每条记忆一个节点、连线是关联，拖拽旋转、滚轮缩放、点节点看详情与邻居；"自动联想"一键按共享实体/标签补边。

Windows 可双击 `<本技能目录>/start-panel.bat` 启动。

**v3.36 之后新增**

- **运行洞察浮窗**（右下角）：版本 / 原文库 / 收尾清点 / 群聊鉴权四张 KPI，加检索库、嵌入、A/B、近 24h 异常；主题（跟随系统/深/浅）与密度（标准/紧凑）也在里面。
- **浮窗可拖**（v3.47）：按住标题栏拖走，松手记住位置（存 `localStorage`），窗口缩放/收起都会把它夹回视口；双击标题栏收起；窄屏首次打开默认收成一小颗。
- **页签深链**（v3.47）：地址栏带 `#memories` / `#diag` 这类 hash，刷新、收藏都回到同一页；`Ctrl/⌘K` 命令面板命中记忆时会先把搜索框填好再跳过去。
- **批量条按需展开**（v3.47）：没勾选时只占一行，勾选后才展开那排批量操作；空库整条收起。
- **诊断页 · 数据目录**：显示当前库位置与体积、是否只读；填一个空目录点「迁移」可整库复制过去（旧目录原样保留）。
- **技能页**：跨宿主技能根目录（有不存在的会收进一行折叠摘要）、DSH 插件只读清单、其他宿主技能「▶ 使用 / ⇙ 借用」。

**面板也能从 AstrBot 插件里开**（v3.47 起）：插件包里带了 `engine/scripts/panel_server.py` 与 `engine/assets/panel.html`，所以装完插件可以

```bash
python <插件目录>/engine/scripts/panel_server.py --no-browser
```

面板内容与主仓库那份同源（`panel_build --check` 守源码↔产物、`sync_engine --check` 守产物↔插件副本两道闸）。插件里没有 `manifest.json`，版本号改读 `SKILL.md`兜底，不会空着。

## 故障排查 FAQ

**Q：模型总是忘走 `startup`，怎么办？**
把「每会话固定动作」第一条抄到宿主（Claude Code / Cursor / OpenCode / Trae）的项目级规则或开场指令里，让宿主每次会话开始时先强制跑一次。`startup` 能把"跑没跑"变成返回码，但"跑不跑"取决于宿主与模型。小模型环境直接改用 `SKILL-tiny.md`（只留硬约束）。

**Q：说了 `remember` 却没写进去？**
先看输出是不是只读降级（返回码 10 / 提示只读）——数据目录不可写时脚本不会写。再用 `stats` 对总数；正常情况不会丢（原子写 + 跨进程锁 + 损坏行自愈），写不进多半是 `--home` 指错了目录或沙箱拒写。

**Q：`startup` 返回码 64？**
命令写错了：`startup` 只接受 `--query "..."`，不接位置参数。见返回码表。

**Q：回忆后记忆层数变少？**
不应该。`recall` 的强化只写回被"想起"的那条，不碰其它层（淡忘区被清空的 bug 已修复并有回归验证）。若异常先 `stats` 对比各层数量。

**Q：情绪曲线和实际感觉对不上？**
"此刻心情"是按强度 × 半衰期（默认 6h）叠加出来的，峰值过了自然回落；要看每天的真实最高点用 `emotion arc`（峰终口径）。方向反了，多半是 `emotion feel` 的情绪名选错了——`emotion list` 看全表。判错还能 `emotion adjust <序号>` 微调，不用整条删。

**Q：哪些场景别用灵枢？**
纯一次性聊天、只想要临时算力不需要记忆、宿主不支持执行脚本、用户明确只想要"记一句话"但不想建库——这些场合灵枢是负担，直接不用。

**Q：`doctor` 报 `release-drift`「装着 vX、源码 vY」怎么办？**
说明你机器上跑的不是这份代码。DSH 的副本在 `~/.dsh/profiles/<profile>/node_modules/lingshu-skill`：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\update_dsh_copy.ps1   # robocopy 就地覆盖，不重启 DSH
```

然后**把插件关一次再开一次**（或重启 DSH）——改文件不会自动重载，不重载就还是在跑旧模块。全局技能目录里的副本用 `skill install lingshu --from <当前目录> --force --yes` 重装。修完再跑一次 `doctor` 确认该项转 PASS。

**Q：dsh-market 更新 lingshu-skill 报 `connection to github.com timed out after 5000ms`？**
宿主在安装前会自己预检 GitHub 连通性，5 秒连不上就放弃——通常网络本身是通的（`git ls-remote https://github.com/Q2667671398/lingshu-skill.git` 能返回），只是宿主阈值太短。两招任选：① 调大宿主 cordis.yml 里的 `githubConnectionTimeoutMs`（建议 15000–30000）；② v2.85 起 `index.json` 的 `repo` 已指向 gitee 主仓库，更新走 gitee 不再碰 GitHub 预检。

**Q：dsh-market 更新报「profile 声明了这些插件，但它们没有安装： dsh-lingshu-startup, dsh-lingshu-cron」？**
上次更新时 DSH 还在运行、占用着技能目录文件，pnpm 换不动目录导致安装残缺，市场为防下次启动卡死把这两个 cordis 插件从 profile 移除了（目录还在）。修法：**完全退出 DSH**（不是最小化，确保进程全退）→ 在市场里重新安装 lingshu-skill → 启动 DSH 验证。记忆数据在 `~/.brain-memory/`，重装不会丢。

**Q：退出 DSH 重装后仍报同样警告（残留旧 profile 条目）？**
这两个插件在旧版（≤v2.7x）是独立 tgz 子包，装过会把 `dsh-lingshu-startup` / `dsh-lingshu-cron` 写进 profile 的 `dsh.profile.bundles`；新版根包已内置挂载它们，商店只装根包，旧条目成了"幽灵声明"永远等不到安装。**删掉残留条目即可**：
1. 完全退出 DSH；
2. 编辑 `%USERPROFILE%\.dsh\profile\package.json`（或宿主实际 profile 目录），在 `dsh.profile.bundles` 数组里删除 `"dsh-lingshu-startup"` 和 `"dsh-lingshu-cron"` 两行（保留其他条目）；
3. 同时在 pnpm store/profile 的 `node_modules` 下若还有 `dsh-lingshu-startup` / `dsh-lingshu-cron` 残留目录，一并删掉；
4. 重启 DSH → dsh-market 更新 lingshu-skill。新版只声明根包，不会再出现这两个名字。

**Q：dsh-market 报 `installation rejected: Cannot validate installed package lingshu-skill: SyntaxError: Unexpected token '﻿'`？**
根 package.json 文件头带 UTF-8 BOM，DSH 严格 JSON.parse 读不了。v2.87 起已去除，更新到 ≥v2.87 即可；若手头是旧版，用记事本「另存为 UTF-8（无 BOM）」重存 package.json 也能临时解决。