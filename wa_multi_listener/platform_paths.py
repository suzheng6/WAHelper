"""双平台数据目录：WhatsApp / Telegram 互不覆盖。"""
from __future__ import annotations

import os
import shutil

from paths import app_root

WA_DIRNAME = "whatsapp"
TG_DIRNAME = "telegram"


def combo_root() -> str:
    return app_root()


def wa_data_root() -> str:
    return os.path.join(combo_root(), WA_DIRNAME)


def tg_data_root() -> str:
    return os.path.join(combo_root(), TG_DIRNAME)


def _has_legacy_wa_flat(root: str) -> bool:
    return os.path.isfile(os.path.join(root, "config.json")) and not os.path.isdir(
        os.path.join(root, WA_DIRNAME)
    )


def migrate_legacy_wa_layout() -> bool:
    """将旧版「exe 根目录即数据目录」迁移到 whatsapp/ 子目录。"""
    root = combo_root()
    if not _has_legacy_wa_flat(root):
        return False
    dest = wa_data_root()
    os.makedirs(dest, exist_ok=True)
    for name in ("config.json", "config.example.json", "sessions", "data", "logs", "userdata_preserve"):
        src = os.path.join(root, name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(dest, name)
        if os.path.exists(dst):
            continue
        try:
            shutil.move(src, dst)
        except OSError:
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    return True
