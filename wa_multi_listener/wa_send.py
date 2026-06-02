"""向 WhatsApp 会话发送消息。"""
from __future__ import annotations

from typing import List, Union

from neonize.aioze.client import NewAClient
from neonize.proto.Neonize_pb2 import JID

from logger_util import error, warning
from wa_jid import invite_code_from_link, parse_chat_ref_to_jid


async def resolve_chat_jid(client: NewAClient, chat_ref: Union[str, int]) -> JID:
    ref = str(chat_ref).strip()
    code = invite_code_from_link(ref)
    if code:
        info = await client.get_group_info_from_link(code)
        return info.JID
    return parse_chat_ref_to_jid(ref)


async def send_text_to_chats(client: NewAClient, chat_refs: List[str], text: str) -> bool:
    ok = False
    for cref in chat_refs:
        try:
            jid = await resolve_chat_jid(client, cref)
            await client.send_message(jid, text)
            ok = True
        except Exception as exc:
            error(f"发送失败：目标={cref} 错误={exc}")
        if len(chat_refs) > 1:
            import asyncio

            await asyncio.sleep(0.25)
    if not ok and chat_refs:
        warning("本轮所有目标均未发送成功")
    return ok
