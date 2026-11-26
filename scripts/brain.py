#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 —— 类脑记忆引擎入口。

v2.22 起实现按领域拆到同目录的 core/memory/emotion/persona/dream/cli：
本文件只做命名空间合并与 re-export，保证 `python scripts/brain.py <子命令>`
以及外部 `import brain` 的全部旧名字照旧可用。
"""

from __future__ import annotations

import sys

import core
import memory
import emotion
import persona
import dream
import cli

_MODULES = (core, memory, emotion, persona, dream, cli)

# 拆分出去的 mem_* 子模块用 `from core import *` 拿 READONLY/HOME——那是
# **import 时的副本**，core 后来改（--ro、schema 降级、--home）它们看不到。
# 后果实测过：`--ro lessons add` 退出码 0 且真把记录写进 lessons.jsonl，
# 而主链路 remember 被正确拒绝——只读闸在教训库上形同不存在。
# 登记进 _SYNC_TARGETS，让 _sync_state() 把状态同步过去（注意只登记进同步表，
# 不进 _MODULES：它们的命名空间不该参与双向合并）。
_EXTRA_SYNC = []
for _name in ("mem_lesson", "mem_score", "mem_topic", "mem_dialogue", "mem_search"):
    _m = sys.modules.get(_name)
    if _m is not None:
        _EXTRA_SYNC.append(_m)

# 拆分前所有函数共用同一个模块命名空间，靠名字互相调用。拆分后先把各模块命名空间
# 合并进本模块，再原样回灌给每个子模块——跨模块调用仍按名字解析（过渡桥，
# 以后可以逐步改成显式 import，但不影响命令行兼容）。
for _m in _MODULES:
    globals().update({k: v for k, v in _m.__dict__.items() if not k.startswith("__")})

for _m in _MODULES:
    _m.__dict__.update({k: v for k, v in globals().items() if not k.startswith("__")})

# --home / --ro 会改 core 里的 HOME/READONLY；登记同步目标后，派生路径与只读状态
# 都会在切换时同步到所有子模块（原先单文件时天然共享，拆分后不会）。
# brain 模块自身也要登记：panels 等扩展模块 import 的是 brain.HOME，
# 漏掉自己会让 --home 切换后 brain.HOME 仍是旧目录（core 已换、brain 没换）。
core._SYNC_TARGETS = [sys.modules[__name__], *list(_MODULES)]
# mem_* 拆分模块只同步状态名（HOME/READONLY/派生路径），不整份覆盖命名空间
core._SYNC_STATE_ONLY = list(_EXTRA_SYNC)
core._sync_state()
core._recompute_paths()


def main(argv=None) -> int:
    return cli.main(argv)


if __name__ == "__main__":
    sys.exit(main())
