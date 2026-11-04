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
// 且可整体关闭（injectReview / injectCheckContext = false）——
// 保证不影响"人格/记忆/灵魂 + AI 自主自动化"的主功能。

import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync, rmSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { randomUUID } from 'node:crypto'

export const name = 'dsh-lingshu-cron'

const DEFAULTS = {
  brainPy: '',            // brain.py 绝对路径；留空自动探测（与 lingshu-startup 同一套逻辑）
  python: '',             // python.exe 绝对路径；留空自动探测（spawn 不解析 PATHEXT）
  timeoutMs: 120000,      // 后台任务子进程超时（做梦/备份可能慢，2 分钟兜底）
  dreamTurns: 8,          // 每 N 轮触发 dream --auto
  extractTurns: 4,        // 每 N 轮把攒下的对话文本静默 extract（即记即存）
  extractMinChars: 80,    // 攒的文本短于该值不触发（惊异门控在 brain.py extract 里有第二道）
  checkContextTurns: 5,   // 每 N 轮触发 check-context 注入
  selfReviewDays: 3,      // 每 N 天触发 skill self-review
  runUsage: true,         // 每轮 turn-stopping 静默跑 usage --reply
  injectReview: true,     // review 有 pending 时注入提醒（每轮有内容才注）
  injectCheckContext: true, // check-context 建议时注入提醒（每 N 轮）
  reviewCacheMs: 30000,   // review --json 结果缓存时长（pre-step 每步都跑，别真跑）
  pruneInjected: true,    // 上下文无感清理：把灵枢自己注入过的旧提醒从消息流里清掉
                          // （原文已被 extract 封存海马体，清掉 = 归档后遗忘，不丢信息）
}

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
  try {
    const registry = ctx?.get?.('skills') ?? ctx?.skills
    let items = registry?.list?.()
    if (items?.then) items = await items
    const hit = Array.isArray(items) ? items.find((s) => s?.name === 'lingshu') : null
    if (hit) {
      for (const [field, isFile] of [
        ['path', true], ['location', true], ['dir', false],
        ['baseDir', false], ['directory', false],
      ]) {
        const raw = hit[field]
        if (typeof raw !== 'string' || !raw) continue
        const root = isFile ? dirname(raw) : raw
        const p = join(root, 'scripts', 'brain.py')
        if (existsSync(p)) return p
      }
    }
  } catch {}
  const envDir = (process.env.LINGSHU_SKILL_DIR || '').trim()
  if (envDir) {
    const p = join(envDir, 'scripts', 'brain.py')
    if (existsSync(p)) return p
  }
  const home = homedir()
  for (const dir of [
    join(home, '.dsh', 'skills', 'lingshu'),
    join(home, '.trae-cn', 'skills', 'lingshu'),
  ]) {
    const p = join(dir, 'scripts', 'brain.py')
    if (existsSync(p)) return p
  }
  return null
}

function findPythonExe(configured) {
  if (configured) return existsSync(configured) ? configured : null
  const home = homedir()
  const candidates = []
  const appHomes = [process.env.APP_HOME, join(home, '.dsh')].filter(Boolean)
  for (const app of appHomes) {
    candidates.push(
      join(app, 'app-runtimes', 'app-primary-runtime', 'dependencies', 'python', 'python.exe'),
      join(app, 'runtimes', 'app-primary-runtime', 'dependencies', 'python', 'python.exe'),
    )
  }
  candidates.push(
    join(home, 'AppData', 'Local', 'Programs', 'Python', 'python.exe'),
    'C:\\Python313\\python.exe',
    'C:\\Python312\\python.exe',
    'C:\\Program Files\\Python313\\python.exe',
  )
  for (const p of candidates) {
    if (existsSync(p)) return p
  }
  return null
}

// ---------------------------------------------------------------- 状态落盘（APP profile 目录，不碰 ~/.brain-memory）

function statePath() {
  const base = process.env.APP_HOME || join(homedir(), '.dsh')
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
        })
      } else {
        const comspec = process.env.ComSpec ?? 'cmd.exe'
        const pythonCmd = pythonCmdFallback || 'python'
        const cmdline = [`"${pythonCmd}"`, ...args.map((a) => `"${a}"`)].join(' ')
        child = spawn(comspec, ['/d', '/s', '/c', cmdline], {
          windowsHide: true,
          windowsVerbatimArguments: true,
          env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
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
    source: { kind: 'skill-invocation', name: 'lingshu', form: 'instructions' },
  }
}

// ---------------------------------------------------------------- 插件主体

export function apply(ctx, config) {
  const cfg = mergeConfig(config)

  const turns = new Map()   // agent -> 本会话轮次
  const running = new Set() // 防并发：同名任务进行中就不重入
  const convo = new Map()   // agent -> { text, seen }（即记即存缓冲 + 已处理消息游标，extract 成功后清空）
  const lastReply = new Map() // agent -> 最后一条 assistant 文本（给 usage --reply 用）
  let state = loadState(statePath())
  let brainPyCache = null
  let warnedOnce = false
  let warnedNoBrain = false

  // review --json 缓存（pre-step 每步都问，30s 内复用）
  let reviewCacheAt = 0
  let reviewCacheVal = null

  let turnStateSeed = null // 状态文件里的 {agentKey: n}，turn-stopping 里对号入座
  // 重启恢复：把上次的累计轮数灌进 seed（turn-stopping 里按 agentKey 对号入座续算）
  try {
    const savedTurns = state?.turns
    if (savedTurns && typeof savedTurns === 'object') {
      turnStateSeed = savedTurns
    }
  } catch {}

  const warn = (msg) => { if (!warnedOnce) { warnedOnce = true; try { ctx.logger?.warn?.(msg) } catch {} } }

  async function ensureBrainPy() {
    if (brainPyCache) return brainPyCache
    const found = await findBrainPy(cfg.brainPy, ctx)
    if (!found) {
      if (!warnedNoBrain) { warnedNoBrain = true; warn('lingshu-cron: 未找到 brain.py（未安装灵枢？），调度跳过') }
      return null
    }
    brainPyCache = found
    return found
  }

  /** fire-and-forget：绝不 await、失败吞掉、成功才标记完成并落盘 */
  function fire(task) {
    if (running.has(task.id)) return
    running.add(task.id)
    ;(async () => {
      try {
        const brainPy = await ensureBrainPy()
        if (!brainPy) return
        const pythonExe = findPythonExe(cfg.python)
        await runBrain(pythonExe, cfg.python, brainPy, task.argv, cfg.timeoutMs)
        // 无论退出码如何都标记"已触发"：失败重跑会造成备份/做梦风暴；
        // 例行任务明天/下个周期再来一次就好。
        if (task.days) {
          state.days = state.days || {}
          state.days[task.id] = todayStr()
          saveState(statePath(), state)
        }
      } catch {} // 顶层兜底：任何异常静默吞掉
      finally {
        running.delete(task.id)
      }
    })()
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
        const base = process.env.APP_HOME || join(homedir(), '.dsh')
        mkdirSync(base, { recursive: true })
        const tmp = join(base, `lingshu-extract-${randomUUID()}.txt`)
        try {
          writeFileSync(tmp, text, 'utf8')
          const pythonExe = findPythonExe(cfg.python)
          await runBrain(pythonExe, cfg.python, brainPy,
            ['extract', '--file', tmp, '--min-chars', String(cfg.extractMinChars)],
            cfg.timeoutMs)
          ok = true // 退出码 0/10/其他都算"跑过了"——去重保证重试也无害，但别无限重试
        } finally {
          try { rmSync(tmp, { force: true }) } catch {}
        }
      } catch {} // 顶层兜底
      finally {
        if (ok) convo.set(agent, { text: '', seen: buf?.seen }) // 清空缓冲，但保留游标（防止重复攒旧消息）
        else if (text.length > 200_000) convo.set(agent, { text: '', seen: buf?.seen }) // 兜底防爆内存
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
      // 重启恢复：状态文件里有该 agent 的累计轮数就续算，没有才从 0 开始
      let n = (turns.get(agent) ?? 0) + 1
      if (!turns.has(agent) && ak && turnStateSeed && Number.isFinite(turnStateSeed[ak])) {
        n = Number(turnStateSeed[ak]) + 1
      }
      turns.set(agent, n)
      // 轮数落盘：重启后按"已累计轮数"续算，避免每次重启都重新数 N 轮才触发
      // dream / extract（ak 为 null 时拿不到稳定标识，退回纯内存计数）
      if (ak) {
        state.turns = state.turns || {}
        if (state.turns[ak] !== n) {
          state.turns[ak] = n
          saveState(statePath(), state)
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

  // 注入类：只在 enter 分支追加消息，有内容才注，缓存 review 查询
  ctx.on('agent/pre-step', async (payload, next) => {
    let decision
    try {
      decision = await next()
      if (!decision || decision.kind !== 'enter') return decision
      const { agent } = payload || {}
      if (!agent) return decision

      // 上下文无感清理：把此前由本插件注入的旧提醒消息清掉。
      // 只识别 source.name === 'lingshu' 的注入消息（本插件唯一的"私有领地"），
      // 绝不碰用户/AI 的原始对话、不碰 APP 的会话管理——这些提醒的原文
      // 已经通过 extract 沉进海马体，清掉只是"归档后遗忘"，信息不丢。
      try {
        if (cfg.pruneInjected) {
          const msgs = Array.isArray(decision.messages) ? decision.messages : []
          const kept = msgs.filter((m) => m?.source?.name !== 'lingshu')
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

      // 1) review 提醒：每轮都查（走缓存），有 pending 才注
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
        if (memCount > 0) {
          return {
            ...decision,
            messages: [...decision.messages, notice(
              `【灵枢审核】有 ${memCount} 条 AI 整理的记忆待确认。先运行 lingshu review 确认或丢弃，再继续当前任务。`,
            )],
          }
        }
      }

      // 2) check-context 提醒：每 N 轮查一次，灵枢按 --used 给分级建议
      if (cfg.injectCheckContext && cfg.checkContextTurns > 0 && n % cfg.checkContextTurns === 0) {
        // 上下文用量这里拿不到精确值，用保守的"轮次触发 + 让灵枢自己判"
        const raw = await runBrain(pythonExe, cfg.python, brainPy, ['check-context', '--used', '60'], 15000)
        const text = raw ? raw.trim() : ''
        if (text) {
          return {
            ...decision,
            messages: [...decision.messages, notice(text)],
          }
        }
      }

      return decision
    } catch {
      // next() 可能已跑过，异常只能吞掉，返回已得的 decision（可能为 undefined）
      return decision
    }
  })
}
