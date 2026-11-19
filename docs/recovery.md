# 事故处置单：记忆/用户文件突然没了

> 这份文档的由来是一次真实事故：用户 `C:\Users\<name>` 下的桌面、文档、下载、
> 图片等库文件夹被未知进程整片清空，`~/.brain-memory` 一起消失；而当时的备份
> 默认写在 `~/Documents/brain-memory-backups` —— 备份和本体同目录同卷，
> 被同一次事故一起带走。**先做对顺序，再谈找原因。**

## 第 0 步：停手（最重要）

- **不要再往出事的盘写任何东西**：不装软件、不下载、不解压、不跑 `backup` 到那块盘。
- 每写入一次，就少一分用文件恢复工具捞回来的机会。
- 需要临时文件就放到**另一块盘**（如 E:）。

## 第 1 步：确认到底丢了什么（1 分钟）

```bash
# 记忆库还在不在、是不是空壳
python scripts/brain.py home verify
python scripts/brain.py status --json

# 会话开始会做空库熔断：出现「⚠ 记忆库疑似被清空」横幅就是它
python scripts/brain.py startup --query "检查"
```

对照 `_meta.json` 里的 `seen` 快照（上次见到的各文件字节数），能看出是"一直是空的"
还是"刚刚变空的"。

## 第 2 步：找备份（按顺序）

| 位置 | 说明 |
|---|---|
| `%BRAIN_MEMORY_BACKUP_DIR%` | 显式指定过就以它为准 |
| 别的卷 `X:\brain-memory-backups\` | **v3.45 起的默认**（跨卷优先） |
| `<记忆库>\backups\` | 同卷兜底 |
| `D:\brain-memory-backups\` | v3.45 之前的旧默认（老版本写盘符根） |
| `~/Documents/brain-memory-backups`、`~/Desktop/brain-memory-backups` | 更老版本的位置，**可能已被同一次事故清掉** |

```bash
# 直接问灵枢最近的备份在哪（空库熔断横幅里也会给这条路径）
python -c "import safety,core;print(safety.backup_dir_candidates(core.HOME));print(core.newest_backup_hint())"
```

备份包命名 `brain-memory_YYYYMMDD_HHMMSS.zip`，旁边可能有 `.sha256` 与 `.enc`。
先验证再恢复：

```bash
python -c "import zipfile,sys;z=zipfile.ZipFile(sys.argv[1]);print('bad:',z.testzip());print(len(z.namelist()),'files')" "备份包.zip"
```

## 第 3 步：恢复

```bash
# 恢复会自动先给现状做一份备份（写进 <记忆库>/backups），失败则中止
python scripts/brain.py restore "D:\brain-memory-backups\brain-memory_20261031_201408.zip" --yes

# 想回到备份那一刻的状态（删掉备份之后新增的受管文件）：
python scripts/brain.py restore "…zip" --yes --exact
```

`--exact` **只清灵枢自己管的文件**；你自己放进记忆库的草稿、导出包、照片会被
原样保留并在输出里列出来。恢复后 `restore` 会打印保留了哪些。

## 第 4 步：查为什么会丢

```bash
# 回收站（普通删除会在这里；$I 文件里是原始路径）
# 卷影副本（"以前的版本"用；注意晚于事故时间的快照没有价值）
vssadmin list shadows /for=C:

# 灵枢自己的运行日志：护栏拒绝、空库熔断、写入失败都在这
python scripts/brain.py logs --level warn
python scripts/brain.py failures --days 7
```

如果是某个程序/脚本干的：查它的清理规则是不是按通配符扫用户目录
（`C:\Users\*\*` 这类写法一跑就是灾难）。

## 第 5 步：别让它再发生

1. **备份放到别的卷**（v3.45 默认如此）。同卷备份 = 同一个炸弹上的两个引信。
2. **每周一次** `python scripts/brain.py backup`，加密备份用 `--password`。
3. **别把记忆库放进用户库文件夹**——`safety.assert_safe_home()` 会直接拒绝
   （桌面/文档/下载/图片/视频/音乐/OneDrive、盘符根、主目录本体）。
4. **别用会"清理"用户目录的工具**做自动整理；要做也先把排除规则写死。
5. 重要数据再留一份**离线/网盘**副本；备份包已剥离 token、跳过密钥文件，可以放心上传。

## 一句话记忆

> 停手 → 找跨卷备份 → 先验证再恢复 → 最后才查原因。
