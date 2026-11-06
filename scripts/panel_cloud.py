#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 云端面板市场：Gitee（主）/ GitHub（镜像）仓库当云端，纯标准库 urllib。

云端约定（仓库布局）：
- panels/<面板名>.zip        面板安装包（≤50MB，安装时解压校验 manifest）
- shots/<面板名>.<ext>       截图（png/jpg/webp，≤2MB），可选但强烈建议
- index.json                 目录清单：{"panels":[{name,title,version,author,
                             description,screenshot,size,updated}]}

接口要点（调研结论，2026-11）：
- Gitee v5：GET/POST/PUT https://gitee.com/api/v5/repos/{owner}/{repo}/contents/{path}
  新建 POST、更新 PUT（需带原文件 sha），content 一律 base64；access_token 放 body/查询。
- GitHub：PUT https://api.github.com/repos/{owner}/{repo}/contents/{path}
  新建不带 sha、更新带 sha；下载用 Accept: application/vnd.github.raw（>1MB 时
  contents 接口的 content 字段为空，必须走 raw 头）。
- token 只存本机 config.json，API 响应一律脱敏；公开仓库浏览不需要 token。

离线/未配置/网络失败一律降级：列表返回空并带 error 提示，不影响本机市场。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import brain


def _resolve_token(value: str) -> str:
    """token 支持 "env:变量名" 引用：不落盘明文，运行时从环境变量读取。

    直接填的明文 token 仍兼容（不推荐；备份时会脱敏剥离，见 cli._sanitize_config_json）。
    """
    v = str(value or "").strip()
    if v.lower().startswith("env:"):
        return os.environ.get(v[4:].strip(), "").strip()
    return v

# 截图白名单扩展名与大小上限（≤2MB），zip 上限沿用 panels.ZIP_MAX_TOTAL
SHOT_WHITELIST = {".png", ".jpg", ".jpeg", ".webp"}
SHOT_MAX = 2 * 1024 * 1024
_CLOUD_TIMEOUT = 20  # 秒；Gitee 稳定，GitHub 国内不稳，超时即降级

# 官方商店源兜底：用户没配置 panel.cloud 时，默认浏览灵枢官方镜像仓库的
# index.json（面板/拓展都发布在这里）。用户配置了自己的仓库后以配置优先。
DEFAULT_GITEE_REPO = "htmcwy_0/lingshu-skill"
DEFAULT_GITHUB_REPO = "Q2667671398/lingshu-skill"
DEFAULT_BRANCH = "main"


# ---------------------------------------------------------------- 配置

def _cfg(resolve_tokens: bool = True) -> dict:
    """读云端配置：panel.cloud.{gitee_repo,gitee_token,github_repo,github_token,branch}
    未配置时回退官方商店源（gitee_repo / branch；GitHub 同理）。
    resolve_tokens=False 时保留 "env:VAR" 原样（set_config 回写用，防明文落盘）。"""
    try:
        c = brain.load_config().get("panel.cloud") or {}
    except Exception:
        c = {}
    c = dict(c) if isinstance(c, dict) else {}
    if not c.get("gitee_repo") and not c.get("github_repo"):
        c.setdefault("gitee_repo", DEFAULT_GITEE_REPO)
        c.setdefault("github_repo", DEFAULT_GITHUB_REPO)
        c.setdefault("branch", DEFAULT_BRANCH)
    # token 环境变量引用在读取侧解析，落盘的可以是 "env:VAR" 而非明文
    if resolve_tokens:
        for _k in ("gitee_token", "github_token"):
            if c.get(_k):
                c[_k] = _resolve_token(c[_k])
    return c


def set_config(gitee_repo: str = "", gitee_token: str = "",
               github_repo: str = "", github_token: str = "",
               branch: str = "") -> dict:
    """保存云端配置（空串=清除该项）。token 属敏感信息：写 config.json 前校验格式；
    填 "env:变量名" 则不落盘明文，运行时从环境变量读取。"""
    c = _cfg(resolve_tokens=False)
    if gitee_repo:
        if gitee_repo.count("/") != 1:
            raise ValueError("Gitee 仓库要填 owner/repo 形式，例如 yourname/lingshu-panels")
        c["gitee_repo"] = gitee_repo.strip()
    else:
        c.pop("gitee_repo", None)
    if gitee_token:
        c["gitee_token"] = gitee_token.strip()
    else:
        c.pop("gitee_token", None)
    if github_repo:
        if github_repo.count("/") != 1:
            raise ValueError("GitHub 仓库要填 owner/repo 形式")
        c["github_repo"] = github_repo.strip()
    else:
        c.pop("github_repo", None)
    if github_token:
        c["github_token"] = github_token.strip()
    else:
        c.pop("github_token", None)
    if branch:
        c["branch"] = branch.strip()
    with brain._locked(brain.HOME / "config.json"):
        full = brain.load_config()
        full["panel.cloud"] = c
        brain.atomic_json(brain.HOME / "config.json", full)
    return status()


def status() -> dict:
    """云端配置状态（token 脱敏：只露前 4 位，让用户能认出是哪个 token）。
    browse 用默认官方源兜底（未配置也能逛商店）；publish 必须显式配置 token。"""
    c = _cfg()
    user_cloud = brain.load_config().get("panel.cloud") or {}
    user_repo = bool(user_cloud.get("gitee_repo"))

    def mask(t: str) -> str:
        t = str(t or "")
        if t.lower().startswith("env:"):
            return t          # 环境变量引用不含密文，原样展示便于辨认
        return (t[:4] + "****") if t else ""

    return {
        "gitee_repo": c.get("gitee_repo", ""),
        "gitee_token_mask": mask(user_cloud.get("gitee_token", "")),
        "github_repo": c.get("github_repo", ""),
        "github_token_mask": mask(user_cloud.get("github_token", "")),
        "branch": c.get("branch", "master"),
        "gitee_ready": user_repo,                # 用户显式配置了自己的 Gitee 仓库
        "browse_ready": bool(c.get("gitee_repo") or c.get("github_repo")),  # 能逛商店（含官方兜底源）
        "using_default_source": not user_repo,   # 浏览的是官方兜底源还是自己的配置
        "publish_ready": bool(user_cloud.get("gitee_repo") and user_cloud.get("gitee_token")),
    }


# ---------------------------------------------------------------- HTTP 底座

def _http(method: str, url: str, *, token: str = "", headers: dict | None = None,
          body: bytes | None = None) -> bytes:
    """一次 HTTP 请求 → 响应字节。非 2xx 抛 RuntimeError（带状态码与响应摘要）。"""
    h = {"User-Agent": "lingshu-panel-market/1.0"}
    if token:
        h["Authorization"] = f"token {token}"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_CLOUD_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read()[:200].decode("utf-8", "replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"网络请求失败：{exc}") from exc


def _gitee_get(repo: str, path: str, token: str, branch: str) -> bytes | None:
    """Gitee 取单文件原始字节（contents 接口 → base64 解码）。不存在 → None。"""
    from urllib.parse import quote
    url = (f"https://gitee.com/api/v5/repos/{repo}/contents/{quote(path)}"
           f"?ref={quote(branch)}")
    if token:
        url += f"&access_token={quote(token)}"
    try:
        raw = _http("GET", url)
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return None
        # API 匿名限流（403）等场景：公开仓库退回 raw 页面直连（不走 contents API 配额）
        try:
            return _http("GET", f"https://gitee.com/{repo}/raw/{branch}/{quote(path)}",
                         token=token)
        except RuntimeError:
            raise exc
    data = json.loads(raw.decode("utf-8"))
    if data.get("encoding") == "base64" and data.get("content"):
        return base64.b64decode(data["content"])
    # Gitee 大文件 encoding 可能是 none：退回 raw 页面下载
    return _http("GET", f"https://gitee.com/{repo}/raw/{branch}/{quote(path)}",
                 token=token)


def _commit_author(c: dict) -> dict | None:
    """从配置取自定义 commit 作者（author_name/author_email）。

    未配置时返回 None——远端 API 默认用 token 所属账号作为提交者。
    """
    name = str(c.get("author_name", "")).strip()
    email = str(c.get("author_email", "")).strip()
    if not name:
        return None
    return {"name": name, "email": email or "lingshu@local"}


def _gitee_put(repo: str, path: str, token: str, branch: str,
               content: bytes, message: str,
               author: dict | None = None) -> None:
    """Gitee 新建/更新单文件：先探 sha，有则 PUT 更新，无则 POST 新建。"""
    from urllib.parse import quote
    b64 = base64.b64encode(content).decode("ascii")
    payload = {"access_token": token, "content": b64, "branch": branch,
               "message": message}
    if author:
        payload["author_name"] = author["name"]
        payload["author_email"] = author["email"]
        payload["committer_name"] = author["name"]
        payload["committer_email"] = author["email"]
    old = _gitee_get(repo, path, token, branch)
    if old is not None:
        # 拿 sha 需要再查一次（_gitee_get 返回的是内容本身），单独请求元数据
        url = (f"https://gitee.com/api/v5/repos/{repo}/contents/{quote(path)}"
               f"?ref={quote(branch)}")
        if token:
            url += f"&access_token={quote(token)}"
        sha = json.loads(_http("GET", url).decode("utf-8")).get("sha")
        payload["sha"] = sha
        _http("PUT", f"https://gitee.com/api/v5/repos/{repo}/contents/{quote(path)}",
              headers={"Content-Type": "application/json"},
              body=json.dumps(payload).encode("utf-8"))
    else:
        _http("POST", f"https://gitee.com/api/v5/repos/{repo}/contents/{quote(path)}",
              headers={"Content-Type": "application/json"},
              body=json.dumps(payload).encode("utf-8"))


def _github_put(repo: str, path: str, token: str, branch: str,
                content: bytes, message: str,
                author: dict | None = None) -> None:
    """GitHub 新建/更新单文件（contents API：不带 sha=新建，带 sha=更新）。"""
    from urllib.parse import quote
    b64 = base64.b64encode(content).decode("ascii")
    payload = {"content": b64, "message": message, "branch": branch}
    if author:
        payload["author"] = {"name": author["name"], "email": author["email"]}
        payload["committer"] = {"name": author["name"], "email": author["email"]}
    url = f"https://api.github.com/repos/{repo}/contents/{quote(path)}"
    # 先探测是否已存在（拿 sha）；404/网络抖动都按"新建"处理
    try:
        meta = json.loads(_http("GET", url + f"?ref={quote(branch)}",
                                token=token).decode("utf-8"))
        payload["sha"] = meta.get("sha")
    except RuntimeError:
        pass
    _http("PUT", url, headers={"Content-Type": "application/json"},
          body=json.dumps(payload).encode("utf-8"))


def _github_get(repo: str, path: str, token: str, branch: str) -> bytes | None:
    """GitHub 取单文件原始字节（Accept raw，>1MB 文件也直接返回二进制）。"""
    from urllib.parse import quote
    url = f"https://api.github.com/repos/{repo}/contents/{quote(path)}?ref={quote(branch)}"
    try:
        return _http("GET", url, token=token,
                     headers={"Accept": "application/vnd.github.raw"})
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise


# ---------------------------------------------------------------- 读取（浏览/下载）

def _repo_list(repo: str, branch: str, getter) -> dict:
    """拉 index.json 并解析；文件不存在 → 空市场，网络错 → 抛错由上层降级。

    index.json 结构：{"panels": [...], "extensions": [...]}（extensions 可缺省）。
    """
    raw = getter(repo, "index.json", branch)
    if raw is None:
        return {"panels": [], "extensions": []}
    data = json.loads(raw.decode("utf-8"))
    if isinstance(data, dict):
        return {
            "panels": data.get("panels") if isinstance(data.get("panels"), list) else [],
            "extensions": data.get("extensions") if isinstance(data.get("extensions"), list) else [],
        }
    return {"panels": [], "extensions": []}


def cloud_list(force: bool = False) -> dict:
    """GET /api/panels/cloud 的返回体：云端目录清单 + 配置状态。

    优先 Gitee；失败再试 GitHub；都失败 → 返回 error 与提示，前端仍可用本机市场。
    60 秒内存缓存（force=True 强制刷新），避免每次打开标签页都打 API。
    """
    global _CACHE
    c = _cfg()
    st = status()
    if not force and _CACHE and time.time() - _CACHE[0] < 60:
        return {**_CACHE[1], "status": st, "cached": True}
    gitee_repo = c.get("gitee_repo", "")
    gh_repo = c.get("github_repo", "")
    branch = c.get("branch", "master")
    gt, gh = c.get("gitee_token", ""), c.get("github_token", "")
    errors: list[str] = []
    items: dict = {}
    if gitee_repo:
        try:
            items = _repo_list(gitee_repo, branch,
                               lambda r, p, b: _gitee_get(r, p, gt, b))
        except Exception as exc:  # noqa: BLE001 —— 网络失败降级，不让市场页挂掉
            errors.append(f"Gitee：{exc}")
    if not items.get("panels") and gh_repo:
        try:
            items = _repo_list(gh_repo, branch,
                               lambda r, p, b: _github_get(r, p, gh, b))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"GitHub：{exc}")
    errors = [e for e in errors if e]
    panels = items.get("panels", [])
    exts = items.get("extensions", [])
    out = {"panels": panels, "extensions": exts, "status": st, "errors": errors,
           "offline": bool(errors) and not panels and not exts}
    if panels or exts:
        _CACHE = (time.time(), out)
    return out


_CACHE: tuple[float, dict] | None = (0.0, {})


def cloud_download(name: str) -> dict:
    """从云端下载面板 zip → 存进本机市场目录（当离线缓存）→ 复用 install 校验落位。"""
    # 落盘前统一过 _safe_name（install 内有校验，但 zip 落盘在 install 之前）
    if not _safe_name(name):
        raise ValueError(f"非法面板名：{name!r}")
    raw = _fetch(f"panels/{name}.zip", name)
    _verify_sha256(name, raw)  # 清单带 sha256 字段时校验，不匹配拒绝安装
    zpath = panels.market_dir() / f"{name}.zip"
    brain.atomic_bytes(zpath, raw)
    return panels.install(name)


def _verify_sha256(name: str, raw: bytes) -> None:
    """下载的 zip 必须与云端 index.json 清单里的 sha256 比对，否则拒绝安装。

    fail-closed：清单拉取失败、清单无此面板、条目缺 sha256，一律拒绝。
    自建源也要提供带 sha256 的 index.json（见 docs/panel-api.md）。
    """
    try:
        items = cloud_list(force=False).get("panels", [])
    except Exception as exc:  # noqa: BLE001 —— 统一转成拒绝安装的 ValueError
        raise ValueError(f"面板 {name} 的云端校验清单获取失败（{exc}），已拒绝安装") from exc
    entry = next((p for p in items if p.get("name") == name), None)
    if not entry:
        raise ValueError(f"面板 {name} 不在云端清单中，已拒绝安装")
    if not entry.get("sha256"):
        raise ValueError(f"面板 {name} 的清单条目缺少 sha256，已拒绝安装")
    if hashlib.sha256(raw).hexdigest().lower() != str(entry["sha256"]).lower():
        raise ValueError(f"面板 {name} 下载校验失败（sha256 不匹配），已拒绝安装")


def cloud_extension_download(ext_id: str) -> dict:
    """从云端下载拓展 zip（extensions/<id>.zip）→ 解压到拓展已安装目录。

    与面板下载分开：面板走 panels.install（有 manifest 校验），
    拓展是宿主适配插件目录，解压即用，只做路径安全校验。
    """
    # 落盘前统一过 _safe_name：拒绝 `..`、路径分隔符、Windows 盘符冒号（防路径穿越）
    if not _safe_name(ext_id):
        raise ValueError(f"非法拓展名：{ext_id!r}")
    raw = _fetch(f"extensions/{ext_id}.zip", ext_id)
    import io
    import zipfile

    import panels  # noqa: PLC0415 —— 校验常量在 panels 里，函数内局部引入
    root = brain.HOME / "extensions" / "installed"
    root.mkdir(parents=True, exist_ok=True)
    dst = root / ext_id
    if dst.exists():
        raise FileExistsError("该拓展已安装，请先删除 extensions/installed 下同名目录")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
        if len(names) > panels.ZIP_MAX_FILES:
            raise ValueError("拓展包文件数超限")
        total = sum(i.file_size for i in z.infolist())
        if total > panels.ZIP_MAX_TOTAL:
            raise ValueError("拓展包超过 50MB")
        dst_real = dst.resolve()
        for n in names:  # 防 zip 路径逃逸（../ 绝对路径 + 解析后必须仍在目标目录内）
            if n.startswith("/") or ".." in Path(n).parts:
                raise ValueError("拓展包含非法路径")
            if not (dst_real / n).resolve().is_relative_to(dst_real):
                raise ValueError(f"拓展包路径逃逸：{n!r}")
        z.extractall(dst)
    brain.log_event("panel", f"云端下载拓展 {ext_id} → {dst}")
    return {"ok": True, "id": ext_id, "dir": str(dst)}


def _fetch(path: str, label: str) -> bytes:
    """按路径从云端拉文件字节：先 Gitee 后 GitHub，都失败抛 ValueError。"""
    c = _cfg()
    branch = c.get("branch", "master")
    raw = None
    errs = []
    if c.get("gitee_repo"):
        try:
            raw = _gitee_get(c["gitee_repo"], path, c.get("gitee_token", ""), branch)
        except Exception as exc:  # noqa: BLE001
            errs.append(f"Gitee：{exc}")
    if raw is None and c.get("github_repo"):
        try:
            raw = _github_get(c["github_repo"], path, c.get("github_token", ""), branch)
        except Exception as exc:  # noqa: BLE001
            errs.append(f"GitHub：{exc}")
    if raw is None:
        raise ValueError(f"云端没有该文件或下载失败（{label}）：{'；'.join(errs) or path}")
    return raw


def _safe_name(name: str) -> bool:
    """拓展 id/面板名安全校验：复用 panels._NAME_RE（防路径分隔符与越界）。

    _NAME_RE 白名单本身不含冒号/斜杠，天然拒绝 `..`、路径分隔符与
    Windows 盘符冒号（`C:`）；这里显式再挡一层，与 panels._extract_zip_safe
    的三重防护口径保持一致。
    """
    if not name or name.startswith("/") or "\\" in name or ".." in name \
            or (len(name) > 1 and name[1] == ":"):
        return False
    return bool(panels._NAME_RE.match(name))


def cloud_shot(name: str, ext: str) -> bytes:
    """取云端截图字节（前端 <img> 直连本机代理端点，避免跨域/混合内容问题）。"""
    if ext not in SHOT_WHITELIST:
        raise ValueError("截图扩展名不支持")
    c = _cfg()
    branch = c.get("branch", "master")
    path = f"shots/{name}{ext}"
    raw = None
    if c.get("gitee_repo"):
        try:
            raw = _gitee_get(c["gitee_repo"], path, c.get("gitee_token", ""), branch)
        except Exception:  # noqa: BLE001
            raw = None
    if raw is None and c.get("github_repo"):
        try:
            raw = _github_get(c["github_repo"], path, c.get("github_token", ""), branch)
        except Exception:  # noqa: BLE001
            raw = None
    if raw is None:
        raise FileNotFoundError("云端没有该截图")
    return raw


# ---------------------------------------------------------------- 发布

def cloud_publish(name: str, screenshot: bytes | None = None,
                  shot_ext: str = ".png") -> dict:
    """把已安装面板发布到云端（Gitee 必配；GitHub 配了就同步镜像）。

    流程：校验 manifest → 打 zip → 传 panels/<name>.zip →（可选）传截图 →
    读 index.json 合并本面板条目后回写。任一远端失败则该远端报错，另一端照常。
    """
    import io
    import zipfile

    import panels
    src = panels._panel_path(panels.panels_dir(), name)
    if not src.exists():
        raise ValueError(f"已安装面板不存在：{name}")
    mf = panels._load_manifest(src)  # 损坏面板不允许发布
    if screenshot is not None:
        if len(screenshot) > SHOT_MAX:
            raise ValueError("截图超过 2MB，请压缩后再上传")
        if shot_ext not in SHOT_WHITELIST:
            raise ValueError("截图只支持 png/jpg/webp")
    # 打 zip：内存打包（总量受 ZIP_MAX_TOTAL 保护，超限抛 ValueError）
    buf = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                rel = f.relative_to(src).as_posix()
                if rel in ("panel.json",) or not rel.startswith("."):
                    total += f.stat().st_size
                    if total > panels.ZIP_MAX_TOTAL:
                        raise ValueError("面板超过 50MB，不适合云端分发")
                    z.write(f, rel)
    zbytes = buf.getvalue()
    now = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    entry = {"name": mf["name"], "title": mf["title"], "version": mf["version"],
             "author": mf.get("author", ""), "description": mf.get("description", ""),
             "screenshot": (f"{name}{shot_ext}" if screenshot is not None else ""),
             "size": len(zbytes), "updated": now}
    c = _cfg()
    branch = c.get("branch", "master")
    author = _commit_author(c)
    msg = f"lingshu: publish panel {name} v{mf['version']}"
    pushed, errors = [], []
    if c.get("gitee_repo") and c.get("gitee_token"):
        try:
            _push_one("gitee", c["gitee_repo"], c["gitee_token"], branch,
                      name, zbytes, screenshot, shot_ext, entry, msg,
                      kind_key="panels", author=author)
            pushed.append("Gitee")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Gitee：{exc}")
    else:
        errors.append("Gitee 未配置仓库或 token（发布需要 Gitee）")
    if c.get("github_repo") and c.get("github_token"):
        try:
            _push_one("github", c["github_repo"], c["github_token"], branch,
                      name, zbytes, screenshot, shot_ext, entry, msg,
                      kind_key="panels", author=author)
            pushed.append("GitHub")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"GitHub：{exc}")
    global _CACHE
    _CACHE = None  # 发布后清缓存，下次列表强制刷新
    if not pushed:
        raise RuntimeError(f"发布失败：{'；'.join(errors)}")
    return {"ok": True, "pushed": pushed,
            "errors": errors, "entry": entry}


def cloud_extension_publish(ext_id: str) -> dict:
    """把拓展已安装目录发布到云端 extensions/<id>.zip + index.json 的 extensions 数组。

    结构与面板发布平行：同一个 index.json，两个子清单互不干扰。
    """
    import io
    import zipfile

    src = brain.HOME / "extensions" / "installed" / ext_id
    if not src.is_dir() or not _safe_name(ext_id):
        raise ValueError(f"已安装拓展不存在或 id 不合法：{ext_id}")
    meta = panels._extension_meta(src) if hasattr(panels, "_extension_meta") else {}
    buf = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                total += f.stat().st_size
                if total > panels.ZIP_MAX_TOTAL:
                    raise ValueError("拓展超过 50MB，不适合云端分发")
                z.write(f, f.relative_to(src).as_posix())
    zbytes = buf.getvalue()
    now = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    entry = {"id": ext_id, "title": ext_id,
             "version": meta.get("version", "0.0.0"),
             "author": meta.get("author", ""),
             "size": len(zbytes), "updated": now}
    c = _cfg()
    branch = c.get("branch", "master")
    author = _commit_author(c)
    msg = f"lingshu: publish extension {ext_id} v{entry['version']}"
    pushed, errors = [], []
    for kind in ("gitee", "github"):
        repo = c.get(f"{kind}_repo", "")
        token = c.get(f"{kind}_token", "")
        if repo and token:
            try:
                _push_one(kind, repo, token, branch, ext_id, zbytes,
                          None, ".png", entry, msg, kind_key="extensions",
                          author=author)
                pushed.append("Gitee" if kind == "gitee" else "GitHub")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{'Gitee' if kind == 'gitee' else 'GitHub'}：{exc}")
    global _CACHE
    _CACHE = None
    if not pushed:
        raise RuntimeError(f"发布失败：{'；'.join(errors)}")
    return {"ok": True, "pushed": pushed, "errors": errors, "entry": entry}


def _push_one(kind: str, repo: str, token: str, branch: str, name: str,
              zbytes: bytes, shot: bytes | None, shot_ext: str,
              entry: dict, msg: str, kind_key: str = "panels",
              author: dict | None = None) -> None:
    """向一个远端推 zip + 截图 + index.json 条目。

    kind_key 决定子目录与清单键：panels → panels/xxx.zip + index.json.panels，
    extensions → extensions/xxx.zip + index.json.extensions（同一份 index.json）。
    """
    put = _gitee_put if kind == "gitee" else _github_put
    get = _gitee_get if kind == "gitee" else _github_get
    put(repo, f"{kind_key}/{name}.zip", token, branch, zbytes, msg, author)
    if shot is not None:
        put(repo, f"shots/{name}{shot_ext}", token, branch, shot, msg, author)
    # index.json 合并回写：读旧清单 → 覆盖同名条目 → 写回（并发竞争可接受，个人市场）
    raw = get(repo, "index.json", token, branch)
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {"panels": [], "extensions": []}
    except Exception:  # noqa: BLE001 —— 云端清单坏了就重建
        data = {"panels": [], "extensions": []}
    if not isinstance(data, dict):
        data = {"panels": [], "extensions": []}
    # key 名：面板条目按 name 匹配，拓展条目按 id 匹配
    id_key = "id" if kind_key == "extensions" else "name"
    items = [p for p in (data.get(kind_key) or []) if p.get(id_key) != name]
    items.append(entry)
    items.sort(key=lambda p: str(p.get("updated", "")), reverse=True)
    data[kind_key] = items
    data.setdefault("panels", data.get("panels", []))
    put(repo, "index.json", token, branch,
        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        msg, author)
