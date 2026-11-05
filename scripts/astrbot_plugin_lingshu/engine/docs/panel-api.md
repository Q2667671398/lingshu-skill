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

### 可用 GET 接口（均返回 JSON）

| 接口 | 说明 |
|---|---|
| `/api/overview` | 聚合大包：stats、layer_counts、memories、persona、mood、mind、desires、dreams 等 |
| `/api/memories?layer=&q=&limit=&offset=…` | 记忆列表（支持层/重要性/时间/标签组合筛选） |
| `/api/search?q=&k=12` | 语义检索 |
| `/api/graph?limit=300` | 联想关系图 |
| `/api/assoc?id=` | 单条记忆的关联邻居 |
| `/api/entity?name=&limit=20` | 实体档案卡 |
| `/api/persona/diff` | 人格漂移对比 |
| `/api/logs?limit=200&level=` | 运行日志 |
| `/api/conflicts?limit=50` | 矛盾记忆对 |
| `/api/inject-preview?q=` | 注入包预览 |
| `/api/growth/review?days=7` | 成长回顾 |
| `/api/workspace/search?q=&scope=knowledge` | 知识库/笔记搜索 |
| `/api/self-update` | 更新检查（只读缓存用 `?auto=1`） |
| `/api/panels` | 面板列表（本规范的管理接口） |
| `/api/panels/market` | 市场目录扫描 |
| `/api/panels/file?panel=&path=&as_page=1` | 面板静态文件（扩展名白名单：html/htm/js/css/json/png/jpg/jpeg/gif/webp/svg/ico/woff/woff2/ttf/map） |

写接口（remember/persona/knowledge 等，见 `panel_server.py` 的 `_WRITE_ENDPOINTS`）自定义面板也可以调，但请谨慎：它们直接改记忆数据。

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

## 7. AI 创建指南

给 AI 的推荐流程：

1. `POST /api/panels/scaffold {"name":"my-panel","title":"我的面板"}` 一键生成合法骨架（自动过 manifest 校验）；
2. 打开 `~/.brain-memory/panels/my-panel/`（可用 `open-folder`），只改 `index.html` 和 manifest 的 title/description/version；
3. 面板内所有数据都从第 3 节的统一 API 拉，样式布局交互完全自由；不要外链 CDN（CSP 只允许同源）；
4. 放进去即生效：面板管理界面点「启用」热加载，无需重启；
5. 出错自动回退：manifest 写坏 → 扫描标记 invalid 不进列表；运行报错 → 只崩 iframe 自动回默认面板，修好再启用即可；
6. 做好了想分享：面板管理 → 「上传到市场」（剪切到 panel-market），整个目录拷给别人即可流通。
