# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller：目录分发（onedir），避免 onefile 每次启动解压导致卡顿与「未响应」感；窗口模式无控制台。"""
import os

block_cipher = None

# 本 spec 位于 tg_multi_listener 目录下，与 main.py 同级
ROOT = os.path.dirname(os.path.abspath(SPEC))
# 固定输出到项目 dist（例如 C:\...\tg_multi_listener\dist），不依赖当前工作目录
DIST = os.path.normpath(os.path.join(ROOT, "dist"))

# 构建产物先用 ASCII 文件名（兼容性）；发布后复制为「超群小帮手.exe」，见 scripts/finalize_dist.py 与 package_release.ps1。
EXE_NAME_ASCII = "ChaoQunHelper"
_ICON = r"C:\Users\USER\Pictures\icon.ico"


_main = os.path.join(ROOT, "main.py")
_datas = []
_doc = os.path.join(ROOT, "docs", "定时任务导入说明与示例.txt")
if os.path.isfile(_doc):
    _datas.append((_doc, "docs"))
_cfg = os.path.join(ROOT, "config.example.json")
if os.path.isfile(_cfg):
    _datas.append((_cfg, "."))

a = Analysis(
    [_main],
    pathex=[ROOT],
    binaries=[],
    datas=_datas,
    hiddenimports=[
        "telethon",
        "telethon.crypto",
        "telethon.extensions",
        "customtkinter",
        "PIL",
        "PIL._tkinter_finder",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=EXE_NAME_ASCII,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_ICON if os.path.isfile(_ICON) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=EXE_NAME_ASCII,
    distpath=DIST,
)
