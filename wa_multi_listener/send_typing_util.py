"""发消息前「正在输入」时长估算与 TG/WA 发送辅助。"""
from __future__ import annotations

import asyncio
import time
from typing import Any

# 约每 12 个字符 1 秒（中英文混合按 len）
_CHARS_PER_SECOND = 12.0
_MIN_TYPING_SECONDS = 1.0
_MAX_TYPING_SECONDS = 8.0
_TYPING_REFRESH_SECONDS = 4.5


def typing_duration_seconds(text: str) -> float:
    """按正文字数估算 typing 展示时长（秒）。"""
    n = len((text or "").strip())
    if n <= 0:
        return 0.0
    sec = n / _CHARS_PER_SECOND
    return max(_MIN_TYPING_SECONDS, min(_MAX_TYPING_SECONDS, sec))


async def telegram_typing_before_send(client: Any, entity: Any, text: str) -> None:
    """Telegram：在 send_message 前周期性上报 typing。"""
    duration = typing_duration_seconds(text)
    if duration <= 0:
        return
    deadline = time.monotonic() + duration
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        async with client.action(entity, "typing"):
            await asyncio.sleep(min(_TYPING_REFRESH_SECONDS, remaining))


async def whatsapp_typing_before_send(client: Any, jid: Any, text: str) -> None:
    """WhatsApp：在 send_message 前周期性上报 composing。"""
    from neonize.utils.enum import ChatPresence, ChatPresenceMedia

    duration = typing_duration_seconds(text)
    if duration <= 0:
        return
    deadline = time.monotonic() + duration
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            await client.send_chat_presence(
                jid,
                ChatPresence.CHAT_PRESENCE_COMPOSING,
                ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT,
            )
        except Exception:
            pass
        await asyncio.sleep(min(_TYPING_REFRESH_SECONDS, remaining))
