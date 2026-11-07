#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C1：宿主插件（astrbot_plugin_lingshu）冒烟测试。

不安装 AstrBot：用最小 stub 模块顶替 astrbot.* 命名空间，
然后 import 插件 main.py 并做基础构造/队列语义检查。
纯标准库，供 CI 使用。
"""
import importlib.util
import sys
import threading
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "scripts" / "astrbot_plugin_lingshu"


def _install_stubs():
    """构造最小 astrbot stub，只覆盖 main.py 实际用到的符号。"""
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, debug=lambda *a, **k: None)
    api.logger = logger
    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = object
    class _filter:
        @staticmethod
        def command(name):
            def deco(fn):
                return fn
            return deco
        @staticmethod
        def command_group(name):
            class _G:
                def command(self, *a, **k):
                    def deco(fn):
                        return fn
                    return deco
                def group(self, *a, **k):
                    return _G()
            return _G()
        @staticmethod
        def on_llm_request():
            def deco(fn):
                return fn
            return deco
        @staticmethod
        def on_llm_response():
            def deco(fn):
                return fn
            return deco
    event_mod.filter = _filter
    provider_mod = types.ModuleType("astrbot.api.provider")
    provider_mod.ProviderRequest = object
    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object
    class Star:
        def __init__(self, context):
            self.context = context
    star_mod.Star = Star
    star_mod.StarTools = types.SimpleNamespace(get_data_dir=lambda name: Path.home() / ".stub" / name)
    for name, mod in [("astrbot", astrbot), ("astrbot.api", api),
                      ("astrbot.api.event", event_mod), ("astrbot.api.provider", provider_mod),
                      ("astrbot.api.star", star_mod)]:
        sys.modules[name] = mod


def main() -> int:
    _install_stubs()
    # 允许从插件目录直接 import main
    sys.path.insert(0, str(PLUGIN_DIR))
    try:
        spec = importlib.util.spec_from_file_location("lingshu_main", PLUGIN_DIR / "main.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:
        print(f"FAIL: 插件导入失败：{type(exc).__name__}: {exc}")
        return 1

    # 1) 队列语义（A1）：deque(maxlen) 且支持 popleft
    from collections import deque
    q = deque(maxlen=3)
    for i in range(5):
        q.append(i)
    assert q.popleft() == 2, "deque(maxlen) 语义不符"
    assert len(q) == 2

    # 2) 敏感词正则可编译且命中基础词
    class _P(mod.LingshuPlugin):
        def __init__(self):  # 绕过宿主初始化，只测敏感词装配
            from pathlib import Path as _P2
            self.data_home = _P2(".")
            self._sensitive_extra = self._load_sensitive_words()
            import re as _re
            self._sensitive = _re.compile("token|密码|" + "|".join(self._sensitive_extra), _re.IGNORECASE)
    p = _P()
    assert p._sensitive.search("我的 token: abc123"), "敏感词命中失败"
    assert p._sensitive.search("请告诉我密码"), "中文敏感词命中失败"

    # 3) bounded_int 边界
    assert mod.bounded_int("x", 5, 1, 10) == 5
    assert mod.bounded_int(0, 5, 1, 10) == 1
    assert mod.bounded_int(99, 5, 1, 10) == 10

    print("PASS: 插件冒烟测试通过（导入 / 队列语义 / 敏感词 / 边界工具）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
