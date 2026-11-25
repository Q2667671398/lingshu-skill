# 灵枢 自定义面板开发规范（panel-api）

本规范面向想给灵枢写自定义面板的用户与 AI。零第三方依赖：后端只调本机 HTTP API，前端原生 HTML/CSS/JS。

## 1. 目录结构与放置方法

两个本地目录（均在记忆库 `~/.brain-memory/` 下，面板服务启动时自动创建）：

| 目录 | 作用 |
|---|---|
| `~/.brain-memory/panels/` | **已安装目录**。面板放进去即被识别、可启用/切换 |
| `~/.brain-memory/panel-market/` | **面板市场（本机共享库）**。上传/分享的面板放这里，供本机安装与拷贝流通 |

每个面板是**一个独立子目录**：

```
panels/
└── my-panel/
    ├── panel.json      ← manifest（必需）
    ├── index.html      ← 入口页（必需，entry 字段指向的文件）
    ├── style.css       ← 可选静态资源
    └── app.js
```

- 分享流转用 zip：把面板打包（或用别人的 zip）放进 market 目录，市场里点「安装」自动解压校验后落位；zip 不会自动解压，只有 manifest 目录形式被「已安装」识别。
- **市场没有云端后端**：panel-market 就是本机共享库，同机器直接可用；把整个 `panel-market/` 目录拷给别人，对方放到自己的 `~/.brain-memory/` 下即可流通。不假装有真云市场。

## 2. manifest 格式（panel.json）

```json
{
  "name": "my-panel",
  "title": "我的面板",
  "version": "1.0.0",
  "author": "",
  "description": "",
  "entry": "index.html",
  "lingshu_api": 1
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✅ | 面板标识，正则 `^[a-z0-9-_一-龥]{1,32}$`（小写字母/数字/-/_/中文），**必须与目录名一致** |
| `title` | ✅ | 展示名（面板管理界面显示） |
| `version` | ✅ | 任意字符串，建议 semver |
| `author` | — | 作者 |
| `description` | — | 描述 |
| `entry` | ✅ | 入口 HTML 相对路径（默认 index.html），**必须在面板目录内** |
| `lingshu_api` | ✅ | API 版本，当前只认 `1` |

**校验规则**（任一失败 → 面板标记 invalid，不进可切换列表，绝不影响默认面板）：

- 缺 `panel.json` / JSON 损坏 / 不是对象；
- `name` 缺失、非法、或与目录名不一致；
- `title` 为空；`lingshu_api` ≠ 1；
- `entry` 是绝对路径 / 含 `..` / 文件不存在。

## 3. 统一 API（所有面板共用同一套数据源）

鉴权：服务端下发 HTML 时把 `/*__PANEL_TOKEN__*/` 替换为一次性 token。入口 HTML 里写：

```js
const PANEL_TOKEN = "/*__PANEL_TOKEN__*/";
async function api(path, opts){
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers || {},
    {Authorization: "Bearer " + PANEL_TOKEN});
  const r = await fetch(path, opts);
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}
```

静态资源引用（css/js/图片）不要写相对路径——页面从 `/api/panels/file?...` 下发，用 helper 改写：

```js
function pfile(p){ return "/api/panels/file?panel=面板名&p=" + encodeURIComponent(p); }
// <link rel="stylesheet" href=...> 需动态注入或直接内联样式
```

### 可用 GET 接口（均返回 JSON，共 33 个）

> 清单按 `panel_server.py` 的 `_route_get` 实际注册生成。**除 `/api/ping`、`/api/alive`
> 外，全部需要 `Authorization: Bearer <token>`**（token 由首页注入）。

**数据类**

| 接口 | 说明 |
|---|---|
| `/api/overview` | 聚合大包：stats、layer_counts、memories、persona、mood、mind、desires、dreams、inner_voice 等 |
| `/api/memories?layer=&q=&limit=&offset=…` | 记忆列表（层/重要性/时间/标签组合筛选） |
| `/api/search?q=&k=12&mode=auto` | 模糊召回（返回 `[{memory, score, parts}]`） |
| `/api/graph?limit=300` | 联想关系图 |
| `/api/assoc?id=` | 单条记忆的关联邻居 |
| `/api/entity?name=&limit=20` | 实体档案卡（`name` 必填，1~64 字符） |
| `/api/conflicts?limit=50` | 矛盾记忆对 |
| `/api/inject-preview?q=` | 注入包预览（会跑一次装配，略重） |
| `/api/workspace` | 知识 / 笔记 / 技能 / 插件的工作区快照 |
| `/api/workspace/search?q=&scope=knowledge` | 知识库 / 笔记搜索 |
| `/api/growth/review?days=7` | 成长回顾 |
| `/api/growth/timeline` | 成长时间线 |
| `/api/insights` | 运行洞察（版本 / 原文库 / 群聊鉴权 / 嵌入状态） |

**人格与心情**

| 接口 | 说明 |
|---|---|
| `/api/persona/diff` | 人格漂移对比 |
| `/api/persona/changes` | AI 近期人格改动（待审入口） |
| `/api/persona/snapshots` | 人格快照列表 |
| `/api/persona/suggest` | 画像归纳候选（带证据链） |
| `/api/mode/explain?q=` | 会话模式判定依据（work / human / auto） |

**运维**

| 接口 | 说明 |
|---|---|
| `/api/logs?limit=200&level=` | 运行日志 |
| `/api/diagnostics` | 自诊断（死链 / 冲突 / 膨胀 / 来源归属） |
| `/api/home` | 数据目录位置与迁移信息 |
| `/api/config` | **设置页**：返回 29+ 项白名单配置（类型/范围/枚举/默认值/中文说明） |
| `/api/data` | **数据页**：备份列表（记录数 / 距今 / 可疑标记）+ 数据规模 |
| `/api/self-update?auto=1&refresh=1&source=npm` | 更新检查；`auto=1` 只读缓存（毫秒返回、绝不联网），`refresh=1` 真拉，`source` 可强制 `npm`/`git` |
| `/api/extensions` | 已装扩展列表 |
| `/api/extensions/ollama/status` | Ollama 扩展状态机（idle/downloading/…/done/error） |

**面板管理**

| 接口 | 说明 |
|---|---|
| `/api/panels` | 面板列表（`{active, panels[], installed_dir, market_dir}`） |
| `/api/panels/market` | 市场目录扫描 |
| `/api/panels/file?panel=&path=&as_page=1` | 面板静态文件（扩展名白名单；`as_page=1` 对 HTML 注入 token） |
| `/api/panels/cloud` | 云端市场状态（配置 / 列表） |
| `/api/panels/cloud/shot?...` | 云端面板截图 |

**生命周期（免 token，面板自己的"活着"信号）**

| 接口 | 说明 |
|---|---|
| `/api/ping` | 探测"面板是否已在跑"：`{ok, app:"lingshu-panel", port, pid}`。**故意免鉴权**——启动器探测时还拿不到 token；它不含任何记忆内容 |
| `/api/alive` | **长轮询保活**：服务端按住最多 25 秒再返回，页面收到立刻再发。它不是定时器，**不受浏览器对隐藏标签页的节流影响**，所以"页面开着就一定连着" |

写接口（remember / persona / knowledge 等）自定义面板也可以调，但请谨慎：它们直接改记忆数据。
完整清单见 `panel_server.py` 的 `_POST_ROUTES`（当前 53 个）。

> **务必自己带 token**：从 `/api/panels/file?as_page=1` 加载的 HTML 会被注入 token；
> 若你把面板放在 iframe 里，用 `window.parent` 取 token 或让宿主注入，别硬编码。

## 4. 面板管理接口

| 方法 | 路径 | Body / 参数 | 说明 |
|---|---|---|---|
| GET | `/api/panels` | — | `{active, panels[], installed_dir, market_dir}`；panels 含 valid/invalid_reason |
| GET | `/api/panels/market` | — | 市场目录扫描（panels + zips） |
| GET | `/api/panels/file` | query | 静态文件；`as_page=1` 对 HTML 注入 token |
| POST | `/api/panels` | `{action:"activate", name}` | 切换面板（name="default" 回默认），热加载、无需重启 |
| POST | `/api/panels` | `{action:"deactivate"}` | 回默认面板 |
| POST | `/api/panels` | `{action:"upload", name}` | 已安装 → 市场（**剪切移动**，同名 409） |
| POST | `/api/panels` | `{action:"install", name}` | 市场 → 已安装（**复制**，市场源保留；zip 自动解压校验） |
| POST | `/api/panels` | `{action:"delete", name, source}` | 删除（source: installed/market；激活中 400 拒删） |
| POST | `/api/panels` | `{action:"open-folder", which}` | 资源管理器打开目录（installed/market） |
| POST | `/api/panels/scaffold` | `{name, title}` | 一键生成最小合法面板骨架（AI 起步入口） |
| GET | `/api/ping` | — | 存活探测（免 token） |
| GET | `/api/alive` | — | 长轮询保活（免 token，按住 ~25s） |
| POST | `/api/shutdown` | `{}` | **关掉面板服务**（设置页「退出服务」用；需 token） |

### 云端市场（Gitee 主 + GitHub 镜像，v2.29+）

云端市场 = 一个你自己的公开 Gitee 仓库（`owner/repo`），仓库布局：

```
panels/<面板名>.zip      面板安装包（≤50MB）
shots/<面板名>.<ext>     截图（png/jpg/webp，≤2MB），可选但强烈建议
index.json               目录清单 {"panels":[{name,title,version,author,description,screenshot,size,updated}]}
```

- **浏览**：不需要 token（公开仓库）；`GET /api/panels/cloud[?force=1]` 拉清单，Gitee 失败自动试 GitHub，60s 缓存，全挂时降级提示不影响本机市场。
- **下载**：`POST /api/panels/cloud {action:"download", name}` → 拉 zip 落本机市场 → 复用 zip 安装校验落位。
- **发布**：`POST /api/panels/cloud {action:"publish", name, screenshot:"data:image/png;base64,…"}` → 内存打 zip → Gitee contents API 上传（更新带 sha）→ 可选同步 GitHub 镜像 → 合并回写 index.json。需要 Gitee 私人令牌（勾 projects 权限）。
- **配置**：`POST /api/panels/cloud {action:"save-config", gitee_repo, gitee_token, github_repo, github_token}`；token 只存本机 config.json 的 `panel.cloud`，API 响应一律脱敏（只露前 4 位）。
- **截图代理**：`GET /api/panels/cloud/shot?name=X&ext=.png`（前端 `<img>` 走本机端点，避免跨域）。

## 5. 热加载与回退机制

- 首页**永远**返回内置默认面板作为「外壳」；外壳启动时读 `window.__LINGSHU_ACTIVE__`（服务端注入），激活了自定义面板就全屏 iframe 加载 `/api/panels/file?panel=X&path=index.html&as_page=1`。
- 切换 = 后端写 config 的 `panel.active`（`POST /api/panels {action:"activate"}`）+ 前端刷新外壳 → iframe 换新页面。服务进程全程不动，**热加载、即时生效**。
- 三层兜底：
  1. 面板 invalid（manifest 损坏等）→ 后端预检拒绝激活 / 外壳检查后直接回退；
  2. iframe 加载失败或 3 秒超时 → 前端自动 deactivate 并提示；
  3. 面板自身 JS 报错只崩 iframe，不影响外壳与其它功能。
- 外壳右上角有悬浮「⇱ 返回默认面板」按钮，永远有逃生口。

## 6. 最小可运行示例

`my-panel/panel.json`：

```json
{"name": "my-panel", "title": "我的面板", "version": "1.0.0",
 "entry": "index.html", "lingshu_api": 1}
```

`my-panel/index.html`：

```html
<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>我的面板</title>
<style>body{font-family:system-ui;background:#14181e;color:#e6e1d5;padding:32px}
.card{background:#1b2028;border:1px solid #2a3140;border-radius:12px;padding:20px;max-width:640px}</style></head>
<body>
<h1>我的面板</h1>
<div class="card"><div id="out">加载中…</div></div>
<script>
const PANEL_TOKEN = "/*__PANEL_TOKEN__*/";
async function api(path, opts){
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers || {},
    {Authorization: "Bearer " + PANEL_TOKEN});
  const r = await fetch(path, opts);
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}
api("/api/overview").then(d => {
  document.getElementById("out").textContent =
    "记忆总数：" + (d.stats && d.stats.total != null ? d.stats.total : "?") +
    "，海马体层：" + (d.layer_counts ? d.layer_counts.hippo : "?");
}).catch(e => document.getElementById("out").textContent = "加载失败：" + e.message);
</script>
</body></html>
```

把目录放进 `~/.brain-memory/panels/`，打开默认面板 → 「面板」按钮 → 点「启用」即可。

## 6.5 生命周期与设置页（自定义面板也能用）

### 面板会自己退，别假设它常驻

- 页面**必须**维持一条 `GET /api/alive` 长轮询（服务端按住 25 秒再返回，收到立刻再发）。
  这不是定时器，所以不受浏览器"隐藏标签页节流"影响——**它断了，服务端就知道页面没了**。
- 服务端超过 `panel_idle_minutes`（默认 1 分钟）没有任何请求，且没有活跃长轮询 → 优雅退出。
- 想立刻退：`POST /api/shutdown`（面板「设置」页有按钮）。
- 双击桌面快捷方式不会起第二个进程：启动器先 `GET /api/ping`，已在跑就只把浏览器叫回来。

**自定义面板如果自己接管了整个页面，请照做这两条**，否则服务端会认为"没人用"而退出：

```js
// 1) 长轮询保活
(async function keepAlive(){
  for (;;) {
    let ok = false;
    try { ok = (await fetch("/api/alive", {cache: "no-store"})).ok } catch (_) {}
    if (!ok) break;
    await new Promise(r => setTimeout(r, 200));
  }
})();

// 2) 兜底心跳（长轮询被中间层掐断时仍能续命）
setInterval(() => {
  try { fetch("/api/ping", {cache: "no-store", keepalive: true}).catch(() => {}) } catch (_) {}
}, 30000);
```

### 设置页接口：`/api/config`

- `GET /api/config` → `{items: [{key, group, label, kind, min, max, options, hint, value, default}...]}`
- `POST /api/config` → body `{key, value}`

**只允许白名单键**（见 `scripts/panel_settings.py` 的 `CONFIG_SCHEMA`）。非白名单、越界、
类型不符一律 `400` + 中文原因。写盘走锁 + 原子替换，只读模式下函数层直接拒绝。

### 数据页接口：`/api/data`、`/api/data/backup`、`/api/data/restore`

- `GET /api/data` → 备份列表（每份含 `records` 记录数、`age_hours`、`suspicious` 标记）+ 数据规模
- `POST /api/data/backup` → 立即备份（**默认不修剪**历史）
- `POST /api/data/restore` → `{name}`，只接受**备份目录内的文件名**（防路径穿越）；
  恢复前会自动把现状再备份一份

## 7. AI 创建指南

给 AI 的推荐流程：

1. `POST /api/panels/scaffold {"name":"my-panel","title":"我的面板"}` 一键生成合法骨架（自动过 manifest 校验）；
2. 打开 `~/.brain-memory/panels/my-panel/`（可用 `open-folder`），只改 `index.html` 和 manifest 的 title/description/version；
3. 面板内所有数据都从第 3 节的统一 API 拉，样式布局交互完全自由；不要外链 CDN（CSP 只允许同源）；
4. 放进去即生效：面板管理界面点「启用」热加载，无需重启；
5. 出错自动回退：manifest 写坏 → 扫描标记 invalid 不进列表；运行报错 → 只崩 iframe 自动回默认面板，修好再启用即可；
6. 做好了想分享：面板管理 → 「上传到市场」（剪切到 panel-market），整个目录拷给别人即可流通。
