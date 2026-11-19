#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话摘要层（附录 C · P2）——五段式摘要 + 校验 + fail-open。

设计要点（照附录 C 与 billion-context 的教训）：
  1. **五段固定结构**：目标 / 已做 / 未决 / 约束 / 原话引用。其中「原话引用」必须逐字保留，
     它是摘要漂移时的纠错锚。
  2. **外部便宜模型可选**：配 `config.summary`（OpenAI 兼容 /chat/completions）就走模型；
     没配、超时、报错、输出不合格 → 一律**回退到离线抽取式摘要**，绝不阻塞主流程（fail-open）。
  3. **提交前校验**：长度上限 + 五个小标题齐全 + 不许整段回显 prompt / 代码块。
  4. 纯标准库；模型调用用 urllib，可注入 `call` 便于测试（不联网也能测全部分支）。

CLI（在 cli.py 里挂 `summarize`）：
    brain.py summarize --input turns.jsonl [--json] [--max-chars 1500]
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.request

HEADERS = ("目标", "已做", "未决", "约束", "原话引用")
DEFAULT_MAX_CHARS = 1500

TEMPLATE = """你是会话摘要器。把下面的对话压成固定五段，供下一轮上下文使用。

必须严格输出这五个小标题（顺序不变），每段 1-3 行：
## 目标
（用户想要什么；可引用一句原话）
## 已做
（已经完成/确认的事实，带路径或命令）
## 未决
（还没定的问题、待办、阻塞）
## 约束
（用户明确说过的限制：风格、技术栈、不要做什么）
## 原话引用
（最多 3 句必须逐字保留的用户原话：纠正、承诺、关键数字）

规则：
- 总长不超过 {max_chars} 字；不要复述本提示，不要输出代码块。
- 上一版摘要只作参考，发现冲突以最新对话为准。

{prev}
---- 对话 ----
{turns}
"""


# ---------------------------------------------------------------- 输入归一

def normalize_turns(raw) -> list[dict]:
    """接受 [{role,content}] / [{role,text}] / {"messages":[...]} / JSONL 行列表。"""
    if isinstance(raw, dict):
        raw = raw.get("messages") or raw.get("turns") or []
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or item.get("sender") or "other").lower()
        if role in ("human", "me", "用户"):
            role = "user"
        elif role in ("ai", "bot", "model", "助手"):
            role = "assistant"
        text = item.get("content", item.get("text", item.get("message", "")))
        if isinstance(text, list):   # OpenAI content parts
            text = " ".join(str(x.get("text") if isinstance(x, dict) else x) for x in text)
        text = str(text or "").strip()
        if text:
            out.append({"role": role, "text": text})
    return out


def load_turns(path) -> list[dict]:
    """读 .jsonl（每行一条）或 .json（数组 / {messages:[...]}）。"""
    from pathlib import Path
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    if p.suffix.lower() == ".jsonl":
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return normalize_turns(rows)
    return normalize_turns(json.loads(text))


# ---------------------------------------------------------------- 五段校验

def validate(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[bool, str]:
    """提交前校验：长度、五个小标题、拒绝回显提示/代码块。"""
    t = str(text or "").strip()
    if not t:
        return False, "空摘要"
    if len(t) > max_chars:
        return False, f"超长：{len(t)} > {max_chars}"
    missing = [h for h in HEADERS if f"## {h}" not in t]
    if missing:
        return False, "缺小标题：" + "、".join(missing)
    if "```" in t:
        return False, "包含代码块"
    if "规则：" in t and "上一版摘要" in t:
        return False, "疑似回显提示词"
    return True, ""


# ---------------------------------------------------------------- 离线抽取式（fail-open 兜底）

_KEY = re.compile(r"不对|不是|改成|记住|务必|不要|必须|只能|预算|注意|风险|TODO|待办|未决|下次|明天")
_QUOTE = re.compile(r"不对|不是|改成|记住|务必|别|不要|必须")


def _clip(s: str, n: int = 120) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def extractive_fallback(turns: list[dict], max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """不调模型的确定性摘要：只挑明确信号，不编造。"""
    users = [t["text"] for t in turns if t["role"] == "user"]
    bots = [t["text"] for t in turns if t["role"] == "assistant"]
    goal = _clip(users[0], 160) if users else "（没读到用户消息）"
    done = [f"- {_clip(b)}" for b in bots[-3:] if len(b) >= 8]
    pending = [f"- {_clip(u)}" for u in users[-3:] if _KEY.search(u)]
    constraints = [f"- {_clip(u)}" for u in users
                   if re.search(r"不要|必须|只能|务必|别", u)][-3:]
    quotes = [f"- {_clip(u, 80)}" for u in users if _QUOTE.search(u)][-3:]

    def block(title: str, lines: list[str], empty: str) -> str:
        body = "\n".join(lines) if lines else empty
        return f"## {title}\n{body}"

    text = "\n".join([
        block("目标", ([f"- {goal}"] if goal else []), "- （未提取到）"),
        block("已做", done, "- （未提取到）"),
        block("未决", pending, "- （未提取到）"),
        block("约束", constraints, "- （未提取到）"),
        block("原话引用", quotes, "- （未提取到）"),
    ])
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


# ---------------------------------------------------------------- 模型调用（可选）

def _cfg_section(cfg: dict | None) -> dict:
    if cfg is not None:
        return dict(cfg.get("summary") or {})
    try:
        import core
        return dict((core.load_config() or {}).get("summary") or {})
    except Exception:
        return {}


def openai_chat(prompt: str, cfg: dict, timeout: float | None = None) -> str | None:
    """OpenAI 兼容 /chat/completions；任何失败返回 None（由上层 fail-open）。"""
    base = str(cfg.get("base_url") or "").rstrip("/")
    model = str(cfg.get("model") or "")
    if not base or not model:
        return None
    key_env = str(cfg.get("api_key_env") or "")
    api_key = os.environ.get(key_env, "") if key_env else ""
    body = json.dumps({
        "model": model,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {api_key}"} if api_key else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout or float(cfg.get("timeout") or 30)) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content")
        return str(content or "").strip() or None
    except Exception:
        return None


# ---------------------------------------------------------------- 主入口

def build_prompt(turns: list[dict], prev_summary: str = "", max_chars: int = DEFAULT_MAX_CHARS) -> str:
    lines = []
    for t in turns:
        who = "用户" if t["role"] == "user" else ("我" if t["role"] == "assistant" else "其他")
        lines.append(f"{who}: {t['text']}")
    prev = f"上一版摘要：\n{prev_summary.strip()}\n" if prev_summary and prev_summary.strip() else ""
    return TEMPLATE.format(max_chars=max_chars, prev=prev, turns="\n".join(lines))


def summarize(turns: list[dict], *, cfg: dict | None = None, prev_summary: str = "",
              max_chars: int = DEFAULT_MAX_CHARS, call=None) -> dict:
    """生成五段摘要：模型优先，失败/不合格一律回退抽取式。

    返回 {text, source: 'model'|'fallback', actions:[...], warnings:[...], chars}
    """
    turns = normalize_turns(turns)
    actions: list[str] = []
    warnings: list[str] = []
    sec = _cfg_section(cfg)
    if not sec.get("enabled"):
        actions.append("summary.enabled=false：走离线抽取式")
        text = extractive_fallback(turns, max_chars)
        return {"text": text, "source": "fallback", "actions": actions,
                "warnings": warnings, "chars": len(text)}

    prompt = build_prompt(turns, prev_summary, max_chars)
    caller = call or (lambda p: openai_chat(p, sec))
    try:
        text = caller(prompt)
    except Exception as exc:  # noqa: BLE001
        text = None
        warnings.append(f"模型调用异常：{exc!r}")
    if text:
        ok, why = validate(text, max_chars)
        if ok:
            return {"text": text, "source": "model", "actions": actions,
                    "warnings": warnings, "chars": len(text)}
        warnings.append(f"模型输出不合格（{why}），回退离线摘要")
    else:
        warnings.append("模型调用失败，回退离线摘要")
    text = extractive_fallback(turns, max_chars)
    return {"text": text, "source": "fallback", "actions": actions,
            "warnings": warnings, "chars": len(text)}


def estimate_tokens(text: str, chars_per_token: int = 2) -> int:
    return int(math.ceil(len(text or "") / max(1, chars_per_token)))
