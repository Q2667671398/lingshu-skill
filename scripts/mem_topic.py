# -*- coding: utf-8 -*-
"""话题栈（v3.69 从 memory.py 拆出）。

依赖约定（与 mem_score.py 同一套，务必遵守，否则循环导入）：
  - 只依赖 core 的名字（TOPIC_* 常量 / _locked / now_ts / read_jsonl /
    write_jsonl / log_event / ensure_home / HOUR / READONLY 等，core 已
    star-import）与标准库；**绝不 import memory**；
  - memory.py 末尾 `from mem_topic import *` 保留 memory.topic_xxx 调用面。

拆出理由：这一段是"快聊死的话题别再硬塞"的自洽逻辑（衰减、TTL、权重、
反馈），与记忆读写主链路无耦合。边界经依赖分析确认：块内自定义名字只有
本块的 6 个 topic_* 函数与 1 个 helper（_topic_check_writable），TOPIC_*
常量全在 core.py，全部随 `from core import *` 可见。

# v3.69 待合并：本文件写入后，从 memory.py 删除对应块（L2718-2866）并在
# 末尾加 star-import；用 behavior_baseline check 逐字验证行为不变。
"""
from __future__ import annotations

from core import *  # noqa: F401,F403

__all__ = [
    "load_topics", "topic_status", "topic_touch",
    "topic_feedback", "topic_drop", "topic_can_spawn",
    "_topic_check_writable",
]


# 会话话题栈：AI 聊天时按「跟随当前 > 唤起历史 > 自发新话题」决策，
# 栈里的权重决定一个旧话题还值不值得再提、以及是否到了该主动开新话题的时候。

def load_topics(now: float = None, scope: str | None = None) -> list:
    """读话题栈：指数衰减未重提话题、丢弃 TTL 过期项，按权重降序返回。

    scope=None 返回全部 scope 的记录；scope="" 只看默认（本机）栈，
    scope="channel@group:..." 只看对应通道的栈（B-08/C-03 通道隔离）。
    """
    now = now if now is not None else now_ts()
    if not TOPIC_FILE.exists():
        return []
    out = []
    for r in read_jsonl(TOPIC_FILE):
        try:
            ts = float(r.get("ts") or 0)
            w = float(r.get("weight", 0))
        except (TypeError, ValueError):
            continue
        if now - ts >= TOPIC_TTL:
            continue  # 7 天没重提，彻底退栈
        if scope is not None and str(r.get("scope", "")) != scope:
            continue
        # 自上次活跃以来每过一个衰减周期（12h）权重 ×TOPIC_DECAY
        periods = int((now - ts) / (12 * HOUR))
        w *= (TOPIC_DECAY ** periods)
        r = dict(r)
        r["weight"] = round(min(max(w, 0.0), 1.0), 3)
        out.append(r)
    out.sort(key=lambda x: -x["weight"])
    return out


def topic_status(scope: str = "") -> dict:
    rows = load_topics(scope=scope)
    return {
        "count": len(rows),
        "capacity": TOPIC_CAPACITY,
        "spawn_threshold": TOPIC_SPAWN_THRESHOLD,
        "scope": scope,
        "top": rows[0]["name"] if rows else None,
        "weak": [r["name"] for r in rows if r["weight"] < TOPIC_SPAWN_THRESHOLD],
        "items": rows,
    }


def _topic_check_writable() -> None:
    """B-09：写路径统一入口检查——只读模式下优雅报错，避免 _locked 在不存在的
    目录里创建锁文件失败报出含糊的 OSError。"""
    if READONLY:
        raise RuntimeError("只读模式（--ro）：话题栈不可修改")
    ensure_home()


def topic_touch(name: str, type_: str = "", weight: float = 0.8,
                why: str = "", delta: float = None, scope: str = "") -> list:
    """用户重提或 AI 接住某话题：拉高权重（默认 0.8，或按 delta 增量）。

    用户主动抛出新话题是正向信号，直接置高；AI 引出的自发话题
    初始权重偏低（weight=0.4），避免抢占用户的主线。
    scope：话题栈作用域（空串=本机默认栈；通道场景传 channel/group/user 组合串）。
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("话题名为空")
    # B-18：weight 与 delta 语义互斥（delta=增量、weight=直接置高），同传会失真
    if delta is not None and weight != 0.8:
        raise ValueError("weight 与 delta 互斥：请只传其一")
    _topic_check_writable()
    with _locked(TOPIC_FILE):
        # 读全量（含各 scope）再按 scope 过滤操作，写回时合并，互不挤占
        rows = load_topics()   # 已剔除 TTL 过期项（I-10 物理清理）
        sub = [r for r in rows if str(r.get("scope", "")) == scope]
        rows = [r for r in rows if str(r.get("scope", "")) != scope]
        for r in sub:
            if r["name"] == name:
                base = r["weight"]
                new = base + delta if delta is not None else weight
                r["weight"] = round(min(max(new, 0.0), 1.0), 3)
                r["ts"] = now_ts()
                r["turns"] = int(r.get("turns", 0)) + 1
                if scope:
                    r["scope"] = scope
                if type_:
                    r["type"] = type_
                if why:
                    r.setdefault("whys", []).append(why)
                break
        else:
            sub.insert(0, {"name": name, "weight": round(min(max(
                (0.4 if delta is not None else weight), 0.0), 1.0), 3),
                "ts": now_ts(), "turns": 1, "type": (type_ or "chat"),
                "scope": scope,
                "spawned_by_ai": delta is not None and weight is None,
                "whys": [why] if why else []})
        if len(sub) > TOPIC_CAPACITY:
            # E-04：被挤出栈尾的话题留痕，别无声消失
            evicted = [r["name"] for r in sub[TOPIC_CAPACITY:]]
            log_event("topic", f"话题被挤出栈（scope={scope or '默认'}）：{'、'.join(evicted)}",
                      evicted="; ".join(evicted))
        sub = sub[:TOPIC_CAPACITY]
        write_jsonl(TOPIC_FILE, rows + sub)
    return sub


def topic_feedback(name: str, positive: bool, why: str = "", scope: str = "") -> list:
    """兴趣反馈闭环：用户敷衍/转移话题 = 负反馈（权重 ×0.6）；
    用户展开多说 = 正反馈（权重 +0.15）。写回话题栈，后续减少用户不感兴趣的自发话题。"""
    _topic_check_writable()   # B-09：与 topic_touch 一致的只读/目录检查
    with _locked(TOPIC_FILE):
        rows = load_topics()
        sub = [r for r in rows if str(r.get("scope", "")) == scope]
        rows = [r for r in rows if str(r.get("scope", "")) != scope]
        for r in sub:
            if r["name"] == name:
                base = r["weight"]
                r["weight"] = round(min(max(base * 0.6 if not positive else base + 0.15, 0.0), 1.0), 3)
                r["ts"] = now_ts()
                if why:
                    r.setdefault("whys", []).append(("+" if positive else "-") + why)
                break
        write_jsonl(TOPIC_FILE, rows + sub)
    return sub


def topic_drop(name: str, scope: str = "") -> list:
    """把话题彻底移出栈（用户明确说不想聊了）。"""
    _topic_check_writable()   # B-09
    with _locked(TOPIC_FILE):
        rows = load_topics()
        sub = [r for r in rows if str(r.get("scope", "")) == scope and r["name"] != name]
        rows = [r for r in rows if not (str(r.get("scope", "")) == scope and r["name"] == name)]
        write_jsonl(TOPIC_FILE, rows + sub)
    return sub


def topic_can_spawn(emotion_blocked: bool = False, scope: str = "") -> dict:
    """自发话题准入判定（供注入包与 AI 参考，最终决定权在 AI 自己）。

    满足：存在权重低于阈值的话题（当前话题快聊死了）且情绪不拦。
    不满足的场景（倾诉/吐槽/认真提问/刚抛新话题/想结束）由 AI 按纪律自行判断。
    """
    st = topic_status(scope=scope)
    weak = st["weak"]
    return {
        "can_spawn": bool(weak) and not emotion_blocked,
        "weak_topics": weak,
        "emotion_blocked": emotion_blocked,
    }
