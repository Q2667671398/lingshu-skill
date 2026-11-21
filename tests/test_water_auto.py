# -*- coding: utf-8 -*-
"""v3.44 水位自动执行（影子模式 + 显式同意）回归测试。

背景：75% 该做梦、90% 该封存——这两个动作过去只靠"模型自觉"。现在 cron 插件
可在水位首次跨档时自动执行，但隐私必须默认安全：

  * 配置在 ~/.brain-memory/config.json 的 water 段，缺省全关
    （autoDream/autoDreamConsent/autoCapture/autoCaptureConsent=false，shadow=true）；
  * shadow=true 只记"本来会做什么"，绝不真执行；
  * 真执行必须 autoXxx && autoXxxConsent 同时为真，且 shadow=false；
  * 90% 的 capture 永不自动执行——capture 需要会话原文，插件拿不到正文、
    绝不自己拼，只记一条待人工确认的逃生建议并提醒一次。

测试分两层：
  1. 静态契约：plugin.js 的接线/关键字 + node --check（始终运行）；
  2. 运行时行为：node 子进程 + 假 cordis ctx 真实加载插件，用假 brain.py
     （其实是 node 脚本，经 runBrain 真起子进程）spy 住所有后台调用，
     断言「调了/没调」与 water-auto.jsonl 台账内容。
     没有 node 时只跳过运行时项（静态契约仍在）。

运行：python tests/test_water_auto.py   （退出码 0 = 全过）
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ 公共测试工具（瞬时 IO 重试）
from io_retry_util import run_with_io_retry  # noqa: E402

CRON = ROOT / "cordis-plugin" / "lingshu-cron" / "plugin.js"
LOG_NAME = "water-auto.jsonl"
MAX_BYTES = 512 * 1024

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


FAKE_BRAIN_JS = r"""
// 由 tests/test_water_auto.py 写入临时目录后执行；不是仓库文件。
// 假 brain.py：被 runBrain 真起子进程调用，把 argv 追加进 marker，spy 住所有后台调用。
import { appendFileSync } from 'node:fs'

const marker = process.env.LINGSHU_WATER_MARKER
const argv = process.argv.slice(2) // [node, 本文件, ...命令参数] → 只剩命令参数
if (marker) {
  try { appendFileSync(marker, JSON.stringify(argv) + '\n', 'utf8') } catch {}
}
if (argv[0] === 'check-context') {
  console.log(`[fake check-context] used=${argv[2] || ''}`)
} else if (argv[0] === 'dream') {
  console.log('[fake dream] ok')
} else if (argv[0] === 'capture') {
  // 真被自动调用就留下证据；本测试断言它永远不出现
  console.log('[fake capture] executed')
} else {
  console.log(`[fake] ${argv.join(' ')}`)
}
process.exit(0)
"""


HARNESS_JS = r"""
// 由 tests/test_water_auto.py 写入临时目录后执行；不是仓库文件。
// 用假 cordis ctx 真实加载 plugin.js，逐个场景驱动 agent/pre-step 到指定水位，
// 再用 marker（假 brain.py 的调用记录）与 water-auto.jsonl 台账验证行为。
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const pluginPath = process.env.LINGSHU_CRON_PLUGIN
const root = process.env.LINGSHU_CRON_TMP
const nodeExe = process.env.LINGSHU_CRON_NODE
const fakeBrain = process.env.LINGSHU_CRON_FAKE_BRAIN
const plugin = await import(pathToFileURL(pluginPath).href)

const results = {}
const warnings = []
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function scenarioDir(name) {
  const dir = join(root, name)
  const app = join(dir, 'app')
  const home = join(dir, 'home')
  mkdirSync(app, { recursive: true })
  mkdirSync(home, { recursive: true })
  return {
    dir,
    app,
    home,
    marker: join(dir, 'calls.jsonl'),
    log: join(home, 'water-auto.jsonl'),
    rotated: join(home, 'water-auto.jsonl.1'),
  }
}

function writeConfig(sc, data) {
  writeFileSync(join(sc.home, 'config.json'), JSON.stringify(data), 'utf8')
}

function readJsonl(file) {
  if (!existsSync(file)) return []
  return readFileSync(file, 'utf8')
    .split('\n')
    .filter((line) => line.trim())
    .map((line) => {
      try { return JSON.parse(line) } catch { return { __bad: line } }
    })
}

/** marker 里每行是一条后台命令的 argv；只取合法数组行。 */
function readCalls(sc) {
  return readJsonl(sc.marker).filter((item) => Array.isArray(item))
}

/** 假 cordis 世界：ctx 只需 on/get/logger。 */
function makeWorld() {
  const handlers = new Map()
  const ctx = {
    logger: {
      warn: (...args) => { warnings.push('warn ' + args.map(String).join(' ')) },
      error: (...args) => { warnings.push('error ' + args.map(String).join(' ')) },
      info: () => {},
      debug: () => {},
    },
    on(name, fn) {
      if (!handlers.has(name)) handlers.set(name, [])
      handlers.get(name).push(fn)
      return () => {}
    },
    get() { return undefined },
  }
  return {
    ctx,
    emit(name, payload) {
      for (const fn of handlers.get(name) || []) fn(payload)
    },
    handler(name) {
      const list = handlers.get(name) || []
      return list.length ? list[list.length - 1] : null
    },
  }
}

// v3.59：注入消息的 source.name 按子插件分开（startup/cron/im 各不相同）。
// 断言"插件注入了什么"时不能再只认 'lingshu'——那是 v3.58 及以前的统一名字，
// 正因为两个子插件共用它才导致 cron 的 prune 会删掉 startup 的记忆块。
const LINGSHU_SOURCES = new Set(['lingshu', 'lingshu-cron', 'lingshu-startup', 'lingshu-im'])

// 与 cron 插件 DEFAULTS 对齐的最小配置：contextLimit=100000 → totalTokens=pct*1000 就是 pct%
const BASE_CONFIG = {
  contextLimit: 100000,
  waterGuard: true,
  waterCheckTurns: 0,
  injectReview: false,
  runUsage: false,
  pruneInjected: false,
  extractTurns: 0,
  timeoutMs: 30000,
  brainPy: fakeBrain,
  python: nodeExe,
}

function mount(sc, extra = {}) {
  process.env.BRAIN_MEMORY_HOME = sc.home
  process.env.APP_DSH = sc.app
  process.env.LINGSHU_WATER_MARKER = sc.marker
  globalThis.__lingshuCronApplied = false // 单进程多场景：清掉重复挂载守卫
  const world = makeWorld()
  plugin.apply(world.ctx, { ...BASE_CONFIG, ...extra })
  return world
}

function makeAgent(id) {
  return { id, session: { id }, ctx: null }
}

function messagesFor(pct) {
  return [{
    id: `m-${pct}`,
    role: 'user',
    content: [{ type: 'text', text: `上下文推进到 ${pct}% 时的会话内容` }],
    metadata: { usage: { totalTokens: pct * 1000 } },
  }]
}

/** 驱动一次 pre-step 到指定水位；返回插件注入的提醒文本（source.name 属灵枢家族）。 */
async function preStep(world, agent, pct) {
  const handler = world.handler('agent/pre-step')
  if (!handler) throw new Error('agent/pre-step handler 未注册')
  const decision = await handler({ agent }, async () => ({ kind: 'enter', messages: messagesFor(pct) }))
  const injected = ((decision && decision.messages) || [])
    .filter((m) => m && m.source && LINGSHU_SOURCES.has(m.source.name))
    .map((m) => (m.content || []).filter((c) => c && c.type === 'text').map((c) => c.text).join(''))
  return { decision, injected }
}

async function waitFor(fn, timeout = 15000) {
  const t0 = Date.now()
  while (Date.now() - t0 < timeout) {
    if (fn()) return true
    await sleep(100)
  }
  return false
}

const CALLS_FORBIDDEN = (calls) => calls.filter((argv) => argv[0] === 'dream' || argv[0] === 'capture')

// ---- 1) 默认全关：没有 water 段 → 不执行、不写盘（既有水位提醒不受影响）----
{
  const sc = scenarioDir('default-off')
  const world = mount(sc) // 不写 config.json：water 缺失 = 默认全关
  const ag = makeAgent('s-default-off')
  const r75 = await preStep(world, ag, 75)
  const r90 = await preStep(world, ag, 90)
  await sleep(1200) // 给可能的后台误投递留出时间
  results['default-off'] = {
    calls: readCalls(sc),
    forbidden: CALLS_FORBIDDEN(readCalls(sc)),
    logExists: existsSync(sc.log),
    rotatedExists: existsSync(sc.rotated),
    injected75: r75.injected,
    injected90: r90.injected,
  }
}

// ---- 2) shadow=true：只记"本来会做什么"，executed=false，且台账 512KB 轮转 ----
{
  const sc = scenarioDir('shadow-75')
  writeConfig(sc, { water: { autoDream: true, autoDreamConsent: true, shadow: true } })
  writeFileSync(sc.log, '{"seed":true}\n'.repeat(60000), 'utf8') // ~840KB，触发轮转
  const sizeBefore = statSync(sc.log).size
  const world = mount(sc)
  const ag = makeAgent('s-shadow-75')
  const r1 = await preStep(world, ag, 75)
  const r2 = await preStep(world, ag, 75) // 已跨过 75%：不应重复记账
  await sleep(1200)
  results['shadow-75'] = {
    sizeBefore,
    entries: readJsonl(sc.log),
    rotatedExists: existsSync(sc.rotated),
    rotatedSize: existsSync(sc.rotated) ? statSync(sc.rotated).size : 0,
    calls: readCalls(sc),
    forbidden: CALLS_FORBIDDEN(readCalls(sc)),
    injected1: r1.injected,
    injected2: r2.injected,
  }
}

// ---- 3) 双开关 true 且 shadow=false：75% 真触发 dream --auto（后台子进程 spy）----
{
  const sc = scenarioDir('exec-dream')
  writeConfig(sc, { water: { autoDream: true, autoDreamConsent: true, shadow: false } })
  const world = mount(sc, { waterGuard: false }) // 关掉水位注入，marker 里只剩自动执行的调用
  const ag = makeAgent('s-exec-dream')
  await preStep(world, ag, 75)
  const ok = await waitFor(() => readCalls(sc).some((argv) => argv[0] === 'dream' && argv[1] === '--auto'))
  const r2 = await preStep(world, ag, 75) // 已跨过：不应再投递第二次
  await sleep(800)
  results['exec-dream'] = {
    ok,
    calls: readCalls(sc),
    entries: readJsonl(sc.log),
    injected2: r2.injected,
  }
}

// ---- 4) 缺同意：autoDream 开了但 consent 没有 → 不执行，只记 no-consent ----
{
  const sc = scenarioDir('no-consent')
  writeConfig(sc, { water: { autoDream: true, autoDreamConsent: false, shadow: false } })
  const world = mount(sc, { waterGuard: false })
  await preStep(world, makeAgent('s-no-consent'), 75)
  await sleep(1000)
  results['no-consent'] = {
    calls: readCalls(sc),
    entries: readJsonl(sc.log),
  }
}

// ---- 5) 90%：capture 永不自动执行，只记账 + 提醒一次 ----
{
  const sc = scenarioDir('capture-90')
  writeConfig(sc, { water: { autoCapture: true, autoCaptureConsent: true, shadow: false } })
  const world = mount(sc) // waterGuard 保持默认 true，验证"记录 + 注入提醒一次"
  const ag = makeAgent('s-capture-90')
  const r1 = await preStep(world, ag, 90)
  const r2 = await preStep(world, ag, 90) // 已提醒过一次，不再重复
  await sleep(1200)
  results['capture-90'] = {
    calls: readCalls(sc),
    forbidden: CALLS_FORBIDDEN(readCalls(sc)),
    entries: readJsonl(sc.log),
    injected1: r1.injected,
    injected2: r2.injected,
  }
}

// ---- 6) 非法配置回退默认：脏值/错类型/只配一半 ----
{
  // 6a) 字段类型全非法（字符串/数字冒充布尔）→ 回退默认全关
  const sc = scenarioDir('invalid-config')
  writeConfig(sc, { water: { autoDream: 'true', autoDreamConsent: 1, shadow: 'false' } })
  const world = mount(sc)
  const r = await preStep(world, makeAgent('s-invalid'), 75)
  await sleep(1000)
  results['invalid-config'] = {
    calls: readCalls(sc),
    forbidden: CALLS_FORBIDDEN(readCalls(sc)),
    logExists: existsSync(sc.log),
    injected: r.injected,
  }

  // 6b) water 段整体不是对象 → 同样回退默认
  const sc2 = scenarioDir('invalid-config-2')
  writeConfig(sc2, { water: 'yes' })
  const world2 = mount(sc2)
  await preStep(world2, makeAgent('s-invalid-2'), 90)
  await sleep(1000)
  results['invalid-config-2'] = {
    calls: readCalls(sc2),
    forbidden: CALLS_FORBIDDEN(readCalls(sc2)),
    logExists: existsSync(sc2.log),
  }

  // 6c) 只配一半（autoDream=true，consent/shadow 用默认）→ 记 no-consent、不执行
  const sc3 = scenarioDir('partial-config')
  writeConfig(sc3, { water: { autoDream: true } })
  const world3 = mount(sc3, { waterGuard: false })
  await preStep(world3, makeAgent('s-partial'), 75)
  await sleep(1000)
  results['partial-config'] = {
    calls: readCalls(sc3),
    forbidden: CALLS_FORBIDDEN(readCalls(sc3)),
    entries: readJsonl(sc3.log),
  }
}

console.log(JSON.stringify({ node: process.version, results, warnings },
  (key, value) => (value === undefined ? null : value)))
"""


def run_harness():
    """运行一次 node harness，返回 {results, warnings, node, nodePath}；无 node 返回 None。"""
    global _HARNESS, _HARNESS_DIR
    if _HARNESS is not None:
        return _HARNESS
    node = find_node()
    if not node:
        _HARNESS = None
        return None
    tmpdir = tempfile.mkdtemp(prefix="lingshu-water-auto-")
    _HARNESS_DIR = tmpdir
    harness = Path(tmpdir) / "harness.mjs"
    harness.write_text(HARNESS_JS, encoding="utf-8")
    fake_brain = Path(tmpdir) / "fake-brain.mjs"
    fake_brain.write_text(FAKE_BRAIN_JS, encoding="utf-8")
    env = dict(os.environ)
    env["LINGSHU_CRON_PLUGIN"] = str(CRON)
    env["LINGSHU_CRON_TMP"] = tmpdir
    env["LINGSHU_CRON_NODE"] = node
    env["LINGSHU_CRON_FAKE_BRAIN"] = str(fake_brain)
    proc = subprocess.run(
        [node, str(harness)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=240,
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
    _HARNESS = {
        "results": data.get("results", {}),
        "warnings": data.get("warnings", []),
        "node": data.get("node"),
        "nodePath": node,
    }
    return _HARNESS


def harness_results():
    state = run_harness()
    if state is None:
        print("SKIP: 没有找到 node，跳过运行时行为检查（静态契约仍然生效）")
        return None
    return state["results"]


# ---------------------------------------------------------------- 静态契约


def test_static_contract():
    """plugin.js 有完整接线 + node --check 通过；且 capture 绝不在自动执行路径上。"""
    assert CRON.is_file(), f"缺少插件文件：{CRON}"
    src = CRON.read_text(encoding="utf-8")
    for needle in (
        LOG_NAME,
        "512 * 1024",
        "autoDream",
        "autoDreamConsent",
        "autoCapture",
        "autoCaptureConsent",
        "shadow: true",
        "estimateWater",
        "runBrain",
        "no-verbatim",          # 90% 只记录，不自动 capture
        "escapeSuggestion",     # 90% 的待人工确认逃生建议
    ):
        assert needle in src, f"lingshu-cron 缺少水位自动执行关键实现「{needle}」"
    # 自动执行只允许 dream --auto 这一条 argv；capture 绝不进 fire 的 argv
    assert re.search(r"argv:\s*\[\s*'capture'", src) is None, \
        "capture 不允许出现在自动执行 argv 里（它需要会话原文，必须人工确认）"
    assert "fire({ id: 'dream', argv: ['dream', '--auto'] })" in src, \
        "75% 自动做梦应沿用 fire()/runBrain 的后台通道（与轮次 dream 共用并发闸）且绝不 await"
    node = find_node()
    if not node:
        print("SKIP: 没有 node，跳过 node --check")
        return
    proc = subprocess.run(
        [node, "--check", str(CRON)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert proc.returncode == 0, f"node --check {CRON} 失败：{proc.stderr}"


# ---------------------------------------------------------------- 运行时行为


def _calls_summary(calls):
    return [list(argv) for argv in calls]


def _entry_fields(entry):
    return set(entry)


def test_default_all_off_no_execute_no_write():
    """默认全关（无 water 段）：75%/90% 都不执行、不写台账；既有水位提醒照常。"""
    results = harness_results()
    if results is None:
        return
    item = results["default-off"]
    assert item["logExists"] is False, "默认全关不该写 water-auto.jsonl"
    assert item["rotatedExists"] is False, "默认全关不该产生轮转文件"
    assert item["forbidden"] == [], \
        f"默认全关不该有 dream/capture 调用：{_calls_summary(item['calls'])}"
    # 既有水位守卫（waterGuard 默认 true）仍然工作：至少有一次 check-context 调用
    assert any(call and call[0] == "check-context" for call in item["calls"]), \
        f"既有水位提醒（check-context）不该被本次改动影响：{_calls_summary(item['calls'])}"
    for injected in (item["injected75"], item["injected90"]):
        assert all("逃生" not in text for text in injected), \
            f"默认全关不该注入 autoCapture 的逃生提醒：{injected}"


def test_shadow_mode_records_without_executing():
    """shadow=true：只记 executed=false 的影子账、不执行；500KB 台账轮转成 .1。"""
    results = harness_results()
    if results is None:
        return
    item = results["shadow-75"]
    entries = item["entries"]
    assert len(entries) == 1, f"首次跨 75% 应恰好记 1 条影子账：{entries}"
    entry = entries[0]
    assert _entry_fields(entry) == {"ts", "level", "action", "executed", "reason"}, \
        f"台账字段不符合契约：{entry}"
    assert entry["level"] == 75, entry
    assert entry["action"] == "dream --auto", entry
    assert entry["executed"] is False, f"shadow 模式不能真执行：{entry}"
    assert entry["reason"] == "shadow", entry
    assert isinstance(entry["ts"], str) and entry["ts"], entry
    assert item["forbidden"] == [], f"shadow 模式不该真起 dream 子进程：{item['calls']}"
    # 512KB 轮转：预置的 ~840KB 旧档被改名成 .1，当前文件只剩新记的这一行
    assert item["rotatedExists"] is True, "台账超 512KB 应轮转为 water-auto.jsonl.1"
    assert item["rotatedSize"] == item["sizeBefore"] and item["rotatedSize"] > MAX_BYTES, \
        f"轮转文件大小不符：{item['rotatedSize']} vs {item['sizeBefore']}"
    # 第二次跨 75% 不重复记账
    assert len(item["injected1"]) >= 1 and item["injected2"] == [], \
        f"档位只处理一次：{item['injected1']} / {item['injected2']}"


def test_execute_requires_both_switches_and_shadow_off():
    """autoDream+consent 且 shadow=false 时 75% 调 dream --auto；缺同意不执行。"""
    results = harness_results()
    if results is None:
        return
    item = results["exec-dream"]
    assert item["ok"] is True, \
        f"双开关 true 且 shadow=false 时 75% 应触发 dream --auto：{_calls_summary(item['calls'])}"
    assert [list(call) for call in item["calls"]] == [["dream", "--auto"]], \
        f"应恰好一次 dream --auto（不重复投递）：{_calls_summary(item['calls'])}"
    entries = item["entries"]
    assert len(entries) == 1, f"执行成功应记 1 条台账：{entries}"
    assert entries[0]["level"] == 75 and entries[0]["executed"] is True, entries[0]
    assert entries[0]["reason"] == "executed", entries[0]
    assert item["injected2"] == [], "第二次跨 75% 不应再投递 dream"

    no_consent = results["no-consent"]
    assert no_consent["calls"] == [], \
        f"缺 autoDreamConsent 时绝不能执行：{_calls_summary(no_consent['calls'])}"
    assert len(no_consent["entries"]) == 1, no_consent["entries"]
    assert no_consent["entries"][0]["executed"] is False, no_consent["entries"][0]
    assert no_consent["entries"][0]["reason"] == "no-consent", no_consent["entries"][0]


def test_capture_never_auto_executed_at_90():
    """90%：capture 永不自动执行——只记 executed=false 的逃生建议并提醒一次。"""
    results = harness_results()
    if results is None:
        return
    item = results["capture-90"]
    assert item["forbidden"] == [], \
        f"90% 绝不能自动调 capture（需要会话原文）：{_calls_summary(item['calls'])}"
    assert not any(call and call[0] == "capture" for call in item["calls"]), item["calls"]
    entries = item["entries"]
    assert len(entries) == 1, f"90% 首次跨档应恰好记 1 条台账：{entries}"
    entry = entries[0]
    assert _entry_fields(entry) == {"ts", "level", "action", "executed", "reason"}, entry
    assert entry["level"] == 90 and entry["action"] == "capture", entry
    assert entry["executed"] is False, f"capture 台账必须 executed=false：{entry}"
    assert entry["reason"] == "no-verbatim", entry
    # 提醒注入一次：第一条 pre-step 有"逃生建议"，第二条没有
    text1 = "\n".join(item["injected1"])
    assert "逃生" in text1, f"90% 应注入一次待人工确认的逃生建议：{item['injected1']}"
    assert "capture" in text1 and "原文" in text1, f"逃生建议应说明 capture 需要原文：{text1}"
    assert "\n".join(item["injected2"]).count("逃生") == 0, \
        f"逃生建议每次会话只注入一次：{item['injected2']}"


def test_invalid_config_falls_back_to_defaults():
    """配置非法值回退默认：脏类型不生效、water 非对象当没配、只配一半按默认补齐。"""
    results = harness_results()
    if results is None:
        return
    bad = results["invalid-config"]
    assert bad["logExists"] is False, "非法布尔值不该让自动执行生效（应回退默认全关）"
    assert bad["forbidden"] == [], f"非法配置不该执行动作：{_calls_summary(bad['calls'])}"

    bad2 = results["invalid-config-2"]
    assert bad2["logExists"] is False, "water 段不是对象时应视作没配（默认全关）"
    assert bad2["forbidden"] == [], _calls_summary(bad2["calls"])

    partial = results["partial-config"]
    assert partial["forbidden"] == [], \
        f"只配 autoDream=true（consent 默认 false）不该执行：{_calls_summary(partial['calls'])}"
    entries = partial["entries"]
    assert len(entries) == 1, f"开了 autoDream 但没同意，应记一条影子账：{entries}"
    assert entries[0]["executed"] is False and entries[0]["reason"] == "no-consent", entries[0]


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
