#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群友隐私保护自测试：本人可查 / 第三人默认拒绝 / 授权放行 / 撤回再拒（纯标准库）。

覆盖：
  A. 本人可查自己（recall 与 channel user 两个入口）。
  B. 第三人未授权被拒（PrivacyDenied 异常 / CLI 退出码 4 与拒绝提示）；
     授权后放行；本人 revoke 后又拒绝。
  C. owner 查他人记忆同样被拒——刻意不豁免（隐私 ≠ 系统级命令）。
  D. privacy grant/revoke/status 生效；不能替别人授权（缺 --user 报用法错）。
  E. 回归：owner/admin 鉴权对系统级命令仍然生效（不回归 test_channel 的 D 组）。
  F. 群级记忆（有 group 无 user）与全局记忆不受隐私规则影响。

运行：python scripts/test_privacy.py
"""
from __future__ import annotations

import io
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
USER_A = "qq:10001"   # 本人 A
USER_B = "qq:20002"   # 目标 B（被打听的人）
OWNER = "qq:90009"    # 主人账号
GROUP = "333"


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


class PrivacyTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="lingshu-privacy-test-"))
        self.home = fresh_home(self.base)
        core.ensure_home()

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    # ---- A. 本人可查自己 ----
    def test_self_query_allowed(self):
        memory.remember("B喜欢打羽毛球", channel="qq", group_id=GROUP, user=USER_B)
        # 本人查自己：recall 带 target_user/asker 全量放行
        vis = recall_texts(channel="qq", group_id=GROUP, user=USER_B,
                           target_user=USER_B, asker=USER_B)
        self.assertIn("B喜欢打羽毛球", vis)
        # CLI channel user 本人查自己
        code, out, _ = run_cli(["channel", "user", USER_B, "--asker", USER_B])
        self.assertEqual(code, 0)
        self.assertIn("B喜欢打羽毛球", out)

    # ---- B. 第三人默认拒绝 / 授权放行 / 撤回再拒 ----
    def test_third_party_gate(self):
        memory.remember("B喜欢打羽毛球", channel="qq", group_id=GROUP, user=USER_B)
        memory.remember("B在攒钱买相机", channel="qq", group_id=GROUP, user=USER_B)

        # 未授权：第三方（A）查关于 B 的记忆 → 拒绝
        with self.assertRaises(memory.PrivacyDenied):
            memory.recall("", k=50, channel="qq", group_id=GROUP, user=USER_A,
                          target_user=USER_B, asker=USER_A)
        code, _, err = run_cli(["channel", "user", USER_B, "--asker", USER_A])
        self.assertEqual(code, 4)
        self.assertIn("隐私保护", err)
        self.assertIn(USER_B, err)
        # CLI recall 带 --target/--asker 同样拒绝
        code, _, err = run_cli(["channel", "recall", "羽毛球", "--channel", "qq",
                                "--group", GROUP, "--target", USER_B, "--asker", USER_A])
        self.assertEqual(code, 4)
        self.assertIn("隐私保护", err)

        # B 本人 grant 后放行
        code, out, _ = run_cli(["channel", "privacy", "grant", "--user", USER_B])
        self.assertEqual(code, 0)
        self.assertTrue(core._consented(USER_B))
        code, out, _ = run_cli(["channel", "user", USER_B, "--asker", USER_A])
        self.assertEqual(code, 0)
        self.assertIn("B喜欢打羽毛球", out)
        code, out, _ = run_cli(["channel", "recall", "羽毛球", "--channel", "qq",
                                "--group", GROUP, "--target", USER_B, "--asker", USER_A])
        self.assertEqual(code, 0)
        self.assertIn("B喜欢打羽毛球", out)

        # B revoke 后又拒绝
        code, _, _ = run_cli(["channel", "privacy", "revoke", "--user", USER_B])
        self.assertEqual(code, 0)
        self.assertFalse(core._consented(USER_B))
        code, _, err = run_cli(["channel", "user", USER_B, "--asker", USER_A])
        self.assertEqual(code, 4)

        # asker 缺失（无身份的本机调用）≠ 本人：fail closed，同样拒绝
        with self.assertRaises(memory.PrivacyDenied):
            recall_texts(target_user=USER_B)

    # ---- C. owner 查他人记忆同样被拒（刻意不豁免）----
    def test_owner_not_exempt(self):
        core.config_set("owners", [OWNER])
        self.assertFalse(core.is_owner(USER_B))
        self.assertTrue(core.is_owner(OWNER))
        memory.remember("B的秘密", channel="qq", group_id=GROUP, user=USER_B)
        # owner 查 B 的记忆：仍然拒绝（隐私不因主人身份放行）
        with self.assertRaises(memory.PrivacyDenied):
            memory.recall("", k=50, channel="qq", group_id=GROUP, user=OWNER,
                          target_user=USER_B, asker=OWNER)
        code, _, err = run_cli(["channel", "user", USER_B, "--asker", OWNER])
        self.assertEqual(code, 4)
        self.assertIn("隐私保护", err)

    # ---- D. privacy grant/revoke/status + 不能替别人授权 ----
    def test_privacy_commands(self):
        # status：纯本机不带 --user 可查
        code, out, _ = run_cli(["channel", "privacy", "status"])
        self.assertEqual(code, 0)
        code, out, _ = run_cli(["channel", "privacy", "status", "--user", USER_B])
        self.assertEqual(code, 0)
        self.assertIn("未授权", out)
        # grant 生效
        code, _, _ = run_cli(["channel", "privacy", "grant", "--user", USER_B])
        self.assertEqual(code, 0)
        code, out, _ = run_cli(["channel", "privacy", "status", "--user", USER_B])
        self.assertIn("已授权", out)
        # consent.json 落盘内容
        data = core.load_consent()
        self.assertTrue(data[USER_B]["granted"])
        # 不能替别人 grant/revoke：缺 --user → 用法错（64）
        code, _, err = run_cli(["channel", "privacy", "grant"])
        self.assertEqual(code, 64)
        code, _, err = run_cli(["channel", "privacy", "revoke"])
        self.assertEqual(code, 64)
        # revoke：撤回 + 对本来没授权的人 revoke 不报错
        code, _, _ = run_cli(["channel", "privacy", "revoke", "--user", USER_B])
        self.assertEqual(code, 0)
        code, _, _ = run_cli(["channel", "privacy", "revoke", "--user", USER_B])
        self.assertEqual(code, 0)

    # ---- E. 回归：owner/admin 鉴权对系统级命令仍然生效 ----
    def test_owner_gate_regression(self):
        core.config_set("owners", [OWNER])
        # 非 owner 在通道上下文跑系统级命令 → 拒绝（退出码 3）
        code, _, err = run_cli(["forget", "m_x", "--channel", "qq", "--user", USER_A])
        self.assertEqual(code, 3)
        self.assertIn("拒绝", err)
        # owner 通过（id 不存在只是正常"没找到"）
        code, _, err = run_cli(["forget", "m_nonexistent", "--channel", "qq", "--user", OWNER])
        self.assertNotIn("拒绝", err)
        # 纯本机调用不受限
        code, _, err = run_cli(["dream", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertNotIn("拒绝", err)

    # ---- F. 群级记忆与全局记忆不受隐私规则影响 ----
    def test_group_and_global_memory_unaffected(self):
        memory.remember("全群都在等的团建安排", channel="qq", group_id=GROUP)   # 群级（无 user）
        memory.remember("全局叮嘱一条")                                        # 全局
        # 第三人查「关于 B 的记忆」被拒，但普通群召回（不带 target）照常看到群级/全局记忆
        vis = recall_texts(channel="qq", group_id=GROUP, user=USER_A)
        self.assertIn("全群都在等的团建安排", vis)
        self.assertIn("全局叮嘱一条", vis)
        code, out, _ = run_cli(["channel", "recall", "团建", "--channel", "qq",
                                "--group", GROUP, "--user", USER_A])
        self.assertEqual(code, 0)
        self.assertIn("全群都在等的团建安排", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
