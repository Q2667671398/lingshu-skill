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
//   { "guard": { "enabled": true, "shadow": false, "owners": [],
//                "readOnlyAllow": ["read", ...], "logPath": "" } }
//   * owners：缺省（未配或空数组）回退同文件顶层 owners 数组——与 CLI 的
//     channel 身份编码一致（形如 "qq@qq_xxx:C858…"），不用再维护第二份名单。
//   * shadow=false（默认）：已启用拦截——非 owner 调用有副作用工具直接拒绝并记日志。
//     显式设 shadow=true 进入观察模式：只记日志不拦，启动时会打一条明确的 warn。
//
// 日志：JSONL，默认 ~/.brain-memory/lingshu-guard.log，字段
//   {ts, action:'allow'|'deny'|'shadow-deny', tool, sender, channel, group}
//   deny = 已拦截；shadow-deny = 观察模式（shadow=true）下"本该拦截"只记账。
// 只记录 IM 会话的判定；桌面会话不产生日志。

import { appendFileSync, mkdirSync, readFileSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

export const name = 'dsh-lingshu-guard'

// 默认只读白名单：v3.75 收紧——移除 read/glob/grep/web_search。
// 原因：这些"只读"工具可读任意本地路径（read/glob/grep）或把内容外带（web_search），
// 而灵枢记忆与 token 就是 HOME 下的明文 *.jsonl / config.json。非 owner 一句
// "读 ~/.brain-memory/hippocampus.jsonl 贴出来"就能绕过 channel/consent 门控拿到
// 全体成员跨群记忆和密钥。现在只放行**不接触本地路径、无副作用**的工具。
// 需要让非 owner 读记忆时，应走灵枢自带的 channel recall（有 group/consent 门控），
// 不靠通用文件读取工具。显式 readOnlyAllow 可覆盖此表（用户自己承担风险）。
const DEFAULT_READ_ONLY = [
  'list_agents',
  'skill',
  'todo_write',
  'ask_user_question',
]

// guard 段默认值。shadow 默认 false：默认就拦（安全优先），不再"只记日志不拦"。
// 需要先观察误伤时显式设 shadow=true，启动会打 warn（观察模式，敏感命令不拦）。
const DEFAULT_GUARD = {
  enabled: true,
  shadow: false,
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

/**
 * owners 匹配（v3.75 收紧）：**只认完整三元组身份**。
 *
 * 原来还把裸 senderId、`channelName:senderId` 当 owner 候选——但 senderId
 * 在不同通道可能相同（如 QQ 和微信都可能有 "12345"），owners 里配了裸
 * senderId 时，任意通道撞号的成员都会被判 owner、拿到全部工具权限。
 * 现在只匹配 CLI 口径的完整身份 `channel:user`（含 botId 的编码形态）。
 * 老配置里若仍是裸 senderId，需要用 `brain.py channel owner list` 查到完整
 * 身份后重新登记；启动时会对疑似裸 senderId 的 owner 打一次告警。
 */
function isOwner(owners, id) {
  if (!owners.length || !id || !id.user) return false
  const candidates = new Set([id.user])
  // 完整三元组（不含 botId 的形态与含 botId 的形态——两者都是编码后的完整身份）
  if (id.channelName) {
    const encName = enc(id.channelName)
    candidates.add(`${encName}:${enc(id.senderId)}`)
    if (id.botId) candidates.add(`${encName}@${enc(id.botId)}:${enc(id.senderId)}`)
  }
  for (const owner of owners) {
    if (candidates.has(owner)) return true
  }
  return false
}

/** 疑似裸 senderId 的 owner（纯数字/短 id，无通道前缀）——启动时告警用。 */
function suspiciousOwners(owners) {
  return owners.filter((o) => !String(o).includes(':') && !String(o).includes('@'))
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
  const sessionId = sessionIdOf(subject) || sessionIdOf(exec?.agent)

  let described = null
  let id = null
  let resolveError = null
  try {
    described = describeFor(ctx, bindings, sessionId)
    if (described) id = toIdentity(described)
  } catch (err) {
    resolveError = err
  }

  if (resolveError) {
    const fields = { sender: '', channel: '', group: '' }
    return denyOrShadow(cfg, tool, fields, failClosedReason(tool))
  }

  // v3.75：区分"真桌面会话"与"IM 会话但身份解析失败"。
  // 原来 described=null 一律放行——但 describeConversationSync 暂不可用且
  // session-pre-bind 事件漏了时，IM 会话也会被当桌面会话，非 owner 可用
  // shell/写文件/subagent 且无审计。现在：bindings 里有这个 session（事件侧
  // 明确登记过 IM 绑定）却解析不出身份 → fail-closed；只有真正无任何绑定
  // 痕迹的才当桌面会话放行。
  if (!described) {
    if (sessionId && bindings.has(sessionId)) {
      const fields = { sender: '', channel: '', group: '' }
      return denyOrShadow(cfg, tool, fields,
        `灵枢·guard 拒绝：这是已绑定的 IM 会话，但身份解析失败（宿主只读服务不可用），`
        + `无法确认发言人，按 fail-closed 拒绝 "${tool}"。`)
    }
    return undefined
  }

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

  // shadow=true 是观察模式（只记日志不拦）。启动时明确警告一次，避免用户
  // "以为已经开启鉴权、其实敏感命令全被放行"。默认 shadow=false 不触发。
  try {
    const gc = resolveGuardConfig(hostConfig)
    if (gc.shadow) {
      warnOnce(ctx, 'lingshu-guard: 当前处于观察模式（shadow=true），敏感命令不会被拦截，只记日志；'
        + '确认不会误伤后请改为 shadow=false 以启用拦截')
    }
    // v3.75：裸 senderId 当 owner 已失效（跨通道撞号可冒充主人），启动时告警一次。
    if (gc.enabled && !gc.shadow) {
      const bare = suspiciousOwners(gc.owners)
      if (bare.length) {
        warnOnce(ctx, 'lingshu-guard: owners 里疑似有裸 senderId（' + bare.slice(0, 5).join('、')
          + '），v3.75 起只认完整三元组身份（channel:user），这些条目已不再生效；'
          + '请用 `brain.py channel owner list` 查到完整身份后重新登记')
      }
    }
  } catch { /* 配置读取异常交给 checkTool 按 fail-closed 处理，这里不重复报错 */ }

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
