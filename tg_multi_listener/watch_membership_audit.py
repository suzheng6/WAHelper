"""检测通讯录监听用户是否仍在对应 Telegram 群内。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Union

from telethon import TelegramClient
from telethon.errors import UserNotParticipantError
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import Channel, Chat

from .compat_config import AppConfig, chat_ref_to_optional_int, parse_chat_ref_input, parse_watch_user_input
from .listener import (
    _resolve_chat_ref_with_clients,
    _resolve_user_target,
    _resolution_clients_ordered,
)


class WatchAuditStatus(str, Enum):
    SKIP = "skip"
    OK = "ok"
    ABSENT = "absent"
    OFFLINE = "offline"
    ERROR = "error"


@dataclass
class WatchAuditRow:
    entry_id: str
    status: WatchAuditStatus
    detail: str = ""


async def _user_in_group_entity(
    client: TelegramClient,
    group_entity: Union[Channel, Chat],
    user_id: int,
) -> bool:
    if isinstance(group_entity, Channel):
        try:
            user_ent = await client.get_input_entity(user_id)
            await client(GetParticipantRequest(group_entity, user_ent))
            return True
        except UserNotParticipantError:
            return False
    try:
        async for p in client.iter_participants(group_entity, limit=2000):
            if int(p.id) == int(user_id):
                return True
        return False
    except UserNotParticipantError:
        return False


async def audit_address_book_watch_users(
    cfg: AppConfig,
    clients: Dict[str, TelegramClient],
) -> Dict[str, WatchAuditRow]:
    out: Dict[str, WatchAuditRow] = {}
    if not clients:
        return out
    primary = next(iter(clients.values()))
    for ent in cfg.address_book:
        eid = ent.id
        if not ent.listen_enabled or not (ent.watch_user or "").strip():
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.SKIP)
            continue
        owner = (ent.owner_account_id or "").strip()
        if owner and owner not in clients:
            out[eid] = WatchAuditRow(
                eid,
                WatchAuditStatus.OFFLINE,
                f"归属账号「{owner}」未在线",
            )
            continue
        chat_client = clients.get(owner) if owner else primary
        try:
            target = parse_watch_user_input((ent.watch_user or "").strip())
        except ValueError as exc:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.ERROR, str(exc))
            continue
        label = (ent.remark or ent.id).strip()
        ref = (ent.chat_ref or "").strip()
        peer_id: Optional[int] = chat_ref_to_optional_int(ref)
        if peer_id is None:
            try:
                chat_ref = parse_chat_ref_input(ref)
            except ValueError as exc:
                out[eid] = WatchAuditRow(eid, WatchAuditStatus.ERROR, str(exc))
                continue
            ordered = _resolution_clients_ordered(primary, clients, owner)
            peer_id = await _resolve_chat_ref_with_clients(
                ordered,
                chat_ref,
                context=f"成员检测「{label}」",
                skip_dialogs_fallback=True,
            )
            if peer_id is None:
                out[eid] = WatchAuditRow(eid, WatchAuditStatus.ERROR, "无法解析群")
                continue
        try:
            group_entity = await chat_client.get_entity(int(peer_id))
        except Exception as exc:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.ERROR, f"群实体失败：{exc}")
            continue
        uid_client = clients.get(owner) if owner else primary
        user_id = await _resolve_user_target(uid_client, target)
        if user_id is None and uid_client is not primary:
            user_id = await _resolve_user_target(primary, target)
        if user_id is None:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.ERROR, "无法解析监听用户")
            continue
        try:
            found = await _user_in_group_entity(chat_client, group_entity, int(user_id))
        except Exception as exc:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.ERROR, str(exc))
            continue
        if found:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.OK)
        else:
            watch_label = str(target) if not isinstance(target, int) else ent.watch_user.strip()
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.ABSENT, watch_label)
    return out
