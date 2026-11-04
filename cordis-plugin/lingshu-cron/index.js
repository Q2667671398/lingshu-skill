// 注意：不要在这里 re-export 不存在的名字——ESM 命名导入在模块链接期就会
// SyntaxError，整个插件加载失败且被 cordis 静默吞掉。
// plugin.js 刻意零外部依赖，不导出 Config（配置走 DEFAULTS + mergeConfig）。
export { name, apply } from './plugin.js'
