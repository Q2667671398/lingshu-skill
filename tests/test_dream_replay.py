#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""做梦「重放→压缩→巩固」与分模式压缩策略的单测。

临时 HOME + READONLY，跑完即删，不碰真实记忆库。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import core
import brain   # 触发 core/memory/dream 的命名空间合并（dream 内部裸调 load_all 等靠它解析）
import dream as dm

FAILS = []
TMP = Path(tempfile.mkdtemp(prefix="ls-dream-test-"))
core.HOME = TMP
core._sync_state()
dm.HOME = TMP
core.READONLY = False
core._sync_state()


def check(name, cond):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}")
        FAILS.append(name)


def seed_memory(text, kind="fact", created=None, layer="hippo", importance=0.5):
    mid = f"m_{abs(hash(text)) % 10**10}"
    rec = {"id": mid, "kind": kind, "text": text, "created": created or core.now_ts(),
           "layer": layer, "importance": importance, "detail": 1.0, "access_count": 0,
           "tags": [], "entities": []}
    with open(TMP / "hippocampus.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def main():
    print("== compaction_policy 分模式 ==")
    pol_work = dm.compaction_policy("work")
    pol_human = dm.compaction_policy("human")
    pol_unknown = dm.compaction_policy("whatever")
    check("work 有 detail_floor=0.55", pol_work["detail_floor"] == 0.55)
    check("work 有 dust_retention_shift=0.10", pol_work["dust_retention_shift"] == 0.10)
    check("human detail_floor=0", pol_human["detail_floor"] == 0.0)
    check("未知模式回退 human", pol_unknown["mode"] == "human")
    check("策略带 mode 键", "mode" in pol_work and "mode" in pol_human)

    print("== replay_recent 重放 ==")
    last = core.now_ts() - 3600
    core.atomic_json(TMP / "dream_state.json", {"last_dream": last, "task_id": "t"})
    old_rec = seed_memory("很老的记忆", created=core.now_ts() - 10 * core.DAY)
    new_rec = seed_memory("上次做梦后的新记忆", created=core.now_ts() - 60)
    replay = dm.replay_recent()
    check("重放包含新记忆", any(x["text"] == "上次做梦后的新记忆" for x in replay))
    check("重放排除做梦前的旧记忆", not any(x["id"] == old_rec["id"] for x in replay))
    # 工作记忆里的未完成事项
    wf = TMP / "working.jsonl"   # core.WORKING_FILE 在 HOME 切换后可能未重指，直接写临时目录
    wf.parent.mkdir(parents=True, exist_ok=True)
    with open(wf, "a", encoding="utf-8") as f:
        f.write(json.dumps({"text": "待办的活儿", "open": True, "ts": core.now_ts()},
                           ensure_ascii=False) + "\n")
    replay2 = dm.replay_recent()
    check("重放包含未完成工作记忆", any(x["from"] == "working" and x["text"] == "待办的活儿"
                                     for x in replay2))
    check("重放条数有上限", len(dm.replay_recent(limit=2)) <= 2)

    print("== build_handoff 分模式压缩 ==")
    ho_work = dm.build_handoff(replay2, "work")
    check("交接带 mode=work", ho_work["mode"] == "work")
    check("交接有状态/要点/未决/下一步",
          all(k in ho_work for k in ("状态", "要点", "未决", "下一步")))
    check("状态含计数", "上次做梦后新增" in ho_work["状态"])
    check("未决含工作记忆", any("待办" in x for x in ho_work["未决"]))
    check("重放计数正确", ho_work["replayed"] == len(replay2))

    print("== save_handoff / latest_handoff ==")
    p = dm.save_handoff(ho_work)
    check("交接文件已落盘", p.exists() and p.parent.name == "handoffs")
    latest = dm.latest_handoff()
    check("latest_handoff 读回同一份", latest and latest["要点"] == ho_work["要点"])
    # 只留最近 10 份
    for i in range(12):
        dm.save_handoff(dict(ho_work, at=core.now_ts() - 100 - i))  # 过去时间戳，别把真跑的交接挤掉
    check("只保留最近 10 份", len(list((TMP / "handoffs").glob("handoff_*.json"))) == 10)

    print("== dream() 端到端（dry_run 预览 + 真跑） ==")
    seed_memory("要被淡化的一条", created=core.now_ts() - 2 * core.DAY, layer="hippo")
    rep = dm.dream("test", dry_run=True)
    check("报告含 replayed", "replayed" in rep)
    check("报告含 policy 模式", rep.get("policy") in ("work", "human"))
    check("dry_run 不落交接", "handoff" not in rep)
    rep2 = dm.dream("test", dry_run=False)
    check("真跑落了交接文件", "handoff" in rep2 and (TMP / "handoffs" / rep2["handoff"]).exists())
    if "handoff" not in rep2:
        # 失败原因打印（通常被 dream 的容错吞掉，去 events.log 里找）
        lg = TMP / "events.log"
        if lg.exists():
            print("    events.log 尾部：")
            for line in lg.read_text(encoding="utf-8").splitlines()[-5:]:
                print("    " + line)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
