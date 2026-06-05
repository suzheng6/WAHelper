"""检测通讯录监听用户是否仍在对应 WhatsApp 群内。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

from neonize.aioze.client import NewAClient
from neonize.proto.Neonize_pb2 import JID

from config import AppConfig, parse_watch_user_input
from group_membership import watch_user_in_group
from invite_resolve import resolve_invite_ref
from logger_util import warning
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


async def _soft_wait_client_me(client: NewAClient, *, timeout: float = 45.0) -> bool:
    """等待 client.me；超时后仍继续尝试读群成员（部分环境下 me 迟迟不同步但 API 可用）。"""
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        me = client.me
        if me is not None and jid_nonempty(me):
            return True
        await asyncio.sleep(0.5)
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


def _clients_for_entry(
    clients: Dict[str, NewAClient],
    primary: NewAClient,
    owner: str,
) -> List[NewAClient]:
    ordered: List[NewAClient] = []
    seen: set[int] = set()

    def add(c: Optional[NewAClient]) -> None:
        if c is None:
            return
        key = id(c)
        if key in seen:
            return
        seen.add(key)
        ordered.append(c)

    if owner and owner in clients:
        add(clients[owner])
    add(primary)
    for c in clients.values():
        add(c)
    return ordered


async def _resolve_group_jid_with_fallback(
    clients: Dict[str, NewAClient],
    primary: NewAClient,
    owner: str,
    chat_ref: str,
) -> Tuple[JID, NewAClient]:
    last_exc: Optional[Exception] = None
    for client in _clients_for_entry(clients, primary, owner):
        try:
            return await _resolve_group_jid(client, chat_ref), client
        except Exception as exc:
            last_exc = exc
    raise last_exc or ValueError("无法解析群")


async def audit_address_book_watch_users(
    cfg: AppConfig,
    clients: Dict[str, NewAClient],
) -> Dict[str, WatchAuditRow]:
    out: Dict[str, WatchAuditRow] = {}
    if not clients:
        return out
    primary = next(iter(clients.values()))
    warmed: set[int] = set()
    for ent in cfg.address_book:
        eid = ent.id
        if not ent.listen_enabled or not (ent.watch_user or "").strip():
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.SKIP)
            continue
        owner = (ent.owner_account_id or "").strip()
        label = (ent.remark or ent.id).strip()
        if owner and owner not in clients:
            row = WatchAuditRow(
                eid,
                WatchAuditStatus.OFFLINE,
                f"归属账号「{owner}」未在线",
            )
            out[eid] = row
            warning(f"成员检测「{label}」：{row.detail}")
            continue
        preferred = clients.get(owner) if owner else primary
        pid = id(preferred)
        if pid not in warmed:
            warmed.add(pid)
            await _soft_wait_client_me(preferred)
        try:
            watch = parse_watch_user_input(ent.watch_user)
        except ValueError as exc:
            row = WatchAuditRow(eid, WatchAuditStatus.ERROR, str(exc))
            out[eid] = row
            warning(f"成员检测「{label}」：{row.detail}")
            continue
        try:
            group_jid, group_client = await _resolve_group_jid_with_fallback(
                clients, primary, owner, ent.chat_ref
            )
        except Exception as exc:
            row = WatchAuditRow(eid, WatchAuditStatus.ERROR, f"群解析失败：{exc}")
            out[eid] = row
            warning(f"成员检测「{label}」：{row.detail}")
            continue
        try:
            found = await watch_user_in_group(group_client, group_jid, watch, label=label)
        except Exception as exc:
            row = WatchAuditRow(eid, WatchAuditStatus.ERROR, f"读取群成员失败：{exc}")
            out[eid] = row
            warning(f"成员检测「{label}」：{row.detail}")
            continue
        if found is None:
            row = WatchAuditRow(
                eid,
                WatchAuditStatus.ERROR,
                "群成员列表为空，WhatsApp 未返回成员数据，无法检测",
            )
            out[eid] = row
            warning(f"成员检测「{label}」：{row.detail}")
        elif found:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.OK)
        else:
            out[eid] = WatchAuditRow(eid, WatchAuditStatus.ABSENT, watch)
        await asyncio.sleep(0.35)
    return out
