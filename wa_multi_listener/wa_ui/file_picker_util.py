"""文件选择对话框辅助。"""
from __future__ import annotations

import os
from typing import Optional


def txt_open_initial_dir(current_path: str = "") -> Optional[str]:
    """定时任务选 TXT：优先打开输入框里上次所选文件的目录；无则返回 None，由系统记住上次位置。"""
    cur = (current_path or "").strip()
    if not cur:
        return None
    d = os.path.dirname(os.path.abspath(cur))
    return d if os.path.isdir(d) else None
