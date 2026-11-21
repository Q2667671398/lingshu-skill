# 灵枢 npm 发布说明（维护者）

## 原理：为什么发 npm 后不用再改市场

awesome-dsh-plugin 目录站会**自动检测**插件的仓库：
- 仓库是 npm 包 → 市场 install 自动变成 `dsh plugin add lingshu-skill@版本`
- 仓库不是 npm 包 → 回退到 `github:owner/repo` 源码安装（需要 git + 连 GitHub，国内常失败）

灵枢之前没发 npm，所以市场条目是 `github:` 源码安装，你朋友装时才报 `git 不是内部命令` / `ECONNREFUSED`。**发一次 npm 后目录站自动改成 npm 包名，以后每次发版只要 publish 新版本，不用再动市场条目。**

## 一次性准备（已完成，在仓库里）

- `package.json`：`name: lingshu-skill`、`files` 白名单（只含发布文件）、`scripts.prepack`（publish 前跑 `npm_prepack.py`）
- `scripts/npm_prepack.py`：sync engine 副本 + 清 pycache，保证包里 engine 完整且无垃圾
- `.npmignore`：兜底排除本地数据（files 白名单优先级更高，主要靠 prepack 清理）

验证过 `npm pack`：132 文件，必需文件齐全、engine 49 文件无缓存、tests 0、cordis-plugin 11。

## 发布步骤（你操作）

```powershell
# 1. 一次性：登录 npm（有账号就直接 login，没有先去 npmjs.com 注册）
npm login

# 2. 确认包名没被占、版本比已发布的大
npm view lingshu-skill version    # 若 404 = 首次发布，正常

# 3. 在仓库根发布（prepack 会自动跑，不用手动 sync）
npm publish
```

发布后：
- npm 上出现 `lingshu-skill@3.74.0`（或你当时的版本）
- 目录站**下次抓取**（约每天一次，市场另有 1 小时缓存）自动把 install 改成 npm 包名
- 你朋友在插件市场点安装 → pnpm 从 npm registry 下 tgz，**不需要 git、不需要连 GitHub**

## 注意

- **每次发版**：改代码 → 升 SKILL.md/package.json 版本 → CHANGELOG → `npm publish`。市场自动跟随，不用手改目录站。
- **包名 `lingshu-skill` 已注册归你**：别人不能再占。首次 publish 后确认 `npm view lingshu-skill version` 返回你的版本。
- **别把 token 写进包**：`.npmignore` + files 白名单已排除 config.json/.env；发布前 `npm pack --dry-run` 再扫一眼最稳。
- **国内网络**：npm registry 国内可达（你朋友装别的插件就是走 npm），所以没问题。
