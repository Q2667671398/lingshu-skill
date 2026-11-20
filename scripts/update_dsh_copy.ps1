# 灵枢 → DSH 插件目录热同步（按发布清单精确复制，不重启 DSH）
#
# 用法（从哪个目录跑都行，脚本自己定位仓库根）：
#   powershell -ExecutionPolicy Bypass -File "<仓库>\scripts\update_dsh_copy.ps1"
#   powershell -ExecutionPolicy Bypass -File "<仓库>\scripts\update_dsh_copy.ps1" -Profile desktop
#
# 为什么不再用 robocopy /E 整目录复制：那会把仓库旁边的本地杂物一起塞进插件目录
# （实测 .tmp_refs 129 MB、_ui_probe 12 MB、panel_src/tests 若干），而 DSH 只需要
# 发布清单 manifest.json 里的交付物。现在只拷清单里列出的文件（当前 65 个）。
#
# 退出码：0 成功；1 有文件没拷成（多半是 DSH 正占用，完全退出 DSH 后重跑）。

param(
    [string]$Profile = ""
)

$ErrorActionPreference = "Stop"

$src = Split-Path -Parent $PSScriptRoot          # 仓库根目录（本脚本在 scripts/ 下）
$profilesRoot = Join-Path $env:USERPROFILE ".dsh\profiles"

function Get-CopyVersion($dir) {
    try {
        $pkg = Get-Content -LiteralPath (Join-Path $dir "package.json") -Raw -Encoding UTF8 | ConvertFrom-Json
        return [string]$pkg.version
    } catch {
        return "?"
    }
}

# 1) 找目标副本：指定 profile，否则自动挑所有已装 lingshu-skill 的 profile
$targets = @()
if ($Profile) {
    $one = Join-Path $profilesRoot "$Profile\node_modules\lingshu-skill"
    if (-not (Test-Path $one)) {
        Write-Host "[错误] profile「$Profile」下没有 lingshu-skill：$one" -ForegroundColor Red
        exit 1
    }
    $targets = @($one)
} elseif (Test-Path $profilesRoot) {
    foreach ($p in Get-ChildItem -LiteralPath $profilesRoot -Directory) {
        $d = Join-Path $p.FullName "node_modules\lingshu-skill"
        if (Test-Path $d) { $targets += $d }
    }
}
if (-not $targets) {
    Write-Host "[错误] 找不到任何 DSH profile 里的 lingshu-skill 副本。" -ForegroundColor Red
    Write-Host "       先让 DSH 装一次 lingshu-skill，或用 -Profile <名字> 指定 profile。" -ForegroundColor Red
    exit 1
}

# 2) 读发布清单
$mfPath = Join-Path $src "manifest.json"
if (-not (Test-Path $mfPath)) {
    Write-Host "[错误] 缺少 manifest.json（发布清单）：$mfPath" -ForegroundColor Red
    Write-Host "       先在仓库根运行：python scripts\make_manifest.py" -ForegroundColor Red
    exit 1
}
$manifest = Get-Content -LiteralPath $mfPath -Raw -Encoding UTF8 | ConvertFrom-Json
$files = @($manifest.files.PSObject.Properties | ForEach-Object { $_.Name })
if ($files.Count -eq 0) {
    Write-Host "[错误] manifest.json 里没有任何文件条目。" -ForegroundColor Red
    exit 1
}

# 3) 逐文件复制（不删除目标里多出来的旧文件，避免误删用户自装的东西）
$hadFailure = $false
foreach ($dst in $targets) {
    $before = Get-CopyVersion $dst
    $copied = 0
    $failed = @()
    foreach ($rel in $files) {
        $relWin = $rel -replace '/', '\'
        $s = Join-Path $src $relWin
        $d = Join-Path $dst $relWin
        try {
            if (-not (Test-Path -LiteralPath $s)) { throw "源文件缺失" }
            $dDir = Split-Path -Parent $d
            if (-not (Test-Path -LiteralPath $dDir)) {
                New-Item -ItemType Directory -Force -Path $dDir | Out-Null
            }
            Copy-Item -LiteralPath $s -Destination $d -Force
            $copied++
        } catch {
            $failed += ("{0} —— {1}" -f $rel, $_.Exception.Message)
        }
    }
    $after = Get-CopyVersion $dst
    Write-Host ("[同步] {0}" -f $dst)
    Write-Host ("       版本 {0} -> {1}，复制 {2}/{3} 个文件" -f $before, $after, $copied, $files.Count) -ForegroundColor Green
    if ($failed.Count -gt 0) {
        $hadFailure = $true
        Write-Host ("       失败 {0} 个（多半是 DSH 正在占用文件，完全退出 DSH 后重跑）：" -f $failed.Count) -ForegroundColor Yellow
        $failed | Select-Object -First 5 | ForEach-Object { Write-Host ("         - {0}" -f $_) -ForegroundColor Yellow }
    }
}

if ($hadFailure) { exit 1 }

Write-Host "[完成] 把 lingshu-skill 插件关一次再开一次（或重启 DSH），新版本才会被加载。" -ForegroundColor Green
Write-Host "       自检：python scripts\brain.py doctor   （看 release-drift 那一项）"
