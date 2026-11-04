#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 功能自测试：relationship 记忆 / 时段语境注入 / persona check（纯标准库）。

覆盖：
  A. KINDS 含 relationship，remember --kind relationship 可写入、recall 可召回。
  B. build_injection 含「我的人际关系」区块（kind=relationship 非 dust 的记忆成块）。
  C. build_injection 含「现在是什么时候」时段语境（日期 + 时段描述）。
  D. persona_check：正常人格 PASS；traits value 越界 → FAIL；
     裸数字 trait / history 缺 source → WARN；onboarding 未完成 → INFO。

运行：python scripts/test_p1_features.py
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core
import memory
import persona
import cli
import brain   # noqa: F401  # 命名空间合并层：补齐跨模块函数

USER = "qq:10001"


def fresh_home(tmp: Path) -> Path:
    home = tmp / ".brain-memory"
    home.mkdir(parents=True, exist_ok=True)
    core.HOME = home
    core._recompute_paths()
    return home


def run_cli(argv: list) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


class P1FeatureTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="lingshu-p1-test-")
        import shutil as _sh
        self.addCleanup(_sh.rmtree, self._tmp, True)
        self.home = fresh_home(Path(self._tmp))

    # ---- A. relationship kind ----
    def test_relationship_kind_write_recall(self):
        self.assertIn("relationship", core.KINDS)
        code, out, err = run_cli(["remember", "老张是我大学同学，欠他一顿饭",
                                  "--kind", "relationship", "--importance", "7"])
        self.assertEqual(code, 0, err)
        hits = memory.recall("老张")
        self.assertTrue(any("欠他一顿饭" in m.get("text", "") for m, _s, _p in hits))

    # ---- B. 注入含人际关系区块 ----
    def test_injection_relationship_block(self):
        memory.remember("小李帮过我很多次，记着这份人情", kind="relationship",
                        importance=7, user=USER)
        text = memory.build_injection("小李")
        self.assertIn("我的人际关系", text)
        self.assertIn("记着这份人情", text)

    # ---- C. 注入含时段语境 ----
    def test_injection_time_context(self):
        text = memory.build_injection("随便聊聊")
        self.assertIn("现在是什么时候", text)
        self.assertIn("，", text)
        # _time_context 本身：日期 + 分句结尾
        tc = memory._time_context()
        self.assertRegex(tc, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}，.+$")

    # ---- D. persona check ----
    def test_persona_check_pass(self):
        p = persona.load_persona()
        r = persona.persona_check()
        self.assertEqual(r["fails"], [])
        self.assertTrue(any("onboarding" in x for x in r["info"]))

    def test_persona_check_dirty(self):
        p = persona.load_persona()
        p["traits"]["好奇"] = {"value": 300, "evidence": [], "why": "测试"}
        p["traits"]["旧特质"] = 50            # 裸数字 → WARN
        p["history"].append({"ts": core.now_ts(), "field": "traits.好奇",
                             "from": 80, "to": 300, "why": "测试"})  # 缺 source → WARN
        persona.save_persona(p)
        r = persona.persona_check()
        self.assertTrue(any("300" in x or "越界" in x for x in r["fails"]), r)
        self.assertTrue(any("裸数字" in x for x in r["warns"]), r)
        self.assertTrue(any("source" in x for x in r["warns"]), r)
        # CLI 接线：FAIL → 退出码 1，输出含 FAIL
        code, out, err = run_cli(["persona", "check"])
        self.assertEqual(code, 1)
        self.assertIn("[FAIL]", out)

    def test_persona_check_json_mode(self):
        p = persona.load_persona()
        p["traits"]["好奇"] = {"value": -1, "evidence": [], "why": "测试"}
        persona.save_persona(p)
        code, out, err = run_cli(["persona", "check", "--json"])
        self.assertEqual(code, 1)
        data = json.loads(out)  # --json 模式输出必须是纯 JSON
        self.assertTrue(data["fails"])


if __name__ == "__main__":
    unittest.main()
