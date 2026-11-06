// lingshu-skill 根入口：把仓库根的 SKILL.md 注册成 DSH 内嵌技能（bundled skill）。
// 零外部依赖：不 import 任何 @deepseek-ai/* 包（bundle 安装时内置包从插件真实路径解析不到）。
// 前置要求：需要 Python 3（brain.py 运行时依赖），缺失时技能内容会说明降级行为。

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

// 子插件（启动注入 / 例行维护调度）也随根包一起激活。
// 它们此前是 cordis-plugin/ 下的独立子包，需要单独 tgz 安装进 profile——
// 插件商店只装根包，子包永远"没按上"。现改为根包 apply 时直接挂载，
// 商店一次安装全带上。两子包均零外部依赖，plugin.js 可直接 import。
//
// 降级声明（#71）：静态 import 意味着任一子插件文件缺失/语法损坏会让整个根包
// 激活失败（技能 + 另一子插件一并失效）。可接受的原因：子插件随根包同一
// bundle 发布、同一校验和守护，运行期缺失仅见于人为删改；届时按下面
// logger.error 的指引重装根包即可恢复。若未来要做真正的部分降级，需把
// import 改为动态 import() 并逐包 try/catch。
import * as startupPlugin from './cordis-plugin/lingshu-startup/plugin.js'
import * as cronPlugin from './cordis-plugin/lingshu-cron/plugin.js'

const here = dirname(fileURLToPath(import.meta.url))

/** 读仓库根 SKILL.md，返回剥离 frontmatter 后的正文；读不到则返回 undefined。 */
function loadSkillBody() {
  try {
    const raw = readFileSync(join(here, 'SKILL.md'), 'utf8')
    // 剥掉 YAML frontmatter（--- ... ---），registry 不需要它
    const m = raw.match(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/)
    return m ? raw.slice(m[0].length).trimStart() : raw
  } catch {
    return undefined
  }
}

export const name = 'lingshu-skill'

// cordis 跨服务访问必须声明注入：直接访问 ctx.skills 会报
// "cannot get property "skills" without inject"，导致宿主激活失败并回滚安装。
export const inject = ['skills']

export function apply(ctx) {
  const content = loadSkillBody()
  if (content === undefined) {
    ctx.logger?.warn?.('lingshu-skill: SKILL.md not found next to index.js, bundled skill not registered')
    return
  }
  ctx.skills.register({
    name: 'lingshu',
    description:
      'Brain-inspired persistent memory, persona and growth system for AI coding agents. ' +
      'Local-only, zero-dependency. Requires Python 3 on PATH (or configure the startup plugin).',
    whenToUse:
      '需要持久记忆、人格设定、成长记录或跨会话待办时使用；' +
      '需 Python 3，缺 Python 时 startup 注入不可用但技能文档仍可读。',
    content,
    invocation: { modelInvocable: true, userInvocable: true },
  })

  // 挂载子插件：startup = 会话启动自动注入人格/记忆；cron = 做梦/清点/用量等例行维护。
  // 各自 try/catch：一个挂载失败不能拖垮另一个和技能注册本身。
  try {
    startupPlugin.apply(ctx, {})
  } catch (e) {
    ctx.logger?.error?.('lingshu-skill: startup 子插件挂载失败，启动注入不可用；重装根包可恢复', e)
  }
  try {
    cronPlugin.apply(ctx, {})
  } catch (e) {
    ctx.logger?.error?.('lingshu-skill: cron 子插件挂载失败，例行维护不可用；重装根包可恢复', e)
  }
}
