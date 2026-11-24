# -*- coding: utf-8 -*-
"""面板「设置」页的配置清单与校验（白名单）。

为什么单独一个模块：面板要能改开关/数值，但**绝不能任意写 config.json**——
配错一个键就能让注入崩掉或把路径指飞。这里只列"安全且常用"的键，带类型/范围/
枚举校验，面板和 API 都走这一份，避免两处各写一套校验。
"""
from __future__ import annotations

import json
from typing import Any

import core

# group 顺序即面板显示顺序；kind ∈ bool / int / float / choice
CONFIG_SCHEMA: list[dict[str, Any]] = [
    # —— 注入与预算 ——
    {"key": "budget_tokens", "group": "注入与预算", "label": "注入 token 预算",
     "kind": "int", "min": 100, "max": 20000, "step": 100,
     "hint": "每次 startup 注入的记忆块上限；调大=记得更多，调小=更省上下文"},
    {"key": "inject_max_chars", "group": "注入与预算", "label": "单条记忆注入字符上限",
     "kind": "int", "min": 80, "max": 5000, "step": 20,
     "hint": "一条记忆在注入里最多显示多少字，超出截断"},
    {"key": "inject_mode", "group": "注入与预算", "label": "注入模式",
     "kind": "choice", "choices": ["full", "lazy"],
     "hint": "full=带正文；lazy=只带要点+id，正文交给 recall 现取（更省 token）"},
    {"key": "remember_max_chars", "group": "注入与预算", "label": "单条记忆原文上限",
     "kind": "int", "min": 200, "max": 8000, "step": 100,
     "hint": "remember 写入时超过就截断，只影响写入上限"},

    # —— 水位与节奏 ——
    {"key": "review_threshold", "group": "水位与节奏", "label": "清点线",
     "kind": "float", "min": 0.1, "max": 0.95, "step": 0.05,
     "hint": "上下文用到这个比例就该补记清点（默认 0.6）"},
    {"key": "dream_threshold", "group": "水位与节奏", "label": "做梦线",
     "kind": "float", "min": 0.1, "max": 0.99, "step": 0.05,
     "hint": "到这条线建议 dream 压缩（默认 0.75）"},
    {"key": "escape_threshold", "group": "水位与节奏", "label": "封存线",
     "kind": "float", "min": 0.1, "max": 1.0, "step": 0.05,
     "hint": "到这条线先 capture 原样封存保底（默认 0.9）"},
    {"key": "mood_inject_per_day", "group": "水位与节奏", "label": "每日心情注入上限",
     "kind": "int", "min": 0, "max": 50, "step": 1,
     "hint": "一天最多在注入里提醒几次心情；0=不提醒"},
    {"key": "mood_inject_change_delta", "group": "水位与节奏", "label": "心情有变化才注入",
     "kind": "bool", "hint": "开=只在心情明显变化时提醒，避免每次都念一遍"},

    # —— 记忆行为 ——
    {"key": "emotion_auto_extract", "group": "记忆行为", "label": "对话后自动评价情绪",
     "kind": "bool", "hint": "封存时顺手写一条情绪事件（强度封顶，不猜）"},
    {"key": "session_state_cache", "group": "记忆行为", "label": "会话状态缓存",
     "kind": "bool", "hint": "库没变时复用上次注入文本，跨天首轮更快"},
    {"key": "session_state_ttl", "group": "记忆行为", "label": "缓存有效期（秒）",
     "kind": "int", "min": 30, "max": 86400, "step": 30,
     "hint": "缓存多久内有效，过期重新生成"},
    {"key": "dialogue_ingest", "group": "记忆行为", "label": "导入其它 AI 的对话",
     "kind": "bool", "hint": "把 Claude/Codex 等会话记录摄取进记忆库"},
    {"key": "dialogue_ingest_auto", "group": "记忆行为", "label": "自动导入（无需手动触发）",
     "kind": "bool", "hint": "开启后按需自动摄取，注意会消耗处理时间"},
    {"key": "context_ab.enabled", "group": "记忆行为", "label": "上下文 A/B 对照",
     "kind": "bool", "hint": "抽样记录不同注入策略的效果，用于调参"},
    {"key": "model_tier", "group": "记忆行为", "label": "模型档位",
     "kind": "choice", "choices": ["auto", "small", "large"],
     "hint": "small=关掉高风险自动能力（小模型建议）；auto=按需"},

    # —— 向量与重排 ——
    {"key": "embed.timeout_sec", "group": "向量与重排", "label": "向量请求超时（秒）",
     "kind": "int", "min": 3, "max": 120, "step": 1,
     "hint": "本地 Ollama 冷启动较慢，调大可减少误降级"},
    {"key": "embed.fail_cooldown_sec", "group": "向量与重排", "label": "失败冷却（秒）",
     "kind": "int", "min": 30, "max": 3600, "step": 30,
     "hint": "连不上向量服务后，多久内不再重试（直接走降级）"},
    {"key": "embed.fallback.enabled", "group": "向量与重排", "label": "降级向量兜底",
     "kind": "bool", "hint": "没有真嵌入时用零依赖哈希向量接管"},
    {"key": "rerank.top_n", "group": "向量与重排", "label": "重排候选数",
     "kind": "int", "min": 3, "max": 100, "step": 1,
     "hint": "送进 rerank 的候选条数，越大越准也越慢"},

    # —— 面板与宿主 ——
    {"key": "panel_idle_minutes", "group": "面板与宿主", "label": "空闲自动退出（分钟）",
     "kind": "int", "min": 0, "max": 1440, "step": 5,
     "hint": "关掉网页后，多少分钟没请求就自动退出服务（默认 1）；0=永不退出（需重启面板生效）"},
    {"key": "panel_port", "group": "面板与宿主", "label": "面板端口",
     "kind": "int", "min": 1024, "max": 65535, "step": 1, "restart": True,
     "hint": "改完需要重启面板才生效"},
    {"key": "extensions_enabled", "group": "面板与宿主", "label": "启用扩展模块",
     "kind": "bool",
     "hint": "扩展是任意本地 Python 代码，等于完全信任本机——只装你自己写的"},

    # —— 自动更新与备份 ——
    {"key": "auto_update", "group": "自动更新与备份", "label": "自动更新灵枢",
     "kind": "bool",
     "hint": "开启后定期检查新版本并自动更新；更新前会先备份记忆库，失败自动回滚"},
    {"key": "auto_update_interval_hours", "group": "自动更新与备份",
     "label": "检查更新间隔（小时）", "kind": "int", "min": 1, "max": 720, "step": 1,
     "hint": "每隔多久查一次新版本（默认 24 小时）"},
    {"key": "backup_interval_hours", "group": "自动更新与备份", "label": "自动备份间隔（小时）",
     "kind": "int", "min": 1, "max": 168, "step": 1,
     "hint": "每隔多久自动备份一次记忆库（默认 6 小时；原来是每天一次）"},
    {"key": "backup_min_new_records", "group": "自动更新与备份", "label": "新增记录触发阈值",
     "kind": "int", "min": 0, "max": 10000, "step": 10,
     "hint": "新增这么多条记录就补一次备份（0=只看时间间隔）"},
    {"key": "backup_keep_hourly", "group": "自动更新与备份", "label": "保留：最近几份",
     "kind": "int", "min": 1, "max": 100, "step": 1,
     "hint": "小时层保留份数；最新一份永不删"},
    {"key": "backup_keep_daily", "group": "自动更新与备份", "label": "保留：每天几份",
     "kind": "int", "min": 0, "max": 90, "step": 1,
     "hint": "天层保留（每天最多留 1 份，这里填天数）"},
    {"key": "backup_keep_weekly", "group": "自动更新与备份", "label": "保留：每周几份",
     "kind": "int", "min": 0, "max": 52, "step": 1,
     "hint": "周层保留（每周最多留 1 份，这里填周数）"},
]

_BY_KEY = {x["key"]: x for x in CONFIG_SCHEMA}


def _get_path(cfg: dict, key: str, default=None):
    node: Any = cfg
    for seg in str(key).split("."):
        if not isinstance(node, dict) or seg not in node:
            return default
        node = node[seg]
    return node


def _default_of(key: str):
    return _get_path(core.DEFAULT_CONFIG, key)


def all_items() -> list[dict]:
    """面板用：每项的元数据 + 当前值 + 默认值。"""
    cfg = core.load_config()
    out = []
    for spec in CONFIG_SCHEMA:
        item = dict(spec)
        item["value"] = _get_path(cfg, spec["key"], _default_of(spec["key"]))
        item["default"] = _default_of(spec["key"])
        out.append(item)
    return out


def coerce(spec: dict, raw) -> Any:
    """按 schema 校验并转换一个值；不合法抛 ValueError（消息可直接给用户看）。"""
    kind = spec["kind"]
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"{spec['label']} 是开关，只接受 true/false")
    if kind in ("int", "float"):
        try:
            num = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{spec['label']} 需要是数字")
        if kind == "int":
            if num != int(num):
                raise ValueError(f"{spec['label']} 需要是整数")
            num = int(num)
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and num < lo:
            raise ValueError(f"{spec['label']} 不能小于 {lo}")
        if hi is not None and num > hi:
            raise ValueError(f"{spec['label']} 不能大于 {hi}")
        return num
    if kind == "choice":
        s = str(raw).strip()
        if s not in spec["choices"]:
            raise ValueError(f"{spec['label']} 只能是 {'/'.join(spec['choices'])}")
        return s
    raise ValueError("未知的配置类型")


def set_value(key: str, raw) -> Any:
    """校验后写盘，返回写入后的值。未知键直接拒绝（白名单）。"""
    spec = _BY_KEY.get(str(key))
    if not spec:
        raise KeyError(f"不允许从面板修改这个配置：{key}")
    val = coerce(spec, raw)
    core.config_set(key, json.dumps(val, ensure_ascii=False))
    return val
