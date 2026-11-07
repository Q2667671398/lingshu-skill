#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 本地可视化面板（仅监听 127.0.0.1）"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain  # noqa: E402
import growth  # noqa: E402
import panels  # noqa: E402  自定义面板管理（目录扫描/激活/zip 安装/市场）
import panel_cloud  # noqa: E402  云端面板市场（Gitee/GitHub 仓库托管）

ASSETS = Path(__file__).resolve().parent.parent / "assets"

# 只读门控名单：这些 POST 端点会写数据目录，数据目录不可写时统一 403
_WRITE_ENDPOINTS = {
    "/api/remember", "/api/persona", "/api/persona/state", "/api/persona/restore",
    "/api/persona/edit", "/api/persona/mind", "/api/persona/interview",
    "/api/persona/self", "/api/persona/rollback", "/api/persona/changes-viewed",
    "/api/memory", "/api/memory/review", "/api/knowledge", "/api/knowledge/review",
    "/api/note", "/api/skill",
    "/api/skill/self-review", "/api/evolve", "/api/dream", "/api/dream/auto",
    "/api/dream/apply", "/api/dream/draft", "/api/memory/bulk",
    "/api/emotion", "/api/mode", "/api/link",
    "/api/forget", "/api/daily", "/api/dream/instruction", "/api/logs",
    "/api/memory/dust",
    # 自定义面板管理：activate/delete/install 等都写数据目录
    "/api/panels", "/api/panels/scaffold",
    # 拓展商店：导出内置宿主适配插件（写 extensions/installed/）
    "/api/extensions",
    # 云端面板市场：发布/下载/配置（token 写 config.json）
    "/api/panels/cloud",
    # 数据目录迁移：写指针文件 + 复制数据（迁移目标在数据目录之外，单独放行只读门控）
    "/api/home/migrate",
}

# 本机随机 token：页面加载时注入，之后每个 API 请求都要带上。
# 注意：token 会随首页响应下发，本机进程 GET / 即可提取，所以它防的不是本机进程，
# 而是浏览器上下文里的误用（恶意网页跨站调用本机端口）；跨站防护主要靠 Host/Origin 校验。
TOKEN = os.environ.get("LINGSHU_PANEL_TOKEN") or secrets.token_urlsafe(24)
# 环境变量给了空串/空白时不能退化成空 token（否则任何请求都能过鉴权）——直接拒启
if not TOKEN.strip():
    print("[面板] LINGSHU_PANEL_TOKEN 为空，拒绝启动（空 token 等于不设防）", file=sys.stderr)
    raise SystemExit(2)
TOKEN = TOKEN.strip()


def _int_param(url, key: str, default: int, low: int, high: int) -> int:
    try:
        val = int((parse_qs(url.query).get(key) or [default])[0])
    except (TypeError, ValueError):
        val = default
    return max(low, min(high, val))


def memory_view(m: dict) -> dict:
    out = dict(m)
    out["retention"] = round(brain.retention(m), 3)
    out["display"] = brain.display_text(m)
    out["created_text"] = brain.fmt_time(m.get("created", 0))
    return out


def overview() -> dict:
    # 逐条 try/except 降级：单条坏记忆/坏文件不能让整个 overview 500（面板全页挂掉）
    def _safe(label: str, fn, default):
        try:
            return fn()
        except Exception as exc:
            brain.log_event("panel", f"overview {label} 降级：{exc}", level="warn")
            return default

    records = brain.load_all() + brain.load_archive()
    ordered = sorted(records, key=lambda m: _created_key(m), reverse=True)
    memories = []
    for m in ordered[:80]:
        try:
            memories.append(memory_view(m))
        except Exception as exc:
            brain.log_event("panel", f"overview 跳过坏记忆 {m.get('id')}: {exc}", level="warn")
    dreams = []
    ddir = brain.HOME / "dreams"
    if ddir.exists():
        for f in sorted(ddir.glob("*.md"), reverse=True)[:12]:
            try:
                dreams.append({"name": f.name, "mtime": brain.fmt_time(int(f.stat().st_mtime)),
                               "content": f.read_text(encoding="utf-8")[:3000]})
            except (OSError, ValueError):
                continue
    st = _safe("stats", brain.stats, {}) or {}
    return {
        "stats": st,
        # 层分布统计条用：各层记忆数（含归档），纯计数不做降级分支
        "layer_counts": {"hippo": st.get("hippo", 0), "cortex": st.get("cortex", 0),
                         "dust": st.get("dust", 0), "archive": st.get("archive", 0)},
        "persona": _safe("persona", lambda: _persona_view(), {}),
        "memories": memories,
        "dreams": dreams,
        "pending": _safe("pending", brain.load_dream_pending, []),
        "mind": _safe("mind", brain.mind_state, {}),
        "mood": _safe("mood", brain.mood_now, {}),
        "emotion_arc": _safe("emotion_arc", lambda: brain.emotion_arc(7), []),
        "desires": _safe("desires", brain.desire_state, {}),
        "emotion_groups": _safe("emotion_groups", lambda: brain.EMOTION_GROUPS, {}),
        "desire_defs": _safe("desire_defs", lambda: brain.DESIRES, {}),
        "rationality": _safe("rationality", brain.rationality, {}),
        "emotion_cfg": _safe("emotion_cfg", brain.emotion_cfg, {}),
        "daily_state": _safe("daily_state", brain._daily_state, {}),
        "last_self_review": load_config_last_review(),
        "onboarding": _safe("onboarding", brain.onboarding_state, {}),
        "readonly": bool(brain.READONLY),
        "model_tier": brain.model_tier(),
        "mode": _safe("mode", lambda: str(brain.load_config().get("mode", "auto") or "auto").lower(), "auto"),
        "review_counts": _safe("review_counts", brain.review_counts, {}),
        "persona_drift": _safe("persona_drift", lambda: brain.persona_drift(7), []),
        "emotion_alerts": _safe("emotion_alerts", lambda: brain.emotion_checks_recent(10), []),
        "dream_draft": _safe("dream_draft", brain.load_dream_draft, None),
        "mode_detail": _safe("mode_detail", lambda: brain.resolve_mode_detail(""), {}),
        "log_summary": _safe("log_summary", _log_summary, {"warn": 0, "error": 0}),
        "home": str(brain.HOME),
    }


def _persona_view() -> dict:
    """完整人格 + 回归后的 state（energy/closeness 读时向基线懒回归，不写盘）。"""
    p = brain.load_persona()
    st = p.get("state") or {}
    if st:
        p["state"] = brain._state_regress(st)
    return p


def _created_key(m: dict):
    """排序键：created 可能是坏类型（手改/LLM 产出），坏值沉底不抛异常。"""
    try:
        return float(m.get("created", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _imp_key(m: dict) -> float:
    """importance 安全转 float：坏值（None/非数字字符串/list 等）沉底为 0。"""
    try:
        return float(m.get("importance") or 0)
    except (TypeError, ValueError):
        return 0.0


def _to_num(v, default=None):
    """把请求里的值安全转 float：失败返回 default（默认 None，调用方据此走 400）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v, default=None):
    """把请求里的值安全转 int：失败返回 default（默认 None，调用方据此走 400）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _log_summary() -> dict:
    """最近 200 条里 error / warn（不含 error）各自多少条，一次读取避免重复解析。"""
    recent = brain.read_logs(200, level=None)
    errs = sum(1 for r in recent if r.get("level") == "error")
    warns = sum(1 for r in recent if r.get("level") == "warn")
    return {"warn": warns, "error": errs}


def load_config_last_review() -> float:
    try:
        return float(brain.load_config().get("last_self_review", 0) or 0)
    except Exception:
        return 0


def filtered_memories(url) -> list:
    """组合筛选：层 + 关键词 + 标签 + 情绪 + 重要性区间 + 时间范围 + 排序。

    面板的搜索框只做字面过滤；真正的模糊回忆走 /api/search（recall）。
    """
    qs = parse_qs(url.query)

    def one(key: str, default: str = "") -> str:
        return (qs.get(key) or [default])[0]

    layer = one("layer").strip()
    tag = one("tag").strip().lower()
    emotion = one("emotion").strip().lower()
    text = one("q").strip().lower()
    sort = one("sort", "created")
    # pending=1：只看待审核的记忆（概览页「待审核」区块跳转过来就是带这个参数）
    only_pending = one("pending", "") == "1"
    limit = _int_param(url, "limit", 200, 1, 500)

    def num(key: str, default: float) -> float:
        try:
            return float(one(key, str(default)) or default)
        except (TypeError, ValueError):
            return default

    imp_min, imp_max = num("imp_min", 0.0), num("imp_max", 1.0)
    # 兼容「1–10 分」输入：大于 1 视为十分制
    if imp_min > 1:
        imp_min /= 10
    if imp_max > 1:
        imp_max /= 10

    days = _int_param(url, "days", 0, 0, 36500)
    since = brain.now_ts() - days * 86400 if days else 0

    rows = []
    for m in brain.load_all() + brain.load_archive():
        if layer and m.get("layer") != layer:
            continue
        if only_pending and m.get("review") != "pending":
            continue
        if tag and tag not in [str(t).lower() for t in (m.get("tags") or [])]:
            continue
        if emotion and emotion not in str(m.get("emotion") or "").lower():
            continue
        if text:
            blob = " ".join([str(m.get("text", "")),
                             " ".join(str(t) for t in (m.get("tags") or [])),
                             " ".join(str(e) for e in (m.get("entities") or []))]).lower()
            if text not in blob:
                continue
        imp = _imp_key(m)
        if imp < imp_min or imp > imp_max:
            continue
        if since and _created_key(m) < since:
            continue
        rows.append(m)

    if sort == "importance":
        rows.sort(key=_imp_key, reverse=True)
    elif sort == "retention":
        rows.sort(key=lambda m: brain.retention(m), reverse=True)
    else:
        rows.sort(key=_created_key, reverse=True)
    return [memory_view(m) for m in rows[:limit]]


def workspace() -> dict:
    """工作台数据汇总。任何一块读取失败都降级成空值并记日志——
    否则一个坏掉的 knowledge/notes 文件会让所有写接口在写成功之后报 400，
    前端以为失败又重试，等于重复写入。"""
    out: dict = {}
    errors: list = []

    def safe(label, fn, fallback):
        try:
            out[label] = fn()
        except Exception as exc:   # noqa: BLE001 —— 单块失败不影响其它块
            out[label] = fallback
            errors.append({"label": label, "type": type(exc).__name__})
            brain.log_event("panel", f"workspace 读取 {label} 失败：{exc}", level="error")

    safe("status", growth.growth_status, {})
    safe("knowledge", lambda: growth.list_knowledge(limit=100), [])
    safe("knowledge_stats", growth.knowledge_stats, {})
    safe("tidy", growth.tidy_state, {})
    safe("notes", lambda: growth.list_notes(limit=100), [])
    safe("note_stats", growth.note_stats, {})
    safe("skills", growth.list_skills, [])
    safe("skill_roots", growth.skill_roots, [])
    safe("skill_scan", growth.scan_skills, [])
    safe("dsh_plugins", growth.list_dsh_plugins, [])
    safe("evolution", lambda: growth.load_evolution(limit=30), [])
    safe("onboarding", brain.onboarding_state, {})
    safe("pending_knowledge", growth.knowledge_pending, [])
    out["readonly"] = bool(brain.READONLY)
    out["model_tier"] = brain.model_tier()
    if errors:
        # 只回标签和异常类型；完整的异常全文已在 log_event 里，别把本机路径/堆栈塞进 HTTP 响应
        out["workspace_errors"] = errors
    return out


def _bad_list(data: dict, *keys) -> str | None:
    """这些字段必须是数组（前端传字符串会被当成字符列表逐字拆开）。"""
    for key in keys:
        val = data.get(key)
        if val is not None and not isinstance(val, list):
            return f"{key} 必须是数组"
    return None


# 这些写端点里有只读 action：只读模式下按 action 放行（skill 的 list/check/show、
# emotion 的 now/check），别把纯查询按钮也一刀切 403
_READONLY_ACTIONS = {
    "/api/skill": {"", "list", "check", "show", "roots", "scan", "use"},
    "/api/emotion": {"", "now", "check"},
}


def _readonly_allowed(path: str, data: dict) -> bool:
    if path == "/api/daily" and bool(data.get("dry_run")):
        return True
    if path == "/api/skill/self-review":
        return True
    allowed = _READONLY_ACTIONS.get(path)
    if allowed is None:
        return False
    return str(data.get("action") or "") in allowed


# --------------------------------------------------------------- 数据目录（查看 / 迁移）

_PTR_FILE = Path.home() / ".brain-memory" / ".home"


def _dir_size(p: Path) -> int:
    """目录体积（字节）：只统计普通文件，符号链接/不可读项跳过。"""
    total = 0
    try:
        for f in p.rglob("*"):
            try:
                if f.is_file() and not f.is_symlink():
                    total += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def _home_info() -> dict:
    ptr_active = False
    try:
        ptr_active = _PTR_FILE.is_file() and bool(_PTR_FILE.read_text(encoding="utf-8").strip())
    except OSError:
        pass
    env_set = bool(os.environ.get("BRAIN_MEMORY_HOME"))
    return {
        "home": str(brain.HOME),
        "default_home": str(Path.home() / ".brain-memory"),
        "via_env": env_set,          # 环境变量指定（优先级最高，面板改不了它）
        "via_pointer": ptr_active,   # 指针文件指定（面板可改）
        "size_bytes": _dir_size(brain.HOME),
        "readonly": bool(brain.READONLY),
    }


def _migrate_home(target_raw: str) -> dict:
    """把数据目录迁移到 target：整目录复制（旧目录保留不动）+ 写指针文件。

    进程内的 brain.HOME 不切换——本进程的各模块已各自复制了 HOME，运行中切换
    会造成新旧两处混写；正确姿势是迁完提示用户重启面板/会话。指针文件对
    下一次进程生效，环境变量 BRAIN_MEMORY_HOME 仍优先。
    """
    target = Path(target_raw).expanduser().resolve()
    if not str(target.name).strip() or target_raw.strip() in ("", "/", "\\"):
        raise ValueError("目标目录不能为空")
    if target == brain.HOME.resolve():
        raise ValueError("目标目录与当前数据目录相同")
    if target.exists() and any(target.iterdir()):
        raise ValueError("目标目录已存在且非空——请选一个空目录（或先手动清理）")
    # 计划写指针的位置必须可写：指针文件永远在默认位置 ~/.brain-memory/ 下
    _PTR_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 复制数据（目标已确保不存在或为空）
    shutil.copytree(brain.HOME, target, symlinks=True, dirs_exist_ok=True)
    _PTR_FILE.write_text(str(target) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "from": str(brain.HOME),
        "to": str(target),
        "pointer": str(_PTR_FILE),
        "note": "迁移完成。重启面板/新会话后生效；旧目录原样保留，确认无误后可自行删除。",
    }



class Handler(BaseHTTPRequestHandler):
    server_version = "LingShuPanel/1.0"

    def setup(self):
        super().setup()
        # 慢/坏客户端最多等 30 秒：没有这个，不读完请求体的连接能让线程永久挂住
        # （类属性 timeout 在 Python 3.13 的 BaseHTTPRequestHandler 上不生效，必须在 socket 上设置）
        self.connection.settimeout(30)

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        # 客户端刷新/关页面会把连接掐断（WinError 10053/10054），响应写不出去是正常现象，
        # 静默收尾即可——不让 ConnectionAbortedError 刷满控制台
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    # -------------------------------------------------- helpers
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        # 客户端刷新/关页面会立刻掐断连接，此时 send_response/write 都会抛
        # ConnectionAbortedError(WinError 10053)/BrokenPipeError。这是常态，不是故障：
        # 整个响应写出过程兜底吞掉并标记关闭连接，绝不打印 traceback（否则控制台被刷屏）。
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")  # 防止把响应当 HTML 解析
            self.send_header("Referrer-Policy", "no-referrer")
            # 页面有内联 <script>/<style>，明确放开；其余资源只能同源，禁止外链脚本/图片域名
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
                             "frame-ancestors 'none'")  # 防恶意网页 iframe 嵌入面板（点击劫持）
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except OSError as exc:
            # 其余 OSError（如 10054 被对端重置）同样是客户端侧问题，静默收尾
            self.close_connection = True
            brain.log_event("panel", f"响应写出失败（客户端已断开）：{exc}", level="info")

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _log_write_failure(self, label: str, exc: Exception, code: int) -> None:
        """写接口的文件系统异常（占用/权限/冲突）落到运行日志，面板「日志」页可溯源。

        str(exc) 含本机路径——日志里要全貌，HTTP 响应里只给通用文案。
        日志本身绝不当崩溃源：写失败降级为 stderr，不影响错误响应的返回。
        """
        try:
            brain.log_event("panel", f"POST {getattr(self, 'path', '?')} {label}",
                            level="warn", detail=str(exc)[:300], code=code)
        except Exception:
            print(f"[面板] {label}: {exc}", file=sys.stderr)

    def _body(self):
        """读并解析 JSON 请求体。

        返回 dict = 正常；返回 None = 已经回了错误响应（调用方直接 return），
        绝不再把"解析失败"当成空对象放过去——否则非法请求会被当成合法空操作。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:  # 负的 Content-Length 视为空请求体
            return {}
        if length > 2_000_000:
            self._json({"error": "请求体过大"}, 413)
            self.close_connection = True
            return None
        try:
            raw = self.rfile.read(length).decode("utf-8-sig")
        except Exception:
            self._json({"error": "无法读取请求体"}, 400)
            return None
        if not raw.strip():
            return {}
        try:
            obj = json.loads(raw)
        except Exception:
            self._json({"error": "请求体不是合法 JSON"}, 400)
            return None
        if not isinstance(obj, dict):
            self._json({"error": "请求体必须是 JSON 对象"}, 400)
            return None
        return obj

    def _origin_ok(self) -> bool:
        """只接受本机页面的请求，挡住跨站表单与 DNS rebinding。

        Host 必须是 127.0.0.1/localhost——否则恶意域名解析到 127.0.0.1 后，
        浏览器会把它当成同源，从而读走全部记忆。
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):       # IPv6：[::1]:8765
            host = host.split("]")[0] + "]"
        else:
            host = host.split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin in (f"http://{self.headers.get('Host', '')}",
                          f"http://127.0.0.1:{self.server.server_port}",
                          f"http://localhost:{self.server.server_port}")

    def _token_ok(self) -> bool:
        """所有 API 请求必须带正确 token（页面注入，非浏览器可借机偷读）。

        正式 token（TOKEN）只随内置首页/默认面板下发；第三方 iframe 面板用
        「当日滚动派生 token」：由主 TOKEN 对日期做 HMAC 派生，逐日失效，
        泄露面被限制在当天且不进浏览器历史的是完整长期凭据。
        """
        got = ""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
        if hmac.compare_digest(got.encode("utf-8"), TOKEN.encode("utf-8")):
            return True
        # 派生 iframe token：SHA256(TOKEN || YYYY-MM-DD) 前 32 位，逐日轮换
        day = time.strftime("%Y-%m-%d", time.localtime())
        digest = hashlib.sha256((TOKEN + "|" + day).encode("utf-8")).hexdigest()[:32]
        # encode 成 bytes 再比：非 ASCII header（恶意/异常客户端）直接传 str 会让
        # compare_digest 抛 TypeError，且 do_POST 的鉴权调用不在 try 里，会断连
        return hmac.compare_digest(got.encode("utf-8"), digest.encode("utf-8"))

    # -------------------------------------------------- routes
    def do_GET(self):
        # 与 do_POST 对齐的兜底：任意一条数据损坏都不能让整个接口断连无响应
        try:
            self._route_get()
        except FileExistsError as exc:
            print(f"[面板] 文件已存在: {exc}", file=sys.stderr)
            self._json({"error": "同名文件已存在"}, 409)
        except PermissionError as exc:
            print(f"[面板] 文件被占用: {exc}", file=sys.stderr)
            self._json({"error": "文件被占用或没有写入权限"}, 400)
        except (ValueError, KeyError) as exc:
            self._json({"error": str(exc)}, 400)
        except Exception:
            import traceback
            # 不再 print_exc 刷控制台：完整堆栈进运行日志（面板「日志」页可看）
            brain.log_event("panel", "GET 接口内部错误", level="error",
                            trace=traceback.format_exc()[-800:])
            self._json({"error": "面板内部错误，详情见运行日志"}, 500)

    def _route_get(self):
        # Host/Origin 校验必须前置：任何 GET 响应都可能含 TOKEN（首页/面板 HTML），
        # 校验放后面就等于先让恶意站点的 DNS rebinding 请求拿到 token 再 403
        if not self._origin_ok():
            self._json({"error": "forbidden"}, 403)
            return
        url = urlparse(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            # 始终下发内置默认面板作为「外壳」：外壳启动时拉 /api/panels，若激活了
            # 自定义面板就用 iframe 热加载它（as_page=1 注入 token）——这样
            # 「面板管理」按钮永远可用、iframe 崩了只崩自身、出错自动回退默认。
            html = ASSETS / "panel.html"
            if not html.exists():
                self._send(404, b"panel.html missing", "text/plain; charset=utf-8")
                return
            content = html.read_bytes().decode("utf-8")
            active = panels._active_name()
            # 注入激活面板名（default/None 都算默认）：外壳据此决定是否挂 iframe
            active_js = json.dumps({"panel": active or "default"}, ensure_ascii=False)
            content = content.replace("</head>",
                                      f'<script>window.__LINGSHU_ACTIVE__ = {active_js};</script>\n</head>', 1)
            # 注入随机 token：页面里每个 API 请求都会带上，杜绝无 Origin 直连偷读
            content = content.replace("/*__PANEL_TOKEN__*/", TOKEN)
            self._send(200, content.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            # 浏览器自动请求，返回透明 1x1 PNG，避免控制台刷 404
            self._send(200, bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                "0000000d4944415478da63fcffff3f030005fe02fea72d1e480000000049454e44ae426082"),
                "image/png")
            return
        # 读取接口同样要校验来源 + token：记忆是隐私数据，不能被任意页面/进程读走
        if path.startswith("/api/") and (not self._origin_ok() or not self._token_ok()):
            self._json({"error": "forbidden"}, 403)
            return
        if path == "/api/overview":
            self._json(overview())
            return
        if path == "/api/self-update":
            # auto=1：面板打开时的自动检查，只读缓存、绝不联网（毫秒返回，不会卡页面）
            # refresh=1：用户手动点「检查更新」，真拉远程源并刷新缓存
            qs = parse_qs(url.query)
            auto = (qs.get("auto") or ["0"])[0] in ("1", "true", "yes")
            refresh = (qs.get("refresh") or ["0"])[0] in ("1", "true", "yes")
            res = growth.update_check("lingshu", force_pull=refresh, auto=auto)
            # 透出源类型（dev/remote/installed/none）与源目标路径，供前端确认框展示。
            # 注意：这里不能再 force_pull=refresh——update_check 刚拉过一遍远程并写了
            # 缓存，再来一遍等于把整条 Gitee/GitHub 拉取链路跑两次，手动检查必然超时。
            if isinstance(res, dict) and res.get("cached") is not False:
                try:
                    src, kind = growth.resolve_source("lingshu", force_pull=False)
                    res["source_kind"] = kind
                    res["target_path"] = str(src)
                except Exception:
                    res.setdefault("source_kind", res.get("source", "none"))
            self._json(res)
            return
        if path == "/api/workspace":
            self._json(workspace())
            return
        if path == "/api/growth/review":
            days = _int_param(url, "days", 7, 1, 365)
            self._json(growth.growth_review(days))
            return
        if path == "/api/workspace/search":
            q = (parse_qs(url.query).get("q") or [""])[0]
            scope = (parse_qs(url.query).get("scope") or ["knowledge"])[0]
            if scope == "notes":
                rows = [{"note": r, "score": s} for r, s in growth.search_notes(q, 30)]
            else:
                rows = [{"knowledge": r, "score": s} for r, s in growth.search_knowledge(q, 30)]
            self._json(rows)
            return
        if path == "/api/search":
            q = (parse_qs(url.query).get("q") or [""])[0]
            k = _int_param(url, "k", 12, 1, 100)
            results = brain.recall(q, k=k, strengthen=False)
            self._json([{"memory": memory_view(m), "score": s, "parts": p} for m, s, p in results])
            return
        if path == "/api/memories":
            self._json(filtered_memories(url))
            return
        if path == "/api/inject-preview":
            q = (parse_qs(url.query).get("q") or [""])[0]
            try:
                packet = brain.build_injection(q)
            except Exception as exc:
                self._json({"error": f"生成注入包失败：{exc}"}, 500)
                return
            self._json({"packet": packet})
            return
        if path == "/api/conflicts":
            limit = _int_param(url, "limit", 50, 1, 200)
            self._json({"conflicts": brain.detect_conflicts(limit)})
            return
        if path == "/api/graph":
            limit = _int_param(url, "limit", 300, 10, 800)
            self._json(brain.assoc_graph(limit))
            return
        if path == "/api/diagnostics":
            # 自诊断：死链 / 冲突未裁决 / 数据膨胀 / 淡忘可逆状态 / 来源归属完整率
            self._json(brain.diagnose())
            return
        if path == "/api/home":
            # 数据目录信息：当前位置、是否指针生效、默认位置、当前库体积（面板设置卡片用）
            self._json(_home_info())
            return
        if path == "/api/assoc":
            mid = (parse_qs(url.query).get("id") or [""])[0]
            self._json({"id": mid, "neighbors": brain.assoc_of(mid)})
            return
        if path == "/api/mode/explain":
            q = (parse_qs(url.query).get("q") or [""])[0]
            self._json(brain.resolve_mode_detail(q))
            return
        if path == "/api/entity":
            # 实体档案卡：name 必须非空且别太长（塞进 URL/存储都安全），limit 用现有风格夹取
            name = (parse_qs(url.query).get("name") or [""])[0].strip()
            if not name or len(name) > 64:
                self._json({"error": "参数 name 必须为 1~64 字符"}, 400)
                return
            limit = _int_param(url, "limit", 20, 1, 100)
            self._json(brain.entity_profile(name, limit))
            return
        if path == "/api/persona/diff":
            self._json(brain.persona_diff())
            return
        if path == "/api/logs":
            limit = _int_param(url, "limit", 200, 1, 2000)
            level = (parse_qs(url.query).get("level") or [""])[0] or None
            if level == "all":
                level = None
            self._json({"logs": brain.read_logs(limit, level=level)})
            return
        if path == "/api/panels":
            self._json(panels.list_panels())
            return
        if path == "/api/extensions":
            # 拓展商店清单：内置宿主适配插件 + 自定义已装拓展
            self._json(panels.list_extensions())
            return
        if path == "/api/panels/market":
            # 本机市场目录扫描（无云端）：列出市场面板 + zip 安装包
            self._json(panels.list_market())
            return
        if path == "/api/panels/cloud":
            # 云端市场目录清单：优先 Gitee，失败试 GitHub，再失败降级提示（不挂页面）
            force = (parse_qs(url.query).get("force") or ["0"])[0] == "1"
            try:
                self._json(panel_cloud.cloud_list(force=force))
            except Exception as exc:  # noqa: BLE001
                self._json({"panels": [], "errors": [str(exc)], "offline": True,
                            "status": panel_cloud.status()}, 200)
            return
        if path == "/api/panels/cloud/shot":
            # 云端面板截图代理：前端 <img> 走本机端点，避免跨域；白名单+大小校验在模块里
            qs = parse_qs(url.query)
            sname = (qs.get("name") or [""])[0]
            sext = (qs.get("ext") or [".png"])[0].lower()
            try:
                body, _ = (lambda b: (b, ""))(panel_cloud.cloud_shot(sname, sext))
            except FileNotFoundError as exc:
                self._json({"error": str(exc)}, 404)
                return
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"截图拉取失败：{exc}"}, 502)
                return
            ctype = {".png": "image/png", ".jpg": "image/jpeg",
                     ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(sext, "image/png")
            self._send(200, body, ctype + "; cache-control=max-age=3600")
            return
        if path == "/api/panels/file":
            # 面板静态文件：面板名过正则、相对路径规范化限死在面板目录内、扩展名白名单。
            # as_page=1 时对 HTML 注入 token（替换 /*__PANEL_TOKEN__*/），iframe 里的
            # 自定义面板页面靠它自取 token 调统一 API——与首页下发逻辑同源同法。
            qs = parse_qs(url.query)
            pname = (qs.get("panel") or [""])[0]
            rel = (qs.get("path") or qs.get("p") or [""])[0]
            as_page = (qs.get("as_page") or ["0"])[0] in ("1", "true", "yes")
            try:
                body, ctype = panels.read_panel_file(pname, rel)
            except PermissionError as exc:
                self._json({"error": str(exc)}, 403)
                return
            except FileNotFoundError as exc:
                self._json({"error": str(exc)}, 404)
                return
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            if as_page and "html" in ctype:
                try:
                    text = body.decode("utf-8")
                    # 安全：只有内置默认面板才注入主 TOKEN。第三方（非内置）面板
                    # 一旦不可信，主 TOKEN 注入即可带出全部记忆——第三方面板想调
                    # API 可通过 Bearer header 携带「当日派生 token」（见 _token_ok，
                    # 逐日轮换、非 query 传参），服务端不主动下发长期主 TOKEN。
                    if pname == panels.DEFAULT_NAME:
                        text = text.replace("/*__PANEL_TOKEN__*/", TOKEN)
                        if "PANEL_TOKEN" not in text:
                            # 面板作者没写占位符时补进 <head>：保证 api() helper 能拿到 token
                            text = text.replace("<head>", '<head>\n<script>const PANEL_TOKEN="' + TOKEN + '";</script>', 1)
                    body = text.encode("utf-8")
                except UnicodeDecodeError:
                    pass  # 非 UTF-8 HTML：原样下发，面板作者自己修
            self._send(200, body, ctype)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._origin_ok() or not self._token_ok():
            # 拒绝前没读请求体：keep-alive 连接里残留的 body 会被下一轮当成请求行解析，
            # 造成连接错乱——断开连接最干净
            self.close_connection = True
            self._json({"error": "forbidden"}, 403)
            return
        path = urlparse(self.path).path
        data = self._body()
        if data is None:
            return
        # 只读门控：写接口在数据目录不可写时直接 403（展示/预览接口放行）
        if brain.READONLY and path in _WRITE_ENDPOINTS and not _readonly_allowed(path, data):
            self.close_connection = True
            self._json({"error": "readonly", "message": "数据目录当前只读，写操作已被禁用。"}, 403)
            return
        try:
            # 分发表：路由 -> 处理方法（分支体已拆到 _api_* 方法）
            handler = _POST_ROUTES.get(path)
            if handler is not None:
                handler(self, path, data)
            else:
                self._json({"error": "not found"}, 404)
        except FileExistsError as exc:
            # 已有同名技能/扩展/笔记：409 冲突比 500 内部错误更符合事实，前端也能直接提示
            # 系统异常的 str() 含本机路径，只回通用文案，细节进运行日志（面板「日志」页可溯源）
            self._log_write_failure("文件已存在", exc, 409)
            self._json({"error": "同名文件已存在"}, 409)
        except PermissionError as exc:
            # 文件被占用 / 无写权限：str() 也含路径，运行日志留全貌，响应只给通用文案
            # —— 不落日志的话，用户只看到一条 400，谁占了文件、占的是哪个文件完全无从排查
            self._log_write_failure("文件被占用或无写权限", exc, 400)
            # WinError 5 的 str() 通常带"拒绝访问"——那是权限问题，重试没用；
            # 其余多为短暂占用（读句柄/杀软扫描），提示稍后重试
            msg = str(exc)
            self._json({"error": "没有写入权限，请检查记忆库文件夹权限"
                        if ("拒绝访问" in msg or "Access is denied" in msg or "denied" in msg.lower())
                        else "文件被占用，请稍后重试"}, 400)
        except (ValueError, KeyError) as exc:
            # 业务异常 / 参数错：消息本身就是给用户看的，直接回显。
            # KeyError 的 str() 自带英文引号（"'没有这个技能'"），剥掉再给前端
            self._json({"error": str(exc).strip("'\"")}, 400)
        except Exception:  # 含 TypeError/AttributeError 等服务端编程错误：日志留全栈，响应只给通用文案，避免回显本机路径
            import traceback
            # 不 print_exc：堆栈进运行日志，控制台不再刷 Traceback
            brain.log_event("panel", "POST 接口内部错误", level="error",
                            trace=traceback.format_exc()[-800:])
            self._json({"error": "面板内部错误，详情见运行日志"}, 500)




# POST 分发表：路由 -> Handler 处理方法（参数统一为 handler(self, path, data)）。
# 定义在所有 _api_* 处理函数之后（模块级查表发生在运行时，不在导入期）。


def _api_home_migrate(self, path, data):
    # 数据目录迁移：整库复制到新位置 + 写指针文件（重启后生效）
    target = str(data.get("target") or "").strip()
    if not target:
        self._json({"error": "缺少 target（目标目录绝对路径）"}, 400)
        return
    if brain.READONLY:
        self._json({"error": "当前数据目录只读，无法迁移"}, 403)
        return
    try:
        self._json(_migrate_home(target))
    except ValueError as e:
        self._json({"error": str(e)}, 400)
    except OSError as e:
        self._json({"error": f"迁移失败：{e}"}, 500)



def _api_remember(self, path, data):
    text = (data.get("text") or "").strip()
    if not text:
        self._json({"error": "内容为空"}, 400)
        return
    tags = data.get("tags") or []
    entities = data.get("entities") or []
    if not isinstance(tags, list) or not isinstance(entities, list):
        self._json({"error": "tags / entities 必须是数组"}, 400)
        return
    rec, how = brain.remember(
        text,
        kind=data.get("kind", "fact"),
        importance=data.get("importance"),
        tags=tags,
        entities=entities,
        source="user",
        emotion=(data.get("emotion") or None),
        project=data.get("project"),
        session=data.get("session"),
    )
    self._json({"ok": True, "how": how, "memory": memory_view(rec)})



def _api_persona_update(self, path, data):
    updates = data.get("updates") or {}
    rejected = brain.update_persona(updates, why="面板编辑")
    self._json({"ok": True, "rejected": rejected, "persona": brain.load_persona()})



def _api_persona_changes_viewed(self, path, data):
    # 用户点了「AI 近期改过人格」通知去查看：标记已读，通知消失
    self._json(brain.mark_persona_changes_viewed())



def _api_persona_state(self, path, data):
    # 读-改-写整段加锁：否则和 CLI/做梦的并发人格写入互相覆盖
    with brain._locked(brain.HOME / "persona.json"):
        p = brain.load_persona()
        st = p.setdefault("state", {})
        # 先按时间结算衰减（精力回升/亲密回淡），再叠加面板新值，口径与读路径一致
        regressed = brain._state_regress(st)
        st.update(regressed)
        for key in ("mood", "mood_note"):
            if key in data:
                st[key] = data[key]
        for key in ("energy", "closeness"):
            if key in data:
                v = _to_int(data[key])
                if v is None:
                    self._json({"error": f"{key} 必须是数字"}, 400)
                    return
                st[key] = max(0, min(100, v))
        st["updated"] = brain.now_ts()
        p.setdefault("history", []).append({
            "ts": brain.now_ts(), "field": "state", "from": None,
            "to": {k: data[k] for k in data}, "why": "面板编辑",
        })
        brain.save_persona(p)
    self._json({"ok": True, "persona": p})



def _api_persona_edit(self, path, data):
    # 类型防御：dict 字段传字符串、list 字段传对象都会在 edit_persona 里 500，
    # 这里先校验给 400；字符串的 likes 等按逗号拆开（容错前端/脚本的手填输入）
    def _as_dict(key):
        v = data.get(key)
        if v in (None, "", {}, []):
            return None
        if isinstance(v, dict):
            return v or None
        raise ValueError(f"{key} 必须是对象（键值对）")

    def _as_list(key):
        v = data.get(key)
        if v in (None, "", [], {}):
            return None
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()] or None
        if isinstance(v, str):
            parts = [s.strip() for s in v.replace("，", ",").split(",")]
            return [s for s in parts if s] or None
        raise ValueError(f"{key} 必须是列表或逗号分隔的字符串")

    try:
        p = brain.edit_persona(
            identity=_as_dict("identity"),
            traits=_as_dict("traits"),
            drives=_as_dict("drives"),
            style=_as_list("style"),
            values=_as_list("values"),
            likes=_as_list("likes"),
            dislikes=_as_list("dislikes"),
            background=_as_list("background"),
            why=data.get("why") or "面板编辑",
        )
    except ValueError as exc:
        self._json({"error": str(exc)}, 400)
        return
    self._json({"ok": True, "persona": p, "onboarding": brain.onboarding_state()})



def _api_persona_mind(self, path, data):
    if data.get("mode") == "tune":
        cur, rsn = _to_int(data.get("curiosity")), _to_int(data.get("reason"))
        if cur is None or rsn is None:
            self._json({"error": "curiosity / reason 必须是数字"}, 400)
            return
        dr = brain.mind_tune(cur, rsn,
                             why=data.get("why") or "面板调节")
    else:
        dr = brain.mind_set(data.get("curiosity"), data.get("reason"), why="面板设定")
    self._json({"ok": True, "drives": dr, "mind": brain.mind_state(),
                "persona": brain.load_persona()})



def _api_mind_weigh(self, path, data):
    r = brain.mind_weigh(data.get("topic", ""), data.get("benefit", 5),
                         data.get("cost", 5), data.get("confidence", 0.6),
                         data.get("urgency", 0.5))
    self._json({"ok": True, "result": r})



def _api_emotion(self, path, data):
    act = data.get("action", "now")
    if act == "check":
        r = brain.emotion_consistency_check(data.get("text", ""))
        self._json({"ok": True, "result": r,
                    "alerts": brain.emotion_checks_recent(10)})
        return
    if act == "feel":
        intensity = _to_num(data.get("intensity"), 0.6)
        if intensity is None:
            self._json({"error": "intensity 必须是数字"}, 400)
            return
        brain.feel(data.get("emotion", ""), intensity,
                   why=data.get("why", ""), about=data.get("about", ""),
                   decay_hours=data.get("decay_hours"))
        self._json({"ok": True, "mood": brain.mood_now(),
                    "rationality": brain.rationality(),
                    "emotion_arc": brain.emotion_arc(7),
                    "desires": brain.desire_state()})
        return
    if act == "evaluate":
        r = brain.emotion_evaluate(data.get("event", ""), data.get("polarity", "auto"),
                                   data.get("importance", 0.6),
                                   why=data.get("why", ""), about=data.get("about", ""))
        self._json({"ok": True, "result": r, "mood": brain.mood_now(),
                    "rationality": brain.rationality(),
                    "emotion_arc": brain.emotion_arc(7),
                    "desires": brain.desire_state()})
        return
    if act == "mirror":
        r = brain.emotion_mirror(data.get("emotion", ""), data.get("intensity", 0.5))
        self._json({"ok": True, "result": r, "mood": brain.mood_now(),
                    "rationality": brain.rationality(),
                    "emotion_arc": brain.emotion_arc(7),
                    "desires": brain.desire_state()})
        return
    if act == "config":
        updates = {}
        if data.get("empathy"):
            updates["empathy"] = {"level": data["empathy"]}
        if data.get("gain") is not None:
            updates["empathy"] = dict(updates.get("empathy", {}),
                                      gain=max(0.0, min(1.0, float(data["gain"]))))
        if data.get("max_intensity") is not None:
            updates["empathy"] = dict(updates.get("empathy", {}),
                                      max_intensity=max(0.0, min(1.0, float(data["max_intensity"]))))
        if data.get("suppressed") is not None:
            updates["suppressed"] = data.get("suppressed") or []
        if data.get("notes") is not None:
            updates["notes"] = data.get("notes")
        cfg = brain.save_emotion_cfg(updates)
        self._json({"ok": True, "emotion_cfg": cfg, "mood": brain.mood_now(),
                    "rationality": brain.rationality()})
        return
    if act == "desire":
        amount = _to_num(data.get("amount"), 15)
        if amount is None:
            self._json({"error": "amount 必须是数字"}, 400)
            return
        brain.desire_satisfy(data.get("emotion", ""), amount)
        self._json({"ok": True, "mood": brain.mood_now(),
                    "rationality": brain.rationality(),
                    "emotion_arc": brain.emotion_arc(7),
                    "desires": brain.desire_state()})
        return
    # 走到这里只允许 now（或缺省不传）→ 返回当前心情快照；拼错/未知 action 明确报错，
    # 别"假成功"让人以为写进去了
    if act not in ("", "now"):
        self._json({"error": f"未知操作：{act}"}, 400)
        return
    self._json({"ok": True, "mood": brain.mood_now(),
                "rationality": brain.rationality(),
                "emotion_arc": brain.emotion_arc(7),
                "desires": brain.desire_state()})



def _api_link(self, path, data):
    act = data.get("action", "link")
    if act == "link":
        r = brain.link_memory(data.get("a", ""), data.get("b", ""),
                              data.get("relation", "相关"),
                              data.get("weight", 1.0), data.get("why", ""))
    elif act == "unlink":
        r = {"ok": brain.unlink_memory(data.get("a", ""), data.get("b", ""))}
    elif act == "auto":
        r = brain.assoc_auto(dry_run=bool(data.get("dry_run")))
    else:
        self._json({"error": "未知操作"}, 400)
        return
    self._json({"ok": True, "result": r, "graph": brain.assoc_graph()})



def _api_daily(self, path, data):
    dry = bool(data.get("dry_run"))
    # 真跑会整理/归档记忆：必须显式 confirm，避免空 body 的误触/重放直接写盘
    if not dry and data.get("confirm") is not True:
        self._json({"error": "真跑每日整理需要显式传 confirm: true（或用 dry_run 先预演）"}, 400)
        return
    r = brain.daily(dry_run=dry, force=bool(data.get("force")))
    self._json({"ok": True, "result": r, "overview": overview() if not dry else None,
                "workspace": workspace() if not dry else None})



def _api_skill_self_review(self, path, data):
    r = growth.self_review(force=bool(data.get("force")),
                           days=_to_int(data.get("days", 3) or 3, 3) or 3)
    self._json({"ok": True, "result": r})



def _api_seed(self, path, data):
    self._json({"error": "人格不再由预写种子生成，请用 /api/persona/edit"}, 410)



def _api_dream(self, path, data):
    # 做梦会重放/巩固/遗忘记忆：必须显式 confirm，空 body 不触发真实写盘
    if data.get("confirm") is not True:
        self._json({"error": "开始做梦需要显式传 confirm: true"}, 400)
        return
    report = brain.dream(trigger="panel")
    self._json({"ok": True, "report": report, "overview": overview()})



def _api_dream_auto(self, path, data):
    # 一键自动做梦：机器重放 → 启发式压缩 → 直接沉淀（不需要再人工审核
    # pending）。产出照旧标 review="pending"，仍可在记忆页复核。
    if data.get("confirm") is not True:
        self._json({"error": "自动做梦需要显式传 confirm: true"}, 400)
        return
    if brain.READONLY:
        self._json({"error": "记忆库当前只读，无法自动做梦沉淀"}, 403)
        return
    r = brain.auto_dream(trigger="panel")
    self._json({"ok": True, "result": r, "overview": overview()})



def _api_dream_instruction(self, path, data):
    # 生成给 AI 的深度做梦指令：面板拿到现成文本，一键复制发给 AI，
    # AI 读 pending.json → 压缩成 apply.json → dream --apply。
    # AI 产出的记忆仍标待审核，人工在记忆页过目——这道闸不绕开。
    # 「现在做梦」按钮已改为 auto_dream（直接沉淀、不留 pending），
    # 所以这里没有 pending 时就先跑一次深度做梦把碎片攒成任务。
    if brain.READONLY:
        self._json({"error": "记忆库当前只读，无法生成做梦任务"}, 403)
        return
    pend = brain.load_dream_pending()
    if not pend:
        brain.dream(trigger="panel")
        pend = brain.load_dream_pending()
    if not pend:
        self._json({"error": "梦很安静：没有可整理的记忆碎片"}, 400)
        return
    c = len(pend.get("clusters") or [])
    instr = (
        f"请帮我完成一次深度做梦整理（任务 {pend.get('task_id')}）：\n"
        f"1. 运行 `python scripts/brain.py dream` 查看 {c} 组碎片（或直接读 "
        f"{(brain.HOME / 'dreams' / 'pending.json')}）；\n"
        "2. 把每组碎片压缩成更抽象、更持久的长期记忆（不要逐条复述原文），"
        "按 pending.json 里的 apply_schema 填写 memories / persona_updates / forget；\n"
        "3. 把结果写成 apply.json 并执行 `python scripts/brain.py dream --apply apply.json`。\n"
        "注意：不要编造记忆里没有的细节；人格 traits 改动只给小幅增量；"
        "产出会进入待审核，用户会在面板记忆页复核。"
    )
    self._json({"ok": True, "instruction": instr, "task_id": pend.get("task_id"),
                "clusters": c})



def _api_dream_apply(self, path, data):
    result = data.get("result") or {}
    if isinstance(result, str):
        result = json.loads(result)
    applied = brain.apply_dream(result)
    self._json({"ok": True, "applied": applied, "overview": overview()})



def _api_dream_draft(self, path, data):
    draft = data.get("draft")
    if draft is None:
        brain.clear_dream_draft()  # 传 null = 清空草稿
        self._json({"ok": True, "draft": None})
        return
    if isinstance(draft, str):
        try:
            draft = json.loads(draft)  # 允许前端直接传 JSON 文本
        except json.JSONDecodeError as exc:
            self._json({"error": f"草稿 JSON 解析失败：{exc}"}, 400)
            return
    if not isinstance(draft, dict):
        self._json({"error": "草稿必须是 JSON 对象"}, 400)
        return
    brain.save_dream_draft(draft)
    self._json({"ok": True, "draft": brain.load_dream_draft()})



def _api_memory_edit(self, path, data):
    mid = data.get("id")
    # 读-改-写整体加锁：save_all 是整库快照覆写，不加锁会覆盖掉并发写入
    with brain._locked(brain.HOME / "hippocampus.jsonl"):
        records = brain.load_all()
        target = next((m for m in records if m["id"] == mid), None)
        if not target:
            self._json({"error": "没有这条记忆"}, 404)
            return
        for key in ("text", "gist", "pinned"):
            if key in data:
                target[key] = data[key]
        if "kind" in data:
            kind = str(data["kind"] or "").strip()
            if kind not in brain.KINDS:  # 白名单：防恶意/手滑写入怪异类型
                self._json({"error": f"kind 只能是：{'、'.join(brain.KINDS)}"}, 400)
                return
            target["kind"] = kind
        if "importance" in data:
            imp = _to_num(data["importance"])
            if imp is None:
                self._json({"error": "importance 必须是数字"}, 400)
                return
            target["importance"] = round(max(0.0, min(1.0, imp / 10 if imp > 1 else imp)), 3)
        if "tags" in data:
            tags = data["tags"]
            if not isinstance(tags, list):
                self._json({"error": "tags 必须是数组"}, 400)
                return
            target["tags"] = [str(t).strip() for t in tags if str(t).strip()]
        brain.save_all(records)
    self._json({"ok": True, "memory": memory_view(target)})



def _api_forget(self, path, data):
    ok = brain.forget(data.get("id", ""))
    self._json({"ok": ok})



def _api_memory_dust(self, path, data):
    # 淡忘可逆通道：fold 折叠（原文收起）/ unfold 解压还原 / revive 彻底复活
    act = data.get("action", "fold")
    mid = data.get("id", "")
    if act == "fold":
        m = brain.fold(mid)
    elif act == "unfold":
        m = brain.unfold(mid)
    elif act == "revive":
        layer = data.get("layer") or "cortex"
        m = brain.revive(mid, layer=layer)
    else:
        self._json({"error": "action 只能是 fold / unfold / revive"}, 400)
        return
    self._json({"ok": True, "action": act, "memory": memory_view(m)})



def _api_knowledge(self, path, data):
    act = data.get("action", "add")
    if act in ("add", "update"):
        bad = _bad_list(data, "points", "tags", "entities")
        if bad:
            self._json({"error": bad}, 400)
            return
    if act == "add":
        # 面板是本机人工操作 → source=user 直接 confirmed；
        # 前端若标明 AI 产出（source=ai）则落为待审核，走 review 流程。
        rec, how = growth.add_knowledge(
            data.get("title", ""), data.get("body", ""), data.get("topic", ""),
            data.get("points"), data.get("tags"), data.get("entities"),
            source=data.get("source", "user"))
        self._json({"ok": True, "how": how, "knowledge": rec, "workspace": workspace()})
    elif act == "update":
        rec = growth.update_knowledge(
            data.get("id", ""), title=data.get("title"), topic=data.get("topic"),
            body=data.get("body"), points=data.get("points"),
            tags=data.get("tags"), entities=data.get("entities"),
            pinned=data.get("pinned"))
        self._json({"ok": True, "knowledge": rec, "workspace": workspace()})
    elif act == "delete":
        self._json({"ok": growth.delete_knowledge(data.get("id", "")), "workspace": workspace()})
    elif act == "review":
        rec = growth.set_knowledge_status(data.get("id", ""), "confirmed")
        self._json({"ok": bool(rec), "knowledge": rec, "workspace": workspace()})
    elif act == "discard":
        rec = growth.set_knowledge_status(data.get("id", ""), "discarded")
        self._json({"ok": bool(rec), "knowledge": rec, "workspace": workspace()})
    elif act == "verify":
        # 记一次验证：证据等级自动+1（E0→E4 封顶），验证过的结论检索时优先
        try:
            rec = growth.knowledge_verify(data.get("id", ""),
                                          note=str(data.get("note") or ""),
                                          level=data.get("level"))
        except (KeyError, ValueError) as exc:
            self._json({"error": str(exc)}, 400)
            return
        self._json({"ok": True, "knowledge": rec, "workspace": workspace()})
    elif act == "tidy":
        self._json({"ok": True, "report": growth.tidy_knowledge(), "workspace": workspace()})
    elif act == "tidy/apply":
        result = data.get("result") or {}
        if isinstance(result, str):
            result = json.loads(result)
        self._json({"ok": True, "applied": growth.apply_tidy(result), "workspace": workspace()})
    else:
        self._json({"error": "未知操作"}, 400)



def _api_note(self, path, data):
    act = data.get("action", "write")
    if act in ("write", "update"):
        bad = _bad_list(data, "tags", "memory_ids")
        if bad:
            self._json({"error": bad}, 400)
            return
    if act == "write":
        rec, how = growth.write_note(
            data.get("type", "journal"), data.get("title", ""), data.get("body", ""),
            data.get("mood", ""), data.get("tags"), data.get("memory_ids"),
            append_same_day=bool(data.get("append", True)))
        self._json({"ok": True, "how": how, "note": rec, "workspace": workspace()})
    elif act == "update":
        rec = growth.update_note(data.get("id", ""), title=data.get("title"),
                                 body=data.get("body"), mood=data.get("mood"),
                                 tags=data.get("tags"))
        self._json({"ok": True, "note": rec, "workspace": workspace()})
    elif act == "delete":
        self._json({"ok": growth.delete_note(data.get("id", "")), "workspace": workspace()})
    else:
        self._json({"error": "未知操作"}, 400)



def _api_skill(self, path, data):
    act = data.get("action", "list")
    if act == "new":
        res = growth.create_skill(data.get("name", ""), data.get("description", ""),
                                  data.get("body", ""), bool(data.get("force")))
        self._json({"ok": True, "skill": res, "workspace": workspace()})
    elif act == "edit":
        res = growth.edit_skill(data.get("name", ""), data.get("body"),
                                data.get("append"), data.get("description"))
        self._json({"ok": True, "skill": res, "workspace": workspace()})
    elif act == "install":
        # 面板安装忽略 from_dir：本地 CLI 才能指外部目录，防远程乱指
        res = growth.install_skill(data.get("name", ""),
                                   bool(data.get("force")),
                                   None,
                                   accept_risk=bool(data.get("yes")))
        self._json({"ok": True, "result": res, "workspace": workspace()})
        return
    elif act == "uninstall":
        # 会删全局技能目录里的东西，记一笔日志，事后能查是谁在面板上删的；
        # managed_only 只允许卸载灵枢装过的技能，防止误删用户其他全局插件
        res = growth.uninstall_skill(data.get("name", ""), managed_only=True)
        brain.log_event("panel", f"卸载技能 {data.get('name', '')} → {res.get('state')}")
        self._json({"ok": True, "result": res,
                    "workspace": workspace()})
    elif act == "delete":
        ok = growth.remove_skill(data.get("name", ""))
        brain.log_event("panel", f"删除自建技能 {data.get('name', '')} → {ok}")
        self._json({"ok": ok, "workspace": workspace()})
    elif act == "check":
        self._json({"ok": True, "checks": growth.check_skill(data.get("name"))})
    elif act == "show":
        self._json({"ok": True, "skill": growth.read_skill(data.get("name", ""))})
    elif act == "list":
        self._json({"ok": True, "skills": growth.list_skills(), "workspace": workspace()})
    elif act == "roots":
        self._json({"ok": True, "roots": growth.skill_roots(),
                    "workspace": workspace()})
    elif act == "root_add":
        res = growth.add_skill_root(data.get("path", ""))
        self._json({"ok": True, "result": res, "workspace": workspace()})
    elif act == "root_rm":
        res = growth.remove_skill_root(data.get("path", ""))
        self._json({"ok": True, "result": res, "workspace": workspace()})
    elif act == "scan":
        rows = growth.scan_skills(data.get("root") or None)
        self._json({"ok": True, "skills": rows, "workspace": workspace()})
    elif act == "use":
        # 只读取正文给前端预览（不自动全局化），便于面板里看外部技能内容
        res = growth.use_skill(data.get("name", ""),
                               int(data.get("chars") or 6000),
                               root=data.get("root") or None)
        self._json({"ok": True, "skill": res})
    elif act == "borrow":
        res = growth.borrow_skill(data.get("name", ""),
                                  data.get("as_name", ""),
                                  data.get("note", ""),
                                  bool(data.get("force")),
                                  root=data.get("root") or None)
        self._json({"ok": True, "skill": res, "workspace": workspace()})
    else:
        self._json({"error": "未知操作"}, 400)



def _api_evolve(self, path, data):
    row = growth.log_evolution(data.get("kind", "self"), data.get("title", ""),
                               data.get("detail", ""), data.get("refs") or [])
    self._json({"ok": True, "evolution": row, "workspace": workspace()})



def _api_persona_interview(self, path, data):
    p = brain.persona_interview(data.get("name", ""), data.get("traits", ""),
                                data.get("style", ""), data.get("values", ""),
                                data.get("callme", ""), data.get("self_name", ""),
                                bool(data.get("unsure")))
    self._json({"ok": True, "persona": p, "onboarding": brain.onboarding_state()})



def _api_persona_self(self, path, data):
    p = brain.persona_self_author(data.get("name", ""), data.get("traits", ""),
                                  data.get("likes", ""), data.get("dislikes", ""),
                                  data.get("habits", ""), data.get("insights", ""),
                                  data.get("style", ""), data.get("values", ""),
                                  data.get("callme", ""), data.get("self_name", ""))
    self._json({"ok": True, "persona": p, "onboarding": brain.onboarding_state()})



def _api_persona_rollback(self, path, data):
    idx = _to_int(data.get("index"))
    if idx is None:
        self._json({"error": "index 必须是数字"}, 400)
        return
    try:
        p = brain.persona_rollback(idx)
    except (ValueError, IndexError) as exc:
        self._json({"error": str(exc)}, 400)
        return
    self._json({"ok": True, "persona": p})



def _api_memory_review(self, path, data):
    mid, act = data.get("id", ""), data.get("action", "")
    if act not in ("confirm", "discard"):
        self._json({"error": "action 只能是 confirm / discard"}, 400)
        return
    try:
        r = brain.set_memory_review(mid, act)
    except (ValueError, PermissionError) as exc:
        self._json({"error": str(exc)}, 400)
        return
    self._json({"ok": True, "memory": r, "review_counts": brain.review_counts()})



def _api_memory_bulk(self, path, data):
    try:
        r = brain.bulk_memory_action(data.get("ids") or [],
                                     data.get("action", ""), data.get("value"))
    except (ValueError, PermissionError) as exc:
        self._json({"error": str(exc)}, 400)
        return
    self._json({"ok": True, "result": r, "review_counts": r.get("review_counts")})



def _api_mode(self, path, data):
    mode = str(data.get("mode", "") or "").lower()
    if mode not in ("work", "human", "auto"):
        self._json({"error": "mode 只能是 work / human / auto"}, 400)
        return
    brain.config_set("mode", mode)
    self._json({"ok": True, "mode": mode})



def _api_logs_post(self, path, data):
    act = str(data.get("action") or "clear")
    if act == "clear":
        n = brain.clear_logs()
        self._json({"ok": True, "cleared": n,
                    "logs": brain.read_logs(200)})
        return
    if act == "delete":
        n = brain.clear_logs(ts=data.get("ts"), kind=data.get("kind"),
                             msg=data.get("msg"))
        self._json({"ok": True, "deleted": n,
                    "logs": brain.read_logs(200)})
        return
    self._json({"error": "action 只能是 clear / delete"}, 400)



def _api_panels(self, path, data):
    # 自定义面板管理：全部转发给 panels.py，统一中文错误消息回给前端
    act = str(data.get("action") or "")
    try:
        if act == "activate":
            pname = str(data.get("name") or "")
            panels.activate(pname)
            self._json({"ok": True, "active": panels._active_name() or "default"})
            return
        if act == "deactivate":
            panels.deactivate()
            self._json({"ok": True, "active": "default"})
            return
        if act == "delete":
            # source: installed=已安装目录 / market=市场目录
            panels.delete(str(data.get("name") or ""),
                          str(data.get("source") or "installed"))
            self._json({"ok": True})
            return
        if act == "open-folder":
            p = panels.open_folder(str(data.get("which") or "installed"))
            self._json({"ok": True, "path": p})
            return
        if act == "upload":
            # 上传 = 把已安装面板剪切移动到市场目录；同名冲突 → 409
            r = panels.upload(str(data.get("name") or ""))
            self._json(r)
            return
        if act == "install":
            # 从市场安装（市场源复制保留）；zip 形式现场解压校验
            r = panels.install(str(data.get("name") or ""))
            self._json(r)
            return
        self._json({"error": "action 只能是 activate / deactivate / delete / "
                             "open-folder / upload / install"}, 400)
    except FileExistsError as exc:
        self._json({"error": f"{exc}（请改个名字后重试）"}, 409)
    except ValueError as exc:
        self._json({"error": str(exc)}, 400)
    except PermissionError as exc:
        self._json({"error": str(exc)}, 403)
    except Exception as exc:
        self._log_write_failure(f"panels {act}", exc, 500)
        self._json({"error": "面板操作失败，详情见运行日志"}, 500)



def _api_extensions(self, path, data):
    # 拓展商店操作：install=导出内置适配插件到 extensions/installed/
    act = str(data.get("action") or "")
    try:
        if act == "install":
            r = panels.export_extension(str(data.get("id") or ""))
            brain.log_event("panel", f"导出拓展 {r.get('id')} → {r.get('installed_dir')}")
            self._json(r)
            return
        if act == "open-folder":
            p = panels.open_folder("extensions")
            self._json({"ok": True, "path": p})
            return
        if act == "cloud-download":
            # 从云端商店拉 extensions/<id>.zip 单独下载安装（不拉整个仓库）
            r = panel_cloud.cloud_extension_download(str(data.get("id") or ""))
            brain.log_event("panel", f"云端下载拓展 {r.get('id')}")
            self._json(r)
            return
        self._json({"error": "action 只能是 install / cloud-download / open-folder"}, 400)
    except FileExistsError as exc:
        self._json({"error": str(exc)}, 409)
    except ValueError as exc:
        self._json({"error": str(exc)}, 400)
    except PermissionError as exc:
        self._json({"error": str(exc)}, 403)
    except Exception as exc:
        self._log_write_failure(f"extensions {act}", exc, 500)
        self._json({"error": "拓展操作失败，详情见运行日志"}, 500)



def _api_panels_cloud(self, path, data):
    # 云端市场操作：publish=发布到云端（可带截图 base64）、
    # download=从云端装到本机、save-config=配置 Gitee/GitHub 仓库与 token
    act = str(data.get("action") or "")
    try:
        if act == "publish":
            pname = str(data.get("name") or "")
            shot_b64 = str(data.get("screenshot") or "")
            shot = None
            ext = ".png"
            if shot_b64:
                if "," in shot_b64:  # data:image/png;base64,xxx 前缀剥掉
                    head, shot_b64 = shot_b64.split(",", 1)
                    for e in (".png", ".jpg", ".jpeg", ".webp"):
                        if e in head:
                            ext = e
                            break
                import base64 as _b64
                shot = _b64.b64decode(shot_b64)
            r = panel_cloud.cloud_publish(pname, shot, ext)
            self._json(r)
            return
        if act == "download":
            r = panel_cloud.cloud_download(str(data.get("name") or ""))
            self._json(r)
            return
        if act == "clear-config":
            r = panel_cloud.set_config(gitee_repo="", gitee_token="",
                                       github_repo="", github_token="")
            self._json(r)
            return
        if act == "save-config":
            r = panel_cloud.set_config(
                gitee_repo=str(data.get("gitee_repo") or ""),
                gitee_token=str(data.get("gitee_token") or ""),
                github_repo=str(data.get("github_repo") or ""),
                github_token=str(data.get("github_token") or ""),
                branch=str(data.get("branch") or ""))
            self._json(r)
            return
        self._json({"error": "action 只能是 publish / download / save-config / clear-config"}, 400)
    except ValueError as exc:
        self._json({"error": str(exc)}, 400)
    except PermissionError as exc:
        self._json({"error": str(exc)}, 403)
    except Exception as exc:  # noqa: BLE001
        self._log_write_failure(f"panels cloud {act}", exc, 502)
        self._json({"error": str(exc) or "云端操作失败，详情见运行日志"}, 502)



def _api_panels_scaffold(self, path, data):
    # 生成最小面板骨架：方便 AI/用户起步（AI 创建自定义面板的落地入口）
    pname = str(data.get("name") or "").strip()
    ptitle = str(data.get("title") or "").strip()
    try:
        self._json(panels.scaffold(pname, ptitle))
    except FileExistsError as exc:
        self._json({"error": str(exc)}, 409)
    except ValueError as exc:
        self._json({"error": str(exc)}, 400)
    return


# POST 分发表：路由 -> Handler 处理方法（参数统一为 handler(self, path, data)）
# 按路由家族分组：首页/记忆写入 → 人格/心智/情绪 → 关联/整理/模式 → 做梦
# → 记忆管理/知识/笔记 → 技能/种子 → 日志 → 面板与拓展商店
_POST_ROUTES = {
        # 首页与记忆写入
        "/api/home/migrate": _api_home_migrate,
        "/api/remember": _api_remember,
        # 人格 / 心智 / 情绪
        "/api/persona": _api_persona_update,
        "/api/persona/changes-viewed": _api_persona_changes_viewed,
        "/api/persona/state": _api_persona_state,
        "/api/persona/edit": _api_persona_edit,
        "/api/persona/mind": _api_persona_mind,
        "/api/persona/interview": _api_persona_interview,
        "/api/persona/self": _api_persona_self,
        "/api/persona/rollback": _api_persona_rollback,
        "/api/mind/weigh": _api_mind_weigh,
        "/api/emotion": _api_emotion,
        # 关联 / 整理 / 模式
        "/api/link": _api_link,
        "/api/daily": _api_daily,
        "/api/evolve": _api_evolve,
        "/api/mode": _api_mode,
        # 做梦
        "/api/dream": _api_dream,
        "/api/dream/auto": _api_dream_auto,
        "/api/dream/instruction": _api_dream_instruction,
        "/api/dream/apply": _api_dream_apply,
        "/api/dream/draft": _api_dream_draft,
        # 记忆管理 / 知识 / 笔记
        "/api/memory": _api_memory_edit,
        "/api/forget": _api_forget,
        "/api/memory/dust": _api_memory_dust,
        "/api/memory/review": _api_memory_review,
        "/api/memory/bulk": _api_memory_bulk,
        "/api/knowledge": _api_knowledge,
        "/api/note": _api_note,
        # 技能 / 种子
        "/api/skill": _api_skill,
        "/api/skill/self-review": _api_skill_self_review,
        "/api/seed": _api_seed,
        # 日志
        "/api/logs": _api_logs_post,
        # 面板与拓展商店
        "/api/panels": _api_panels,
        "/api/panels/cloud": _api_panels_cloud,
        "/api/panels/scaffold": _api_panels_scaffold,
        "/api/extensions": _api_extensions,
}


def _startup_update_check() -> None:
    """服务启动时后台检测一次更新并写入缓存（失败静默，不影响面板启动）。

    浏览器刷新面板只读这份缓存，绝不联网——只有重启面板服务才会真正
    拉远程源检测最新版本。
    """
    def _job():
        import traceback
        try:
            # auto=False 才能真正走 _update_check_live 联网检测并写缓存；
            # auto=True 只在读缓存模式，会被 early-return 拦截
            growth.update_check("lingshu", force_pull=True, auto=False)
        except Exception:
            try:
                brain.log_event("panel", "启动时更新检测失败", level="warn",
                                trace=traceback.format_exc()[-500:])
            except Exception:
                pass
    threading.Thread(target=_job, name="startup-update-check", daemon=True).start()


def serve(port: int = 8765, open_browser: bool = True) -> None:
    brain.ensure_home()
    brain._resolve_readonly(False)   # 面板也要探测只读：数据目录不可写时 POST 写接口返回 403
    _startup_update_check()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        print(f"[错误] 端口 {port} 被占用或不可用：{exc}", file=sys.stderr)
        print("换一个端口试试：python scripts/panel_server.py --port 8766", file=sys.stderr)
        raise SystemExit(1) from exc
    httpd.daemon_threads = True   # 退出时不等挂住的请求线程，防止 Ctrl+C 后进程残留
    url = f"http://127.0.0.1:{port}"
    print(f"灵枢 · 记忆面板：{url}")
    print(f"记忆库：{brain.HOME}")
    print("Ctrl+C 停止")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--home", help="测试用：指定数据目录（透传给 brain，不影响正式数据）")
    a = ap.parse_args()
    if a.home:
        brain._override_home(["--home", a.home])
    serve(a.port, not a.no_browser)
