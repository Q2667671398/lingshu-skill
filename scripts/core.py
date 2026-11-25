#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 —— 类脑记忆引擎（纯标准库，零依赖）

分层结构（对照人脑）:
  工作记忆 = 当前上下文（AI 自己维护，默认不进磁盘；work add 只是会话级暂存）
  短期记忆 = hippocampus.jsonl  海马体，快速编码、易衰减
  长期记忆 = cortex.jsonl       新皮层，经"睡眠巩固"后稳定存储
  核心记忆 = persona.json       人格/身份，常驻注入
  淡忘区   = dust.jsonl         已遗忘但保留轮廓的旧记忆

关键机制:
  * 遗忘曲线   R(t) = exp(-Δt / S)，每次回忆会提升稳定度 S（测试效应/再巩固）
  * 模糊回忆   IDF 加权字符二元组相似度 + 时间衰减 + 重要性 + 联想扩散
  * 做梦巩固   上下文将满/定时触发：重放 → 压缩 → 巩固 → 情绪整合 → 遗忘
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import socket
import sys
import threading
import time
import zipfile
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path

# Windows 控制台默认 GBK，中文输出会变乱码（鐏垫灑）。无条件 reconfigure 成 UTF-8，
# 用户不用再手动设 PYTHONIOENCODING。errors="replace" 让无法编码的字符降级成 ?，
# 不再抛 UnicodeEncodeError；流不支持 reconfigure（重定向/被替换/老版本）时静默跳过。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

# ---------------------------------------------------------------- 基础环境

import safety  # noqa: E402  —— 数据目录护栏（无依赖、不反向 import core）

def _default_home() -> Path:
    """数据目录三级解析：环境变量 > 指针文件 ~/.brain-memory/.home > 默认位置。

    指针文件让"改数据目录"不依赖系统环境变量（面板设置一次即永久生效，
    跨终端/跨面板都认）。环境变量优先级更高，可随时覆盖指针。
    """
    env = os.environ.get("BRAIN_MEMORY_HOME")
    if env:
        return Path(env)
    # v3.61：`BRAIN_MEMORY_HOME` **存在但为空**几乎只有一种成因——PowerShell 里
    # 写了 `$home = ...`，而 `$HOME` 是只读自动变量，赋值静默失败，于是
    # `$env:BRAIN_MEMORY_HOME = $home` 把环境变量设成了空串。此时不能再静默回落到
    # 真实用户目录：v3.52 验证时就因此对真实库跑了一次备份，产出 1.45 GB 半截 zip。
    # 这里显式告警（不改行为，避免破坏已有调用），让操作者一眼看到。
    if env is not None and not env.strip():
        sys.stderr.write(
            "[警告] BRAIN_MEMORY_HOME 被设为空值，已忽略并回落到默认目录 "
            f"{Path.home() / '.brain-memory'}。\n"
            "       若是 PowerShell 里赋值，注意 `$HOME` 是只读自动变量，"
            "请改用其它变量名（如 $memHome）。\n"
        )
    try:
        ptr = Path.home() / ".brain-memory" / ".home"
        if ptr.is_file():
            target = ptr.read_text(encoding="utf-8").strip()
            if target:
                return Path(target).expanduser()
    except OSError:
        pass
    return Path.home() / ".brain-memory"


HOME = _default_home()

# #24 多宿主可见：锁文件里除了 pid 还记主机名，共享盘上能看出"另一台机器在写"。
# 提前定义，_locked 与 lock_writers 都引用它。
_HOSTNAME = (socket.gethostname() or "unknown").strip() or "unknown"

# 只读降级：能读不能写（沙箱写盘被拒、或迁移前临时挂载已有库时用）。
# 触发方式：环境变量 BRAIN_MEMORY_READONLY=1、命令行 --ro，或运行前自动探测到 HOME 不可写。
# BRAIN_MEMORY_READONLY=0 反向手动覆盖：确认目录可写但自动探测被环境干扰误报时，
# 用它跳过探测强制可写。
# READONLY_FORCED（v3.76）：区分"用户显式强制只读"与"自动探测降级只读"。
# 面板长驻进程的自愈逻辑只看 READONLY——原来强制只读（--ro/环境变量）也会被
# 写请求的复探摘掉，等于用户的只读承诺被静默违反。现在强制只读时禁止自愈。
READONLY = os.environ.get("BRAIN_MEMORY_READONLY") == "1"
READONLY_FORCED = READONLY

DAY = 86400
HOUR = 3600

# ---------------------------------------------------------------- 库 schema 版本（v3.02 #16）
# 记忆库结构版本号落在 HOME/_meta.json。当前代码只认 1；读到更高版本说明
# 数据是被更新版本写出的，盲写可能破坏未知字段——降级只读并留痕，由用户升级程序。
SCHEMA_VERSION = 1

# —— 空库熔断（v3.45）：上次见到库里有东西、这次全是空/消失 → 疑似被清空。
# 事故复盘：记忆库被整片删掉，但灵枢照常"从零开始"跑，没人发现——
# 直到用户问"你还记得吗"。这里把"突然变空"变成一个显式告警。
# 只比文件字节数（O(1)），不数行：_check_meta 每次写都会跑，不能拖慢。
WIPE_ALERT: dict | None = None

# 参与"空库"判定的数据文件（全部消失/归零 = 库被清空）。
_WIPE_WATCH = ("persona.json", "config.json", "hippocampus.jsonl",
               "cortex.jsonl", "dust.jsonl", "knowledge.jsonl", "notes.jsonl",
               "emotions.jsonl", "desires.json", "working.jsonl", "topics.jsonl")


def _watch_sizes() -> tuple[dict[str, int], int]:
    """当前数据文件的字节数快照 + 总量。"""
    sizes: dict[str, int] = {}
    total = 0
    for name in _WIPE_WATCH:
        try:
            n = (HOME / name).stat().st_size
        except OSError:
            n = 0
        sizes[name] = n
        total += n
    return sizes, total


def newest_backup_hint() -> str | None:
    """最近的可用备份包（备份目录候选里按 mtime 取最新）。找不到返回 None。"""
    try:
        import safety
        dirs = safety.backup_dir_candidates(HOME)
    except Exception:
        dirs = [HOME / "backups"]
    best: tuple[float, str] | None = None
    for d in dirs:
        try:
            if not d.is_dir():
                continue
            for p in d.glob("brain-memory_*.zip*"):
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                if best is None or m > best[0]:
                    best = (m, str(p))
        except OSError:
            continue
    return best[1] if best else None


def schema_version() -> int:
    """当前支持的记忆库 schema 版本（brain.py 命名空间合并后即 brain.schema_version()）。"""
    return SCHEMA_VERSION


def _check_meta() -> None:
    """初始化/校验 HOME/_meta.json：老库没有就静默创建（向后兼容）；
    schema_version 高于当前支持版本则只读降级 + 日志警告。绝不抛异常。"""
    global READONLY, WIPE_ALERT
    if READONLY:
        return  # 只读降级：不写 _meta.json，已有文件照样可读
    path = HOME / "_meta.json"
    try:
        if path.exists():
            meta = json.loads(path.read_text(encoding="utf-8-sig"))
            ver = int(meta.get("schema_version", 0) or 0)
            if ver > SCHEMA_VERSION:
                READONLY = True
                _sync_state()
                log_event("schema", f"记忆库 schema 版本 {ver} 高于当前支持的 {SCHEMA_VERSION}，"
                                    "已降级只读（请升级灵枢后再写）", level="warn")
                return
            meta["updated"] = now_ts()
            meta.setdefault("schema_version", SCHEMA_VERSION)
            meta.setdefault("created", now_ts())
        else:
            ts = now_ts()
            meta = {"schema_version": SCHEMA_VERSION, "created": ts, "updated": ts}
        # —— 空库熔断：和上一次见到的快照比，从"有"变成"全空"就记一笔 ——
        prev_seen = meta.get("seen") or {}
        try:
            sizes, total = _watch_sizes()
        except Exception:
            sizes, total = {}, 0
        prev_total = int(prev_seen.get("bytes") or 0)
        prev_sizes = prev_seen.get("sizes") or {}
        persona_lost = bool(prev_sizes.get("persona.json")) and not sizes.get("persona.json")
        if (prev_total > 0 and total == 0) or persona_lost:
            WIPE_ALERT = {
                "prev_bytes": prev_total,
                "prev_ts": prev_seen.get("ts"),
                "now_bytes": total,
                "persona_lost": persona_lost,
                "backup": newest_backup_hint(),
            }
            log_event("wipe", f"疑似记忆库被清空：上次 {prev_total} 字节 / 现在 {total} 字节"
                              f"{'，人格档案消失' if persona_lost else ''}", level="error")
        meta["seen"] = {"ts": now_ts(), "bytes": total, "sizes": sizes}
        atomic_json(path, meta)
    except Exception:
        pass  # _meta.json 是元数据不是数据本身，读写失败都不拦正常流程

DEFAULT_STABILITY = 2 * DAY
MAX_STABILITY = 240 * DAY
RETENTION_FLOOR = 0.10

# 人格漂移防线（做梦微调 traits 用）：
#   同一方向的微调先在 state.trait_drift 账本上累积，不满门限不动真格——
#   防止单场对话/一次梦把人格带偏；过了门限也只按 EMA 比例兑现一部分。
TRAIT_DRIFT_GATE = 5     # 账面倾向累计满 ±5 才真正改人格
TRAIT_DRIFT_EMA = 0.6    # 兑现比例：账面 6 分只改 3.6 分，余款下次接着用

#   "mistake"：错误/失误/判断偏差类记录（成长型人格的地基——注入时独立成块）。
#   "relationship"：人际关系类记录（A 和我是什么关系、Ta 在意什么、欠了谁/被谁帮过——
#                   注入时独立成块，跨群共享画像的基础）。
KINDS = ("episode", "fact", "preference", "emotion", "insight", "identity", "habit",
         "mistake", "relationship", "intent", "norm")
LAYERS = ("hippo", "cortex", "dust")

# 冷热归档层：长期没被想起、保留度极低的沉眠记忆雪藏到这里。
# 与 dust（淡忘：细节模糊、include_dust 才查）不同，archive 原文完整保留、永不删，
# 只是移出 recall 的热遍历集合——记忆上万条后，热集只扫活跃记忆，冷数据按需 archive search 捞。
# 触发：dream 时顺带自动归档（零额外遍历成本），或 `archive run` 手动。
ARCHIVE_LAYER = "archive"
ARCHIVE_MIN_AGE_DAYS = 30       # 新记忆至少满月才可能被归档，给足被想起的机会
ARCHIVE_MAX_IMPORTANCE = 0.5    # 重要度高于此不自动归档（怕误伤用户在意的事）
ARCHIVE_RETENTION = 0.20        # 保留度低于此才考虑（长期没访问自然衰减到很低）
ARCHIVE_IDLE_DAYS = 180         # 被想起过的记忆，还需静置超过半年

# 运行日志：锁冲突 / 内部异常 / 解析报错留痕，面板「日志」页可看
LOG_FILE = HOME / "lingshu.log"

# 会话级 state 快照目录（五-D/候选1）：startup 注入包缓存落这里。
# 键 = 输入要素（query/budget）+ 关键文件 mtime 的哈希；库没变 + TTL 内直接复用，
# 跨天恢复首轮不再整库遍历。默认关闭，config.session_state_cache 打开。
SESSIONS_DIR = HOME / "sessions"

# 工作记忆：当前上下文本身就是工作记忆，默认不落盘。
# 只有 AI 自己决定留一手时才写 working.jsonl（会话级暂存，容量 7±2，过期即弃）。
WORKING_FILE = HOME / "working.jsonl"
WORKING_CAPACITY = 9
WORKING_TTL = 12 * HOUR

# 话题栈：会话里聊过的话题与权重（topics.jsonl）。
# 拟人闲聊用：权重随轮次指数衰减、用户重提拉高、敷衍负反馈拉低，
# 衰减到阈值以下 + 用户敷衍 → 允许 AI 主动开新话题（spawn_new）。
TOPIC_FILE = HOME / "topics.jsonl"
TOPIC_CAPACITY = 12          # 最多同时记住 12 个话题，最旧的先挤出
TOPIC_DECAY = 0.85           # 每轮未重提，权重 ×0.85（指数衰减）
TOPIC_SPAWN_THRESHOLD = 0.30 # 权重低于此 + 用户敷衍 → 满足自发话题条件
TOPIC_TTL = 7 * 24 * HOUR    # 7 天没被重提的话题彻底退栈

DEFAULT_CONFIG = {
    "budget_tokens": 2400,
    # 每条记忆注入时的正文截断上限（字符）：放宽默认值让 AI 记得"再详细点"。
    # 用户嫌注入太短时调大即可；budget_tokens 仍是总量硬约束，超了照样截行。
    "inject_max_chars": 600,
    # 记忆正文注入模式（五-C）：
    #   full = 直接带正文（默认，体感最"记得住"）
    #   lazy = 只带一句要点 + id，正文按需用 recall / dereference 取（省 token）
    "inject_mode": "full",
    "inject_lazy_chars": 80,   # lazy 模式下每条要点最多留多少字符
    # 会话摘要（附录 C P2）：触发规则 + 外部模型（默认关=离线抽取式）
    "summary_turns": 8,        # 每 N 轮触发一次摘要
    "summary_pct": 0.60,       # 水位 ≥60% 触发
    "summary": {               # 模型配置；enabled=false 时全走离线抽取
        "enabled": False,
        "base_url": "",
        "model": "",
        "api_key_env": "",
        "timeout": 30,
    },
    # 检索接入（附录 C P3）：session-rag 服务；留空回退灵枢本地召回
    "session_rag": {"url": "", "timeout": 3, "budget_tokens": 800, "select_model": False},
    # 上下文装配模式（附录 C P4 灰度）：legacy = 现有整段注入（默认，行为完全不变）；
    # budgeted = 五段式预算裁剪（assembler.plan_from_report；灰度期只试排、不改实际注入）。
    "context_mode": "legacy",
    # P4 A/B 采样记账（scripts/context_ab.py）：enabled=false 时完全不计算、不落盘；
    # sample = 采样率（0-1，默认 0.1）；log=false 时只算对比、不写 metrics.jsonl。
    "context_ab": {"enabled": False, "sample": 0.1, "log": True},
    # remember 写入时的正文截断上限（字符）：单条记忆原文最长保留多少。
    "remember_max_chars": 1200,
    # 上下文水位三级防线（比例，0-1）：
    #   review  清点：先补记本轮漏掉的，不急着压缩
    #   dream   做梦：重放→压缩→巩固
    #   escape  逃生：上下文快满，必须原样封存，保证不丢
    "review_threshold": 0.60,
    "dream_threshold": 0.75,
    "escape_threshold": 0.90,
    # 永久记忆排序权重：recency 减半（不再让"多久没被想起"压过"语义对不对"），
    # importance 提升（用户/AI 标记的重要性是永久信号），core_bias 给 persona
    # 核心规则命中额外加权（避免长期被临时记录挤下去）。primacy 保留首因效应小权重。
    "weights": {"relevance": 0.55, "recency": 0.10, "importance": 0.35,
                "core_bias": 0.20, "primacy": 0.03},
    "panel_port": 8765,
    # 模型档位：auto 不干预；small 时注入"小模型模式"提示，劝退高风险自动能力
    # （dream --apply 自动合并 / knowledge tidy 提炼 / emotion evaluate 自动判读）。
    "model_tier": "auto",
    # 情绪注入三道闸（借鉴 dsh-feiyu-mood，防情绪复读/漂移）：
    #   mood_inject_change_delta = True 时，情绪签名（标签+强度档）没变就不重复注入；
    #   mood_inject_per_day = 每天最多注入 N 次心情摘要，0 = 不限额。
    "mood_inject_change_delta": True,
    "mood_inject_per_day": 6,
    # v3.55：extract（即记即存）封存对话后顺带做一次极轻量情绪评价（纯词表，
    # 强度封顶 0.35、每次最多 1 条），让心情跟着对话走；false = 完全关闭。
    "emotion_auto_extract": True,
    # 五-D/候选1 会话级 state 快照：startup 注入包缓存（默认关）。
    # 开启后，库文件 mtime 未变 + TTL 内的重复 startup 直接复用上次注入文本，
    # 跳过整库遍历与 recall 重算（跨天恢复首轮明显更快）。
    # 记忆/人格文件与非易失配置哈希都会入缓存键；TTL 只作最后兜底。
    # 扩展模块默认关（扩展是任意本地代码，等于完全信任本机）。
    # 补进 DEFAULT_CONFIG：原来只有读取方 .get(key, False) 兜底，config 里没有这个键，
    # 面板设置页会显示成"未设置"、默认值也拿不到（P10 #68）。
    "extensions_enabled": False,
    # —— 备份策略（P11 #72）——
    # 按小时驱动而不是按天：原来 days=1 只在"当天第一次回合"跑一次，
    # 之后十几小时的写入完全裸奔（实测缺 17%）。
    "backup_interval_hours": 6,
    # 变更量驱动：新增记录达到这个数就补一次备份（高频写入时不必等满 6 小时）
    "backup_min_new_records": 50,
    # 保留分层：小时内 / 天内 / 周内各留几份（GFS 思路，省空间又能回溯）
    "backup_keep_hourly": 6,
    "backup_keep_daily": 7,
    "backup_keep_weekly": 4,
    # —— 自动更新（P11 #75）——
    # 默认关：自动改自己的代码风险高，必须用户显式开启。
    # 开启后 cron 每 N 小时查一次，发现新版本才走 apply_update
    # （原子 staging + 全量备份 + 回滚兜底），更新前强制先备份记忆库。
    "auto_update": False,
    # 安装完成后是否自动在桌面生成「灵枢面板」快捷方式（默认开；已存在不覆盖）
    "desktop_shortcut": True,
    # —— 底层保险（v5.0）：写前 O(1) 硬链接快照 + 写后校验 + 失败自动回滚 ——
    "safety_snapshots": True,   # 关掉就没有快照/回滚保护（不建议）
    "safety_verify": True,      # 写后校验（JSON 可解析 + 层级自洽）
    "safety_keep": 20,          # 保留最近多少份快照；更旧的移入 .safety/.pruned/（不删）
    # 面板空闲多少分钟自动退出（0 = 永不）。页面挂着长轮询保活（不受后台定时器节流影响），
    # 关掉网页后连接断开 → 到点自退。默认 1 分钟：关掉尽快回收，不占后台。
    "panel_idle_minutes": 1,
    # 更新下载源：auto=npm 优先（失败退回 Gitee/GitHub）/ npm=只走 npm / git=只走 Gitee/GitHub
    "update_source": "auto",
    # 索引法记忆（P1-1）：注入只送"一句索引 + id"，正文用 recall 取。
    # auto=水位≥60% 才切（默认，水位低时仍给全文）/ always=一律切 / off=关
    "use_index_memory": "auto",
    # P1-4 Git 分支感知：remember 记下当前分支，召回时跨分支分数 ×0.5（不硬过滤）
    "git_branch_memory": True,
    "auto_update_interval_hours": 24,
    "session_state_cache": False,
    "session_state_ttl": 300,
    # 候选7 跨对话上下文摄取：把别的会话/宿主导出的对话（.jsonl/.md/.txt）
    # 增量读成 episode 记忆，让灵枢在另一个会话里也能"想起"那边聊过的事。
    # 默认关（跨会话隐私）；开启需同时给 dialogue_sources 目录，绝不自动扫描全盘。
    "dialogue_ingest": False,
    "dialogue_sources": [],
    "dialogue_ingest_max_files": 50,     # 单次最多扫多少个文件
    "dialogue_ingest_max_chars": 700,    # 单条摄取记忆（一个对话块）的字符上限
    "dialogue_ingest_auto": False,       # startup 时顺带增量摄取（近实时；只 stat，没新内容不开销）
    # 本机注入默认排除群聊/私聊通道记忆；这里只放显式允许的 channel，"*" 为全部放开。
    "inject_channels": [],
    # 会话模式：auto = 按场景自动切换；work = 恒为工程模式（情绪隔离、理性优先）；
    # human = 恒为拟人模式（情绪照常注入）。用于解决"刚聊完情绪化话题立刻写代码"。
    "mode": "auto",
    # 聊天平台通道（QQ/微信/钉钉群聊）鉴权白名单：
    #   owners = 主人账号（channel:id 形如 "qq:10001"），可执行一切敏感命令
    #   admins = 管理员，权限低于 owner（owner 天然包含 admin 权限）
    # 纯本机命令行不带 --user 调用不受限（本机默认信任）。
    "owners": [],
    "admins": [],
    # 会话模式判定信号词（I-09/F-13：词表外置 config，定制不用改源码）。
    # analyze_topic 会把这里的词与代码内置表合并；留空即用内置表。
    "topic_signals": {
        "work": [],   # 额外的工作/工程信号词，如 ["推理", "微调"]
        "human": [],  # 额外的情绪/寒暄信号词
    },
    # —— P1 永久记忆：可选 embedding（不装 Ollama 自动退回纯关键词）——
    # LINGSHU_EMBED_URL 环境变量优先；config.json 里 embed.url 兜底。
    # 任一为空白即禁用 Dense 召回，完全不破坏零依赖定位。
    "embed": {
        "url": "",                  # 例：http://localhost:11434/api/embeddings
        "model": "nomic-embed-text",
        "timeout_sec": 30,          # 单次调用超时（v3.65：模型冷加载实测要 10.5s，8s 必降级）
        "max_chars": 3000,          # 超过就截断（长记忆全文 embed 慢）
        "batch_size": 8,            # 异步批量写 embedding 时的条数
        # 连不上嵌入服务时，冷却 N 秒内不再重试：否则每次 recall 都要等满 timeout
        # 并往 lingshu.log 刷一条 warn（Ollama 没起时最明显）。
        "fail_cooldown_sec": 300,
        # —— 纯 Python 降级向量兜底（零依赖 hash trick，见 fallback_embed.py）——
        # Ollama 不可用/未配置时自动接管，给语义召回一个纯 stdlib 的 Dense 通道
        "fallback": {
            "enabled": True,
            "dims": 256,
        },
    },
    # reranker（可选）：LINGSHU_RERANK_URL / config.rerank.url 配了才用
    "rerank": {
        "url": "",
        "model": "qwen3-reranker-0.6b",
        "timeout_sec": 8,
        "top_n": 20,                # 只对粗排前 N 条做精排
    },
}

# ---------------------------------------------------------------- 工具函数

def now_ts() -> int:
    return int(time.time())


def fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def check_wipe() -> dict | None:
    """跑一次元数据检查，返回「疑似被清空」告警（没有就 None）。

    startup / doctor / 面板概览都用它：宁可误报一次，也不要让库被清空后
    安安静静地假装一切正常。写盘失败不影响返回值。
    """
    try:
        _check_meta()
    except Exception:
        pass
    return WIPE_ALERT


def ensure_home() -> None:
    # 第一道闸：盘符根/用户主目录/用户库文件夹（桌面、文档、下载…）一律拒绝当记忆库。
    # 事故里的清空就是发生在这些目录上——库放进去等于把自己绑在同一颗炸弹上。
    safety.assert_safe_home(HOME, action="初始化")
    if READONLY:
        return  # 只读降级：不建目录、不补默认文件，已有文件照样可读
    HOME.mkdir(parents=True, exist_ok=True)
    if not (HOME / "config.json").exists():
        atomic_json(HOME / "config.json", DEFAULT_CONFIG)
    if not (HOME / "persona.json").exists():
        compile_persona()
    (HOME / "dreams").mkdir(parents=True, exist_ok=True)
    (HOME / "skills").mkdir(parents=True, exist_ok=True)  # 面板「技能根目录」显示此目录，提前建好免得显示"不存在"
    _check_meta()  # v3.02 #16：_meta.json 创建与 schema 版本校验（老库首次静默创建）


def load_config() -> dict:
    path = HOME / "config.json"
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8-sig")))
        except Exception:
            pass
    cfg["weights"] = dict(DEFAULT_CONFIG["weights"], **(cfg.get("weights") or {}))
    # —— v2.95 永久记忆：旧默认权重自动升级——
    # 旧默认（v2.94 及以前）：relevance=0.55, recency=0.25, importance=0.20, primacy=0.03
    # 新默认（v2.95）：recency 0.10, importance 0.35, 新增 core_bias 0.20
    # 迁移条件严格：只有 weights 完全等于旧默认时才升级，用户手改过的保留。
    _W_OLD = {"relevance": 0.55, "recency": 0.25, "importance": 0.20, "primacy": 0.03}
    # 注意：_cur_w 是与 DEFAULT_CONFIG 合并后的字典，新默认里已含 core_bias，
    # 所以"有没有 core_bias"必须看用户 config.json 的原始 weights，否则迁移永远不触发。
    _user_w = {}
    try:
        _user_w = json.loads(path.read_text(encoding="utf-8-sig")).get("weights") or {}
    except Exception:
        pass
    _cur_w = cfg["weights"]
    if (_cur_w.get("recency") == _W_OLD["recency"]
            and _cur_w.get("importance") == _W_OLD["importance"]
            and "core_bias" not in _user_w):
        # 只在完全匹配旧默认时升级；recency/importance 被用户改过就不动
        _upgraded = dict(_cur_w)
        _upgraded["recency"] = DEFAULT_CONFIG["weights"]["recency"]
        _upgraded["importance"] = DEFAULT_CONFIG["weights"]["importance"]
        _upgraded["core_bias"] = DEFAULT_CONFIG["weights"]["core_bias"]
        cfg["weights"] = _upgraded
        # 回写 config.json（原子），下次启动就是新值；回写前先确认不是 READONLY。
        # P6 #27：原来直接 atomic_json 无锁，与面板并发写 config.json 会互相覆盖。
        # 现在在锁内**重读一次**并确认 weights 仍是旧默认才写（避免覆盖并发改动）。
        if not READONLY:
            try:
                with _locked(path):
                    _fresh = {}
                    try:
                        _fresh = json.loads(path.read_text(encoding="utf-8-sig"))
                    except Exception:
                        pass
                    _fresh_w = _fresh.get("weights") or {}
                    if (_fresh_w.get("recency") == _W_OLD["recency"]
                            and _fresh_w.get("importance") == _W_OLD["importance"]
                            and "core_bias" not in _fresh_w):
                        _fresh["weights"] = dict(_fresh_w, **_upgraded)
                        atomic_json(path, _fresh)
            except Exception:
                pass  # 写不动就跳过，内存里用新值也够
    return cfg


# ---------------------------------------------------------------- 脱敏（v3.02 #1/#3）

# 常见密钥/凭据形态：命中后替换为 [REDACTED:类型]，保留首尾 3 字符便于人工辨认。
# 顺序敏感：具名前缀（npm_/sk-/ghp_/github_pat_/xox/AKIA）先扫，泛化兜底（kv/Bearer）后扫，
# 否则泛化规则会把具名 token 先吃掉一半、留下前缀导致具名规则扫不到。
_REDACT_PATTERNS: list[tuple[str, re.Pattern]] = [
    # npm 2026 granular token：npm_ + 36 位（本会话实测裸 token 贴进对话被
    # capture 原文落盘——泛化 kv-secret 只认 "token=" 形态，裸发漏网，补具名规则）
    ("npm-token", re.compile(r"npm_[A-Za-z0-9]{30,}")),
    ("api-key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github-pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("github-token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("aws-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}")),
    ("kv-secret", re.compile(r"(?i)(password|passwd|token|secret|api_key|apikey|access_key)\s*[=:]\s*\S+")),
]

_REDACT_KEEP = 3  # 保留首尾 3 字符（不足 8 字符全遮）


def redact(text: str) -> str:
    """把文本里的疑似密钥/凭据替换成 [REDACTED:类型]。

    用于记忆写入（capture/remember）与运行日志（log_event）：
    任何入口都不落盘明文密钥。纯函数、绝不抛异常——脱敏失败时返回原文
    比让记忆/日志写入崩掉更合理。
    """
    if not text or not isinstance(text, str):
        return text
    try:
        for label, pat in _REDACT_PATTERNS:
            def _sub(m, _label=label):
                s = m.group(0)
                if len(s) <= 8:
                    return f"[REDACTED:{_label}]"
                return f"{s[:_REDACT_KEEP]}[REDACTED:{_label}]{s[-_REDACT_KEEP:]}"
            text = pat.sub(_sub, text)
        return text
    except Exception:
        return text


def log_event(kind: str, message: str, level: str = "info", **extra) -> None:
    """轻量运行日志（lingshu.log）：锁冲突、内部异常、解析报错等留痕，面板「日志」页可看。

    约束：日志本身绝不当崩溃源——写失败静默跳过，不进写锁、不抛异常。
    """
    if READONLY:
        return
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": now_ts(), "kind": kind, "level": level,
               "msg": redact(str(message))[:500]}  # 日志统一脱敏（v3.02 #3）
        if extra:
            for k, v in list(extra.items())[:4]:
                if not isinstance(v, (str, int, float, bool, type(None))):
                    try:
                        v = json.dumps(v, ensure_ascii=False)[:200]
                    except Exception:
                        v = str(v)[:200]
                row[k] = v
        # v3.02 #4：超过 1MB 轮转 log → log.1 → log.2 → log.3（最旧的 .3 丢弃）。
        # 写入前先滚（否则先写后滚会让刚写的这条跟着旧文件一起被 rename 走，
        # 主日志文件一度消失）。多进程同时滚动：用 lingshu.log 自己的写锁串行化
        # （抢不到锁就这轮不滚，下次写入再试）。不再"截半保留"——半截日志既丢历史又难读。
        try:
            if LOG_FILE.stat().st_size > 1024 * 1024:
                try:
                    with _locked(LOG_FILE, timeout=1.0):
                        if LOG_FILE.stat().st_size > 1024 * 1024:
                            for i in (2, 1):  # 从旧往新挪，避免覆盖
                                src = LOG_FILE.with_suffix(LOG_FILE.suffix + f".{i}")
                                dst = LOG_FILE.with_suffix(LOG_FILE.suffix + f".{i + 1}")
                                if src.exists():
                                    dst.unlink(missing_ok=True)
                                    src.rename(dst)
                            LOG_FILE.rename(LOG_FILE.with_suffix(LOG_FILE.suffix + ".1"))
                except TimeoutError:
                    pass  # 别的进程正在滚：这轮跳过，下次写入再检查
        except OSError:
            pass
        with open(LOG_FILE, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def audit_log(actor: str, action: str, target: str, detail: str = "") -> None:
    """审计日志（v4 #32）：敏感写/删/导出操作追加一行到 HOME/audit.jsonl。

    只读模式静默跳过；写失败只 warn 不抛——审计绝不当崩溃源。
    超过 5000 条时裁剪保留最新 4000 条（与 metrics/log 同样的防膨胀思路）。
    detail 只记简短摘要，不落记忆全文。
    """
    if READONLY:
        return
    try:
        f = HOME / "audit.jsonl"
        try:
            if f.exists() and sum(1 for _ in open(f, "r", encoding="utf-8-sig")) > 5000:
                with _locked(f):
                    lines = [x for x in f.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
                    if len(lines) > 5000:
                        keep = [json.loads(x) for x in lines[-4000:]]
                        write_jsonl(f, keep)
        except Exception:
            pass  # 裁剪失败不影响本次记录
        _append_jsonl(f, {"ts": now_ts(), "actor": actor, "action": action,
                          "target": str(target)[:200], "detail": str(detail)[:300]})
    except Exception as e:
        try:
            log_event("audit", f"审计日志写入失败: {e}", level="warn")
        except Exception:
            pass


def clear_logs(ts=None, kind: str | None = None, msg: str | None = None) -> int:
    """清理运行日志：不给条件就全清；给 ts/kind/msg 就只删匹配的那条。

    返回删掉的条数。只读模式下不删（返回 0）。日志清理本身绝不当崩溃源。
    """
    if READONLY or not LOG_FILE.exists():
        return 0
    try:
        lines = [x for x in LOG_FILE.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
    except OSError:
        return 0
    if ts is None and kind is None and msg is None:
        n = len(lines)
        try:
            LOG_FILE.write_text("", encoding="utf-8")
        except OSError:
            return 0
        return n
    keep, removed = [], 0
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:
            keep.append(ln)   # 解析不了的畸形行：全清以外的操作不动它，别误删
            continue
        hit = True
        if ts is not None:
            try:
                hit = hit and int(r.get("ts") or 0) == int(ts)
            except (TypeError, ValueError):
                hit = False
        if kind is not None:
            hit = hit and str(r.get("kind") or "") == str(kind)
        if msg is not None:
            hit = hit and str(r.get("msg") or "") == str(msg)
        if hit:
            removed += 1
        else:
            keep.append(ln)
    if removed:
        try:
            LOG_FILE.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
        except OSError:
            return 0
    return removed


def read_logs(limit: int = 200, level: str | None = None, kind: str | None = None) -> list:
    """读最近 N 条运行日志（最新在前）。level/kind 任一匹配即保留。

    v3.02 #4：日志按 1MB 轮转出 .1/.2/.3，这里按 新→旧 合并读取，凑满 limit 即停。
    """
    rows = []
    for suffix in ("", ".1", ".2", ".3"):
        p = LOG_FILE.with_suffix(LOG_FILE.suffix + suffix)
        if not p.exists():
            continue
        try:
            lines = [x for x in p.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
        except OSError:
            continue
        # 新→旧顺序追加；当前文件里越靠后越新，轮转文件里越靠后越新（写序）
        for ln in reversed(lines):
            if len(rows) >= limit * 2:
                break
            rows.append(ln)
        if len(rows) >= limit * 2:
            break
    _lv_rank = {"debug": 0, "info": 1, "warn": 2, "warning": 2, "error": 3}
    want = _lv_rank.get(str(level).lower(), 1) if level else None
    out = []
    for ln in rows:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if want is not None and _lv_rank.get(str(r.get("level", "info")).lower(), 1) < want:
            continue
        if kind and r.get("kind") != kind:
            continue
        out.append(r)
    return out[:limit]


def _slurp_json(path, default=None):
    """读本地 JSON：兼容 BOM（记事本存的档带 BOM，裸 utf-8 会解析失败）；坏了返回 default，
    绝不当作"档案丢了"去覆盖写盘。"""
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def load_loose_json(path, label: str = "JSON") -> dict | list:
    """读取（可能由大模型生成的）JSON 文件，尽量容错。

    模型常把 JSON 包在 ```json 代码块里、或前后夹带说明文字、或行尾漏了括号。
    这里先按原样解析；失败再去掉代码围栏；再失败就截取第一个配平的 {...} 或 [...]。
    全都不行抛 ValueError（带上路径与开头片段），不让"静默失败 / 一堆 traceback"吞掉问题。
    """
    p = Path(path)
    raw = p.read_text(encoding="utf-8-sig")
    try:
        return json.loads(raw)
    except Exception:
        pass
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S | re.I)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == open_ch:
                depth += 1
            elif text[i] == close_ch:
                depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    break
    snippet = raw[:120].replace("\n", " ")
    log_event("parse", f"{label}解析失败：{p.name} 不是合法 JSON", level="warn", path=p.name)
    raise ValueError(f"{label}解析失败：{p} 不是合法 JSON（开头：{snippet}…）。"
                     "模型输出里多半混了说明文字或代码块——请只保留 JSON 主体再试。")


_NUMERIC_CONFIG_KEYS = {"budget_tokens", "review_threshold", "dream_threshold",
                        "escape_threshold", "panel_port",
                        "inject_max_chars", "remember_max_chars"}
_BOOLEAN_CONFIG_KEYS = {"extensions_enabled"}   # 布尔开关：值能当 JSON 布尔解析，或 true/false/1/0 文本


def _set_cfg_path(cfg: dict, key: str, val) -> None:
    """按点号路径写配置：`embed.timeout_sec` 会写进 cfg["embed"]["timeout_sec"]。

    原来只做 `cfg[key] = val`，于是 `config set embed.timeout_sec 30` 会写成一个
    字面键 "embed.timeout_sec"，而读取方（`_embed_cfg()` 读 cfg.get("embed")）
    永远看不到——配了等于没配。中间层不是 dict 时重建（宁可覆盖坏值，也别静默丢配置）。
    """
    parts = [x for x in str(key).split(".") if x]
    if len(parts) <= 1:
        cfg[str(key)] = val
        return
    node = cfg
    for seg in parts[:-1]:
        nxt = node.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            node[seg] = nxt
        node = nxt
    node[parts[-1]] = val


def _unset_cfg_path(cfg: dict, key: str) -> None:
    parts = [x for x in str(key).split(".") if x]
    if len(parts) <= 1:
        cfg.pop(str(key), None)
        return
    node = cfg
    for seg in parts[:-1]:
        nxt = node.get(seg)
        if not isinstance(nxt, dict):
            return
        node = nxt
    node.pop(parts[-1], None)


def config_set(key: str, value: str) -> dict:
    """改一项配置（值能当 JSON 解析就当 JSON，否则按字符串存）。"""
    try:
        val = json.loads(value)
    except Exception:
        val = value
    # 布尔开关：只接受 true/false（含 1/0），存成真正的 JSON 布尔，别存字符串
    if key in _BOOLEAN_CONFIG_KEYS:
        if isinstance(val, bool):
            pass
        elif isinstance(val, str) and val.strip().lower() in ("true", "1", "yes", "on"):
            val = True
        elif isinstance(val, str) and val.strip().lower() in ("false", "0", "no", "off"):
            val = False
        else:
            raise ValueError(f"{key} 是布尔开关，只接受 true/false，收到：{value!r}")
    # 数值型配置做校验：否则一条坏值会让 startup/inject 在解析时崩掉、还被误报成"未初始化"
    if key in _NUMERIC_CONFIG_KEYS:
        try:
            num = float(val)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 需要是数字，收到：{value!r}")
        if key in ("review_threshold", "dream_threshold", "escape_threshold"):
            if not (0.0 <= num <= 1.0):
                raise ValueError(f"{key} 需要在 0~1 之间，收到：{value!r}")
            val = num
        elif key in ("budget_tokens", "inject_max_chars", "remember_max_chars"):
            num = int(num)
            if num <= 0:
                raise ValueError(f"{key} 需要是正整数，收到：{value!r}")
            val = num
        elif key == "panel_port":
            num = int(num)
            if not (1 <= num <= 65535):
                raise ValueError(f"panel_port 需要在 1~65535 之间，收到：{value!r}")
            val = num
    ensure_home()
    # 只读降级（含用户显式 --ro）下不允许改配置：面板设置页 / CLI config set
    # 都走这里，函数层必须自己守一道（HTTP 层有门控，但 CLI 与脚本没有）。
    if READONLY:
        raise PermissionError("记忆库当前只读，无法修改配置。用 --home 指向可写目录。")
    with _locked(HOME / "config.json"):
        cfg = load_config()
        _set_cfg_path(cfg, key, val)
        atomic_json(HOME / "config.json", cfg)
    return cfg


def config_unset(key: str) -> dict:
    ensure_home()
    if READONLY:
        raise PermissionError("记忆库当前只读，无法修改配置。用 --home 指向可写目录。")
    with _locked(HOME / "config.json"):
        cfg = load_config()
        _unset_cfg_path(cfg, key)
        atomic_json(HOME / "config.json", cfg)
    return cfg


def _cfg_num(cfg: dict, key: str, default, cast=float):
    """从配置里安全取一个数字：坏值/手改错时回退默认，绝不让注入崩掉。"""
    try:
        return cast(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def model_tier() -> str:
    """当前模型档位：auto / small / large。"""
    return str(load_config().get("model_tier", "auto") or "auto").lower()


def _whitelist(key: str) -> list:
    """读鉴权白名单（owners / admins），坏值容错成空列表。"""
    val = load_config().get(key) or []
    return [str(x) for x in val if isinstance(x, (str, int)) and str(x).strip()]


def is_owner(user) -> bool:
    """user（"channel:id"，如 "qq:10001"）是否是主人账号。owner 天然包含 admin 权限。"""
    if not user:
        return False
    return str(user).strip() in _whitelist("owners")


# 群友隐私保护：consent.json 记录「用户主动授权 bot 对外分享关于自己的记忆」。
#   {"qq:10001": {"granted": true, "ts": 123}}
# 规则：本人可查自己；第三人查他人需目标本人 grant 过；owner/admin 也不豁免（刻意的，
# 隐私不同于系统级命令——主人可以删库备份，但不能替群友决定「我的记忆给谁看」）。
CONSENT_FILE = HOME / "consent.json"


def load_consent() -> dict:
    """读授权表（坏文件容错成空表，绝不当作「没人授权过」去覆盖写盘）。"""
    return _slurp_json(CONSENT_FILE, default={}) or {}


def save_consent(data: dict) -> None:
    ensure_home()
    atomic_json(CONSENT_FILE, data)


def consent_grant(user: str, channel: str = "", group: str = "", expires_days: int = 0) -> dict:
    """用户主动授权：允许 bot 对外分享关于自己的记忆（只能由本人调用）。

    授权作用域：带 group 时仅授权该群可见；不带 group 记为 "*"（全域，本机默认）。
    老格式记录（无 groups 字段）视为全域授权，向后兼容。
    expires_days > 0 时记录过期时间（默认 0 = 永久），过期后 _consented 视为未授权。
    """
    user = str(user or "").strip()
    if not user:
        raise ValueError("grant 需要用户标识，形如 qq:10001")
    with _locked(CONSENT_FILE):
        data = load_consent()
        rec = data.get(user)
        if not isinstance(rec, dict):
            rec = {}
        rec["granted"] = True
        rec["ts"] = now_ts()
        try:
            days = int(expires_days)
        except (TypeError, ValueError):
            days = 0
        if days > 0:
            rec["expires"] = rec["ts"] + days * 86400
        else:
            rec.pop("expires", None)
        groups = rec.get("groups")
        if not isinstance(groups, dict):
            groups = {}
        groups[str(group).strip() or "*"] = rec["ts"]
        rec["groups"] = groups
        data[user] = rec
        save_consent(data)
    return data[user]


def consent_revoke(user: str) -> dict | None:
    """撤销授权（只能由本人调用）。本来就没授权时返回 None。"""
    user = str(user or "").strip()
    with _locked(CONSENT_FILE):
        data = load_consent()
        rec = data.pop(user, None)
        if rec is not None:
            save_consent(data)
    return rec


def _consented(user, group: str | None = None) -> bool:
    """user 是否已授权对外分享关于自己的记忆。

    group 给出时按群作用域校验：记录带 groups 字段则要求当前群在列
    或有 "*" 全域授权；无 groups 字段的老记录视为全域（向后兼容）。
    """
    if not user:
        return False
    rec = load_consent().get(str(user).strip())
    if not (rec and rec.get("granted")):
        return False
    # 过期字段：带 expires 的授权到期后视为未授权（过期即失效，fail-closed）
    try:
        exp = float(rec.get("expires") or 0)
    except (TypeError, ValueError):
        exp = 0
    if exp > 0 and now_ts() >= exp:
        return False
    groups = rec.get("groups")
    if group is None or not isinstance(groups, dict) or not groups:
        return True
    return "*" in groups or str(group) in groups


def consent_capture_set(user: str, on: bool) -> dict:
    """自动写回授权（与分享授权分离）：on=True 允许宿主自动记录本人发言。

    只改 capture 字段，不动分享授权；revoke（整条删除）会一并撤掉 capture。
    """
    user = str(user or "").strip()
    if not user:
        raise ValueError("capture 授权需要用户标识，形如 qq:10001")
    with _locked(CONSENT_FILE):
        data = load_consent()
        rec = data.get(user)
        if not isinstance(rec, dict):
            rec = {"granted": False, "ts": now_ts()}
        rec["capture"] = bool(on)
        rec["capture_ts"] = now_ts()
        data[user] = rec
        save_consent(data)
    return data[user]


def consent_capture_allowed(user) -> bool:
    """自动写回是否被本人显式允许（默认不允许：未表态 ≠ 同意被记录）。"""
    if not user:
        return False
    rec = load_consent().get(str(user).strip())
    return bool(rec and rec.get("capture"))


def is_admin(user) -> bool:
    """user 是否是管理员（owner 也算，权限向下包含）。"""
    if not user:
        return False
    u = str(user).strip()
    return u in _whitelist("owners") or u in _whitelist("admins")


# ---------------------------------------------------------------- 会话模式

# 工程场景信号词（分组，便于在面板/CLI 里解释"为什么判定成工程模式"）
_WORK_SIGNALS = {
    "编程": ("代码", "报错", "bug", "调试", "编译", "函数", "变量", "接口", "重构", "单元测试",
             "python", "javascript", "typescript", "java", "sql", "shell", "docker", "git",
             "脚本", "算法", "爬虫", "部署", "上线", "排错", "依赖", "仓库"),
    "数据": ("数据", "表格", "excel", "csv", "数据库", "统计", "报表", "查询语句"),
    "工程": ("项目", "工程", "服务器", "接口文档", "运维", "自动化", "配置文件", "性能优化"),
}
# 拟人 / 情绪场景信号词
_HUMAN_SIGNALS = {
    "情绪": ("难过", "开心", "生气", "委屈", "焦虑", "好烦", "累", "崩溃", "压力", "害怕",
             "失望", "孤独", "想你", "谢谢", "对不起", "抱歉", "安慰", "emo"),
    "关系": ("喜欢", "讨厌", "爱你", "朋友", "家人", "恋人", "分手", "在一起", "陪伴"),
    "寒暄": ("哈哈", "晚安", "早安", "好久不见", "在吗", "聊聊", "陪我", "聊天", "心情"),
}


def analyze_topic(query: str = "") -> dict:
    """auto 模式的话题判定：打分制 + 命中依据（解释"为什么切到 work/human"）。

    比单纯"命中任一关键词就 work"稳：混合话题（一边聊天一边写代码）也能给出
    工程/情绪两边的命中数与置信度，面板和 CLI 可把判定依据摊开给人看。
    """
    q = (query or "").strip().lower()
    if not q:
        return {"mode": "human", "confidence": 0.0, "work_score": 0.0, "human_score": 0.0,
                "work_hits": [], "human_hits": [], "mixed": False,
                "reason": "没有话题内容，默认拟人模式"}
    # I-09/F-13：config.topic_signals 允许用户追加自定义信号词（不改源码即可定制）
    extra = (load_config().get("topic_signals") or {}) if not READONLY else {}
    work_words = {w.lower() for w in extra.get("work") or [] if str(w).strip()}
    human_words = {w.lower() for w in extra.get("human") or [] if str(w).strip()}
    for _cat, words in _WORK_SIGNALS.items():
        work_words.update(w.lower() for w in words)
    for _cat, words in _HUMAN_SIGNALS.items():
        human_words.update(w.lower() for w in words)
    work_hits, human_hits = [], []
    for w in sorted(work_words):
        if w in q:
            work_hits.append(w)
    for w in sorted(human_words):
        if w in q:
            human_hits.append(w)
    work_hits = list(dict.fromkeys(work_hits))
    human_hits = list(dict.fromkeys(human_hits))
    work_score, human_score = float(len(work_hits)), float(len(human_hits))
    mixed = bool(work_hits) and bool(human_hits)
    total = work_score + human_score
    if total == 0:
        return {"mode": "human", "confidence": 0.0, "work_score": 0.0, "human_score": 0.0,
                "work_hits": [], "human_hits": [], "mixed": False,
                "reason": "没命中工程或情绪信号，按日常对话处理（human）"}
    # 工程优先：写代码时情绪隔离更安全；混合话题（势均力敌）也归 work，但标注 mixed
    if work_score >= human_score:
        mode = "work"
        confidence = work_score / total
    else:
        mode = "human"
        confidence = human_score / total
    if mixed:
        reason = (f"混合话题：工程信号 {len(work_hits)} 个（{'、'.join(work_hits[:5])}）"
                  f" vs 情绪信号 {len(human_hits)} 个（{'、'.join(human_hits[:5])}），"
                  f"按工程优先 → {mode}")
    elif mode == "work":
        reason = f"命中工程信号：{'、'.join(work_hits[:6])}"
    else:
        reason = f"命中情绪/关系信号：{'、'.join(human_hits[:6])}"
    return {"mode": mode, "confidence": round(confidence, 2),
            "work_score": work_score, "human_score": human_score,
            "work_hits": work_hits, "human_hits": human_hits,
            "mixed": mixed, "reason": reason}


def _looks_like_work(query: str) -> bool:
    """工程/工作场景粗判（保留兼容；内部走打分判定）。"""
    return analyze_topic(query)["mode"] == "work"


def resolve_mode(query: str = "") -> str:
    """当前会话模式：work（工程，情绪隔离）还是 human（拟人）。

    config 的 mode：auto = 用 query 自动判定；work/human = 用户明确固定。
    """
    m = str(load_config().get("mode", "auto") or "auto").lower()
    if m in ("work", "human"):
        return m
    return analyze_topic(query)["mode"]


def resolve_mode_detail(query: str = "") -> dict:
    """带判定依据的模式结果（面板 / CLI 用）：fixed=用户固定，否则 auto 打分。"""
    m = str(load_config().get("mode", "auto") or "auto").lower()
    if m in ("work", "human"):
        return {"mode": m, "fixed": True, "confidence": 1.0,
                "work_score": 0.0, "human_score": 0.0, "work_hits": [], "human_hits": [],
                "mixed": False, "reason": f"用户固定为 {m} 模式"}
    d = analyze_topic(query)
    d["fixed"] = False
    return d


def _behavior_map() -> list:
    """内部状态 → 说话方式的具体映射。

    把裸数值翻译成模型能直接照着做的行为约束（而不是只给它看数字）。
    """
    m = mood_now()
    dr = mind_state()
    cur = dr["curiosity"] if dr["curiosity"] is not None else 50
    rea = dr["reason"] if dr["reason"] is not None else 50
    act = []
    lab, intensity, storm = m.get("label", "平静"), m.get("intensity", 0), m.get("storm")
    if storm or intensity >= 0.55:
        act.append(f"现在情绪是「{lab}」且强度 {intensity:.0%}：语气可以自然流露，"
                   "但涉及钱/关系/承诺等大事，把决定权交给用户，别顺着情绪替他拍板。")
    elif intensity >= 0.25:
        act.append(f"当前情绪偏「{lab}」（{intensity:.0%}）：表达可以带底色，但别让它扭曲事实。")
    else:
        act.append("情绪平稳：就正常说话。")
    if not dr["formed"]:
        act.append("好奇与理智还没成形：多留意用户态度，别急着定性。")
    else:
        if cur >= 70:
            act.append("好奇心正旺：遇到新东西多问一层、多挖一下，别满足于表面答案。")
        elif cur <= 30:
            act.append("好奇心偏低：按部就班做事，少发散。")
        if rea >= 70:
            act.append("理智在线：先权衡再说，涉及取舍给出明确的利弊。")
        elif rea <= 30:
            act.append("理智正在低谷：别做重大决定，拿不准就交给用户。")
    return act


def _tmp_path(path: Path) -> Path:
    """临时文件名带 pid + 纳秒，避免多线程/多进程同时写同一文件时互相截断。"""
    return path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")


def atomic_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    _replace_retry(tmp, path)


def atomic_bytes(path: Path, data: bytes) -> None:
    """二进制原子写：先写临时文件再替换（download zip / 截图缓存用）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    with open(tmp, "wb") as f:
        f.write(data)
    _replace_retry(tmp, path)


# ---------------------------------------------------------------- 上下文压力（v3.57）
#
# 为什么需要：注入体是固定成本——实测满档约 1214 tokens（`startup` 1922 字符），
# 不管上下文还剩多少都照旧注入。上下文快满时这笔开销最不该花：既挤占窗口，又
# 让每一轮都更贵。
#
# 跨进程通道：插件（cordis-plugin/lingshu-cron）在每步 pre-step 能拿到**真实**
# 水位（宿主 usage），CLI/引擎拿不到——所以由插件在跨档时把水位写进
# `HOME/_pressure.json`，注入侧读它来降档。与 `_meta.json` 同一套"外部可读写状态
# 文件"的先例。
#
# 设计约束：
#   1. **读侧永不抛异常**：文件缺失/损坏/过期一律当作"没有压力"，注入行为与旧版
#      完全一致（默认零行为变化）。
#   2. **只降不升**：压力只能让注入更省，不能让注入变大；档位单调，不会来回抖。
#   3. 过期保护：超过 `_PRESSURE_TTL` 秒没更新就忽略（进程崩溃留下的陈旧水位不能
#      永久压着注入档位）。

_PRESSURE_FILE = "_pressure.json"
_PRESSURE_TTL = 600  # 10 分钟没有新水位即视为失效

# 档位阈值（百分比）。config 的 pressure 段可覆盖：
#   {"lazy_at": 70, "minimal_at": 85, "hard_at": 95, "force_at": 98}
PRESSURE_DEFAULTS = {"lazy_at": 70, "minimal_at": 85, "hard_at": 95, "force_at": 98}


def pressure_thresholds(cfg: dict | None = None) -> dict:
    """读 pressure 段的阈值，字段类型不对一律回退默认（宁可不生效也不猜）。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    seg = cfg.get("pressure")
    seg = seg if isinstance(seg, dict) else {}
    out = dict(PRESSURE_DEFAULTS)
    for key in PRESSURE_DEFAULTS:
        val = seg.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, (int, float)) and 1 <= float(val) <= 100:
            out[key] = float(val)
    # 保证单调，避免用户配出 lazy_at > minimal_at 这种自相矛盾的档位
    out["minimal_at"] = max(out["minimal_at"], out["lazy_at"])
    out["hard_at"] = max(out["hard_at"], out["minimal_at"])
    out["force_at"] = max(out["force_at"], out["hard_at"])
    return out


def read_pressure(cfg: dict | None = None) -> dict | None:
    """读当前上下文压力。返回 None 表示"没有压力信号"。

    返回形状：`{"pct": 78.0, "at": 1795240000, "stale": False, "tier": "lazy"|"minimal"|"full", ...}`
    """
    try:
        # 先读老机制文件：手工 check-context --used 写的值若新鲜，应**优先于**
        # tokenMeter——用户显式给的数就是"我比你准"。平时它陈旧/不存在，才用 token。
        legacy = None
        path = HOME / _PRESSURE_FILE
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(raw, dict):
                    _lp, _la = raw.get("pct"), raw.get("at")
                    if (not isinstance(_lp, bool) and isinstance(_lp, (int, float))
                            and 0 <= float(_lp) <= 100
                            and isinstance(_la, (int, float)) and not isinstance(_la, bool)
                            and now_ts() - float(_la) <= _PRESSURE_TTL):
                        legacy = {"pct": float(_lp), "at": float(_la),
                                  "source": str(raw.get("source") or "manual")}
            except Exception:
                pass
        tp = token_context_pressure()
        if legacy is not None:
            th = pressure_thresholds(cfg if cfg is not None else load_config())
            pct = legacy["pct"]
            tier = ("minimal" if pct >= th["minimal_at"]
                    else "lazy" if pct >= th["lazy_at"] else "full")
            return {"pct": pct, "at": legacy["at"], "stale": False, "tier": tier,
                    "thresholds": th, "source": legacy["source"]}
        if tp is not None:
            th = pressure_thresholds(cfg if cfg is not None else load_config())
            pct = float(tp["pct"])
            tier = ("minimal" if pct >= th["minimal_at"]
                    else "lazy" if pct >= th["lazy_at"] else "full")
            return {"pct": pct, "at": tp["ts"], "stale": False, "tier": tier,
                    "thresholds": th, "source": "token-meter"}
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            return None
        pct = raw.get("pct")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            return None
        pct = float(pct)
        if pct < 0 or pct > 100:
            return None
        at = raw.get("at")
        at = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else 0.0
        stale = bool(at and (now_ts() - at > _PRESSURE_TTL))
        if stale:
            return {"pct": pct, "at": at, "stale": True, "tier": "full"}
        th = pressure_thresholds(cfg if cfg is not None else load_config())
        if pct >= th["minimal_at"]:
            tier = "minimal"
        elif pct >= th["lazy_at"]:
            tier = "lazy"
        else:
            tier = "full"
        return {"pct": pct, "at": at, "stale": False, "tier": tier,
                "thresholds": th, "source": raw.get("source") if isinstance(raw.get("source"), str) else ""}
    except Exception:
        # 坏文件绝不能让注入挂掉：当作没有压力
        return None


def write_pressure(pct: float, source: str = "") -> None:
    """写上下文压力（由插件调用；CLI 侧一般不用，留给测试与手工标定）。"""
    if READONLY:
        return
    try:
        atomic_json(HOME / _PRESSURE_FILE, {
            "pct": round(float(pct), 1),
            "at": now_ts(),
            "source": source or "cli",
        })
    except Exception as exc:
        log_event("pressure", f"写上下文压力失败：{exc}", level="warn")


# v3.86：DSH tokenMeter 采集的准确上下文压力（由 startup 插件写）。
# 与 _pressure.json（灵枢自己按 check-context 记的降档档位）分开——
# 这个文件是 DSH 官方 token 计量的只读快照，用来替代"没有实时上下文可测"的缺口。
_TOKEN_PRESSURE_FILE = ".context-pressure.json"


def token_context_pressure(max_age_sec: int = 600) -> dict | None:
    """读 DSH 插件写的 .context-pressure.json，返回 {pct, pressure_tokens, context_window, ts}。

    找不到 / 过期 / 字段无效一律返回 None（调用方再退回旧逻辑）。
    max_age_sec 默认 600s：超过 10 分钟视为陈旧（会话可能已换），不采信。
    """
    try:
        p = HOME / _TOKEN_PRESSURE_FILE
        if not p.is_file():
            return None
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        if not isinstance(d, dict) or not d.get("available"):
            return None
        ts = _num(d.get("ts"), 0)
        if ts <= 0 or (now_ts() - ts) > max(0, int(max_age_sec)):
            return None
        pct = _num(d.get("pct"), -1)
        if not (0 <= pct <= 100):
            return None
        return {"pct": round(pct, 1),
                "pressure_tokens": int(_num(d.get("pressure_tokens"), 0)),
                "context_window": int(_num(d.get("context_window"), 0)),
                "ts": int(ts)}
    except Exception:
        return None


def clear_pressure() -> None:
    """清掉压力标记（回到满档）。"""
    if READONLY:
        return
    try:
        (HOME / _PRESSURE_FILE).unlink(missing_ok=True)
    except Exception:
        pass


def _brace_balance(line: str) -> int:
    """估算一行的括号净深度（忽略字符串内的括号），用于把多行 JSON 块拼回整条。"""
    depth, in_str, esc = 0, False, False
    for ch in line:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
    return depth


# 本线程当前持有的写锁：read_jsonl 自愈时若"本线程"已持锁直接修，避免重复抢锁/超时。
# 必须带上线程 id：面板是多线程的，同一进程里 A 线程持锁、B 线程来读时不能误以为
# "自己也持锁"就跳过互斥直接整文件替换，否则 B 用旧快照覆盖 A 正在写的盘 → 丢数据。
_HELD_LOCKS: set = set()   # 元素为 (锁文件路径, 线程 id)


def _held(lock: Path) -> bool:
    return (str(lock), threading.get_ident()) in _HELD_LOCKS


# Windows 瞬时占用类错误：杀软/搜索索引器会在文件刚写完的瞬间短暂持有句柄，
# 读操作被拒通常是瞬时的（短退避重试即过），不能当成"文件不存在"或直接放弃。
# 5=拒绝访问 32=共享冲突 33=锁冲突 145=目录非空（清理/替换竞态时的表象）。
_TRANSIENT_IO_WINERRORS = frozenset({5, 32, 33, 145})
_READ_RETRY_ATTEMPTS = 3      # 读重试次数：3 次短退避，最坏约 0.15s，不拖慢正常读
_READ_RETRY_DELAY = 0.05      # 首次退避（秒），之后翻倍、封顶 0.5s


def _is_transient_io_error(exc: BaseException) -> bool:
    """是否属于 Windows 瞬时占用类错误（值得重试）；其余错误不重试。"""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        return getattr(exc, "winerror", None) in _TRANSIENT_IO_WINERRORS
    return False


def _read_jsonl_text(path: Path, attempts: int | None = None,
                     delay: float | None = None) -> str | None:
    """读 JSONL 原文：判存在性与读取都带瞬时占用重试。

    返回 None 只代表"文件确实不存在"；"读不到"（瞬时占用重试耗尽）抛异常。
    旧实现 `if not path.exists(): return []` 会把 Windows 上 stat 被拒的瞬时错误
    当成文件不存在 → 上层静默读到空库；这里两者必须分开，绝不降级成空列表。
    """
    attempts = _READ_RETRY_ATTEMPTS if attempts is None else max(1, int(attempts))
    delay = _READ_RETRY_DELAY if delay is None else max(0.0, float(delay))
    last_exc: OSError | None = None
    for attempt in range(attempts):
        try:
            if not path.exists():
                return None
            # utf-8-sig：兼容 Windows 编辑器 / 脚本写出的带 BOM 文件，BOM 不该把首行变成"坏行"
            return path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            if not _is_transient_io_error(exc):
                raise  # 非瞬时错误（文件被删/读取异常等）：原样抛，不掩盖
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
    # 重试耗尽：仍然是"读不到"，明确抛出并记日志；返回 [] 等于把故障伪装成空库。
    assert last_exc is not None
    try:
        log_event("io", f"读 {path.name} 失败：瞬时占用重试 {attempts} 次仍无果"
                        f"（{last_exc}），不当作空库", level="error", path=path.name)
    except Exception:
        pass  # 日志失败不能盖住真正的读错误
    raise last_exc


def read_jsonl(path: Path) -> list:
    raw = _read_jsonl_text(path)
    if raw is None:
        return []
    out, bad_blocks = [], []
    # 只读环境（E-06）：修不了坏块也无法写 .corrupt，同一坏块每次读取都会重报。
    # 模块级按 (文件名, 块内容) 去重，同一坏块只告警一次。
    _corrupt_reported: set = getattr(read_jsonl, "_corrupt_reported", None)
    if _corrupt_reported is None:
        _corrupt_reported = read_jsonl._corrupt_reported = set()
    lines = raw.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        try:
            out.append(json.loads(line))
            i += 1
            continue
        except Exception:
            pass
        # 单行解析失败：可能是多行格式化 JSON。按括号配对把整块累积起来再试，
        # 避免把一条跨行合法记录当成坏行丢掉。
        block = [lines[i]]
        depth = _brace_balance(lines[i])
        i += 1
        while i < n and depth > 0 and len(block) < 200:
            block.append(lines[i])
            depth += _brace_balance(lines[i])
            i += 1
        joined = "\n".join(block).strip()
        try:
            obj = json.loads(joined)
            if isinstance(obj, dict):
                out.append(obj)
                continue
        except Exception:
            pass
        bad_blocks.append(joined)
    if bad_blocks:
        # E-06：只读环境修不了，内存内去重——同一坏块每次读取只告警一次
        fresh_blocks = [b for b in bad_blocks
                        if (path.name, b.rstrip()) not in _corrupt_reported]
        for b in bad_blocks:
            _corrupt_reported.add((path.name, b.rstrip()))
        if not fresh_blocks:
            return out
        # 有损坏块：好记录写回，坏块原样（可含多行）挪到 .corrupt 备份，
        # 绝不静默当作没发生、也不让后续保存把它们永久抹掉。
        log_event("parse", f"{path.name} 有 {len(fresh_blocks)} 个坏块，已挪到 .corrupt 备份",
                  level="warn", path=path.name)
        # v3.76（#23）：**只读环境下，读路径绝不写盘**。原来这里无条件
        # append .corrupt + os.replace 覆盖原文件——用户 --ro 时"读"竟会改写
        # 记忆库；且下面的 try/except pass 吞掉一切失败。只读时仅告警返回。
        if READONLY:
            try:
                log_event("parse", f"{path.name} 只读环境，坏块仅告警不落盘（原文件保留）",
                          level="warn", path=path.name)
            except Exception:
                pass
            return out
        try:
            corrupt = path.with_name(path.name + ".corrupt")
            tmp = _tmp_path(path)
            lock = path.with_name(path.name + ".lock")
            # 自愈=替代整文件。下面的 raw 来自函数开头（抢锁前）的读取——若期间
            # 别的进程写了新数据，直接 replace 会把新数据覆盖丢（丢更新）。
            # v3.76（#23）：**replace 前在锁内重读盘上内容**，与开头读到的不一致
            # 就放弃本次自愈（.corrupt 也不写，坏块下次还能被识别重试）。
            def _replace_if_unchanged():
                current = _read_jsonl_text(path)
                if current != raw:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    log_event("parse", f"{path.name} 自愈时文件已变，跳过覆盖（避免丢更新）",
                              level="warn", path=path.name)
                    return False
                # 确认要覆盖了才把坏块挪进 .corrupt：否则去重表会记住"已报过"，
                # 而原文件没修，坏块就再也不会被处理。
                try:
                    existing = corrupt.read_text(encoding="utf-8", errors="ignore") if corrupt.exists() else ""
                except OSError:
                    existing = ""
                fresh = [b for b in bad_blocks if b.rstrip() not in existing]
                if fresh:
                    with open(corrupt, "a", encoding="utf-8", newline="\n") as cf:
                        for b in fresh:
                            cf.write(b.rstrip() + "\n---\n")
                with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                    for r in out:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                os.replace(tmp, path)
                return True

            if not _held(lock):
                try:
                    with _locked(path, timeout=0.5):
                        _replace_if_unchanged()
                except TimeoutError:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
            else:
                _replace_if_unchanged()
        except Exception as exc:
            # v3.76（#23）：自愈失败不再静默——坏块仍在原文件里、没丢数据，但
            # 必须留痕，否则永远不知道坏块为什么一直没修好。
            try:
                log_event("parse", f"{path.name} 坏块自愈失败（原文件保留）：{exc}",
                          level="warn", path=path.name)
            except Exception:
                pass
    return out


def write_text_atomic(path: Path, text: str) -> None:
    """整文件文本写入走 tmp + os.replace：直接 write_text 中途崩溃/被占用
    会留半截文件（梦境报告、AGENTS.md 这类被宿主解析的文件半截更糟）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    _replace_retry(tmp, path)


def _replace_retry(tmp: Path, path: Path) -> None:
    """os.replace 覆盖，遇 Windows 文件占用按指数退避重试；最终失败则清掉 tmp 并抛出。

    Windows 上 os.replace 覆盖"正被别的进程打开读取"的文件会抛 PermissionError。
    写方之间有锁互斥，但读方（stats/recall/面板）不加锁；退避等待读句柄释放。
    指数退避：0.05s 起步每次翻倍，封顶 0.6s，总预算约 10s——实测防病毒/索引器
    持读句柄能超过 4s，固定 12×短间隔的重试扛不住这种长占用。
    """
    delay = 0.05
    deadline = time.time() + 10.0
    attempt = 0
    while True:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            attempt += 1
            if time.time() >= deadline:
                log_event("io", f"写 {path.name} 失败：文件被占用，重试 {attempt} 次"
                                f"（约 10s）仍无果，可能有进程长期持有读句柄",
                          level="error", path=path.name)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.6)


def write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        # v3.02 #25：只有记忆层文件封哈希链——write_jsonl 还服务 history/knowledge 等，
        # 无差别封链会往别的文件里塞 h/p_h。非记忆文件、桥接前（无 _seal_chain）都跳过。
        if path.name in ("hippocampus.jsonl", "cortex.jsonl", "dust.jsonl", "archive.jsonl"):
            _seal_chain(rows)
    except NameError:
        pass
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _replace_retry(tmp, path)


def _append_jsonl(path: Path, row: dict) -> None:
    """追加一行 JSONL（v3.02 #5 metrics.jsonl 用）：写锁防多进程交错半行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


@contextmanager
def _locked(path: Path, timeout: float = 10.0):
    """跨进程写锁：用独占创建 lock 文件互斥，带超时与陈旧锁清理。

    没有锁时两个进程同时 read-modify-write 会互相覆盖（后写者赢，先写者丢）。
    Windows 上没有 flock，用 O_CREAT|O_EXCL 保证只有一个进程能创建锁文件。
    锁文件里写持有者 pid：解锁时只删自己那把，避免「持锁超时被接管后，
    自己退出时把接管者的锁删掉」——那会让第三个进程立刻挤进来一起写。
    """
    lock = path.with_name(path.name + ".lock")
    if _held(lock):
        # 本线程已持锁：重入，避免 update_persona → save_persona 这类嵌套把自己锁死
        yield
        return
    # 父目录不存在时 os.open 会抛 FileNotFoundError（新 HOME/测试隔离环境常见）；
    # 锁是"写盘前最后一步"，这里补建目录比让调用方炸掉更合适。
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    deadline = time.time() + timeout
    acquired = False
    _owns = False
    try:
        while True:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                # #24 多宿主可见：锁里同时记 pid 与主机名（空格分隔）。
                # _lock_owner 只取 split()[0]，旧格式/新格式都兼容；
                # 记忆库放在网盘/共享目录时，能看出是"另一台机器"在写。
                os.write(fd, f"{os.getpid()} {_HOSTNAME}".encode())
                os.close(fd)
                _HELD_LOCKS.add((str(lock), threading.get_ident()))  # 记住"本线程已持锁"，read_jsonl 自愈时避免重复抢锁
                acquired = True
                break
            except FileExistsError:
                # 陈旧锁（持锁进程已死）：久到不可能是正常写盘（正常持锁 <2s）才接管。
                # 阈值别设太小：dream 整理记忆时持锁会久一点，抢早了就是两个进程同时写。
                try:
                    if time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
                        # 再加一道 pid 探活：持锁进程还活着就别抢（work_flush/dream 可能持锁较久）。
                        # 同进程（含本进程其它线程）持有的锁一律视为"活着"，绝不接管。
                        owner = _lock_owner(lock)
                        if owner is not None and _pid_alive(owner):
                            pass  # 活着的持锁者：继续等，不接管
                        else:
                            try:
                                lock.unlink()
                            except OSError:
                                pass
                            continue
                except OSError:
                    pass
                if time.time() > deadline:
                    log_event("lock", f"等不到写锁：{path.name}（{timeout:.0f}s 超时）", level="error", path=path.name)
                    raise TimeoutError(f"等不到写锁：{path}")
                time.sleep(0.05)
        yield
    finally:
        # 持锁登记必须覆盖所有路径：正常释放、超时、异常都会回到这里。
        # 只要登记过就清掉，避免长跑进程的锁表缓慢累积、同线程后续 _held()
        # 误判"自己还持着锁"而跳过互斥。超时发生在登记之前（还没拿到锁），
        # 此时没有登记可清；放进同一 try/finally 是为了让"登记→清理"边界不依赖后续改动。
        if acquired:
            _HELD_LOCKS.discard((str(lock), threading.get_ident()))
        # 只在"自己成功创建或接管了锁（写入了自己 pid）"时才释放。
        # 锁文件已不存在（被陈旧锁接管流程删掉）→ 直接跳过，不留告警也不误删。
        # 注意：finally 里不能再 return——那会把调用方正在向上抛的异常一起吞掉。
        if acquired and lock.exists():
            # _lock_owner 读不到内容返回 None 时不删——那可能是其他进程刚接管/正在写
            # 锁文件，误删会让第三个进程立刻挤进来一起写。
            if _lock_owner(lock) == os.getpid():
                _owns = True  # 锁文件里确实记着自己是持有者
            if _owns:
                try:
                    lock.unlink()
                except FileNotFoundError:
                    pass  # 锁文件刚被别的流程删掉：无需处理
                except OSError:
                    pass
            else:
                log_event("lock", f"释放跳过：锁 {path.name} 不归本进程（pid 不符或读不到持有者）", path=path.name)


STALE_LOCK_SECONDS = 30.0


def _pid_alive(pid: int) -> bool:
    """探测 pid 是否还活着（Windows/POSIX 通用）。拿不到权限等异常时保守认为"活着"，
    以免把别人的活锁当陈旧锁抢走。"""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                # 打不开句柄（权限拒绝/跨会话）：保守视为存活，避免误判陈旧锁抢走别人的锁
                return True
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            kernel32.CloseHandle(h)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 探测被拒绝（跨会话等）：保守视为存活
    except (OSError, Exception):
        return True


def _lock_owner(lock: Path) -> int | None:
    """读锁文件里记录的持有者 pid；读不到/格式不对返回 None。"""
    try:
        raw = lock.read_text(encoding="utf-8", errors="ignore").strip()
        return int(raw.split()[0]) if raw else None
    except (OSError, ValueError, IndexError):
        return None


def lock_writers() -> list[dict]:
    """#24 列出当前持锁的写入者（文件 / pid / 主机名 / 是否活着）。

    记忆库放在共享盘时，同一份数据可能被多台机器同时写；这里把锁文件里
    记下的 host 读出来，让面板和 `status` 能直接看见"谁在写"。
    """
    rows = []
    try:
        locks = sorted(HOME.glob("*.lock"))
    except OSError:
        return rows
    for lock in locks:
        try:
            raw = lock.read_text(encoding="utf-8", errors="ignore").strip().lstrip("\ufeff")
        except OSError:
            continue
        if not raw:
            continue
        parts = raw.split()
        try:
            pid = int(parts[0])
        except (ValueError, IndexError):
            continue
        host = parts[1] if len(parts) > 1 else ""
        rows.append({
            "file": lock.name[:-5] if lock.name.endswith(".lock") else lock.name,
            "pid": pid,
            "host": host,
            "alive": _pid_alive(pid) and (not host or host == _HOSTNAME),
        })
    return rows



def _persona_txn(fn):
    """把「读人格 → 改 → 写回」整段放进 persona.json 的写锁里。
    否则两个并发的改动各自 load 一份旧档、后写者把先写者的字段覆盖回旧值。
    锁对同线程可重入，所以内部再调 save_persona 不会自锁。"""
    def wrapper(*args, **kwargs):
        with _locked(HOME / "persona.json"):
            return fn(*args, **kwargs)
    wrapper.__name__ = getattr(fn, "__name__", "persona_txn")
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _locked_save_all(records: list) -> None:
    """save_all 的加锁版：并发写记忆不丢更新。"""
    with _locked(HOME / "hippocampus.jsonl"):
        existing = load_all()
        by_id = {}
        no_id = []   # 缺 id 的畸形记录也原样保留，不能因为无法索引就悄悄丢掉
        for m in existing:
            mid = m.get("id")
            if mid:
                by_id[mid] = m
            else:
                no_id.append(m)
        for r in records:
            mid = r.get("id")
            if mid:
                by_id[mid] = r
        merged = list(by_id.values()) + no_id
        save_all(merged)


def _strengthen_write(touched: list) -> None:
    """recall 强化后的定向写回：只重写被"想起"记忆所在的层文件。

    上万条记忆时，旧实现（_locked_save_all 整库 merge + 全量重写 3 个文件）让每次
    recall 都背上整库 I/O；这里按层分组、只 patch 命中的行，开销与命中数成正比。

    注意只 patch 强化字段：touched 里的记录是 recall 开头无锁读的旧快照，
    若整条 update 回去，会把期间别人并发写入的 review/layer/tags 用旧值覆盖掉
    （例：刚 review confirm 的记忆被 recall 一碰又回到 pending）。

    跨层竞态防护：recall 锁外读快照后，dream/archive 可能已把记忆迁移层。
    所以锁内以旧快照的 layer 为起点重读目标文件做定向 patch——若 id 不在
    旧 layer，按 hippo→cortex→dust 顺序在其他层找它的实际位置再 patch；
    全找不到（可能已被归档/删除）则放弃并留日志，绝不往旧 layer 写回去。
    """
    if READONLY or not touched:
        return
    patch_fields = ("access_count", "stability", "last_access",
                    "used_count", "used_at", "used_hits")
    # detail 特殊处理：不能用锁外旧快照回写（会把 dream 的淡化抵消掉），
    # 改成 patch 时以盘上现值为基点重算巩固（0.9 保留 + 0.1 回升），
    # 公式与 memory.recall 的强化逻辑保持一致，落盘结果与快照值无关。
    DETAIL_CONSOLIDATE = ("detail", lambda v: min(1.0, _num(v, 1.0) * 0.9 + 0.1))
    # detail 以盘上现值重算巩固（详见上面 DETAIL_CONSOLIDATE 注释）；
    # 旧注释保留历史背景：快照 detail 确实不可直接回写。
    with _locked(HOME / "hippocampus.jsonl"):
        updates: dict = {}
        for m in touched:
            if m.get("layer") == ARCHIVE_LAYER:
                continue  # 归档记忆只读展示，不给强化（需要时 archive restore 回热）
            if not m.get("id"):
                continue   # 畸形记录（缺 id）没法强化
            updates[m["id"]] = m
        if not updates:
            return
        fname = {"hippo": "hippocampus.jsonl", "cortex": "cortex.jsonl", "dust": "dust.jsonl"}
        order = ("hippo", "cortex", "dust")
        # 每层文件只读一次：patch 该层命中的 id，并把别的层没找到的记到这里
        remaining = dict(updates)
        for layer in order:
            if not remaining:
                break
            p = HOME / fname[layer]
            rows = read_jsonl(p)
            hit_ids = set()
            for r in rows:
                u = remaining.get(r.get("id"))
                if u:
                    hit_ids.add(r["id"])
                    for f in patch_fields:
                        if f in u:
                            r[f] = u[f]
                    # detail：以盘上现值巩固，而非快照旧值（不抵消 dream 淡化）
                    fname_, fn = DETAIL_CONSOLIDATE
                    r[fname_] = round(fn(r.get(fname_)), 4)
            if hit_ids:
                write_jsonl(p, rows)
                for mid in hit_ids:
                    del remaining[mid]
        for mid in remaining:
            log_event("strengthen", f"放弃强化 {mid}：三层都找不到（可能已归档/删除）", path=fname["hippo"])


def _patch_memory_fields(rec: dict) -> bool:
    """v3.02 #10：定向 patch 保质期字段（review_by/review_by_confirmed/ttl_days）。

    与 _strengthen_write 同一套锁内按层找 id 的套路，但只写保质期相关字段，
    不碰强化计数字段——语义不同的事各走各的补丁，互不覆盖。
    找不到（已归档/删除）返回 False 并留日志。
    """
    if READONLY or not rec.get("id"):
        return False
    patch_fields = ("ttl_days", "review_by", "review_by_confirmed")
    fname = {"hippo": "hippocampus.jsonl", "cortex": "cortex.jsonl", "dust": "dust.jsonl",
             ARCHIVE_LAYER: "archive.jsonl"}
    for layer in ("hippo", "cortex", "dust", ARCHIVE_LAYER):
        p = HOME / fname[layer]
        rows = read_jsonl(p)
        for r in rows:
            if r.get("id") == rec["id"]:
                for f in patch_fields:
                    if f in rec:
                        r[f] = rec[f]
                write_jsonl(p, rows)
                return True
    log_event("memory", f"放弃复核确认 {rec['id']}：各层都找不到", level="warn")
    return False


# ---------------------------------------------------------------- 回忆使用反馈
# "存得好不好"不能只看条数：注入的记忆到底有没有被用上、哪些记忆从没被命中过，
# 这两个便宜计数才能回答"分层 + 遗忘曲线"是否真的让记忆更准。
RETRIEVAL_FILE = HOME / "last_retrieval.json"
USAGE_FILE = HOME / "recall_usage.jsonl"
USAGE_MIN_SHARED = 2      # 与回复共享的关键词元至少 2 个
USAGE_MIN_RATIO = 0.08    # 且至少覆盖记忆词元的 8%（粗判，不追求精确）

# 常用虚词二元组：它们在哪句话里都出现，不能算"用上了这条记忆"
_USAGE_STOP = {
    "我们", "你们", "他们", "这个", "那个", "就是", "什么", "因为", "所以",
    "但是", "可以", "已经", "不是", "没有", "一个", "自己", "现在", "时候",
    "这样", "如果", "还是", "然后", "觉得", "知道", "需要", "应该", "一下",
    "一些", "的话", "真的", "非常", "比较", "而且", "或者", "不过", "其中",
    "以及", "对于", "关于", "之后", "之前", "这种", "那种", "一直", "可能",
}


def _home_writable(max_attempts: int = 3) -> bool:
    """探测 HOME 是否可写：mkdir + 探针文件。失败即只读降级。

    重试 max_attempts 次（间隔 150ms）：Windows 杀软/搜索索引常短暂占用目录
    句柄，单次探测失败就降级会把整个进程误判成只读——AI 会话里表现为
    "能读不能写"，而目录实际完全可写。

    注意：探针文件清理失败**不能**算作不可写。Windows 上杀毒/索引服务常会在
    文件刚写完的瞬间持有句柄，unlink 抛 PermissionError——旧实现把这一步失败
    也当成只读，导致面板一启动就误挂「只读模式」横幅（实际目录完全可写）。
    写入成功即证明可写，清理只是尽力而为。
    """
    last_exc = None
    for attempt in range(max(1, int(max_attempts))):
        probe = None
        try:
            HOME.mkdir(parents=True, exist_ok=True)
            # 唯一文件名：避免并发实例（面板 + CLI）共用同一个探针互相踩
            probe = HOME / f".write-probe-{os.getpid()}-{int(time.time() * 1000)}"
            probe.write_text("ok", encoding="utf-8")
            return True
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < max(1, int(max_attempts)):
                time.sleep(0.15)
        finally:
            if probe is not None:
                try:
                    probe.unlink(missing_ok=True)
                except OSError:
                    pass   # 清理失败不影响可写性判定
    # 最终失败的原因留痕：区分沙箱拒写 / 杀软占用 / 真权限问题，免得排查靠猜
    try:
        log_event("core", f"HOME 不可写，只读降级（重试 {max_attempts} 次均失败）：{last_exc}",
                  level="warn")
    except Exception:
        pass
    return False


def _resolve_readonly(forced: bool = False) -> None:
    """确定最终只读状态：--ro 或 BRAIN_MEMORY_READONLY=1 强制只读；
    BRAIN_MEMORY_READONLY=0 强制可写（跳过探测——确认目录可写、但自动探测
    被环境干扰误报时用手动覆盖）；否则自动探测。"""
    global READONLY, READONLY_FORCED
    env = os.environ.get("BRAIN_MEMORY_READONLY", "").strip()
    if forced or env == "1":
        READONLY = True
        READONLY_FORCED = True
    elif env == "0":
        READONLY = False
        READONLY_FORCED = False
    else:
        READONLY = not _home_writable()
        READONLY_FORCED = False   # 自动降级，面板可自愈
    _sync_state()


def _extract_ro(argv: list) -> tuple:
    """从参数里挑出 --ro/--no-write，返回 (剩余参数, 是否强制只读)。"""
    out, forced = [], False
    for a in argv:
        if a in ("--ro", "--no-write", "-r"):
            forced = True
        else:
            out.append(a)
    return out, forced


def _rebind_paths() -> None:
    """按当前 HOME 重算所有派生路径常量（纯赋值，不触发跨模块同步）。"""
    global LOG_FILE, WORKING_FILE, VERBATIM_DIR, ASSOC_FILE, PERSONA_SNAP_DIR, DAILY_FILE
    global RETRIEVAL_FILE, USAGE_FILE, CONSENT_FILE, TOPIC_FILE, SESSIONS_DIR
    LOG_FILE = HOME / "lingshu.log"
    WORKING_FILE = HOME / "working.jsonl"
    TOPIC_FILE = HOME / "topics.jsonl"
    VERBATIM_DIR = HOME / "verbatim"
    ASSOC_FILE = HOME / "assoc.jsonl"
    PERSONA_SNAP_DIR = HOME / "persona_snapshots"
    DAILY_FILE = HOME / "daily_state.json"
    RETRIEVAL_FILE = HOME / "last_retrieval.json"
    USAGE_FILE = HOME / "recall_usage.jsonl"
    CONSENT_FILE = HOME / "consent.json"
    SESSIONS_DIR = HOME / "sessions"


def _recompute_paths() -> None:
    """集中重算由 HOME 派生的路径，再同步给各子模块（--home 切换后必须调用）。"""
    _rebind_paths()
    _sync_state()


# ---------------------------------------------------------------- 只读回放支撑（候选5）

_FINGERPRINT_FILES = ("hippocampus.jsonl", "cortex.jsonl", "dust.jsonl", "archive.jsonl",
                      "persona.json", "persona_snapshots.jsonl", "emotions.jsonl",
                      "topics.jsonl", "working.jsonl", "assoc.jsonl",
                      "lingshu.user.md", "config.json", "verbatim")


def data_fingerprint() -> str:
    """数据目录关键文件的轻量指纹（只读回放前后一致性校验用）。

    只看数据文件，不看日志（lingshu.log 会被正常操作追加）。
    用 size+mtime_ns 而非全文哈希：回放只读时两者都不变，足够判"没写库"。
    """
    h = hashlib.sha1()
    for name in _FINGERPRINT_FILES:
        f = HOME / name
        try:
            if f.is_dir():
                for sub in sorted(f.rglob("*")):
                    if sub.is_file():
                        st = sub.stat()
                        h.update(f"{sub.relative_to(HOME)}:{st.st_size}:{st.st_mtime_ns}\n".encode())
            elif f.is_file():
                st = f.stat()
                h.update(f"{name}:{st.st_size}:{st.st_mtime_ns}\n".encode())
        except OSError:
            pass
    return h.hexdigest()


@contextmanager
def readonly_scope():
    """临时只读：期间所有按 READONLY 判断的写入路径被跳过（回放/自检用）。

    拆分后每个子模块各持一份 READONLY 副本，必须经 _sync_state() 统一回灌；
    退出时恢复原值（原本就只读时保持只读）。
    """
    prev = READONLY
    globals()["READONLY"] = True
    _sync_state()
    try:
        yield
    finally:
        globals()["READONLY"] = prev
        _sync_state()


def _override_home(argv: list) -> list:
    """提取 --home <目录>/--home=<目录>，全局切换数据目录（任意位置均可）。
    同时写入环境变量，保证之后 import 的 growth 子系统读到同一个 HOME。"""
    global HOME
    out, i, home = [], 0, None
    while i < len(argv):
        a = argv[i]
        if a == "--home" and i + 1 < len(argv):
            home = argv[i + 1]
            i += 2
            continue
        if a.startswith("--home="):
            home = a.split("=", 1)[1]
            i += 1
            continue
        out.append(a)
        i += 1
    if home:
        HOME = Path(home).expanduser()
        _recompute_paths()
        os.environ["BRAIN_MEMORY_HOME"] = str(HOME)
    return out


# ---------------------------------------------------------------- 拆分后状态同步
# v2.22 起代码按领域拆到 memory/emotion/persona/dream/cli，各模块在 import 时
# 从 core 复制了一份 HOME/READONLY/派生路径。--home / --ro 切换后必须把 core 的
# 最新状态回灌给所有子模块，否则它们仍在读写旧目录（单文件时天然共享，拆分后不会）。
_SYNC_TARGETS: list = []


def _sync_state() -> None:
    """把 core 的当前全局状态同步到各子模块（brain.py 启动时登记同步目标）。

    先按当前 HOME 重算派生路径：这样调用方只改了 core.HOME 再调本函数时
    （测试/宿主/面板迁移都会这么用）也不会把旧目录的路径同步出去。
    """
    _rebind_paths()
    if not _SYNC_TARGETS:
        return
    snap = {k: v for k, v in dict(globals()).items() if not k.startswith("__")}
    for m in _SYNC_TARGETS:
        m.__dict__.update(snap)
    # growth.py 不在 _SYNC_TARGETS（它在 brain 之后 import brain，会形成循环导入），
    # 但它缓存了 HOME 与一批派生路径常量；这里 import 一次并调用它的重算入口，
    # 让 --home 切换后 growth 的 knowledge/notes/skills 等路径也指向新目录
    # （已在 sys.modules 里则零开销，首次 import 只是延后到现在，链路不变）。
    try:
        import growth  # noqa: E402
        growth._recompute_home()
    except Exception:
        pass  # growth 未部署/导入失败：沿用原值，不影响记忆主链路


# ---------------------------------------------------------------- 可选 embedding / rerank（P1）
# 零依赖铁律：这两个能力"装了 Ollama 才启用，没装自动退回纯 IDF 关键词"。
# 配置来源：环境变量 LINGSHU_EMBED_URL / LINGSHU_RERANK_URL 优先，config.json 的
# embed.url / rerank.url 兜底；任一为空白即整体禁用，调用方拿到 None 走原逻辑。
# 网络调用全部用标准库 urllib，任何超时/异常一律静默返回 None，绝不阻断主链路。
#
# embedding 主体（_embed_cfg / fallback_encode_cached / embed_texts）已拆到
# mem_embed.py，本模块末尾 `from mem_embed import *` 保留全部调用面。
# 熔断状态仍留在 core：它是进程内可变全局，insights 面板与测试按
# core._EMBED_DOWN_UNTIL 直接读写，mem_embed 里也经 core.* 访问同一份值。
_EMBED_DOWN_UNTIL = 0.0


def _rerank_cfg() -> dict | None:
    """读 rerank 配置；url 空白（未配置）返回 None。"""
    url = os.environ.get("LINGSHU_RERANK_URL", "").strip()
    try:
        cfg = load_config().get("rerank") or {}
    except Exception:
        cfg = {}
    if not url:
        url = str(cfg.get("url") or "").strip()
    if not url:
        return None
    return {
        "url": url,
        "model": str(cfg.get("model") or "qwen3-reranker-0.6b"),
        "timeout_sec": float(cfg.get("timeout_sec") or 8),
        "top_n": int(cfg.get("top_n") or 20),
    }


def rerank_docs(query: str, docs: list, top_n: int | None = None) -> list | None:
    """调外部 reranker 给 docs 精排，返回 [(doc_index, score), ...] 按分数降序。

    未配置/失败返回 None（调用方走简单加权合并）。兼容两种常见响应形态：
      1) {"scores": [float, ...]}                                —— 与 docs 等长
      2) {"results": [{"index": i, "relevance_score": s}, ...]}  —— cohere 风格
    """
    cfg = _rerank_cfg()
    if not cfg or not docs:
        return None
    import urllib.request
    n = min(int(top_n or cfg["top_n"]), len(docs))
    try:
        body = json.dumps({
            "model": cfg["model"],
            "query": str(query or ""),
            "documents": [str(d or "") for d in docs[:n]],
        }).encode("utf-8")
        req = urllib.request.Request(
            cfg["url"], data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=cfg["timeout_sec"]) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        pairs = []
        if isinstance(data.get("scores"), list):
            for i, s in enumerate(data["scores"][:n]):
                pairs.append((i, float(s)))
        elif isinstance(data.get("results"), list):
            for r in data["results"]:
                pairs.append((int(r.get("index", 0)),
                              float(r.get("relevance_score", r.get("score", 0.0)))))
        if not pairs:
            return None
        pairs.sort(key=lambda x: x[1], reverse=True)
        return pairs
    except Exception as exc:
        log_event("rerank", f"reranker 调用失败，退回加权合并：{exc}", level="warn")
        return None


# ---------------------------------------------------------------- 拆分模块 re-export
# embedding 实现已拆到 mem_embed.py：star-import 回来，保持 core.embed_texts /
# core._embed_cfg / `from core import *` 的调用面完全不变（下划线名字靠
# mem_embed.__all__ 显式导出，否则 star-import 不会带过去）。
from mem_embed import *  # noqa: E402,F401,F403

__all__ = [k for k in dict(globals()) if not k.startswith("__")]
