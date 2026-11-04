// lingshu-skill 根入口：把仓库根的 SKILL.md 注册成 DSH 内嵌技能（bundled skill）。
// 零外部依赖：不 import 任何 @deepseek-ai/* 包（bundle 安装时内置包从插件真实路径解析不到）。
// 前置要求：需要 Python 3（brain.py 运行时依赖），缺失时技能内容会说明降级行为。

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

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
}
