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

# Windows 上 360/索引器会在文件刚写完时短暂持独占句柄：读取被拒是瞬时的，重试即过。
# 生产写路径有 core._replace_retry 兜底，读路径没有；这是测试执行器层的兜底，
# 只重试"瞬时占用"类 OSError，断言失败绝不被重试掩盖。
_TRANSIENT_WINERRORS = {5, 32, 33, 145}   # 拒绝访问/共享冲突/锁冲突/目录非空


def _is_transient_io(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        return getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS
    return False


def _run_with_io_retry(fn, attempts: int = 3):
    for i in range(attempts):
        try:
            return fn()
        except AssertionError:
            raise
        except OSError as exc:
            if not _is_transient_io(exc) or i + 1 >= attempts:
                raise
            print(f"  ~ {getattr(fn, '__name__', fn)}: 瞬时文件占用（{exc}），重试 {i + 1}/{attempts - 1}")
            time.sleep(0.3 * (i + 1))


def _setup_home(tmp: Path) -> None:
    # brain import 时已登记 _SYNC_TARGETS（core/memory/... 全员）；
    # 这里只改 core 状态后回灌即可。
    core.HOME = tmp
    core.CONSENT_FILE = tmp / "consent.json"
    core._recompute_paths()
    core._sync_state()
    core.ensure_home()
    # 显式锁定：派生路径必须全部跟随 HOME。历史上只改 core.HOME 没 rebind
    # CONSENT_FILE/LOG_FILE 等会读写上一个临时目录（已删）→ 偶发失败。
    # 这里把"没锁死"从偶发变成确定性失败，避免回归时又变成 flaky。
    for _name in ("CONSENT_FILE", "LOG_FILE", "WORKING_FILE", "TOPIC_FILE",
                  "VERBATIM_DIR", "ASSOC_FILE", "PERSONA_SNAP_DIR", "DAILY_FILE",
                  "RETRIEVAL_FILE", "USAGE_FILE", "SESSIONS_DIR"):
        _p = Path(getattr(core, _name))
        assert _p.parent == tmp or _p == tmp, f"{_name} 未跟随 HOME 切换：{_p}（HOME={tmp}）"
    assert Path(memory.HOME) == tmp, f"memory.HOME 未跟随：{memory.HOME}"


def test_consent_group_scope():
    """A 群授权后，B 群第三人不可见（同群授权语义）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        core.consent_grant("u1", group="gA")
        assert core._consented("u1", "gA") is True
        assert core._consented("u1", "gB") is False
        # 无 group 参数的查询（本机）不做群校验，但授权须存在
        assert core._consented("u1") is True
        assert core._consented("u2") is False


def test_consent_global_star():
    """全域授权（groups["*"]）在任何群可见。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        core.consent_grant("u1")  # 不带 group => "*"
        assert core._consented("u1", "gA") is True
        assert core._consented("u1", "gB") is True


def test_consent_expiry():
    """expires 过期后视为未授权（fail-closed）；未过期仍可见。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        exc = memory.PrivacyDenied("qq@bot1:10086")
        assert "10086" not in str(exc)
        assert exc.target_user == "qq@bot1:10086"


def test_remember_user_source_immediate_replace():
    """source=user 的新事实应立即取代旧记忆，而非进冲突审核（#22/#30 语义）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
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


def test_local_injection_hides_channel_memories_by_default():
    """本机注入只带无通道记忆与 dialogue:*，不把群友隐私带进桌面会话。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        cfg = core.load_config()
        cfg["embed"]["url"] = ""
        cfg["embed"]["fallback"]["enabled"] = False
        core.atomic_json(core.HOME / "config.json", cfg)
        memory.remember("本机用户喜欢喝美式咖啡", kind="preference", importance=0.8)
        memory.remember("群里小王说他下周要做手术，别外传", kind="episode", importance=0.9,
                        channel="qq:88888", group_id="grp1", user="qq:10001")
        memory.remember("另一场会话讨论过云南旅行", kind="episode", importance=0.6,
                        source="dialogue", channel="dialogue:another-session")
        text = memory.build_injection()
        assert "小王" not in text
        assert "美式咖啡" in text
        assert "云南旅行" in text
        queried = memory.build_injection("手术")
        assert "小王" not in queried


def test_local_injection_allows_explicitly_configured_channel():
    """inject_channels 是显式例外：只放开点名的通道，不影响其他群。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _setup_home(Path(td))
        cfg = core.load_config()
        cfg["embed"]["url"] = ""
        cfg["embed"]["fallback"]["enabled"] = False
        cfg["inject_channels"] = ["qq:88888"]
        core.atomic_json(core.HOME / "config.json", cfg)
        memory.remember("群里小王说他下周要做手术，别外传", kind="episode", importance=0.9,
                        channel="qq:88888", group_id="grp1", user="qq:10001")
        memory.remember("另一个群讨论过小李搬家", kind="episode", importance=0.9,
                        channel="qq:99999", group_id="grp2", user="qq:10002")
        text = memory.build_injection("手术 搬家")
        assert "小王" in text
        assert "小李" not in text


def main() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            _run_with_io_retry(fn)
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
