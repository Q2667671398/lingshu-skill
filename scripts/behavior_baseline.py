# -*- coding: utf-8 -*-
"""行为基线：record → check（借自 rust-anything 的"每一步都能证明行为没变"）。

为什么需要
----------
拆大模块（`memory.py` / `cli.py` / `growth.py` 都在 4000 行量级）时，现有保障只有
"测试断言集合不变"。但断言是**抽样**——它能证明测试覆盖到的部分没坏，证明不了
"同一份输入产出的文本仍然逐字相同"。而灵枢的核心产物恰恰是**文本**（注入包、
召回结果、日报），一个字变了就是行为变了。

rust-anything 的做法值得抄：先把原程序在固定输入上的输出**录下来**（record），
改造完**重放**（replay）逐字比对，差异为空才算过门（gate）。本文件就是那层。

用法
----
    python scripts/behavior_baseline.py record   # 录基线（改造前跑一次）
    python scripts/behavior_baseline.py check    # 重放比对（改造后跑）
    python scripts/behavior_baseline.py diff     # 只打印差异，不判退出码

基线文件 `tests/behavior-baseline.json`：与 `bench-baseline.json` 同处，**不进发布
manifest**（tests 不进清单），随仓库走即可。

为什么能逐字比对：本工具在**临时 HOME** 里用固定的种子数据重建同一个库，所以
时间戳、随机 id 之外的一切都应当是确定性的。不能确定的部分（时间、id、耗时）
在 `_normalize()` 里统一抹平成占位符——**抹平要写清楚抹了什么**，否则基线会变成
"看起来一样"的自欺。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILL_ROOT = ROOT.parent
BASELINE = SKILL_ROOT / "tests" / "behavior-baseline.json"

# 固定种子：改这里等于换基线样本，必须重新 record
SEED_MEMORIES = [
    ("约定：提交前必须跑五项发布检查", "preference", 0.9, ["流程", "发布"]),
    ("本机 python 是 shim，会吞退出码，验证一律用真解释器", "fact", 0.85, ["环境", "python"]),
    ("决策：不做 DiskGenius 文件恢复，别再建议这一步", "preference", 0.8, ["决定"]),
    ("洞察：插件异常必须吞掉，否则会回滚会话——所以失败要留痕", "insight", 0.75, ["插件", "设计"]),
    ("事件：v3.53 记忆静默停录，根因是找不到 brain.py", "episode", 0.7, ["事故"]),
    ("习惯：改完代码先跑 run_tests.py 再谈结论", "habit", 0.6, ["习惯"]),
]

QUERIES = ["发布检查", "python 解释器", "插件失败", "插件", ""]


def _normalize(text: str) -> str:
    """把不可确定的部分抹平——**只抹这些**，其余逐字比对。

    抹平项与理由：
      · 记忆 id（m_<时间戳>_<随机>）：时间戳与随机后缀天然每次不同
      · 10 位以上时间戳数字：同上
      · ISO / 本地日期时间：同上
      · 毫秒耗时数字：性能不是本基线的目的（那是 bench.py 的事）
      · 时段描述（下午/夜晚 + 一句节奏说明 + 今天周末）：startup 注入会把当前
        真实时段写进去，上午录的基线下午 check 必然不同——它不是被比对的行为，
        只是"现在几点"的投影。抹成 <TOD> 占位符。
    """
    text = re.sub(r"m_\d{10}_[0-9a-f]{8,}", "m_<ID>", text)
    text = re.sub(r"\b1[6-9]\d{8}\b", "<TS>", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?", "<DATETIME>", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}", "<DATE>", text)
    text = re.sub(r"\b\d+(\.\d+)?\s*(ms|毫秒)\b", "<DUR>", text)
    # 时段描述：`<DATETIME>，下午，正事多在此时（今天周末）。` / `<DATETIME>，深夜（…）——…。`
    # 一律抹到句末句号（时段词后面可能是逗号，也可能是括号，格式随时段而变）。
    text = re.sub(r"，(凌晨|早晨|上午|中午|下午|傍晚|夜晚|深夜)[^\n]*?。",
                  "，<TOD>。", text)
    return text


# 固定种子时间基准：种子记忆的 created 全部由它派生，保证每次 record 都是同一组数值
SEED_BASE_TS = 1795000000


def _pin_times(home: Path) -> None:
    """把种子记忆的 created/last_access 钉成**固定且互不相同**的值。

    为什么必须这么做：注入的「刚刚发生（本会话新增）」段取的是
    `[m for m in _recent40 if created >= last_startup][:4]`——**前 4 条**。种子记忆若是
    同一秒写入，created 全相等，`recent()` 的排序就并列，哪 4 条进入该段取决于
    JSONL 的文件先后（进而受写入顺序/哈希影响），于是同一份库每次产出不同顺序的
    注入包（实测 4 次 check 里 3 次报差异）。
    钉成 base - i*600 之后：时间各不相同、排序唯一，`last_startup` 也写死成中间值，
    切分点每次完全一致。
    """
    base = SEED_BASE_TS
    for layer in ("hippocampus.jsonl", "cortex.jsonl", "dust.jsonl",
                  "knowledge.jsonl", "notes.jsonl", "verbatim.jsonl"):
        f = home / layer
        if not f.is_file():
            continue
        rows = []
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        # **只按 text 定序**，不看 created：6 条种子在同一秒内写入时 created 相等，
        # 但只要写入跨过秒边界（实测 1/3 概率），按 created 排序的结果就每次不同，
        # 于是"谁拿到最新时间戳"跟着变、`[:4]` 的切分点也变（实测表现为
        # 「事件」与「习惯」两条在「最近的记忆」和「刚刚发生」之间来回跳）。
        # 文本互不相同，按它排序才是真正稳定的。
        rows.sort(key=lambda m: str(m.get("text") or ""))
        with f.open("w", encoding="utf-8", newline="\n") as fh:
            for i, m in enumerate(rows):
                ts = base - i * 600
                m["created"] = ts
                m["last_access"] = ts
                fh.write(json.dumps(m, ensure_ascii=False) + "\n")
    # last_startup 钉在"第 3 条之后"——稳定地把最新 2 条判为「刚刚发生」
    cfg_path = home / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.is_file() else {}
    except Exception:
        cfg = {}
    cfg["last_startup"] = {"ts": base - 600 + 1, "query": "baseline"}
    cfg["wake_count"] = 1
    (home).mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _seed(home: Path) -> None:
    """在给定 HOME 里造一份确定性的库（用子进程，避免污染本进程状态）。"""
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import core, brain\n"
        "core.HOME = __import__('pathlib').Path(%r)\n"
        "core._sync_state()\n"
        "brain.ensure_home()\n"
        "seeds = %r\n"
        "for text, kind, imp, tags in seeds:\n"
        "    brain.remember(text, kind=kind, importance=imp, tags=tags)\n"
        "print('seeded', len(seeds))\n"
    ) % (str(ROOT), str(home), SEED_MEMORIES)
    env = dict(os.environ, BRAIN_MEMORY_HOME=str(home), PYTHONIOENCODING="utf-8",
               PYTHONHASHSEED="0")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=300)
    if r.returncode != 0:
        raise SystemExit(f"[错误] 造种子库失败：{r.stderr[:400]}")
    _pin_times(home)


def _capture(home: Path) -> dict:
    """在给定 HOME 上采集所有"文本类输出"的快照。"""
    code = (
        "import json, sys; sys.path.insert(0, %r)\n"
        "import core, brain, memory\n"
        "core.HOME = __import__('pathlib').Path(%r)\n"
        "core._sync_state()\n"
        "out = {}\n"
        "out['injection'] = memory.build_injection('')\n"
        "for q in %r:\n"
        "    out['recall:' + q] = memory.build_injection(q)\n"
        "out['recall_json'] = json.dumps(\n"
        "    [ {k: m.get(k) for k in ('kind','text','importance','tags')}\n"
        "      for m in sorted(brain.load_all(), key=lambda x: str(x.get('text'))) ],\n"
        "    ensure_ascii=False, sort_keys=True)\n"
        "print(json.dumps(out, ensure_ascii=False))\n"
    ) % (str(ROOT), str(home), QUERIES)
    env = dict(os.environ, BRAIN_MEMORY_HOME=str(home), PYTHONIOENCODING="utf-8",
               # 固定哈希种子：recall 内部用 set 收集命中/实体/标签，集合遍历顺序随
               # 字符串哈希随机化而变，会让"同一份库"产出不同顺序的注入包（实测：
               # 4 次采集指纹全一致 vs 不固定时 1/3 概率不同）。这不是产品行为差异，
               # 是解释器级别的随机化——比对金标准文本的工具必须把它钉住。
               PYTHONHASHSEED="0")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=300)
    if r.returncode != 0:
        raise SystemExit(f"[错误] 采集行为快照失败：{r.stderr[:400]}")
    raw = json.loads(r.stdout)
    return {k: _normalize(v) for k, v in raw.items()}


def _fresh_capture() -> dict:
    """新 HOME → 造种子 → 采集。每次都用全新临时目录，保证同样输入同样输出。"""
    home = Path(tempfile.mkdtemp(prefix="ls-behavior-"))
    try:
        _seed(home)
        return _capture(home)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def record(quiet: bool = False) -> int:
    snap = _fresh_capture()
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps({"version": 1, "snapshot": snap},
                                   ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    if not quiet:
        total = sum(len(v) for v in snap.values())
        print(f"[record] 基线已保存：{BASELINE}")
        print(f"[record] 采集 {len(snap)} 个输出，共 {total} 字符（已抹平 id/时间/耗时）")
    return 0


def _diff(old: dict, new: dict) -> list[str]:
    """逐 key、逐行比对；返回人可读的差异清单。"""
    diffs: list[str] = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if a == b:
            continue
        if a is None or b is None:
            diffs.append(f"[{key}] 该输出{'消失' if b is None else '新增'}")
            continue
        al, bl = a.splitlines(), b.splitlines()
        for i in range(max(len(al), len(bl))):
            x = al[i] if i < len(al) else "<缺失>"
            y = bl[i] if i < len(bl) else "<缺失>"
            if x != y:
                diffs.append(f"[{key}] 第 {i + 1} 行\n    - {x[:160]}\n    + {y[:160]}")
                if len(diffs) >= 40:
                    diffs.append("…（差异过多，已截断）")
                    return diffs
    return diffs


def check(quiet: bool = False) -> int:
    if not BASELINE.is_file():
        print(f"[错误] 没有基线：{BASELINE}（先跑 record）", file=sys.stderr)
        return 2
    old = json.loads(BASELINE.read_text(encoding="utf-8"))["snapshot"]
    new = _fresh_capture()
    diffs = _diff(old, new)
    if not diffs:
        if not quiet:
            print(f"[check] OK：{len(new)} 个输出与基线逐字一致")
        return 0
    print(f"[check] 行为变了：{len(diffs)} 处差异", file=sys.stderr)
    for d in diffs:
        print("  " + d, file=sys.stderr)
    print("\n如果这是**有意**的行为变更：跑 record 重录基线，并在提交信息里写明为什么变。",
          file=sys.stderr)
    return 1


def diff_only() -> int:
    if not BASELINE.is_file():
        print(f"[错误] 没有基线：{BASELINE}", file=sys.stderr)
        return 2
    old = json.loads(BASELINE.read_text(encoding="utf-8"))["snapshot"]
    new = _fresh_capture()
    diffs = _diff(old, new)
    if not diffs:
        print("[diff] 无差异")
        return 0
    for d in diffs:
        print("  " + d)
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="行为基线：逐字比对改造前后输出")
    ap.add_argument("action", nargs="?", default="check", choices=("record", "check", "diff"))
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)
    if a.action == "record":
        return record(a.quiet)
    if a.action == "diff":
        return diff_only()
    return check(a.quiet)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
