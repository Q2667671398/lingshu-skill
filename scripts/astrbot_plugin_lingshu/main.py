#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢记忆桥 —— AstrBot Star 插件。

通过 subprocess 进程隔离调用灵枢 CLI（scripts/brain.py），不 import 灵枢代码，
灵枢崩溃/升级都不影响 AstrBot 本体。设计要点：

  * 身份拼装：channel = event.get_platform_name()（aiocqhttp/telegram…），
    user = f"{channel}:{sender_id}"，与灵枢 owners/consent 白名单里的
    "qq:10001" 形式对齐；group_id 取 event.get_group_id()（私聊为空）。
  * 隐私转发：`lingshu 同意/拒绝` 以该用户身份执行 channel privacy grant/revoke
    （只能操作自己）；`lingshu 我的信息` 查自己；`lingshu 问 <user> …` 带
    --asker 走隐私门控，灵枢的拒绝提示（stderr，退出码 4）原样回复。
  * 主人命令：`lingshu 忘记 …` 等系统级命令仅当 sender 在本机 owners 白名单里
    （先用 `python brain.py channel owner add qq:xxx` 在本机加好）。这里插件侧
    也做一道本地校验（调 channel owner list 不涉及隐私），双保险。
  * 说明：本插件按 AstrBot 官方文档（docs.astrbot.app/dev/star/）编写：
    @register 装饰器、@filter.command 指令、AstrMessageEvent、
    event.unified_msg_origin、event.get_sender_id()、yield event.plain_result()。
    若你的 AstrBot 版本 API 有出入（如配置读取方式），以对应版本文档微调。

配置项（_conf_schema.json）：lingshu_path / python / timeout。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

# 退出码语义（与 brain.py CLI 约定一致）
EXIT_PRIVACY_DENIED = 4   # 隐私门控拒绝：第三人查未授权的人
EXIT_GATE_DENIED = 3      # owner 门控拒绝：非主人跑系统级命令


def _default_lingshu_path() -> str:
    """灵枢默认安装路径：~/.dsh/skills/lingshu（配置里可覆盖）。"""
    return os.path.join(os.path.expanduser("~"), ".dsh", "skills", "lingshu")


@star.register("astrbot_plugin_lingshu", "lingshu",
               "灵枢记忆桥：把群聊/私聊记忆接入 AstrBot，带群友隐私保护",
               "0.1.0", "https://github.com/lingshu-skill/lingshu-skill")
class LingshuPlugin(star.Star):

    def __init__(self, context: star.Context):
        super().__init__(context)
        # AstrBotConfig：AstrBot 会把 _conf_schema.json 定义的配置注入进来。
        # 按当前文档写法从 context / self.config 拿；若版本不同请微调这一段。
        cfg = getattr(self, "config", None) or {}
        self.lingshu_path = os.path.expanduser(
            str(cfg.get("lingshu_path") or _default_lingshu_path()))
        self.python = str(cfg.get("python") or sys.executable)
        self.timeout = int(cfg.get("timeout") or 15)
        self.brain = os.path.join(self.lingshu_path, "scripts", "brain.py")
        logger.info(f"[lingshu] 桥接就绪：{self.brain}")

    # ------------------------------------------------------------ 底层封装

    def _run_cli(self, *args: str) -> tuple:
        """同步跑一次灵枢 CLI，返回 (退出码, stdout, stderr)。

        subprocess 进程隔离：不 import 灵枢代码，环境变量也不泄漏；
        CLI 本身有 15s 内可完成的保证，阻塞时间可控。
        """
        cmd = [self.python, self.brain, *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=self.timeout)
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except FileNotFoundError:
            return 1, "", f"[lingshu] 找不到解释器或 brain.py：{cmd}"
        except subprocess.TimeoutExpired:
            return 1, "", f"[lingshu] 调用超时（>{self.timeout}s）"
        except Exception as exc:   # 绝不让插件因一次 CLI 失败而崩
            return 1, "", f"[lingshu] 调用失败：{exc}"

    async def _run_cli_async(self, *args: str) -> tuple:
        """在线程池里跑同步 subprocess，避免阻塞事件循环。"""
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._run_cli(*args))

    def _ident(self, event: AstrMessageEvent) -> tuple:
        """从事件提取 (channel, user, group_id)。私聊 group_id 为空字符串。"""
        channel = event.get_platform_name() or "unknown"
        sender_id = str(event.get_sender_id() or "")
        group_id = str(event.get_group_id() or "")
        return channel, f"{channel}:{sender_id}", group_id

    def _is_owner(self, user: str) -> bool:
        """本地校验调用者是否在灵枢 owners 白名单里（读 owner list，非隐私数据）。"""
        code, out, _ = self._run_cli("channel", "owner", "list")
        return code == 0 and user in out

    # ------------------------------------------------------------ 指令

    @filter.command_group("lingshu")
    def lingshu(self):
        """灵枢记忆桥：记住/找回/同意/拒绝/我的信息/问/忘记"""
        pass

    @lingshu.command("记住")
    async def remember(self, event: AstrMessageEvent, *, text: str = ""):
        """lingshu 记住 <内容>：以说话人身份写入灵枢（带通道三元组）"""
        if not text:
            yield event.plain_result("用法：lingshu 记住 <内容>")
            return
        channel, user, group_id = self._ident(event)
        code, out, err = await self._run_cli_async(
            "channel", "remember", text,
            "--channel", channel, "--group", group_id, "--user", user)
        yield event.plain_result(out if code == 0 else (err or f"写入失败（退出码 {code}）"))

    @lingshu.command("找回")
    async def recall(self, event: AstrMessageEvent, *, query: str = ""):
        """lingshu 找回 <关键词>：按群隔离规则召回记忆"""
        if not query:
            yield event.plain_result("用法：lingshu 找回 <关键词>")
            return
        channel, user, group_id = self._ident(event)
        args = ["channel", "recall", query, "--channel", channel,
                "--group", group_id, "--user", user, "--k", "5"]
        code, out, err = await self._run_cli_async(*args)
        if code == EXIT_PRIVACY_DENIED:
            yield event.plain_result(err)   # 灵枢的拒绝提示原样转发
        else:
            yield event.plain_result(out if code == 0 else (err or f"召回失败（退出码 {code}）"))

    # ---- 群友隐私：只能操作自己的授权 ----

    @lingshu.command("同意")
    async def consent_grant(self, event: AstrMessageEvent):
        """lingshu 同意：授权 bot 对外分享关于我的记忆（只能本人操作）"""
        _c, user, _g = self._ident(event)
        code, out, err = await self._run_cli_async(
            "channel", "privacy", "grant", "--user", user)
        yield event.plain_result(out if code == 0 else (err or f"授权失败（退出码 {code}）"))

    @lingshu.command("拒绝")
    async def consent_revoke(self, event: AstrMessageEvent):
        """lingshu 拒绝：撤回「对外分享关于我的记忆」的授权"""
        _c, user, _g = self._ident(event)
        code, out, err = await self._run_cli_async(
            "channel", "privacy", "revoke", "--user", user)
        yield event.plain_result(out if code == 0 else (err or f"撤回失败（退出码 {code}）"))

    @lingshu.command("我的信息")
    async def my_profile(self, event: AstrMessageEvent):
        """lingshu 我的信息：查看关于我自己的全部记忆（本人查自己，全量）"""
        _c, user, _g = self._ident(event)
        code, out, err = await self._run_cli_async(
            "channel", "user", user, "--asker", user)
        yield event.plain_result(out if code == 0 else (err or f"查询失败（退出码 {code}）"))

    # ---- 第三人打听：走隐私门控，拒绝/放行原样转发 ----

    @lingshu.command("问")
    async def ask_about(self, event: AstrMessageEvent, target_user: str = "", *,
                        query: str = ""):
        """lingshu 问 <user，如 qq:20002> <问题>：查关于某人的记忆（需对方同意或本人）"""
        if not target_user or not query:
            yield event.plain_result("用法：lingshu 问 <user> <问题>，如：lingshu 问 qq:20002 他喜欢什么")
            return
        _c, asker, _g = self._ident(event)
        code, out, err = await self._run_cli_async(
            "channel", "user", target_user, "--asker", asker, "--k", "5")
        if code == EXIT_PRIVACY_DENIED:
            yield event.plain_result(err)   # 「关于 xx 的记忆需要 xx 本人同意或本人查询」
        elif code == 0:
            yield event.plain_result(out if out else f"灵枢里还没有关于 {target_user} 的记忆。")
        else:
            yield event.plain_result(err or f"查询失败（退出码 {code}）")

    # ---- 主人命令：仅 owners 白名单可执行 ----

    @lingshu.command("忘记")
    async def owner_forget(self, event: AstrMessageEvent, memory_id: str = ""):
        """lingshu 忘记 <记忆id>：删除记忆（仅主人）"""
        _c, user, _g = self._ident(event)
        if not self._is_owner(user):
            yield event.plain_result("[拒绝] 忘记是系统级命令，仅主人（owners 白名单）可用。")
            return
        code, out, err = await self._run_cli_async("forget", memory_id)
        yield event.plain_result(out if code == 0 else (err or f"删除失败（退出码 {code}）"))

    @lingshu.command("做梦")
    async def owner_dream(self, event: AstrMessageEvent):
        """lingshu 做梦：触发记忆巩固（仅主人）"""
        _c, user, _g = self._ident(event)
        if not self._is_owner(user):
            yield event.plain_result("[拒绝] 做梦是系统级命令，仅主人（owners 白名单）可用。")
            return
        code, out, err = await self._run_cli_async("dream")
        yield event.plain_result(out if code == 0 else (err or f"做梦失败（退出码 {code}）"))

    async def terminate(self):
        """插件卸载/停用时调用（无持久资源需清理）。"""
