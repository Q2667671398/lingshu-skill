# -*- coding: utf-8 -*-
"""growth.py 的纯计算部分（从 growth.py 拆出）。

拆分约束（与 mem_score.py / mem_embed.py 同一套）：
  - 只放纯计算：入参是文本/数字/dict，不读盘、不写盘、不依赖 HOME；
  - 需要时间/数值/分词时仍走 brain 命名空间（brain.now_ts / brain._num /
    brain.fmt_date / brain.DAY / brain.terms）——与搬迁前调用的是同一批函数对象，
    语义零变化；
  - brain 不在导入期 import：`import growth` 时 growth.py 末尾的 `from growth_parts
    import *` 可能发生在 brain 初始化过程中（core._recompute_paths 会 import growth），
    导入期再 import brain 会成环并让星号导入拿到半初始化命名空间；所以只在函数调用期
    延迟 import，本模块也不 import growth；
  - growth.py 末尾 `from growth_parts import *`，旧调用面照旧可用。
"""
from __future__ import annotations

import re

# 星号导入必须显式列出下划线开头/常量名（growth.py 末尾的 `from growth_parts import *` 依赖它）
__all__ = [
    # 通用文本与数值
    "_split", "_dedupe", "_clip",
    # 技能名与 SKILL.md 纯文本处理
    "SLUG_RE", "_WINDOWS_RESERVED", "_SKILL_NAME_ALIASES", "_SLUG_TO_LEGACY_NAME",
    "_require_slug", "parse_frontmatter", "_compose",
    # 版本 / 时长 / 连续天数
    "_ver", "_fmt_ago", "_streak",
    # 知识淡出时间窗口
    "FADE_ACTIVE_DAYS", "FADE_DORMANT_DAYS", "_FADE_PENALTY", "_FADE_MARK",
    "_protected_knowledge", "knowledge_fade_phase",
    # 知识文档词袋与生成物识别
    "_kdoc", "_LEGACY_AGENTS_SIGNATURE", "_looks_generated",
]


# ---------------------------------------------------------------- 通用文本与数值（纯计算，无 IO）

def _split(text: str | None) -> list:
    if not text:
        return []
    return [x.strip() for x in re.split(r"[、,，;；\n/|]+", text) if x.strip()]


def _dedupe(items: list) -> list:
    out, seen = [], set()
    for x in items:
        x = str(x).strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _clip(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= n else text[:n].rstrip() + "…"


# ---------------------------------------------------------------- 技能名与 SKILL.md 文本处理（纯字符串，不碰盘）

# 技能名：小写字母/数字/连字符/下划线，也允许中文（中文生态里技能就叫「质检评审」这类名字）
SLUG_RE = re.compile(r"^[a-z0-9\u4e00-\u9fff][a-z0-9-_\u4e00-\u9fff]{0,62}$")
# Windows 保留设备名：即使过了正则也不能当技能目录名（con/com1 建目录会失败或异常）
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul",
                     *(f"com{i}" for i in range(1, 10)),
                     *(f"lpt{i}" for i in range(1, 10))}


# 中文显示名 → kebab-case 目录名。DSH 的文件系统技能发现只认 kebab-case 的 name，
# 中文名会被静默跳过（装完像"改名"或干脆不出现），所以装/查/删都统一折算成英文 slug。
_SKILL_NAME_ALIASES = {
    "灵枢": "lingshu",
    "灵枢-lite": "lingshu-lite",
    "灵枢-tiny": "lingshu-tiny",
}
# 反查：kebab slug → 它对应的旧中文目录名（旧版安装会把中文名当目录名）
_SLUG_TO_LEGACY_NAME = {v: k for k, v in _SKILL_NAME_ALIASES.items()}


def _require_slug(name: str) -> str:
    # 统一归一化：大小写、空格、下划线都折算成连字符，保证 new / show / rm 用同一个名字都能找到
    name = (name or "").strip().lower().replace(" ", "-").replace("_", "-")
    name = _SKILL_NAME_ALIASES.get(name, name)   # 中文名折算成 kebab-case（DSH 才认）
    if not SLUG_RE.match(name):
        raise ValueError("技能名不合法：只能用字母/数字/连字符/下划线/中文，1-63 个字符")
    if name in _WINDOWS_RESERVED:   # con/com1 这类设备名在 Windows 上建目录会失败
        raise ValueError(f"技能名不能用 Windows 保留设备名：{name}")
    return name


def parse_frontmatter(text: str) -> tuple:
    """解析 YAML frontmatter。纯标准库，不引 yaml 依赖。

    支持：单行 key: value、引号包裹、块标量（| 保留换行 / > 折叠为空格）、
    以及常见简写（值里含冒号、以 # 开头）。够读 DSH / Claude / Trae 各家的 SKILL.md。
    """
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text
    # 闭界必须是整行 "---"（行尾可带空白）：裸 find("\n---") 会把正文里
    # "---xxx" 这类行误当边界，导致 head 被提前截断
    m = re.search(r"\n---[ \t]*(?:\n|$)", text)
    if m is None or m.start() < 3:
        return {}, text
    end = m.start()
    head = text[3:end]
    body = text[m.end():].lstrip("\n")

    meta = {}
    lines = head.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        if val in ("|", ">", "|-", ">-", "|+", ">+"):
            # 块标量：收集后续缩进行
            block, j = [], i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip() and not nxt.startswith((" ", "\t")):
                    break
                block.append(nxt.strip())
                j += 1
            sep = "\n" if val.startswith("|") else " "
            meta[key] = sep.join(x for x in block if x)
            i = j
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        meta[key] = val
        i += 1
    return meta, body


def _compose(name: str, description: str, body: str, version: int, created: str, updated: str,
             origin: str = "灵枢") -> str:
    description = re.sub(r"\s+", " ", description).strip()
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"origin: {origin}\n"
        f"version: {version}\n"
        f"created: {created}\n"
        f"updated: {updated}\n"
        "---\n\n"
        + body.strip() + "\n"
    )


# ---------------------------------------------------------------- 版本 / 时长 / 连续天数换算（纯计算）

def _ver(v) -> object:
    """版本号容错：自建技能是自增整数，本体是语义化版本（2.11.2）。
    直接 int('2.11.2') 会抛 ValueError 把 skill list 整个搞崩，这里退化成原串。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return str(v or "1").strip() or "1"


def _fmt_ago(secs: float) -> str:
    import brain  # 延迟导入：导入期成环风险见模块 docstring
    secs = max(0.0, secs)  # 系统时钟回拨/last 写了未来时间时会出现负数，钳为 0
    if secs < 3600:
        return f"{secs / 60:.0f} 分钟"
    if secs < brain.DAY:
        return f"{secs / 3600:.1f} 小时".replace(".0", "")
    return f"{secs / brain.DAY:.1f} 天".replace(".0", "")


def _streak(days: set) -> int:
    import brain  # 延迟导入：导入期成环风险见模块 docstring
    n = 0
    for i in range(0, 3650):
        d = brain.fmt_date(brain.now_ts() - i * brain.DAY)
        if d in days:
            n += 1
        elif i == 0:
            continue
        else:
            break
    return n


# ---------------------------------------------------------------- 知识淡出时间窗口（按 updated 距今换算相位，纯计算）

# 知识淡出分层（借鉴 Mnemosyne/大肥鱼 memory_policy 的 detail→gist→dormant 三相）：
# 分层不写入、不删原文，检索/展示时按 updated 距今实时计算相位——
#   active  活跃：30 天内动过（更新或验证），正常检索；
#   fading  淡出：30–180 天，检索降权，正文展示截短（细节淡出，概要与来源保留）；
#   dormant 休眠：>180 天，默认检索跳过；关键词仍能命中但只给标题线索，
#           看原文走 knowledge show <id>（对应"ID 可查原始证据"）。
# 豁免（始终 active，对应 protected）：pinned 置顶、E4/E3 已验证铁律——
# 这些条目代表"重要或反复被证实"，不该因为没人提就沉底。
FADE_ACTIVE_DAYS = 30
FADE_DORMANT_DAYS = 180
_FADE_PENALTY = 0.03   # fading 相位检索降权：淡出的结论该让位给新鲜说法

_FADE_MARK = {"active": "🌱", "fading": "🍂", "dormant": "💤"}


def _protected_knowledge(rec: dict) -> bool:
    return bool(rec.get("pinned")) or rec.get("evidence_level") in ("E3", "E4")


def knowledge_fade_phase(rec: dict, now: float | None = None) -> str:
    """计算一条知识的淡出相位：active / fading / dormant。只读，不写盘。"""
    import brain  # 延迟导入：导入期成环风险见模块 docstring
    if _protected_knowledge(rec):
        return "active"
    ts = brain._num(rec.get("updated"), 0) or brain._num(rec.get("created"), 0)
    if ts <= 0:
        return "active"   # 时间不明的条目不推测新鲜程度（借鉴 memory_policy 对 unknown 的处理）
    days = (now if now is not None else brain.now_ts()) - ts
    if days < FADE_ACTIVE_DAYS * 86400:
        return "active"
    if days < FADE_DORMANT_DAYS * 86400:
        return "fading"
    return "dormant"


# ---------------------------------------------------------------- 知识文档词袋与生成物识别（纯字符串）

def _kdoc(rec: dict) -> set:
    import brain  # 延迟导入：导入期成环风险见模块 docstring
    # 字段可能是 None（手改/LLM 产出），统一转成字符串再切词，避免 " ".join 抛 TypeError
    def _s(x):
        return str(x) if x is not None else ""
    return brain.terms(" ".join([
        _s(rec.get("title")) * 2, _s(rec.get("body")),
        " ".join(_s(p) for p in (rec.get("points") or [])),
        " ".join(_s(t) for t in (rec.get("tags") or [])),
    ]))


# 旧版（无区块标记）生成文件的可识别签名：命中说明整文件都是灵枢生成的，没有用户手写内容
_LEGACY_AGENTS_SIGNATURE = "由灵枢在全局化时生成"


def _looks_generated(text: str) -> bool:
    """旧版无标记文件是否「整份都是灵枢生成的」——只有这样才能安全整体替换。
    任何一行不属于生成结构，就说明用户手写过，必须保留。"""
    if _LEGACY_AGENTS_SIGNATURE not in text:
        return False
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if (s.startswith("#") or s.startswith("**何时用**") or s.startswith("**怎么用**")
                or s.startswith("**入口**") or s.startswith("**来源**") or s.startswith("**版本**")
                or _LEGACY_AGENTS_SIGNATURE in s):
            continue
        return False
    return True
