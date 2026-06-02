"""监听目标消息：收到时只记账，定时发送前由发送账号对该条标记已读（每号每条最多一次）。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from threading import RLock
from typing import Dict, List, Optional, Set, Tuple

from neonize.aioze.client import NewAClient
from neonize.proto.Neonize_pb2 import JID
from neonize.utils.enum import ReceiptType

from logger_util import debug
from wa_jid import keys_for_chat_ref, keys_for_match
from wa_send import resolve_chat_jid

_MAX_PENDING_PER_CHAT = 64


@dataclass
class WaPendingWatchMessage:
    chat_key: str
    msg_id: str
    chat_jid: JID
    sender_jid: JID
    ts: float


class WaWatchReadTracker:
    def __init__(self) -> None:
        self._lock = RLock()
        self._pending: Dict[str, List[WaPendingWatchMessage]] = {}
        self._marked: Set[Tuple[str, str, str]] = set()

    def record(
        self,
        *,
        chat_key: str,
        msg_id: str,
        chat_jid: JID,
        sender_jid: JID,
    ) -> None:
        ck = (chat_key or "").strip().lower()
        mid = (msg_id or "").strip()
        if not ck or not mid:
            return
        with self._lock:
            lst = self._pending.setdefault(ck, [])
            if any(x.msg_id == mid for x in lst):
                return
            lst.append(
                WaPendingWatchMessage(
                    chat_key=ck,
                    msg_id=mid,
                    chat_jid=chat_jid,
                    sender_jid=sender_jid,
                    ts=time.time(),
                )
            )
            if len(lst) > _MAX_PENDING_PER_CHAT:
                self._pending[ck] = lst[-_MAX_PENDING_PER_CHAT:]

    def pick_for_send(self, account_id: str, chat_keys: Set[str]) -> Optional[WaPendingWatchMessage]:
        aid = (account_id or "").strip()
        if not aid or not chat_keys:
            return None
        with self._lock:
            for ck in sorted(chat_keys):
                key = ck.strip().lower()
                for p in self._pending.get(key, []):
                    if (aid, p.chat_key, p.msg_id) not in self._marked:
                        return p
            return None

    def mark_done(self, account_id: str, chat_key: str, msg_id: str) -> None:
        aid = (account_id or "").strip()
        ck = (chat_key or "").strip().lower()
        mid = (msg_id or "").strip()
        if not aid or not ck or not mid:
            return
        with self._lock:
            self._marked.add((aid, ck, mid))


async def _resolve_chat_keys(client: NewAClient, chat_ref: str) -> Set[str]:
    keys = keys_for_chat_ref(chat_ref)
    if keys:
        return keys
    try:
        jid = await resolve_chat_jid(client, chat_ref)
        return set(keys_for_match(jid))
    except Exception:
        return set()


async def mark_wa_watch_message_read(client: NewAClient, pending: WaPendingWatchMessage) -> bool:
    try:
        await client.mark_read(
            pending.msg_id,
            chat=pending.chat_jid,
            sender=pending.sender_jid,
            receipt=ReceiptType.READ,
        )
        return True
    except Exception as exc:
        debug(f"监听目标已读标记失败（不影响发送）：群={pending.chat_key} msg={pending.msg_id} {exc}")
        return False


async def mark_watch_read_before_send(
    client: NewAClient,
    account_id: str,
    chat_ref: str,
    tracker: Optional[WaWatchReadTracker],
) -> None:
    if tracker is None:
        return
    keys = await _resolve_chat_keys(client, chat_ref)
    pending = tracker.pick_for_send(account_id, keys)
    if pending is None:
        return
    if await mark_wa_watch_message_read(client, pending):
        tracker.mark_done(account_id, pending.chat_key, pending.msg_id)
