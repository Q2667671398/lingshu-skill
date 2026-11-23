# -*- coding: utf-8 -*-
"""可选 embedding / 降级向量（v3.39 从 core.py 拆出）。

零依赖铁律：配了 embed.url（或环境变量 LINGSHU_EMBED_URL）才启用真嵌入；
未配置/不可达时自动退回 fallback_embed.py 的纯 stdlib hash 向量；兜底也关掉
才返回 None，调用方按"无 Dense 能力"降级。网络调用全走标准库 urllib，
任何超时/异常一律静默返回 None，绝不阻断主链路。

依赖约定（与 mem_dialogue.py 一致）：
  - 可变全局（HOME / config / 日志 / 熔断状态）一律走 `core.*` 模块属性，
    不 `from core import xxx` 复制值——core 会 rebind HOME，复制出来的是快照；
  - 熔断状态 _EMBED_DOWN_UNTIL 留在 core：insights 面板与测试都按
    core._EMBED_DOWN_UNTIL 直接读写，必须共享同一份进程内状态；
  - `import core` 延迟到函数调用时（`_core()`），因为 core.py 末尾会
    `from mem_embed import *`——导入期不触碰 core，杜绝循环导入。
"""
from __future__ import annotations

import json
import os
import time

# star-import 导出面：core.py 末尾 `from mem_embed import *` 依赖它。
# 下划线开头的 _embed_cfg 必须显式列出，否则 star-import 不会带过去
# （memory.py / cli.py 等模块 `from core import *` 后要直接调 _embed_cfg()）。
__all__ = ["_embed_cfg", "fallback_encode_cached", "embed_texts"]

_core_mod = None


def _core():
    """延迟拿 core 模块（首次调用后缓存）：避免导入期循环，同时始终读活值。"""
    global _core_mod
    m = _core_mod
    if m is None:
        import core
        m = _core_mod = core
    return m


def _embed_cfg() -> dict | None:
    """读 embed 配置；url 空白（未配置）返回 None。"""
    url = os.environ.get("LINGSHU_EMBED_URL", "").strip()
    try:
        cfg = _core().load_config().get("embed") or {}
    except Exception:
        cfg = {}
    if not url:
        url = str(cfg.get("url") or "").strip()
    if not url:
        return None
    return {
        "url": url,
        "model": str(cfg.get("model") or "nomic-embed-text"),
        "timeout_sec": float(cfg.get("timeout_sec") or 8),
        "max_chars": int(cfg.get("max_chars") or 3000),
        "batch_size": int(cfg.get("batch_size") or 8),
    }


# fallback_embed.encode 的进程内 LRU 缓存（查询/旧记忆在线编码时避免重复 hash）
_FB_EMBED_CACHE: dict = {}
_FB_EMBED_CACHE_MAX = 2000


def fallback_encode_cached(text: str, dims: int) -> list | None:
    """fallback_embed.encode 的进程内 LRU 缓存（查询/旧记忆在线编码时避免重复 hash）。
    任何异常返回 None，不炸。"""
    global _FB_EMBED_CACHE
    try:
        key = (dims, str(text or ""))
        hit = _FB_EMBED_CACHE.get(key)
        if hit is not None:
            return hit
        import fallback_embed
        v = fallback_embed.encode(text, dims)
        if v is not None:
            if len(_FB_EMBED_CACHE) >= _FB_EMBED_CACHE_MAX:
                _FB_EMBED_CACHE.clear()   # 简单粗暴防膨胀；缓存命中是纯优化，丢了不伤正确性
            _FB_EMBED_CACHE[key] = v
        return v
    except Exception:
        return None


# v3.95（P9 #65）：冷却状态**持久化到磁盘**。原来 core._EMBED_DOWN_UNTIL 只是
# 模块全局，而 brain.py 每次调用都是新进程 → 抑制永远只在本次进程内生效。
# 后果：Ollama 没开/挂了时，每一次 recall/prior 都要白等满 embed.timeout_sec
# （默认 30s）才降级。写盘后新进程也能直接走降级向量。
_EMBED_STATE_FILE = "_embed_state.json"


def _down_until_path():
    try:
        return _core().HOME / _EMBED_STATE_FILE   # core 是惰性获取的模块，不能直接引用
    except Exception:
        return None


def _load_down_until() -> float:
    try:
        p = _down_until_path()
        if p is None or not p.is_file():
            return 0.0
        return float((json.loads(p.read_text(encoding="utf-8")) or {}).get("down_until") or 0)
    except Exception:
        return 0.0


def _save_down_until(ts: float) -> None:
    try:
        p = _down_until_path()
        if p is None:
            return
        if ts <= 0:
            p.unlink(missing_ok=True)          # 恢复成功：清掉冷却标记
        else:
            from core import atomic_json
            atomic_json(p, {"down_until": float(ts)})
    except Exception:
        pass                                    # 状态落盘失败不能影响嵌入主链路


def embed_texts(texts: list) -> list | None:
    """批量生成 embedding（Ollama /api/embeddings）。

    返回与 texts 等长的 list[list[float]]；未配置/超时/异常/返回结构不符
    一律静默返回 None——调用方按"无 Dense 能力"降级，不做半吊子混合。
    """
    core = _core()
    cfg = _embed_cfg()
    if not texts:
        return None
    # 冷却期：上次连不上远端，这次直接走兜底，不再等满 timeout 也不重复告警
    # 冷却判定：**每个进程只在首次判断时读一次磁盘**，之后以内存为准。
    # 这样既能跨进程继承冷却（新进程读到磁盘上的未来时间），又允许"把内存置 0
    # 当作已过期"的显式重置生效（测试/排障会这么做）。
    if not getattr(core, "_EMBED_STATE_LOADED", False):
        try:
            core._EMBED_DOWN_UNTIL = max(float(getattr(core, "_EMBED_DOWN_UNTIL", 0.0) or 0.0),
                                         _load_down_until())
        except Exception:
            pass
        core._EMBED_STATE_LOADED = True
    if cfg and time.time() < float(getattr(core, "_EMBED_DOWN_UNTIL", 0.0) or 0.0):
        cfg = None
    # 未配置真嵌入服务时，先看降级向量兜底能不能接管（embed.fallback 默认开）；
    # 兜底也关了才真正返回 None（调用方按"无 Dense 能力"降级）。
    if not cfg:
        try:
            import fallback_embed
            fb = fallback_embed.cfg()
        except Exception:
            fb = None
        if not fb:
            return None
        out = []
        for t in texts:
            v = fallback_encode_cached(str(t or "")[:3000], fb["dims"])
            if v is None:
                return None    # 批量里有空文本等坏输入：整体按"无向量"处理
            out.append(v)
        return out
    import urllib.request  # 标准库，延迟 import：不用 Dense 时不付加载成本
    out = []
    try:
        for t in texts:
            body = json.dumps({
                "model": cfg["model"],
                "prompt": str(t or "")[: cfg["max_chars"]],
            }).encode("utf-8")
            req = urllib.request.Request(
                cfg["url"], data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=cfg["timeout_sec"]) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            vec = data.get("embedding")
            if not isinstance(vec, list) or not vec:
                return None
            out.append([float(x) for x in vec])
        core._EMBED_DOWN_UNTIL = 0.0
        _save_down_until(0)
        return out
    except Exception as exc:
        _cd = 300.0
        try:
            _cd = max(30.0, float((cfg or {}).get("fail_cooldown_sec") or 300))
        except Exception:
            pass
        core._EMBED_DOWN_UNTIL = time.time() + _cd
        _save_down_until(core._EMBED_DOWN_UNTIL)
        core.log_event("embed", f"embedding 调用失败，{int(_cd)} 秒内不再重试，先走降级向量：{exc}",
                       level="warn")
    # 降级兜底：真嵌入不可用（未配置/不可达）时用纯 stdlib hash 嵌入接管
    # （config embed.fallback.enabled 默认开）。批量语义：返回等长列表。
    try:
        import fallback_embed  # 同目录模块，零依赖
        fb = fallback_embed.cfg()
        if fb:
            out = []
            for t in texts:
                v = fallback_encode_cached(str(t or "")[:3000], fb["dims"])
                if v is None:
                    return None    # 批量里有空文本等坏输入：整体按"无向量"处理
                out.append(v)
            return out
    except Exception:
        pass
    return None
