# -*- coding: utf-8 -*-
"""教训库（v3.70 从 memory.py 拆出）。

依赖约定（与 mem_topic.py 同一套，务必遵守，否则循环导入）：
  - 只依赖 core 的名字（HOME / READONLY / read_jsonl / write_jsonl / _locked /
    now_ts / _num / audit_log / log_event / ensure_home 等，core 已 star-import）
    与标准库（hashlib / os / Path）；**绝不 import memory**；
  - memory.py 末尾 `from mem_lesson import *` 保留 memory.{lessons_file,
    load_lessons, add_lesson, ...} 调用面。

拆出理由：教训库是一组自洽的 read-modify-write 操作（读库/校验/退役/审核），
与记忆主链路无耦合。边界经依赖分析确认：L2830-2955 内 9 个函数依赖块外
memory 函数为空，只依赖 core + 标准库。injectable_lessons 依赖 _assoc_file
（关联图），留在 memory.py，不随块搬。
"""
from __future__ import annotations

import hashlib
import os

import core  # noqa: F401  教训库要动态取 core.HOME（--home/--init 切换后指向新目录）
from core import *  # noqa: F401,F403
# _num 是下划线开头，from core import * 不会带进来；memory.py 原本靠
# mem_score 的 star-import 提供它，新模块要显式取，否则 add_lesson 必崩。
from mem_score import _num  # noqa: F401
from core import _locked  # noqa: F401  下划线名字 star-import 不带，需显式取

__all__ = [
    "LESSONS_FILE_NAME", "lessons_file", "_lesson_id",
    "load_lessons", "save_lessons", "list_lessons", "add_lesson",
    "validate_lesson", "set_lesson_status", "lessons_for_review",
]


# 教训库：用对了 times_validated+1、confidence 上调；用错了降权；confidence <0.2 标记
# retired 退出注入——错的教训比没有教训更危险。
LESSONS_FILE_NAME = "lessons.jsonl"


def lessons_file() -> Path:
    """教训库文件路径。动态取 core.HOME：--home/--init 切换后指向新目录。"""
    return core.HOME / LESSONS_FILE_NAME


def _lesson_id(ts: int) -> str:
    h = hashlib.sha1(f"lesson{ts}{os.urandom(4).hex()}".encode("utf-8")).hexdigest()[:8]
    return f"les_{ts}_{h}"


def load_lessons() -> list:
    """读教训库。坏行跳过 + warn 日志（与 read_jsonl 同风格），畸形条目过滤。"""
    raw = read_jsonl(lessons_file())
    good, bad = [], 0
    for r in raw:
        if isinstance(r, dict) and r.get("id") and r.get("text"):
            good.append(r)
        else:
            bad += 1
    if bad:
        log_event("lessons", f"教训库跳过 {bad} 条畸形记录（缺 id/text 或类型错）", level="warning")
    return good


def save_lessons(rows: list) -> None:
    write_jsonl(lessons_file(), rows)


def list_lessons(include_retired: bool = False) -> list:
    ls = load_lessons()
    if not include_retired:
        ls = [l for l in ls if not l.get("retired")]
    # 按 confidence 从高到低，同分新的在前
    return sorted(ls, key=lambda l: (-_num(l.get("confidence"), 0.5), -_num(l.get("created"), 0)))


def add_lesson(text: str, evidence=None, confidence: float = 0.5,
               status: str = "active") -> dict:
    """新增一条教训。做梦提炼的默认 status="pending"（待审核闸），人工 add 的直接 active。"""
    ensure_home()
    text = (text or "").strip()
    if not text:
        raise ValueError("教训内容为空")
    if READONLY:
        raise PermissionError("记忆库当前只读，无法写教训。用 --home 指向可写目录。")
    conf = _num(confidence, 0.5)
    conf = max(0.0, min(1.0, conf))
    ts = now_ts()
    lesson = {
        "id": _lesson_id(ts),
        "text": text,
        "kind": "lesson",
        "status": status if status in ("pending", "active") else "pending",
        "confidence": round(conf, 3),
        "times_validated": 0,
        "evidence": [str(e) for e in (evidence or []) if e],
        "created": ts,
        "updated": ts,
    }
    # 写入加锁：教训库与记忆库一样是 read-modify-write，不加锁并发写会互相覆盖
    with _locked(lessons_file()):
        save_lessons(load_lessons() + [lesson])
    audit_log("system" if status == "pending" else "user", "lesson_add", lesson["id"],
              f"confidence={lesson['confidence']}, status={lesson['status']}")
    log_event("lessons", f"新增教训 {lesson['id']}（{lesson['status']}）")
    return lesson


def validate_lesson(lesson_id: str, ok: bool) -> dict:
    """校验教训：ok=True 认为有用（times_validated+1、confidence 上调）；
    ok=False 踩了坑（降权）；confidence <0.2 标记 retired，不再注入。"""
    if READONLY:
        raise PermissionError("记忆库当前只读，无法校验教训。用 --home 指向可写目录。")
    lesson_id = str(lesson_id or "").strip()
    with _locked(lessons_file()):
        rows = load_lessons()
        target = next((l for l in rows if l.get("id") == lesson_id), None)
        if not target:
            raise ValueError(f"没有这条教训：{lesson_id}")
        conf = _num(target.get("confidence"), 0.5)
        if ok:
            target["times_validated"] = int(_num(target.get("times_validated"), 0)) + 1
            # 上调有上限（0.95）：再对也不能当铁律，教训永远依赖语境
            conf = min(0.95, conf + 0.10)
        else:
            conf = conf - 0.15
        target["confidence"] = round(max(0.0, conf), 3)
        target["updated"] = now_ts()
        # 低于 0.2 退役：留着仅作档案，注入与校验提示都不再带它
        if target["confidence"] < 0.2 and not target.get("retired"):
            target["retired"] = True
            target["retired_at"] = now_ts()
            audit_log("system", "lesson_retire", lesson_id,
                      f"confidence={target['confidence']} < 0.2，自动退役")
        save_lessons(rows)
    audit_log("user", "lesson_validate", lesson_id,
              f"ok={ok}, confidence={target['confidence']}, "
              f"times_validated={target['times_validated']}")
    return dict(target)


def set_lesson_status(lesson_id: str, status: str) -> dict:
    """审核闸：pending → active（review confirm 用）/ active → discarded（review discard 用）。"""
    if status not in ("pending", "active", "discarded"):
        raise ValueError(f"非法状态：{status}")
    if READONLY:
        raise PermissionError("记忆库当前只读，无法改教训状态。用 --home 指向可写目录。")
    with _locked(lessons_file()):
        rows = load_lessons()
        target = next((l for l in rows if l.get("id") == lesson_id), None)
        if not target:
            raise ValueError(f"没有这条教训：{lesson_id}")
        target["status"] = status
        target["updated"] = now_ts()
        save_lessons(rows)
    audit_log("user", "lesson_review", lesson_id, f"status → {status}")
    return dict(target)


def lessons_for_review() -> list:
    return [l for l in load_lessons() if l.get("status") == "pending"]
