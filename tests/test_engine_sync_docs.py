# -*- coding: utf-8 -*-
"""engine 副本同步（含文档）回归测试。

背景：插件里随包发 SKILL.md / README / CHANGELOG / docs，但 sync_engine 以前只管
.py，实测 engine 里躺着一份 **v2.87** 的 SKILL.md（主仓库 v3.47，差 60 个版本），
AstrBot 用户拿到的说明书是半年前的。本测试锁住三件事：

  - `.py` 与文档不一致时 `--check` 必须红（不能只查脚本）；
  - `sync` 之后两边逐字一致（换行归一化后）；
  - 主仓库删掉的文档，副本要跟着删——不给插件留过期页面；
  - 全部一致时 `check()` 返回 0。

跑法：python tests/test_engine_sync_docs.py   （退出码 0 = 全过）
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sync_engine  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (("  -> " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(name)


class FakeRepo:
    """搭一个最小仓库布局：根 scripts/*.py + 根文档 + engine 副本目录。"""

    def __init__(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.plugin = self.root / "scripts" / "astrbot_plugin_lingshu"
        self.engine = self.plugin / "engine"
        self.scripts = self.root / "scripts"
        self.dst = self.engine / "scripts"
        for d in (self.scripts, self.dst, self.engine / "docs"):
            d.mkdir(parents=True, exist_ok=True)

    def write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        # newline="" 关掉 Windows 的行尾翻译：测试要的是「确定的 LF 内容」，
        # 否则磁盘上是 CRLF，后面拿字节手工造 CRLF 会变成 \r\r\n。
        p.write_text(text, encoding="utf-8", newline="")

    def write_engine(self, rel: str, text: str) -> None:
        p = self.engine / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="")

    def __enter__(self):
        self._saved = (sync_engine.ROOT, sync_engine.SRC, sync_engine.DST, sync_engine.ENGINE)
        sync_engine.ROOT = self.root
        sync_engine.SRC = self.scripts
        sync_engine.DST = self.dst
        sync_engine.ENGINE = self.engine
        return self

    def __exit__(self, *exc):
        sync_engine.ROOT, sync_engine.SRC, sync_engine.DST, sync_engine.ENGINE = self._saved
        self.td.cleanup()
        return False


def quiet(fn, *a):
    """吞掉被调函数的 stdout/stderr，测试只关心返回值。"""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        return fn(*a)


def main() -> int:
    with FakeRepo() as repo:
        # 主仓库：一个脚本 + 三份文档（含 docs/ 子目录）
        repo.write("scripts/brain.py", "print('hi')\n")
        repo.write("SKILL.md", "---\nname: lingshu\nversion: 3.47\n---\n")
        repo.write("docs/commands.md", "# 命令\n")
        repo.write("docs/recovery.md", "# 恢复\n")

        # 1) 副本全空 → check 必须红（脚本缺失 + 文档缺失）
        check("空副本 → check 红", quiet(sync_engine.check) == 1)

        # 2) 同步一次 → 全绿
        quiet(sync_engine.sync)
        check("同步后 check 绿", quiet(sync_engine.check) == 0)
        check("脚本副本已就位", (repo.dst / "brain.py").is_file())
        check("SKILL.md 副本内容一致",
              (repo.engine / "SKILL.md").read_text(encoding="utf-8") == "---\nname: lingshu\nversion: 3.47\n---\n")
        check("docs/ 子目录副本已就位", (repo.engine / "docs" / "recovery.md").is_file())

        # 3) CRLF 与 LF 视为同一内容（历史教训：裸 sha 比较全误报）
        (repo.engine / "SKILL.md").write_bytes(
            (repo.root / "SKILL.md").read_bytes().replace(b"\n", b"\r\n"))
        check("只有换行差异 → 不算漂移", quiet(sync_engine.check) == 0)
        # 4) 文档真变了 → check 必须红（这是本次修的核心：以前只查 .py）
        (repo.engine / "SKILL.md").write_text("---\nversion: 2.87\n---\n", encoding="utf-8")
        check("文档过期 → check 红", quiet(sync_engine.check) == 1)
        quiet(sync_engine.sync)
        check("再同步后恢复绿", quiet(sync_engine.check) == 0)
        check("SKILL.md 已被刷成主仓库版本", "3.47" in (repo.engine / "SKILL.md").read_text(encoding="utf-8"))

        # 5) 主仓库删掉文档 → 副本跟着删（不留过期页面）
        (repo.root / "docs" / "recovery.md").unlink()
        check("主仓库删了、副本还在 → check 红", quiet(sync_engine.check) == 1)
        quiet(sync_engine.sync)
        check("同步后副本被删除", not (repo.engine / "docs" / "recovery.md").exists())
        check("清理后 check 绿", quiet(sync_engine.check) == 0)

    # 6) B 方案契约：面板服务与面板产物必须随插件副本走
    #    （以前 panel_server.py 在 SKIP 里，插件里只剩一份跑不起来的 panel.html）
    check("panel_server.py 不再被排除在副本外", "panel_server.py" not in sync_engine.SKIP)
    check("面板产物在同步清单里", "assets/panel.html" in sync_engine.DOC_FILES)

    # 7) 真实仓库自检（这个测试文件跑在真仓库里，顺手确认现状是绿的）
    real = quiet(sync_engine.check)
    check("真实仓库 engine 副本当前一致", real == 0, f"check()={real}")

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + ", ".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
