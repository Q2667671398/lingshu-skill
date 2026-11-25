# -*- coding: utf-8 -*-
"""灵枢主动搭话（P1-3）：会话之间按概率自己说一句。

借鉴 dsh-speak-up 的"本地掷骰子"——**不进模型、不花 token**，纯模板 + 词表。

四条闸门，缺一不可（任何一条不满足就安静，绝不刷屏）：
  ① `speak_up_prob` > 0（默认 0.10；设 0 永不出声）
  ② 用户这一轮**只是陈述**（没在问问题）
  ③ 灵枢此刻情绪不激动（`rationality.effective > 30`）
  ④ 距上次主动搭话 ≥ `speak_up_cooldown_min`（默认 15 分钟）

模板库取值：从最近记忆里挑一个关键词/主题，塞进句子里——避免"干巴巴的固定话术"。
"""
from __future__ import annotations

import json
import random
import re
import time

import brain
import core

STATE_FILE = "speak-up.json"
DEFAULT_PROB = 0.10
DEFAULT_COOLDOWN_MIN = 15

# 模板库：{关键词} 会被替换成最近记忆里的一个主题词。刻意写得短、不油腻。
TEMPLATES = (
    "你好像在忙{kw}，要不要说说？",
    "刚才提到{kw}，我有点感兴趣。",
    "关于{kw}，要我帮你翻翻以前的记录吗？",
    "{kw}那件事，你后来怎么定的？",
    "我留意到{kw}，需要我记一笔吗？",
    "说到{kw}——我之前是不是也碰过？",
)

# 判定"在提问"的标志：有问号、或出现这些请求词
_QUESTION_RE = re.compile(r"[？?]|(吗|呢|怎么|如何|为什么|为何|是否|能不能|可不可以|要不要|帮我|请问)")
# 主题词排除：整词黑名单 + 子串黑名单（含这些基本不是"话题词"）
_STOP = {"这个", "那个", "什么", "现在", "就是", "然后", "可以", "需要", "一下",
         "我们", "你们", "他们", "自己", "时候", "问题", "东西", "事情", "系统",
         "内容", "部分", "方式", "情况", "方法", "功能", "代码"}
_STOP_SUB = ("你", "我", "他", "她", "它", "咱", "您", "的", "了", "着", "吗",
             "呢", "吧", "啊", "哦", "嗯", "是", "在", "有", "和", "与", "或",
             "把", "被", "对", "从", "到", "会", "能", "要", "就", "也", "都",
             "很", "太", "更", "最", "但", "而", "如", "若", "因", "所", "这",
             "那", "哪", "什", "么", "怎", "为", "用", "做", "说", "看", "想")


def _state_path():
    return core.HOME / STATE_FILE


def _load_state() -> dict:
    try:
        d = json.loads(_state_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    try:
        core.atomic_json(_state_path(), st)
    except (OSError, ValueError):
        pass


def _cfg() -> dict:
    try:
        return core.load_config()
    except Exception:  # noqa: BLE001
        return {}


def prob() -> float:
    """触发概率（0~1）。坏配置回落默认值。"""
    try:
        v = float(_cfg().get("speak_up_prob", DEFAULT_PROB))
    except (TypeError, ValueError):
        return DEFAULT_PROB
    return max(0.0, min(1.0, v))


def cooldown_sec() -> float:
    try:
        v = float(_cfg().get("speak_up_cooldown_min", DEFAULT_COOLDOWN_MIN))
    except (TypeError, ValueError):
        v = DEFAULT_COOLDOWN_MIN
    return max(0.0, v) * 60.0


def is_question(text: str) -> bool:
    """这一轮用户是不是在提问/请求（那就别主动搭话，先回答）。"""
    return bool(_QUESTION_RE.search(str(text or "")))


def _good_topic(w: str) -> bool:
    """一个候选是否适合当话题词（人/物/概念，而不是代词+动词碎片）。"""
    w = str(w or "").strip().lstrip("#")
    if not (2 <= len(w) <= 12):
        return False
    if w in _STOP:
        return False
    # 含任何虚词/代词字符就不是话题词（"你可以用" 这类就是被这条挡掉的）
    if any(ch in w for ch in _STOP_SUB):
        return False
    # 至少一半得是汉字/字母/数字，排除纯符号
    core_chars = [c for c in w if c.isalnum()]
    if len(core_chars) < 2 or len(core_chars) < len(w) / 2:
        return False
    return True


def _topic_from_recent() -> str:
    """从最近记忆的**实体/标签**里抽一个话题词；抽不到返回 ""（宁可不说）。"""
    try:
        rows = brain.recent(12) or []
    except Exception:  # noqa: BLE001
        return ""
    cands = []
    for m in rows:
        for w in (m.get("entities") or []):
            if _good_topic(w):
                cands.append(str(w).strip().lstrip("#"))
        for w in (m.get("tags") or []):
            if _good_topic(w):
                cands.append(str(w).strip().lstrip("#"))
    if not cands:
        return ""
    # 去重后从"较新"的候选里挑，避免老话题反复出现
    uniq = list(dict.fromkeys(cands))
    return uniq[-1] if len(uniq) == 1 else random.choice(uniq[-6:])


def maybe(last_user_text: str = "", force: bool = False,
          rng: random.Random | None = None, now: float | None = None) -> dict:
    """决定是否主动搭话。返回 {triggered, text, reason}。

    `force=True` 跳过概率与冷却（调试/验收用），但仍守"不是提问"这条。
    """
    now = time.time() if now is None else now
    rng = rng or random

    p = prob()
    if p <= 0 and not force:
        return {"triggered": False, "reason": "speak_up_prob=0（已关闭）"}
    if last_user_text and is_question(last_user_text):
        return {"triggered": False, "reason": "这一轮是提问，先回答"}
    try:
        import emotion
        eff = float((emotion.rationality() or {}).get("effective") or 0)
    except Exception:  # noqa: BLE001
        eff = 100.0
    if not force and eff <= 30:
        return {"triggered": False, "reason": f"情绪太激动（理智 {eff:.0f} ≤ 30）"}
    st = _load_state()
    last = float(st.get("last_at") or 0)
    cd = cooldown_sec()
    if not force and now - last < cd:
        left = int((cd - (now - last)) / 60) + 1
        return {"triggered": False, "reason": f"冷却中（还需 {left} 分钟）"}
    if not force and rng.random() >= p:
        return {"triggered": False, "reason": f"掷骰子未中（p={p:.2f}）"}

    kw = _topic_from_recent()
    if not kw:
        return {"triggered": False, "reason": "没有可用的主题词（库太新/太干净）"}
    text = rng.choice(TEMPLATES).format(kw=kw)
    st["last_at"] = now
    st["last_text"] = text
    st["count"] = int(st.get("count") or 0) + 1
    _save_state(st)
    # 留痕：doctor / 面板日志能看到"它什么时候自己开过口"
    try:
        import failures
        failures.record("speak_up.triggered", text[:120], level="info")
    except Exception:  # noqa: BLE001
        pass
    try:
        brain.log_event("speak_up", f"主动搭话：{text[:80]}")
    except Exception:  # noqa: BLE001
        pass
    return {"triggered": True, "text": text, "topic": kw,
            "count": st["count"], "prob": p}
