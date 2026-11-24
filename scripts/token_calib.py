# -*- coding: utf-8 -*-
"""水位估算标定（盈不足/双假设法）的查看与交叉验证。

模型：`tokens = chars / charsPerToken + systemOverheadTokens`
两个未知数。宿主给出真实 token 时，cron 插件会记一组 `(chars, tokens)` 样本，
并用最小二乘同时反推 ratio 与 overhead（写在 `<记忆库>/token-calib.json`）。

本模块做两件事：
  1. `show()` —— 把样本与插件写下的拟合结果摊开给人看；
  2. `refit()` —— **用同一套公式独立算一遍**，与插件的拟合值对账。
     两边不一致就说明有一边写错了（这是给"JS 侧算法没法单元测试"补的保险）。

只读为主：`refit(write=True)` 才会写 `token-calib.json`。
"""
from __future__ import annotations

import json
from pathlib import Path

import core

SAMPLES = "token-calib.jsonl"
FIT = "token-calib.json"
MIN_SPAN = 2000          # 样本字符跨度下限（跨度太小解不稳定）
MAX_SAMPLES = 500


def _paths():
    home = Path(core.HOME)
    return home / SAMPLES, home / FIT


def load_samples(limit: int = MAX_SAMPLES) -> list:
    sp, _ = _paths()
    if not sp.is_file():
        return []
    out = []
    try:
        for line in sp.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if isinstance(o, dict) and o.get("chars", 0) > 0 and o.get("tokens", 0) > 0:
                out.append({"chars": float(o["chars"]), "tokens": float(o["tokens"]),
                            "at": int(o.get("at") or 0)})
    except OSError:
        return []
    return out[-limit:]


def fit(samples: list) -> dict | None:
    """最小二乘 y = a + b·x（x=字符数，y=token）。与插件 _fitFrom 同公式。"""
    n = len(samples)
    if n < 2:
        return None
    xs = [s["chars"] for s in samples]
    ys = [s["tokens"] for s in samples]
    if max(xs) - min(xs) < MIN_SPAN:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0:
        return None
    b = sxy / sxx
    a = my - b * mx
    if b <= 0:
        return None
    ratio = 1 / b
    if not (0.5 <= ratio <= 5):
        return None
    if not (0 <= a <= 200000):
        return None
    return {"charsPerToken": round(ratio, 3),
            "systemOverheadTokens": int(round(a)), "samples": n}


def stored_fit() -> dict | None:
    _, fp = _paths()
    if not fp.is_file():
        return None
    try:
        j = json.loads(fp.read_text(encoding="utf-8"))
        return j if isinstance(j, dict) else None
    except Exception:
        return None


def refit(write: bool = False) -> dict:
    """独立重算一遍，并与插件写下的值对账。"""
    samples = load_samples()
    mine = fit(samples)
    theirs = stored_fit()
    agree = None
    if mine and theirs:
        try:
            agree = (abs(float(theirs.get("charsPerToken") or 0) - mine["charsPerToken"]) < 0.01
                     and abs(int(theirs.get("systemOverheadTokens") or 0)
                             - mine["systemOverheadTokens"]) <= 50)
        except Exception:
            agree = False
    if write and mine:
        _, fp = _paths()
        try:
            core.atomic_json(fp, {**mine, "source": "python-refit"})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "fit": mine, "stored": theirs}
    return {"ok": True, "samples": len(samples), "fit": mine,
            "stored": theirs, "agree": agree}


def render(rep: dict | None = None) -> str:
    rep = rep or refit()
    lines = []
    n = rep.get("samples") or 0
    lines.append(f"水位标定样本：{n} 组（模型 tokens = chars / ratio + overhead）")
    if n < 2:
        lines.append("  样本不足（至少 2 组、且字符跨度 ≥2000 才有解）。")
        lines.append("  样本由 cron 插件在\"宿主给出真实 token\"时自动记录，正常使用会慢慢攒。")
    fitd = rep.get("fit")
    if fitd:
        lines.append(f"  独立重算：charsPerToken={fitd['charsPerToken']}  "
                     f"overhead={fitd['systemOverheadTokens']}  n={fitd['samples']}")
    elif n >= 2:
        lines.append("  有样本但跨度不够或拟合越界，暂不用（保守起见退回配置默认）。")
    stored = rep.get("stored")
    if stored:
        lines.append(f"  插件已写入：charsPerToken={stored.get('charsPerToken')}  "
                     f"overhead={stored.get('systemOverheadTokens')}  "
                     f"n={stored.get('samples')}")
        if rep.get("agree") is False:
            lines.append("  ⚠ 两边结果不一致——说明有一侧算错，需要查（用 --refit 以 Python 为准重写）。")
        elif rep.get("agree") is True:
            lines.append("  ✓ 两边一致")
    else:
        lines.append("  插件还没写过拟合值（会退回配置默认 charsPerToken/overhead）。")
    lines.append("")
    lines.append("默认配置：charsPerToken=2、systemOverheadTokens=18000（都可在 config.json 覆盖）")
    return "\n".join(lines)
