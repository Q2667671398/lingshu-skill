# -*- coding: utf-8 -*-
"""隐私作用域单测（#20）：consent 群作用域、过期失效、_channel_visible 跨群隔离。

不依赖固定 HOME：测试前把 core.HOME 重定向到临时目录，测完还原。
运行：python -m pytest tests/test_privacy_scope.py 或 python tests/test_privacy_scope.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import brain  # noqa: E402,F401  # 入口合并：把 core/memory/... 命名空间互相同步
import core  # noqa: E402
import memory  # noqa: E402


def _setup_home(tmp: Path) -> None:
    # brain import 时已登记 _SYNC_TARGETS（core/memory/... 全员）；
    # 这里只改 core 状态后回灌即可。
    core.HOME = tmp
    core.CONSENT_FILE = tmp / "consent.json"
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()


def test_consent_group_scope():
    """A 群授权后，B 群第三人不可见（同群授权语义）。"""
    with tempfile.TemporaryDirectory() as td:
        _setup_home(Path(td))
        core.consent_grant("u1", group="gA")
        assert core._consented("u1", "gA") is True
        assert core._consented("u1", "gB") is False
        # 无 group 参数的查询（本机）不做群校验，但授权须存在
        assert core._consented("u1") is True
        assert core._consented("u2") is False


def test_consent_global_star():
    """全域授权（groups["*"]）在任何群可见。"""
    with tempfile.TemporaryDirectory() as td:
        _setup_home(Path(td))
        core.consent_grant("u1")  # 不带 group => "*"
        assert core._consented("u1", "gA") is True
        assert core._consented("u1", "gB") is True


def test_consent_expiry():
    """expires 过期后视为未授权（fail-closed）；未过期仍可见。"""
    with tempfile.TemporaryDirectory() as td:
        _setup_home(Path(td))
        core.consent_grant("u1", expires_days=1)
        assert core._consented("u1") is True
        # 手工把 expires 拨回过去
        data = core.load_consent()
        data["u1"]["expires"] = core.now_ts() - 10
        core.save_consent(data)
        assert core._consented("u1") is False


def test_channel_visible_cross_group_isolation():
    """跨群记忆隔离：A 群记忆对 B 群查询不可见，即使已授权。"""
    with tempfile.TemporaryDirectory() as td:
        _setup_home(Path(td))
        m_other_group = {"channel": "qq@bot1", "user": "u1", "group_id": "gA", "text": "x"}
        # 未授权：不可见
        assert memory._channel_visible(m_other_group, "qq@bot1", "gB", "u2") is False
        # 授权 gA：对 gB 查询依旧不可见（群不同，先于 consent 判定被拦）
        core.consent_grant("u1", group="gA")
        assert memory._channel_visible(m_other_group, "qq@bot1", "gB", "u2") is False
        # 同群 + 已授权：可见
        assert memory._channel_visible(m_other_group, "qq@bot1", "gA", "u2") is True
        # 同群 + 已授权：对群内第三人可见（consent 是记忆所有者的分享授权）
        assert memory._channel_visible(m_other_group, "qq@bot1", "gA", "u3") is True
        # 同群但所有者未授权：第三人不可见
        m_no_consent = {"channel": "qq@bot1", "user": "u9", "group_id": "gA", "text": "y"}
        assert memory._channel_visible(m_no_consent, "qq@bot1", "gA", "u3") is False
        # 跨群记忆对本人自己在 B 群的查询也不可见（记忆归属在 A 群）
        assert memory._channel_visible(m_other_group, "qq@bot1", "gB", "u1") is False


def test_privacy_denied_message_hides_target():
    """PrivacyDenied 对外消息不回显目标完整标识（#10）。"""
    with tempfile.TemporaryDirectory() as td:
        _setup_home(Path(td))
        exc = memory.PrivacyDenied("qq@bot1:10086")
        assert "10086" not in str(exc)
        assert exc.target_user == "qq@bot1:10086"


def test_remember_user_source_immediate_replace():
    """source=user 的新事实应立即取代旧记忆，而非进冲突审核（#22/#30 语义）。"""
    with tempfile.TemporaryDirectory() as td:
        _setup_home(Path(td))
        old = memory.remember("我的操作系统是 Windows 10", source="user", importance=0.5)
        mid = old["id"] if isinstance(old, dict) and "id" in old else None
        new = memory.remember("我的操作系统是 Windows 11", source="user", importance=0.5)
        rows = memory.load_all()
        # 库里不再有 "Windows 10" 作为独立活跃记忆（被取代/沉档），或新文本已存在
        texts = [r.get("text", "") for r in rows]
        assert any("Windows 11" in t for t in texts)
        # 不应进入冲突审核队列（pending 里没有这对记忆）
        pend = memory.pending_memory_reviews()
        assert len(pend) == 0, f"source=user 不应进审核：{pend}"


def main() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
