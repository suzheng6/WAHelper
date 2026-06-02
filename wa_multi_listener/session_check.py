"""检测 WhatsApp 会话库是否已有登录数据（不弹窗）。"""
from __future__ import annotations

import os

from config import Account


def has_saved_session(acc: Account) -> bool:
    path = acc.db_path()
    if not os.path.isfile(path):
        return False
    try:
        from neonize.aioze.client import ClientFactory

        devs = ClientFactory.get_all_devices_from_db(path)
        if devs:
            return True
    except Exception:
        pass
    try:
        return os.path.getsize(path) > 8192
    except OSError:
        return False
