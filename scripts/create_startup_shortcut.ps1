# 在登录启动文件夹创建看门狗快捷方式（登录后自动拉起数据服务）。
# 路径全部动态解析：仓库定位到本脚本所在 scripts/ 的上级；
# pythonw 优先取仓库 .venv（配 requirements.txt），没有则用系统 PATH 里的。
$ErrorActionPreference = 'Stop'

$startup = [Environment]::GetFolderPath('Startup')
$repo = Split-Path -Parent $PSScriptRoot
$here = $PSScriptRoot
$watchdog = Join-Path $here 'server_watchdog.pyw'

$pythonw = Join-Path $repo '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) {
    $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
}
if (-not $pythonw) {
    $pythonExe = (Get-Command python.exe -ErrorAction Stop).Source
    $pythonw = Join-Path (Split-Path $pythonExe -Parent) 'pythonw.exe'
}
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found: $pythonw" }

$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $startup 'desktop-deck-server-watchdog.lnk'))
$lnk.TargetPath = $pythonw
$lnk.Arguments = ('"' + $watchdog + '"')
$lnk.WorkingDirectory = $here
$lnk.Save()

# 清理旧项目名遗留的快捷方式（qoder-deck 时代，指向已失效的旧路径）
$legacy = Join-Path $startup 'qoder-deck-server-watchdog.lnk'
if (Test-Path $legacy) { Remove-Item $legacy; Write-Output ('removed legacy: ' + $legacy) }

Write-Output ('created: ' + $lnk.FullName)
Write-Output ('target:  ' + $pythonw + ' ' + $watchdog)
