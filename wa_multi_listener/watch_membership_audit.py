"""检测通讯录监听用户是否仍在对应 WhatsApp 群内。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

from neonize.aioze.client import NewAClient
from neonize.proto.Neonize_pb2 import JID

from config import AppConfig, parse_watch_user_input
from group_membership import watch_user_in_group
from invite_resolve import resolve_invite_ref
from wa_jid import invite_code_from_link, jid_from_chat_key, jid_nonempty, keys_for_chat_ref, parse_chat_ref_to_jid
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


async def _wait_client_ready(client: NewAClient, *, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        me = client.me
        if me is not None and jid_nonempty(me):
            return True
        await asyncio.sleep(0.25)
    return False


async def _resolve_group_jid(client: NewAClient, chat_ref: str) -> JID:
    cref = (chat_ref or "").strip()
    if not cref:
        raise ValueError("群标识为空")
    if invite_code_from_link(cref):
        resolved = await resolve_invite_ref(client, cref, log_label="成员检测")
        if resolved and "@g.us" in resolved.lower():
            return parse_chat_ref_to_jid(resolved)
    for key in keys_for_chat_ref(cref):
        if "@g.us" not in key:
            continue
        jid = jid_from_chat_key(key)
        if jid is not None:
            return jid
    return await resolve_chat_jid(client, cref)


async def audit_address_book_watch_users(
    cfg: AppConfig,
    clients: Dict[str, NewAClient],
) -> Dict[str, WatchAuditRow]:
    out: Dict[str, WatchAuditRow] = {}
    if not clients:
        return out
    primary = next(iter(clients.values()))
    ready_clients: Dict[str, bool] = {}
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
        cid = owner or next(iter(clients))
        if cid not in ready_clients:
            ready_clients[cid] = await _wait_client_ready(client)
        if not ready_clients[cid]:
            out[eid] = WatchAuditRow(
                eid,
                WatchAuditStatus.ERROR,
                "账号信息未就绪，请稍后再试",
            )
            continue
        try:
            watch = parse_watch_user_input(ent.watch_user)
        except ValueError as exc:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.ERROR, str(exc))
            continue
        label = (ent.remark or ent.id).strip()
        try:
            group_jid = await _resolve_group_jid(client, ent.chat_ref)
        except Exception as exc:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.ERROR, f"群解析失败：{exc}")
            continue
        try:
            found = await watch_user_in_group(client, group_jid, watch, label=label)
        except Exception as exc:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.ERROR, f"读取群成员失败：{exc}")
            continue
        if found is None:
            out[eid] = WatchAuditRow(
                eid,
                WatchAuditStatus.ERROR,
                "群成员列表为空，WhatsApp 未返回成员数据，无法检测",
            )
        elif found:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.OK)
        else:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.ABSENT, watch)
        await asyncio.sleep(0.35)
    return out
