# -*- coding: utf-8 -*-
"""自动更新灵枢（P11 #75）。

安全顺序（任一步失败都不留半新半旧）：
  1. 先查是否有新版本（growth.update_check，不联网就不动手）
  2. **先备份记忆库**（记忆比代码重要，更新失败至少记忆在）
  3. growth.apply_update 原子替换（内部：SHA256 校验 → 全量备份 → 回滚兜底）
  4. 写审计日志 + 结果
默认关闭（config.auto_update=false）：自动改自己的代码风险高，必须用户显式开启。
"""
from __future__ import annotations

import json
import time

import core


def _cfg() -> dict:
    try:
        return core.load_config()
    except Exception:
        return {}


def enabled() -> bool:
    return bool(_cfg().get("auto_update"))


def check(force_pull: bool = False) -> dict:
    """查有没有新版本（只读）。"""
    import growth
    try:
        return growth.update_check("lingshu", force_pull=force_pull)
    except Exception as exc:  # noqa: BLE001
        core.log_event("update", f"检查更新失败：{exc}", level="warn")
        return {"ok": False, "error": str(exc)}


def run(force: bool = False) -> dict:
    """执行一次自动更新（含更新前备份）。返回结果字典。"""
    if core.READONLY:
        return {"ok": False, "reason": "记忆库只读，跳过自动更新"}
    if not force and not enabled():
        return {"ok": False, "skipped": True, "reason": "auto_update 未开启"}

    chk = check(force_pull=True)
    if not chk or not chk.get("update_available"):
        return {"ok": True, "skipped": True,
                "reason": "已是最新", "current": chk.get("current"),
                "latest": chk.get("latest")}

    # ① 更新前先备份记忆库（失败就不更新：宁可旧代码，不能丢记忆）
    bak = None
    try:
        import cli
        code = cli.cmd_backup(target=None, keep=0, password=None, assume_yes=True)
        if code != 0:
            return {"ok": False, "reason": "更新前备份失败，已中止（不冒险）"}
        import panel_data
        lst = panel_data.list_backups(limit=1)
        bak = (lst.get("items") or [{}])[0].get("name")
    except Exception as exc:  # noqa: BLE001
        core.log_event("update", f"更新前备份异常：{exc}", level="error")
        return {"ok": False, "reason": f"更新前备份异常，已中止：{exc}"}

    # ② 原子更新
    try:
        import growth
        src, kind = growth.resolve_source("lingshu", force_pull=False)
        res = growth.install_skill("lingshu", force=True, from_dir=str(src))
    except Exception as exc:  # noqa: BLE001
        core.log_event("update", f"自动更新失败：{exc}", level="error")
        return {"ok": False, "reason": f"更新失败（已回滚）：{exc}", "backup": bak}

    core.log_event("update", f"自动更新完成：{chk.get('current')} → {chk.get('latest')}；"
                             f"更新前备份 {bak}")
    return {"ok": True, "updated": True, "backup": bak,
            "from": chk.get("current"), "to": chk.get("latest"),
            "detail": res if isinstance(res, dict) else str(res),
            "at": int(time.time())}
