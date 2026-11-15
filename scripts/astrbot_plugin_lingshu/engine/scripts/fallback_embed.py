# -*- coding: utf-8 -*-
"""灵枢 —— 纯 stdlib 降级向量（hash trick 嵌入）。

零依赖铁律下的向量兜底：不装 Ollama 时，用字符级 hash 嵌入给语义召回
提供一个"聊胜于无"的 Dense 通道。思路：

  * 分词：中文按 2-gram（与 memory.terms 一致）、英文/数字按词；
  * 每个 token 用 md5 hash 到固定维度（默认 256）的桶里，带 ±1 符号
    （md5 第 17 位十六进制位决定正负，等价一个免费的第二哈希）；
  * TF 做 1+log 亚线性加权（长文不至于一词独大），最后 L2 归一化。

向量存记忆条目 m["fvec"]（list[float]，4 位小数）；查询时同样编码后算
余弦。这是局部敏感哈希式词面语义：同义改写命中有限，但跨词序/跨词形
（"并发方案" vs "那个并发的方案"）比纯关键词稳。真嵌入（Ollama）可用时
优先用真的，本模块只在不可达/未配置时接管。

坏输入纪律：任何异常一律返回 None 或零相似，绝不抛出、绝不阻断主链路。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-z0-9]+")


def cfg() -> dict | None:
    """读 config.json 的 embed.fallback 节；未启用返回 None。

    默认开启（enabled 缺省 True），dims 默认 256。读不到配置/坏 JSON
    一律按未启用处理（返回 None），不炸。
    """
    try:
        # 延迟 import：core.load_config 依赖项目根，直接读文件更独立、不引入环
        home = Path.home() / ".brain-memory"
        cfg_path = home / "config.json"
        if not cfg_path.is_file():
            return None
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        emb = data.get("embed") if isinstance(data.get("embed"), dict) else {}
        fb = emb.get("fallback") if isinstance(emb.get("fallback"), dict) else {}
        if fb.get("enabled") is False:
            return None
        dims = int(fb.get("dims") or 256)
        if dims < 32 or dims > 4096:
            dims = 256
        return {"enabled": True, "dims": dims}
    except Exception:
        return None


def tokenize(text: str) -> list:
    """中文 2-gram + 英文/数字词。坏输入返回空表，绝不抛。"""
    try:
        t = str(text or "").lower()
        toks = []
        for seg in _CJK.findall(t):
            if len(seg) == 1:
                toks.append(seg)
            for i in range(len(seg) - 1):
                toks.append(seg[i:i + 2])
        toks.extend(_WORD.findall(t))
        return toks
    except Exception:
        return []


def encode(text: str, dims: int = 256) -> list | None:
    """文本 → L2 归一化 hash 嵌入向量；空文本/坏输入返回 None。"""
    try:
        dims = int(dims)
        toks = tokenize(text)
        if not toks or dims <= 0:
            return None
        tf = {}
        for tk in toks:
            tf[tk] = tf.get(tk, 0) + 1
        vec = [0.0] * dims
        for tk, c in tf.items():
            h = hashlib.md5(tk.encode("utf-8", "ignore")).hexdigest()
            bucket = int(h[:8], 16) % dims
            sign = 1.0 if int(h[8], 16) % 2 == 0 else -1.0
            vec[bucket] += sign * (1.0 + math.log(c))
        norm = math.sqrt(sum(x * x for x in vec))
        if norm <= 0:
            return None
        return [round(x / norm, 4) for x in vec]
    except Exception:
        return None


def cosine(a: list, b: list) -> float:
    """余弦相似度；维度不符/坏输入返回 0.0（向量已归一化，可直接内积）。"""
    try:
        if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(x * x for x in b)) or 1.0
        return max(0.0, min(1.0, dot / (na * nb)))
    except Exception:
        return 0.0
