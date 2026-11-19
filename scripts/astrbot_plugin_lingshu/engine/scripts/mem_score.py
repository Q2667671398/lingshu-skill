# -*- coding: utf-8 -*-
"""评分与保留度纯函数（v3.40 从 memory.py 拆出）。

依赖约定：
  - 只做纯计算：入参是文本/数字/记忆 dict，不读盘、不写盘、不依赖 HOME；
  - 时间与常量走 core（now_ts/HOUR/DAY/DEFAULT_STABILITY），不复制可变全局；
  - memory.py 末尾 `from mem_score import *` 保留 memory.xxx 调用面。
"""
from __future__ import annotations

import math
import re

from core import *  # noqa: F401,F403  （now_ts/HOUR/DAY/DEFAULT_STABILITY 等纯函数/常量）

__all__ = [
    "terms", "jaccard", "build_idf", "relevance",
    "_num", "heuristic_importance", "_imp_key_safe",
    "retention", "activation", "ACTIVATION_FORGET_THRESHOLD", "activation_forget",
    "_KIND_STABILITY_FACTOR", "_cosine_sim",
]


# ---------------------------------------------------------------- 文本与相似度

_CJK = re.compile(r"[\u4e00-\u9fff]+")
# 数字/编号（如 "版本0" vs "版本1"）必须保留为独立 term，否则不同数字的记忆会被误判为重复而合并覆盖
_WORD = re.compile(r"[a-z0-9]+")
_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")


def terms(text: str) -> set:
    """中文用字符二元组，英文/数字用词。构成模糊匹配的最小单元。"""
    t = (text or "").lower()
    out = set()
    for seg in _CJK.findall(t):
        if len(seg) == 1:
            out.add(seg)
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
    out.update(_WORD.findall(t))
    return out


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def build_idf(docs: list) -> dict:
    df = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    n = max(1, len(docs))
    return {t: math.log((n + 1) / (c + 0.5)) + 0.5 for t, c in df.items()}


def relevance(q_terms: set, d_terms: set, idf: dict) -> float:
    shared = q_terms & d_terms
    if not shared:
        return 0.0
    num = sum(idf.get(t, 0.5) for t in shared)
    den = math.sqrt(sum(idf.get(t, 0.5) for t in q_terms) * sum(idf.get(t, 0.5) for t in d_terms))
    return min(1.0, num / den) if den else 0.0


# ---------------------------------------------------------------- 数值与重要性打分

def _num(v, default: float = 0.0) -> float:
    """把可能是 None / 字符串 / 数字 / bool / NaN 的值安全转成 float；转不了用默认值。
    记忆库里手改、旧版本、坏字段都可能让数值变成 null 或字符串，硬 float() 会整条崩。"""
    if v is None or isinstance(v, bool):   # bool 是 int 子类，True 会变 1.0，按坏值回退
        return float(default)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float(default)
    # 模型可能产出 NaN/Infinity（JSON 里非法但 Python json 会放行），比较/运算会一路污染
    if math.isnan(f) or math.isinf(f):
        return float(default)
    return f


def heuristic_importance(text: str, kind: str, entities=None,
                         correction: bool = False, emotional: float = 0.0) -> float:
    """#42 多维重要性打分：写入时就回答"这条记忆有多重要"。

    维度（SCM multi-dimensional importance / Memory Bear activation 的轻量版）：
      kind 基线 → 命中关键词 → 文本长度 → 实体数量 → 情绪强度 → 是否 correction。
    打分只影响起步权重，后续回忆次数/做梦巩固仍会拉开差距。
    """
    base = {
        "identity": 0.95, "insight": 0.75, "preference": 0.70,
        "habit": 0.65, "emotion": 0.60, "fact": 0.58, "episode": 0.40,
        "mistake": 0.70, "relationship": 0.70,
    }.get(kind, 0.5)
    hot = ("记住", "重要", "永远", "一定", "绝不", "喜欢", "讨厌", "生日",
           "名字", "答应", "承诺", "第一次", "决定")
    if any(w in text for w in hot):
        base += 0.15
    if len(text) >= 60:
        base += 0.05
    # 实体维度：涉及的人/物越多，关系网络价值越高（上限 +0.10）
    n_ent = len(entities or [])
    if n_ent:
        base += min(0.10, 0.04 * n_ent)
    # 情绪维度：写入时情绪越激动记得越牢（调用方已算好的 arousal，0~1）
    if emotional > 0:
        base += min(0.12, 0.12 * _num(emotional, 0.0))
    # correction 维度：被纠正过的说法是失败闭环第一手证据，起步就该高
    if correction:
        base = max(base, 0.75)
    return max(0.05, min(1.0, round(base, 2)))


def _imp_key_safe(m: dict) -> float:
    """importance 安全取值：坏类型（None/list 等）沉底为 0，不让一条坏记忆炸掉画像。"""
    return _num(m.get("importance"), 0.0)


# ---------------------------------------------------------------- 保留度 / 唤醒值

def retention(m: dict, now: int | None = None) -> float:
    """艾宾浩斯遗忘曲线：R = exp(-Δt / S)"""
    now = now or now_ts()
    last = m.get("last_access")
    if last is None:
        last = m.get("created", now)
    dt = max(0.0, now - _num(last, now))
    stability = max(HOUR, _num(m.get("stability"), DEFAULT_STABILITY))
    # v3.01 遗忘可纠正（借鉴 fish-soul 艾宾浩斯）：mistake/relationship 这类
    # 成长型记忆默认更抗遗忘——稳定度乘一个大于 1 的系数（等效半衰期更长）。
    kind = str(m.get("kind") or "")
    stability *= _KIND_STABILITY_FACTOR.get(kind, 1.0)
    return math.exp(-dt / stability)


# 类别稳定度系数：>1 更抗遗忘（半衰期更长），mistake/relationship 是成长地基
_KIND_STABILITY_FACTOR = {"mistake": 1.5, "relationship": 1.5, "identity": 2.0, "preference": 1.2}


def activation(m: dict, now: int | None = None) -> float:
    """#43 唤醒值（ACT-R activation + Memory Bear 遗忘引擎）：一条记忆"此刻有多容易被想起"。

    activation = importance × R(艾宾浩斯保留度) × recency_bonus
      - importance：写入时打分的多维重要性（#42）
      - R：retention() 的指数衰减（stability 越大衰减越慢）
      - recency_bonus：最近 3 天内被召回过给小额加成（用进废退）
    取值 0~1，做梦/归档按它排序决定"谁先被压缩淡忘"，recall 命中则自然回升
    （last_access 更新 → R 回升）。纯计算不改库，只读调用零风险。
    """
    now = now or now_ts()
    imp = max(0.0, min(1.0, _num(m.get("importance"), 0.5)))
    R = retention(m, now)
    # 用进废退：3 天内想起过一次给 +0.08，封顶一次（连续想起靠 R 本身维持）
    idle = now - _num(m.get("last_access"), _num(m.get("created"), now))
    recency = 0.08 if idle <= 3 * DAY else 0.0
    return round(max(0.0, min(1.0, imp * R + recency)), 4)


# 唤醒值软处理阈值：做梦时 activation 低于此值的记忆降层/切断关联。
# 不是删除——dust 层可 revive 复活，被意外召回（recall 触碰）后 R 回升即自然"复活"。
ACTIVATION_FORGET_THRESHOLD = 0.06


def activation_forget(records: list, now: int | None = None,
                      threshold: float = ACTIVATION_FORGET_THRESHOLD) -> list:
    """#43 唤醒值遗忘引擎（软处理）：对 activation < 阈值的活跃层记忆做三步弱化——
    1) 降层到 dust；2) 切断 outgoing 关联边（别的记忆指向它的边不动，留复活线索）；
    3) 清 embedding（向量召回不再命中，等 revive 后重新生成）。
    不碰：pinned / correction / identity / 待审核 / 已经在 dust 的。
    返回被处理的记录列表（调用方决定是否落盘）。"""
    now = now or now_ts()
    hit = []
    by_id = {m.get("id"): m for m in records if m.get("id")}
    for m in records:
        if m.get("layer") not in ("hippo", "cortex") or m.get("pinned"):
            continue
        if m.get("correction") or m.get("kind") == "identity" or m.get("review") == "pending":
            continue
        if activation(m, now) < threshold:
            m["layer"] = "dust"
            m["forgotten_at"] = now
            m["detail"] = round(max(0.12, _num(m.get("detail"), 1) * 0.4), 2)
            m.pop("embedding", None)          # 向量降级：复活后 _embed_wanted 会重新调度
            for sid in (m.get("links") or []):
                src = by_id.get(sid)
                if src and src.get("links"):
                    src["links"] = [x for x in src["links"] if x != m["id"]]
            m["links"] = []
            hit.append(m)
    return hit


# ---------------------------------------------------------------- 向量相似度（纯标准库）

def _cosine_sim(a: list, b: list) -> float:
    """纯标准库余弦相似度：零向量/空向量/维度不一致都返回 0.0，不抛异常。

    从 memory._dense_recall 的内联算式抽出：对非零且同维的向量，结果与旧实现完全一致；
    零向量在旧实现里靠 `or 1.0` 兜底成 0.0，这里显式判断，便于单测覆盖边界。
    """
    n = len(a)
    if not n or n != len(b):
        return 0.0
    an = math.sqrt(sum(x * x for x in a)) or 1.0
    bn = math.sqrt(sum(x * x for x in b)) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (an * bn)
