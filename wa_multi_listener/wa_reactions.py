"""WhatsApp 定时任务：发送后延迟表情反应（排除发送账号，其余角色随机 1–2 人）。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from neonize.aioze.client import NewAClient
from neonize.proto.Neonize_pb2 import JID
from neonize.utils.jid import JIDToNonAD

from logger_util import debug, error, info, warning
from schedule_reactions import (
    _track_reaction_task,
    pick_reaction_accounts,
    reaction_delay_seconds,
    reaction_emoji_for_role_label,
    resolve_main_account_for_job_target,
)
from wa_jid import jid_nonempty


@dataclass
class WaSentMessageMeta:
    chat_ref: str
    chat_jid: JID
    message_id: str
    message_sender_jid: JID


async def canonical_sender_jid_for_reaction(client: NewAClient) -> Optional[JID]:
    """发送者 JID：优先手机号格式，便于其它账号在群内 build_reaction。"""
    try:
        me = await client.get_me()
    except Exception as exc:
        warning(f"[WA] 获取本账号 JID 失败：{exc}")
        return None
    jid = JIDToNonAD(me.JID)
    if not jid_nonempty(jid):
        return None
    if jid.Server == "lid":
        try:
            pn = await client.get_pn_from_lid(jid)
            if jid_nonempty(pn):
                return JIDToNonAD(pn)
        except Exception as exc:
            debug(f"[WA] LID→手机号失败，仍用原 JID：{exc}")
    return jid


async def whatsapp_send_reaction(
    client: NewAClient,
    chat_jid: JID,
    message_sender_jid: JID,
    message_id: str,
    emoji: str,
) -> None:
    """对指定消息发送表情反应（群聊需传入原消息发送者的 participant JID）。"""
    mid = (message_id or "").strip()
    if not mid:
        raise ValueError("message_id 为空")
    chat = JIDToNonAD(chat_jid)
    if not jid_nonempty(chat):
        raise ValueError("chat_jid 无效")
    sender = JIDToNonAD(message_sender_jid)
    if not jid_nonempty(sender):
        sender = await canonical_sender_jid_for_reaction(client)
    if sender is None or not jid_nonempty(sender):
        raise ValueError("无法解析原消息发送者 JID")
    reaction_msg = await client.build_reaction(chat, sender, mid, emoji)
    await client.send_message(chat, reaction_msg)


async def _run_whatsapp_reaction_delayed(
    *,
    shared_clients: Dict[str, NewAClient],
    account_locks: Dict[str, asyncio.Lock],
    chat_jid: JID,
    message_id: str,
    message_sender_jid: JID,
    acc_id: str,
    label: str,
    delay_sec: float,
) -> None:
    emoji = reaction_emoji_for_role_label(label)
    try:
        await asyncio.sleep(delay_sec)
        client = shared_clients.get(acc_id)
        if client is None:
            warning(f"[WA] 定时点赞跳过：账号 {acc_id} 未连接（延迟后）")
            return
        lock = account_locks.get(acc_id)
        if lock is None:
            lock = asyncio.Lock()
            account_locks[acc_id] = lock
        async with lock:
            await whatsapp_send_reaction(
                client, chat_jid, message_sender_jid, message_id, emoji
            )
        info(f"[WA] 定时点赞完成：账号={acc_id} 表情={emoji} 延迟={delay_sec / 60:.1f} 分钟")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error(f"[WA] 定时点赞失败：账号={acc_id} 表情={emoji} 错误={exc}")


def schedule_whatsapp_reactions(
    *,
    shared_clients: Dict[str, Any],
    account_locks: Dict[str, asyncio.Lock],
    chat_jid: JID,
    message_id: str,
    message_sender_jid: JID,
    sender_account_id: str,
    main_account_id: str,
    enabled_account_ids: Set[str],
) -> int:
    """为每条消息排程延迟点赞；立即返回，各账号独立随机 1–10 分钟后执行。"""
    mid = (message_id or "").strip()
    if not mid:
        debug("[WA] 定时点赞跳过：无 message_id")
        return 0
    if not jid_nonempty(message_sender_jid):
        debug("[WA] 定时点赞跳过：无原消息发送者 JID")
        return 0
    reactors = pick_reaction_accounts(
        sender_account_id=sender_account_id,
        main_account_id=main_account_id,
        available_account_ids=enabled_account_ids,
    )
    if not reactors:
        debug("[WA] 定时点赞跳过：无可用点赞账号")
        return 0
    scheduled = 0
    for label, acc_id in reactors:
        if shared_clients.get(acc_id) is None:
            warning(f"[WA] 定时点赞跳过：账号 {acc_id} 未连接")
            continue
        delay_sec = reaction_delay_seconds()
        task = asyncio.create_task(
            _run_whatsapp_reaction_delayed(
                shared_clients=shared_clients,
                account_locks=account_locks,
                chat_jid=chat_jid,
                message_id=mid,
                message_sender_jid=message_sender_jid,
                acc_id=acc_id,
                label=label,
                delay_sec=delay_sec,
            )
        )
        _track_reaction_task(task)
        scheduled += 1
        info(
            f"[WA] 定时点赞已排程：账号={acc_id} 表情={reaction_emoji_for_role_label(label)} "
            f"约 {delay_sec / 60:.1f} 分钟后"
        )
    return scheduled


__all__ = [
    "WaSentMessageMeta",
    "canonical_sender_jid_for_reaction",
    "schedule_whatsapp_reactions",
    "whatsapp_send_reaction",
    "resolve_main_account_for_job_target",
]
