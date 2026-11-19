# -*- coding: utf-8 -*-
"""lingshu-guard（群聊发言人鉴权插件）回归测试。

背景（P0 安全闭环）：dsh-im 把 QQ/微信等群聊接进 DSH 后，群友也能让模型调工具；
不鉴权等于把 shell / 写文件 / subagent 交给任何群成员。本插件按「发言人身份」在
工具执行前拦截：owner 全放行、其他人只允许只读工具、没有 IM 绑定的桌面会话不受
影响、拿不到发言人身份时 fail-closed。

测试分两层：
  1. 静态契约：两个 JS 文件的接线、默认值、拒绝路径关键字（始终运行）；
  2. 运行时行为：用 node 子进程 + 假 cordis ctx 真实加载插件，驱动
     agent/created → tools.guard / tools/pre-execute，验证放行与拒绝；
     没有 node 时只跳过运行时项（静态契约仍在）。

运行：python tests/test_lingshu_guard.py   （退出码 0 = 全过）
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ 公共测试工具（瞬时 IO 重试）
from io_retry_util import run_with_io_retry  # noqa: E402
GUARD = ROOT / "cordis-plugin" / "lingshu-guard" / "plugin.js"
INDEX = ROOT / "index.js"

# 与 plugin.js 的身份编码（channel=platform@botId，user=channel:encoded sender）一致。
OWNER_USER = "qq@bot1:OWNER123"
STRANGER_USER = "qq@bot1:STRANGER"

_HARNESS = None
_HARNESS_DIR = None


def find_node():
    """找 node：LINGSHU_NODE > PATH > DSH 自带运行时。找不到返回 None。"""
    candidates = []
    env_node = os.environ.get("LINGSHU_NODE", "").strip()
    if env_node:
        candidates.append(env_node)
    which = shutil.which("node")
    if which:
        candidates.append(which)
    homes = [
        os.environ.get("DSH_HOME", "").strip(),
        str(Path.home() / ".dsh"),
    ]
    for home in homes:
        if not home:
            continue
        candidates.append(str(Path(home) / "dsh-runtimes" / "dsh-primary-runtime"
                              / "dependencies" / "node" / "bin" / "node.exe"))
    for cand in candidates:
        if cand and Path(cand).is_file():
            return cand
    return None


def _cleanup():
    global _HARNESS_DIR
    if _HARNESS_DIR:
        shutil.rmtree(_HARNESS_DIR, ignore_errors=True)
        _HARNESS_DIR = None


atexit.register(_cleanup)


HARNESS_JS = r"""
// 由 tests/test_lingshu_guard.py 写入临时目录后执行；不是仓库文件。
// 用假 cordis ctx 真实加载 plugin.js，驱动 agent/created 注册守卫，再逐条调用守卫。
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const pluginPath = process.env.LINGSHU_GUARD_PLUGIN
const root = process.env.LINGSHU_GUARD_TMP
const plugin = await import(pathToFileURL(pluginPath).href)

const OWNER_SENDER = 'OWNER123'
const STRANGER_SENDER = 'STRANGER'
const OWNER = 'qq@bot1:OWNER123'

const results = []
const warnings = []

function scenarioDir(name) {
  const dir = join(root, name)
  mkdirSync(dir, { recursive: true })
  return dir
}

function writeConfig(dir, data) {
  writeFileSync(join(dir, 'config.json'), JSON.stringify(data), 'utf8')
}

function useHome(dir) {
  process.env.BRAIN_MEMORY_HOME = dir
}

function describe({ senderId = null, channel = 'qq', botId = 'bot1', chatType = 'group', chatId = 'g1' } = {}) {
  return {
    channel,
    botId,
    conversationKey: `${channel}:group:${chatId}`,
    chatType,
    chatId,
    threadId: null,
    senderId,
    senderName: null,
  }
}

// 假 cordis 世界：ctx 上有 on/get/logger；每个 agent 有自己的 agent.ctx，
// 通过 agent.ctx.get('tools') 暴露 guard（或没有 guard 的旧宿主）。
function makeWorld({ dshIm = null } = {}) {
  const handlers = new Map()
  const ctx = {
    logger: {
      warn: (...args) => { warnings.push('warn ' + args.map(String).join(' ')) },
      error: (...args) => { warnings.push('error ' + args.map(String).join(' ')) },
      info: () => {},
    },
    on(name, fn) {
      if (!handlers.has(name)) handlers.set(name, [])
      handlers.get(name).push(fn)
      return () => {}
    },
    get(name) {
      return name === 'dshIm' ? dshIm : undefined
    },
    emit() {},
  }
  const emit = (name, payload) => {
    for (const fn of handlers.get(name) || []) fn(payload)
  }
  function agent(sessionId, { guardApi = true } = {}) {
    const guards = []
    const preExecute = []
    const tools = {}
    if (guardApi) tools.guard = (fn) => { guards.push(fn); return () => {} }
    const agentCtx = {
      on(name, fn) {
        if (name === 'tools/pre-execute') preExecute.push(fn)
        return () => {}
      },
      get(name) {
        if (name === 'tools') return tools
        if (name === 'dshIm') return dshIm
        return undefined
      },
    }
    const created = { id: sessionId, session: { id: sessionId }, ctx: agentCtx }
    return { agent: created, guards, preExecute }
  }
  return { ctx, emit, agent }
}

function mount(world, config = {}) {
  plugin.apply(world.ctx, config)
  return world
}

function record(name, payload) {
  results.push({ name, ...payload })
}

// ---- 1) 非 owner：pwsh 拒；read / grep 只读放行（shadow=false，自定义日志路径）----
{
  const home = scenarioDir('non-owner')
  const logPath = join(home, 'custom-guard.log')
  writeConfig(home, { owners: [OWNER], guard: { shadow: false, logPath } })
  useHome(home)
  const world = makeWorld({
    dshIm: {
      describeConversationSync: (sid) => (sid === 's-nonowner' ? describe({ senderId: STRANGER_SENDER }) : null),
    },
  })
  mount(world)
  const { agent, guards } = world.agent('s-nonowner')
  world.emit('agent/created', { agent })
  const guard = guards[0]
  record('non-owner', {
    guardCount: guards.length,
    pwsh: guard ? guard({ name: 'pwsh', arguments: { command: 'whoami' }, agent }) : null,
    read: guard ? guard({ name: 'read', arguments: { file_path: 'x' }, agent }) : null,
    grep: guard ? guard({ name: 'grep', arguments: { pattern: 'x' }, agent }) : null,
    logPath,
  })
}

// ---- 2) owner：全部工具放行（guard 段没配 owners → 回退同文件顶层 owners）----
{
  const home = scenarioDir('owner')
  writeConfig(home, { owners: [OWNER], guard: { shadow: false } })
  useHome(home)
  const world = makeWorld({
    dshIm: {
      describeConversationSync: (sid) => (sid === 's-owner' ? describe({ senderId: OWNER_SENDER }) : null),
    },
  })
  mount(world)
  const { agent, guards } = world.agent('s-owner')
  world.emit('agent/created', { agent })
  const guard = guards[0]
  record('owner', {
    guardCount: guards.length,
    pwsh: guard ? guard({ name: 'pwsh', arguments: { command: 'whoami' }, agent }) : null,
    write: guard ? guard({ name: 'write', arguments: { file_path: 'x' }, agent }) : null,
    logPath: join(home, 'lingshu-guard.log'),
  })
}

// ---- 3) 事件缓存：没有 dshIm 服务时靠 dsh-im/session-pre-bind 里的 senderId ----
{
  const home = scenarioDir('binding-cache')
  writeConfig(home, { owners: [OWNER], guard: { shadow: false } })
  useHome(home)
  const world = makeWorld({ dshIm: null })
  mount(world)
  world.emit('dsh-im/session-pre-bind', {
    sessionId: 's-cache-stranger', channel: 'qq', botId: 'bot1',
    conversationKey: 'qq:group:g2', chatType: 'group', chatId: 'g2', threadId: null,
    senderId: STRANGER_SENDER, senderName: '路人',
  })
  world.emit('dsh-im/session-pre-bind', {
    sessionId: 's-cache-owner', channel: 'qq', botId: 'bot1',
    conversationKey: 'qq:group:g2', chatType: 'group', chatId: 'g2', threadId: null,
    senderId: OWNER_SENDER, senderName: '主人',
  })
  const stranger = world.agent('s-cache-stranger')
  const owner = world.agent('s-cache-owner')
  world.emit('agent/created', { agent: stranger.agent })
  world.emit('agent/created', { agent: owner.agent })
  record('binding-cache', {
    strangerGuardCount: stranger.guards.length,
    ownerGuardCount: owner.guards.length,
    strangerDenied: stranger.guards[0]
      ? stranger.guards[0]({ name: 'pwsh', arguments: {}, agent: stranger.agent })
      : null,
    ownerAllowed: owner.guards[0]
      ? owner.guards[0]({ name: 'pwsh', arguments: {}, agent: owner.agent })
      : null,
  })
}

// ---- 4) 桌面：无 IM 绑定 → 默认 enabled=true 也不误伤，且不写日志 ----
{
  const home = scenarioDir('desktop')
  writeConfig(home, { owners: [OWNER] })
  useHome(home)
  const world = makeWorld({ dshIm: { describeConversationSync: () => null } })
  mount(world)
  const { agent, guards } = world.agent('s-desktop')
  world.emit('agent/created', { agent })
  const guard = guards[0]
  record('desktop', {
    guardCount: guards.length,
    pwsh: guard ? guard({ name: 'pwsh', arguments: { command: 'dir' }, agent }) : null,
    write: guard ? guard({ name: 'write', arguments: {}, agent }) : null,
    logExists: existsSync(join(home, 'lingshu-guard.log')),
  })
}

// ---- 5) shadow（默认 true）：只记 shadow-deny，不拦 ----
{
  const home = scenarioDir('shadow')
  writeConfig(home, { owners: [OWNER] })
  useHome(home)
  const world = makeWorld({
    dshIm: {
      describeConversationSync: (sid) => (sid === 's-shadow' ? describe({ senderId: STRANGER_SENDER }) : null),
    },
  })
  mount(world)
  const { agent, guards } = world.agent('s-shadow')
  world.emit('agent/created', { agent })
  const guard = guards[0]
  record('shadow', {
    guardCount: guards.length,
    pwsh: guard ? guard({ name: 'pwsh', arguments: {}, agent }) : null,
    logPath: join(home, 'lingshu-guard.log'),
  })
}

// ---- 6) 有 IM 绑定但拿不到 senderId → fail-closed（只读工具也拒）----
{
  const home = scenarioDir('fail-closed')
  writeConfig(home, { owners: [OWNER], guard: { shadow: false } })
  useHome(home)
  const world = makeWorld({
    dshIm: {
      describeConversationSync: (sid) => (sid === 's-noid' ? describe({ senderId: null, chatId: 'g9' }) : null),
    },
  })
  mount(world)
  const { agent, guards } = world.agent('s-noid')
  world.emit('agent/created', { agent })
  const guard = guards[0]
  record('fail-closed', {
    guardCount: guards.length,
    pwsh: guard ? guard({ name: 'pwsh', arguments: {}, agent }) : null,
    read: guard ? guard({ name: 'read', arguments: {}, agent }) : null,
    logPath: join(home, 'lingshu-guard.log'),
  })
}

// ---- 7) enabled=false：整体放行且不写日志 ----
{
  const home = scenarioDir('disabled')
  writeConfig(home, { owners: [OWNER], guard: { enabled: false, shadow: false } })
  useHome(home)
  const world = makeWorld({
    dshIm: {
      describeConversationSync: (sid) => (sid === 's-off' ? describe({ senderId: STRANGER_SENDER }) : null),
    },
  })
  mount(world)
  const { agent, guards } = world.agent('s-off')
  world.emit('agent/created', { agent })
  const guard = guards[0]
  record('disabled', {
    guardCount: guards.length,
    pwsh: guard ? guard({ name: 'pwsh', arguments: {}, agent }) : null,
    logExists: existsSync(join(home, 'lingshu-guard.log')),
  })
}

// ---- 8) 旧宿主没有 tools.guard → 退回 tools/pre-execute（waterfall）----
{
  const home = scenarioDir('fallback')
  writeConfig(home, { owners: [OWNER], guard: { shadow: false } })
  useHome(home)
  const world = makeWorld({
    dshIm: {
      describeConversationSync: (sid) => (sid === 's-fallback' ? describe({ senderId: STRANGER_SENDER }) : null),
    },
  })
  mount(world)
  const { agent, guards, preExecute } = world.agent('s-fallback', { guardApi: false })
  world.emit('agent/created', { agent })
  let nextCount = 0
  const next = async () => { nextCount += 1; return { kind: 'allow' } }
  const handler = preExecute[0]
  const deny = handler ? await handler({ name: 'pwsh', arguments: {}, agent }, next) : null
  const allow = handler ? await handler({ name: 'read', arguments: {}, agent }, next) : null
  record('fallback', {
    guardCount: guards.length,
    preCount: preExecute.length,
    deny,
    allow,
    nextCount,
    logPath: join(home, 'lingshu-guard.log'),
  })
}

// JSON.stringify 会丢掉值为 undefined 的键；统一转成 null，Python 侧才能区分
// 「放行（null）」与「没跑到（键缺失）」。
console.log(JSON.stringify(
  { node: process.version, results, warnings },
  (key, value) => (value === undefined ? null : value),
))
"""


def run_harness():
    """运行一次 node harness，返回 {结果名: 结果}；无 node 时返回 None。"""
    global _HARNESS, _HARNESS_DIR
    if _HARNESS is not None:
        return _HARNESS
    node = find_node()
    if not node:
        _HARNESS = {"skipped": True}
        return _HARNESS
    tmpdir = tempfile.mkdtemp(prefix="lingshu-guard-test-")
    _HARNESS_DIR = tmpdir
    harness = Path(tmpdir) / "harness.mjs"
    harness.write_text(HARNESS_JS, encoding="utf-8")
    env = dict(os.environ)
    env["LINGSHU_GUARD_PLUGIN"] = str(GUARD)
    env["LINGSHU_GUARD_TMP"] = tmpdir
    proc = subprocess.run(
        [node, str(harness)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=180,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "node harness 失败（rc=%s）\nstdout:\n%s\nstderr:\n%s"
            % (proc.returncode, proc.stdout[-4000:], proc.stderr[-4000:])
        )
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    if not lines:
        raise AssertionError("node harness 没有输出\nstderr:\n%s" % proc.stderr[-4000:])
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise AssertionError("node harness 输出不是 JSON：%s\n%s" % (exc, lines[-1][:2000]))
    results = {item["name"]: item for item in data.get("results", [])}
    _HARNESS = {"skipped": False, "results": results, "warnings": data.get("warnings", []),
                "node": data.get("node"), "nodePath": node}
    return _HARNESS


def harness_results():
    state = run_harness()
    if state is None or state.get("skipped"):
        print("SKIP: 没有找到 node，跳过运行时行为检查（静态契约仍然生效）")
        return None
    return state["results"]


def read_log(path):
    """读 JSONL 鉴权日志；文件不存在返回 []。"""
    file = Path(path)
    if not file.exists():
        return []
    entries = []
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


# ---------------------------------------------------------------- 静态契约


def test_static_contract():
    assert GUARD.is_file(), f"缺少插件文件：{GUARD}"
    src = GUARD.read_text(encoding="utf-8")
    for needle in (
        "export function apply",
        "agent/created",
        "tools/pre-execute",
        "tools.guard(",
        "describeConversationSync",
        "dsh-im/session-pre-bind",
        "dsh-im/session-unbind",
        "shadow-deny",
        "readOnlyAllow",
        "fail-closed",
        "ToolGuard",
    ):
        assert needle in src, f"lingshu-guard 缺少关键实现「{needle}」"
    for tool in ("read", "glob", "grep", "web_search",
                 "list_agents", "skill", "todo_write", "ask_user_question"):
        assert f"'{tool}'" in src, f"默认只读白名单缺少 {tool}"
    for tool in ("'pwsh'", "'write'", "'subagent'"):
        assert tool not in src, f"只读白名单/策略里不该出现有副作用工具 {tool}"
    assert "enabled: true" in src, "guard 默认应 enabled=true"
    assert "shadow: true" in src, "guard 默认应 shadow=true（先观察再拦）"

    index_src = INDEX.read_text(encoding="utf-8")
    assert "import * as guardPlugin from './cordis-plugin/lingshu-guard/plugin.js'" in index_src, \
        "index.js 没有 import lingshu-guard"
    assert "guardPlugin.apply(ctx, cfg.guard || {})" in index_src, \
        "index.js 没有挂载 guardPlugin（且应透传 cfg.guard）"

    node = find_node()
    if not node:
        print("SKIP: 没有 node，跳过 node --check")
        return
    for file in (GUARD, INDEX):
        proc = subprocess.run(
            [node, "--check", str(file)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        assert proc.returncode == 0, f"node --check {file} 失败：{proc.stderr}"


# ---------------------------------------------------------------- 运行时行为


def test_non_owner_denied_and_readonly_allowed():
    results = harness_results()
    if results is None:
        return
    item = results["non-owner"]
    assert item["guardCount"] == 1, "agent/created 后没有注册工具守卫"
    reason = item["pwsh"]
    assert isinstance(reason, str) and reason, f"非 owner 调 pwsh 没有被拒：{reason!r}"
    assert "拒绝" in reason and "pwsh" in reason, f"拒绝原因不明确：{reason}"
    assert "owner" in reason.lower(), f"拒绝原因应说明 owner 白名单：{reason}"
    assert item["read"] is None, f"只读工具 read 被误拦：{item['read']!r}"
    assert item["grep"] is None, f"只读工具 grep 被误拦：{item['grep']!r}"

    entries = read_log(item["logPath"])
    assert entries, "自定义 logPath 没有写入鉴权日志"
    for entry in entries:
        assert set(entry) == {"ts", "action", "tool", "sender", "channel", "group"}, \
            f"日志字段不符合 JSONL 契约：{entry}"
    denies = [e for e in entries if e["action"] == "deny"]
    pwsh_deny = next((e for e in denies if e["tool"] == "pwsh"), None)
    assert pwsh_deny is not None, f"日志里没有 pwsh 的 deny 记录：{entries}"
    assert pwsh_deny["sender"] == STRANGER_USER, pwsh_deny
    assert pwsh_deny["channel"] == "qq@bot1", pwsh_deny
    assert pwsh_deny["group"] == "g1", pwsh_deny
    assert any(e["action"] == "allow" and e["tool"] in ("read", "grep") for e in entries), \
        f"只读工具放行没有记 allow：{entries}"


def test_owner_allowed_and_event_cache():
    results = harness_results()
    if results is None:
        return
    item = results["owner"]
    assert item["guardCount"] == 1, "owner 会话没有注册守卫"
    assert item["pwsh"] is None, f"owner 调 pwsh 被误拦：{item['pwsh']!r}"
    assert item["write"] is None, f"owner 调 write 被误拦：{item['write']!r}"
    entries = read_log(item["logPath"])
    allow_pwsh = [e for e in entries if e["action"] == "allow" and e["tool"] == "pwsh"]
    assert allow_pwsh, f"owner 放行没有记 allow：{entries}"
    assert allow_pwsh[0]["sender"] == OWNER_USER, allow_pwsh[0]

    cache = results["binding-cache"]
    assert cache["strangerGuardCount"] == 1 and cache["ownerGuardCount"] == 1
    assert isinstance(cache["strangerDenied"], str) and "拒绝" in cache["strangerDenied"], \
        f"仅靠 session-pre-bind 缓存的非 owner 没有被拒：{cache['strangerDenied']!r}"
    assert cache["ownerAllowed"] is None, \
        f"仅靠 session-pre-bind 缓存的 owner 被误拦：{cache['ownerAllowed']!r}"


def test_desktop_and_disabled_unaffected():
    results = harness_results()
    if results is None:
        return
    desktop = results["desktop"]
    assert desktop["guardCount"] == 1, "桌面 agent 也应注册守卫（身份解析后放行）"
    assert desktop["pwsh"] is None, f"桌面会话调 pwsh 被误伤：{desktop['pwsh']!r}"
    assert desktop["write"] is None, f"桌面会话调 write 被误伤：{desktop['write']!r}"
    assert desktop["logExists"] is False, "桌面会话不该产生鉴权日志"

    disabled = results["disabled"]
    assert disabled["pwsh"] is None, f"enabled=false 时仍拦截：{disabled['pwsh']!r}"
    assert disabled["logExists"] is False, "enabled=false 时不该写日志"


def test_shadow_only_logs():
    results = harness_results()
    if results is None:
        return
    item = results["shadow"]
    assert item["pwsh"] is None, f"shadow=true 不应拦截，却返回：{item['pwsh']!r}"
    entries = read_log(item["logPath"])
    assert entries, "shadow 模式没有写日志"
    assert all(e["action"] == "shadow-deny" for e in entries), f"shadow 模式不应出现真 deny：{entries}"
    entry = next(e for e in entries if e["tool"] == "pwsh")
    assert entry["sender"] == STRANGER_USER, entry
    assert entry["channel"] == "qq@bot1" and entry["group"] == "g1", entry
    assert isinstance(entry["ts"], str) and entry["ts"], entry


def test_fail_closed_and_pre_execute_fallback():
    results = harness_results()
    if results is None:
        return
    fail_closed = results["fail-closed"]
    assert isinstance(fail_closed["pwsh"], str) and fail_closed["pwsh"], \
        "拿不到 senderId 时没有 fail-closed 拒绝 pwsh"
    assert isinstance(fail_closed["read"], str) and fail_closed["read"], \
        "拿不到发言人身份时只读工具也必须 fail-closed"
    assert "fail-closed" in fail_closed["pwsh"].lower(), fail_closed["pwsh"]
    entries = read_log(fail_closed["logPath"])
    assert [e for e in entries if e["action"] == "deny" and e["tool"] == "pwsh"], entries

    fallback = results["fallback"]
    assert fallback["guardCount"] == 0, "旧宿主不该调用不存在的 tools.guard"
    assert fallback["preCount"] == 1, "旧宿主没有退回 tools/pre-execute"
    deny = fallback["deny"]
    assert isinstance(deny, dict) and deny.get("kind") == "deny", f"pre-execute 没返回 deny：{deny!r}"
    assert "pwsh" in deny.get("reason", "") and "拒绝" in deny.get("reason", ""), deny
    allow = fallback["allow"]
    assert isinstance(allow, dict) and allow.get("kind") == "allow", f"只读工具被 pre-execute 误拦：{allow!r}"
    assert fallback["nextCount"] == 1, "拒绝时不该继续调用 next()，放行时应调用一次"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            run_with_io_retry(fn)
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
