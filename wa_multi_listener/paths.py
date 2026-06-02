"""应用目录：开发时为包目录；打包后为 exe 旁。"""
from __future__ import annotations

import os
import sys


def app_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(*parts: str) -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", app_root())
        return os.path.join(base, *parts)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)
