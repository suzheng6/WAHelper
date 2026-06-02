"""整合版与独立运行双模式下的 config 导入。"""
from __future__ import annotations

try:
    from tg_multi_listener.config import *  # noqa: F403, F401
except ImportError:
    from config import *  # noqa: F403, F401
