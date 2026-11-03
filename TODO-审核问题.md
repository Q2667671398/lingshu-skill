# 灵枢审核问题待办清单

> 来源：2026-11-04 全面审核报告。共 22 项（高危 11 / 中危 9 / 低危 2），按修复优先级排序。
> 建议顺序：高危（安全 1~7 → 数据完整性 8~11）→ 中危 → 低危收尾。

---

## 高危（必须尽快修）

- [x] **1.【安全】panel_server.py:469-486 — 首页在 Host/Origin 校验前下发 TOKEN**
  DNS rebinding 可窃取全量 token。修复：Host/Origin 校验前置到任何含 TOKEN 的响应之前。✅ v2.48 已修

- [x] **2.【安全】panel_server.py:652-662 — as_page=1 把主 TOKEN 注入任意第三方面板 iframe**
  同源外可带出全部记忆。修复：第三方面板与主面板 token 隔离（独立子 token 或不注入）。✅ v2.48 已修（第三方面板不注入）

- [x] **3.【安全】panel_cloud.py:295-323 — cloud_extension_download 未调 _safe_name，路径穿越**
  且缺盘符冒号校验。修复：下载落盘统一走 `_safe_name`，补 Windows 盘符（`C:`）拒绝逻辑。✅ v2.48 已修

- [x] **4.【安全】cli.py:2146-2174 — `channel owner add` 无门控**
  带 `--user` 的普通用户可自封 owner。修复：owner 管理命令仅限本机或已有 owner 执行。✅ v2.48 已修

- [x] **5.【安全】cli.py:47-58 / 80-93 — 备份泄漏 config.json 中的 token**
  修复：备份时对 config.json 脱敏（跳过或剥离 cloud token 字段）。✅ v2.48 已修

- [x] **6.【安全】cli.py:2096-2128 — 通道身份自声明无凭证**
  `--user` 全靠宿主传入。修复：至少文档明确信任边界；有条件时增加宿主侧共享密钥校验。✅ v2.48 已修（README 信任边界说明）

- [x] **7.【安全】cli.py:749 — `if _u and _c` 门控可绕过**
  敏感命令身份判定口径不一。修复：owner/admin 鉴权统一收口到单一函数，所有敏感命令复用。✅ v2.48 已修（`_require_privileged`）

- [x] **8.【数据】growth.py:3062-3077 / 4020-4027 — 扩展无沙箱任意代码执行（inject 自动跑）**
  修复：加确认开关（首次执行需显式确认）+ 文档警示；长期考虑受限执行环境。✅ v2.48 已修（`extensions_enabled` 默认 false）

- [x] **9.【数据】growth.py:287 / 408 — add_knowledge AI 来源默认 confirmed，审核闸主路径被绕过**
  修复：AI 来源知识默认 `review="pending"`，走 confirm/discard 流程。✅ v2.48 已修

- [x] **10.【数据】dream.py:416-422 + persona.py:270-273 — NaN 致人格更新崩溃且不可恢复**
  apply 已 save_all 删掉 pending，崩溃后无法重试。修复：traits/valence 写入前 NaN/Inf 校验；校验失败保留 pending 不删。✅ v2.48 已修

- [x] **11.【数据】memory.py:743/759/786 — importance 字符串致 TypeError**
  模型产 JSON 里 importance 可能为字符串。修复：入库前强制 `float()` 转换，失败回退默认值。✅ v2.48 已修

## 中危（下一批）

- [x] **12. memory.py:1002-1004 — 隐私门 asker=None 时静默失效**：显式区分"无身份"与"本人"，无身份时拒绝或提示。✅ v2.49 已修（fail-closed）
- [x] **13. memory.py:1147 — detail 算出后未落盘**：让计算结果写回记录。✅ v2.49 已修（锁内重算落盘）
- [x] **14. memory.py:200-205 — review_report 吞异常**：至少 log_event 留痕。✅ v2.49 已修
- [x] **15. memory.py:1529-1545 — capture 锁外去重 + L1542 死代码**：去重移入锁内，删死代码。✅ v2.49 已修
- [x] **16. memory.py:1725-1726 — assoc_graph O(P×(E+T))**：建倒排索引降复杂度。✅ v2.49 已修
- [x] **17. cli.py:150-158 — restore 非 tty 时静默覆盖**：非交互环境显式 `--yes` 才允许覆盖。✅ v2.49 已修
- [x] **18. cli.py:102-103 — 备份修剪按文件名排序错误**：按修改时间排序。✅ v2.49 已修
- [x] **19. cli.py:1126 / 2102 + growth.py:1192 — 全量 load_all 性能**：按需加载或加索引。✅ v2.49 已修（count_recent 流式计数）
- [x] **20. growth.py:2372-2499 `skill install --from` 任意目录部署 / L3719 `--file` 读任意文件**：限制白名单目录；L514-517 tidy O(n²) 持锁需优化；L1311-1316 Retry-After 过大挂死需封顶。✅ v2.49 已修
- [x] **21. persona/dream/emotion 中危组**：persona.py:288-296 update_persona 仅 traits 检查锁；persona.py:364/646 溯源缺 source + edit_persona 绕过 drift 防线；dream.py:396-406 forget 静默丢；emotion.py:477 EMA valence 未 clamp/滤 NaN；emotion.py:746 `int(dr["reason"])` 脏数据崩溃。✅ v2.49 已修

## 低危（收尾清理）

- [x] **22. panels.py:516 scaffold title XSS**：`__TITLE__` 替换时做 HTML 转义。✅ v2.49 已修
- [x] **23. panel_cloud.py:89-92 token 明文存 config.json + 下载无 SHA256 校验**：提示风险；下载校验清单哈希。✅ v2.49 已修
- [x] **24. 死代码清理**：dream.py:369 `old is rec` 恒 False；cli.py:1035-1042 重复代码；panels.py:166 死 try / L177-184 重复请求；growth.py:3556 引用不存在的 note update/delete CLI、L917 parse_frontmatter 误截断、L2689 负数时间。✅ v2.49 已修（部分条目经核实此前迭代已清理，按保守原则未动）
- [x] **25. 文档一致性**：README 版本号统一为 v2.49；mechanics.md「细节回升」描述与 `_strengthen_write` 实现对齐。✅ v2.49 已修

---

## 修复约定

- 每修完一批：跑 `scripts/test_*.py` 回归 + 升 SKILL.md 版本号 + CHANGELOG 补小节（pre-commit 门禁要求）。
- 安全类修复（1~7）建议单独成版，便于用户尽快更新。
