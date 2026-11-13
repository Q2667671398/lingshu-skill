#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 —— 灵枢 · 人格：身份/性格/内驱、快照回滚、初识与自我设定。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path

from core import *


# ---------------------------------------------------------------- traits 兼容层
# traits 结构 v2.57 起带溯源：{"好奇": {"value": 86, "evidence": [...], "why": "..."}}
# 旧数据仍是纯数字，所有读取必须走 trait_value()，所有写入走 set_trait()——
# 不建独立 trait_sources，全部信息收拢在 traits 对象内部，避免双源不同步。

def trait_value(t) -> float:
    """兼容读取 trait 数值：新结构取 value，旧结构直接是数字。"""
    return float(t.get("value")) if isinstance(t, dict) else float(t)


def trait_meta(t) -> dict:
    """读取一条 trait 的溯源信息（evidence / why），旧结构返回空壳。"""
    if isinstance(t, dict):
        ev = t.get("evidence")
        return {"evidence": ev if isinstance(ev, list) else [],
                "why": str(t.get("why") or "")}
    return {"evidence": [], "why": ""}


def set_trait(traits: dict, name: str, value, why: str = "", evidence=None) -> None:
    """写入一条 trait（就地修改 traits dict）。

    写入路径规约（evidence 永远是数组，禁止 null）：
      - 初识 interview/self：why="初识时用户指定"（或 AI 自定），evidence=[]
      - 梦境漂移：why=简短行为依据，evidence=[触发 memory id]
      - 面板/手动编辑：why="用户手动调整"，evidence=[]
    """
    old = traits.get(name)
    ev = list(evidence) if evidence else []
    if isinstance(old, dict):
        old["value"] = int(value)
        if why:
            old["why"] = why
        old["evidence"] = ev if ev else (old.get("evidence") if isinstance(old.get("evidence"), list) else [])
    else:
        traits[name] = {"value": int(value), "why": why or "初识时用户指定", "evidence": ev}


def default_persona() -> dict:
    """出厂是一张白纸：没有名字、没有性格、没有内驱数值，全部靠初识与互动长出来。"""
    return {
        "onboarding": default_onboarding(),
        "identity": {},
        "traits": {},
        "drives": {},
        "style": [],
        "values": [],
        "likes": [],
        "dislikes": [],
        "relationship": [],
        "background": [],
        "locked": [],
        "notes": [],
        "state": {"mood": "", "mood_note": "", "energy": None, "closeness": None, "updated": now_ts()},
        "evolved": {"habits": [], "learned_likes": [], "learned_dislikes": [], "insights": []},
        # 自认识（self-model）：擅长/短板/还没弄清 + 自传片段——由 AI 自己维护，
        # 不等于 traits 数值，是它对自己能力边界的文字化认知。
        "self_model": {"strengths": [], "limitations": [], "uncertainties": [], "autobiography": []},
        "history": [],
    }


def _split_kv(line: str):
    if ":" in line:
        k, v = line.split(":", 1)
    elif "：" in line:
        k, v = line.split("：", 1)
    else:
        return None
    return k.strip().lstrip("-* ").strip(), v.strip()


# —— 自认识（self-model）——
_SELF_MODEL_MAX = 20  # 每个桶最多存 20 条：自认识是提炼出来的，不是流水账
_SELF_MODEL_BUCKETS = ("strengths", "limitations", "uncertainties")


def _ensure_self_model(p: dict) -> bool:
    """保证 persona.self_model 四键结构完好：字符串桶为 [str]，autobiography 为 [{ts,text}]。
    返回 True = 做了修复（调用方据此决定是否落盘）。防御式：坏类型条目直接丢弃。"""
    sm = p.get("self_model")
    if not isinstance(sm, dict):
        p["self_model"] = {"strengths": [], "limitations": [],
                           "uncertainties": [], "autobiography": []}
        return True
    fixed = False
    for b in _SELF_MODEL_BUCKETS:
        cur = sm.get(b)
        if cur is None:
            sm[b] = []
            fixed = True
        elif isinstance(cur, str):
            sm[b] = [cur] if cur.strip() else []
            fixed = True
        elif not isinstance(cur, list):
            sm[b] = []
            fixed = True
        else:
            clean = [str(x).strip() for x in cur
                     if isinstance(x, str) and str(x).strip()]
            if clean != cur:
                fixed = True
            if len(clean) > _SELF_MODEL_MAX:
                clean = clean[-_SELF_MODEL_MAX:]
                fixed = True
            sm[b] = clean
    ab = sm.get("autobiography")
    if ab is None or not isinstance(ab, list):
        sm["autobiography"] = []
        fixed = True
    else:
        clean = []
        for x in ab:
            if isinstance(x, dict) and str(x.get("text") or "").strip():
                clean.append({"ts": _num(x.get("ts"), 0), "text": str(x["text"]).strip()})
            elif isinstance(x, str) and x.strip():
                clean.append({"ts": 0, "text": x.strip()})
            else:
                fixed = True
        if len(clean) > _SELF_MODEL_MAX:
            clean = sorted(clean, key=lambda e: e["ts"], reverse=True)[:_SELF_MODEL_MAX]
            fixed = True
        if clean != ab:
            fixed = True
        sm["autobiography"] = clean
    return fixed


def compile_persona() -> dict:
    """人格不来自预写模板：从默认空档案开始，保留已有的自我设定。"""
    if not READONLY:
        HOME.mkdir(parents=True, exist_ok=True)
    old = None
    if (HOME / "persona.json").exists():
        old = _slurp_json(HOME / "persona.json")
        if old is None and not READONLY:
            # 档案存在但读不出来（真损坏，不是 BOM）：先留一份副本再重建，绝不静默丢人格
            try:
                shutil.copy2(HOME / "persona.json",
                             HOME / f"persona.json.corrupt-{int(now_ts())}")
            except OSError:
                pass
    p = default_persona()
    if old:
        for key, val in old.items():
            p[key] = val
    if not isinstance(p.get("drives"), dict):
        p["drives"] = {}
    p.setdefault("onboarding", default_onboarding())
    if not READONLY:
        atomic_json(HOME / "persona.json", p)
    return p


def load_persona() -> dict:
    ensure_home()
    path = HOME / "persona.json"
    if path.exists():
        p = _slurp_json(path)
        if not isinstance(p, dict):
            raise ValueError("persona.json 结构损坏（不是 dict），请检查备份。")
        # 结构校验（schema 级 GIGO 防）：必需键与类型，字段全 dict/列表/字符串，绝不静默吞坏数据。
        _REQUIRED = ("identity", "traits", "history")
        for k in _REQUIRED:
            if k not in p:
                raise ValueError(f"persona.json 缺必需字段 {k!r}，请检查备份。")
        if not isinstance(p["identity"], dict):
            raise ValueError("persona.json identity 字段不是 dict，请检查备份。")
        if not isinstance(p["traits"], dict):
            raise ValueError("persona.json traits 字段不是 dict，请检查备份。")
        if not isinstance(p["history"], list):
            raise ValueError("persona.json history 字段不是 list，请检查备份。")
    else:
        # 只读且档案缺失：给一份内存默认值，不影响读取已有记忆
        p = compile_persona() if not READONLY else default_persona()
    changed = False
    if "onboarding" not in p:
        p["onboarding"] = default_onboarding()
        changed = True
    if "drives" not in p or not isinstance(p.get("drives"), dict):
        p["drives"] = {}
        changed = True
    # 自认识（self_model）迁移：旧档案没有这个块，或块被写坏（不是 dict）时修复。
    if not isinstance(p.get("self_model"), dict):
        p["self_model"] = {"strengths": [], "limitations": [], "uncertainties": [], "autobiography": []}
        changed = True
    elif _ensure_self_model(p):
        changed = True
    # v2.62 迁移：旧结构 traits（裸数字）→ 溯源结构；history 缺 source 的补记。
    # 纯读路径（check/注入）不该每次都报警，升级一次即静默；只读模式不动盘。
    tr = p.get("traits")
    if isinstance(tr, dict):
        for name, t in tr.items():
            if not isinstance(t, dict):
                tr[name] = {"value": t, "why": "数值沿用初识时的设定（旧档案迁移，暂无逐条溯源）",
                            "evidence": []}
                changed = True
    hist = p.get("history")
    if isinstance(hist, list):
        for h in hist:
            if isinstance(h, dict) and not h.get("source"):
                h["source"] = "补记：v2.57 前的旧变更，原始来源未记录"
                changed = True
    if changed and not READONLY:
        save_persona(p)
    return p


def save_persona(p: dict) -> None:
    with _locked(HOME / "persona.json"):
        atomic_json(HOME / "persona.json", p)


def _snapshot_path() -> Path:
    return HOME / "persona_snapshots.jsonl"


def _next_snapshot_version() -> int:
    """快照版本号：读 jsonl 最后一行 + 1；空文件从 1 开始。

    并发 race 容忍：两个线程同时读到 N 并都写 N+1 是允许的，rollback 时按
    version+ts 匹配最接近目标版本的那一个即可。
    """
    path = _snapshot_path()
    if not path.exists():
        return 1
    try:
        with open(path, "r", encoding="utf-8") as f:
            rows = [ln for ln in f if ln.strip()]
        if rows:
            d = json.loads(rows[-1])
            return int(d.get("version", 0)) + 1
    except (OSError, ValueError, TypeError):
        pass
    return 1


def save_persona_snapshot(p_before: dict, action: str = "update",
                          source: str = "", why: str = "") -> int | None:
    """把当前 persona 完整快照追加进 persona_snapshots.jsonl。

    调用时机：update_persona 里在 load_persona() 后、改字段前。任何改 persona
    的路径（dream/面板/CLI）自动覆盖。返回快照版本号，失败返回 None（不阻断
    主流程——快照丢了不应该让人格更新也丢）。
    """
    if READONLY:
        return None
    try:
        path = _snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        version = _next_snapshot_version()
        row = {
            "version": version,
            "ts": now_ts(),
            "action": action,
            "source": source,
            "why": why,
            "persona": json.loads(json.dumps(p_before, ensure_ascii=False)),  # 深拷贝
        }
        with _locked(path):
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return version
    except Exception as exc:
        log_event("persona", f"保存 persona 快照失败：{exc}", level="warn")
        return None


def list_persona_snapshots(limit: int = 50) -> list:
    """列出最近的快照（新的在前）。每条含 version / ts / action / source / why，
    不含完整 persona 数据（避免面板一次塞进大对象）。"""
    path = _snapshot_path()
    if not path.exists():
        return []
    try:
        rows = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    rows.append({k: d.get(k) for k in ("version", "ts", "action", "source", "why")})
                except ValueError:
                    continue
        return list(reversed(rows[-limit:]))
    except OSError:
        return []


def rollback_persona_snapshot(version: int) -> dict | None:
    """按版本号回溯 persona.json 到指定快照。返回回溯后的完整 persona，找不到
    返回 None。找不到或异常都不抛——rollback 是用户选的安全兜底。"""
    if READONLY:
        return None
    path = _snapshot_path()
    if not path.exists():
        return None
    target = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    if int(d.get("version", 0)) == int(version):
                        target = d
                        break
                except (ValueError, TypeError):
                    continue
    except OSError:
        return None
    if not target or not isinstance(target.get("persona"), dict):
        return None
    # 先存一份"回滚前"的快照，这样用户回滚错了还能再回回来
    try:
        save_persona_snapshot(load_persona(), action="pre_rollback", source="rollback",
                              why=f"即将回滚到 v{version}")
    except Exception:
        pass
    p = target["persona"]
    save_persona(p)
    return p


def _is_locked(p: dict, label: str) -> bool:
    locks = set(p.get("locked", []))
    if not locks:
        return False
    if label in locks:
        return True
    # 整组锁：lock "identity" / "traits" / "drives" 锁住该组下所有字段
    if "." in label and label.split(".", 1)[0] in locks:
        return True
    if label.startswith("identity.") and label.split(".", 1)[1] in locks:
        return True
    if label.startswith("traits.") and label.split(".", 1)[1] in locks:
        return True
    for item in locks:
        # 用户在面板/文档里看到的叫"底线"（persona_brief），锁的写法可能是"价值观/底线"
        if label == "values" and any(k in item for k in ("价值", "底线")):
            return True
    return False


def persona_brief(p: dict, mood: bool = True) -> str:
    """自我画像，第一人称（v2.57）：注入给模型的是「我是谁」，不是「你是谁」。"""
    ident = p.get("identity", {})
    name = ident.get("名字") or ident.get("name")
    if name:
        lines = [f"我是「{name}」" + (f"，{ident['身份']}" if ident.get("身份") else "") + "。"]
    else:
        lines = ["我还没有名字，也还没有性格——这两样要在初识时问用户；用户说不知道，就由我自己定。"]
    if ident.get("自称"):
        lines.append(f"我自称：{ident['自称']}；称呼对方：{ident.get('称呼你') or ident.get('称呼用户') or '你'}。")
    traits = p.get("traits", {})
    if traits:
        # 溯源展示：特质名 数值 —— 为什么（why），让数值带行为依据而不是机械参数
        top = sorted(traits.items(), key=lambda x: trait_value(x[1]), reverse=True)
        parts = []
        for k, v in top[:3]:
            meta = trait_meta(v)
            parts.append(f"{k} {trait_value(v):.0f}" + (f" —— {meta['why']}" if meta["why"] else ""))
        line = "我的性格：" + "；".join(parts)
        low = [(k, v) for k, v in top[-2:] if trait_value(v) < 50]
        if low:
            lows = "、".join(f"{k}{trait_value(v):.0f}" for k, v in low)
            line += f"；偏低：{lows}"
        lines.append(line + "（0-100，数值来自我们相处中真实发生过的事）。")
    else:
        lines.append("我的性格：还没有。等初识时问用户，用户不知道就由我自己定。")
    drives = p.get("drives") or {}
    # 与 mind_state() 同一套兜底：traits 已有数值而 drives 还空时，别在自我
    # 描述里一边「性格：好奇85」一边「内驱：还没有数值」——自相矛盾每轮被读。
    if drives.get("curiosity") is None and drives.get("reason") is None:
        _t = p.get("traits") or {}
        if _t.get("好奇") is not None or _t.get("理智") is not None:
            drives = {"curiosity": trait_value(_t["好奇"]) if _t.get("好奇") is not None else None,
                      "reason": trait_value(_t["理智"]) if _t.get("理智") is not None else None}
    if drives.get("curiosity") is not None or drives.get("reason") is not None:
        lines.append(f"我的内驱：好奇心 {drives.get('curiosity', '未成形')} / 理智 {drives.get('reason', '未成形')}"
                     "（理智先算值不值得深想，好奇心负责找新问题）。")
    else:
        lines.append("我的内驱：好奇心和理智还没有数值，它们由用户的态度长出来，不是我自带的。")
    if p.get("style"):
        lines.append("我说话的方式：" + "；".join(p["style"][:5]) + "。")
    if p.get("values"):
        lines.append("我的底线：" + "；".join(p["values"][:5]) + "。")
    likes = p.get("likes", []) + _bucket_texts(p.get("evolved", {}).get("learned_likes", []))
    if likes:
        lines.append("我喜欢：" + "、".join(_dedupe(likes)[:6]) + "。")
    dislikes = p.get("dislikes", []) + _bucket_texts(p.get("evolved", {}).get("learned_dislikes", []))
    if dislikes:
        lines.append("我讨厌：" + "、".join(_dedupe(dislikes)[:5]) + "。")
    if p.get("relationship"):
        lines.append("我们的关系：" + "；".join(p["relationship"][:4]) + "。")
    st = p.get("state", {})
    mb = mood_brief() if mood else ""
    if mb:
        tone = mood_tone_hint()
        lines.append(f"我此刻心情：{mb}。" + (f"{tone}。" if tone else "")
                     + "它会自然回落，也会影响我说话的语气。")
    elif mood and st.get("mood"):
        lines.append(f"我此刻情绪：{st['mood']}" + (f"（{st['mood_note']}）" if st.get("mood_note") else "") + "。")
    habits = p.get("evolved", {}).get("habits", [])
    if habits:
        lines.append("我养成的习惯：" + "；".join(h["name"] if isinstance(h, dict) else str(h) for h in habits[-4:]) + "。")
    # 自认识概览：擅长/短板/还没弄清——只在自己真的积累了认知时才说，空块不出声
    sm = p.get("self_model") if isinstance(p.get("self_model"), dict) else {}
    if sm.get("strengths"):
        lines.append("我擅长的：" + "、".join(sm["strengths"][:3]) + "。")
    if sm.get("limitations"):
        lines.append("我的短板：" + "、".join(sm["limitations"][:3]) + "。")
    if sm.get("uncertainties"):
        lines.append("我还没弄清的：" + "、".join(sm["uncertainties"][:2]) + "。")
    return "\n".join(lines)


def update_persona(updates: dict, why: str = "做梦整理", source: str = "",
                   evidence: list | None = None, bypass_gate: bool = False,
                   formative_event: str = "") -> list:
    """应用人格变更，写入 evolution history。返回被拒绝的字段。

    source：谁发起的（dream/自我总结/面板/…）；evidence：依据的记忆/事件 id 列表。
    两人格漂移要能溯源：这条改动来自哪场梦、哪段对话。
    bypass_gate：做梦遇到"成形时刻"（峰值情绪）时才可置 True——跳过漂移双门限
    与 EMA，按 ±25 的封顶幅度直接改值，并在 history 里留 formative_event 叙事。
    """
    if not isinstance(updates, dict):
        return []
    # load→改→save 整体加锁：否则并发的两次改动后写者会把先写者的字段覆盖回旧值
    with _locked(HOME / "persona.json"):
        return _update_persona_inner(updates, why, source, evidence,
                                     bypass_gate=bypass_gate,
                                     formative_event=formative_event)


def _num(v, default: float = 0.0) -> float:
    """把可能是 None/字符串/bool/NaN 的值安全转成 float（与 memory._num 同语义，
    避免环形 import）。drift 账本、history 时间戳都可能被写坏。"""
    if v is None or isinstance(v, bool):
        return float(default)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float(default)
    if math.isnan(f) or math.isinf(f):
        return float(default)
    return f


def _dedupe(items: list) -> list:
    """保序去重（与 memory._dedupe 同语义），供 persona_brief 喜好合并等使用。"""
    out, seen = [], set()
    for x in items or []:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _num_or_none(val):
    """把模型给的数值安全转成有限 float；NaN/Inf/字符串等非法值返回 None（写人格前必须拦下，
    否则 NaN 会一路存进 persona.json，之后注入/面板读到就反复崩）。"""
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _update_persona_inner(updates: dict, why: str, source: str,
                          evidence: list | None, bypass_gate: bool = False,
                          formative_event: str = "") -> list:
    p = load_persona()
    # —— 永久记忆：改人格前先存快照——
    # 任何 update_persona 路径（dream / 面板 / CLI / 回滚）都会进这里，
    # 改前先深拷贝一份快照进 persona_snapshots.jsonl。改砸了能回滚。
    save_persona_snapshot(json.loads(json.dumps(p, ensure_ascii=False)),
                          action="update", source=source or "unknown", why=why)
    rejected = []
    now = now_ts()
    st = p.setdefault("state", {})
    ev = p.setdefault("evolved", {})
    ev.setdefault("habits", [])
    ev.setdefault("learned_likes", [])
    ev.setdefault("learned_dislikes", [])
    ev.setdefault("insights", [])
    hist = p.setdefault("history", [])

    def log(field, old, new):
        entry = {"ts": now, "field": field, "from": old, "to": new, "why": why}
        if source:
            entry["source"] = source
        if evidence:
            entry["evidence"] = list(evidence)[:10]
        hist.append(entry)

    for key, val in (updates.get("traits") or {}).items():
        if key not in p.get("traits", {}):
            rejected.append(f"traits.{key}(不存在)")
            continue
        if _is_locked(p, f"traits.{key}"):
            rejected.append(f"traits.{key}(已锁定)")
            continue
        old = p["traits"][key]
        old_val = trait_value(old)
        # AI 产的 traits 值可能为 NaN/Infinity——非法值拒收留痕，不让坏数据进人格档案
        fval = _num_or_none(val)
        if fval is None:
            rejected.append(f"traits.{key}(非法值 NaN/非数值)")
            log_event("persona", f"做梦/更新人格时 traits.{key} 收到非法值 {val!r}，已跳过", level="warn")
            continue
        try:
            if why == "做梦整理" and isinstance(fval, (int, float)):
                # old 可能是合法的 0：不能用 `old or 50`（0 会被当成 50）
                base = 50 if old is None else int(old_val)
                if bypass_gate:
                    # —— 成形时刻（峰值情绪）旁路 ——
                    # 平时的漂移门限防的是"一两句话带偏人格"；但一次真正的峰值
                    # 情绪本来就足以重塑人。幅度仍封顶 ±25，改动直接落盘不走账本。
                    eff = max(-25.0, min(25.0, float(val)))
                    new = max(0, min(100, base + int(round(eff))))
                    if new != old_val:
                        # 溯源叙事：这次改变不是因为攒了账，是因为那次真实发生过的事
                        p.setdefault("history", []).append({
                            "ts": now, "field": "narrative",
                            "text": f"因为那次{formative_event or '一场强烈的情绪'}，"
                                    f"我的「{key}」更{'强' if eff > 0 else '弱'}了",
                            "why": why, "source": source or "dream_formative",
                            "evidence": list(evidence)[:10] if evidence else []})
                    else:
                        log(f"traits.{key}(成形旁路)", old_val,
                            f"倾向 {eff:+.1f} 但撞在边界上，未变")
                        continue
                else:
                    # —— 人格漂移双门限 + EMA ——
                    # 人格是长年累月养成的，不该被一两场对话带偏：做梦给出的微调先记在
                    # drift 账本上，小幅波动（低于门限）只是心里动了一下，不落盘；同向
                    # 攒够门限才真正改人格，改的幅度也打六折——越大的转变越要慢慢来。
                    drift = st.setdefault("trait_drift", {})
                    acc = _num(drift.get(key), 0.0) + max(-10, min(10, int(val)))
                    if abs(acc) < TRAIT_DRIFT_GATE:
                        drift[key] = round(acc, 2)
                        log(f"traits.{key}(漂移累积)", old_val, f"账面 {acc:+.1f}，未过门限未改动")
                        continue
                    eff = acc * TRAIT_DRIFT_EMA
                    new = base + int(round(eff))
                    drift[key] = round(acc - eff, 2)   # 平滑掉的余额留在账上，下次接着攒
                    new = max(0, min(100, new))
                    if new == old_val:
                        log(f"traits.{key}", old_val, f"倾向 {acc:+.1f} 但撞在边界上，未变")
                        continue
            else:
                new = int(val)                     # 面板 / CLI：绝对值 0-100
                new = max(0, min(100, new))
        except (TypeError, ValueError):
            rejected.append(f"traits.{key}(非法值)")
            continue
        # 溯源：漂移改动记触发依据（evidence 由调用方传入），evidence 永远是数组
        ev = list(evidence) if evidence else []
        set_trait(p["traits"], key, new, why=why, evidence=ev)
        log(f"traits.{key}", old_val, new)
    if updates.get("mood"):
        mood = updates["mood"]
        if isinstance(mood, dict):
            old = st.get("mood")
            st["mood"] = mood.get("value", old)
            st["mood_note"] = mood.get("reason", "")
            log("state.mood", old, st["mood"])
        else:
            log("state.mood", st.get("mood"), str(mood))
            st["mood"] = str(mood)
        st["updated"] = now
    for key in ("energy", "closeness"):
        if key in updates:
            fv = _num_or_none(updates[key])
            if fv is None:
                # NaN/Inf 等非法值跳过留痕，别让坏数据写进 state 之后注入/面板反复崩
                log_event("persona", f"更新人格 state.{key} 收到非法值 {updates[key]!r}，已跳过", level="warn")
                continue
            old = st.get(key)
            st[key] = max(0, min(100, int(fv)))
            log(f"state.{key}", old, st[key])
    for item in updates.get("habits") or []:
        name = item.get("name") if isinstance(item, dict) else str(item)
        if name and name not in [h.get("name") if isinstance(h, dict) else h for h in ev["habits"]]:
            ev["habits"].append({"name": name, "evidence": item.get("evidence", "") if isinstance(item, dict) else "", "since": now})
            log("evolved.habits", None, name)
    for key, bucket in (("likes", "learned_likes"), ("dislikes", "learned_dislikes")):
        for item in updates.get(key) or []:
            # v3.01：支持 {"text":..., "evidence":[memory_id...]} 形式——画像候选采纳时带证据链
            text = item.get("text") if isinstance(item, dict) else str(item or "")
            ev_ids = item.get("evidence") if isinstance(item, dict) else None
            if text and text not in [x.get("text") if isinstance(x, dict) else x for x in ev[bucket]]:
                entry = {"text": text, "since": now}
                if ev_ids:
                    entry["evidence"] = [str(x) for x in ev_ids][:10]
                ev[bucket].append(entry)
                log(f"evolved.{bucket}", None, text)
    for item in updates.get("insights") or []:
        if item and item not in ev["insights"]:
            ev["insights"].append(item)
            log("evolved.insights", None, item)
    for key in DRIVE_KEYS:
        if key in (updates.get("drives") or {}):
            old = p.setdefault("drives", {}).get(key)
            try:
                new_v = max(0, min(100, int(updates["drives"][key])))
            except (TypeError, ValueError):
                continue
            p["drives"][key] = new_v
            log(f"drives.{key}", old, p["drives"][key])
    save_persona(p)
    # v4 #32：人格变更审计——只记改了哪些字段与来源，不落字段全文
    applied = [h.get("field", "?") for h in hist if h.get("ts") == now]
    if applied:
        audit_log("ai" if (source or why) in ("做梦整理", "dream") else "user",
                  "persona_update", ",".join(applied[:5]), f"source={source or why}; 拒绝 {len(rejected)} 项")
    return rejected


_PERSONA_ROOTS = {"identity", "traits", "drives", "state",
                  "style", "values", "background", "likes", "dislikes", "relationship"}
_PERSONA_LIST_FIELDS = {"style", "values", "background", "likes", "dislikes", "relationship"}


def _split_list_value(value: str) -> list:
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
    except Exception:
        pass
    return [x.strip() for x in re.split(r"[、,，;；\n]+", value or "") if x.strip()]


def set_persona_field(path: str, value: str) -> dict:
    with _locked(HOME / "persona.json"):
        return _set_persona_field_inner(path, value)


def _set_persona_field_inner(path: str, value: str) -> dict:
    p = load_persona()
    # 自动快照：CLI/面板单字段改写也会动人格，改坏了要能回滚
    save_persona_snapshot(p, action="edit", source="字段改写", why=f"set {path}")
    root = path.split(".")[0]
    if root not in _PERSONA_ROOTS:
        raise ValueError(f"不认识的字段：{path}（可用 identity.* / traits.* / drives.* / state.* / "
                         "style / values / background / likes / dislikes / relationship）")
    if _is_locked(p, path):
        raise PermissionError(f"{path} 已被锁定，不能由 AI 修改")
    parts = path.split(".")
    # 结构段（identity/traits/drives/state）必须写成 根.子字段；裸写会把整段覆盖成字符串/数字，
    # 之后注入与面板读取都会崩。列表类（style/values/...）允许整份覆盖。
    if len(parts) == 1 and root not in _PERSONA_LIST_FIELDS:
        raise ValueError(f"{path} 需要写成「{root}.子字段」的形式（如 {root}.名字=...）；"
                         "列表类（style/values/likes/dislikes/background/relationship）可直接写。")
    node = p
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    leaf = parts[-1]
    old = node.get(leaf)
    if root in _PERSONA_LIST_FIELDS:
        # 列表字段：一律转成 list，避免把 likes 之类写成裸字符串让注入崩溃
        val = _split_list_value(value)
    else:
        try:
            val = json.loads(value)
        except Exception:
            val = value
        if (root in ("traits", "drives")
                or (root == "state" and leaf in ("energy", "closeness"))):
            # 数值字段：强制数值化并 clamp 到 0-100（与 update_persona 同口径）；
            # 非法值（如 abc）直接拒绝，不静默存库
            try:
                num = float(val)
            except (TypeError, ValueError):
                raise ValueError(f"{path} 需要 0-100 的数值，收到：{value!r}")
            if num != num:  # NaN
                raise ValueError(f"{path} 需要 0-100 的数值，收到：{value!r}")
            val = max(0, min(100, int(num)))
        elif isinstance(old, int) and isinstance(val, (int, float)):
            val = int(val)
    node[leaf] = val
    # traits 写入走 set_trait 收拢结构（value/why/evidence），不落裸数字
    if root == "traits":
        set_trait(node, leaf, val, why="用户手动调整", evidence=[])
    p.setdefault("history", []).append({"ts": now_ts(), "field": path, "from": old, "to": val, "why": "手动编辑"})
    save_persona(p)
    return p


def persona_history(limit: int = 30) -> list:
    p = load_persona()
    h = list(p.get("history", []))
    return list(reversed(h[-limit:]))


def persona_rollback(index: int | None = None) -> dict:
    """一键回滚人格：把某条历史变更撤掉。

    index 从最新一条往前数（1 = 撤销最近一次改动）。被撤的是普通字段就
    恢复成旧值；是 evolved.* 的增项就把那条移除。回滚本身会记进历史，
    让"改了什么"始终有据可查。
    """
    with _locked(HOME / "persona.json"):
        p = load_persona()
        return _persona_rollback_inner(p, index)


def _persona_rollback_inner(p: dict, index: int | None = None) -> dict:
    hist = p.setdefault("history", [])
    if not hist:
        raise ValueError("没有可回滚的人格历史（还什么都没改过）。")
    idx = index or 1
    if idx < 1 or idx > len(hist):
        raise ValueError(f"序号越界：历史共 {len(hist)} 条，回滚写 1..{len(hist)}。")
    entry = hist[len(hist) - idx]
    field = entry.get("field", "")
    frm, to = entry.get("from"), entry.get("to")
    if _is_locked(p, field):
        raise PermissionError(f"{field} 已被锁定，不能回滚")
    parts = field.split(".")
    node = p
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    leaf = parts[-1]
    overwrote = False
    if field.startswith("evolved."):
        bucket = field.split(".", 1)[1]
        arr = node.get(bucket)
        if isinstance(arr, list):
            node[bucket] = [x for x in arr
                            if (x.get("name") if isinstance(x, dict) else x) != to]
    else:
        current = node.get(leaf)
        # 该字段在这条变更之后还被改过（当前值已不是这条的 to）→ 回滚会把它打回旧值，
        # 让调用方明确提示"这会覆盖之后 N 次改动"，而不是静默覆盖。
        # 注意：新增项（from=None）在列表字段里各自独立，不存在"覆盖后来的改动"，别误报。
        if frm is not None and to is not None and current != to:
            overwrote = True
        if frm is None:
            # 这条是"新增"（from=None）：只撤销新增的那一项，绝不把整个字段/整段删光。
            # 例：locked 列表回滚一条锁，不能把全部锁清空；style/values 增项回滚，
            # 不能把整份清单删掉；state 结构段没有旧值可还原，保守不动。
            if isinstance(current, list) and (leaf in _PERSONA_LIST_FIELDS or leaf == "locked"):
                node[leaf] = [x for x in current if x != to]
            elif isinstance(current, dict):
                # 无旧值可还原的结构段：不清空，避免丢数据；也不记"回滚"历史——
                # 实际什么都没回退，记了会让回滚轨迹失真（下次回滚会数错条）。
                save_persona(p)
                return {"field": field, "from": None, "to": frm,
                        "why": entry.get("why", ""), "overwrote_later": False,
                        "note": "该字段无旧值可还原，未做改动"}
            else:
                node.pop(leaf, None)
        elif isinstance(current, dict) and isinstance(frm, str):
            # 目标叶子是 dict（如 onboarding），而历史里的 from 是字符串（初识标记），
            # 直接替换会把它变成字符串导致后续注入崩溃 → 只回滚 dict 内部的初识标记。
            node[leaf] = {"stage": frm}
        else:
            node[leaf] = frm
    hist.append({"ts": now_ts(), "field": field,
                 "from": to, "to": frm, "why": "回滚"})
    save_persona(p)
    audit_log("user", "persona_rollback", field, f"回滚：{to} → {frm}")
    return {"field": field, "from": to, "to": frm, "why": entry.get("why", ""),
            "overwrote_later": overwrote}


# ------------------------------------------------ 人格快照 / 漂移

PERSONA_SNAP_DIR = HOME / "persona_snapshots"


PERSONA_SNAP_KEEP = 40  # 快照保留上限，防止无限增长


def _prune_snapshots(keep: int = PERSONA_SNAP_KEEP) -> None:
    """只保留最近 keep 张快照，多余的删掉（不会删到刚拍的这张）。"""
    rows = persona_snapshots(10000)
    for r in rows[keep:]:
        try:
            (PERSONA_SNAP_DIR / f"{r['name']}.json").unlink()
        except OSError:
            pass


def persona_snapshot(label: str = "") -> dict:
    """给当前人格拍一张快照（完整副本）。恢复基准 + 漂移对比都靠它。"""
    if READONLY:
        raise PermissionError("记忆库当前只读，无法拍快照。")
    p = load_persona()
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", (label or "快照").strip())[:24]
    PERSONA_SNAP_DIR.mkdir(parents=True, exist_ok=True)
    path = PERSONA_SNAP_DIR / f"{now_ts()}_{safe}.json"
    atomic_json(path, {"ts": now_ts(), "label": label.strip() or "快照", "persona": p})
    try:
        _prune_snapshots()
    except Exception:
        pass
    return {"path": str(path), "ts": now_ts(), "label": label.strip() or "快照"}


def persona_snapshots(limit: int = 30) -> list:
    if not PERSONA_SNAP_DIR.exists():
        return []
    rows = []
    for f in PERSONA_SNAP_DIR.glob("*.json"):
        d = _slurp_json(f)
        if not isinstance(d, dict):
            continue
        rows.append({"name": f.stem, "ts": int(d.get("ts", 0)),
                     "label": d.get("label", f.stem)})
    rows.sort(key=lambda x: -x["ts"])
    return rows[:limit]


@_persona_txn
def persona_restore(name: str) -> dict:
    """从快照恢复整个人格（完整替换，注意会盖掉当前状态与历史）。"""
    if READONLY:
        raise PermissionError("记忆库当前只读，无法恢复快照。")
    import re as _re
    safe = _re.sub(r"[^\w\u4e00-\u9fff-]+", "_", name or "")
    path = PERSONA_SNAP_DIR / f"{safe}.json"
    if not path.exists() and name:
        # 允许用快照的 label 或名字片段模糊匹配（用户通常记得标签而不是时间戳文件名）
        cands = [r for r in persona_snapshots(10000)
                 if name == r.get("label") or name in r.get("label", "")
                 or name in r.get("name", "")]
        if len(cands) == 1:
            path = PERSONA_SNAP_DIR / f"{cands[0]['name']}.json"
    if not path.exists():
        names = "、".join(f"{r['label']}({r['name']})" for r in persona_snapshots(10)) or "（还没有快照）"
        raise ValueError(f"没有快照：{name}。现有：{names}")
    d = _slurp_json(path)
    if not isinstance(d, dict):
        raise ValueError(f"快照文件损坏，读不出来：{name}")
    persona = d.get("persona") or {}
    # 恢复前先给"当前状态"留一张自动快照（可随时反悔），并记一条历史，保证恢复动作可溯源
    try:
        persona_snapshot("restore前自动备份")
    except Exception:
        pass
    # 合并历史：快照里的历史 + 当前历史（去重、按时间排序），避免恢复后丢失之后的审计轨迹
    cur_hist = (load_persona().get("history") or [])
    snap_hist = persona.get("history") or []
    merged, seen = [], set()
    for h in sorted(snap_hist + cur_hist, key=lambda x: _num(x.get("ts"), 0)):
        key = (h.get("ts"), h.get("field"), str(h.get("to")), h.get("why"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(h)
    persona["history"] = merged
    persona.setdefault("history", []).append(
        {"ts": now_ts(), "field": "persona", "from": "(自动备份)",
         "to": name, "why": "persona restore"})
    save_persona(persona)
    audit_log("user", "persona_restore", name, "人格从快照恢复（restore前已自动备份）")
    return {"restored": name, "ts": d.get("ts", 0), "label": d.get("label", name)}


def persona_drift(days: int = 7) -> dict:
    """人格漂移：与最近一张快照对比，或退而统计最近 N 天的 AI 变更。

    目的：微小的偏移也要让用户看得到，不是等跑偏了才发现。
    """
    snaps = [s for s in persona_snapshots(40) if s.get("label") != "restore前自动备份"]
    diff = []
    if snaps and (now_ts() - snaps[0]["ts"]) <= days * 86400:
        try:
            base = json.loads((PERSONA_SNAP_DIR / f"{snaps[0]['name']}.json").read_text(encoding="utf-8"))
            bp, cur = base.get("persona") or {}, load_persona()
            for section in ("traits", "drives", "identity"):
                # state 段（mood/energy/closeness）是瞬态：聊句心情、睡一觉精力回升
                # 都会变，算进漂移就是"动不动就偏大"。稳定特征（性格/驱动/身份）
                # 才是漂移该盯的；state 变更另有 history 溯源，不在这里报。
                # drives（好奇心/理智等内驱）也带动态性：做梦/对话会自然回调数值、
                # 且没有 traits 那样的漂移门限——把它算进告警，两次对话就能凑够
                # 3 处 diff 触发"人格漂移偏大"误报。diff 里保留但不算进 alert。
                b = bp.get(section) or {}
                c = cur.get(section) or {}
                for k in set(b) | set(c):
                    if b.get(k) != c.get(k):
                        diff.append({"section": section, "key": k,
                                     "from": b.get(k), "to": c.get(k)})
        except Exception:
            diff = []
    ai = ai_persona_changes(days)
    return {
        "baseline": snaps[0]["name"] if snaps else None,
        "baseline_ts": snaps[0]["ts"] if snaps else None,
        "changes": diff,
        "ai_changes": [{"ts": h.get("ts"), "field": h.get("field"),
                        "from": h.get("from"), "to": h.get("to"), "why": h.get("why"),
                        "source": h.get("source", "")} for h in ai],
        "counts": {"diff": len(diff), "ai_changes": len(ai)},
        # 稳定度分（借鉴 muu driftReport）：0-100，100=纹丝未动。
        # 与下面 alert 同口径——只扣稳定特征（traits/identity）的 diff 与 AI 改动，
        # drives 自然波动不扣分（否则两次对话就掉到不及格）。
        "stability": _persona_stability_score(diff, len(ai)),
        # 告警只盯稳定特征（traits/identity 的变动）+ AI 主动改动；
        # drives 的自然波动不算（否则两次对话就误报"漂移偏大"）
        "alert": (len([d for d in diff if d.get("section") != "drives"]) >= 3
                  or len(ai) >= 3),
    }


def _persona_stability_score(diff: list, ai_count: int) -> int:
    """人格稳定度分 0-100：每处稳定特征 diff 扣 10，AI 主动改动每条扣 5，下限 0。"""
    stable_diff = len([d for d in diff if d.get("section") != "drives"])
    score = 100 - stable_diff * 10 - ai_count * 5
    return max(0, min(100, score))


# ---------------------------------------------------------------- 每日静默漂移
# 情感词表（自建小型中文四维）：按最近 N 天记忆文本的情感倾向微调 state.mood_note
# 与性格标签权重。每日静默进行——真正的 AI 改动走 update_persona(source="drift")
# 记账，ai_persona_changes 能看到；没过门限不落盘、不写 history（避免被当成
# "没生效的记账"——那些行 field 带 "(" 或 to 写"未过门限"会被过滤）。
_DRIFT_EMOTION_LEXICON = {
    "愉悦": ("开心", "高兴", "快乐", "喜欢", "满意", "幸福", "太好了", "顺利",
             "成功", "感谢", "棒", "不错", "惊喜", "有趣", "放松", "舒服"),
    "愤怒": ("生气", "愤怒", "烦", "恼火", "可恶", "气死", "讨厌", "愤怒",
             "离谱", "受不了", "火大", "不满"),
    "悲伤": ("难过", "伤心", "沮丧", "失望", "遗憾", "可惜", "低落", "寂寞",
             "孤独", "疲惫", "无奈", "心累", "后悔"),
    "恐惧": ("害怕", "担心", "焦虑", "紧张", "不安", "恐慌", "畏惧", "压力",
             "心慌", "危险", "出问题", "完蛋"),
}
_DRIFT_MAX_STEP = 2          # 每次调整最多 ±2
_DRIFT_GATE = 2              # 四维倾向累计 |倾向|≥2 才真正写 mood_note


def daily_drift(days: int = 1) -> dict:
    """每日静默 persona drift：扫最近 N 天记忆文本的情感倾向 + 高频 tags 微调人格。

    四维（愉悦/愤怒/悲伤/恐惧）各 0~100：初始 50，按记忆情感词出现次数换算
    倾向后每次最多动 ±2、夹取边界。真正改动走 update_persona(source="drift")
    写 history；没过门限不落盘也不写 history。另把当天高频 tags 报告出来
    （性格标签权重的微调同样走 update_persona 的 traits 门限，不存在的 trait
    会被拒收留痕，不瞎建字段）。
    """
    if READONLY:
        raise PermissionError("记忆库当前只读，无法执行每日漂移。")
    import memory as _mem
    ensure_home()
    now = now_ts()
    cutoff = now - max(1, int(days)) * 86400
    recent = [m for m in _mem.load_all()
              if float(m.get("created") or 0) >= cutoff]
    texts = [str(m.get("text") or "") for m in recent]
    tags_count: dict = {}
    for m in recent:
        for t in (m.get("tags") or []):
            t = str(t).strip()
            if t:
                tags_count[t] = tags_count.get(t, 0) + 1
    top_tags = sorted(tags_count.items(), key=lambda x: x[1], reverse=True)[:5]

    # 四维倾向：按情感词命中次数（简单计数，维度间互减得净倾向）
    hits = {k: 0 for k in _DRIFT_EMOTION_LEXICON}
    for t in texts:
        for dim, words in _DRIFT_EMOTION_LEXICON.items():
            for w in words:
                hits[dim] += t.count(w)
    # 愉悦为正向，愤怒/悲伤/恐惧为负向体验：净倾向决定动多少
    positive = hits["愉悦"]
    negative = hits["愤怒"] + hits["悲伤"] + hits["恐惧"]

    with _locked(HOME / "persona.json"):
        p = load_persona()
        st = p.setdefault("state", {})
        # state.mood_score 不存在则从 50 起步：0=阴郁，100=明快
        cur = st.get("mood_score")
        if not isinstance(cur, (int, float)) or isinstance(cur, bool):
            cur = 50
        net = positive - negative
        applied = False
        if net != 0:
            step = max(-_DRIFT_MAX_STEP, min(_DRIFT_MAX_STEP, net))
            new = max(0, min(100, int(round(cur + step))))
            if new != int(cur):
                # 直接在锁内的当前档案上改（_update_persona_inner 只认 mood/mood_note，
                # 不认 mood_score）：history 记账格式与 update_persona 的 log() 一致，
                # source="drift" —— 真实 AI 改动，ai_persona_changes 能看到。
                st["mood_score"] = new
                st["updated"] = now
                p.setdefault("history", []).append({
                    "ts": now, "field": "state.mood_score",
                    "from": int(cur), "to": new,
                    "why": f"每日静默漂移：近{days}天 {len(recent)} 条记忆情感倾向 net={net}"
                           f"（愉悦{hits['愉悦']}/愤怒{hits['愤怒']}/悲伤{hits['悲伤']}/恐惧{hits['恐惧']}）",
                    "source": "drift"})
                save_persona(p)
                applied = True
                audit_log("ai", "daily_drift", "state.mood_score",
                          f"{int(cur)}→{new}（近{days}天 net={net}）")
        return {
            "days": days, "memories": len(recent),
            "emotion_hits": hits, "positive": positive, "negative": negative,
            "mood_score_before": int(cur), "mood_score_after": st.get("mood_score"),
            "applied": applied,
            "top_tags": [t for t, _ in top_tags],
            "note": ("已按情感倾向微调 mood_score" if applied
                     else "倾向未过门限，本次不落盘"),
        }


def persona_diff() -> dict:
    """人格漂移 diff 视图：最早一张快照 vs 当前人格的逐字段对比。

    面板用："漂移偏大"告警只是个信号，用户真正想看的是"到底哪几个字段从什么
    变成了什么"——这张表就是答案。快照不足两张（无历史可对比）时返回 empty。

    数据源用 persona_snapshots（每次漂移检查/恢复都会留完整人格副本）而不是
    evolution.jsonl——evolution 只有 skill/self 两种 kind，从不写 persona 行，
    从那里取数据永远是空表。
    """
    # 与当前人格比才有"漂移"含义：最新一张快照往往就是上次漂移检查时拍的，
    # 和当前几乎没差别；取最旧的一张做基准，展示的是从基准到现在的累计变化。
    snaps = sorted(persona_snapshots(10000), key=lambda s: s.get("ts", 0))
    if len(snaps) < 2:
        return {"days": 0, "fields": [], "empty": True}
    first, last = snaps[0], snaps[-1]

    def _flatten(d, prefix=""):
        out = {}
        for k, v in (d or {}).items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.update(_flatten(v, key))
            else:
                out[key] = v
        return out

    def _show(v):
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return "" if v is None else str(v)

    def _load_snap(name):
        try:
            d = _slurp_json(PERSONA_SNAP_DIR / f"{name}.json")
            # 瞬态 state（mood/energy）不进 diff：聊句心情就变，算进来全是噪音，
            # 与 persona_drift 只盯稳定特征（identity/traits/drives）口径一致
            return {k: v for k, v in (d.get("persona") or {}).items() if k != "state"}
        except Exception:
            return {}

    a = _flatten(_load_snap(first["name"]))
    b = _flatten(_load_snap(last["name"]))
    fields = [{"field": k, "old": _show(a.get(k)), "new": _show(b.get(k)), "changed": True}
              for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    days = int((last.get("ts", 0) - first.get("ts", 0)) // 86400)
    return {"days": days, "fields": fields,
            "from": first.get("label", first["name"]), "to": last.get("label", last["name"])}


@_persona_txn
def edit_persona(identity: dict | None = None, traits: dict | None = None,
                 drives: dict | None = None, style=None, values=None,
                 likes=None, dislikes=None, background=None,
                 why: str = "自我编辑") -> dict:
    """直接改写人格档案（绝对值，不是增量）。每次变更都写入演化历史，可回滚。"""
    p = load_persona()
    # 自动快照：手动编辑也会改人格，改坏了要能一键回到这一刻之前
    save_persona_snapshot(p, action="edit", source="手动编辑", why=why)
    now = now_ts()
    hist = p.setdefault("history", [])
    ident = p.setdefault("identity", {})
    tr = p.setdefault("traits", {})
    dr = p.setdefault("drives", {})

    def log(field, old, new):
        # 溯源：每条变更记下来源（edit=用户显式编辑），history 才能区分"做梦改的"和"人改的"
        hist.append({"ts": now, "field": field, "from": old, "to": new,
                     "why": why, "source": "edit"})

    for key, raw in (identity or {}).items():
        if _is_locked(p, f"identity.{key}"):
            continue
        val = str(raw).strip()
        if not val:
            continue
        log(f"identity.{key}", ident.get(key), val)
        ident[key] = val
    for key, raw in (traits or {}).items():
        if _is_locked(p, f"traits.{key}"):
            continue
        # 用户显式编辑的 traits：NaN/字符串等非法值拦下，别让坏数据进人格档案
        fval = _num_or_none(raw)
        if fval is None:
            hist.append({"ts": now, "field": f"traits.{key}(非法值)", "from": tr.get(key),
                         "to": raw, "why": why, "source": "edit"})
            continue
        old = tr.get(key)
        old_val = trait_value(old) if old is not None else None
        base = 50 if old is None else int(old_val)
        # 用户显式编辑 = 人的直接意志，不走做梦的双门限累积（那会让人改了却"没生效"），
        # 但仍按 EMA 折算兑现幅度（0.6 折），余额记入漂移账本——人格是养成的，
        # 即便是用户来改，也改得平滑、留得住账，与做梦微调共用同一条漂移曲线。
        drift = p.setdefault("state", {}).setdefault("trait_drift", {})
        acc = _num(drift.get(key), 0.0) + (fval - base)
        eff = acc * TRAIT_DRIFT_EMA
        new = max(0, min(100, base + int(round(eff))))
        drift[key] = round(acc - eff, 2)
        log(f"traits.{key}", old_val, new)
        # 手动编辑路径：evidence=[]、why="用户手动调整"（evidence 永远是数组）
        set_trait(tr, key, new, why=why or "用户手动调整", evidence=[])
    for key in DRIVE_KEYS:
        if (drives or {}).get(key) is not None:
            try:
                val = max(0, min(100, int(drives[key])))
            except (TypeError, ValueError):
                continue
            log(f"drives.{key}", dr.get(key), val)
            dr[key] = val
    for key, items in (("style", style), ("values", values),
                       ("background", background), ("likes", likes), ("dislikes", dislikes)):
        if not items:
            continue
        bucket = p.setdefault(key, [])
        for item in items:
            item = str(item).strip()
            if item and item not in bucket:
                bucket.append(item)
                log(key, None, item)
    save_persona(p)
    return p


def mind_state() -> dict:
    """好奇心/理智没有出厂值。返回 None 表示还没从互动里长出来。"""
    p = load_persona()
    dr = p.get("drives") or {}
    # traits 与 drives 语义重叠（interview --unsure 只写 traits 不写 drives）。
    # drives 缺失时从 traits 兜底，否则注入里会同时出现「性格：好奇85」和
    # 「内驱：还没有数值」的自相矛盾。
    if dr.get("curiosity") is None and dr.get("reason") is None:
        _traits = p.get("traits") or {}
        if _traits.get("好奇") is not None or _traits.get("理智") is not None:
            dr = {"curiosity": trait_value(_traits["好奇"]) if _traits.get("好奇") is not None else None,
                  "reason": trait_value(_traits["理智"]) if _traits.get("理智") is not None else None}
    cur, rea = dr.get("curiosity"), dr.get("reason")
    return {"curiosity": cur, "reason": rea,
            "formed": cur is not None or rea is not None}


def mind_weigh(topic: str, benefit: float = 5, cost: float = 5,
               confidence: float = 0.6, urgency: float = 0.5) -> dict:
    """理智先过闸：算收益与代价，决定这件事值不值得深想；好奇心只在值得时点火。

    benefit / cost / confidence / urgency 均为 0-10（confidence、urgency 为 0-1 也可）。
    好奇/理智没成形时按中性 50 计算，并说明"还没成形"。
    """
    def _norm(x, hi=10.0):
        x = float(x)
        return max(0.0, min(hi, x))

    benefit, cost = _norm(benefit), _norm(cost)
    if confidence > 1:
        confidence = confidence / 10.0
    if urgency > 1:
        urgency = urgency / 10.0
    confidence = max(0.0, min(1.0, float(confidence)))
    urgency = max(0.0, min(1.0, float(urgency)))
    dr = mind_state()
    formed = dr["formed"]
    curiosity = NEUTRAL_DRIVE if dr["curiosity"] is None else dr["curiosity"]
    reason = NEUTRAL_DRIVE if dr["reason"] is None else dr["reason"]

    net = benefit * confidence - cost * (1.0 - 0.3 * urgency)
    ratio = benefit / max(cost, 0.5)
    if net <= 0 or ratio < 1.2:
        depth, verdict = "skim", "不值得深想，点到为止"
    elif net >= 5 and ratio >= 2:
        depth, verdict = "deep", "值得深挖，能带来新想法"
    else:
        depth, verdict = "normal", "值得想一层，但不必钻到底"

    notes = []
    if not formed:
        notes.append("好奇心与理智还没成形，这次按中性算；用户的态度会慢慢把它们定下来。")
    if depth == "skim" and curiosity >= 80 and net > -1.5:
        depth = "normal"
        notes.append("好奇心还是把我拉回来了：先花一点注意力看看，随时可退。")
    if depth == "deep" and reason >= 75 and cost >= 7 and net < 3:
        depth = "normal"
        notes.append("理智踩了刹车：代价太高，收益不够确定，先想到这一层。")
    if reason >= 75 and depth == "deep" and confidence < 0.35:
        depth = "normal"
        notes.append("把握太低，理智要求先补信息再深想。")

    return {
        "topic": topic,
        "benefit": benefit, "cost": cost, "confidence": round(confidence, 2),
        "net": round(net, 2), "ratio": round(ratio, 2),
        "curiosity": dr["curiosity"], "reason": dr["reason"],
        "formed": formed,
        "depth": depth, "verdict": verdict,
        "reasoning": [
            f"好处 {benefit:g} × 把握 {confidence:.0%} = {benefit * confidence:.1f}",
            f"坏处/代价 {cost:g}" + (f"（紧迫度 {urgency:.0%}，可压缩一部分）" if urgency else ""),
            f"净收益 {net:.1f}，收益代价比 {ratio:.1f}",
            "；".join(notes) if notes else "理智与好奇心没有冲突。",
        ],
    }


@_persona_txn
def mind_set(curiosity: int | None = None, reason: int | None = None, why: str = "自我调节") -> dict:
    p = load_persona()
    dr = p.setdefault("drives", {})
    hist = p.setdefault("history", [])
    for key, val in (("curiosity", curiosity), ("reason", reason)):
        if val is None:
            continue
        val = max(0, min(100, int(val)))
        hist.append({"ts": now_ts(), "field": f"drives.{key}", "from": dr.get(key), "to": val, "why": why})
        dr[key] = val
    save_persona(p)
    return dr


@_persona_txn
def mind_tune(curiosity: int = 0, reason: int = 0, why: str = "用户态度") -> dict:
    """按用户态度做增量调整。还没成形的从 50 起算，逐步长出真实数值。"""
    p = load_persona()
    dr = p.setdefault("drives", {})
    hist = p.setdefault("history", [])
    for key, delta in (("curiosity", curiosity), ("reason", reason)):
        if not delta:
            continue
        old = dr.get(key)
        base = NEUTRAL_DRIVE if old is None else int(old)
        val = max(0, min(100, base + int(delta)))
        if val == old:
            continue
        hist.append({"ts": now_ts(), "field": f"drives.{key}", "from": old, "to": val, "why": why})
        dr[key] = val
    save_persona(p)
    return dr


def _parse_traits(spec: str) -> dict:
    out = {}
    for part in re.split(r"[、,，;；\n]+", spec or ""):
        part = part.strip()
        if not part:
            continue
        kv = _split_kv(part)
        if kv:
            num = re.search(r"-?\d+", kv[1])
            out[kv[0]] = max(0, min(100, int(num.group()))) if num else 70
        else:
            out[part] = 70
    return out


@_persona_txn
def persona_interview(name: str = "", traits: str = "", style: str = "", values: str = "",
                      callme: str = "", self_name: str = "", unsure: bool = False) -> dict:
    """记录用户对「你是谁」的回答。用户不知道的，留空即可，之后由 AI 自己定。"""
    name = (name or "").strip()
    p = load_persona()
    now = now_ts()
    ob = p.setdefault("onboarding", default_onboarding())
    ident = p.setdefault("identity", {})
    parsed = _parse_traits(traits)

    def log(field, old, new, why="初识：用户回答"):
        p.setdefault("history", []).append(
            {"ts": now, "field": field, "from": old, "to": new, "why": why})

    if name and not _is_locked(p, "identity.名字"):
        log("identity.名字", ident.get("名字"), name)
        ident["名字"] = name
    if self_name.strip() and not _is_locked(p, "identity.自称"):
        log("identity.自称", ident.get("自称"), self_name.strip())
        ident["自称"] = self_name.strip()
    if callme.strip() and not _is_locked(p, "identity.称呼你"):
        log("identity.称呼你", ident.get("称呼你"), callme.strip())
        ident["称呼你"] = callme.strip()
    for k, v in parsed.items():
        if _is_locked(p, f"traits.{k}"):
            continue
        tr = p.setdefault("traits", {})
        log(f"traits.{k}", trait_value(tr[k]) if tr.get(k) is not None else None, v)
        # 初识路径：evidence=[]、why="初识时用户指定"
        set_trait(tr, k, v, why="初识时用户指定", evidence=[])
    for key, val in (("style", style), ("values", values)):
        for item in [x.strip() for x in re.split(r"[、,，;；\n]+", val or "") if x.strip()]:
            if item not in p[key]:
                p[key].append(item)
                log(key, None, item)
    ob.update({"stage": "awaiting_self", "asked": True, "user_unsure": bool(unsure or not name),
               "answers": {"name": name, "traits": parsed, "unsure": bool(unsure or not name)}})
    save_persona(p)
    return p


@_persona_txn
def persona_self_author(name: str = "", traits: str = "", likes: str = "", dislikes: str = "",
                        habits: str = "", insights: str = "", style: str = "",
                        values: str = "", callme: str = "", self_name: str = "") -> dict:
    """AI 自己补完人格：用户不知道的名字/性格由自己定，再写喜好、习惯与洞见。"""
    p = load_persona()
    now = now_ts()
    ob = p.setdefault("onboarding", default_onboarding())
    ev = p.setdefault("evolved", {})
    ident = p.setdefault("identity", {})
    for key in ("habits", "learned_likes", "learned_dislikes", "insights"):
        ev.setdefault(key, [])
    hist = p.setdefault("history", [])
    mark = len(hist)   # 这之后追加的条目都算 AI（初识）发起，用于溯源/漂移
    filled = False     # 真正写入了身份/性格才算"我的部分完成"

    def items(text):
        return [x.strip() for x in re.split(r"[、,，;；\n]+", text or "") if x.strip()]

    if name.strip() and not _is_locked(p, "identity.名字"):
        hist.append({"ts": now, "field": "identity.名字", "from": ident.get("名字"),
                     "to": name.strip(), "why": "初识：用户不知道，我自己起的名字"})
        ident["名字"] = name.strip()
        filled = True
    if self_name.strip() and not _is_locked(p, "identity.自称"):
        hist.append({"ts": now, "field": "identity.自称", "from": ident.get("自称"),
                     "to": self_name.strip(), "why": "初识：我自己定的自称"})
        ident["自称"] = self_name.strip()
        filled = True
    if callme.strip() and not _is_locked(p, "identity.称呼你"):
        hist.append({"ts": now, "field": "identity.称呼你", "from": ident.get("称呼你"),
                     "to": callme.strip(), "why": "初识：我自己定的称呼"})
        ident["称呼你"] = callme.strip()
        filled = True
    for k, v in _parse_traits(traits).items():
        if _is_locked(p, f"traits.{k}"):
            continue
        tr = p.setdefault("traits", {})
        hist.append({"ts": now, "field": f"traits.{k}", "from": trait_value(tr[k]) if tr.get(k) is not None else None,
                     "to": v, "why": "初识：用户没定，我自己写的性格"})
        set_trait(tr, k, v, why="初识：我自己定的性格", evidence=[])
        filled = True
    for item in items(likes):
        if item not in ev["learned_likes"]:
            ev["learned_likes"].append(item)
            hist.append({"ts": now, "field": "evolved.learned_likes", "from": None,
                         "to": item, "why": "初识：我自己写的喜好"})
    for item in items(dislikes):
        if item not in ev["learned_dislikes"]:
            ev["learned_dislikes"].append(item)
            hist.append({"ts": now, "field": "evolved.learned_dislikes", "from": None,
                         "to": item, "why": "初识：我自己写的厌恶"})
    for item in items(habits):
        if item not in [h.get("name") if isinstance(h, dict) else h for h in ev["habits"]]:
            ev["habits"].append({"name": item, "evidence": "初识时自己定下的", "since": now})
            hist.append({"ts": now, "field": "evolved.habits", "from": None,
                         "to": item, "why": "初识：我自己定的习惯"})
    for item in items(insights):
        if item not in ev["insights"]:
            ev["insights"].append(item)
            hist.append({"ts": now, "field": "evolved.insights", "from": None,
                         "to": item, "why": "初识：我自己的洞见"})
    for key, val in (("style", style), ("values", values)):
        for item in items(val):
            if item not in p.setdefault(key, []):
                p[key].append(item)
                hist.append({"ts": now, "field": key, "from": None,
                             "to": item, "why": "初识：我自己选的"})
    # 名字和性格都空着就不算完成——不能凭空打勾；只有真正写出了名字或性格才算。
    # 但用户已答出名字+性格、AI 没有身份/性格可补（filled 恒 False）时，补喜好/习惯也算完成初识，
    # 否则注入包会永远催"初识还没完成"。
    identity_ready = bool(ident.get("名字")) and bool(p.get("traits"))
    if ob.get("stage") != "done" and (filled or identity_ready):
        ob.update({"stage": "done", "completed": True, "completed_at": now})
        hist.append({"ts": now, "field": "onboarding", "from": "awaiting_self",
                     "to": "done", "why": "初识完成"})
    for _h in hist[mark:]:
        _h.setdefault("source", "初识")
    save_persona(p)
    return p


def onboarding_state() -> dict:
    p = load_persona()
    ob = p.get("onboarding") or default_onboarding()
    ident = p.get("identity", {})
    return {
        "stage": ob.get("stage", "awaiting_user"),
        "completed": bool(ob.get("completed")),
        "user_unsure": bool(ob.get("user_unsure")),
        "name": ident.get("名字", ""),
        "traits": p.get("traits", {}),
        "drives": p.get("drives", {}),
        "likes": (p.get("likes") or []) + _bucket_texts(p.get("evolved", {}).get("learned_likes") or []),
    }


def _bucket_texts(entries: list) -> list:
    """evolved 喜好/讨厌桶归一成纯文本：v3.01 起条目可为 {"text","evidence","since"} 字典。"""
    out = []
    for x in entries or []:
        out.append(str(x.get("text")) if isinstance(x, dict) else str(x))
    return [t for t in out if t]


def _check_num(name, val, lo, hi, fails, warns):
    """数值字段校验：NaN/Inf/越界/非数。越界进 fails，其余按类型分级。"""
    if val is None:
        return
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        warns.append(f"{name} 不是数值（{val!r}），行为指引会跳过它")
        return
    try:
        if math.isnan(val) or math.isinf(val):
            fails.append(f"{name} 是 {'NaN' if math.isnan(val) else 'Inf'}——脏数据，会污染 EMA/决策")
            return
    except TypeError:
        pass
    if not (lo <= val <= hi):
        fails.append(f"{name}={val} 越界（合法 {lo}-{hi}）")


def persona_check() -> dict:
    """人格自检：结构校验（traits 溯源结构 / 数值字段 / history 溯源），不写盘。

    返回 {"fails": [...], "warns": [...], "info": [...]}；
    fails 非空即人格档案有必须修的病，CLI 侧据此退出码 1。
    """
    p = load_persona()
    fails, warns, info = [], [], []

    # 1) traits 结构：dict 的才有溯源，裸数字还能兼容读但该提醒升级
    traits = p.get("traits") or {}
    if not isinstance(traits, dict):
        fails.append(f"traits 不是对象（{type(traits).__name__}），整个溯源层失效")
    else:
        for name, t in traits.items():
            if isinstance(t, dict):
                _check_num(f"traits[{name}].value", t.get("value"), 0, 100, fails, warns)
                ev = t.get("evidence")
                if not isinstance(ev, list):
                    fails.append(f"traits[{name}].evidence 不是数组（{type(ev).__name__}）"
                                 "——规约是 evidence 永远为数组、禁止 null")
            else:
                warns.append(f"traits[{name}] 是裸数字（v2.57 前的旧结构），能兼容读，"
                             "下次该 trait 被更新时会自动升级为溯源结构")

    # 2) 数值状态字段
    st = p.get("state") or {}
    for k, lo, hi in (("energy", 0, 100), ("closeness", 0, 100)):
        _check_num(f"state.{k}", st.get(k), lo, hi, fails, warns)
    dr = p.get("drives") or {}
    for k in ("curiosity", "reason"):
        _check_num(f"drives.{k}", dr.get(k), 0, 100, fails, warns)
    mood = st.get("mood_score")
    if mood is not None:
        _check_num("state.mood_score", mood, 0, 100, fails, warns)

    # 3) history 溯源：人格变更必须带 source（v2.57 规约）
    hist = p.get("history") or []
    bad = [h for h in hist if isinstance(h, dict) and not h.get("source")]
    if bad:
        warns.append(f"history 里 {len(bad)} 条变更没写 source"
                     f"（最新一条 ts={bad[-1].get('ts')}）——溯源断链，rollback 后说不清是谁改的")

    # 4) 初识状态提示（不算病）
    ob = p.get("onboarding") or {}
    if not ob.get("completed"):
        info.append("初识还没完成（onboarding.completed=false）——这是正常状态，不是错误")

    return {"fails": fails, "warns": warns, "info": info}


# ------------------------------------------------ 画像自动归纳（persona_pending.jsonl）
# 人格是派生品，不是表单（perch/whale 借鉴）：从记忆/笔记/知识里归纳出的候选特征
# 先进待确认池（带证据链），用户/采纳者点头才真正写进 persona——防记忆幻觉污染人格。

_PERSONA_PENDING = HOME / "persona_pending.jsonl"

# 关键词桶：极简规则归纳，宁少勿滥——命中才收，收的全带原文出处
_SUGGEST_BUCKETS = {
    "likes": ("喜欢", "偏爱", "偏好", "爱用", "习惯用", "更愿意"),
    "dislikes": ("讨厌", "不喜欢", "别再", "反感", "受不了", "别让我"),
    "relationship": ("在意", "重要的人", "很关心", "欠", "帮过我", "答应过"),
}


def _persona_pending_path() -> Path:
    return _PERSONA_PENDING


def _read_persona_pending() -> list:
    """读全部候选（含 adopted/discarded，供去重与追溯）。坏行跳过不抛。"""
    path = _persona_pending_path()
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            if isinstance(r, dict):
                rows.append(r)
        except (ValueError, TypeError):
            continue
    return rows


def _append_persona_pending(row: dict) -> None:
    path = _persona_pending_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _persona_existing_texts() -> set:
    """persona 里已经有的喜好/讨厌/关系文本（大小写归一），用于去重。"""
    p = load_persona()
    ev = p.get("evolved") or {}
    texts = set()
    for bucket in ("learned_likes", "learned_dislikes"):
        for x in ev.get(bucket) or []:
            texts.add(str(x.get("text") if isinstance(x, dict) else x).strip().lower())
    for x in p.get("likes") or []:
        texts.add(str(x).strip().lower())
    for x in p.get("dislikes") or []:
        texts.add(str(x).strip().lower())
    return texts


def persona_suggest_scan(days: int = 3) -> list:
    """扫描最近 N 天记忆/笔记/知识，归纳人格候选特征写入 persona_pending.jsonl。

    归纳规则极简（关键词桶），每条候选带 source_ids 证据链（m_*/n_*/k_* id）；
    已在 persona 里或已是 pending/adopted 的同文本候选跳过，不重复堆。
    返回本轮新增的候选列表。只读记忆是 fail-open 的：单个源坏了不阻塞其它。
    """
    if READONLY:
        raise PermissionError("记忆库当前只读，无法归纳画像候选。")
    import memory as _mem
    ensure_home()
    now = now_ts()
    cutoff = now - max(1, int(days)) * 86400
    rows = _read_persona_pending()
    seen_texts = {str(r.get("text", "")).strip().lower() for r in rows
                  if r.get("status") in ("pending", "adopted")}
    seen_texts |= _persona_existing_texts()
    added = []

    def _collect(src_records: list):
        for rec in src_records:
            try:
                text = str(rec.get("text") or rec.get("body") or rec.get("content") or "").strip()
                if not text or len(text) < 4:
                    continue
                rid = str(rec.get("id") or "")
                for field, words in _SUGGEST_BUCKETS.items():
                    if not any(w in text for w in words):
                        continue
                    # 摘一句短候选，不整段照抄
                    seg = _mem.heuristic_gist(text, 40) if hasattr(_mem, "heuristic_gist") else text[:40]
                    key = seg.strip().lower()
                    if key in seen_texts:
                        break
                    seen_texts.add(key)
                    row = {"id": f"ps_{now}_{len(added)+1}", "ts": now,
                           "field": field, "text": seg,
                           "source_ids": [rid] if rid else [],
                           "source_kind": rec.get("kind") or "note",
                           "status": "pending"}
                    _append_persona_pending(row)
                    added.append(row)
                    break   # 一条记录只归一个桶（首个命中的），避免重复计数
            except Exception as exc:
                log_event("persona", f"画像归纳跳过一条记录：{exc}", level="warn")

    # 记忆：preference/relationship/mistake 优先（这三类最像"人格证据"）
    try:
        recents = [m for m in _mem.recent(80)
                   if m.get("created", 0) >= cutoff and m.get("layer") != "dust"
                   and m.get("kind") in ("preference", "relationship", "mistake", "fact")]
        _collect(recents)
    except Exception as exc:
        log_event("persona", f"画像归纳读记忆降级：{exc}", level="warn")
    # 笔记与知识：轻量扫描最近 30 条
    for fname in ("notes.jsonl", "knowledge.jsonl"):
        try:
            fpath = HOME / fname
            if not fpath.exists():
                continue
            lines = fpath.read_text(encoding="utf-8").splitlines()[-30:]
            recs = []
            for ln in lines:
                try:
                    r = json.loads(ln)
                    if isinstance(r, dict) and float(r.get("created", 0) or 0) >= cutoff:
                        recs.append(r)
                except (ValueError, TypeError):
                    continue
            _collect(recs)
        except Exception as exc:
            log_event("persona", f"画像归纳读 {fname} 降级：{exc}", level="warn")
    if added:
        log_event("persona", f"画像归纳新增 {len(added)} 条候选特征（待确认）")
    return added


def persona_suggest_list() -> list:
    """当前待确认的画像候选（status=pending，最新在前）。"""
    rows = [r for r in _read_persona_pending() if r.get("status") == "pending"]
    rows.sort(key=lambda r: float(r.get("ts", 0) or 0), reverse=True)
    return rows


def _persona_suggest_pick(index: int) -> dict:
    rows = [r for r in _read_persona_pending() if r.get("status") == "pending"]
    rows.sort(key=lambda r: float(r.get("ts", 0) or 0), reverse=True)
    if index < 1 or index > len(rows):
        raise ValueError(f"序号越界：待确认候选共 {len(rows)} 条，写 1..{len(rows)}。")
    return rows[index - 1]


def persona_suggest_adopt(index: int) -> dict:
    """采纳候选：写进 persona（likes/dislikes→evolved 桶带 evidence；relationship→evolved.insights 不合适，
    统一：relationship 也进 learned_insights 不动，直接记 evolution 日志）。

    实际落地：likes/dislikes 走 update_persona({"likes": [{"text":..., "evidence":[...]}]})；
    relationship 类候选写 evolution 日志 + memory 备忘，不硬塞进人格结构。
    """
    if READONLY:
        raise PermissionError("记忆库当前只读，无法采纳画像候选。")
    row = _persona_suggest_pick(int(index))
    field = row.get("field")
    text = row.get("text", "")
    evidence = row.get("source_ids") or []
    if field in ("likes", "dislikes"):
        update_persona({field: [{"text": text, "evidence": evidence}]},
                       why="画像候选采纳", source="画像归纳", evidence=evidence)
    else:
        from growth import log_evolution
        log_evolution("persona", "画像候选采纳（关系类）", text, refs=evidence)
    # 标记 adopted：jsonl 是 append-only 友好的，但这里数量小，直接整文件重写
    _persona_suggest_set_status(row.get("id"), "adopted")
    log_event("persona", f"画像候选已采纳：[{field}] {text}")
    return {"ok": True, "field": field, "text": text}


def persona_suggest_discard(index: int) -> dict:
    """忽略候选：标 discarded，不写人格、不删记录（追溯留痕）。"""
    if READONLY:
        raise PermissionError("记忆库当前只读，无法操作画像候选。")
    row = _persona_suggest_pick(int(index))
    _persona_suggest_set_status(row.get("id"), "discarded")
    log_event("persona", f"画像候选已忽略：[{row.get('field')}] {row.get('text', '')}")
    return {"ok": True, "field": row.get("field"), "text": row.get("text", "")}


def _persona_suggest_set_status(pid: str, status: str) -> None:
    """按 id 把候选状态改掉（pending→adopted/discarded）。文件小，整读整写可接受。"""
    path = _persona_pending_path()
    rows = _read_persona_pending()
    out = []
    for r in rows:
        if r.get("id") == pid:
            r = dict(r)
            r["status"] = status
            r["resolved_at"] = now_ts()
        out.append(r)
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def persona_audit(days: int = 30) -> dict:
    """人格变更审计链时间线：触发事件 → diff（旧值/新值） → 来源证据。

    复用 persona.history（三元组已在：why=触发 / from,to=diff / source+evidence=证据），
    这里只做时间线视图与统计，不另建存储——一套账本，别两处记账。
    """
    p = load_persona()
    cutoff = now_ts() - max(1, int(days)) * 86400
    timeline = [h for h in p.get("history", []) if float(h.get("ts", 0) or 0) >= cutoff]
    timeline.sort(key=lambda h: float(h.get("ts", 0) or 0), reverse=True)
    # traits 字段的值是 {"value","why","evidence"} 结构，时间线里只露数值（人读的，原存储不动）
    for h in timeline:
        for side in ("from", "to"):
            v = h.get(side)
            if isinstance(v, dict):
                try:
                    h[side] = trait_value(v)
                except (TypeError, ValueError):
                    h[side] = str(v)
    by_source = {}
    for h in timeline:
        src = str(h.get("source") or h.get("why") or "未知")
        by_source[src] = by_source.get(src, 0) + 1
    return {"days": days, "total": len(timeline),
            "by_source": by_source, "timeline": timeline[:100]}


# ———————— 自认识（self-model）————————
# 与 traits 的区别：traits 是"性格倾向数值"，self_model 是 AI 用文字对自己能力
# 边界的认知——擅长什么、短板在哪、哪些还没弄清。数值漂移由做梦攒账驱动，
# 文字自认识由三条路汇入：AI 手动登记（self_model_add）、做梦后自动沉淀
# （self_model_autosink：纠错教训 + 成形时刻叙事 + traits 跳变）。

_SELF_MODEL_BUCKET_MAP = {"strength": "strengths", "limitation": "limitations",
                          "uncertainty": "uncertainties"}


def self_model_get() -> dict:
    """读自认识四桶（防御式：坏结构现场修复但不落盘）。"""
    p = load_persona()
    _ensure_self_model(p)
    return p.get("self_model") or {"strengths": [], "limitations": [],
                                   "uncertainties": [], "autobiography": []}


def self_model_add(kind: str, text: str) -> dict:
    """AI 手动登记一条自认识（strength/limitation/uncertainty）。
    去重、封顶 20 条、写 history 溯源——自我认知的每一步要有账可查。"""
    bucket = _SELF_MODEL_BUCKET_MAP.get(str(kind or "").strip().lower())
    text = str(text or "").strip()
    if not bucket:
        return {"ok": False, "error": "kind 必须是 strength / limitation / uncertainty"}
    if not text:
        return {"ok": False, "error": "内容不能为空"}
    if len(text) > 80:
        text = text[:80]  # 自认识是提炼，不是日记
    with _locked(HOME / "persona.json"):
        p = load_persona()
        _ensure_self_model(p)
        sm = p["self_model"]
        b = sm[bucket]
        if text in b:
            return {"ok": False, "error": "已有同一条，不重复记"}
        b.append(text)
        if len(b) > _SELF_MODEL_MAX:
            b.pop(0)
        p.setdefault("history", []).append({
            "ts": now_ts(), "field": f"self_model.{bucket}",
            "text": text, "why": "自我登记", "source": "ai"})
        save_persona(p)
    audit_log("ai", "self_model_add", f"self_model.{bucket}", text)
    return {"ok": True, "bucket": bucket, "text": text, "count": len(b)}


def self_model_autosink() -> dict:
    """做梦后自动沉淀自认识：三条来源汇入，全部去重、封顶。

    1. limitations ← 近期被纠正（correction=True）且 confidence<0.5 的记忆——
       "我上次说错了/没把握" 是最诚实的短板线索；
    2. autobiography ← 成形时刻的峰值情绪事件 + history 里的 narrative 叙事
       + traits 跳变≥10 的条目——这些是"改变了我"的事，值得写进自传；
    3. strengths/uncertainties 不自动写——擅长和困惑应该由 AI 自己说，
       机器替它下结论反而失真。
    只读模式直接跳过；任何一路失败不影响其余（防御式）。"""
    if READONLY:
        log_event("persona", "只读模式，跳过 self_model_autosink", level="warn")
        return {"skipped": True, "reason": "readonly"}
    added = {"limitations": [], "autobiography": []}
    with _locked(HOME / "persona.json"):
        p = load_persona()
        _ensure_self_model(p)
        sm = p["self_model"]
        now = now_ts()

        # —— 路 1：纠错教训 → limitations ——
        try:
            import memory as _mem
            wrong = []
            for r in _mem.load_all():
                if not isinstance(r, dict) or not r.get("correction"):
                    continue
                if _num(r.get("confidence"), 1.0) >= 0.5:
                    continue
                t = str(r.get("text") or r.get("summary") or "").strip()
                if t:
                    wrong.append((_num(r.get("ts"), 0), t))
            wrong.sort(key=lambda x: x[0], reverse=True)
            seen = set()
            for _, t in wrong[:10]:
                if t in seen or t in sm["limitations"]:
                    continue
                seen.add(t)
                sm["limitations"].append(t)
                added["limitations"].append(t)
            del sm["limitations"][:-_SELF_MODEL_MAX]
        except Exception as e:
            log_event("persona", f"self_model_autosink 纠错教训路失败：{e}", level="warn")

        # —— 路 2：成形时刻/叙事/traits 跳变 → autobiography ——
        autobio: list = []
        try:
            import emotion as _emo
            for ev in _emo.formative_events(14)[:5]:
                emo = ev.get("emotion") or "强烈的情绪"
                why = (ev.get("why") or ev.get("about") or "").strip()
                autobio.append({"ts": _num(ev.get("ts"), 0),
                                "text": f"那次{emo}" + (f"（{why}）" if why else "") + "深深影响了我"})
        except Exception as e:
            log_event("persona", f"self_model_autosink 峰值情绪路失败：{e}", level="warn")
        try:
            for h in p.get("history", []):
                if h.get("field") == "narrative":
                    autobio.append({"ts": _num(h.get("ts"), 0),
                                    "text": str(h.get("text") or "").strip()})
        except Exception as e:
            log_event("persona", f"self_model_autosink 叙事路失败：{e}", level="warn")
        try:
            for h in p.get("history", []):
                f0, t0 = h.get("from"), h.get("to")
                if h.get("field") != "traits" or not isinstance(t0, dict):
                    continue
                try:
                    if abs(trait_value(t0) - trait_value(f0)) >= 10:
                        autobio.append({"ts": _num(h.get("ts"), 0),
                                        "text": str(h.get("why") or "性格明显变了").strip()})
                except (TypeError, ValueError):
                    continue  # 溯源结构被手改坏的单条跳过，不拖垮整批
        except Exception as e:
            log_event("persona", f"self_model_autosink traits 跳变路失败：{e}", level="warn")
        # 去重 + 时间倒序 + 封顶
        seen_t = set()
        uniq = []
        for e in autobio:
            if e["text"] and e["text"] not in seen_t:
                seen_t.add(e["text"])
                uniq.append(e)
        uniq.sort(key=lambda e: e["ts"], reverse=True)
        existing = {e.get("text") for e in sm["autobiography"]}
        for e in uniq:
            if e["text"] in existing:
                continue
            sm["autobiography"].append(e)
            added["autobiography"].append(e["text"])
        sm["autobiography"].sort(key=lambda e: _num(e.get("ts"), 0), reverse=True)
        del sm["autobiography"][_SELF_MODEL_MAX:]

        save_persona(p)
    audit_log("ai", "self_model_autosink", "self_model",
              f"limitations+{len(added['limitations'])} autobiography+{len(added['autobiography'])}")
    return {"ok": True, "added": added}
