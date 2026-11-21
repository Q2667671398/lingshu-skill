# 灵枢 tgz 直链安装（给网络受限 / 没装 git 的用户）

## 背景

灵枢在 DSH 插件市场的条目写的是 `github:` 源码安装：

```
install: dsh plugin --profile web add github:Q2667671398/lingshu-skill
```

这要求 pnpm 调用 `git` clone GitHub 仓库。国内直连 GitHub 常 `ECONNREFUSED`，
没装 git 的机器报 `git 不是内部或外部命令`——都装不上。别的插件能装是因为它们
是 npm 版本号或 tgz 直链，pnpm 下压缩包即可，不需要 git。

## 解法：tgz 直链

把灵枢打成 npm 格式的 tgz，挂到 GitHub Release，市场条目改成 tgz 直链：

```
install: dsh plugin add https://github.com/Q2667671398/lingshu-skill/releases/download/v3.72.0/lingshu-skill-3.72.0.tgz
```

pnpm/npm 下载解压即可，**不需要 git、不需要 clone GitHub**。

## 打包（发布者操作）

```powershell
$py = "D:\ServBay\packages\python\current\python.exe"
& $py scripts\make_dsh_tgz.py
# 产出 lingshu-skill-<version>.tgz（仓库根，已 gitignore）
```

脚本会先跑 `sync_engine.sync()` 生成 `engine/` 副本（已从 git 移除、但插件运行
需要），再按 manifest 发布文件 + engine 全目录打成顶层 `package/` 的 tgz。
实测 v3.72.0：116 文件 / 1538 KB，`npm install <tgz>` 可直接装、结构正确。

## 上传到 GitHub Release

1. 在 GitHub 建 Release `vX.Y`（tag 已存在的可直接用），把 `.tgz` 作为附件上传；
2. 拿到直链：`https://github.com/Q2667671398/lingshu-skill/releases/download/vX.Y/lingshu-skill-X.Y.0.tgz`；
3. 更新插件市场目录站（`dsh-plugin-catalog` 的 `plugins.json`）里灵枢条目：
   - `install` 改成上面的 tgz 直链命令
   - 或 `npm`/`version` 留空、`install` 用 tgz（对齐 `dsh-pnpm-build-control` 的写法）

## 用户侧（你朋友）

市场里点安装即可，命令变成下载 tgz——不用装 git、不用连 GitHub。

如果暂时改不了市场条目，也可以手动装：

```powershell
dsh plugin --profile desktop add <tgz直链或本地路径>
```
