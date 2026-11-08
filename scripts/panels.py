#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 自定义面板管理：目录扫描、manifest 校验、激活、本机市场、上传/安装/删除。

面板约定（规范详见 docs/panel-api.md）：
- 已安装目录：brain.HOME / "panels"，每个面板一个子目录 <面板名>/；
- 面板市场目录：brain.HOME / "panel-market"（本机共享库，拷贝目录即可流通，
  本项目无云端后端，市场 = 本机目录，不假装有真云市场）；
- 子目录必须含 panel.json（manifest）：{"name","title","version","entry","lingshu_api"}；
- 内置默认面板是 assets/panel.html，永远存在、不可删除（name = "default"）；
- 激活状态记在 config.json 的 "panel.active"（缺省 = 内置默认面板）；
- 前端外壳（默认面板）启动时拉 /api/panels，active 非 default 就用 iframe 热加载
  自定义面板，出错/超时自动回退——后端只负责如实报告面板状态，兜底在前端。

只依赖标准库 + brain（core），供 panel_server.py 转发调用。
"""

from __future__ import annotations

import io
import html
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain  # noqa: E402  (core.py 的 re-export，含 HOME / load_config / atomic_json)

# 内置默认面板名：面板列表里排在最前，标记「内置」，不可删除/上传
DEFAULT_NAME = "default"
# 静态文件白名单扩展名：超出者 GET /api/panels/file 一律 403（防源码/凭据外带）
FILE_WHITELIST = {".html", ".htm", ".js", ".css", ".json", ".png", ".jpg", ".jpeg",
                  ".svg", ".ico", ".gif", ".webp", ".woff", ".woff2", ".ttf", ".map"}
# manifest name 规则：小写字母/数字/连字符/下划线/中文，1~32 字符
# 中文允许是产品决定：面板名直接当目录名，用户可见可读比纯 ASCII 更友好
_NAME_RE = re.compile(r"^[a-z0-9-_\u4e00-\u9fff]{1,32}$")
# 当前支持的 API 版本：manifest.lingshu_api 必须等于 1（未来不兼容时升版本号）
SUPPORTED_API = 1
# zip 解压防护：解压后总大小 ≤ 50MB、文件数 ≤ 500（zip bomb / 磁盘塞爆）
ZIP_MAX_TOTAL = 50 * 1024 * 1024
ZIP_MAX_FILES = 500


# ---------------------------------------------------------------- 目录与路径

def panels_dir() -> Path:
    """已安装面板根目录：brain.HOME/panels（懒创建，只读模式下不创建）。"""
    d = brain.HOME / "panels"
    if not brain.READONLY:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return d


def market_dir() -> Path:
    """面板市场（本机共享库）目录：brain.HOME/panel-market。

    流通方式：整目录拷贝 / zip 互相传——文档里写清楚，不假装有云端市场。
    """
    d = brain.HOME / "panel-market"
    if not brain.READONLY:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return d


def _panel_path(root: Path, name: str) -> Path:
    """面板目录拼路径。name 已过正则校验，天然无路径分隔符，这里再防一层。"""
    if not _NAME_RE.match(name or ""):
        raise ValueError("非法面板名")
    return root / name


def _safe_rel(panel_root: Path, rel: str) -> Path:
    """相对路径规范化后必须仍在 panel_root 内（防 .. 逃逸），返回绝对路径。"""
    p = (panel_root / rel).resolve()
    if not (p == panel_root.resolve() or panel_root.resolve() in p.parents):
        raise PermissionError("路径越出面板目录")
    return p


# ---------------------------------------------------------------- manifest

def _load_manifest(panel_root: Path) -> dict:
    """读并校验 panel.json。任何问题抛 ValueError（调用方转为 invalid 状态）。"""
    mf = panel_root / "panel.json"
    if not mf.exists():
        raise ValueError("缺少 panel.json")
    try:
        data = json.loads(mf.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ValueError(f"panel.json 解析失败：{exc}")
    if not isinstance(data, dict):
        raise ValueError("panel.json 必须是对象")
    name = str(data.get("name") or "")
    if not _NAME_RE.match(name):
        raise ValueError(f"name 不合法（小写字母/数字/-/_/中文，1~32 字符）：{name!r}")
    if name != panel_root.name:
        raise ValueError(f"name ({name}) 与目录名 ({panel_root.name}) 不一致")
    if not str(data.get("title") or "").strip():
        raise ValueError("缺少 title（展示名）")
    api_ver = data.get("lingshu_api")
    if api_ver != SUPPORTED_API:
        raise ValueError(f"lingshu_api 必须为 {SUPPORTED_API}，当前是 {api_ver!r}")
    entry = str(data.get("entry") or "index.html")
    if entry.startswith(("/", "\\")) or ".." in Path(entry).parts or Path(entry).is_absolute():
        raise ValueError(f"entry 不允许绝对路径或 ..：{entry!r}")
    if not (panel_root / entry).is_file():
        raise ValueError(f"entry 文件不存在：{entry}")
    return {
        "name": name,
        "title": str(data.get("title") or name),
        "version": str(data.get("version") or "0.0.0"),
        "author": str(data.get("author") or ""),
        "description": str(data.get("description") or ""),
        "entry": entry,
    }


def _scan_dir(root: Path, source: str) -> list:
    """扫描一个面板根目录（installed 或 market）→ 面板条目列表。

    坏面板 valid=False 照样列出（用户能看见并删除），绝不抛异常影响其他面板。
    """
    items: list = []
    if not root.exists():
        return items
    for d in sorted(root.iterdir()):
        if d.name.startswith(".") or not d.is_dir():
            continue  # 隐藏目录/文件、zip 都不按面板目录算
        try:
            mf = _load_manifest(d)
            items.append({**mf, "valid": True, "invalid_reason": "", "source": source,
                          "dir": str(d), "has_entry": True})
        except ValueError as exc:
            # 损坏面板：不参与加载，但留在管理列表里供用户删除/修复
            items.append({"name": d.name, "title": d.name, "version": "?", "author": "",
                          "description": "", "entry": "", "valid": False,
                          "invalid_reason": str(exc), "source": source,
                          "dir": str(d), "has_entry": False})
        except OSError:
            continue  # 目录读不了（权限/被占用）：跳过，不影响其他面板
    return items


def _scan_market_zips(root: Path) -> list:
    """市场目录里的 zip 也列出来（source=market-zip），安装时现场解压校验。"""
    items: list = []
    if not root.exists():
        return items
    for f in sorted(root.glob("*.zip")):
        try:
            # zip 名去扩展名当展示名；真实 name 安装时从包内 manifest 读
            items.append({"name": f.stem, "title": f.stem, "version": "?", "author": "",
                          "description": "zip 安装包（安装时解压校验）", "entry": "",
                          "valid": True, "invalid_reason": "", "source": "market-zip",
                          "dir": str(f), "has_entry": False, "zip": True})
        except OSError:
            continue
    return items


# ---------------------------------------------------------------- 列表/读取

def list_panels() -> dict:
    """GET /api/panels 的返回体：active + 已安装列表 + 目录路径。"""
    builtin = {
        "name": DEFAULT_NAME, "title": "内置默认面板", "version": "builtin",
        "author": "灵枢", "description": "随灵枢本体发布的默认面板（assets/panel.html）",
        "entry": "panel.html", "valid": True, "invalid_reason": "",
        "source": "builtin", "dir": "assets", "has_entry": True,
    }
    items = [builtin] + _scan_dir(panels_dir(), "installed")
    active = _active_name()
    return {
        "active": active or DEFAULT_NAME,
        "panels": items,
        "installed_dir": str(panels_dir()),
        "market_dir": str(market_dir()),
    }


def list_market() -> dict:
    """GET /api/panels/market 的返回体：本机市场目录扫描结果（无云端，纯本机）。"""
    root = market_dir()
    return {"market_dir": str(root), "panels": _scan_dir(root, "market"),
            "zips": _scan_market_zips(root)}


def _active_name() -> str | None:
    """当前激活的自定义面板名；未激活/激活的是默认面板 → None。"""
    try:
        v = brain.load_config().get("panel.active")
    except Exception:
        return None
    return str(v) if v else None


def check_entry(name: str) -> str:
    """activate 前预检：manifest+entry 都能过则返回 ""，否则返回错误原因。"""
    try:
        _load_manifest(_panel_path(panels_dir(), name))
        return ""
    except Exception as exc:
        return str(exc)


# ---------------------------------------------------------------- 动作

def _save_active(name: str | None) -> None:
    """把激活面板写进 config.json（加锁读改写，与 CLI 并发写安全）。"""
    with brain._locked(brain.HOME / "config.json"):
        cfg = brain.load_config()
        if name:
            cfg["panel.active"] = name
        else:
            cfg.pop("panel.active", None)
        brain.atomic_json(brain.HOME / "config.json", cfg)


def activate(name: str) -> None:
    """激活自定义面板（热切换：前端拉列表后换 iframe，服务进程不动）。"""
    if name in ("", DEFAULT_NAME, "None"):
        _save_active(None)
        return
    err = check_entry(name)
    if err:
        raise ValueError(f"面板无法加载：{err}")
    _save_active(name)


def deactivate() -> None:
    """回到内置默认面板。"""
    _save_active(None)


def delete(name: str, source: str) -> None:
    """删除 installed 或 market 里的面板目录；激活中的面板不允许删（400）。"""
    if name == DEFAULT_NAME:
        raise ValueError("内置默认面板不可删除")
    root = panels_dir() if source == "installed" else market_dir()
    d = _panel_path(root, name)
    if not d.exists():
        raise ValueError(f"面板不存在：{name}")
    if source == "installed" and _active_name() == name:
        raise ValueError("该面板正在使用中，请先切换回默认面板再删除")
    shutil.rmtree(d, ignore_errors=False)


def upload(name: str) -> dict:
    """上传 = 把已安装面板「剪切移动」到市场目录（shutil.move）。

    目标已存在 → FileExistsError（server 层转 409，提示改名）。
    """
    src = _panel_path(panels_dir(), name)
    if not src.exists():
        raise ValueError(f"已安装面板不存在：{name}")
    _load_manifest(src)  # 损坏面板不允许上传分享
    dst = _panel_path(market_dir(), name)
    if dst.exists():
        raise FileExistsError(f"市场里已有同名面板：{name}")
    shutil.move(str(src), str(dst))
    return {"ok": True, "market": str(dst)}


def install(name: str) -> dict:
    """从市场安装到已安装目录。

    市场里的分享源用「复制」保留（供本机其他用户/下次继续安装）；
    目标已存在 → FileExistsError（409）；zip 形式则现场解压校验后落位。
    """
    mroot = market_dir()
    mdir = _panel_path(mroot, name)
    if mdir.exists():
        mf = _load_manifest(mdir)  # 先校验再装：坏面板不落盘
        dst = _panel_path(panels_dir(), mf["name"])
        if dst.exists():
            raise FileExistsError(f"已安装目录里已有同名面板：{mf['name']}")
        shutil.copytree(mdir, dst)
        return {"ok": True, "name": mf["name"], "title": mf["title"],
                "version": mf["version"]}
    zpath = _panel_path(mroot, name)
    if not zpath.exists():
        zpath = mroot / f"{name}.zip"
    if zpath.suffix.lower() != ".zip" or not zpath.exists():
        raise ValueError(f"市场里没有该面板（目录或 zip）：{name}")
    # zip 安装：解到临时目录校验 manifest 通过后再落位（坏包不留垃圾）
    return _install_zip(zpath)


def _install_zip(zpath: Path) -> dict:
    """解压市场 zip 安装：总量/文件数/路径三重防护，manifest 校验后才落位。"""
    data = zpath.read_bytes()
    tmp = panels_dir() / ".tmp-install"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        _extract_zip_safe(data, tmp)
        # 定位 zip 里的 panel.json：根目录/顶层目录都兼容
        for cand in [tmp, *sorted(tmp.iterdir())]:
            if (cand / "panel.json").is_file():
                try:
                    mf = _load_manifest(cand)
                except ValueError:
                    continue
                dst = _panel_path(panels_dir(), mf["name"])
                if dst.exists():
                    raise FileExistsError(f"已安装目录里已有同名面板：{mf['name']}")
                shutil.move(str(cand), str(dst))
                return {"ok": True, "name": mf["name"], "title": mf["title"],
                        "version": mf["version"]}
        raise ValueError("zip 内未找到有效的 panel.json")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _extract_zip_safe(data: bytes, dest: Path) -> None:
    """安全解压 zip：总量/文件数/路径/扩展名四重防护（zip bomb 与路径逃逸）。"""
    # 面板是纯静态资源，不允许 zip 内带可执行内容（防整目录移动后 .js/.exe 冒充面板）
    allowed_ext = {".json", ".html", ".htm", ".css", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".txt", ".md"}
    total = 0
    count = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        dest_real = dest.resolve()
        for info in z.infolist():
            nm = info.filename
            if nm.startswith("/") or ".." in Path(nm).parts or (len(nm) > 1 and nm[1] == ":"):
                raise ValueError(f"zip 内含非法路径：{nm!r}")
            # 白名单校验：解析后的绝对路径必须仍在目标目录内（双写绕过/链接条目兜底）
            if not (dest_real / nm).resolve().is_relative_to(dest_real):
                raise ValueError(f"zip 内路径逃逸：{nm!r}")
            if not nm.endswith("/"):
                ext = Path(nm).suffix.lower()
                if ext not in allowed_ext:
                    raise ValueError(f"zip 内含不允许的文件类型：{nm!r}（仅允许静态资源）")
            total += info.file_size
            if total > ZIP_MAX_TOTAL:
                raise ValueError("zip 解压后超过 50MB 上限")
            count += 1
            if count > ZIP_MAX_FILES:
                raise ValueError("zip 文件数超过 500 上限")
        z.extractall(dest)


# ---------------------------------------------------------------- 拓展商店（宿主适配插件）

# 项目内随包分发的适配插件目录：kind → (相对路径, 宿主名)
# usage = 装完之后的接入教程（面板拓展卡片展示）；复制路径等操作由用户手动完成
_BUILTIN_EXTENSIONS = [
    {"id": "astrbot_plugin_lingshu", "host": "AstrBot", "rel": "scripts/astrbot_plugin_lingshu",
     "title": "灵枢记忆桥", "desc": "把群聊/私聊记忆接入 AstrBot，含群友隐私保护"
     "（本人同意才能对外分享关于自己的记忆）。subprocess 进程隔离调用灵枢 CLI。",
     "usage": "① 点击「安装」→ 插件被导出到 ~/.brain-memory/extensions/installed/"
              "astrbot_plugin_lingshu/；② 把整个目录复制到 AstrBot 的 data/plugins/ 下；"
              "③ 在 AstrBot 插件管理页重载（或重启 AstrBot）；④ 群/私聊发送"
              "「lingshu 帮助」验证接入，可用指令：lingshu 记住/找回/同意/拒绝/忘记。"},
    {"id": "lingshu-startup", "host": "cordis (Koishi)", "rel": "cordis-plugin/lingshu-startup",
     "title": "灵枢启动器", "desc": "宿主启动时通过 brain.py startup 注入灵枢会话上下文。",
     "usage": "① 点击「安装」→ 插件被导出到 ~/.brain-memory/extensions/installed/"
              "lingshu-startup/；② 把整个目录复制到 Koishi/cordis 的插件目录并安装依赖；"
              "③ 在 Koishi 控制台「插件配置」里启用 lingshu-startup；"
              "④ 重启后机器人收到首条消息即自动注入灵枢记忆上下文。"},
]


def _extension_meta(d: Path) -> dict:
    """尽力读一个适配插件的元信息（yaml/py 头注释），读不到给默认值。"""
    meta = {"version": "?", "author": ""}
    y = d / "metadata.yaml"
    if y.is_file():
        try:
            txt = y.read_text(encoding="utf-8-sig")
            m = re.search(r"^version:\s*(\S+)", txt, re.M)
            if m:
                meta["version"] = m.group(1)
            m = re.search(r"^author:\s*(\S+)", txt, re.M)
            if m:
                meta["author"] = m.group(1)
        except OSError:
            pass
    return meta


def list_extensions() -> dict:
    """GET /api/extensions：拓展商店清单。

    两类来源：
    1. 内置（随灵枢本体发布）：项目 scripts/ / cordis-plugin/ 下的适配插件；
    2. 自定义（用户/第三方）：brain.HOME/extensions/<kind>/ 目录，整目录拷进来即可，
       manifest 元信息尽力读取，读不到也能展示/安装。
    「安装」= 把插件目录复制到对应宿主的插件目录之外的可分发位置再由用户搬运，
    这里只负责把内置插件导出到 brain.HOME/extensions/installed/（zip 不参与，纯目录），
    真正接入宿主（如 AstrBot 的 plugins 目录）仍需用户手动放置——面板里写清楚。
    """
    root = brain.HOME / "extensions"
    installed = root / "installed"
    items = []
    proj = brain.PROJECT_ROOT if hasattr(brain, "PROJECT_ROOT") else Path(__file__).resolve().parent.parent
    for spec in _BUILTIN_EXTENSIONS:
        src = proj / spec["rel"]
        entry = {
            "id": spec["id"], "title": spec["title"], "host": spec["host"],
            "desc": spec["desc"], "usage": spec.get("usage", ""), "source": "builtin",
            "dir": str(src), "exists": src.is_dir(),
        }
        if src.is_dir():
            entry.update(_extension_meta(src))
        # 已导出过？标记 installed，面板上显示「已安装」而不是重复导出
        entry["exported"] = (installed / spec["id"]).is_dir()
        if entry["exported"]:
            entry["dir"] = str(installed / spec["id"])
        items.append(entry)
    if installed.is_dir():
        for d in sorted(installed.iterdir()):
            if d.name.startswith(".") or not d.is_dir():
                continue
            if any(x["id"] == d.name for x in _BUILTIN_EXTENSIONS):
                continue  # 内置同名已列过
            e = {"id": d.name, "title": d.name, "host": "自定义", "desc": "",
                 "source": "custom", "dir": str(d), "exists": True, "exported": True}
            e.update(_extension_meta(d))
            items.append(e)
    # 云端商店条目：Gitee/GitHub 仓库 index.json 的 extensions 数组（同仓库 panels/ 子目录平级）。
    # 单独展示为 source:"cloud"，点下载走 cloud_extension_download 单独拉 zip，不拉整个仓库。
    cloud_ids = {x["id"] for x in items}
    cloud: list = []
    try:
        import panel_cloud  # 延迟导入：无云端配置时零开销
        data = panel_cloud.cloud_list()
        for ce in data.get("extensions", []):
            cid = str(ce.get("id") or ce.get("name") or "")
            if not cid or cid in cloud_ids:
                continue
            cloud_ids.add(cid)
            cloud.append({
                "id": cid, "title": str(ce.get("title") or cid),
                "host": str(ce.get("host") or "云端"),
                "desc": str(ce.get("description") or ce.get("desc") or ""),
                "usage": str(ce.get("usage") or ""),
                "version": str(ce.get("version") or ""),
                "author": str(ce.get("author") or ""),
                "source": "cloud", "exists": True,
                "exported": (root / "installed" / cid).is_dir(),
                "size": ce.get("size", 0),
            })
        if data.get("errors"):
            cloud.append({"id": "", "source": "cloud-error",
                          "desc": "；".join(data["errors"])})
    except Exception:
        pass  # 没配置云端/网络失败：商店照常显示本机条目
    return {"extensions": items + cloud, "installed_dir": str(installed),
            "readonly": bool(brain.READONLY)}


def export_extension(ext_id: str) -> dict:
    """把内置适配插件复制到 brain.HOME/extensions/installed/（「从商店下载安装」）。"""
    if not _NAME_RE.match(ext_id or ""):
        raise ValueError("非法拓展名")
    spec = next((s for s in _BUILTIN_EXTENSIONS if s["id"] == ext_id), None)
    if not spec:
        raise ValueError(f"商店里没有这个拓展：{ext_id}")
    if brain.READONLY:
        raise PermissionError("记忆库当前只读，无法安装拓展。")
    proj = brain.PROJECT_ROOT if hasattr(brain, "PROJECT_ROOT") else Path(__file__).resolve().parent.parent
    src = proj / spec["rel"]
    if not src.is_dir():
        raise ValueError(f"拓展源目录不存在：{spec['rel']}")
    dst = brain.HOME / "extensions" / "installed" / ext_id
    if dst.exists():
        raise FileExistsError(f"已经安装过：{ext_id}（先删除再重装）")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
    return {"ok": True, "id": ext_id, "host": spec["host"], "installed_dir": str(dst)}


def open_folder(which: str) -> str:
    """用资源管理器打开 panels / panel-market 目录，返回路径。

    os.startfile 仅 Windows；其他平台 subprocess 调 open/xdg-open，失败静默
    （路径照常返回，用户可以自己导航过去）。
    """
    if which == "market":
        d = market_dir()
    elif which == "extensions":
        d = brain.HOME / "extensions" / "installed"
    else:
        d = panels_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        if hasattr(os, "startfile"):
            os.startfile(str(d))  # noqa: S606  Windows 专属，路径来自本机数据目录
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(d)])
        else:
            subprocess.Popen(["xdg-open", str(d)])
    except Exception:
        pass  # 打不开不报错：路径已在响应里，用户可手动前往
    return str(d)


def scaffold(name: str, title: str) -> dict:
    """生成最小合法面板骨架：panel.json + index.html（含 api() helper 示例）。

    AI/用户一键起步：POST /api/panels/scaffold → 改文件 → 即刻可在管理界面启用。
    """
    if not _NAME_RE.match(name or ""):
        raise ValueError("面板名不合法（小写字母/数字/-/_/中文，1~32 字符）")
    d = _panel_path(panels_dir(), name)
    if d.exists():
        raise FileExistsError(f"面板已存在：{name}")
    d.mkdir(parents=True)
    (d / "panel.json").write_text(json.dumps({
        "name": name, "title": title or name, "version": "0.1.0",
        "author": "", "description": "脚手架生成的最小面板",
        "entry": "index.html", "lingshu_api": 1,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    # 标题进 HTML 文本位前做转义，防 XSS（模板结构占位符原样替换不受影响）
    safe_title = html.escape(title or name, quote=False)
    (d / "index.html").write_text(_SCAFFOLD_HTML.replace("__TITLE__", safe_title),
                                  encoding="utf-8")
    return {"ok": True, "name": name, "path": str(d)}


# ---------------------------------------------------------------- 静态文件

def read_panel_file(name: str, rel: str) -> tuple[bytes, str]:
    """读面板目录内静态文件 → (内容, Content-Type)。越界/白名单外抛异常。"""
    d = _panel_path(panels_dir(), name)
    p = _safe_rel(d, rel)
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在：{rel}")
    ext = p.suffix.lower()
    if ext not in FILE_WHITELIST:
        raise PermissionError(f"扩展名不在白名单：{ext}")
    ctype = {
        ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8", ".png": "image/png",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
        ".webp": "image/webp", ".svg": "image/svg+xml", ".ico": "image/x-icon",
        ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
        ".map": "application/json; charset=utf-8",
    }[ext]
    return p.read_bytes(), ctype


# 脚手架骨架 HTML：含 PANEL_TOKEN 占位符 + api() helper + /api/overview 调用示例，
# AI/用户起步即改即用；token 由服务端下发 HTML 时注入（替换占位符）
_SCAFFOLD_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  body{font-family:system-ui;background:#14181e;color:#e6e1d5;margin:0;padding:32px}
  .card{background:#1b2028;border:1px solid #2a3140;border-radius:12px;padding:20px;max-width:640px}
</style>
</head>
<body>
<h1>__TITLE__</h1>
<div class="card"><div id="out">加载中…</div></div>
<script>
const PANEL_TOKEN = "/*__PANEL_TOKEN__*/";
// 统一 API helper：Bearer token 已由服务端注入（见 docs/panel-api.md）
async function api(path, opts){
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers || {}, {Authorization: "Bearer " + PANEL_TOKEN});
  const r = await fetch(path, opts);
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}
api("/api/overview").then(d => {
  document.getElementById("out").textContent =
    "记忆总数：" + (d.stats && d.stats.total != null ? d.stats.total : "?") +
    "，海马体层：" + (d.layer_counts ? d.layer_counts.hippo : "?");
}).catch(e => document.getElementById("out").textContent = "加载失败：" + e.message);
</script>
</body>
</html>
"""
