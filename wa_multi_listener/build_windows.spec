# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir 打包：无控制台窗口，数据与 exe 同目录。"""
import os

import neonize
from PyInstaller.utils.hooks import collect_all, collect_submodules
from neonize.utils.platform import generated_name

block_cipher = None

ROOT = os.path.dirname(os.path.abspath(SPEC))
PARENT = os.path.normpath(os.path.join(ROOT, ".."))
DIST = os.path.normpath(os.path.join(ROOT, "dist"))

EXE_NAME_ASCII = "WAHelper"
COLLECT_NAME = os.environ.get("WA_COLLECT_NAME", EXE_NAME_ASCII)
_ICON = r"C:\Users\USER\Pictures\icon.ico"


def _collect_wa_modules() -> list:
    names: list = []
    for fn in os.listdir(ROOT):
        if fn.endswith(".py") and fn != "main.py":
            names.append(os.path.splitext(fn)[0])
    wa_ui = os.path.join(ROOT, "wa_ui")
    if os.path.isdir(wa_ui):
        names.append("wa_ui")
        for fn in os.listdir(wa_ui):
            if fn.endswith(".py") and not fn.startswith("__"):
                names.append("wa_ui." + os.path.splitext(fn)[0])
    return names


_main = os.path.join(ROOT, "main.py")
_datas = []
_binaries = []
_hiddenimports = [
    "customtkinter",
    "PIL",
    "PIL._tkinter_finder",
    "segno",
    "phonenumbers",
    "magic",
    "linkpreview",
    "neonize",
    "neonize.aioze",
    "neonize.aioze.client",
    "neonize.aioze.events",
    "neonize._binder",
    "neonize.proto",
    "neonize.proto.Neonize_pb2",
    "neonize.utils",
    "neonize.utils.jid",
    "neonize.utils.message",
    "telethon",
    "telethon.crypto",
    "telethon.extensions",
] + _collect_wa_modules()

try:
    _hiddenimports += collect_submodules("tg_multi_listener")
except Exception:
    _hiddenimports += [
        "tg_multi_listener",
        "tg_multi_listener.compat_config",
        "tg_multi_listener.config",
        "tg_multi_listener.ui.embed",
        "tg_multi_listener.ui.app",
        "tg_multi_listener.ui.theme",
        "tg_multi_listener.ui.login_dialog",
        "tg_multi_listener.listener",
        "tg_multi_listener.telethon_coordinator",
        "tg_multi_listener.telethon_auth",
        "tg_multi_listener.scheduler",
        "tg_multi_listener.schedule2_runner",
        "tg_multi_listener.schedule_txt_import",
        "tg_multi_listener.notifier",
        "tg_multi_listener.stats",
        "tg_multi_listener.group_owner",
        "tg_multi_listener.session_check",
        "tg_multi_listener.paths",
        "tg_multi_listener.logger_util",
    ]

for pkg in ("neonize", "phonenumbers", "google.protobuf", "telethon"):
    try:
        d, b, h = collect_all(pkg)
        _datas += d
        _binaries += b
        _hiddenimports += h
    except Exception:
        pass

_neonize_root = os.path.dirname(neonize.__file__)
_go_dll = os.path.join(_neonize_root, generated_name())
if os.path.isfile(_go_dll):
    _binaries.append((_go_dll, "neonize"))
else:
    raise FileNotFoundError(f"未找到 neonize 动态库：{_go_dll}")

for _doc_name in ("定时任务导入说明与示例.txt", "整合版说明.txt"):
    _doc = os.path.join(ROOT, "docs", _doc_name)
    if os.path.isfile(_doc):
        _datas.append((_doc, "docs"))
for _cfg_name in ("config.example.json", "config.example.tg.json"):
    _cfg = os.path.join(ROOT, _cfg_name)
    if os.path.isfile(_cfg):
        _datas.append((_cfg, "."))

_rthook = os.path.join(ROOT, "hooks", "rthook_wa_path.py")

a = Analysis(
    [_main],
    pathex=[ROOT, PARENT],
    binaries=_binaries,
    datas=_datas,
    hiddenimports=_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[_rthook] if os.path.isfile(_rthook) else [],
    excludes=["ui"],
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
    name=COLLECT_NAME,
    distpath=DIST,
)
