#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨会话原文检索服务（附录 B · P0 的最小实现）。

零依赖：sqlite3 + FTS5 trigram（中文子串可查）。职责很窄：
    init    建库建表
    ingest  把对话 JSONL 增量写进原文库（按内容哈希幂等）
    search  FTS 检索（返回 id/session/role/ts/text）
    serve   本地 HTTP：POST /v1/search（附录 B 契约），GET /health

刻意不做：向量、模型挑选、多平台导入（那些是 P1+）。
它的作用是让 `retriever` 的 session-rag 分支有个真服务可连，先跑通端到端。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  session TEXT NOT NULL,
  platform TEXT DEFAULT '',
  workspace TEXT DEFAULT '',
  role TEXT DEFAULT '',
  ts REAL DEFAULT 0,
  text TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text, content='messages', content_rowid='rowid', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.rowid, old.text);
END;
"""


def db_path(home=None) -> Path:
    try:
        import core
        base = Path(home or core.HOME)
    except Exception:
        base = Path(home or (Path.home() / ".brain-memory"))
    return base / "session_rag.db"


def connect(path=None) -> sqlite3.Connection:
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(path=None) -> dict:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        rows = conn.execute("SELECT count(*) AS n FROM messages").fetchone()["n"]
    return {"ok": True, "path": str(path or db_path()), "messages": rows}


def _msg_id(session: str, role: str, ts: float, text: str) -> str:
    raw = f"{session}|{role}|{ts}|{text}".encode("utf-8")
    return "sr_" + hashlib.sha256(raw).hexdigest()[:24]


def ingest_messages(rows, *, session: str = "default", platform: str = "",
                    workspace: str = "", path=None) -> dict:
    """rows: [{"role","content"/"text","ts"}]；按内容哈希幂等。"""
    added = skipped = 0
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            text = r.get("content", r.get("text", ""))
            if isinstance(text, list):
                text = " ".join(str(x.get("text") if isinstance(x, dict) else x) for x in text)
            text = str(text or "").strip()
            if not text:
                skipped += 1
                continue
            role = str(r.get("role") or "other").lower()
            ts = float(r.get("ts") or r.get("created") or 0)
            mid = _msg_id(session, role, ts, text)
            try:
                conn.execute(
                    "INSERT INTO messages(id, session, platform, workspace, role, ts, text)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (mid, session, platform, workspace, role, ts, text))
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1
        conn.commit()
        total = conn.execute("SELECT count(*) AS n FROM messages").fetchone()["n"]
    return {"ok": True, "added": added, "skipped": skipped, "total": total}


def ingest_jsonl(file, *, session: str = "", platform: str = "", workspace: str = "",
                 path=None) -> dict:
    p = Path(file)
    rows = []
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return ingest_messages(rows, session=session or p.stem, platform=platform,
                           workspace=workspace, path=path)


def search(query: str, k: int = 10, *, path=None) -> list[dict]:
    q = str(query or "").strip()
    if not q:
        return []
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        # FTS5 trigram 只对 ≥3 字符的词元建索引：中文两字词（"云南"）MATCH 不到，
        # 短查询走 LIKE 兜底（200k 条内可接受；更长的词仍走 FTS 排序）。
        if len(q) < 3:
            rows = conn.execute(
                "SELECT id, session, platform, workspace, role, ts, text, 0.0 AS score"
                " FROM messages WHERE text LIKE ? ORDER BY ts DESC LIMIT ?",
                (f"%{q}%", max(1, int(k)))).fetchall()
            return [{"id": r["id"], "session": r["session"], "platform": r["platform"],
                     "workspace": r["workspace"], "role": r["role"], "ts": r["ts"],
                     "text": r["text"], "score": float(r["score"] or 0.0)} for r in rows]
        try:
            rows = conn.execute(
                "SELECT m.id, m.session, m.platform, m.workspace, m.role, m.ts, m.text,"
                " bm25(messages_fts) AS score"
                " FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid"
                " WHERE messages_fts MATCH ? ORDER BY score LIMIT ?",
                (q, max(1, int(k)))).fetchall()
        except sqlite3.OperationalError:
            return []
    return [{"id": r["id"], "session": r["session"], "platform": r["platform"],
             "workspace": r["workspace"], "role": r["role"], "ts": r["ts"],
             "text": r["text"], "score": float(r["score"] or 0.0)} for r in rows]


def stats(path=None) -> dict:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        n = conn.execute("SELECT count(*) AS n FROM messages").fetchone()["n"]
        sess = conn.execute("SELECT count(DISTINCT session) AS n FROM messages").fetchone()["n"]
    return {"messages": n, "sessions": sess, "path": str(path or db_path())}


def serve(host: str = "127.0.0.1", port: int = 8791, *, path=None) -> None:
    """极简 HTTP：POST /v1/search {"query","k"}、GET /health。仅本机。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静默
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0] == "/health":
                self._json({"ok": True, **stats(path)})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            if self.path.split("?")[0] != "/v1/search":
                self._json({"error": "not found"}, 404)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(min(n, 1 << 20)) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400)
                return
            self._json({"results": search(data.get("query", ""), data.get("k", 10), path=path)})

    ThreadingHTTPServer((host, port), H).serve_forever()
