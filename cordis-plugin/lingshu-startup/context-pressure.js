// 灵枢 · 上下文压力采集器
//
// 为什么需要它：DSH 宿主注入的"上下文估算约 X%（来源：chars）"是字符粗估，
// 实测偏低（用户看到 14%、真实 39%）。DSH 官方有准确的 token 计量服务：
//   ctx.tokenMeter.contextPressure(session) -> { pressureTokens, contextWindow }
// 但灵枢主体是 Python 进程（brain.py），拿不到 Cordis ctx。本模块在 DSH 插件层
// （JS，有 ctx）每步读一次准确压力，写到 HOME/.context-pressure.json，
// brain.py check-context / startup 注入再读这个文件——补上"没实时上下文可测"的缺口。
//
// 设计约束：
// - 只读 DSH 服务、只写 HOME 下一个小 JSON；绝不改 profile/注入文本；
// - 任何异常都吞掉（pre-step 是 wrapping 监听器，抛异常会破坏请求）；
// - ctx.tokenMeter 不存在（旧版 DSH/未挂载）时静默跳过，写 availability=false 让 Python 侧知道。

import { readFileSync, rmSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

const asText = (v) => String(v ?? '').trim()

/** 记忆目录：BRAIN_MEMORY_HOME > ~/.brain-memory/.home 指针 > ~/.brain-memory。 */
function memoryHome() {
  const env = asText(process.env.BRAIN_MEMORY_HOME)
  if (env) return env
  const base = join(homedir(), '.brain-memory')
  try {
    const ptr = asText(readFileSync(join(base, '.home'), 'utf8'))
    if (ptr) return ptr
  } catch { /* 没有指针文件就用默认目录 */ }
  return base
}

/**
 * 采集一次上下文压力并落盘。由 startup 插件在 agent/pre-step 里调用。
 * @param ctx Cordis 上下文（需有 tokenMeter 服务）
 * @param agent 当前 agent（用于取 session）
 */
export function sampleContextPressure(ctx, agent) {
  const out = {
    ts: Math.floor(Date.now() / 1000),
    available: false,
    source: 'token-meter',
    pressure_tokens: null,
    context_window: null,
    pct: null,
  }
  let file = ''
  try {
    const dir = memoryHome()
    file = join(dir, '.context-pressure.json')
    const meter = typeof ctx?.get === 'function' ? ctx.get('tokenMeter') : (ctx?.tokenMeter ?? null)
    const session = agent?.session ?? null
    if (!meter || typeof meter.contextPressure !== 'function' || !session) {
      out.reason = 'tokenMeter 服务或 session 不可用（旧版 DSH 或未挂载）'
      return write(file, out)
    }
    const snap = meter.contextPressure(session) || {}
    const p = Number(snap.pressureTokens)
    const w = Number(snap.contextWindow)
    out.available = Number.isFinite(p) && Number.isFinite(w) && w > 0
    if (out.available) {
      out.pressure_tokens = Math.max(0, Math.round(p))
      out.context_window = Math.round(w)
      out.pct = Math.min(100, Math.max(0, Math.round((p / w) * 1000) / 10))
    } else {
      out.reason = 'provider 还没上报 usage / route 未声明窗口（字段缺省）'
    }
  } catch (e) {
    out.reason = `采集失败：${e?.message || String(e)}`.slice(0, 200)
  }
  return write(file, out)
}

function write(file, out) {
  try {
    if (!file) return out
    writeFileSync(file, JSON.stringify(out, null, 2) + '\n', 'utf8')
  } catch { /* 写失败不影响请求 */ }
  return out
}

/**
 * 新会话启动时清掉上个会话留下的样本（P9 #57）。
 *
 * 为什么需要：`agent/created` 生成 startup 文本时，`.context-pressure.json`
 * 还是**上个会话** pre-step 写的（本会话 pre-step 尚未运行）。不清掉的话，
 * 全新会话会显示"水位 90%（本会话实测）"——误导 AI 在空会话里做梦/封存。
 * 清掉后首轮诚实显示"未知（本次尚未测量）"，pre-step 再写本会话真实值。
 */
export function clearContextPressure() {
  try {
    rmSync(join(memoryHome(), '.context-pressure.json'), { force: true })
  } catch { /* 清不掉也不影响：首轮只是显示旧值 */ }
}
