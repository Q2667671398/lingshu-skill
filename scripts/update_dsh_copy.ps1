# 灵枢 → DSH 插件目录热同步（不重启 DSH、不走 pnpm 重装）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\update_dsh_copy.ps1
# 原理：robocopy 增量复制到 node_modules\lingshu-skill，被 DSH 占用的文件也能就地覆盖。

$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$src = Split-Path -Parent $PSScriptRoot          # 仓库根目录（本脚本在 scripts/ 下）
$dst = "$env:USERPROFILE\.dsh\profiles\desktop\node_modules\lingshu-skill"

if (-not (Test-Path $dst)) { Write-Host "DSH plugin dir not found: $dst" -ForegroundColor Red; exit 1 }

# 排除 .git / 工作残留，不把开发目录的杂物带进插件
robocopy $src $dst /E /COPY:DT /R:0 /W:0 /NFL /NDL /NJH /XD "$src\.git" "$src\.trae" | Out-Null

$code = $LASTEXITCODE
if ($code -ge 8) {
    Write-Host "robocopy failed, exit code $code" -ForegroundColor Red; exit $code
}

$ver = (Get-Content "$dst\package.json" -Raw | ConvertFrom-Json).version
Write-Host "Sync done. Plugin version: v$ver (robocopy code ${code}: 0=no change 1=updated)" -ForegroundColor Green
