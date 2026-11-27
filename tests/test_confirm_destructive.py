# -*- coding: utf-8 -*-
"""破坏性操作统一确认（safety.confirm_destructive，v3.52）回归。

背景：`restore` 以前自己写了一套 `input()` 询问、`purge` 用 `--confirm` 令牌，
同一种"会覆盖/删除数据"的操作在不同命令上要求不同确认方式。v3.52 起统一走
`safety.confirm_destructive()`，本测试锁住四条规则：

  1. 无论是否交互，**先打印影响面**（动哪个目录、会不会删文件）；
  2. `assume_yes=True`（`--yes`）→ 直接放行；
  3. 非交互（管道 / AI 调用 / 计划任务）且没给 --yes → **拒绝**，并说清要加 --yes；
  4. 交互询问 → 只有 y/yes 放行，回车/其它都算取消（默认 N）。

零依赖：纯标准库，直接 `python tests/test_confirm_destructive.py` 运行。
"""
from __future__ import annotations

import builtins
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import brain  # noqa: E402,F401  （确保模块路径与生产一致）
import safety  # noqa: E402


class _FakeStdin:
    """最小可用的 stdin 替身：只实现 isatty 和 read 相关属性。"""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def read(self, *a, **k):  # pragma: no cover - 用不到
        return ""


class _NoIsattyStdin:
    """没有 isatty 的 stdin（个别宿主会替换成包装对象）。"""


def _with_stdin(fake, fn):
    old = sys.stdin
    sys.stdin = fake
    try:
        return fn()
    finally:
        sys.stdin = old


def _with_input(answer, fn):
    old = builtins.input

    def fake_input(*a, **k):
        if isinstance(answer, BaseException):
            raise answer
        return answer

    builtins.input = fake_input
    try:
        return fn()
    finally:
        builtins.input = old


def test_assume_yes_passes_and_still_prints_impact():
    buf = io.StringIO()
    ok = safety.confirm_destructive(
        "恢复",
        ["把备份内容写回 /tmp/home", "恢复前先留现状备份"],
        assume_yes=True,
        stream=buf,
    )
    assert ok is True, "assume_yes=True 必须放行"
    text = buf.getvalue()
    assert "影响面" in text, f"应打印影响面标题，实际：{text!r}"
    assert "写回" in text and "现状备份" in text, "每一条影响面都要打印出来"
    assert "--yes" in text, "放行时也要说明是靠 --yes 过的"


def test_non_interactive_without_yes_is_refused():
    buf = io.StringIO()
    err = io.StringIO()
    old_err = sys.stderr
    sys.stderr = err
    try:
        ok = _with_stdin(
            _FakeStdin(False),
            lambda: safety.confirm_destructive("恢复", "写回整个记忆库", stream=buf),
        )
    finally:
        sys.stderr = old_err
    assert ok is False, "非交互且没给 --yes 必须拒绝"
    assert "--yes" in err.getvalue(), f"错误信息要提示加 --yes，实际：{err.getvalue()!r}"
    assert "影响面" in buf.getvalue(), "拒绝前也要打印影响面"


def test_interactive_yes_passes():
    ok = _with_stdin(
        _FakeStdin(True),
        lambda: _with_input("y", lambda: safety.confirm_destructive("恢复", "写回", stream=io.StringIO())),
    )
    assert ok is True, "交互下回答 y 应放行"


def test_interactive_blank_is_cancel():
    buf = io.StringIO()
    ok = _with_stdin(
        _FakeStdin(True),
        lambda: _with_input("", lambda: safety.confirm_destructive("恢复", "写回", stream=buf)),
    )
    assert ok is False, "回车＝取消（默认 N）"
    assert "已取消" in buf.getvalue()


def test_interactive_n_is_cancel():
    ok = _with_stdin(
        _FakeStdin(True),
        lambda: _with_input("n", lambda: safety.confirm_destructive("恢复", "写回", stream=io.StringIO())),
    )
    assert ok is False, "回答 n 必须取消"


def test_eof_on_input_counts_as_cancel():
    ok = _with_stdin(
        _FakeStdin(True),
        lambda: _with_input(EOFError(), lambda: safety.confirm_destructive("恢复", "写回", stream=io.StringIO())),
    )
    assert ok is False, "input() 抛 EOFError 应当取消而不是崩溃"


def test_stdin_without_isatty_is_not_interactive():
    ok = _with_stdin(_NoIsattyStdin(), lambda: safety.is_interactive())
    assert ok is False, "没有 isatty 的 stdin 视为非交互（不能因此崩溃）"


def test_str_impact_is_accepted():
    buf = io.StringIO()
    ok = safety.confirm_destructive("备份", "生成含全部记忆原文的明文 zip", assume_yes=True, stream=buf)
    assert ok is True
    assert "明文 zip" in buf.getvalue(), "单条字符串影响面也要打印"


def test_hint_is_shown_on_refusal():
    err = io.StringIO()
    old_err = sys.stderr
    sys.stderr = err
    try:
        _with_stdin(
            _FakeStdin(False),
            lambda: safety.confirm_destructive(
                "恢复", "写回", hint="脚本调用请加 --yes", stream=io.StringIO()
            ),
        )
    finally:
        sys.stderr = old_err
    assert "脚本调用请加 --yes" in err.getvalue(), "拒绝时应把 hint 一起打出来"


def test_exported_in_all():
    assert "confirm_destructive" in safety.__all__, "__all__ 要包含 confirm_destructive"
    assert "is_interactive" in safety.__all__, "__all__ 要包含 is_interactive"


def test_restore_uses_shared_guard():
    """restore 的非交互拒绝信息应来自统一确认（而不是自己那套文案）。"""
    src = (ROOT / "scripts" / "cli.py").read_text(encoding="utf-8")
    assert 'safety.confirm_destructive("恢复"' in src, "restore 必须调用统一确认函数"
    assert "非交互环境下恢复会覆盖现有数据" not in src, "旧的自写文案应已删除"


def test_purge_prints_impact_through_shared_guard():
    src = (ROOT / "scripts" / "cli.py").read_text(encoding="utf-8")
    assert 'safety.confirm_destructive(\n                "物理删除记忆"' in src or \
           '"物理删除记忆"' in src, "purge 也要打印影响面"
    assert "此操作不可恢复" in src, "purge 的不可恢复提示要保留"


def _ensure_utf8_stdout() -> None:
    """stdout/stderr 切 UTF-8——Windows CI 是 cp1252，print 中文会直接崩。"""
    for _name in ("stdout", "stderr"):
        _s = getattr(sys, _name, None)
        _fn = getattr(_s, "reconfigure", None)
        if callable(_fn):
            try:
                _fn(encoding="utf-8", errors="replace")
                continue
            except (OSError, ValueError, AttributeError):
                pass
        try:
            import io
            _buf = getattr(_s, "buffer", None)
            if _buf is not None:
                setattr(sys, _name,
                        io.TextIOWrapper(_buf, encoding="utf-8", errors="replace",
                                         line_buffering=True))
        except Exception:
            pass


if __name__ == "__main__":
    _ensure_utf8_stdout()
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
    sys.exit(1 if failed else 0)
