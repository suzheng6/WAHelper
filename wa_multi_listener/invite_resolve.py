"""群邀请链接解析（连接后异步）。"""
from __future__ import annotations

from typing import Optional

from neonize.aioze.client import NewAClient

from logger_util import info, warning
from wa_jid import invite_code_from_link, jid_to_key, register_resolved_chat


async def resolve_invite_ref(client: NewAClient, chat_ref: str, *, log_label: str = "群") -> Optional[str]:
    """将邀请链接解析为 @g.us JID 字符串；非链接则原样返回。"""
    cref = (chat_ref or "").strip()
    if not cref:
        return None
    code = invite_code_from_link(cref)
    if not code:
        return cref
    try:
        gi = await client.get_group_info_from_link(code)
        if gi and gi.JID and not gi.JID.IsEmpty:
            register_resolved_chat(cref, gi.JID)
            resolved = jid_to_key(gi.JID)
            name = gi.GroupName.Name if gi.GroupName else ""
            info(f"{log_label}邀请已解析：{cref[:50]}… → {resolved}" + (f"（{name}）" if name else ""))
            return resolved
        warning(f"无法从邀请链接取得群 JID：{cref}")
    except Exception as exc:
        warning(f"解析群邀请失败「{cref[:60]}」：{exc}")
    return None
