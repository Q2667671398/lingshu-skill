#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""附录 B · P1：DSH 会话日志导入（多平台入口 + 工作区隔离）。

DSH 把一场会话写成：
    <sessions>/<工作区编码目录>/<会话目录>/session.v4.jsonl.zstd

其中有三个必须处理的现实约束：
  1. 文件是**多帧拼接**的 zstd 容器：每行/每批事件一个独立帧。Node 的
     `zlib.zstdDecompressSync` 一次只解第一帧，直接解整个文件只会拿到
     313 字节的 header（本机实测）。所以 Node 侧先做「帧结构扫描」再逐帧解压
     （与 DSH 自己 `scanZstdFrames` 的结构扫描逻辑等价），把明文流式写进临时文件。
  2. 本机系统 Python **没有 zstandard 模块**，因此不把 Python 当解码器。
  3. 会话事件是 `{"type": "...", "time": 毫秒, "data": {...}}`：
     只导入 `user/message` / `assistant/message` 里的 text 片段（role/content/ts），
     `reasoning`、`tool-call`、`tool/result`、`system/message`、`command/*` 等
     非对话条目一律跳过；`user/message` 里 `source.kind` 不是 `user` 的注入块
     （runtime-context / system-reminder）也算系统事件，跳过。

解码优先级（都不静默降级）：
  Node 22+（先探测 `node -e "require('node:zlib').zstdDecompressSync"`）
  → 系统 `zstd -d` 命令
  → 抛 DecoderUnavailable（「需要 Node 22+ 或 zstd 命令」）。

导入：`session_rag.ingest_messages(rows, session=会话id, platform="dsh",
workspace=工作区)`，按内容哈希幂等；解压后的临时文件逐行流式解析，每 500 条
批量入库，内存不随文件大小增长（8MB 压缩 / 1 万条事件级别实测可用）。

CLI:
    python scripts/session_import.py [--sessions-dir PATH] [--limit N]
                                     [--workspace 名称] [--dry-run] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # 允许 `python scripts/session_import.py` 与 tests 直接 import
    sys.path.insert(0, str(HERE))

import session_rag  # noqa: E402

PLATFORM = "dsh"
BATCH_SIZE = 500
DEFAULT_TIMEOUT = 600.0  # 单文件解码的最长等待（秒）

# 只认 DSH 会话日志：session.v4.jsonl.zstd / session.jsonl / 未来版本号
_SESSION_LOG_RE = re.compile(r"^session(?:\.v\d+)?\.jsonl(\.zstd)?$", re.IGNORECASE)

_NODE_PROBE = (
    "process.exit(typeof require('node:zlib').zstdDecompressSync === 'function' ? 0 : 1)"
)
_NODE_VERSION = "process.stdout.write(process.version)"

# 多帧 zstd 解码器（Node 22+）。先做与 DSH 相同的帧结构扫描，再逐帧解压并
# 立刻写盘 —— 不把整份明文同时留在内存里。
_NODE_DECODE_JS = r"""
const fs = require('node:fs')
const zlib = require('node:zlib')
const src = process.argv[2]
const dst = process.argv[3]
const MAGIC = 0xFD2FB528
const buf = fs.readFileSync(src)

// 返回完整帧的结束偏移；遇到文件尾部的不完整帧返回 -1（live 会话正在追加）。
function frameEnd(buffer, start) {
  if (buffer.length - start < 4) return -1
  if (buffer.readUInt32LE(start) !== MAGIC) {
    throw new Error('corrupt Zstandard session log: invalid frame magic at byte ' + start)
  }
  let off = start + 4
  if (off === buffer.length) return -1
  const descriptor = buffer.readUInt8(off)
  off += 1
  if ((descriptor & 0x18) !== 0) {
    throw new Error('corrupt Zstandard session log: reserved frame-header bit at byte ' + (off - 1))
  }
  const contentSizeFlag = descriptor >>> 6
  const singleSegment = (descriptor & 0x20) !== 0
  const checksum = (descriptor & 0x04) !== 0
  const dictionaryFlag = descriptor & 0x03
  const dictionaryBytes = dictionaryFlag === 3 ? 4 : dictionaryFlag
  const contentSizeBytes = contentSizeFlag === 0
    ? (singleSegment ? 1 : 0)
    : (1 << contentSizeFlag)
  const remainingHeaderBytes = (singleSegment ? 0 : 1) + dictionaryBytes + contentSizeBytes
  if (buffer.length - off < remainingHeaderBytes) return -1
  off += remainingHeaderBytes
  for (;;) {
    if (buffer.length - off < 3) return -1
    const blockHeader = buffer.readUIntLE(off, 3)
    off += 3
    const lastBlock = (blockHeader & 1) !== 0
    const blockType = (blockHeader >>> 1) & 0x03
    const blockSize = blockHeader >>> 3
    if (blockType === 0x03) {
      throw new Error('corrupt Zstandard session log: reserved block type at byte ' + (off - 3))
    }
    const payloadBytes = blockType === 0x01 ? 1 : blockSize
    if (buffer.length - off < payloadBytes) return -1
    off += payloadBytes
    if (lastBlock) break
  }
  if (checksum) {
    if (buffer.length - off < 4) return -1
    off += 4
  }
  return off
}

const fd = fs.openSync(dst, 'w')
let frames = 0
let plainBytes = 0
let tornBytes = 0
let offset = 0
try {
  while (offset < buf.length) {
    const end = frameEnd(buf, offset)
    if (end < 0) {
      tornBytes = buf.length - offset
      break
    }
    const plain = zlib.zstdDecompressSync(buf.subarray(offset, end))
    fs.writeSync(fd, plain)
    plainBytes += plain.length
    frames += 1
    offset = end
  }
} finally {
  fs.closeSync(fd)
}
process.stdout.write(JSON.stringify({
  frames: frames,
  plainBytes: plainBytes,
  tornBytes: tornBytes,
  compressedBytes: buf.length,
}))
"""


class DecoderUnavailable(RuntimeError):
    """本机既没有可解 zstd 的 Node（22+），也没有 zstd 命令。"""


@dataclass(frozen=True)
class Decoder:
    """已探测通过的 zstd 解码后端。"""

    backend: str   # "node" | "zstd"
    exe: str
    version: str = ""


@dataclass(frozen=True)
class SessionLog:
    """一个待导入的 DSH 会话日志文件。"""

    path: Path
    workspace_dir: str   # sessions/<工作区编码目录>/
    session_dir: str     # sessions/<工作区编码目录>/<会话目录>/
    compressed: bool


def default_sessions_dir() -> Path:
    """DSH 会话根目录：默认 ``~/.dsh/sessions``（尊重 DSH_HOME 自定义安装）。"""
    env_home = str(os.environ.get("DSH_HOME") or "").strip()
    base = Path(env_home) if env_home else (Path.home() / ".dsh")
    return base / "sessions"


# ---------------------------------------------------------------- 解码器探测

def _run_quiet(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, **kwargs)


def _node_has_zstd(exe: str, timeout: float = 20.0) -> bool:
    """探测指定的 node 是否带 node:zlib zstd（Node 22+ / 23.8+ 才有）。"""
    try:
        rep = _run_quiet([exe, "-e", _NODE_PROBE], timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return rep.returncode == 0


def _node_version(exe: str) -> str:
    try:
        rep = _run_quiet([exe, "-e", _NODE_VERSION], timeout=20.0)
    except (OSError, subprocess.SubprocessError):
        return ""
    if rep.returncode != 0:
        return ""
    return rep.stdout.decode("utf-8", "replace").strip()


def _dsh_runtime_node_candidates() -> list[str]:
    """DSH 自带的 Node 运行时（PATH 上没有 node 或不支持 zstd 时兜底）。"""
    roots: list[Path] = []
    env_home = str(os.environ.get("DSH_HOME") or "").strip()
    if env_home:
        roots.append(Path(env_home))
    roots.append(Path.home() / ".dsh")
    out: list[str] = []
    seen: set[str] = set()
    for root in roots:
        try:
            runtimes = sorted((root / "dsh-runtimes").glob("*/dependencies/node/bin"))
        except OSError:
            continue
        for bindir in runtimes:
            for name in ("node.exe", "node"):
                cand = bindir / name
                key = os.path.normcase(str(cand))
                if key in seen:
                    continue
                seen.add(key)
                if cand.is_file():
                    out.append(str(cand))
    return out


def find_node_exe() -> str | None:
    """返回第一个「node 能解 zstd」的可执行文件；没有则 None。

    查找顺序：LINGSHU_NODE 环境变量 → PATH 上的 node → DSH 自带运行时。
    每个候选都真跑一次 ``node -e`` 探测，不只看版本号猜。
    """
    candidates: list[str] = []
    env_node = str(os.environ.get("LINGSHU_NODE") or "").strip()
    if env_node:
        candidates.append(env_node)
    for name in ("node", "node.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates.extend(_dsh_runtime_node_candidates())
    seen: set[str] = set()
    for cand in candidates:
        key = os.path.normcase(str(cand))
        if not key or key in seen:
            continue
        seen.add(key)
        if _node_has_zstd(str(cand)):
            return str(cand)
    return None


def find_zstd_exe() -> str | None:
    """系统 zstd CLI（次选解码器）。没有则 None。"""
    for name in ("zstd", "zstd.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def detect_decoder(*, node_exe: str | None = None) -> Decoder:
    """确定 zstd 解码后端；两者都没有时抛 DecoderUnavailable（绝不静默）。"""
    if node_exe:
        if not _node_has_zstd(str(node_exe)):
            raise DecoderUnavailable(
                f"指定的 Node 不支持 node:zlib zstd：{node_exe}（需要 Node 22+）")
        return Decoder("node", str(node_exe), _node_version(str(node_exe)))
    node = find_node_exe()
    if node:
        return Decoder("node", node, _node_version(node))
    zstd = find_zstd_exe()
    if zstd:
        return Decoder("zstd", zstd, "")
    raise DecoderUnavailable(
        "需要 Node 22+ 或 zstd 命令：本机既没有能解 zstd 的 Node"
        "（node:zlib.zstdDecompressSync 探测失败，PATH 与 DSH 自带运行时都不行），"
        "也没有 zstd 命令。系统 Python 没有 zstandard 模块，安装 Node 22+ 或 "
        "zstd 后重试；不会静默跳过或假装导入成功。")


# ---------------------------------------------------------------- 会话发现

def discover_sessions(sessions_dir=None, *, limit: int | None = None) -> list[SessionLog]:
    """遍历 <sessions>/<工作区>/<会话>/session*.jsonl[.zstd]，按修改时间倒序。"""
    root = Path(sessions_dir).expanduser() if sessions_dir else default_sessions_dir()
    if not root.is_dir():
        raise FileNotFoundError(f"DSH 会话目录不存在：{root}")
    logs: list[SessionLog] = []
    try:
        projects = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        raise RuntimeError(f"无法读取会话目录 {root}：{exc}") from exc
    for project in projects:
        if not project.is_dir():
            continue
        try:
            sessions = sorted(project.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for sess in sessions:
            if not sess.is_dir():
                continue
            try:
                entries = sorted(sess.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                continue
            for f in entries:
                match = _SESSION_LOG_RE.match(f.name)
                if not match or not f.is_file():
                    continue
                logs.append(SessionLog(path=f, workspace_dir=project.name,
                                       session_dir=sess.name,
                                       compressed=bool(match.group(1))))
    logs.sort(key=lambda x: _mtime(x.path), reverse=True)
    if limit is not None and int(limit) > 0:
        logs = logs[: int(limit)]
    return logs


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------- zstd 解码

def decode_session_file(src, dst, decoder: Decoder) -> dict:
    """把多帧 zstd 会话日志解成明文 JSONL，写进 dst 临时文件；返回解码元数据。

    返回统一 snake_case 键：backend/exe/frames/plain_bytes/compressed_bytes/
    torn_bytes（torn_bytes = 文件尾部不完整的 live 帧字节数，已忽略）。
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    raw = _decode_with_node(src, dst, decoder.exe) if decoder.backend == "node" \
        else _decode_with_zstd_cli(src, dst, decoder.exe)
    meta = {
        "backend": str(raw.get("backend") or decoder.backend),
        "exe": str(raw.get("exe") or decoder.exe),
        "frames": raw.get("frames"),
        "plain_bytes": int(raw.get("plainBytes") if raw.get("plainBytes") is not None
                           else raw.get("plain_bytes") or 0),
        "compressed_bytes": int(raw.get("compressedBytes")
                                if raw.get("compressedBytes") is not None
                                else raw.get("compressed_bytes") or 0),
        "torn_bytes": int(raw.get("tornBytes") if raw.get("tornBytes") is not None
                          else raw.get("torn_bytes") or 0),
    }
    if raw.get("warning"):
        meta["warning"] = raw["warning"]
    return meta


def _decode_with_node(src: Path, dst: Path, node_exe: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="lingshu-zstd-js-") as td:
        script = Path(td) / "decode_session.js"
        script.write_text(_NODE_DECODE_JS, encoding="utf-8")
        try:
            rep = subprocess.run([node_exe, str(script), str(src), str(dst)],
                                 capture_output=True, timeout=DEFAULT_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Node 解压超时（>{DEFAULT_TIMEOUT:g}s）：{src}") from exc
        except OSError as exc:
            raise RuntimeError(f"Node 启动失败（{node_exe}）：{exc}") from exc
    if rep.returncode != 0:
        err = rep.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"Node 解压失败（{src}）：{err[:500] or f'rc={rep.returncode}'}")
    try:
        meta = json.loads(rep.stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        meta = {}
    meta.setdefault("plainBytes", dst.stat().st_size if dst.exists() else 0)
    return meta


def _decode_with_zstd_cli(src: Path, dst: Path, zstd_exe: str) -> dict:
    try:
        with open(dst, "wb") as out:
            rep = subprocess.run([zstd_exe, "-d", "-q", "-c", str(src)],
                                 stdout=out, stderr=subprocess.PIPE,
                                 timeout=DEFAULT_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"zstd 解压超时（>{DEFAULT_TIMEOUT:g}s）：{src}") from exc
    except OSError as exc:
        raise RuntimeError(f"zstd 启动失败（{zstd_exe}）：{exc}") from exc
    stderr = (rep.stderr or b"").decode("utf-8", "replace").strip()
    meta = {"frames": None, "torn_bytes": 0,
            "plain_bytes": dst.stat().st_size if dst.exists() else 0,
            "compressed_bytes": src.stat().st_size if src.exists() else 0}
    if rep.returncode != 0:
        # live 会话尾部可能是半帧：zstd CLI 会报错但仍吐出完整帧的明文。
        # 只在明文以换行结尾（最后一帧完整）时接受，否则当失败。
        tail = b""
        if dst.exists() and dst.stat().st_size:
            with open(dst, "rb") as fh:
                fh.seek(max(0, dst.stat().st_size - 1))
                tail = fh.read(1)
        if not tail.endswith(b"\n"):
            raise RuntimeError(f"zstd 解压失败（{src}）：{stderr[:500] or f'rc={rep.returncode}'}")
        meta["warning"] = f"zstd 返回码 {rep.returncode}，已接受以换行结尾的部分输出：{stderr[:200]}"
    return meta


# ---------------------------------------------------------------- JSONL 解析

def read_session_header(jsonl_file) -> dict:
    """读解压后 JSONL 的第一条 ``{"type":"session"}``（格式保证是首行，最多扫 64 行）。"""
    try:
        with open(jsonl_file, "r", encoding="utf-8-sig", errors="replace") as fh:
            for _ in range(64):
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "session":
                    return event
    except OSError:
        pass
    return {}


def _text_fragments(content) -> str:
    """content 可能是字符串或 block 数组：只取 text 片段（跳过 reasoning/image/tool-*）。"""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            if block.strip():
                parts.append(block.strip())
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if text is not None and str(text).strip():
                parts.append(str(text).strip())
    return "\n".join(parts)


def _event_to_row(event: dict) -> dict | None:
    """把一条 DSH 会话事件转成 ingest 行；非对话事件（工具/推理/系统）返回 None。"""
    event_type = event.get("type")
    data = event.get("data")
    if not isinstance(data, dict):
        data = {}
    if event_type == "user/message":
        source = data.get("source")
        kind = str((source or {}).get("kind") or "") if isinstance(source, dict) else ""
        if kind and kind != "user":
            return None  # runtime-context / system-reminder 等注入块不是用户发言
        text = _text_fragments(data.get("content"))
        role = "user"
    elif event_type == "assistant/message":
        message = data.get("message")
        if not isinstance(message, dict):
            message = {}
        text = _text_fragments(message.get("content"))
        role = "assistant"
    else:
        return None
    if not text:
        return None  # 只有 reasoning / 只有 tool-call 的 assistant 轮次
    time_ms = event.get("time")
    ts = float(time_ms) / 1000.0 if isinstance(time_ms, (int, float)) else 0.0
    return {"role": role, "content": text, "ts": ts}


# ---------------------------------------------------------------- 导入

def _workspace_matches(want, workspace: str, workspace_dir: str) -> bool:
    """--workspace 匹配：精确路径 / basename / 原始工作区目录名，且允许子串。"""
    wanted = str(want or "").strip()
    if not wanted:
        return True
    low = wanted.lower().rstrip("\\/")
    names = {str(workspace or "").strip().lower().rstrip("\\/"),
             str(workspace_dir or "").strip().lower()}
    if workspace:
        names.add(Path(workspace).name.lower())
    if low in names:
        return True
    for name in names:
        if name and low in name:
            return True
    return False


def import_session_file(log: SessionLog, *, decoder: Decoder, dry_run: bool = False,
                        db=None, workspace_filter: str | None = None,
                        batch_size: int = BATCH_SIZE) -> dict:
    """解压 → 解析 → 批量入库一个会话文件（解压后的明文只在临时目录存活）。"""
    report: dict = {
        "file": str(log.path),
        "workspace_dir": log.workspace_dir,
        "session_dir": log.session_dir,
        "compressed": log.compressed,
        "session": log.session_dir,
        "workspace": log.workspace_dir,
        "messages": 0,
        "added": 0,
        "skipped": 0,
        "bad_lines": 0,
        "workspace_matched": True,
        "torn_bytes": 0,
    }
    with tempfile.TemporaryDirectory(prefix="lingshu-session-decode-") as td:
        plain = Path(td) / "session.jsonl"
        if log.compressed:
            meta = decode_session_file(log.path, plain, decoder)
        else:
            shutil.copyfile(log.path, plain)
            meta = {"backend": "plain", "frames": None, "torn_bytes": 0,
                    "plain_bytes": log.path.stat().st_size, "compressed_bytes": 0}
        report["decode"] = {
            "backend": meta.get("backend"),
            "frames": meta.get("frames"),
            "compressed_bytes": meta.get("compressed_bytes"),
            "plain_bytes": meta.get("plain_bytes"),
        }
        report["torn_bytes"] = int(meta.get("torn_bytes") or 0)
        if meta.get("warning"):
            report["warning"] = meta["warning"]

        header = read_session_header(plain)
        session_id = str(header.get("id") or "").strip() or log.session_dir
        workspace = str(header.get("cwd") or "").strip() or log.workspace_dir
        report["session"] = session_id
        report["workspace"] = workspace
        if not _workspace_matches(workspace_filter, workspace, log.workspace_dir):
            report["workspace_matched"] = False
            return report

        parsed = added = skipped = bad_lines = 0
        batch: list[dict] = []

        def flush() -> None:
            nonlocal added, skipped
            if not batch:
                return
            if not dry_run:
                rep = session_rag.ingest_messages(
                    batch, session=session_id, platform=PLATFORM,
                    workspace=workspace, path=db)
                added += int(rep.get("added") or 0)
                skipped += int(rep.get("skipped") or 0)
            batch.clear()

        with open(plain, "r", encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    bad_lines += 1
                    continue
                if not isinstance(event, dict):
                    bad_lines += 1
                    continue
                if event.get("type") == "session":
                    continue  # 首行 header，不是对话
                row = _event_to_row(event)
                if row is None:
                    continue
                parsed += 1
                batch.append(row)
                if len(batch) >= max(1, int(batch_size)):
                    flush()
            flush()

        report["messages"] = parsed
        report["bad_lines"] = bad_lines
        if not dry_run:
            report["added"] = added
            report["skipped"] = skipped
        else:
            report["dry_run"] = True
    return report


def import_sessions(sessions_dir=None, *, limit: int | None = None,
                    workspace: str | None = None, dry_run: bool = False,
                    db=None, decoder: Decoder | None = None) -> dict:
    """遍历导入 DSH 会话目录；返回可 JSON 化的报告。

    ``limit`` 限制「最终导入的会话数」而不是「扫描的文件数」：工作区过滤在
    解出会话 header 的 cwd 之后生效，过滤通过的按修改时间倒序取前 N 个。
    """
    started = time.time()
    root = Path(sessions_dir).expanduser() if sessions_dir else default_sessions_dir()
    if not root.is_dir():
        raise FileNotFoundError(f"DSH 会话目录不存在：{root}")
    logs = discover_sessions(root)
    dec = decoder if decoder is not None else detect_decoder()
    max_sessions = int(limit) if limit is not None and int(limit) > 0 else None
    files: list[dict] = []
    totals = {"files": 0, "sessions": 0, "messages": 0, "added": 0, "skipped": 0,
              "bad_lines": 0, "torn_bytes": 0}
    errors = 0
    for log in logs:
        try:
            rep = import_session_file(log, decoder=dec, dry_run=dry_run, db=db,
                                      workspace_filter=workspace)
        except RuntimeError as exc:
            errors += 1
            files.append({"file": str(log.path), "error": str(exc), "workspace_dir":
                          log.workspace_dir, "session_dir": log.session_dir})
            continue
        files.append(rep)
        if not rep.get("workspace_matched", True):
            continue
        totals["files"] += 1
        totals["sessions"] += 1
        totals["messages"] += int(rep.get("messages") or 0)
        totals["added"] += int(rep.get("added") or 0)
        totals["skipped"] += int(rep.get("skipped") or 0)
        totals["bad_lines"] += int(rep.get("bad_lines") or 0)
        totals["torn_bytes"] += int(rep.get("torn_bytes") or 0)
        if max_sessions is not None and totals["files"] >= max_sessions:
            break
    report = {
        "ok": errors == 0,
        "sessions_dir": str(root),
        "workspace_filter": str(workspace or ""),
        "dry_run": bool(dry_run),
        "decoder": {"backend": dec.backend, "exe": dec.exe, "version": dec.version},
        "files": files,
        "totals": totals,
        "errors": errors,
        "elapsed_ms": int(round((time.time() - started) * 1000)),
    }
    if not dry_run:
        try:
            report["db"] = session_rag.stats(path=db)
        except Exception as exc:  # noqa: BLE001 - 统计失败不该抹掉导入结果
            report["db"] = {"error": f"{type(exc).__name__}: {exc}"}
    return report


# ---------------------------------------------------------------- CLI

def _print_human(report: dict) -> None:
    dec = report.get("decoder") or {}
    version = f" {dec['version']}" if dec.get("version") else ""
    print(f"解码器：{dec.get('backend')}{version}（{dec.get('exe')}）")
    print(f"会话目录：{report.get('sessions_dir')}")
    if report.get("workspace_filter"):
        print(f"工作区过滤：{report['workspace_filter']}")
    for item in report.get("files") or []:
        if item.get("error"):
            print(f"- [错误] {item.get('session_dir') or item.get('file')}：{item['error']}")
            continue
        if not item.get("workspace_matched", True):
            continue
        tail = f"消息 {item.get('messages', 0)}，新增 {item.get('added', 0)}，" \
               f"跳过 {item.get('skipped', 0)}"
        if item.get("bad_lines"):
            tail += f"，坏行 {item['bad_lines']}"
        if item.get("torn_bytes"):
            tail += f"，忽略残帧 {item['torn_bytes']}B"
        if item.get("warning"):
            tail += f"，警告：{item['warning'][:120]}"
        print(f"- [{item.get('workspace')}] {item.get('session')}（{tail}）")
    filtered = sum(1 for item in report.get("files") or []
                   if not item.get("error") and not item.get("workspace_matched", True))
    if filtered:
        print(f"（按工作区过滤掉 {filtered} 个会话）")
    totals = report.get("totals") or {}
    suffix = "（dry-run，未写库）" if report.get("dry_run") else ""
    print(f"共 {totals.get('files', 0)} 个会话：解析 {totals.get('messages', 0)} 条，"
          f"新增 {totals.get('added', 0)}，跳过 {totals.get('skipped', 0)}，"
          f"用时 {report.get('elapsed_ms', 0) / 1000.0:.1f}s{suffix}")
    db = report.get("db")
    if isinstance(db, dict) and not db.get("error"):
        print(f"原文库：{db.get('messages', 0)} 条 / {db.get('sessions', 0)} 场会话"
              f"（platform={db.get('by_platform')}）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="session_import.py",
        description="把 DSH 会话日志（多帧 zstd）导入 session_rag 原文库（附录 B · P1）")
    parser.add_argument("--sessions-dir", default="",
                        help="DSH 会话根目录，默认 ~/.dsh/sessions")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多导入 N 个会话（工作区过滤后按修改时间倒序，0=全部）")
    parser.add_argument("--workspace", default="",
                        help="只导入匹配的工作区（路径/basename/目录名，子串匹配）")
    parser.add_argument("--dry-run", action="store_true", help="只解析不写库")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = import_sessions(
            sessions_dir=args.sessions_dir or None,
            limit=args.limit or None,
            workspace=args.workspace or None,
            dry_run=bool(args.dry_run),
        )
    except DecoderUnavailable as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"[错误] 导入失败：{exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0 if report.get("ok") else 1


def _ensure_utf8_stdout() -> None:
    """把 stdout/stderr 切到 UTF-8，避免 Windows 控制台（cp1252/cp936）编不了中文就崩。

    CI 的 windows-latest stdout 是 cp1252：脚本一 print 中文就 UnicodeEncodeError
    直接退出（实测 sync_engine.py / make_checksums.py 都因此挂过，Ubuntu 同期正常）。
    这不是业务 bug，是"中文输出 + 非 UTF-8 控制台"的必然结果。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        fn = getattr(stream, "reconfigure", None)
        if callable(fn):
            try:
                fn(encoding="utf-8", errors="replace")
                continue
            except (OSError, ValueError, AttributeError):
                pass
        try:
            import io
            buf = getattr(stream, "buffer", None)
            if buf is not None:
                setattr(sys, name,
                        io.TextIOWrapper(buf, encoding="utf-8", errors="replace",
                                         line_buffering=True))
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    _ensure_utf8_stdout()
    sys.exit(main())
