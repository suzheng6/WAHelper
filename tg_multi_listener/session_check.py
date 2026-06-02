"""检测 session 是否已在 Telegram 端完成授权（不弹窗）。"""
from __future__ import annotations

import asyncio
import os

from telethon import TelegramClient

from .compat_config import Account, AppConfig


async def is_session_authorized(acc: Account, cfg: AppConfig, *, connect_timeout: float = 6.0) -> bool:
    if not int(cfg.api_id) or not str(cfg.api_hash).strip():
        return False
    path = acc.session_path()
    if not os.path.isfile(path):
        return False
    client = TelegramClient(path, cfg.api_id, cfg.api_hash)
    try:
        await asyncio.wait_for(client.connect(), timeout=float(connect_timeout))
        return bool(await client.is_user_authorized())
    except Exception:
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def is_session_authorized_sync(acc: Account, cfg: AppConfig, *, connect_timeout: float = 6.0) -> bool:
    try:
        return asyncio.run(is_session_authorized(acc, cfg, connect_timeout=connect_timeout))
    except RuntimeError:
        return False
    except Exception:
        return False
