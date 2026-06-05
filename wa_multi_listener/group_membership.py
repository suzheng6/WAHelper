"""确认监听账号是否已加入目标群，并从成员表解析监听用户 JID/LID。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, List, Optional, Set

from neonize.proto.Neonize_pb2 import JID

from logger_util import info, warning
from wa_jid import (
    _phone_for_sender_jid,
    jid_nonempty,
    jid_to_key,
    keys_for_match,
    normalize_phone,
    phones_equivalent,
)

if TYPE_CHECKING:
    from neonize.aioze.client import NewAClient

GROUP_INFO_TIMEOUT_SEC = 20.0


async def _get_group_info_timed(client: "NewAClient", group_jid: JID) -> object:
    try:
        return await asyncio.wait_for(
            client.get_group_info(group_jid),
            timeout=GROUP_INFO_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"读取群信息超时（{GROUP_INFO_TIMEOUT_SEC:g} 秒）") from exc


def _iter_participants(gi) -> List:
    parts = getattr(gi, "Participants", None)
    if parts is None:
        return []
    out: List = []
    try:
        for item in parts:
            out.append(item)
        return out
    except TypeError:
        pass
    if isinstance(parts, JID) and jid_nonempty(parts):
        return [parts]
    if hasattr(parts, "PhoneNumber") or hasattr(parts, "JID") or hasattr(parts, "LID"):
        return [parts]
    return []


def _is_group_participant_row(row) -> bool:
    return hasattr(row, "PhoneNumber") or hasattr(row, "JID") or hasattr(row, "LID")


async def _participant_matches_watch_phone(
    client: "NewAClient",
    participant,
    watch_phone: str,
) -> bool:
    """成员表里 PhoneNumber 常为空；条目可能是 GroupParticipant 或裸 JID。"""
    if isinstance(participant, JID) and jid_nonempty(participant):
        if participant.Server == "s.whatsapp.net":
            if phones_equivalent(watch_phone, str(participant.User or "")):
                return True
        phone = await _phone_for_sender_jid(client, participant)
        return bool(phone and phones_equivalent(watch_phone, phone))

    if not _is_group_participant_row(participant):
        return False

    pphone = normalize_phone(getattr(participant, "PhoneNumber", None) or "")
    if pphone and phones_equivalent(watch_phone, pphone):
        return True
    for attr in ("JID", "LID"):
        pjid = getattr(participant, attr, None)
        if not jid_nonempty(pjid):
            continue
        if getattr(pjid, "Server", "") == "s.whatsapp.net":
            if phones_equivalent(watch_phone, str(getattr(pjid, "User", "") or "")):
                return True
        phone = await _phone_for_sender_jid(client, pjid)
        if phone and phones_equivalent(watch_phone, phone):
            return True
    return False


async def verify_account_in_group(client: "NewAClient", group_jid: JID, *, label: str = "") -> None:
    if not jid_nonempty(group_jid):
        return
    tag = label or jid_to_key(group_jid)
    me = client.me
    if me is None or not jid_nonempty(me):
        info(f"群「{tag}」：账号信息未就绪，暂无法校验是否已入群")
        return
    try:
        gi = await _get_group_info_timed(client, group_jid)
    except Exception as exc:
        warning(f"群「{tag}」：读取群信息失败（{exc}），请确认监听账号已加入该群")
        return

    my_key = jid_to_key(me)
    my_phone = normalize_phone(me.User)
    in_group = False
    for p in _iter_participants(gi):
        if _is_group_participant_row(p):
            if jid_nonempty(getattr(p, "JID", None)) and jid_to_key(p.JID) == my_key:
                in_group = True
                break
            pphone = normalize_phone(getattr(p, "PhoneNumber", None) or "")
        elif isinstance(p, JID) and jid_nonempty(p):
            if jid_to_key(p) == my_key:
                in_group = True
                break
            pphone = normalize_phone(p.User or "")
        else:
            continue
        if pphone and my_phone and phones_equivalent(my_phone, pphone):
            in_group = True
            break

    n = len(_iter_participants(gi))
    if in_group:
        info(f"群「{tag}」：监听账号已在群内（约 {n} 名成员）")
    else:
        warning(
            f"群「{tag}」：当前登录账号不在成员列表中（约 {n} 人）。"
            "未入群则收不到任何群消息，请换已加群的账号登录或先用该号加入群聊。"
        )


async def resolve_watch_user_keys_in_group(
    client: "NewAClient",
    group_jid: JID,
    watch_phone: str,
    *,
    label: str = "",
    quiet: bool = False,
) -> Set[str]:
    """从群成员表查找监听目标，返回其 JID/LID 匹配键（比实时 LID 解析更可靠）。"""
    tag = label or jid_to_key(group_jid)
    keys: Set[str] = set()
    try:
        gi = await _get_group_info_timed(client, group_jid)
    except Exception as exc:
        if not quiet:
            warning(f"群「{tag}」：读取成员失败，无法锁定监听用户 {watch_phone}（{exc}）")
        return keys

    for p in _iter_participants(gi):
        if await _participant_matches_watch_phone(client, p, watch_phone):
            parts: list[str] = []
            if _is_group_participant_row(p):
                if jid_nonempty(getattr(p, "JID", None)):
                    keys.update(keys_for_match(p.JID))
                    parts.append(jid_to_key(p.JID))
                if jid_nonempty(getattr(p, "LID", None)):
                    keys.update(keys_for_match(p.LID))
                    parts.append(jid_to_key(p.LID))
            elif isinstance(p, JID) and jid_nonempty(p):
                keys.update(keys_for_match(p))
                parts.append(jid_to_key(p))
            if not quiet:
                info(
                    f"群「{tag}」：成员表已锁定监听用户 {watch_phone} → "
                    + " / ".join(parts)
                )
            return keys

    if not quiet:
        n = len(_iter_participants(gi))
        if n == 0:
            warning(f"群「{tag}」：成员列表为空，无法确认 {watch_phone} 是否在群内。")
        else:
            warning(
                f"群「{tag}」：成员表中未找到 {watch_phone}（共 {n} 人）。"
                "请确认号码含国家码；仍将尝试按消息发送者 JID 匹配。"
            )
    return keys


async def watch_user_in_group(
    client: "NewAClient",
    group_jid: JID,
    watch_phone: str,
    *,
    label: str = "",
) -> Optional[bool]:
    """True=在群内，False=不在，None=成员列表不可用无法判断。"""
    tag = label or jid_to_key(group_jid)
    try:
        gi = await _get_group_info_timed(client, group_jid)
    except Exception:
        raise
    parts = _iter_participants(gi)
    if not parts:
        return None
    for p in parts:
        if await _participant_matches_watch_phone(client, p, watch_phone):
            return True
    return False


def extra_group_chat_keys(gi) -> Set[str]:
    """群 JID 及其关联父群/链接群，避免消息事件里 chat 与邀请解析结果不一致。"""
    keys: Set[str] = set()
    if gi and gi.JID and not gi.JID.IsEmpty:
        keys.update(keys_for_match(gi.JID))
    lp = getattr(gi, "GroupLinkedParent", None)
    if lp is not None:
        parent = getattr(lp, "LinkedParentJID", None)
        if parent is not None and jid_nonempty(parent):
            keys.update(keys_for_match(parent))
    return keys
