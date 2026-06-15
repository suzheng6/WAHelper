"""定时任务：本条消息发送后的延迟文字点赞（排除发送账号，其余角色随机 1–2 人）。"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from logger_util import debug, error, info, warning

REACTION_ROLE_LABELS: Tuple[str, ...] = ("女一", "女二", "男二", "主号")
_HEART = "❤️"
_THUMBS = "👍"
_REACTION_DELAY_MIN_SEC = 60.0
_REACTION_DELAY_MAX_SEC = 600.0
_PENDING_REACTION_TASKS: Set[asyncio.Task[Any]] = set()


def reaction_delay_seconds() -> float:
    """每条点赞独立随机等待 1–10 分钟（秒）。"""
    return random.uniform(_REACTION_DELAY_MIN_SEC, _REACTION_DELAY_MAX_SEC)


def reaction_emoji_for_role_label(label: str) -> str:
    if (label or "").strip() == "男二":
        return _THUMBS
    return _HEART


def pick_reaction_accounts(
    *,
    sender_account_id: str,
    main_account_id: str,
    available_account_ids: Set[str],
) -> List[Tuple[str, str]]:
    """从四角色池中排除发送账号，再随机 1–2 人点赞。返回 (角色名, 账号简称)。"""
    sender = (sender_account_id or "").strip()
    main = (main_account_id or "").strip()
    pool: List[Tuple[str, str]] = []
    for label in REACTION_ROLE_LABELS:
        acc = main if label == "主号" else label
        acc = (acc or "").strip()
        if not acc or acc == sender:
            continue
        if acc not in available_account_ids:
            continue
        pool.append((label, acc))
    if not pool:
        return []
    count = random.randint(1, min(2, len(pool)))
    return random.sample(pool, count)


@dataclass
class WaSentMessageMeta:
    chat_ref: str
    chat_jid: Any
    message_id: str
    sender_jid: Any


def _track_reaction_task(task: asyncio.Task[Any]) -> None:
    _PENDING_REACTION_TASKS.add(task)
    task.add_done_callback(_PENDING_REACTION_TASKS.discard)


async def _resolve_telegram_entity_for_send(client: Any, chat_ref: Union[int, str]) -> Any:
    if isinstance(chat_ref, str):
        s = chat_ref.strip()
        if not s:
            raise ValueError("empty chat ref")
        if (s.startswith("-") and len(s) > 1 and s[1:].isdigit()) or (s.isdigit() and not s.startswith("@")):
            chat_ref = int(s)
        else:
            try:
                return await client.get_entity(s)
            except Exception:
                await client.get_dialogs(limit=500)
                return await client.get_entity(s)
    cid = int(chat_ref)
    try:
        return await client.get_entity(cid)
    except Exception:
        await client.get_dialogs(limit=500)
        return await client.get_entity(cid)


async def telegram_react_once(client: Any, entity: Any, msg_id: int, emoji: str) -> None:
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji

    await client(
        SendReactionRequest(
            peer=entity,
            msg_id=int(msg_id),
            reaction=[ReactionEmoji(emoticon=emoji)],
        )
    )


async def _run_telegram_reaction_delayed(
    *,
    shared_clients: Dict[str, Any],
    account_locks: Dict[str, asyncio.Lock],
    chat_ref: Union[int, str],
    msg_id: int,
    acc_id: str,
    label: str,
    delay_sec: float,
) -> None:
    emoji = reaction_emoji_for_role_label(label)
    try:
        await asyncio.sleep(delay_sec)
        client = shared_clients.get(acc_id)
        if client is None:
            warning(f"定时点赞跳过：账号 {acc_id} 未连接（延迟后）")
            return
        lock = account_locks.get(acc_id)
        if lock is None:
            lock = asyncio.Lock()
            account_locks[acc_id] = lock
        async with lock:
            if not await client.is_user_authorized():
                warning(f"定时点赞跳过：账号 {acc_id} 未登录（延迟后）")
                return
            entity = await _resolve_telegram_entity_for_send(client, chat_ref)
            await telegram_react_once(client, entity, msg_id, emoji)
        info(f"定时点赞完成：账号={acc_id} 表情={emoji} 延迟={delay_sec / 60:.1f} 分钟")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error(f"定时点赞失败：账号={acc_id} 表情={emoji} 错误={exc}")


def schedule_telegram_reactions(
    *,
    shared_clients: Dict[str, Any],
    account_locks: Dict[str, asyncio.Lock],
    chat_ref: Union[int, str],
    msg_id: int,
    sender_account_id: str,
    main_account_id: str,
    enabled_account_ids: Set[str],
) -> int:
    """为每条消息排程延迟点赞；立即返回，各账号独立随机 1–10 分钟后执行。"""
    reactors = pick_reaction_accounts(
        sender_account_id=sender_account_id,
        main_account_id=main_account_id,
        available_account_ids=enabled_account_ids,
    )
    if not reactors:
        debug("定时点赞跳过：无可用点赞账号")
        return 0
    scheduled = 0
    for label, acc_id in reactors:
        if shared_clients.get(acc_id) is None:
            warning(f"定时点赞跳过：账号 {acc_id} 未连接")
            continue
        delay_sec = reaction_delay_seconds()
        task = asyncio.create_task(
            _run_telegram_reaction_delayed(
                shared_clients=shared_clients,
                account_locks=account_locks,
                chat_ref=chat_ref,
                msg_id=msg_id,
                acc_id=acc_id,
                label=label,
                delay_sec=delay_sec,
            )
        )
        _track_reaction_task(task)
        scheduled += 1
        info(f"定时点赞已排程：账号={acc_id} 表情={reaction_emoji_for_role_label(label)} 约 {delay_sec / 60:.1f} 分钟后")
    return scheduled


async def whatsapp_react_once(
    client: Any,
    chat_jid: Any,
    target_sender_jid: Any,
    message_id: str,
    emoji: str,
) -> None:
    reaction_msg = await client.build_reaction(chat_jid, target_sender_jid, message_id, emoji)
    await client.send_message(chat_jid, reaction_msg)


async def _run_whatsapp_reaction_delayed(
    *,
    shared_clients: Dict[str, Any],
    account_locks: Dict[str, asyncio.Lock],
    chat_jid: Any,
    message_id: str,
    target_sender_jid: Any,
    acc_id: str,
    label: str,
    delay_sec: float,
) -> None:
    emoji = reaction_emoji_for_role_label(label)
    try:
        await asyncio.sleep(delay_sec)
        client = shared_clients.get(acc_id)
        if client is None:
            warning(f"定时点赞跳过：账号 {acc_id} 未连接（延迟后）")
            return
        lock = account_locks.get(acc_id)
        if lock is None:
            lock = asyncio.Lock()
            account_locks[acc_id] = lock
        async with lock:
            await whatsapp_react_once(client, chat_jid, target_sender_jid, message_id, emoji)
        info(f"定时点赞完成：账号={acc_id} 表情={emoji} 延迟={delay_sec / 60:.1f} 分钟")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error(f"定时点赞失败：账号={acc_id} 表情={emoji} 错误={exc}")


def schedule_whatsapp_reactions(
    *,
    shared_clients: Dict[str, Any],
    account_locks: Dict[str, asyncio.Lock],
    chat_jid: Any,
    message_id: str,
    target_sender_jid: Any,
    sender_account_id: str,
    main_account_id: str,
    enabled_account_ids: Set[str],
) -> int:
    """为每条消息排程延迟点赞；立即返回，各账号独立随机 1–10 分钟后执行。"""
    reactors = pick_reaction_accounts(
        sender_account_id=sender_account_id,
        main_account_id=main_account_id,
        available_account_ids=enabled_account_ids,
    )
    if not reactors:
        debug("定时点赞跳过：无可用点赞账号")
        return 0
    scheduled = 0
    for label, acc_id in reactors:
        if shared_clients.get(acc_id) is None:
            warning(f"定时点赞跳过：账号 {acc_id} 未连接")
            continue
        delay_sec = reaction_delay_seconds()
        task = asyncio.create_task(
            _run_whatsapp_reaction_delayed(
                shared_clients=shared_clients,
                account_locks=account_locks,
                chat_jid=chat_jid,
                message_id=message_id,
                target_sender_jid=target_sender_jid,
                acc_id=acc_id,
                label=label,
                delay_sec=delay_sec,
            )
        )
        _track_reaction_task(task)
        scheduled += 1
        info(f"定时点赞已排程：账号={acc_id} 表情={reaction_emoji_for_role_label(label)} 约 {delay_sec / 60:.1f} 分钟后")
    return scheduled


# 兼容旧调用名（现为排程，非立即执行）
apply_telegram_scheduled_reactions = schedule_telegram_reactions
apply_whatsapp_scheduled_reactions = schedule_whatsapp_reactions


def resolve_main_account_for_job_target(cfg: Any, job: Any, chat_ref: Any) -> str:
    """从通讯录解析该发送目标对应的群主号/归属账号。"""
    emap = {e.id: e for e in getattr(cfg, "address_book", []) or []}
    ref_s = str(chat_ref or "").strip()
    for eid in getattr(job, "chat_entry_ids", None) or []:
        ent = emap.get(str(eid))
        if not ent:
            continue
        owner = (getattr(ent, "owner_account_id", None) or "").strip()
        if not owner:
            continue
        ent_ref = (getattr(ent, "chat_ref", None) or "").strip()
        ent_id = str(getattr(ent, "chat_id", None) or "").strip()
        if ref_s and (ref_s == ent_ref or ref_s == ent_id):
            return owner
    for eid in getattr(job, "chat_entry_ids", None) or []:
        ent = emap.get(str(eid))
        if ent:
            owner = (getattr(ent, "owner_account_id", None) or "").strip()
            if owner:
                return owner
    return ""
