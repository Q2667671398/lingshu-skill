#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""panel_cloud 冒烟测试（离线部分）：配置存取/脱敏/缓存/发布前置校验。"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="lingshu-cloud-test-")
os.environ["BRAIN_MEMORY_HOME"] = _TMP
os.environ.setdefault("BRAIN_MEMORY_READONLY", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain  # noqa: E402
import panels  # noqa: E402
import panel_cloud  # noqa: E402

PASS = 0


def ok(cond, msg):
    global PASS
    assert cond, msg
    PASS += 1
    print(f"  ok - {msg}")


def main():
    # 配置存取：非法仓库格式拒绝，合法写入，token 脱敏
    try:
        panel_cloud.set_config(gitee_repo="bad-format")
        raise AssertionError("非法仓库格式应被拒")
    except ValueError:
        ok(True, "非法仓库格式（无 /）被拒")
    panel_cloud.set_config(gitee_repo="demo/panels", gitee_token="gtok123456",
                           github_repo="demo/gh", github_token="htok123456",
                           branch="main")
    st = panel_cloud.status()
    ok(st["gitee_repo"] == "demo/panels", "Gitee 仓库写入")
    ok(st["gitee_token_mask"] == "gtok****", "token 脱敏只露前 4 位")
    ok(st["branch"] == "main", "分支写入")
    ok(st["publish_ready"], "Gitee 仓库+token 就绪 → publish_ready")
    # 清空配置
    panel_cloud.set_config()
    ok(not panel_cloud.status()["gitee_ready"], "clear 后 gitee_ready=False")

    # cloud_list 未配置：空列表不报错
    r = panel_cloud.cloud_list(force=True)
    ok(r["panels"] == [], "未配置时云端列表为空")

    # cloud_publish 前置校验：面板不存在 → ValueError；截图超大 → ValueError
    try:
        panel_cloud.cloud_publish("no-such-panel")
        raise AssertionError("不存在面板应被拒")
    except ValueError:
        ok(True, "发布不存在面板被拒")
    try:
        panel_cloud.cloud_publish("x", screenshot=b"a" * (panel_cloud.SHOT_MAX + 1))
        raise AssertionError("超大截图应被拒")
    except ValueError:
        ok(True, "超过 2MB 截图被拒")
    try:
        panel_cloud.cloud_publish("x", shot_ext=".gif")
        raise AssertionError("非法截图格式应被拒")
    except ValueError:
        ok(True, "gif 截图格式被拒")

    # cloud_shot 扩展名白名单
    try:
        panel_cloud.cloud_shot("x", ".exe")
        raise AssertionError("exe 截图应被拒")
    except ValueError:
        ok(True, "截图扩展名白名单拦截 .exe")

    # 发布真实面板：未配置云端 → RuntimeError（Gitee 未配置）
    d = panels.scaffold("cloud-demo", "云端演示")
    ok(Path(d["path"]).exists(), "脚手架面板已生成")
    try:
        panel_cloud.cloud_publish("cloud-demo")
        raise AssertionError("未配置云端应报发布失败")
    except RuntimeError:
        ok(True, "未配置云端时发布报 RuntimeError")

    print(f"\n全部 {PASS} 项断言通过 ✔")


if __name__ == "__main__":
    import shutil
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
