// 灵枢 · 例行维护调度插件（lingshu-cron）
//
// 设计思想：灵枢的例行维护（做梦、每日整理、备份、冲突检测、用量上报、技能自检）
// 原本压在"模型自觉"上——模型记不住就不做。本插件把调度搬进 APP：
// 模型只负责内容（记什么、想什么），维护归插件。
//
// 三条铁律（对齐 turn-stopping / pre-step 的真实语义）：
// 1. 绝不 await：agent/turn-stopping 是串行事件，await 长任务会拖住回合关闭。
//    所有任务 fire-and-forget，失败静默吞掉。
// 2. 防并发：running Set 挡住同一任务重入（用户连发消息时事件密集触发）。
// 3. 状态落盘：状态文件放 APP profile 目录（不碰 ~/.brain-memory），
//    否则每次重启 daily/backup 都会重跑。临时文件 + rename 原子写。
//
// 数据安全：本插件只调用灵枢 CLI 的只读/自维护命令，不改任何会话上下文；
// 注入类（review / check-context）默认也开启，但只在有内容时注入一次，
// 且可整体关闭（injectReview / waterGuard = false）——
// 保证不影响"人格/记忆/灵魂 + AI 自主自动化"的主功能。
//
// 水位自动执行（v3.44，影子模式 + 显式同意，隐私默认安全）：
// 75% 做梦 / 90% 封存过去靠"模型自觉"，现在可由 config.json 的 water 段自动执行，
// 但默认全关：autoDream/autoCapture 及其 consent 都缺省 false，shadow 缺省 true；
// shadow=true 只记"本来会做什么"，真执行必须 autoXxx && autoXxxConsent 同真且
// shadow=false。90% 的 capture 永不自动执行（插件拿不到会话原文，绝不自己拼），
// 只记一条待人工确认的逃生建议并在会话里提醒一次。详见下方 WATER 段注释。

import { spawn } from 'node:child_process'
import { appendFileSync, existsSync, mkdirSync, readFileSync, readdirSync, renameSync, statSync, writeFileSync, rmSync } from 'node:fs'
import { homedir } from 'node:os'
import { delimiter, dirname, join } from 'node:path'
import { randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'

export const name = 'dsh-lingshu-cron'

// 本插件文件在 <包根>/cordis-plugin/lingshu-cron/plugin.js → 上两级是包根
const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url))
const PACKAGE_ROOT = join(PLUGIN_DIR, '..', '..')

const DEFAULTS = {
  brainPy: '',            // brain.py 绝对路径；留空自动探测（与 lingshu-startup 同一套逻辑）
  python: '',             // python.exe 绝对路径；留空自动探测（spawn 不解析 PATHEXT）
  timeoutMs: 120000,      // 后台任务子进程超时（做梦/备份可能慢，2 分钟兜底）
  dreamTurns: 8,          // 每 N 轮触发 dream --auto
  extractTurns: 4,        // 每 N 轮把攒下的对话文本静默 extract（即记即存）
  extractMinChars: 80,    // 攒的文本短于该值不触发（惊异门控在 brain.py extract 里有第二道）
  selfReviewDays: 3,      // 每 N 天触发 skill self-review
  runUsage: true,         // 每轮 turn-stopping 静默跑 usage --reply
  injectReview: true,     // review 有 pending 时注入提醒（每会话一次，见 reviewNotice*）
  reviewNoticeMin: 5,     // v3.57：待审条数达到这个数才提醒（零星一两条不打扰）
  reviewNoticeOnce: true, // v3.57：每会话最多提醒一次（false = 每次都提，旧行为）
  waterGuard: true,       // 水位守卫：pre-step 估真实水位，首次跨 60/75/90 各注一条提醒（v3.01）
  charsPerToken: 2,       // 中文语境 token 估算：约 2 字符 ≈ 1 token（v3.23 从 3 下调，避免低估）
  systemOverheadTokens: 18000, // system prompt + 工具 schema 的固定开销（不进 messages，v3.23）
  contextLimit: 131072,   // 模型上下文窗口 token 数（估算分母；宿主给 usage 时用宿主值）
  waterCheckTurns: 10,    // 每 N 轮做一次「软自检」：估算没到阈值也提醒 AI 自查（v3.23）
  reviewCacheMs: 30000,   // review --json 结果缓存时长（pre-step 每步都跑，别真跑）
  pruneInjected: false,   // v3.59：默认**不清理**已注入的旧消息。
                          // 为什么反过来了：提示词缓存从请求开头逐项匹配，删掉历史中段的
                          // 一条消息等于**改写前缀**——从该处往后全部无法复用缓存，新模型还会
                          // 丢弃变更点之后的推理项（OpenAI《保持前缀不变》）。所以"删旧消息省
                          // token"是净亏。只在压缩/上下文重建后由宿主清一次才划算。
                          // 想恢复旧行为：config 里设 pruneInjected: true。
                          // （原文已被 extract 封存海马体，清掉 = 归档后遗忘，不丢信息）
}

// 本插件注入消息的 source.name：**故意与 startup 的 'lingshu-startup' 区分开**。
// 原来两个子插件都用 'lingshu'，于是 cron 的 pruneInjected 会把 startup 注入的
// 人格/记忆块一起删掉（谁先跑谁赢）。lingshu-im 早就因为同一个坑改用了
// 'lingshu-im'（见其 plugin.js 注释），这里补齐。
const INJECT_SOURCE = 'lingshu-cron'

/** 灵枢家族所有注入消息的 source.name——水位估算要排除它们，只看真实对话。 */
const LINGSHU_SOURCES = new Set(['lingshu', 'lingshu-cron', 'lingshu-startup', 'lingshu-im'])

/** 合并用户配置与默认值 */
function mergeConfig(user) {
  const merged = { ...DEFAULTS }
  if (user && typeof user === 'object') {
    for (const k of Object.keys(DEFAULTS)) {
      const v = user[k]
      if (v !== undefined && v !== null && v !== '') merged[k] = v
    }
  }
  return merged
}

// ---------------------------------------------------------------- 探测（照抄 lingshu-startup 已验证方案）

async function findBrainPy(configured, ctx) {
  if (configured) return existsSync(configured) ? configured : null
  // ① 本包自带优先：../../scripts/brain.py 与插件同包发布，不依赖宿主状态
  //    （v3.53 之前缺这一条 → 注册 runtime 技能时没给 resourceBase/path，
  //     注册表查不到路径，即记即存与天级任务就静默停摆、days 永远不推进）
  for (const p of brainPyCandidates()) {
    if (existsSync(p)) return p
  }
  // ② 技能注册表（装了全局技能副本时更准；字段名跨版本不同，逐个试）
  try {
    const registry = ctx?.get?.('skills') ?? ctx?.skills
    let items = registry?.list?.()
    if (items?.then) items = await items
    const hit = Array.isArray(items) ? items.find((s) => s?.name === 'lingshu') : null
    if (hit) {
      const bases = [
        [hit.path, true], [hit.location, true],
        [hit?.resourceBase?.path, false],
        [hit.dir, false], [hit.baseDir, false], [hit.directory, false],
      ]
      for (const [raw, isFile] of bases) {
        if (typeof raw !== 'string' || !raw) continue
        const root = isFile ? dirname(raw) : raw
        const p = join(root, 'scripts', 'brain.py')
        if (existsSync(p)) return p
      }
    }
  } catch {}
  return null
}

/** 有可能藏着 brain.py 的绝对路径清单（纯函数，测试与排障共用） */
export function brainPyCandidates() {
  const out = [join(PACKAGE_ROOT, 'scripts', 'brain.py')]
  const envDir = (process.env.LINGSHU_SKILL_DIR || '').trim()
  if (envDir) out.push(join(envDir, 'scripts', 'brain.py'))
  const home = homedir()
  out.push(
    join(home, '.dsh', 'skills', 'lingshu', 'scripts', 'brain.py'),
    join(home, '.trae-cn', 'skills', 'lingshu', 'scripts', 'brain.py'),
  )
  return out
}

/** 有可能可用的 python 可执行文件清单（纯函数，测试与排障共用） */
export function pythonCandidates() {
  const home = homedir()
  const out = []
  for (const app of [process.env.APP_DSH, join(home, '.dsh')].filter(Boolean)) {
    out.push(join(app, 'app-runtimes', 'app-primary-runtime', 'dependencies', 'python', 'python.exe'))
    out.push(join(app, 'runtimes', 'app-primary-runtime', 'dependencies', 'python', 'python.exe'))
  }
  // PATH 上的 python.exe / python3.exe（ServBay、Store、conda 等都能命中）。
  // 不选 .cmd shim：spawn 走 CreateProcess，不解析 PATHEXT。
  for (const dir of (process.env.PATH || '').split(delimiter)) {
    if (!dir) continue
    out.push(join(dir, 'python.exe'), join(dir, 'python3.exe'))
  }
  out.push(
    join(home, 'AppData', 'Local', 'Programs', 'Python', 'python.exe'),
    'C:\\Python313\\python.exe',
    'C:\\Python312\\python.exe',
    'C:\\Program Files\\Python313\\python.exe',
  )
  return out
}

/** 静默失败留痕：写 ~/.dsh/lingshu-diag.json（doctor / 排障读它） */
export function writeDiag(kind, detail) {
  try {
    const base = process.env.APP_DSH || join(homedir(), '.dsh')
    const payload = {
      ts: new Date().toISOString(),
      plugin: 'lingshu-cron',
      kind,
      detail: detail && typeof detail === 'object' ? detail : { message: String(detail ?? '') },
    }
    writeFileSync(join(base, 'lingshu-diag.json'), JSON.stringify(payload, null, 2), 'utf8')
  } catch {}
}

function findPythonExe(configured) {
  if (configured) return existsSync(configured) ? configured : null
  for (const p of pythonCandidates()) {
    if (existsSync(p)) return p
  }
  return null
}

// ---------------------------------------------------------------- 状态落盘（APP profile 目录，不碰 ~/.brain-memory）

function statePath() {
  const base = process.env.APP_DSH || join(homedir(), '.dsh')
  return join(base, 'lingshu-cron-state.json')
}

function loadState(path) {
  try {
    const raw = readFileSync(path, 'utf8')
    const obj = JSON.parse(raw)
    return obj && typeof obj === 'object' ? obj : {}
  } catch {
    return {}
  }
}

function saveState(path, state) {
  try {
    mkdirSync(dirname(path), { recursive: true })
    const tmp = `${path}.${randomUUID()}.tmp`
    writeFileSync(tmp, JSON.stringify(state, null, 2), 'utf8')
    try { rmSync(path, { force: true }) } catch {}
    renameSync(tmp, path) // 原子替换（同目录 rename）
  } catch {}
}

const todayStr = () => new Date().toISOString().slice(0, 10)

/** agent 的稳定标识（用于状态落盘）：拿不到稳定 id 就返回 null（仅内存计数）。 */
function agentKey(agent) {
  const k = agent?.id ?? agent?.agentId ?? agent?.sessionId ?? agent?.session?.id
  return typeof k === 'string' && k ? k : null
}

function dueByDays(task, state) {
  const last = state?.days?.[task.id]
  return last !== todayStr()
}

// ---------------------------------------------------------------- 水位自动执行（影子模式 + 显式同意）
//
// 配置在 ~/.brain-memory/config.json 的 water 段（不是本插件的 config——改完
// 下一个水位档位即刻生效，不用重启宿主）：
//   "water": {"autoDream": false, "autoDreamConsent": false,
//             "autoCapture": false, "autoCaptureConsent": false, "shadow": true}
// 语义（缺省全关，隐私默认安全）：
//   * autoXxx 与 autoXxxConsent 都缺省 false；shadow 缺省 true；
//   * shadow=true 时只记"本来会做什么"，绝不真执行；
//   * 真执行必须 autoXxx && autoXxxConsent 同时为真，且 shadow=false；
//   * 配置缺 water 段/字段类型不对（非布尔）一律回退上面这套默认值——
//     宁可不生效，也不从脏配置里"猜"一个 true 出来。
// 动作：
//   * 首次跨 75%：满足执行条件就后台跑 dream --auto（沿用 runBrain 的 fire-and-forget，
//     绝不 await）；否则只写一行影子台账（autoDream 全关时连台账都不写）；
//   * 首次跨 90%：capture 永不自动执行——capture 要的是会话原文，插件在 pre-step
//     只拿得到消息与水位、拿不到"原文"，绝不自己拼凑；改为记一条"待人工确认的
//     逃生建议"并注入提醒一次，只有用户在会话里明确要求才真 capture。
// 台账：~/.brain-memory/water-auto.jsonl，每行 {ts, level, action, executed, reason}；
// 超 512KB 轮转为 water-auto.jsonl.1（只留一份旧档，思路同 failures.py）。

const WATER_AUTO_LOG = 'water-auto.jsonl'
const WATER_AUTO_MAX_BYTES = 512 * 1024

const DEFAULT_WATER = {
  autoDream: false,
  autoDreamConsent: false,
  autoCapture: false,
  autoCaptureConsent: false,
  shadow: true,
}

/** 记忆目录：BRAIN_MEMORY_HOME > ~/.brain-memory/.home 指针 > ~/.brain-memory（与 guard 同口径）。 */
function memoryHome() {
  const env = (process.env.BRAIN_MEMORY_HOME || '').trim()
  if (env) return env
  const base = join(homedir(), '.brain-memory')
  try {
    const ptr = readFileSync(join(base, '.home'), 'utf8').trim()
    if (ptr) return ptr
  } catch {} // 没有指针文件就用默认目录
  return base
}

/** 只认真正的布尔值：字符串 "true" / 数字 1 这类手写脏值一律回退默认（隐私开关宁可不生效）。 */
function boolOr(value, dflt) {
  return typeof value === 'boolean' ? value : dflt
}

/** 合并默认值与 config.json 的 water 段；water 缺失/非对象/字段非法都回退默认。 */
function resolveWaterConfig(raw) {
  const w = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw.water : null
  const water = w && typeof w === 'object' && !Array.isArray(w) ? w : {}
  const out = { ...DEFAULT_WATER }
  for (const key of Object.keys(DEFAULT_WATER)) out[key] = boolOr(water[key], DEFAULT_WATER[key])
  return out
}

// config.json 很小，但 pre-step 每步都会问一次：按 mtime 缓存，文件没变不重读。
// 用户改完配置、mtime 变化后下一次判定即生效（冻结配置会让撤回同意变难）。
let waterConfigCache = { path: '', mtime: -1, value: DEFAULT_WATER }

function loadWaterAutoConfig() {
  try {
    const file = join(memoryHome(), 'config.json')
    let mtime = -1
    try { mtime = statSync(file).mtimeMs } catch { mtime = -1 }
    if (waterConfigCache.path === file && waterConfigCache.mtime === mtime) return waterConfigCache.value
    let raw = null
    if (mtime >= 0) {
      try { raw = JSON.parse(readFileSync(file, 'utf8')) } catch { raw = null } // 坏 JSON 视作没配
    }
    const value = resolveWaterConfig(raw)
    waterConfigCache = { path: file, mtime, value }
    return value
  } catch {
    return { ...DEFAULT_WATER }
  }
}

/** 追加一行自动执行台账；超 512KB 先轮转成 water-auto.jsonl.1。台账 IO 失败绝不抛。 */
function recordWaterAuto(level, action, executed, reason) {
  try {
    const file = join(memoryHome(), WATER_AUTO_LOG)
    mkdirSync(dirname(file), { recursive: true })
    try {
      if (existsSync(file) && statSync(file).size > WATER_AUTO_MAX_BYTES) {
        const old = `${file}.1`
        try { rmSync(old, { force: true }) } catch {}
        renameSync(file, old) // 只留一份旧档：失败可见化（failures.py）同款轮转
      }
    } catch {} // 轮转失败不拦记账：顶多当前文件继续长大
    appendFileSync(file, JSON.stringify({
      ts: new Date().toISOString(),
      level,                       // 75 | 90
      action,                      // 'dream --auto' | 'capture'
      executed: executed === true, // true = 已投递后台执行（fire-and-forget 不回传成败）
      reason: String(reason || ''), // executed / busy / shadow / no-consent / no-verbatim
    }) + '\n', 'utf8')
  } catch {} // 台账本身不能成为故障源
}

// ---------------------------------------------------------------- 上下文压力（v3.57）

/** 压力阈值：与 scripts/core.py 的 PRESSURE_DEFAULTS **保持一致**；config 的
 *  pressure 段可覆盖。两边各写一份是因为插件不 import Python，注释互指即可。 */
const PRESSURE_DEFAULTS = { lazy_at: 70, minimal_at: 85, hard_at: 95, force_at: 98 }

/** 读 pressure 阈值（写坏/缺项一律回退默认，宁可不生效也不猜）。 */
export function pressureThresholds() {
  let seg = {}
  try {
    const cfg = JSON.parse(readFileSync(join(memoryHome(), 'config.json'), 'utf8'))
    const raw = cfg?.pressure
    if (raw && typeof raw === 'object' && !Array.isArray(raw)) seg = raw
  } catch {}
  const out = { ...PRESSURE_DEFAULTS }
  for (const key of Object.keys(PRESSURE_DEFAULTS)) {
    const v = seg[key]
    if (typeof v === 'boolean') continue
    if (typeof v === 'number' && Number.isFinite(v) && v >= 1 && v <= 100) out[key] = v
  }
  out.minimal_at = Math.max(out.minimal_at, out.lazy_at)
  out.hard_at = Math.max(out.hard_at, out.minimal_at)
  out.force_at = Math.max(out.force_at, out.hard_at)
  return out
}

/** 上次写 Pressure 的百分比（按 agent 记，避免每步都写盘）。 */
const pressureWritten = new Map()

/**
 * 把水位写进 `HOME/_pressure.json`（注入侧读它降档）。
 * 只在"跨 5 个百分点"或"跨档"时真写，减少磁盘写；失败静默返回 false。
 */
export function writePressure(pct, source, prevMax) {
  try {
    if (pct == null || !Number.isFinite(Number(pct))) return false
    const p = Math.round(Number(pct))
    const th = pressureThresholds()
    const tier = p >= th.minimal_at ? 'minimal' : p >= th.lazy_at ? 'lazy' : 'full'
    const key = `${tier}`
    const last = pressureWritten.get('last')
    if (last && last.tier === key && Math.abs(p - last.pct) < 5) return false
    pressureWritten.set('last', { pct: p, tier: key })
    const file = join(memoryHome(), '_pressure.json')
    const tmp = `${file}.${randomUUID()}.tmp`
    writeFileSync(tmp, JSON.stringify({ pct: p, at: Math.floor(Date.now() / 1000), source: String(source || '') }), 'utf8')
    try { rmSync(file, { force: true }) } catch {}
    renameSync(tmp, file)
    return true
  } catch { return false }
}

/**
 * 98% 强制自救：只跑**没有破坏性**的两条——
 *   `auto-archive`：把审核通过的 pending 记忆按类型转正（原记忆只加 archived_at，不删）
 *   `memory diagnose`：只报告问题，不修复
 * capture 永不自动（插件拿不到会话原文），purge 永不自动（物理删除）。
 */
export function forceActions() {
  return [
    { id: 'autoarchive', argv: ['auto-archive'] },
    { id: 'diagnose', argv: ['memory', 'diagnose'] },
  ]
}

/**
 * 90% 的"待人工确认的逃生建议"：说明为什么不由插件 capture，并给出人工确认后的命令。
 */function escapeSuggestion(pct, source) {
  const from = source === 'usage' ? '宿主 usage' : '消息字符估算'
  return `【灵枢·水位 90% 逃生建议】上下文估算约 ${pct}%（来源：${from}）。`
    + '封存 capture 需要会话原文，插件拿不到正文、也绝不替你拼凑，所以这里只提醒、不自动执行。'
    + '请先问用户是否现在封存；用户明确要求后，由你把原文原样贴进命令再跑：'
    + ' python scripts/brain.py capture "<把原文粘进来>" --tag 逃生 --kind episode'
    + '（每次会话只提醒这一次；autoCapture 开关只代表"允许考虑自动封存"，但 capture 永不自动调用。）'
}

// ---------------------------------------------------------------- 子进程（照抄 lingshu-startup 的 spawn 方案）

function runBrain(pythonExe, pythonCmdFallback, brainPy, argv, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false
    const done = (v) => { if (!settled) { settled = true; resolve(v) } }
    const args = [brainPy, ...argv]
    let child
    try {
      if (pythonExe) {
        child = spawn(pythonExe, args, {
          windowsHide: true,
          env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
          // 显式把工作目录挪到用户主目录：绝不继承宿主 cwd。宿主 cwd 若在插件
          // 目录内，Windows 会因"目录被进程占用"拒绝 pnpm 的目录 rename，
          // 导致市场更新（remove+add）报"文件被占用、请退出 DSH"。
          cwd: homedir(),
        })
      } else {
        const comspec = process.env.ComSpec ?? 'cmd.exe'
        const pythonCmd = pythonCmdFallback || 'python'
        const cmdline = [`"${pythonCmd}"`, ...args.map((a) => `"${a}"`)].join(' ')
        child = spawn(comspec, ['/d', '/s', '/c', cmdline], {
          windowsHide: true,
          windowsVerbatimArguments: true,
          env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
          cwd: homedir(), // 同上：不把宿主 cwd（可能在插件目录内）占给子进程
        })
      }
    } catch {
      return done(null)
    }
    let out = ''
    child.stdout?.on('data', (d) => { out += d.toString('utf8') })
    child.stderr?.resume() // 必须 drain：塞满 64KB 管道子进程会写阻塞永不退出
    child.on('error', () => done(null))
    // 手动超时兜底：taskkill /T /F 杀整棵进程树（cmd 兜底分支里的 python 孙进程也要杀）
    const killer = setTimeout(() => {
      try {
        if (child.pid) {
          const tk = spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
          tk.on('error', () => {})
        }
      } catch {}
      try { child.kill() } catch {}
      done(null)
    }, timeoutMs)
    killer.unref?.()
    child.on('close', (code) => {
      clearTimeout(killer)
      done(code === 0 || code === 10 ? out : null)
    })
  })
}

// ---------------------------------------------------------------- 注入消息构造（与 lingshu-startup 同格式）

function notice(text) {
  return {
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'skill-invocation', name: INJECT_SOURCE, form: 'instructions' },
  }
}

// ---------------------------------------------------------------- 插件主体

export function apply(ctx, config) {
  // v3.23：重复挂载守卫。profile 里同时启用独立包 dsh-lingshu-startup 与 lingshu-skill
  // （根包 index.js 内部也挂 startup/cron）时，同一进程会出现两份 cron：
  // 定时任务重复跑、水位提醒重复注入。进程级只允许一份。
  if (globalThis.__lingshuCronApplied) {
    try { ctx.logger?.warn?.('lingshu-cron: 已有一份灵枢 cron 在运行，跳过重复挂载') } catch {}
    return
  }
  globalThis.__lingshuCronApplied = true
  const cfg = mergeConfig(config)

  const turns = new Map()   // agent -> 本会话轮次
  const running = new Set() // 防并发：同名任务进行中就不重入
  const convo = new Map()   // agent -> { text, seen }（即记即存缓冲 + 已处理消息游标，extract 成功后清空）
  const lastReply = new Map() // agent -> 最后一条 assistant 文本（给 usage --reply 用）
  const waterState = new Map() // agent -> { max } 水位守卫已提醒的最高档（60/75/90 各提醒一次）
  // v3.57：review 提醒已发过的 agent（每会话一次，不再每轮催）
  const reviewNoticeAt = new Map()
  const waterAutoState = new Map() // agent -> { max } 水位自动执行已处理到的最高档（75/90 各动作一次）
  let state = loadState(statePath())

  // 启动时清理崩溃残留的 extract 明文临时文件（超过 1 天的直接删）
  try {
    const base = process.env.APP_DSH || join(homedir(), '.dsh')
    const cutoff = Date.now() - 24 * 3600 * 1000
    for (const f of readdirSync(base)) {
      if (!f.startsWith('lingshu-extract-') || !f.endsWith('.txt')) continue
      const p = join(base, f)
      try {
        if (statSync(p).mtimeMs < cutoff) rmSync(p, { force: true })
      } catch {}
    }
  } catch {}
  let brainPyCache = null
  let warnedOnce = false
  let warnedNoBrain = false

  // 状态节流落盘器（#69）：变更只置脏，30 秒合并刷盘，避免每轮 turn-stopping 都写磁盘
  const stateSaver = (() => {
    let dirty = false
    let timer = null
    const flush = () => {
      timer = null
      if (!dirty) return
      dirty = false
      saveState(statePath(), state)
    }
    return {
      markDirty() {
        dirty = true
        if (!timer) {
          timer = setTimeout(flush, 30_000)
          if (timer?.unref) timer.unref()
        }
      },
      flushNow: flush,
    }
  })()
  const markStateDirty = () => stateSaver.markDirty()

  // review --json 缓存（pre-step 每步都问，30s 内复用）
  let reviewCacheAt = 0
  let reviewCacheVal = null

  // 重启恢复（#77）：state.turns 本身就是事实源，turn-stopping 直接读它续算，无需 seed 拷贝

  const warn = (msg) => { if (!warnedOnce) { warnedOnce = true; try { ctx.logger?.warn?.(msg) } catch {} } }

  async function ensureBrainPy() {
    if (brainPyCache) return brainPyCache
    const found = await findBrainPy(cfg.brainPy, ctx)
    if (!found) {
      // 静默失败必须留痕：这是 2026-11-20「记忆突然不录入了」的直接证据来源
      if (!warnedNoBrain) {
        warnedNoBrain = true
        writeDiag('brainPy-not-found', {
          tried: brainPyCandidates(),
          packageRoot: PACKAGE_ROOT,
          configured: cfg.brainPy || null,
        })
        warn('lingshu-cron: 未找到 brain.py（未安装灵枢？），调度跳过')
      }
      return null
    }
    brainPyCache = found
    return found
  }

  /** fire-and-forget：绝不 await、失败允许下轮补跑一次（当日仅一次，再败等明天）。
   *  返回 false 表示同名任务正在进行、本次没有投递（供水位自动执行如实记账）。 */
  function fire(task) {
    if (running.has(task.id)) return false
    running.add(task.id)
    ;(async () => {
      try {
        const brainPy = await ensureBrainPy()
        if (!brainPy) return
        const pythonExe = findPythonExe(cfg.python)
        const out = await runBrain(pythonExe, cfg.python, brainPy, task.argv, cfg.timeoutMs)
        // 退出码 0/10 才算跑成；失败不标记完成，下轮补跑一次，再失败就等明天
        // （state.retries 记当日已补跑次数，防持续失败造成备份/做梦风暴）。
        if (task.days) {
          state.days = state.days || {}
          if (out !== null) {
            state.days[task.id] = todayStr()
            if (state.retries) delete state.retries[task.id]
          } else {
            state.retries = state.retries || {}
            const r = state.retries[task.id] || { date: todayStr(), count: 0 }
            if (r.date !== todayStr()) { r.date = todayStr(); r.count = 0 }
            r.count += 1
            state.retries[task.id] = r
            if (r.count >= 1) state.days[task.id] = todayStr() // 已补跑过一次，今天不再纠缠
          }
          stateSaver.flushNow() // 天级任务低频，立即落盘保证不丢标记
        }
      } catch {} // 顶层兜底：任何异常静默吞掉
      finally {
        running.delete(task.id)
      }
    })()
    return true
  }

  /**
   * v3.57（保护 B 的执行端）：98% 强制自救——**同步 await**（与 fire 的 fire-and-forget
   * 不同：这里必须等结果，才能把"做了什么"如实告诉模型）。只跑无破坏性动作。
   * 返回一行摘要（失败也返回，说明失败了几个），绝不抛异常。
   */
  async function runForceActions(pythonExe, brainPy) {
    const done = []
    const failed = []
    for (const task of forceActions()) {
      try {
        const out = await runBrain(pythonExe, cfg.python, brainPy, task.argv, cfg.timeoutMs)
        if (out === null) failed.push(task.id)
        else {
          // 取首行当摘要，避免把整段输出灌进上下文
          const line = String(out).trim().split(/\r?\n/).find((x) => x.trim()) || task.id
          done.push(line.trim().slice(0, 60))
        }
      } catch { failed.push(task.id) }
    }
    const parts = []
    if (done.length) parts.push(done.join('；'))
    if (failed.length) parts.push(`失败：${failed.join('、')}（可手工重试）`)
    return parts.join('。')
  }

  /**
   * 水位自动执行判定（首次跨档才做一次）。纯同步返回要注入的提醒文本（null = 不注入）；
   * 真执行一律走 fire() 的后台通道，绝不 await。
   *   - 75%：autoDream && autoDreamConsent && shadow=false → fire `dream --auto`；
   *          否则若 autoDream 开着，只记影子台账（executed=false，reason=shadow/no-consent）；
   *          autoDream 关着就什么都不记（缺省全关 = 零副作用）。
   *   - 90%：capture 永不自动执行——插件拿不到会话原文，绝不自己拼；
   *          只记 executed=false（reason=no-verbatim）并返回一次"待人工确认的逃生建议"。
   * 两个 auto 开关都关时整段不判定、不写盘。
   */
  function runWaterAuto(agent, pct, source, wcfg) {
    if (!agent || pct == null) return null
    const armed = wcfg.autoDream === true || wcfg.autoCapture === true
    if (!armed) return null // 默认全关：不判定、不写盘
    const level = pct >= 90 ? 90 : pct >= 75 ? 75 : 0
    if (!level) return null
    const st = waterAutoState.get(agent) || { max: 0 }
    const prev = Number(st.max) || 0
    if (level <= prev) return null // 档位只推进一次（首次跨 75/90 各做一次）
    let noticeText = null
    // 75%：该做梦了。直接从低位跳到 90% 时也补做这一档（prev < 75）。
    // 复用 turn-stopping 那条任务名 'dream'：全局同一时刻只许一份 dream 在跑
    // （与轮次驱动的 dream 撞车时返回 busy，如实记 executed=false）。
    if (level >= 75 && prev < 75 && wcfg.autoDream === true) {
      const consent = wcfg.autoDreamConsent === true
      if (consent && wcfg.shadow === false) {
        const fired = fire({ id: 'dream', argv: ['dream', '--auto'] })
        recordWaterAuto(75, 'dream --auto', fired, fired ? 'executed' : 'busy')
      } else {
        // 影子模式或没给同意：只记"本来会做什么"，不动手
        recordWaterAuto(75, 'dream --auto', false, consent ? 'shadow' : 'no-consent')
      }
    }
    // 90%：该封存了，但 capture 需要原文 —— 插件永不自动拼，交人工确认。
    if (level >= 90 && wcfg.autoCapture === true) {
      recordWaterAuto(90, 'capture', false, 'no-verbatim')
      noticeText = escapeSuggestion(pct, source)
    }
    waterAutoState.set(agent, { max: level })
    return noticeText
  }

  /**
   * 即记即存：把攒的对话文本写到临时文件，静默跑 brain.py extract
   * （惊异门控 + capture 逐段去重都在 Python 侧）。成功后清空缓冲，
   * 失败保留缓冲下轮重试。fire-and-forget 三铁律同样适用。
   */
  function fireExtract(agent) {
    if (running.has('extract')) return
    const buf = convo.get(agent)
    const text = (buf?.text || '').trim()
    if (!text || text.length < cfg.extractMinChars) return // 没攒够有信息量的内容，不跑
    running.add('extract')
    ;(async () => {
      let ok = false
      try {
        const brainPy = await ensureBrainPy()
        if (!brainPy) return
        // 临时文件放 APP profile 目录（与状态文件同层），用完由 brain.py 删除
        const base = process.env.APP_DSH || join(homedir(), '.dsh')
        mkdirSync(base, { recursive: true })
        const tmp = join(base, `lingshu-extract-${randomUUID()}.txt`)
        try {
          writeFileSync(tmp, text, 'utf8')
          const pythonExe = findPythonExe(cfg.python)
          const out = await runBrain(pythonExe, cfg.python, brainPy,
            ['extract', '--file', tmp, '--min-chars', String(cfg.extractMinChars)],
            cfg.timeoutMs)
          // 只有退出码 0/10（写入成功/惊异未过门）才算跑成；失败保留缓冲，
          // 下个 extractTurns 周期重试——原逻辑把错误退出也当成功清缓冲，会丢对话。
          ok = out !== null
        } finally {
          try { rmSync(tmp, { force: true }) } catch {}
        }
      } catch {} // 顶层兜底
      finally {
        if (ok) convo.set(agent, { text: '', seen: buf?.seen }) // 清空缓冲，但保留游标（防止重复攒旧消息）
        else if (text.length > 200_000) {
          // 兜底防爆内存：截断保留头部 100k 而非全丢，并至少告警一次（#64）
          try { logger.warn(`[lingshu-cron] extract 失败且缓冲超 200k，截断保留前 100k 字符（已丢失 ${(text.length - 100_000)} 字符）`) } catch {}
          convo.set(agent, { text: text.slice(0, 100_000), seen: buf?.seen })
        }
        running.delete('extract')
      }
    })()
  }

  // 每轮结束：轮次驱动的静默任务（dream / usage）+ 天驱动任务（daily/backup/conflicts/selfreview）
  ctx.on('agent/turn-stopping', (payload) => {
    try {
      const agent = payload?.agent
      if (!agent) return
      const ak = agentKey(agent)
      // 轮数唯一事实源是状态文件 state.turns[ak]（#77）：重启续算不再依赖内存 map，
      // 内存 turns 只在拿不到稳定 ak 时兜底，避免双轨计数互相回退。
      let prev
      if (ak && Number.isFinite(Number(state.turns?.[ak]))) prev = Number(state.turns[ak])
      else prev = turns.get(agent) ?? 0
      const n = prev + 1
      turns.set(agent, n)
      // 轮数落盘：节流写（30s 合并刷盘，#69），重启后按状态文件续算
      if (ak) {
        state.turns = state.turns || {}
        if (state.turns[ak] !== n) {
          state.turns[ak] = n
          markStateDirty()
        }
      }

      const silentTasks = [
        { id: 'dream', turns: cfg.dreamTurns, argv: ['dream', '--auto'] },
        { id: 'daily', days: 1, argv: ['daily'] },
        { id: 'backup', days: 1, argv: ['backup'] },
        { id: 'conflicts', days: 1, argv: ['detect-conflicts'] },
        { id: 'selfreview', days: cfg.selfReviewDays, argv: ['skill', 'self-review'] },
        // 第④步：每日静默 persona drift —— 扫描近 1 天记忆的情感倾向，
        // 微调 state.mood_score（0=阴郁 100=明快），真实变化才落盘并记账
        { id: 'drift', days: 1, argv: ['drift', '--days', '1'] },
        // 第⑤步：每日自动归档 —— 把审核通过的 pending 记忆按"事实型→知识库 /
        // 事件型→日记"分类转正（原记忆只加 archived_at 标记，绝不删）
        { id: 'autoarchive', days: 1, argv: ['auto-archive'] },
      ]
      for (const t of silentTasks) {
        if (t.turns && n % t.turns === 0) fire(t)
        if (t.days && dueByDays(t, state)) fire(t)
      }
      if (cfg.runUsage) {
        // usage --reply 用"最后一条 assistant 回复"的真实文本统计记忆是否被用上；
        // 传空串会导致关键词重叠恒 0、每条记忆都记成"没被用上"，污染使用率统计。
        // 没有真实回复文本时宁可跳过本轮，也不给假数据。
        const reply = (lastReply.get(agent) || '').trim()
        if (reply) fire({ id: 'usage', argv: ['usage', '--reply', reply.slice(0, 4000)] })
      }
      // 即记即存：每 N 轮把攒的对话文本静默封存进海马体
      if (cfg.extractTurns > 0 && n % cfg.extractTurns === 0) fireExtract(agent)
    } catch {} // turn-stopping 是串行事件，任何异常都不能拖住回合关闭
  })

  // 水位估算：宿主给了 usage 就用真值（prompt 侧 = 未缓存 + 缓存读 + 缓存写）；
  // 否则按「消息正文字符数 + system/工具固定开销」折算。
  // v3.23 修：旧版只数 messages 的 text 字符，system prompt 与工具 schema 完全不计，
  // 而本 profile 光这两项就上万 token —— 真实 65% 时估算只有 20%，阈值永远不触发。
  // 返回 {pct, source}；拿不到任何依据返回 null。
  function estimateWater(agent, decision) {
    try {
      const msgs = Array.isArray(decision?.messages) ? decision.messages : []
      const num = (x) => (Number.isFinite(x) ? x : 0)
      for (let i = msgs.length - 1; i >= 0; i--) {
        const u = msgs[i]?.metadata?.usage || msgs[i]?.usage || msgs[i]?.modelUsage
          || msgs[i]?.metadata?.modelUsage
        if (!u || typeof u !== 'object') continue
        const prompt = num(u.inputTokens ?? u.promptTokens) + num(u.cacheReadTokens)
          + num(u.cacheWriteTokens)
        const total = num(u.totalTokens)
        const used = total > 0 ? total : prompt
        if (used > 0) {
          return { pct: Math.min(100, Math.round(used / cfg.contextLimit * 100)), source: 'usage' }
        }
      }
      let chars = 0
      for (const m of msgs) {
        // 估算只看"真实对话"：灵枢自己注入的消息不算（v3.59：三个子插件各有名字）
        if (LINGSHU_SOURCES.has(m?.source?.name)) continue
        for (const c of (Array.isArray(m?.content) ? m.content : [])) {
          if (c?.type === 'text' && typeof c.text === 'string') chars += c.text.length
        }
      }
      if (!chars) {
        // v3.57（保护 C）：原来这里直接 return null，守卫就此静默跳过——「以为没到水位，
        // 其实已经快满」正是要防的情况。现在至少按 system/工具固定开销给一个下限
        // （拿不到正文时它也是真实存在的成本），并标明来源，让提醒口径说实话。
        const floor = Number(cfg.systemOverheadTokens) || 0
        if (!floor) return null
        return { pct: Math.min(100, Math.round(floor / cfg.contextLimit * 100)), source: 'floor' }
      }
      const tokens = chars / cfg.charsPerToken + (cfg.systemOverheadTokens || 0)
      return { pct: Math.min(100, Math.round(tokens / cfg.contextLimit * 100)), source: 'chars' }
    } catch { return null }
  }

  // 注入类：只在 enter 分支追加消息，有内容才注，缓存 review 查询
  ctx.on('agent/pre-step', async (payload, next) => {
    let decision
    try {
      decision = await next()
      if (!decision || decision.kind !== 'enter') return decision
      const { agent } = payload || {}
      if (!agent) return decision

      // 上下文清理（v3.59 起默认关闭）：只清**本插件自己**注入过的旧提醒。
      // 两个要点：
      //   1. 只认 `INJECT_SOURCE`（'lingshu-cron'），绝不碰 startup 的
      //      'lingshu-startup' 记忆块，也不碰 im 的 'lingshu-im'；
      //   2. 默认不执行——删历史中段的消息会改写提示词缓存前缀，后续全部无法
      //      复用（见 DEFAULTS 里 pruneInjected 的说明）。用户显式开启才清。
      try {
        if (cfg.pruneInjected) {
          const msgs = Array.isArray(decision.messages) ? decision.messages : []
          const kept = msgs.filter((m) => m?.source?.name !== INJECT_SOURCE)
          if (kept.length !== msgs.length) {
            decision = { ...decision, messages: kept }
          }
        }
      } catch {}
      // 即记即存缓冲：只攒"本步新增"的文本——按消息 id 记游标，重复推送的旧消息
      // 不重复入缓冲（否则缓冲膨胀、extract 内容重复）。同时记录最后一条
      // assistant 文本给 usage --reply 用（usage 需要真实回复，不能传空串）。
      try {
        const msgs = Array.isArray(decision.messages) ? decision.messages : []
        if (msgs.length) {
          const buf = convo.get(agent) || { text: '', seen: [] }
          const seenIds = new Set(Array.isArray(buf.seen) ? buf.seen : [])
          let text = buf.text
          let lastAsst = lastReply.get(agent) || ''
          let batchIds = []
          for (const m of msgs) {
            const mid = m?.id
            if (mid != null) {
              if (seenIds.has(mid)) continue // 上一步已攒过这条，跳过（只加增量）
              batchIds.push(mid)
            }
            for (const c of (Array.isArray(m?.content) ? m.content : [])) {
              if (c?.type === 'text' && typeof c.text === 'string' && c.text.trim()) {
                const t = c.text.trim()
                if (m.role === 'assistant') lastAsst = t.slice(0, 4000)
                text += (text ? '\n' : '') + m.role + '：' + t.slice(0, 4000) + '\n'
              }
            }
          }
          // 游标：只保留最近几批的 id（防无限增长），本批新 id 全部记入
          if (batchIds.length) {
            seenIds.add(`\0batch:${JSON.stringify(batchIds)}`)
          }
          convo.set(agent, {
            text,
            seen: [...seenIds].slice(-16), // 最近 16 个批次标记足够覆盖重推场景
          })
          if (lastAsst) lastReply.set(agent, lastAsst)
        }
      } catch {}

      const n = (turns.get(agent) ?? 1)
      const brainPy = await ensureBrainPy()
      if (!brainPy) return decision
      const pythonExe = findPythonExe(cfg.python)

      // 0) 水位估算：注入与自动执行共用同一口径（宿主 usage 优先，否则
      //    「消息字符 + system/工具固定开销」）。只在需要时才算：waterGuard 关闭
      //    且 water 自动执行全关时，默认零开销。
      const waterAutoCfg = loadWaterAutoConfig()
      const waterArmed = waterAutoCfg.autoDream === true || waterAutoCfg.autoCapture === true
      const est = (cfg.waterGuard || waterArmed) ? estimateWater(agent, decision) : null
      const pct = est && Number.isFinite(est.pct) ? est.pct : null

      // 1) 水位自动执行（v3.44，默认全关）：放在 review 之前判定——review 有待确认时
      //    会提前 return，不能让它挡掉「75% 该做梦 / 90% 该逃生」的自动动作。
      //    真执行走 fire() 的后台通道，绝不 await。
      try {
        const autoNotice = runWaterAuto(agent, pct, est?.source || '', waterAutoCfg)
        if (autoNotice) {
          decision = { ...decision, messages: [...decision.messages, notice(autoNotice)] }
        }
      } catch {} // 自动执行判定异常不能拖住 pre-step

      // 2) review 提醒（v3.57 降级）：原来是"每轮 + 命令式"——「先运行 lingshu review
      //    确认或丢弃，再继续当前任务」，逼模型每次都先跑一轮工具才回答（实测多一轮
      //    工具往返 ≈ TTFT +2~4s，且用户会感觉"开局慢"）。现在：
      //      · 文案改成非命令式（"有空再看"），不再抢占回答；
      //      · 每会话最多提醒一次（reviewNoticeOnce 默认 true）；
      //      · 待审条数达到 reviewNoticeMin（默认 5）才提，零星一两条留给用户自己看。
      if (cfg.injectReview) {
        if (Date.now() - reviewCacheAt > cfg.reviewCacheMs) {
          const raw = await runBrain(pythonExe, cfg.python, brainPy, ['review', '--json'], 15000)
          try {
            const parsed = raw ? JSON.parse(raw) : null
            reviewCacheVal = parsed
            reviewCacheAt = Date.now()
          } catch {
            reviewCacheVal = null // JSON 损坏视作无 pending，下个缓存周期再试
            reviewCacheAt = Date.now()
          }
        }
        const memCount = (reviewCacheVal?.memories?.length ?? 0)
          + (reviewCacheVal?.knowledge?.length ?? 0)
          + (reviewCacheVal?.persona?.length ?? 0)
        // 已达下限、且没在本会话提醒过（reviewNoticeOnce=false 时每次都提）
        const alreadyNoticed = reviewNoticeAt.get(agent) === true
        const gated = cfg.reviewNoticeOnce && alreadyNoticed
        if (memCount >= Math.max(1, Number(cfg.reviewNoticeMin) || 1) && !gated) {
          reviewNoticeAt.set(agent, true)
          return {
            ...decision,
            messages: [...decision.messages, notice(
              `【灵枢审核】有 ${memCount} 条 AI 整理的记忆待确认（不急，有空 \`lingshu review\` 看一眼即可，不影响当前任务）。`,
            )],
          }
        }
      }

      // 3) 水位守卫（v3.01 设计 / v3.23 修估算口径）：用上面算好的 est/pct，
      //    首次跨过 60/75/90 各注入一条带真实数字的提醒。
      //    另加软自检：每 waterCheckTurns 轮若还没跨阈值，也戳一下，防止估算偏低时永远不响。
      if (cfg.waterGuard) {
        const st = waterState.get(agent) || { max: 0, softAt: -1, lastPct: null }
        if (pct != null) {
          // v3.57（保护 A 的写入端）：把真实水位落成 HOME/_pressure.json，注入侧
          // （memory.build_injection 读 core.read_pressure）据此整体降档。
          // 每跨 5 个百分点或跨档时写一次，避免每步都写盘；失败静默（不能拖住 pre-step）。
          writePressure(pct, est?.source || 'unknown', st.max || 0)

          // v3.57（保护 B）：95 / 98 两个硬档。90 仍是"建议"（用户可能还想继续干活），
          // 95 改命令式，98 起自动做**无破坏性**自救（auto-archive 只打归档标记、
          // diagnose 只报告），绝不自动 capture（拿不到原文）也绝不 purge（物理删除）。
          const th = pressureThresholds()
          if (pct >= th.force_at) {
            const did = await runForceActions(pythonExe, brainPy)
            if (st.forceAt !== true) {
              st.forceAt = true
              waterState.set(agent, st)
              return {
                ...decision,
                messages: [...decision.messages, notice(
                  `【灵枢·水位 ${pct}%】已到强制自救线（${th.force_at}%），已自动执行无破坏性压缩：${did || '无可用动作'}。`
                  + `现在只做三件事之一：① 让用户确认封存 capture；② 结束本轮话题；③ 继续也得先说明风险。不要再开新话题。`,
                )],
              }
            }
            return decision
          }
          if (pct >= th.hard_at && st.hardAt !== true) {
            st.hardAt = true
            waterState.set(agent, st)
            return {
              ...decision,
              messages: [...decision.messages, notice(
                `【灵枢·水位 ${pct}%】已过硬线（${th.hard_at}%）：立刻先跑 \`python scripts/brain.py dream --auto\``
                + `（或 \`auto-archive\`），压缩完再继续。不做压缩就继续对话，后面的内容很可能被宿主截断。`,
              )],
            }
          }

          const step = pct >= 90 ? 90 : pct >= 75 ? 75 : pct >= 60 ? 60 : 0
          if (step > (st.max || 0)) {
            st.max = step
            waterState.set(agent, st)
            const raw = await runBrain(pythonExe, cfg.python, brainPy, ['check-context', '--used', String(pct)], 15000)
            const text = raw ? raw.trim() : ''
            if (text) {
              return { ...decision, messages: [...decision.messages, notice(text)] }
            }
          }
          // 软自检（v3.59 重写）：原来"每 waterCheckTurns 轮无条件注入一条"，实测
          // 水位才 14% 也照发，用户会看到"没压缩却还在注入上下文"。现在只在
          // **确有变化**时提醒，其余情况只记账不占上下文：
          //   ① 从没测过水位（本会话首次拿到估算）；
          //   ② 估算比上次提醒时涨了 ≥10 个百分点（真的在变满）；
          //   ③ 距上次提醒已过 waterCheckTurns 轮，**且**确实在涨（≥2 个百分点）。
          // 另：文案里不再叫模型去跑 check-context——那会多一次工具往返，而这条
          // 提醒本身通常没到需要动作的程度。
          const every = Math.max(0, Number(cfg.waterCheckTurns) || 0)
          const lastPct = Number.isFinite(st.lastPct) ? st.lastPct : null
          const grew = lastPct === null ? Infinity : pct - lastPct
          const turnsSince = Number.isFinite(st.softAt) && st.softAt > 0 ? n - st.softAt : Infinity
          const worthNoticing = lastPct === null
            || grew >= 10
            || (every > 0 && turnsSince >= every && grew >= 2)
          if (worthNoticing && step === 0 && n > 0 && st.softAt !== n) {
            st.softAt = n
            st.lastPct = pct
            waterState.set(agent, st)
            return {
              ...decision,
              messages: [...decision.messages, notice(
                `【灵枢·水位】上下文估算约 ${pct}%（来源：${est.source}）。还没到清点线，正常继续即可。`
              )],
            }
          }
          // 不提醒也要更新"上次见过的水位"，下次才有得比
          if (lastPct !== pct) {
            st.lastPct = pct
            waterState.set(agent, st)
          }
        } else {
          waterState.set(agent, st)
        }
      }

      return decision
    } catch {
      // next() 可能已跑过，异常只能吞掉，返回已得的 decision（可能为 undefined）
      return decision
    }
  })

  // 插件卸载/进程退出前把脏状态刷盘，避免节流窗口内的轮数/任务标记丢失
  const flushOnExit = () => { try { stateSaver.flushNow() } catch {} }
  ctx.on('agent/dispose', flushOnExit)
  try { process.on('exit', flushOnExit) } catch {}
}
