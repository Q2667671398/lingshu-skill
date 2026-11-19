# -*- coding: utf-8 -*-
"""v3.25：IM 子插件的配置入口回归（#36）。

背景：根包挂载 lingshu-im 时以前传的是 `{}`，autoRecall/autoCapture 永远是默认值
（都关），用户在 profile 里怎么配都传不进来。修好后：
  1. index.js 把配置透传给三个子插件；
  2. lingshu-im 支持 `{autoRecall:true}` 或 `{im:{autoRecall:true}}`；
  3. 没配时回退 `~/.brain-memory/config.json` 的 `im` 段；
  4. 都没有 → 默认关（跨会话隐私）。

静态契约 + 有 node 时做一次真实读取（没 node 就跳过运行时那半）。
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
INDEX = ROOT / "index.js"
IM = ROOT / "cordis-plugin" / "lingshu-im" / "plugin.js"


def test_index_passes_config_to_subplugins():
    src = INDEX.read_text(encoding="utf-8")
    assert "imPlugin.apply(ctx, imCfg)" in src, "im 配置没有透传"
    assert "startupPlugin.apply(ctx, cfg.startup" in src
    assert "cronPlugin.apply(ctx, cfg.cron" in src


def test_im_has_brain_config_fallback():
    src = IM.read_text(encoding="utf-8")
    assert "export function readBrainImConfig" in src
    assert "readBrainImConfig()" in src
    # 默认值必须仍是关（隐私默认）
    assert "autoRecall: false" in src
    assert "autoCapture: false" in src


def test_im_reads_config_json_at_runtime():
    node = os.environ.get("LINGSHU_NODE") or shutil.which("node")
    if not node:
        print("SKIP: 没有 node，跳过运行时读取检查")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        home = Path(td)
        (home / "config.json").write_text(
            json.dumps({"im": {"autoRecall": True, "recallBudget": 999}}, ensure_ascii=False),
            encoding="utf-8")
        code = (
            "import(" + json.dumps(IM.as_uri()) + ").then(m => {"
            "  const c = m.readBrainImConfig();"
            "  console.log(JSON.stringify({autoRecall: c.autoRecall, recallBudget: c.recallBudget}));"
            "}).catch(e => { console.error(e); process.exit(1) })"
        )
        env = dict(os.environ, BRAIN_MEMORY_HOME=str(home))
        r = subprocess.run([node, "--input-type=module", "-e", code],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=60)
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout.strip().splitlines()[-1])
        assert data == {"autoRecall": True, "recallBudget": 999}, data


if __name__ == "__main__":
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
