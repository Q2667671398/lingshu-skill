#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自定义面板管理（panels.py）单元测试：纯标准库，无 pytest，直接运行即测。

运行：python scripts/test_panels.py
全部通过打印 OK 并退出码 0；任何断言失败抛异常退出码非 0。

用 BRAIN_MEMORY_HOME 环境变量把数据目录指到临时目录，测试不碰真实 ~/.brain-memory。
必须在 import panels/brain 之前设置环境变量，所以这里不用统一 import 头。
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

# 先落临时 HOME 再 import：core.HOME 在 import 时就读环境变量，顺序不能反
_TMP = tempfile.mkdtemp(prefix="lingshu-panels-test-")
os.environ["BRAIN_MEMORY_HOME"] = _TMP
os.environ.setdefault("BRAIN_MEMORY_READONLY", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain  # noqa: E402
import panels  # noqa: E402

_H = Path(_TMP)
PASS = 0


def ok(cond, msg):
    """极简断言：失败带中文消息抛 AssertionError，结束进程。"""
    global PASS
    if not cond:
        raise AssertionError("测试失败：" + msg)
    PASS += 1
    print("  ok -", msg)


def make_panel(root: Path, name: str, *, entry="index.html", html=True,
               manifest=None, drop_manifest=False, bad_json=False):
    """在 root 下造一个面板目录，可按需制造各类损坏形态。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if not drop_manifest:
        mf = manifest if manifest is not None else {
            "name": name, "title": name + " 面板", "version": "1.0.0",
            "author": "test", "description": "", "entry": entry,
            "lingshu_api": 1,
        }
        (d / "panel.json").write_text(
            "{ broken" if bad_json else json.dumps(mf, ensure_ascii=False),
            encoding="utf-8")
    if html:
        (d / entry).write_text("<html><body>" + name + "</body></html>",
                               encoding="utf-8")
    return d


def main():
    inst = panels.panels_dir()
    mkt = panels.market_dir()
    ok(inst == _H / "panels", f"已安装目录 = {inst}")
    ok(mkt == _H / "panel-market", f"市场目录 = {mkt}")
    ok(inst.exists() and mkt.exists(), "目录懒创建生效")

    # ---------- manifest 校验规则 ----------
    good = make_panel(inst, "good-panel")
    ok(panels.check_entry("good-panel") == "", "合法面板通过校验")

    make_panel(inst, "no-json", drop_manifest=True)
    ok("panel.json" in panels.check_entry("no-json"), "缺 manifest 判为损坏")

    make_panel(inst, "bad-json", bad_json=True)
    ok("解析失败" in panels.check_entry("bad-json"), "manifest 损坏判为损坏")

    # 注意：check_entry 入口先挡非法名（_panel_path 抛「非法面板名」），
    # 但 _load_manifest 对 name 不合法也有独立校验——这里两层都必须挡住
    ok("非法" in panels.check_entry("Bad_Name!"), "非法 name 判为损坏")
    # 目录名合法但 manifest name 不合法（含大写/特殊字符）→ manifest 校验挡住
    make_panel(inst, "badmf-name", manifest={"name": "Bad_Name!", "title": "x",
               "version": "1", "entry": "index.html", "lingshu_api": 1})
    ok("name 不合法" in panels.check_entry("badmf-name"), "manifest 内非法 name 判为损坏")

    make_panel(inst, "no-entry", drop_manifest=False, html=False)
    ok("entry 文件不存在" in panels.check_entry("no-entry"), "缺 entry 文件判为损坏")

    make_panel(inst, "mismatch", manifest={"name": "other", "title": "x",
               "version": "1", "entry": "index.html", "lingshu_api": 1})
    ok("不一致" in panels.check_entry("mismatch"), "name 与目录名不一致判为损坏")

    make_panel(inst, "wrong-api", manifest={"name": "wrong-api", "title": "x",
               "version": "1", "entry": "index.html", "lingshu_api": 99})
    ok("lingshu_api" in panels.check_entry("wrong-api"), "API 版本不符判为损坏")

    ok(panels.check_entry("Missing") != "", "不存在的面板预检报错")

    # ---------- list_panels / list_market ----------
    lp = panels.list_panels()
    names = [p["name"] for p in lp["panels"]]
    ok(names[0] == "default", "内置默认面板置顶")
    bad = {p["name"]: p for p in lp["panels"] if not p["valid"]}
    ok(set(bad) >= {"no-json", "bad-json", "badmf-name", "no-entry", "mismatch",
                    "wrong-api"},
       f"损坏面板列出且 valid=False（{sorted(bad)}）")
    ok(all("invalid_reason" in p for p in lp["panels"]), "每个条目都带损坏原因字段")
    ok(lp["active"] == "default", "未激活时 active=default")
    ok(lp["installed_dir"] == str(inst), "返回已安装目录路径")
    ok(all(p["source"] == "installed" for p in lp["panels"] if p["name"] != "default"),
       "列表来源标记 installed")

    lm = panels.list_market()
    ok(lm["panels"] == [] and lm["zips"] == [], "空市场返回空列表")

    # ---------- activate / current ----------
    panels.activate("good-panel")
    ok(panels._active_name() == "good-panel", "激活后 config 记录面板名")
    try:
        panels.activate("no-json")  # 损坏面板 activate 必须抛 ValueError
        ok(False, "损坏面板不允许激活")
    except ValueError:
        ok(True, "损坏面板激活被拒（ValueError）")
    # 上一步 activate("no-json") 抛异常 → active 仍是 good-panel
    ok(panels._active_name() == "good-panel", "激活失败不改变当前激活状态")
    panels.deactivate()
    ok(panels._active_name() is None, "回到默认面板")
    # config 损坏时读取兜底：直接写坏文件再读
    (_H / "config.json").write_text("{ broken", encoding="utf-8")
    ok(panels._active_name() is None, "config 损坏时读取回退默认")
    (_H / "config.json").write_text("{}", encoding="utf-8")

    # ---------- upload（剪切移动到市场） ----------
    make_panel(inst, "up-panel")
    r = panels.upload("up-panel")
    ok(r["ok"] and not (inst / "up-panel").exists() and (mkt / "up-panel").exists(),
       "upload 剪切移动：源消失、市场出现")
    # upload 后 up-panel 在市场里，install 复制回已安装目录（先移走 market 源免干扰后续）
    r = panels.install("up-panel")
    ok(r["ok"] and (inst / "up-panel").exists(), "install 把市场面板复制回已安装")
    try:
        panels.upload("Missing")
        ok(False, "上传不存在面板应报错")
    except ValueError:
        ok(True, "上传不存在的面板报 ValueError")
    try:
        panels.upload("bad-json")
        ok(False, "损坏面板不允许上传")
    except ValueError:
        ok(True, "损坏面板上传被拒")

    # ---------- install（从市场复制 / zip） ----------
    panels.activate("up-panel")  # 激活着，delete 应被拒；install 换个新面板不受影响

    # zip 安装：先打包市场目录再装
    import zipfile
    zpath = mkt / "zipped.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        pass  # 占位：真正测试用下面的 from-zip.zip
    # 改 zip 面板名为独立名字，避免和已装的 good-panel 冲突
    zpath2 = mkt / "from-zip.zip"
    with zipfile.ZipFile(zpath2, "w") as z:
        z.writestr("from-zip/panel.json", json.dumps(
            {"name": "from-zip", "title": "zip 面板", "version": "1",
             "entry": "index.html", "lingshu_api": 1}))
        z.writestr("from-zip/index.html", "<html>zip</html>")
    r = panels.install("from-zip")
    ok(r["ok"] and r["name"] == "from-zip" and (inst / "from-zip" / "index.html").exists(),
       "zip 安装解压落位成功")
    ok(not (inst / ".tmp-install").exists(), "zip 安装临时目录已清理")
    # 坏 zip（无 manifest）不落垃圾
    zb = mkt / "broken.zip"
    with zipfile.ZipFile(zb, "w") as z:
        z.writestr("x/readme.txt", "no manifest here")
    try:
        panels.install("broken")
        ok(False, "无 manifest 的 zip 应报错")
    except ValueError as e:
        ok("panel.json" in str(e), "坏 zip 报「未找到有效 panel.json」")
    ok(not (inst / "broken").exists(), "坏 zip 不留下半安装目录")

    # ---------- delete ----------
    try:
        panels.delete("up-panel", "installed")
        ok(False, "激活中的面板删除应被拒")
    except ValueError:
        ok(True, "激活中的面板禁止删除")
    panels.deactivate()
    panels.delete("up-panel", "installed")
    ok(not (inst / "up-panel").exists(), "未激活面板删除成功")
    try:
        panels.delete("default", "installed")
        ok(False, "内置面板删除应被拒")
    except ValueError:
        ok(True, "内置默认面板不可删除")

    # ---------- read_panel_file 安全 ----------
    body, ctype = panels.read_panel_file("good-panel", "index.html")
    ok(b"good-panel" in body and "html" in ctype, "面板静态文件可读且类型正确")
    try:
        panels.read_panel_file("good-panel", "../config.json")
        ok(False, "路径逃逸应被拒")
    except (PermissionError, ValueError):
        ok(True, ".. 路径逃逸被拒")
    (inst / "good-panel" / "secret.py").write_text("x = 1", encoding="utf-8")
    try:
        panels.read_panel_file("good-panel", "secret.py")
        ok(False, "白名单外扩展名应被拒")
    except PermissionError:
        ok(True, "非白名单扩展名 403")
    try:
        panels.read_panel_file("good-panel", "nope.css")
        ok(False, "不存在的文件应报错")
    except FileNotFoundError:
        ok(True, "缺失文件 404")

    # ---------- scaffold ----------
    r = panels.scaffold("demo-ai", "AI 演示面板")
    ok(r["ok"] and (inst / "demo-ai" / "panel.json").exists(), "脚手架生成 manifest")
    html_text = (inst / "demo-ai" / "index.html").read_text(encoding="utf-8")
    ok("/*__PANEL_TOKEN__*/" in html_text and "/api/overview" in html_text,
       "脚手架 HTML 含 token 占位符与 API 示例")
    ok(panels.check_entry("demo-ai") == "", "脚手架面板本身通过校验")
    try:
        panels.scaffold("demo-ai", "again")
        ok(False, "重名脚手架应报冲突")
    except FileExistsError:
        ok(True, "重名脚手架报 FileExistsError")
    try:
        panels.scaffold("../evil", "x")
        ok(False, "非法名脚手架应报错")
    except ValueError:
        ok(True, "非法面板名脚手架被拒")

    # ---------- open_folder（只验证路径返回，不真开窗口） ----------
    # 直接调 open_folder 会真的拉起资源管理器：测试目录在主() 结束时被 rmtree，
    # 桌面上就弹出「位置不可用」错误框（Windows 用户实测踩到）。把打开动作打桩
    # 成空操作，只断言返回路径；顺带验证打开失败不影响返回值。
    with mock.patch.object(panels.os, "startfile", lambda *a, **k: None, create=True), \
            mock.patch.object(panels.subprocess, "Popen", lambda *a, **k: None):
        ok(panels.open_folder("installed") == str(inst), "open_folder 返回已安装目录路径")
        ok(panels.open_folder("market") == str(mkt), "open_folder 返回市场目录路径")

    print(f"\n全部 {PASS} 项断言通过 ✔")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)  # 测试数据目录清理，不留垃圾
