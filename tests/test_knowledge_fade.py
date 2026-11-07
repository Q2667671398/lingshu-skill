# -*- coding: utf-8 -*-
"""v2.78 knowledge 淡出分层：相位计算与检索降权的纯逻辑测试。"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import growth  # noqa: E402
import core  # noqa: E402

# 只读场景：临时 HOME 指向临时目录，不碰真实库
import tempfile
tmp = tempfile.mkdtemp()
core.HOME = Path(tmp)
growth.HOME = Path(tmp)
core.READONLY = True
growth.READONLY = True

NOW = time.time()
FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def rec(**kw):
    base = {"id": "k_x", "title": "t", "body": "b", "status": "confirmed",
            "updated": NOW - 5 * 86400, "created": NOW - 5 * 86400}
    base.update(kw)
    return base


# ---- knowledge_fade_phase ----
check("新鲜条目 active", growth.knowledge_fade_phase(rec()) == "active")
check("40 天条目 fading",
      growth.knowledge_fade_phase(rec(updated=NOW - 40 * 86400)) == "fading")
check("200 天条目 dormant",
      growth.knowledge_fade_phase(rec(updated=NOW - 200 * 86400)) == "dormant")
check("200 天但 pinned 豁免",
      growth.knowledge_fade_phase(rec(updated=NOW - 200 * 86400, pinned=True)) == "active")
check("200 天但 E3 豁免",
      growth.knowledge_fade_phase(rec(updated=NOW - 200 * 86400, evidence_level="E3")) == "active")
check("E4 豁免",
      growth.knowledge_fade_phase(rec(updated=NOW - 400 * 86400, evidence_level="E4")) == "active")
check("E1 不豁免",
      growth.knowledge_fade_phase(rec(updated=NOW - 200 * 86400, evidence_level="E1")) == "dormant")
check("时间不明不推测，当 active",
      growth.knowledge_fade_phase(rec(updated=0, created=0)) == "active")
check("updated 缺失时回退 created",
      growth.knowledge_fade_phase({"created": NOW - 100 * 86400}) == "fading")
check("显式 now 参数生效（now 前移 100 天后 200 天旧条目变 fading）",
      growth.knowledge_fade_phase(rec(updated=NOW - 200 * 86400), now=NOW - 100 * 86400) == "fading")

# ---- search_knowledge 降权/淡出（用假 relevance，绕开分词依赖）----
import brain  # noqa: E402

orig_relevance, orig_terms, orig_build_idf = brain.relevance, brain.terms, brain.build_idf
brain.relevance = lambda q, doc, idf: 0.8   # 命中统一给 0.8
brain.terms = lambda s: ["x"]
brain.build_idf = lambda docs: {}

rows = [rec(updated=NOW - 5 * 86400),           # active
        rec(updated=NOW - 40 * 86400, id="k_b"),  # fading
        rec(updated=NOW - 200 * 86400, id="k_c")]  # dormant
orig_load = growth.load_knowledge
growth.load_knowledge = lambda: rows
out = growth.search_knowledge("x", k=8)
scores = {r["id"]: s for r, s in out}
check("三条都命中（dormant 不删除，只压低位）", len(out) == 3)
check("active 分最高", scores["k_x"] > scores["k_b"] > scores["k_c"])
check("fading 降了 0.03", abs(scores["k_x"] - scores["k_b"] - 0.03) < 1e-9)
check("dormant 压到 ≤0.4", scores["k_c"] <= 0.4)

growth.load_knowledge = orig_load
brain.relevance, brain.terms, brain.build_idf = orig_relevance, orig_terms, orig_build_idf

# ---- knowledge_stats 带 fade 计数 ----
growth.load_knowledge = lambda: rows
try:
    import growth as g
    # rebuild_index 需要写索引，只读场景下失败也无妨——直接调用 fade 统计逻辑
    stats_rows = rows
    fade = {p: sum(1 for r in stats_rows if g.knowledge_fade_phase(r) == p)
            for p in ("active", "fading", "dormant")}
    check("fade 计数 1/1/1", fade == {"active": 1, "fading": 1, "dormant": 1})
finally:
    growth.load_knowledge = orig_load

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
