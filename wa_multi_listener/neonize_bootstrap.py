"""启动前：若程序目录有补丁版 neonize DLL，则覆盖 site-packages 内同名文件。"""
from __future__ import annotations

import importlib.util
import os
import shutil

from logger_util import info, warning
from paths import app_root


def _dll_name() -> str:
    import platform

    plat = platform.system()
    if plat == "Windows":
        return "neonize-windows-amd64.dll"
    if plat == "Darwin":
        return "neonize-darwin-amd64.dylib"
    return "neonize-linux-amd64.so"


def install_patched_neonize_dll_if_present() -> None:
    import sys

    name = _dll_name()
    src = os.path.join(app_root(), name)
    if not os.path.isfile(src):
        return
    # 打包版：DLL 已在 exe 旁，无需覆盖 _internal（避免文件占用 WinError 32）
    if getattr(sys, "frozen", False):
        bundled = os.path.join(app_root(), name)
        if os.path.isfile(bundled):
            return
    spec = importlib.util.find_spec("neonize")
    if not spec or not spec.origin:
        warning("未找到 neonize 包，无法安装补丁 DLL")
        return
    dst = os.path.join(os.path.dirname(spec.origin), name)
    try:
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
            return
        shutil.copy2(src, dst)
        info(f"已应用程序目录下的 neonize 补丁库：{name}")
    except OSError as exc:
        warning(f"复制 neonize 补丁库失败：{exc}")
