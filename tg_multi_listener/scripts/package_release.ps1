# 在 tg_multi_listener 目录下执行：生成 exe 并覆盖式产出
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$DistOut = [System.IO.Path]::GetFullPath((Join-Path $Root "dist"))
Write-Host ("==> DistOut: {0}" -f $DistOut)

Write-Host "==> Installing deps (if missing)"
python -m pip install -q -r requirements.txt
python -m pip install -q -r requirements-build.txt

Write-Host "==> PyInstaller"
python -m PyInstaller --noconfirm --distpath "$DistOut" build_windows.spec

$ExeNested = [System.IO.Path]::Combine($DistOut, "ChaoQunHelper", "ChaoQunHelper.exe")
$ExeRoot = [System.IO.Path]::Combine($DistOut, "ChaoQunHelper.exe")

$hasNested = Test-Path -LiteralPath $ExeNested
$hasRoot = Test-Path -LiteralPath $ExeRoot

if (-not $hasNested -and -not $hasRoot) {
    Write-Error ("Missing PyInstaller output: {0} or {1}" -f $ExeNested, $ExeRoot)
    exit 1
}

Write-Host "==> Flatten dist root + finalize + zip"
python (Join-Path $Root "scripts\finalize_dist.py")
python (Join-Path $Root "scripts\assemble_release.py")

Write-Host ("Done. DistOut: {0}" -f $DistOut)
