"""检测通讯录监听用户是否仍在对应 WhatsApp 群内。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict

from neonize.aioze.client import NewAClient

from config import AppConfig, parse_watch_user_input
from group_membership import watch_user_in_group
from wa_send import resolve_chat_jid


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


async def audit_address_book_watch_users(
    cfg: AppConfig,
    clients: Dict[str, NewAClient],
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
        client = clients.get(owner) if owner else primary
        try:
            watch = parse_watch_user_input(ent.watch_user)
        except ValueError as exc:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.ERROR, str(exc))
            continue
        label = (ent.remark or ent.id).strip()
        try:
            group_jid = await resolve_chat_jid(client, ent.chat_ref)
        except Exception as exc:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.ERROR, f"群解析失败：{exc}")
            continue
        try:
            found = await watch_user_in_group(client, group_jid, watch, label=label)
        except Exception as exc:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.ERROR, str(exc))
            continue
        if found:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.OK)
        else:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.ABSENT, watch)
    return out
