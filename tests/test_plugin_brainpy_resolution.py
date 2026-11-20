# -*- coding: utf-8 -*-
"""插件「找得到 brain.py 与 python」的回归（v3.53）。

背景（真实事故）：2026-11-20 用户报"记忆又不录入了"。查到最后是插件的自动探测
失效——根包 `index.js` 注册 runtime 技能时**没给 `resourceBase`/`path`**，DSH 的
`runtimeCandidate` 就只在有 path 时才把位置暴露给 `list()`，于是 qsubg 插件查不到
`scripts/brain.py`，startup 注入、即记即存（extract）、天级任务全部**静默停摆**：
`~/.dsh/lingshu-cron-state.json` 的 `days` 停在 11-19、`hippocampus` 不再增长，
而插件里所有异常都必须吞掉（事件串行，抛出会回滚会话），所以没有任何报错可查。

锁四件事：
  1. `index.js` 注册技能时带 `resourceBase`（directory 指向包根）；
  2. 两个子插件都能**按自身位置**算出 `<包根>/scripts/brain.py`，不依赖宿主状态；
  3. python 探测覆盖 PATH（ServBay / Store / conda 等布局），不再只认硬编码目录；
  4. 探测失败会写 `~/.dsh/lingshu-diag.json`，不再静默。

本测试在有 node 时做**真实模块导入**（插件零外部依赖，可直接 import），
没有 node 时退化为源码断言并明确标注 SKIP。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STARTUP = ROOT / "cordis-plugin" / "lingshu-startup" / "plugin.js"
CRON = ROOT / "cordis-plugin" / "lingshu-cron" / "plugin.js"
INDEX = ROOT / "index.js"

_TMPDIRS: list[Path] = []


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _find_node() -> str | None:
    for name in ("node", "node.exe"):
        exe = shutil.which(name)
        if exe:
            return exe
    # Windows 上 ServBay / 官方安装器的常见落点
    for cand in (
        r"D:\ServBay\bin\node.exe",
        r"C:\Program Files\nodejs\node.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "node.exe"),
    ):
        if Path(cand).is_file():
            return cand
    return None


def _run_node(script: str, node: str) -> tuple[int, str]:
    d = Path(tempfile.mkdtemp(prefix="ls-node-"))
    _TMPDIRS.append(d)
    f = d / "probe.mjs"
    f.write_text(script, encoding="utf-8")
    r = subprocess.run([node, str(f)], capture_output=True, text=True, encoding="utf-8", timeout=120)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# ---------------------------------------------------------------- 源码断言（永远跑）


def test_index_registers_resource_base():
    src = _read(INDEX)
    assert "resourceBase" in src, "index.js 注册技能时必须带 resourceBase（v3.53 事故根因）"
    assert "kind: 'directory'" in src, "resourceBase 必须是 directory 类型"
    assert "path: here" in src, "resourceBase.path 必须指向包根（here）"


def test_plugins_resolve_brain_py_relative_to_package():
    for p in (STARTUP, CRON):
        src = _read(p)
        assert "PACKAGE_ROOT" in src, f"{p.name} 应算出包根"
        assert "brainPyCandidates" in src, f"{p.name} 应有可测试的候选清单函数"
        assert "pythonCandidates" in src, f"{p.name} 应有可测试的 python 候选函数"
        assert "'scripts', 'brain.py'" in src, f"{p.name} 候选里应包含包内 scripts/brain.py"
        # 包内相对路径必须**排在注册表探测之前**（宿主状态不可信）
        body = src[src.index("async function findBrainPy"):]
        head = body[: body.index("registry")] if "registry" in body else body
        assert "brainPyCandidates()" in head, f"{p.name} 应先用包内相对路径，再问注册表"


def test_python_detection_scans_path():
    for p in (STARTUP, CRON):
        src = _read(p)
        assert "process.env.PATH" in src, f"{p.name} 应在 PATH 里找 python.exe"
        assert "delimiter" in src, f"{p.name} 应用 path.delimiter 切分 PATH（跨平台）"
        assert "python3.exe" in src, f"{p.name} 也应认 python3.exe"


def test_diag_is_written_on_failure():
    for p in (STARTUP, CRON):
        src = _read(p)
        assert "lingshu-diag.json" in src, f"{p.name} 失败时要写 diag 文件"
        assert "brainPy-not-found" in src, f"{p.name} 应区分失败原因"


# ---------------------------------------------------------------- 真实导入（有 node 时）


def test_real_import_resolves_package_brain_py():
    node = _find_node()
    if not node:
        print("   SKIP 未找到 node，跳过真实模块导入")
        return
    script = f"""
import {{ pathToFileURL }} from 'node:url'
import {{ existsSync }} from 'node:fs'
const startup = await import(pathToFileURL({json.dumps(str(STARTUP).replace(chr(92), '/'))}).href)
const cron = await import(pathToFileURL({json.dumps(str(CRON).replace(chr(92), '/'))}).href)
const sb = startup.brainPyCandidates()
const cb = cron.brainPyCandidates()
console.log(JSON.stringify({{
  sb0: sb[0], cb0: cb[0],
  exists: existsSync(sb[0]) && existsSync(cb[0]),
  same: sb[0] === cb[0],
  pyCount: cron.pythonCandidates().length,
  pyHit: cron.pythonCandidates().find((p) => existsSync(p)) || null,
}}))
"""
    rc, out = _run_node(script, node)
    assert rc == 0, f"node 导入插件失败：{out[:400]}"
    data = json.loads(out.strip().splitlines()[-1])
    assert data["same"], f"两个插件的包内候选应一致：{data}"
    assert data["sb0"].replace("\\", "/").endswith("/scripts/brain.py"), data["sb0"]
    assert data["exists"], f"包内 scripts/brain.py 应存在：{data['sb0']}"
    assert data["pyCount"] > 20, "python 候选应包含 PATH 扫描结果"
    if data["pyHit"]:
        assert Path(data["pyHit"]).is_file(), data["pyHit"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    try:
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
    finally:
        for d in _TMPDIRS:
            shutil.rmtree(d, ignore_errors=True)
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
