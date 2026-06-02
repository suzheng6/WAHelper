"""应用目录：开发时为源码包所在文件夹；PyInstaller 打包后为 exe 所在文件夹。

持久化数据（config、sessions、logs）始终写在「分发根目录」，与 exe 放在一起，便于 U 盘拷贝。
随包只读资源（示例文档）在打包体内，通过 resource_path 读取。
"""
from __future__ import annotations

import os
import sys


def app_root() -> str:
    """用户可写根目录：exe 旁或绿色版解压目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(*parts: str) -> str:
    """打包进程序内的只读文件（开发时也可直接读源码旁文件）。"""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", app_root())
        return os.path.join(base, *parts)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)
