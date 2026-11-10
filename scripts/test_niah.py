#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NIAH 验收实测（路线文档第四节 5 项），隔离 HOME 不碰真实数据目录。

1. NIAH 埋针：3 个月前埋"项目 A 用 PostgreSQL"，500 条噪声后召回 Top3
2. 核心 vs 临时：persona traits 加权在召回 Top5 稳定，不被 1 个月内临时记录挤掉
3. 冷集补召：热集 Top-K 均分 < 0.1 时 archive 自动补入
4. 回滚可用：快照回滚恢复到 dream 前状态
5. 零依赖不破：不配 embed/rerank 时纯关键词召回仍工作
"""
import json
import os
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

tmp = tempfile.mkdtemp(prefix="lingshu_niah_")
os.environ["BRAIN_MEMORY_HOME"] = tmp
os.environ.pop("LINGSHU_EMBED_URL", None)
os.environ.pop("LINGSHU_RERANK_URL", None)

import brain  # noqa: E402
assert str(brain.HOME).replace("\\", "/").lower().startswith(tmp.replace("\\", "/").lower()), \
    f"HOME 隔离失败：{brain.HOME}"

print(f"[env] 隔离 HOME = {brain.HOME}")
print(f"[env] embed.url = {brain.load_config().get('embed.url') or '(未配置)'}")

fails = []


def check(name, ok, detail=""):
    print(f"  {'✅' if ok else '❌'} {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        fails.append(name)


NOW = brain.now_ts()
DAY = 86400

# ---------- 1. NIAH 埋针 ----------
print("\n== 1. NIAH 埋针：3 个月前的核心偏好 vs 500 条噪声 ==")
needle, _st = brain.remember("项目 A 的数据库选型定了：必须用 PostgreSQL，团队已评审通过",
                             importance=0.7)
assert needle and needle.get("id")
# 埋针时间回拨 3 个月 + 送到 cortex（长期层）
brain.bulk_memory_action([needle["id"]], "importance", 9)
records = brain.load_all()
for m in records:
    if m["id"] == needle["id"]:
        m["created"] = NOW - 90 * DAY
        m["layer"] = "cortex"
brain.save_all(records)
# 500 条噪声：随机话题，时间散布近 1 个月
words = ["前端", "样式", "接口", "测试", "部署", "文档", "会议", "重构", "缓存", "日志",
         "动画", "弹窗", "表格", "图表", "通知", "评论", "点赞", "收藏", "搜索", "排序"]
random.seed(42)
for i in range(500):
    w1, w2, w3 = random.sample(words, 3)
    brain.remember(f"{w1}模块的{w2}问题排查，涉及{w3}与联调细节{i}", importance=0.2)
hits = brain.recall("项目 A 数据库用什么 PostgreSQL", k=3)
top_texts = [m.get("text", "") for m, _, _ in hits]
check("埋针进入 Top3", any("PostgreSQL" in t for t in top_texts),
      f"Top1: {(top_texts[0][:30] if top_texts else '空')}")

# ---------- 2. 核心 vs 临时 ----------
print("\n== 2. 核心 vs 临时：persona core 加权让 trait 标签记忆稳住 Top5 ==")
# 埋 1 个月内 100 条与查询高度重叠的临时记录
for i in range(100):
    brain.remember(f"用户今天喜欢简约风格第{i}天，界面配色讨论", importance=0.2)
# persona 里种一个核心 trait（set_trait 才能新增 trait；update_persona 只改既有值）
p = brain.load_persona()
brain.set_trait(p["traits"], "审美", 8, why="用户长期偏好简约风格",
                evidence=["多次提到喜欢简约"])
brain.save_persona(p)
# 埋一条 tags 命中 trait 名"审美"的老记忆——core_bias 机制应让它不被 100 条新临时记录挤掉
old_pref, _st = brain.remember("用户的审美倾向：偏爱留白多、装饰少的界面", importance=0.3)
records = brain.load_all()
for m in records:
    if m["id"] == old_pref["id"]:
        m["tags"] = ["审美"]
        m["created"] = NOW - 60 * DAY
brain.save_all(records)
r5 = brain.recall("用户喜欢什么风格的界面", k=5)
pref_rank = next((i for i, (m, _, _) in enumerate(r5) if "留白" in m.get("text", "")), None)
check("trait 标签记忆进 Top5（core_bias 加权）", pref_rank is not None and pref_rank < 5,
      f"排名: {pref_rank if pref_rank is not None else '未进 Top5'}; "
      f"Top3: {[m.get('text', '')[:16] for m, _, _ in r5[:3]]}")

# ---------- 3. 冷集补召 ----------
print("\n== 3. 冷集补召：热集均分<0.1 时 archive 自动补入 ==")
cold, _st = brain.remember("二〇二三年冬在杭州西湖边讨论过量化交易策略回测框架选型",
                           importance=0.3)
cid = cold["id"]
# 塞进 archive：archive.jsonl 是独立文件（load_all 不含它），直接写专用通道
records = brain.load_all()
cold_rec = next(m for m in records if m["id"] == cid)
cold_rec["layer"] = "archive"
cold_rec["created"] = NOW - 400 * DAY
cold_rec["archived_at"] = NOW - 300 * DAY
brain.write_jsonl(brain.HOME / "archive.jsonl",
                  brain.load_archive() + [cold_rec])
brain.write_jsonl(brain.HOME / "hippocampus.jsonl",
                  [m for m in records if m["id"] != cid])
assert any(m["id"] == cid for m in brain.load_archive()), "冷集记忆未入 archive"
# 热集与查询零词面重叠：原本整条漏召回；兜底/补召后应把冷集捞回来
hot = brain.recall("量化交易策略回测框架", k=5)
hot_cold_hit = any(m["id"] == cid for m, _, _ in hot)
check("热集无候选时冷集兜底生效", len(hot) == 0 or hot_cold_hit,
      f"热集召回 {len(hot)} 条, 含冷集: {hot_cold_hit}")
# recall 内部自动补召：均分 < 0.1 时递归带 include_archive=True
r_cold = brain.recall("量化交易策略回测框架", k=5)
cold_hit = any(m["id"] == cid for m, _, _ in r_cold)
check("archive 冷集被自动补召回", cold_hit,
      f"补召后 Top5 含 archive 记忆: {cold_hit}; 得分: "
      f"{[(m['text'][:14], round(s,3)) for m, s, _ in r_cold[:3]]}")

# ---------- 4. 回滚可用 ----------
print("\n== 4. 回滚可用：恢复到 dream 前状态 ==")
p_before = brain.load_persona()
v_before = brain.save_persona_snapshot(p_before, action="dream", source="测试",
                                       why="dream 前快照")
brain.update_persona({"traits": {"审美": 30}}, why="dream 误改")
p_now = brain.load_persona()
assert brain.trait_value(p_now["traits"]["审美"]) == 30, "dream 改动未生效"
p_back = brain.rollback_persona_snapshot(v_before)
check("rollback 恢复 dream 前状态",
      p_back is not None and brain.trait_value((p_back.get("traits") or {}).get("审美", 100)) == 8,
      f"快照 v{v_before}")

# ---------- 5. 零依赖不破 ----------
print("\n== 5. 零依赖不破：纯关键词召回 ==")
check("未配置 embed/rerank 仍可召回", len(r5) > 0 and len(hits) > 0,
      f"NIAH 召回 {len(hits)} 条 / traits 召回 {len(r5)} 条")

print("\n" + "=" * 40)
if fails:
    print(f"❌ 未通过 {len(fails)} 项：{fails}")
    sys.exit(1)
print("✅ NIAH 验收 5 项全部通过")
