"""确认监听账号是否已加入目标群，并从成员表解析监听用户 JID/LID。"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Set

from neonize.proto.Neonize_pb2 import JID

from logger_util import info, warning
from wa_jid import jid_nonempty, jid_to_key, keys_for_match, normalize_phone, phones_equivalent

if TYPE_CHECKING:
    from neonize.aioze.client import NewAClient


def _iter_participants(gi) -> List:
    parts = getattr(gi, "Participants", None)
    if parts is None:
        return []
    if isinstance(parts, JID):
        return []
    try:
        return list(parts)
    except TypeError:
        return []


async def verify_account_in_group(client: "NewAClient", group_jid: JID, *, label: str = "") -> None:
    if not jid_nonempty(group_jid):
        return
    tag = label or jid_to_key(group_jid)
    me = client.me
    if me is None or not jid_nonempty(me):
        info(f"群「{tag}」：账号信息未就绪，暂无法校验是否已入群")
        return
    try:
        gi = await client.get_group_info(group_jid)
    except Exception as exc:
        warning(f"群「{tag}」：读取群信息失败（{exc}），请确认监听账号已加入该群")
        return

    my_key = jid_to_key(me)
    my_phone = normalize_phone(me.User)
    in_group = False
    for p in _iter_participants(gi):
        if jid_nonempty(p.JID) and jid_to_key(p.JID) == my_key:
            in_group = True
            break
        pphone = normalize_phone(p.PhoneNumber or "")
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
) -> Set[str]:
    """从群成员表查找监听目标，返回其 JID/LID 匹配键（比实时 LID 解析更可靠）。"""
    tag = label or jid_to_key(group_jid)
    keys: Set[str] = set()
    try:
        gi = await client.get_group_info(group_jid)
    except Exception as exc:
        warning(f"群「{tag}」：读取成员失败，无法锁定监听用户 {watch_phone}（{exc}）")
        return keys

    for p in _iter_participants(gi):
        pphone = normalize_phone(p.PhoneNumber or "")
        if not pphone or not phones_equivalent(watch_phone, pphone):
            continue
        parts: list[str] = []
        if jid_nonempty(p.JID):
            keys.update(keys_for_match(p.JID))
            parts.append(jid_to_key(p.JID))
        if jid_nonempty(p.LID):
            keys.update(keys_for_match(p.LID))
            parts.append(jid_to_key(p.LID))
        info(
            f"群「{tag}」：成员表已锁定监听用户 {watch_phone} → "
            + " / ".join(parts)
        )
        return keys

    warning(
        f"群「{tag}」：成员表中未找到 {watch_phone}（共 {len(_iter_participants(gi))} 人）。"
        "请确认号码含国家码；仍将尝试按消息发送者 JID 匹配。"
    )
    return keys


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
