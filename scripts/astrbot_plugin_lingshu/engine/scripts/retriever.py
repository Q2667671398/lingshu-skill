#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检索接入（附录 C · P3）——优先问 session-rag 服务，没有就退回灵枢自己的召回。

统一契约（附录 B §4）：
    POST {session_rag.url}/v1/search  {"query": "...", "k": 20}
      -> {"results": [{"id","text","score","source"}]}

拿不到服务（没配 / 超时 / 报错 / 返回不可解析）→ **回退 `memory.recall()`**，
并在结果里标 `source='lingshu'` + warning。绝不因为检索坏了阻塞注入。

二段式：`search()` 拿候选 → `select()` 按预算挑（模型可选，失败退回打分顺序）。
"""
from __future__ import annotations

import json
import urllib.request

DEFAULT_BUDGET_TOKENS = 800
CHARS_PER_TOKEN = 2


def _cfg(cfg: dict | None) -> dict:
    if cfg is not None:
        return dict(cfg.get("session_rag") or {})
    try:
        import core
        return dict((core.load_config() or {}).get("session_rag") or {})
    except Exception:
        return {}


def estimate_tokens(text: str) -> int:
    n = len(str(text or ""))
    return (n + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _normalize(items) -> list[dict]:
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or it.get("content") or "").strip()
        if not text:
            continue
        out.append({
            "id": str(it.get("id") or it.get("memory_id") or ""),
            "text": text,
            "score": float(it.get("score") or 0.0),
            "source": str(it.get("source") or it.get("platform") or ""),
        })
    return out


def http_search(query: str, k: int = 20, *, cfg: dict | None = None, call=None,
                timeout: float | None = None) -> list[dict] | None:
    """问 session-rag 的 /v1/search；失败返回 None（由上层回退）。"""
    c = _cfg(cfg)
    url = str(c.get("url") or "").rstrip("/")
    if not url and call is None:
        return None
    if call is not None:
        try:
            return _normalize(call(query, k))
        except Exception:
            return None
    body = json.dumps({"query": query, "k": int(k)}).encode("utf-8")
    req = urllib.request.Request(url + "/v1/search", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or float(c.get("timeout") or 3)) as r:
            # 响应上限 2MB：服务异常/恶意代理返回超大 body 时不能把内存打满
            data = json.loads(r.read(2 * 1024 * 1024).decode("utf-8", "replace"))
        rows = data.get("results") if isinstance(data, dict) else data
        return _normalize(rows)
    except Exception as exc:  # noqa: BLE001
        try:
            import failures
            failures.record("retriever.session_rag", f"{type(exc).__name__}: {exc}",
                            ctx={"url": url})
        except Exception:
            pass
        return None


def recall_search(query: str, k: int = 20) -> list[dict]:
    """灵枢本地召回（词面/实体/向量混合，取决于配置），失败返回空。"""
    try:
        import memory
        rows = memory.recall(query, k=int(k))
    except Exception:
        return []
    out = []
    for item in rows or []:
        try:
            m, score, _parts = item
        except Exception:
            continue
        out.append({
            "id": str(m.get("id") or ""),
            "text": str(m.get("text") or ""),
            "score": float(score or 0.0),
            "source": "lingshu",
        })
    return out


def search(query: str, k: int = 20, *, cfg: dict | None = None, call=None) -> dict:
    """先服务后本地，返回 {results, source, warnings}。"""
    warnings: list[str] = []
    rows = http_search(query, k, cfg=cfg, call=call)
    if rows is not None:
        return {"results": rows, "source": "session-rag", "warnings": warnings}
    c = _cfg(cfg)
    if c.get("url"):
        warnings.append("session-rag 不可用，已回退灵枢本地召回")
    rows = recall_search(query, k)
    return {"results": rows, "source": "lingshu", "warnings": warnings}


# ---------------------------------------------------------------- 挑选

def _heuristic(cands: list[dict], budget_tokens: int) -> list[dict]:
    kept, used = [], 0
    for it in cands:
        t = estimate_tokens(it["text"])
        if kept and used + t > budget_tokens:
            continue
        kept.append(it)
        used += t
        if used >= budget_tokens:
            break
    return kept


def _model_select(cands: list[dict], query: str, call) -> list[dict] | None:
    """让模型从候选里挑：只要 id 列表，不重写正文。"""
    brief = "\n".join(f"{i+1}. id={c['id']} 摘要={c['text'][:120]}"
                      for i, c in enumerate(cands))
    prompt = (f"查询：{query}\n下面是从记忆里检索到的候选片段。只挑和查询真正相关的，"
              f"最多 5 条。只输出 JSON：{{\"keep\":[\"id\", ...]}}\n\n{brief}")
    try:
        raw = call(prompt)
        data = json.loads(str(raw or "").strip())
        keep = [str(x) for x in (data.get("keep") or [])]
    except Exception:
        return None
    if not keep:
        return None
    by_id = {c["id"]: c for c in cands}
    out = [by_id[i] for i in keep if i in by_id]
    return out or None


def select(cands: list[dict], query: str = "", *, budget_tokens: int | None = None,
           cfg: dict | None = None, call=None) -> dict:
    """候选 → 选中（模型优先，失败/超预算退回启发式）。"""
    c = _cfg(cfg)
    budget = int(budget_tokens or c.get("budget_tokens") or DEFAULT_BUDGET_TOKENS)
    cands = _normalize(cands)
    if not cands:
        return {"kept": [], "source": "empty", "warnings": []}
    warnings: list[str] = []
    kept = None
    if call is not None or c.get("select_model"):
        caller = call
        if caller is None:
            try:
                from summarizer import openai_chat
                caller = lambda p: openai_chat(p, dict(c.get("select") or {}))
            except Exception:
                caller = None
        if caller is not None:
            kept = _model_select(cands, query, caller)
            if kept is None:
                warnings.append("模型挑选失败/不可用，退回打分顺序")
    source = "model" if kept is not None else "heuristic"
    if kept is None:
        kept = _heuristic(cands, budget)
    return {"kept": kept, "source": source, "warnings": warnings,
            "tokens": sum(estimate_tokens(x["text"]) for x in kept)}


def build_snippet(query: str, k: int = 20, *, budget_tokens: int | None = None,
                  cfg: dict | None = None, call=None) -> str:
    """一步到位：检索 → 挑选 → 拼成带来源标记的文本块（空结果返回 ""）。"""
    res = search(query, k, cfg=cfg)
    picked = select(res["results"], query, budget_tokens=budget_tokens, cfg=cfg, call=call)
    if not picked["kept"]:
        return ""
    lines = [f"## 检索到的相关片段（来源：{res['source']}）"]
    for it in picked["kept"]:
        tag = f"[{it['source'] or 'mem'}:{it['id']}]" if it["id"] else f"[{it['source'] or 'mem'}]"
        lines.append(f"- {tag} {it['text'][:400]}")
    return "\n".join(lines)
