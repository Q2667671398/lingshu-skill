// 灵枢 · 群聊发言人鉴权插件（lingshu-guard）
//
// 为什么需要它：dsh-im 把 QQ/微信/飞书等 IM 会话接进 DSH 后，群聊里任何成员都
// 能跟模型聊天——但模型手里的工具是宿主的全部能力：shell、写文件、subagent……
// 群友一句「帮我执行」就等于把终端交出去了。本插件在工具执行前按「发言人是谁」
// 做鉴权：
//   * 桌面会话（没有 IM 绑定）不受影响，工具照常；
//   * 群聊/私聊里的 owner 放行一切工具；
//   * 其他人只放行只读工具，其余拒绝；拿不到发言人身份时 fail-closed。
//
// 机制（签名读自 app.asar 内 @deepseek-ai/dsh-tools 源码，别凭记忆改）：
//   src/index.ts       guard(guard: ToolGuard): () => void
//   src/contracts.ts   type ToolGuard = (execution: Readonly<ToolExecution>) => string | ToolGuardDenial | undefined
//                      interface ToolGuardDenial { readonly reason: string; readonly info?: ToolErrorInfo }
//   src/pipeline.ts    pre-execute → 审批 → checkGuards → 工具体；
//                      guard 返回 string 即拒绝（作为模型可见的失败原因），
//                      返回 undefined 放行；守卫是单调的（后注册监听器翻不了案）。
// 所以：优先 ctx.tools.guard(fn)，在 agent/created 时注册到该 agent 的 ctx 上
// （tools.guard 的 effect 归 agent 作用域所有，agent 销毁自动清理；guard 同时
// 覆盖该 agent 派生的子 agent）。宿主太旧没有 tools.guard 时退回
// agentCtx.on('tools/pre-execute', …)（waterfall，同样随 agent 作用域清理）。
// 两种路径都返回明确的 deny 原因；不存在「只能返回布尔」的 API。
//
// 身份（编码与 cordis-plugin/lingshu-im/plugin.js 的 toIdentity 完全一致）：
//   channel = encodeURIComponent(platform) + '@' + encodeURIComponent(botId)
//   user    = channel + ':' + encodeURIComponent(senderId)
//   group   = chatType !== 'dm' 时取 chatId，私聊为空
// 两路来源互补（与 lingshu-im 相同，事件先到/服务后加载都能覆盖）：
//   1. dshIm.describeConversationSync(sessionId)  ← 宿主只读服务（同步反查）
//   2. dsh-im/session-pre-bind 事件缓存           ← 绑定瞬间的 channel/sender
// groups 的 conversationKey 解析不出 senderId 时会话侧为 null，恰好靠事件缓存
// 和 describe 的 authors 快照补齐；两路都没有 senderId 就按 fail-closed 拒绝。
//
// 配置（~/.brain-memory/config.json 的 guard 段；支持 BRAIN_MEMORY_HOME 与 .home
// 指针；宿主透传的 config 优先级更高）：
//   { "guard": { "enabled": true, "shadow": true, "owners": [],
//                "readOnlyAllow": ["read", ...], "logPath": "" } }
//   * owners：缺省（未配或空数组）回退同文件顶层 owners 数组——与 CLI 的
//     channel 身份编码一致（形如 "qq@qq_xxx:C858…"），不用再维护第二份名单。
//   * shadow=true（默认）：只记日志不拦，先观察再收紧。
//
// 日志：JSONL，默认 ~/.brain-memory/lingshu-guard.log，字段
//   {ts, action:'allow'|'deny'|'shadow-deny', tool, sender, channel, group}
// 只记录 IM 会话的判定；桌面会话不产生日志。

import { appendFileSync, mkdirSync, readFileSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

export const name = 'dsh-lingshu-guard'

// 默认只读白名单：只读/无副作用工具。刻意不含 write/edit/pwsh/subagent/workflow。
const DEFAULT_READ_ONLY = [
  'read',
  'glob',
  'grep',
  'web_search',
  'list_agents',
  'skill',
  'todo_write',
  'ask_user_question',
]

// guard 段默认值。shadow 默认 true：先观察日志、确认不误伤，再手动关掉收紧。
const DEFAULT_GUARD = {
  enabled: true,
  shadow: true,
  readOnlyAllow: null, // null = 用 DEFAULT_READ_ONLY
  logPath: '',
}

const enc = (v) => encodeURIComponent(String(v ?? ''))
const asText = (v) => String(v ?? '').trim()

// ---------------------------------------------------------------- 配置

/** 记忆目录：BRAIN_MEMORY_HOME > ~/.brain-memory/.home 指针 > ~/.brain-memory。 */
function memoryHome() {
  const env = asText(process.env.BRAIN_MEMORY_HOME)
  if (env) return env
  const base = join(homedir(), '.brain-memory')
  try {
    const ptr = readFileSync(join(base, '.home'), 'utf8').trim()
    if (ptr) return ptr
  } catch { /* 没有指针文件就用默认目录 */ }
  return base
}

// config.json 很小但每次工具调用都读不划算：按 mtime 缓存一份（brain.py 改完
// 配置、文件 mtime 变化后下一次调用即生效，不用重启宿主）。
let configCache = { path: '', mtime: -1, data: {} }

function readBrainConfig() {
  try {
    const file = join(memoryHome(), 'config.json')
    let mtime = -1
    try { mtime = statSync(file).mtimeMs } catch { mtime = -1 }
    if (configCache.path === file && configCache.mtime === mtime) return configCache.data
    let data = {}
    if (mtime >= 0) {
      try {
        const parsed = JSON.parse(readFileSync(file, 'utf8'))
        data = parsed && typeof parsed === 'object' ? parsed : {}
      } catch { data = {} }
    }
    configCache = { path: file, mtime, data }
    return data
  } catch {
    return {}
  }
}

function stringList(value) {
  if (!Array.isArray(value)) return []
  return value
    .filter((x) => typeof x === 'string' || typeof x === 'number')
    .map((x) => String(x).trim())
    .filter(Boolean)
}

/**
 * 合并默认值、config.json 的 guard 段与宿主透传 config。
 * 宿主 config（profile/bundle 里显式写的）优先；空字符串视为未配置。
 */
function resolveGuardConfig(hostConfig) {
  const file = readBrainConfig()
  const fileGuard = file?.guard && typeof file.guard === 'object' ? file.guard : {}
  const host = hostConfig && typeof hostConfig === 'object' ? hostConfig : {}
  const pick = (key) => {
    const h = host[key]
    if (h !== undefined && h !== null && h !== '') return h
    return fileGuard[key]
  }
  const enabled = pick('enabled') === undefined ? DEFAULT_GUARD.enabled : pick('enabled') !== false
  const shadow = pick('shadow') === undefined ? DEFAULT_GUARD.shadow : pick('shadow') !== false
  // owners：guard 段没配/为空时回退同文件顶层 owners（CLI 用的那份名单）。
  let owners = stringList(pick('owners'))
  if (!owners.length) owners = stringList(file?.owners)
  // readOnlyAllow：配了数组就照用（显式 [] = 一个只读工具都不放），否则用默认表。
  const allowRaw = pick('readOnlyAllow')
  const readOnlyAllow = Array.isArray(allowRaw) ? stringList(allowRaw) : DEFAULT_READ_ONLY
  const logPathRaw = pick('logPath')
  const logPath = asText(logPathRaw) || join(memoryHome(), 'lingshu-guard.log')
  return { enabled, shadow, owners, readOnlyAllow, logPath }
}

// ---------------------------------------------------------------- 身份

/** Harness session id：agent.session.id（与 lingshu-im 一致），其余字段兜底。 */
function sessionIdOf(agent) {
  const s = agent?.session
  for (const v of [s?.id, agent?.sessionId, agent?.id]) {
    if (typeof v === 'string' && v) return v
  }
  return null
}

/**
 * 把绑定/服务返回的 conversation 描述归一成灵枢身份三元组（最小复刻
 * lingshu-im 的 toIdentity，不 import 它以免循环依赖）。
 * 返回 null 表示「有 IM 绑定但拿不到发言人身份」——调用方按 fail-closed 处理。
 */
function toIdentity(described) {
  if (!described || typeof described !== 'object') return null
  const channelName = asText(described.channel)
  if (!channelName) return null
  const botId = asText(described.botId)
  const channel = botId ? `${enc(channelName)}@${enc(botId)}` : enc(channelName)
  const senderId = asText(described.senderId)
  if (!senderId) return null
  const user = `${channel}:${enc(senderId)}`
  const chatType = asText(described.chatType)
  const chatId = asText(described.chatId)
  const group = chatType && chatType !== 'dm' ? chatId : ''
  return { channel, user, group, channelName, botId, chatType, chatId, senderId }
}

/** 取身份解析服务（宿主可能没装 dsh-im，或服务还没就绪）。 */
function dshImService(ctx) {
  try {
    return ctx?.get?.('dshIm') ?? null
  } catch {
    return null
  }
}

/**
 * 合并事件缓存与 describeConversationSync 的结果。
 * 合并规则：服务侧字段覆盖事件侧；但服务侧的空值（null/''）不覆盖事件侧——
 * 群聊 conversationKey 解析不出 senderId 时服务侧是 null，而 session-pre-bind
 * 事件里的 authors 快照可能带 senderId，不能把它抹掉。
 */
function describeFor(ctx, bindings, sessionId) {
  if (!sessionId) return null
  let base = bindings.get(sessionId) || null
  let live = null
  try {
    const svc = dshImService(ctx)
    if (svc && typeof svc.describeConversationSync === 'function') {
      live = svc.describeConversationSync(sessionId)
    }
  } catch {
    live = null
  }
  if (live && typeof live !== 'object') live = null
  if (!base) return live
  if (!live) return base
  const merged = { ...base, ...live }
  for (const key of [
    'channel', 'botId', 'chatType', 'chatId', 'threadId',
    'conversationKey', 'senderId', 'senderName',
  ]) {
    const v = merged[key]
    if ((v === null || v === undefined || v === '') && base[key] !== undefined) {
      merged[key] = base[key]
    }
  }
  return merged
}

/** owners 匹配：整身份（CLI 形态）、原始 senderId、platform:senderId 都算。 */
function isOwner(owners, id) {
  if (!owners.length || !id) return false
  const candidates = new Set([
    id.user,
    id.senderId,
    `${id.channelName}:${id.senderId}`,
  ])
  if (id.botId) candidates.add(`${id.channelName}@${id.botId}:${id.senderId}`)
  for (const owner of owners) {
    if (candidates.has(owner)) return true
  }
  return false
}

/** 日志里的身份字段（身份不完整时也尽量给出通道/群，便于排查）。 */
function logFields(id, described) {
  const channelName = asText(described?.channel) || id?.channelName || ''
  const botId = asText(described?.botId) || id?.botId || ''
  const channel = id?.channel
    || (channelName ? (botId ? `${enc(channelName)}@${enc(botId)}` : enc(channelName)) : '')
  const rawSender = asText(described?.senderId)
  const sender = id?.user || (rawSender ? `${channel}:${enc(rawSender)}` : '')
  const chatType = asText(described?.chatType) || id?.chatType || ''
  const group = id?.group || (chatType && chatType !== 'dm' ? asText(described?.chatId) : '')
  return { sender, channel, group }
}

// ---------------------------------------------------------------- 日志

function appendLog(logPath, entry) {
  try {
    mkdirSync(dirname(logPath), { recursive: true })
    appendFileSync(logPath, JSON.stringify(entry) + '\n', 'utf8')
  } catch { /* 日志失败不能拖垮工具调用 */ }
}

function nowIso() {
  return new Date().toISOString()
}

// ---------------------------------------------------------------- 策略

function senderLabel(id) {
  return id.senderName ? `${id.senderName}(${id.senderId})` : id.senderId
}

function denyReason(id, tool) {
  const where = id.group ? `群 ${id.group}` : '私聊'
  return `灵枢·guard 拒绝：IM 发言人 ${senderLabel(id)}（通道 ${id.channel}，${where}）`
    + `不在 owners 白名单，只允许只读工具（readOnlyAllow）；工具 "${tool}" 有副作用，已拒绝。`
}

function failClosedReason(tool) {
  return `灵枢·guard 拒绝：这是 IM 绑定会话，但拿不到发言人身份（channel/senderId 缺失），`
    + `无法确认是谁在调用工具，按 fail-closed 拒绝 "${tool}"。`
}

function denyOrShadow(cfg, tool, fields, reason) {
  const action = cfg.shadow ? 'shadow-deny' : 'deny'
  appendLog(cfg.logPath, { ts: nowIso(), action, tool, ...fields })
  return cfg.shadow ? undefined : reason
}

/**
 * 核心判定（同步）：返回 string 表示拒绝原因，undefined 表示放行。
 * 任何内部异常都 fail-closed（影子模式除外），绝不静默放行。
 */
function checkTool(ctx, bindings, hostConfig, exec, agent) {
  let cfg
  try {
    cfg = resolveGuardConfig(hostConfig)
  } catch {
    return '灵枢·guard 拒绝：鉴权配置读取失败，按 fail-closed 拒绝。'
  }
  if (!cfg.enabled) return undefined

  const tool = asText(exec?.name)
  const subject = agent || exec?.agent || null

  let described = null
  let id = null
  let resolveError = null
  try {
    const sessionId = sessionIdOf(subject) || sessionIdOf(exec?.agent)
    described = describeFor(ctx, bindings, sessionId)
    if (described) id = toIdentity(described)
  } catch (err) {
    resolveError = err
  }

  if (resolveError) {
    const fields = { sender: '', channel: '', group: '' }
    return denyOrShadow(cfg, tool, fields, failClosedReason(tool))
  }

  // 没有 IM 绑定 = 桌面/普通会话：不介入（否则所有本地使用都会被拦）。
  if (!described) return undefined

  // 有 IM 绑定但身份不完整：无法确认发言人 → fail-closed（只读工具也拒绝）。
  if (!id) {
    const fields = logFields(null, described)
    return denyOrShadow(cfg, tool, fields, failClosedReason(tool))
  }

  const fields = logFields(id, described)
  if (isOwner(cfg.owners, id)) {
    appendLog(cfg.logPath, { ts: nowIso(), action: 'allow', tool, ...fields })
    return undefined
  }
  if (cfg.readOnlyAllow.includes(tool)) {
    appendLog(cfg.logPath, { ts: nowIso(), action: 'allow', tool, ...fields })
    return undefined
  }
  return denyOrShadow(cfg, tool, fields, denyReason(id, tool))
}

// ---------------------------------------------------------------- 插件主体

function warnOnce(ctx, message, error) {
  try {
    ctx?.logger?.warn?.(message, error)
  } catch { /* logger 不可用就算了 */ }
}

export function apply(ctx, config) {
  const hostConfig = config && typeof config === 'object' ? config : {}
  // sessionId -> dsh-im/session-pre-bind 载荷（事件侧身份缓存）
  const bindings = new Map()
  let warned = false
  const warn = (message, error) => {
    if (warned) return
    warned = true
    warnOnce(ctx, message, error)
  }

  // dsh-im 绑定通告：只登记，不做 IO（事件在消息处理链路里同步发出）。
  ctx.on('dsh-im/session-pre-bind', (payload) => {
    try {
      const sessionId = asText(payload?.sessionId)
      if (!sessionId) return
      bindings.set(sessionId, { ...payload })
      if (bindings.size > 4096) {
        for (const key of [...bindings.keys()].slice(0, 1024)) bindings.delete(key)
      }
    } catch { /* 坏载荷忽略 */ }
  })
  ctx.on('dsh-im/session-unbind', (payload) => {
    try {
      const sessionId = asText(payload?.sessionId)
      if (sessionId) bindings.delete(sessionId)
    } catch { /* 坏载荷忽略 */ }
  })

  const check = (exec, agent) => checkTool(ctx, bindings, hostConfig, exec, agent)

  /**
   * 给一个 agent 注册守卫。注册在 agent.ctx 上，作用域随 agent 销毁自动释放：
   * 对话结束、会话删除、agent 重建都不会留下悬挂的守卫或重复注册。
   */
  function registerFor(agent) {
    try {
      const agentCtx = agent?.ctx
      if (!agentCtx || typeof agentCtx.on !== 'function') return
      let tools = null
      try {
        tools = typeof agentCtx.get === 'function' ? agentCtx.get('tools') : null
      } catch { tools = null }
      if (tools && typeof tools.guard === 'function') {
        try {
          // 首选：单调执行守卫（dsh-tools 的正式 API）。
          tools.guard((exec) => check(exec, agent))
          return
        } catch (error) {
          warn('lingshu-guard: tools.guard 注册失败，退回 tools/pre-execute', error)
        }
      }
      // 旧宿主降级：pre-execute waterfall 里做同样的判定。
      agentCtx.on('tools/pre-execute', async (exec, next) => {
        const reason = check(exec, agent)
        if (reason) return { kind: 'deny', reason }
        return next()
      })
    } catch (error) {
      warn('lingshu-guard: 注册工具守卫失败，IM 鉴权未生效（请检查宿主 dsh-tools 版本）', error)
    }
  }

  ctx.on('agent/created', (payload) => {
    registerFor(payload?.agent)
  })

  // 热加载兜底：插件晚于 agent 创建挂载时，给已经活着的 agent 补注册。
  try {
    const registry = ctx.get?.('agents')
    const live = registry && typeof registry.list === 'function' ? registry.list() : null
    if (Array.isArray(live)) {
      for (const agent of live) registerFor(agent)
    }
  } catch { /* 没有 agents 服务就只靠 agent/created */ }
}
