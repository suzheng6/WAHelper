"""监听目标消息：收到时只记账，定时发送前由发送账号对该条标记已读（每号每条最多一次）。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from threading import RLock
from typing import Dict, List, Optional, Set, Tuple

from telethon import TelegramClient, utils

from .compat_config import chat_peer_ids_for_match
from .logger_util import warning

_MAX_PENDING_PER_CHAT = 64


@dataclass
class TgPendingWatchMessage:
    peer_id: int
    msg_id: int
    ts: float


class TgWatchReadTracker:
    def __init__(self) -> None:
        self._lock = RLock()
        self._pending: Dict[int, List[TgPendingWatchMessage]] = {}
        self._marked: Set[Tuple[str, int, int]] = set()

    def record(self, *, peer_id: int, msg_id: int) -> None:
        pid = int(peer_id)
        mid = int(msg_id)
        if not pid or not mid:
            return
        with self._lock:
            lst = self._pending.setdefault(pid, [])
            if any(x.msg_id == mid for x in lst):
                return
            lst.append(TgPendingWatchMessage(peer_id=pid, msg_id=mid, ts=time.time()))
            if len(lst) > _MAX_PENDING_PER_CHAT:
                self._pending[pid] = lst[-_MAX_PENDING_PER_CHAT:]

    def pick_for_send(self, account_id: str, peer_ids: Set[int]) -> Optional[TgPendingWatchMessage]:
        aid = (account_id or "").strip()
        if not aid or not peer_ids:
            return None
        with self._lock:
            for pid in sorted(peer_ids):
                for p in self._pending.get(int(pid), []):
                    if (aid, p.peer_id, p.msg_id) not in self._marked:
                        return p
            return None

    def mark_done(self, account_id: str, peer_id: int, msg_id: int) -> None:
        aid = (account_id or "").strip()
        if not aid:
            return
        with self._lock:
            self._marked.add((aid, int(peer_id), int(msg_id)))


def peer_ids_for_entity(entity: object) -> Set[int]:
    try:
        pid = int(utils.get_peer_id(entity))
    except Exception:
        return set()
    return set(chat_peer_ids_for_match(pid))


async def mark_tg_watch_message_read(
    client: TelegramClient,
    pending: TgPendingWatchMessage,
    entity: object,
) -> bool:
    try:
        await client.send_read_acknowledge(entity, max_id=pending.msg_id)
        return True
    except Exception as exc:
        warning(
            f"监听目标已读标记失败（不影响发送）：peer={pending.peer_id} msg={pending.msg_id} {exc}"
        )
        return False


async def mark_watch_read_before_send(
    client: TelegramClient,
    account_id: str,
    entity: object,
    tracker: Optional[TgWatchReadTracker],
) -> None:
    if tracker is None:
        return
    peer_ids = peer_ids_for_entity(entity)
    pending = tracker.pick_for_send(account_id, peer_ids)
    if pending is None:
        return
    if await mark_tg_watch_message_read(client, pending, entity):
        tracker.mark_done(account_id, pending.peer_id, pending.msg_id)
