#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通道适配自测试：跨群用户记忆 + owner 鉴权（纯标准库）。

覆盖：
  A. 群隔离：群 A 记忆在群 B 上下文 recall 不可见；同群可见；全局记忆两边都可见。
  B. 跨群用户：同一 user 在两个群各写一条，channel user 能聚合看到两条。
  C. 向后兼容：不带 channel 的 remember/recall 与旧数据完全一致；
     旧记忆（无 channel 字段）在新过滤逻辑下视为全局记忆。
  D. owner 鉴权：非 owner 调敏感命令被拒、owner 通过；owner add/remove/list 生效。

运行：python scripts/test_channel.py
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
import cli
import brain   # noqa: F401  # 命名空间合并层：补齐 persona/dream 等拆分函数（ensure_home 需要）

# 测试里出现的固定身份/群号
USER = "qq:10001"
USER2 = "qq:20002"
GROUP_A, GROUP_B = "111", "222"


def fresh_home(tmp: Path) -> Path:
    """让 core/memory/cli 全部指到临时 HOME（模拟 BRAIN_MEMORY_HOME 隔离）。"""
    home = tmp / ".brain-memory"
    home.mkdir(parents=True, exist_ok=True)
    core.HOME = home
    core._recompute_paths()
    return home


def run_cli(argv: list) -> tuple:
    """跑一遍 CLI，返回 (退出码, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    except SystemExit as exc:   # argparse 用法错误抛 SystemExit(64)
        code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


def recall_texts(**kw) -> set:
    """召回结果的正文集合（便于断言可见性）。空查询 = 不过相关度，纯看可见性过滤。"""
    return {m.get("text", "") for m, _s, _p in memory.recall("", k=50, **kw)}


class ChannelTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="lingshu-channel-test-"))
        self.home = fresh_home(self.base)
        core.ensure_home()

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    # ---- A. 群隔离 ----
    def test_group_isolation(self):
        # 群 A 一条、群 B 一条、全局一条
        memory.remember("A群里的秘密暗号", channel="qq", group_id=GROUP_A)
        memory.remember("B群里的团建安排", channel="qq", group_id=GROUP_B)
        memory.remember("这是我给你的全局叮嘱")   # 无通道三元组 = 全局记忆

        vis_a = recall_texts(channel="qq", group_id=GROUP_A)
        vis_b = recall_texts(channel="qq", group_id=GROUP_B)
        self.assertIn("A群里的秘密暗号", vis_a)
        self.assertNotIn("B群里的团建安排", vis_a)
        self.assertIn("B群里的团建安排", vis_b)
        self.assertNotIn("A群里的秘密暗号", vis_b)
        # 全局记忆两边都可见
        self.assertIn("这是我给你的全局叮嘱", vis_a)
        self.assertIn("这是我给你的全局叮嘱", vis_b)

    def test_user_memory_cross_group(self):
        # 同一 user 在两个群各写一条；另一用户在群 B 写一条
        memory.remember("10001在A群说过的话", channel="qq", group_id=GROUP_A, user=USER)
        memory.remember("10001在B群说过的话", channel="qq", group_id=GROUP_B, user=USER)
        memory.remember("别人在B群说的话", channel="qq", group_id=GROUP_B, user=USER2)

        # 群 B 上下文召回 10001：能看到 B 群群记忆 + 自己跨群的 A 群记忆
        vis = recall_texts(channel="qq", group_id=GROUP_B, user=USER)
        self.assertIn("10001在A群说过的话", vis)   # 自己的记忆跨群可见
        self.assertIn("10001在B群说过的话", vis)   # 同群
        self.assertIn("别人在B群说的话", vis)       # 同群记忆可见（哪怕不是自己的）
        # 群 A 上下文召回 10001：能看到自己的 A 群记忆 + 自己跨群的 B 群记忆，
        # 看不到 B 群里别人的记忆
        vis_a = recall_texts(channel="qq", group_id=GROUP_A, user=USER)
        self.assertIn("10001在A群说过的话", vis_a)
        self.assertIn("10001在B群说过的话", vis_a)  # user 匹配 → 自己的记忆跨群可见
        self.assertNotIn("别人在B群说的话", vis_a)  # 其他群的他人记忆不可见

        # 跨群用户画像聚合：channel user 能看到两条
        code, out, _ = run_cli(["channel", "user", USER])
        self.assertEqual(code, 0)
        self.assertIn("10001在A群说过的话", out)
        self.assertIn("10001在B群说过的话", out)
        self.assertIn("共 2 条记忆", out)

    # ---- C. 向后兼容 ----
    def test_backward_compatible(self):
        # 不带 channel 参数的 remember：记录里不应有通道字段
        rec, how = memory.remember("老式记忆一条")
        self.assertEqual(how, "created")
        self.assertNotIn("channel", rec)
        self.assertNotIn("group_id", rec)
        self.assertNotIn("user", rec)
        # 不带 channel 的 recall 照常召回
        self.assertIn("老式记忆一条", recall_texts())

        # 旧记忆（无 channel 字段）在新过滤逻辑下视为全局记忆
        vis = recall_texts(channel="qq", group_id=GROUP_A, user=USER)
        self.assertIn("老式记忆一条", vis)

        # 纯 CLI 老命令仍可用
        code, out, _ = run_cli(["recall", "老式记忆"])
        self.assertEqual(code, 0)
        self.assertIn("老式记忆一条", out)

    # ---- D. owner 鉴权 ----
    def test_owner_gate_and_management(self):
        # 敏感命令：非 owner 被拒（退出码 3）
        code, _, err = run_cli(["forget", "m_x", "--channel", "qq", "--user", USER])
        self.assertEqual(code, 3)
        self.assertIn("拒绝", err)
        # owner add / list / remove 生效
        code, _, _ = run_cli(["channel", "owner", "add", USER])
        self.assertEqual(code, 0)
        code, out, _ = run_cli(["channel", "owner", "list"])
        self.assertEqual(code, 0)
        self.assertIn(USER, out)
        self.assertTrue(core.is_owner(USER))

        # owner 天然包含 admin 权限；admins 名单独立
        self.assertTrue(core.is_admin(USER))
        self.assertFalse(core.is_admin("qq:88888"))
        # add 后敏感命令立即放行（读 config.json，非缓存）；id 不存在只是正常"没找到"
        code, out, err = run_cli(["forget", "m_nonexistent", "--channel", "qq", "--user", USER])
        self.assertNotIn("拒绝", err)

        # 纯本机调用（不带 --user）不受限
        code, _, err = run_cli(["dream", "--dry-run"])
        self.assertNotIn("拒绝", err)
        self.assertEqual(code, 0)

        code, _, _ = run_cli(["channel", "owner", "remove", USER])
        self.assertEqual(code, 0)
        self.assertFalse(core.is_owner(USER))
        # remove 后又会被拒
        code, _, err = run_cli(["forget", "m_x", "--channel", "qq", "--user", USER])
        self.assertEqual(code, 3)

    # ---- CLI channel remember/recall ----
    def test_cli_channel_remember_recall(self):
        code, _, _ = run_cli(["channel", "remember", "CLI写入的群A记忆",
                              "--channel", "qq", "--group", GROUP_A, "--user", USER])
        self.assertEqual(code, 0)
        code, out, _ = run_cli(["channel", "recall", "CLI写入", "--channel", "qq", "--group", GROUP_A])
        self.assertEqual(code, 0)
        self.assertIn("CLI写入的群A记忆", out)
        # 群 B 看不到
        code, out, _ = run_cli(["channel", "recall", "CLI写入", "--channel", "qq", "--group", GROUP_B])
        self.assertEqual(code, 0)
        self.assertNotIn("CLI写入的群A记忆", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
