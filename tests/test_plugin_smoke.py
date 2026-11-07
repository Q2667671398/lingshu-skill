#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C1：宿主插件（astrbot_plugin_lingshu）冒烟测试。

不安装 AstrBot：用最小 stub 模块顶替 astrbot.* 命名空间，
然后 import 插件 main.py 并做基础构造/队列语义检查。
纯标准库，供 CI 使用。
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import threading
import types
from pathlib import Path
from urllib.parse import quote

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "scripts" / "astrbot_plugin_lingshu"
REPO_ROOT = Path(__file__).resolve().parent.parent

# 4.13 装饰器注册表：stub 装饰器把被装饰方法记录到这里，供冒烟断言使用
DECORATED = {"command": [], "on_llm_request": [], "on_llm_response": [],
             "event_message_type": []}


def _install_stubs():
    """构造最小 astrbot stub，只覆盖 main.py 实际用到的符号。"""
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, debug=lambda *a, **k: None)
    api.logger = logger
    event_mod = types.ModuleType("astrbot.api.event")

    class EventMessageEvent:
        """AstrMessageEvent 假类：提供插件实际调用的宿主接口。"""
        unified_msg_origin = "stub:platform:session"

        def __init__(self, platform="stub", sender="u1", platform_id="pid1",
                     group_id="", message_str="", messages=None):
            self._platform, self._sender = platform, sender
            self._platform_id, self._group_id = platform_id, group_id
            self._message_str = message_str
            self._messages = messages or []
            self._stopped = False
            self._results = []

        def get_platform_name(self):
            return self._platform

        def get_sender_id(self):
            return self._sender

        def get_platform_id(self):
            return self._platform_id

        def get_group_id(self):
            return self._group_id

        def get_sender_name(self):
            return "测试用户"

        def get_message_str(self):
            return self._message_str

        def get_messages(self):
            return self._messages

        def plain_result(self, text):
            self._results.append(text)
            return text

        def stop_event(self):
            self._stopped = True

    class AstrMessageEvent(EventMessageEvent):
        pass

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.EventMessageType = types.SimpleNamespace(ALL="all", GROUP_MESSAGE="g", PRIVATE_MESSAGE="p")

    def _register(kind, *meta):
        def deco(fn):
            DECORATED[kind].append((getattr(fn, "__name__", str(fn)), meta))
            return fn
        return deco

    class _filter:
        @staticmethod
        def command(name):
            return _register("command", name)
        @staticmethod
        def command_group(name):
            class _G:
                def command(self, *a, **k):
                    return _register("command", name, *a)
                def group(self, *a, **k):
                    return _G()
            return _G()
        @staticmethod
        def event_message_type(mt):
            return _register("event_message_type", mt)
        @staticmethod
        def on_llm_request():
            return _register("on_llm_request")
        @staticmethod
        def on_llm_response():
            return _register("on_llm_response")
    event_mod.filter = _filter

    provider_mod = types.ModuleType("astrbot.api.provider")

    class ProviderRequest:
        def __init__(self, system_prompt="", contexts=None):
            self.system_prompt = system_prompt
            self.contexts = contexts or []
    provider_mod.ProviderRequest = ProviderRequest

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object
    class Star:
        def __init__(self, context):
            self.context = context
    star_mod.Star = Star

    _data_dir_state = {"dir": None}

    def _get_data_dir(name):
        d = _data_dir_state["dir"] / name if _data_dir_state["dir"] else Path.home() / ".stub" / name
        return d

    star_mod.StarTools = types.SimpleNamespace(get_data_dir=_get_data_dir)
    # 供测试把 StarTools.get_data_dir 重定向到 tempfile 目录
    star_mod._set_data_root = _data_dir_state.__setitem__
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

    # 4) 装饰器 stub 检查：main.py 被 @filter.xxx 装饰的方法数量 > 0，
    #    且不因 4.13 装饰器签名缺失而 import 失败（走到这里已说明 import 成功）。
    total_decorated = sum(len(v) for v in DECORATED.values())
    assert total_decorated > 0, "未发现任何 @filter.xxx 装饰的方法"
    assert len(DECORATED["command"]) > 0, "缺少命令装饰器"
    assert any(name == "lingshu" for name, _meta in DECORATED["command"]), "lingshu 命令未注册"
    assert any(name == "capture_turn" for name, _ in DECORATED["on_llm_response"]), \
        "on_llm_response 钩子未注册"
    # inject_memory 在本实现中无装饰器（由宿主 4.13 钩子约定调用），这里仅保证注册表可访问

    # 5) 完整实例化：模拟 4.13 宿主生命周期（__init__ → initialize → terminate）
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # 把 StarTools.get_data_dir 重定向到 tempfile 目录（config 已显式给 data_home，
        # 此处主要覆盖 data_home 缺省时的回落路径正确性）
        import sys as _sys
        _sys.modules["astrbot.api.star"]._set_data_root("dir", tmp_path)

        config = {
            "lingshu_path": str(REPO_ROOT),   # 仓库 scripts/brain.py 存在
            "data_home": str(tmp_path / "memory"),
            "timeout": 5,
        }
        plugin = mod.LingshuPlugin(context=None, config=config)
        assert plugin.data_home == (tmp_path / "memory").resolve(), "data_home 解析错误"
        assert plugin.brain.is_file(), "lingshu_path 应解析到仓库 scripts/brain.py"
        assert plugin._sensitive is not None, "敏感词正则未编译"
        assert plugin._sensitive.search("api key: abc"), "敏感词正则命中失败"
        assert plugin._capture_queue.maxlen == 500, "自动写回队列上限不符"
        assert plugin._closed is False, "__init__ 后 _closed 应为 False"

        # initialize：LINGSHU_PATH 指到仓库 scripts/，data_home 用 tempfile。
        # _run_cli_async 会真起子进程跑 brain.py；若失败允许 health 为"核心调用失败"，
        # 只断言不抛未捕获异常，且 _capture_worker 已启动。
        os.environ["LINGSHU_PATH"] = str(REPO_ROOT)
        try:
            asyncio.run(plugin.initialize())
        finally:
            os.environ.pop("LINGSHU_PATH", None)
        assert not (plugin.health.startswith("初始化失败")), \
            f"initialize 意外失败：{plugin.health}"
        assert plugin.health in ("核心与存储可用", "核心调用失败，请检查解释器和权限"), \
            f"health 状态不可识别：{plugin.health}"
        assert plugin._capture_worker_task is not None, "_capture_worker 未启动"

        # _capture_worker 队列语义：deque(maxlen=500) 满后丢最旧不报 AttributeError
        for i in range(520):
            plugin._capture_queue.append(("c", "u", "g", f"msg{i}"))
        assert len(plugin._capture_queue) == 500
        assert plugin._capture_queue[0][3] == "msg20", "队列满后应丢最旧"
        # 触发一次消费路径：手动调用 _capture_turn 的节流分支（auto_capture 关闭时
        # capture_turn 直接返回，这里直接往队列塞一项再人工 pop 模拟 worker 消费）
        item = plugin._capture_queue.popleft()
        assert item[0] == "c"

        # terminate：清队列、置 _closed、取消 worker
        asyncio.run(plugin.terminate())
        assert plugin._closed is True, "terminate 后 _closed 应为 True"
        assert len(plugin._capture_queue) == 0, "terminate 后队列应清空"
        assert plugin._capture_worker_task.cancelled() or plugin._capture_worker_task.done(), \
            "terminate 后 _capture_worker 应已停止"

    # 6) _ident：完整 event 返回 (channel, user, group)，缺字段抛 ValueError
    ev = mod.AstrMessageEvent(platform="aiocqhttp", sender="123", platform_id="bot1", group_id="g9")
    channel, user, group = plugin._ident(ev)
    assert channel == quote("aiocqhttp", safe="") + "@" + quote("bot1", safe=""), \
        f"channel 不符：{channel}"
    assert user.startswith(channel + ":"), "user 应以 channel 为前缀"
    assert group == "g9"
    ev_bad = mod.AstrMessageEvent(platform="", sender="123", platform_id="bot1")
    try:
        plugin._ident(ev_bad)
        raise AssertionError("缺 platform 时 _ident 应抛 ValueError")
    except ValueError:
        pass
    ev_bad2 = mod.AstrMessageEvent(platform="p", sender="", platform_id="bot1")
    try:
        plugin._ident(ev_bad2)
        raise AssertionError("缺 sender 时 _ident 应抛 ValueError")
    except ValueError:
        pass

    # 7) _validate_text：空 / 超长 / 横线开头 / 正常
    max_text = plugin.max_text
    for bad in ("", "   ", "-" + "x" * 5, "\x00abc"):
        try:
            plugin._validate_text(bad)
            raise AssertionError(f"_validate_text 应拒绝：{bad!r}")
        except ValueError:
            pass
    try:
        plugin._validate_text("长" * (max_text + 1))
        raise AssertionError("超长文本应抛 ValueError")
    except ValueError:
        pass
    assert plugin._validate_text("  正常内容  ") == "正常内容", "正常值应 strip 后原样返回"

    print("PASS: 插件冒烟测试通过（导入 / 队列语义 / 敏感词 / 边界工具 / "
          "4.13 生命周期 / 装饰器注册 / _ident / _validate_text / 捕获队列）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
