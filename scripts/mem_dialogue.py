# -*- coding: utf-8 -*-
"""跨对话上下文摄取（v3.39 从 memory.py 拆出）。

依赖约定：
  - 可变全局（HOME/锁/原子写/日志）走 `core.*`，不复制值；
  - 需要 memory 的读写（remember/load_all）在调用时取模块，避免导入期循环。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import core
from core import *  # noqa: F401,F403  （now_ts/_num/_cfg_num 等纯函数）


def _mem():
    import memory
    return memory


_DIALOGUE_EXTS = (".jsonl", ".md", ".txt")
_DIALOGUE_MAX_DEPTH = 2
_DIALOGUE_READ_CAP = 8 * 1024 * 1024  # 单次最多读 8MB；大文件按完整行分批推进偏移


def _dialogue_offsets_path() -> Path:
    return core.HOME / "dialogue_offsets.json"


def _dialogue_load_offsets() -> dict:
    p = _dialogue_offsets_path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _dialogue_decode(blob: bytes, source: str) -> str:
    """BOM → UTF-8 严格解码 → GB18030 回退；实际编码留日志，不乱码静默入库。"""
    if not blob:
        return ""
    encoding = "utf-8"
    if blob.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif blob.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        try:
            return blob.decode("utf-8")
        except UnicodeDecodeError:
            encoding = "gb18030"
    try:
        text = blob.decode(encoding)
    except UnicodeDecodeError:
        text = blob.decode(encoding, errors="replace")
    core.log_event("dialogue", f"对话摄取解码（{source}）：{encoding}", level="info")
    return text


def _dialogue_turns_from_text(text: str, ext: str) -> list:
    """把导出片段解析成 [(role, text)]；role ∈ user / ai / other。"""
    turns = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        if ext == ".jsonl":
            try:
                d = json.loads(s)
            except Exception:
                continue
            if isinstance(d, str):
                turns.append(("other", d))
                continue
            if not isinstance(d, dict):
                continue
            txt = d.get("content") or d.get("text") or d.get("message") or ""
            if isinstance(txt, list):   # OpenAI 风格 content parts
                txt = " ".join(str(x.get("text") if isinstance(x, dict) else x) for x in txt)
            txt = str(txt).strip()
            if not txt:
                continue
            raw_role = str(d.get("role") or d.get("sender") or d.get("from") or "").lower()
            if raw_role in ("user", "human", "me", "我", "用户"):
                role = "user"
            elif raw_role in ("assistant", "ai", "bot", "model", "灵枢", "助手"):
                role = "ai"
            else:
                role = "other"
            turns.append((role, txt))
        else:
            m = re.match(r"^\s*(user|human|assistant|ai|bot|model|我|用户|助手|灵枢)\s*[:：]\s*(.+)$",
                         s, re.I)
            if m:
                who = m.group(1).lower()
                role = "user" if who in ("user", "human", "我", "用户") else "ai"
                turns.append((role, m.group(2).strip()))
            else:
                turns.append(("other", s))
    return turns


def _dialogue_chunks(turns: list, max_chars: int = 700) -> list:
    """相邻轮次拼成块（用户/我 前缀）；块满就切，一块 = 一条 episode 记忆。"""
    chunks, buf, size = [], [], 0
    for role, text in turns:
        if len(text) < 2:
            continue
        prefix = "用户: " if role == "user" else ("我: " if role == "ai" else "")
        piece = prefix + text
        if buf and size + len(piece) > max_chars:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(piece)
        size += len(piece) + 1
    if buf:
        chunks.append("\n".join(buf))
    return [c for c in chunks if len(c) >= 8]


def _dialogue_files(sources: list, max_files: int) -> list:
    """收集候选文件（最多 max_files 个，按 mtime 新→旧）。"""
    found = []
    for src in sources:
        root = Path(str(src)).expanduser()
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for f in root.rglob("*"):
            try:
                if len(f.parts) - base_depth > _DIALOGUE_MAX_DEPTH:
                    continue
                if f.is_file() and f.suffix.lower() in _DIALOGUE_EXTS:
                    found.append(f)
            except OSError:
                continue
    found.sort(key=lambda f: -(f.stat().st_mtime if f.exists() else 0))
    return found[:max_files]


def ingest_dialogues(paths=None, limit: int | None = None, dry_run: bool = False) -> dict:
    """增量摄取其他会话的对话导出 → episode 记忆。

    参数：
      paths  额外/替代的源目录（缺省用 config.dialogue_sources）
      limit  本次最多处理多少个文件（缺省 config.dialogue_ingest_max_files）
      dry_run 只统计不写库（不推进偏移、不写记忆）
    返回：{enabled, files, scanned, imported, skipped, chars, dry_run}
    """
    cfg = load_config()
    if not cfg.get("dialogue_ingest"):
        return {"ok": True, "enabled": False, "files": 0, "scanned": 0,
                "imported": 0, "skipped": 0, "chars": 0, "dry_run": dry_run,
                "reason": "未开启：config dialogue_ingest=false"}
    sources = [str(x) for x in (paths or cfg.get("dialogue_sources") or []) if str(x).strip()]
    if not sources:
        return {"ok": True, "enabled": True, "files": 0, "scanned": 0,
                "imported": 0, "skipped": 0, "chars": 0, "dry_run": dry_run,
                "reason": "没有配置 dialogue_sources 目录"}
    max_files = max(1, min(int(limit or cfg.get("dialogue_ingest_max_files") or 50), 500))
    max_chars = max(200, min(int(cfg.get("dialogue_ingest_max_chars") or 700), 2000))
    imported, skipped, chars, touched = 0, 0, 0, 0
    # offsets 既记读取位置，也记已导入块哈希：去重不再全量扫记忆库；
    # 整个读偏移→写记忆→存偏移过程持同一把锁，避免两个进程重复导入。
    with core._locked(_dialogue_offsets_path()):
        files = _dialogue_files(sources, max_files)
        offsets = _dialogue_load_offsets()
        known = {h for entry in offsets.values() if isinstance(entry, dict)
                 for h in (entry.get("chunk_hashes") or [])}
        for f in files:
            key = str(f.resolve())
            try:
                st = f.stat()
            except OSError:
                continue
            entry = offsets.get(key) or {}
            # 用 offset==size 判断"已读干净"：尾部半行没读完时不跳过，
            # 下次等它补上换行再继续（否则会永久漏掉最后一条）。
            if entry.get("offset") == st.st_size and entry.get("mtime") == st.st_mtime:
                continue   # 没变过：完全不读
            start = int(entry.get("offset") or 0)
            if st.st_size < start:
                start = 0   # 文件被截断/重写：从头再来（块哈希去重兜底）
            remaining = max(0, st.st_size - start)
            try:
                with open(f, "rb") as fh:
                    fh.seek(start)
                    blob = fh.read(min(_DIALOGUE_READ_CAP, remaining))
            except OSError:
                continue
            if remaining > _DIALOGUE_READ_CAP:
                core.log_event("dialogue", f"对话摄取分段读取（{f.name}）："
                                      f"偏移 {start}，本次 {len(blob)} 字节，剩余下次继续",
                          level="info")
            # 偏移永远落在完整行边界：尾部半行（正在写/无换行结尾）留到下次，
            # 否则续读时会把一条完整的新记录误当成"残片"丢掉。
            if blob and not blob.endswith(b"\n"):
                cut = blob.rfind(b"\n")
                if cut < 0:
                    continue
                blob = blob[:cut + 1]
            text = _dialogue_decode(blob, f.name)
            turns = _dialogue_turns_from_text(text, f.suffix.lower())
            file_hashes = list(entry.get("chunk_hashes") or [])
            for chunk in _dialogue_chunks(turns, max_chars):
                digest = hashlib.sha256(chunk.strip().encode("utf-8")).hexdigest()
                if digest in known:
                    skipped += 1
                    continue
                if not dry_run:
                    try:
                        _mem().remember(chunk, kind="episode", importance=0.45,
                                 tags=["对话摄取", f.stem], source="dialogue",
                                 channel=f"dialogue:{f.stem}")
                    except Exception as exc:
                        core.log_event("dialogue", f"对话摄取写入失败（{f.name}）：{exc}", level="warn")
                        continue
                known.add(digest)
                file_hashes.append(digest)
                imported += 1
                chars += len(chunk)
            if not dry_run:
                # offset 只推进到最后一个完整行末尾；尾部半行等下次补全
                offsets[key] = {"offset": start + len(blob), "size": st.st_size,
                                "mtime": st.st_mtime, "ts": now_ts(),
                                "chunk_hashes": file_hashes}
                touched += 1
        if not dry_run and touched:
            try:
                core.atomic_json(_dialogue_offsets_path(), offsets)
            except Exception as exc:
                core.log_event("dialogue", f"对话摄取偏移保存失败：{exc}", level="warn")
    return {"ok": True, "enabled": True, "files": len(files), "scanned": len(files),
            "imported": imported, "skipped": skipped, "chars": chars,
            "dry_run": dry_run, "sources": sources}

