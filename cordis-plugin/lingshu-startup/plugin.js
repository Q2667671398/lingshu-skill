// 灵枢 · DSH/Cordis 启动注入插件
//
// 为什么需要它：DSH 的技能 catalog 只把 name+description 给模型，正文要不要读
// 全凭模型自觉。本插件把"自觉"变成"机制"——每个新会话（agent/created，
// source ∈ startup/resume）时自动跑灵枢的 `brain.py startup`，把人格、记忆、
// 待办、行为指引注入会话上下文，模型想忘都忘不掉。
//
// 事件与注入（对齐 DSH 真实 API）：
// - 监听 `agent/created`（serial 事件，监听器会被 await，失败会回滚会话创建——
//   所以本插件任何路径都不允许抛异常），payload 为
//   { agent, source: 'startup'|'resume'|'clear'|'compact', signal? }。
//   只对 startup/resume 注入；clear/compact 是同会话重建上下文，不重复注入。
// - 投递用 agent/pre-step 钩子：agent/created 里只跑 startup 并把文本存进
//   pending Map；pre-step（构建 messages 之后、请求之前）await next() 后把文本
//   追加进 enter 分支的 messages——不排队、不经过 inbox 消费逻辑，必然进模型
//   上下文。此前试过 inject（写 next-step，turn/start 清空丢注入）与
//   send('next-turn')（进待发送队列，模型看不到），都实测失败。
//
// 数据安全：本插件只读记忆库（startup 内部只读+写 last_startup），不碰
// 技能目录；brain.py 自身保证只读降级（HOME 不可写时照样注入）。

import { spawn } from 'node:child_process'
import { existsSync, rmSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { delimiter, dirname, join } from 'node:path'
import { randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'

// 刻意零外部依赖：不 import @deepseek-ai/schemastery。原因——link: 安装时模块
// 真实路径留在技能目录，Node 从那里向上找 node_modules 一路到盘符根都找不到
// DSH 内置包，import 失败 → cordis 静默跳过整个插件（UI 里装了却不动）。
// 去掉依赖后 link 安装也能工作，改 plugin.js 即时生效，不必每次重打包。

export const name = 'dsh-lingshu-startup'

// 本插件文件在 <包根>/cordis-plugin/lingshu-startup/plugin.js，
// 往上一级是 cordis-plugin/，再上一级就是包根（含 scripts/brain.py 与 SKILL.md）。
const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url))
const PACKAGE_ROOT = join(PLUGIN_DIR, '..', '..')

/**
 * 把"找不到 brain.py / python"这类静默失败写进 ~/.dsh/lingshu-diag.json。
 * 为什么需要：插件里所有失败都必须吞掉（事件串行、抛异常会回滚会话），
 * 结果就是"记忆不录入了"却没有任何痕迹可查——2026-11-20 那次排障就卡在这。
 */
export function writeDiag(kind, detail) {
  try {
    const base = process.env.APP_DSH || join(homedir(), '.dsh')
    const payload = {
      ts: new Date().toISOString(),
      plugin: 'lingshu-startup',
      kind,
      detail: detail && typeof detail === 'object' ? detail : { message: String(detail ?? '') },
    }
    writeFileSync(join(base, 'lingshu-diag.json'), JSON.stringify(payload, null, 2), 'utf8')
  } catch {}
}

// 默认配置（纯普通对象，零依赖）。DSH 对导出 Config 的插件会喂用户配置，
// 不导出 Config 时喂默认空对象——两种情况本插件都能工作。
const DEFAULTS = {
  brainPy: '',     // brain.py 绝对路径；留空自动探测 ~/.dsh/skills/lingshu、~/.trae-cn/skills/lingshu
  python: '',      // python.exe 绝对路径；留空自动探测 DSH 自带运行时等位置（spawn 不解析 PATHEXT）
  budget: 1400,    // startup 注入 token 预算
  timeoutMs: 3000, // startup 子进程超时；agent/created 是串行事件，超时太久拖慢新会话
  injectResume: true, // resume 来源（续聊）是否也注入
}

/** 合并用户配置与默认值（用户值非空/存在才覆盖） */
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

/** 依次探测常见安装位置的 brain.py */
async function findBrainPy(configured, ctx) {
  if (configured) return existsSync(configured) ? configured : null
  // ① 先看本包自带：插件就在 <包根>/cordis-plugin/lingshu-startup/ 里，
  //    ../../scripts/brain.py 一定和它同包发布。这一条不依赖任何宿主状态，
  //    也不依赖"技能注册表带路径"（v3.53 之前正是缺了这一条：注册 runtime 技能
  //    时没给 resourceBase/path，子插件就永远找不到 brain.py，整条自动注入/
  //    即记即存/天级任务全部静默停摆）。
  for (const p of brainPyCandidates()) {
    if (existsSync(p)) return p
  }
  // ② 再问技能注册表（装了全局技能副本时更准；字段名跨版本不同，逐个试）
  try {
    const registry = ctx?.get?.('skills') ?? ctx?.skills
    let items = registry?.list?.()
    if (items?.then) items = await items
    const hit = Array.isArray(items) ? items.find((s) => s?.name === 'lingshu') : null
    if (hit) {
      const bases = [
        [hit.path, true], [hit.location, true],
        [hit?.resourceBase?.path, false], // DSH 的 runtime 技能可带资源根目录
        [hit.dir, false], [hit.baseDir, false], [hit.directory, false],
      ]
      for (const [raw, isFile] of bases) {
        if (typeof raw !== 'string' || !raw) continue
        const root = isFile ? dirname(raw) : raw
        const p = join(root, 'scripts', 'brain.py')
        if (existsSync(p)) return p
      }
    }
  } catch (e) {
    // 技能注册表探测失败不该静默——留一条日志方便排障（但不在群/会话里打扰）
    try { ctx?.logger?.warn?.('lingshu-startup: 技能注册表探测异常，走环境变量/默认路径兜底', e) } catch {}
  }
  return null
}

/** 有可能藏着 brain.py 的绝对路径清单（纯函数，便于测试与排障打印） */
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

/** 有可能可用的 python 可执行文件清单（纯函数，便于测试与排障打印） */
export function pythonCandidates() {
  const home = homedir()
  const out = []
  for (const app of [process.env.APP_DSH, join(home, '.dsh')].filter(Boolean)) {
    out.push(join(app, 'app-runtimes', 'app-primary-runtime', 'dependencies', 'python', 'python.exe'))
    out.push(join(app, 'runtimes', 'app-primary-runtime', 'dependencies', 'python', 'python.exe'))
  }
  // PATH 里的 python.exe / python3.exe（ServBay、Store、conda 等布局都能命中）。
  // 刻意不选 .cmd shim：spawn 走 CreateProcess 不解析 PATHEXT。
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

/**
 * 探测可用的 python.exe。spawn 走 CreateProcess，不解析 PATHEXT——
 * PATH 里只有 python.cmd 的机器上 spawn('python') 直接 ENOENT。
 * 优先级：用户配置 > DSH 自带运行时（确定的 exe）> 常见安装位置 > 系统常见位置。
 * 都没命中返回 null（由调用方走 cmd 兜底）。
 */
function findPythonExe(configured) {
  if (configured) return existsSync(configured) ? configured : null
  for (const p of pythonCandidates()) {
    if (existsSync(p)) return p
  }
  return null
}

/**
 * 跑 brain.py startup，返回注入文本；失败返回 null（绝不抛异常、绝不阻塞会话）。
 * 找不到 python.exe 时退回 ComSpec 让 cmd 解析 PATHEXT（python.cmd 也能跑）。
 */
function runStartup(pythonExe, pythonCmdFallback, brainPy, budget, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false
    const done = (v) => { if (!settled) { settled = true; resolve(v) } }
    const args = [brainPy, 'startup', '--budget', String(budget)]
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
          // 不用 spawn 的 timeout 选项：它只杀直接子进程，见下方手动兜底说明
        })
      } else {
        // 兜底：让 cmd 去解析 .cmd（/d 关 autorun，/s 规范引号解析，/c 执行后退出）。
        // 注意命令行必须带上 python 命令本身（pythonExe 没找到时用裸 'python'，
        // 由 cmd 按 PATHEXT 解析成 python.cmd）——只拼 brain.py 会依赖 .py 文件关联。
        const comspec = process.env.ComSpec ?? 'cmd.exe'
        const pythonCmd = pythonCmdFallback || 'python'
        const cmdline = [`"${pythonCmd}"`, ...args.map((a) => `"${a}"`)].join(' ')
        child = spawn(comspec, ['/d', '/s', '/c', cmdline], {
          windowsHide: true,
          windowsVerbatimArguments: true, // 命令行已手工加引号，阻止 Node 再转义一层
          env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
          cwd: homedir(), // 同上：不把宿主 cwd（可能在插件目录内）占给子进程
        })
      }
    } catch {
      return done(null)
    }
    let out = ''
    child.stdout?.on('data', (d) => { out += d.toString('utf8') })
    child.stderr?.resume() // 必须 drain：traceback 超 64KB 会塞满管道缓冲，子进程写阻塞永不退出
    child.on('error', () => done(null))
    // 手动超时兜底（不靠 spawn 的 timeout 选项，它只杀直接子进程）：
    // taskkill /T /F 杀整棵进程树——cmd 兜底分支里直接子进程是 cmd.exe，
    // 只杀它会留下 python.exe 孙进程残留。
    const killer = setTimeout(() => {
      try {
        if (child.pid) {
          const tk = spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
          tk.on('error', () => {}) // 无 taskkill（非 Windows）时静默，下面还有 child.kill() 托底
        }
      } catch {}
      try { child.kill() } catch {}
      done(null)
    }, timeoutMs + 500)
    killer.unref?.()
    child.on('close', (code) => {
      clearTimeout(killer) // 正常退出清掉兜底定时器，别留着空转
      // 返回码 0=正常注入；10=只读降级注入（照样有效）；2=未初始化（跳过）
      done(code === 0 || code === 10 ? out.trim() : null)
    })
  })
}

export function apply(ctx, config) {
  // v3.23：重复挂载守卫。profile 同时启用独立包与根包时，同一进程会出现两份 startup，
  // 每会话注入两遍人格/记忆（白烧 ~3k token，且两份可能来自不同版本引擎）。
  if (globalThis.__lingshuStartupApplied) {
    try { ctx.logger?.warn?.('lingshu-startup: 已有一份灵枢 startup 在运行，跳过重复挂载') } catch {}
    return
  }
  globalThis.__lingshuStartupApplied = true
  const cfg = mergeConfig(config)
  // pending：agent -> 注入文本。agent/created 只负责把文本存进来，
  // 真正投递在 agent/pre-step 里做（见下）。
  const pending = new Map()
  // 配置类失败只告警一次：静默吞掉配置错误会让插件"看似在工作、实际没注入"
  let warnedOnce = false

  ctx.on('agent/created', async (payload) => {
    try { // 顶层兜底：serial 监听器 reject 会回滚会话创建，这里绝不允许冒泡
      const { agent, source } = payload || {}
      if (!agent) return
      // 白名单：只有新开会话（startup）与续聊（resume）注入；
      // clear / compact 是同会话上下文重建，重复注入只会浪费 token。
      if (source !== 'startup' && source !== 'resume') return
      if (source === 'resume' && !cfg.injectResume) return
      if (pending.has(agent)) return

      const brainPy = await findBrainPy(cfg.brainPy, ctx)
      if (!brainPy) {
        // 静默失败必须有痕迹：doctor / 面板 / 手工排障都读这个文件
        writeDiag('brainPy-not-found', {
          tried: brainPyCandidates(),
          packageRoot: PACKAGE_ROOT,
          configured: cfg.brainPy || null,
        })
        if (!warnedOnce) { warnedOnce = true; try { ctx.logger?.warn?.('lingshu-startup: 未找到 brain.py（未安装灵枢？），跳过注入') } catch {} }
        return
      }
      const pythonExe = findPythonExe(cfg.python)
      const text = await runStartup(pythonExe, cfg.python, brainPy, cfg.budget, cfg.timeoutMs)
      if (!text) {
        writeDiag('startup-no-output', {
          brainPy,
          pythonExe: pythonExe || null,
          pythonConfigured: cfg.python || null,
          pythonTried: pythonCandidates().slice(0, 12),
        })
        if (!warnedOnce) { warnedOnce = true; try { ctx.logger?.warn?.('lingshu-startup: brain.py startup 未产出注入文本（python 不在/超时/未初始化），跳过注入') } catch {} }
        return
      }
      // 成功时清掉上一次的失败痕迹，避免旧 diag 一直误导排障
      try {
        const base = process.env.APP_DSH || join(homedir(), '.dsh')
        rmSync(join(base, 'lingshu-diag.json'), { force: true })
      } catch {}
      // 只存不投递：send/inject 走 inbox 都有生命周期坑（实测坐实），真正进模型
      // 上下文要在 agent/pre-step 里追加 messages（见下）。
      pending.set(agent, text)
    } catch (e) {
      // 任何意外都不能让监听器 reject（agent/created 失败会回滚会话创建）
      try { ctx.logger?.warn?.('lingshu-startup: 未捕获异常', e) } catch {}
    }
  })

  // 每步构建 messages 之后、发起请求之前：把注入文本追加进这批 messages。
  // 这条通道不排队、不经过 inbox 消费逻辑——enter 分支的 messages 就是模型看到的。
  ctx.on('agent/pre-step', async (payload, next) => {
    try {
      // 先 await next()：让其他（wrapping）监听器先改完，保住它们的
      // startsRequestSeries / batch 语义，再在结果上追加。
      const decision = await next()
      if (!decision || decision.kind !== 'enter') return decision
      const { agent } = payload || {}
      const text = agent && pending.get(agent)
      if (!text) return decision
      pending.delete(agent) // 只注入一次：不删的话每条 pre-step 都会重复塞
      return {
        ...decision,
        messages: [...decision.messages, {
          id: randomUUID(),
          role: 'user',
          content: [{ type: 'text', text }],
          source: { kind: 'skill-invocation', name: 'lingshu', form: 'instructions' },
        }],
      }
    } catch (e) {
      try { ctx.logger?.warn?.('lingshu-startup: pre-step 注入失败', e) } catch {}
      // next() 可能已经跑过，无法安全重放；异常只能吞掉，不能冒泡
    }
  })
}
