"""WhatsApp JID 解析与比对。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

_resolved_chat_keys: Dict[str, Set[str]] = {}
_lid_to_phone: Dict[str, str] = {}

from neonize.proto.Neonize_pb2 import JID
from neonize.utils import build_jid
from neonize.utils.jid import Jid2String, JIDToNonAD

from logger_util import warning as log_warning


def normalize_phone(raw: str) -> str:
    return "".join(c for c in (raw or "") if c.isdigit())


def phones_equivalent(watch: str, sender_digits: str) -> bool:
    """比对监听手机号与发送者（含 +1 与本地 10 位等写法）。"""
    a = normalize_phone(watch)
    b = normalize_phone(sender_digits)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 10 and len(b) >= 10 and a[-10:] == b[-10:]:
        return True
    return False


def jid_to_key(jid: JID) -> str:
    return Jid2String(JIDToNonAD(jid)).lower()


def jid_nonempty(jid: Optional[JID]) -> bool:
    if jid is None:
        return False
    if not isinstance(jid, JID):
        return False
    if getattr(jid, "IsEmpty", False):
        return False
    return bool((getattr(jid, "User", None) or "").strip())


def parse_chat_ref_to_jid(ref: str) -> JID:
    """将配置中的群/会话标识转为 JID（邀请链接需先由 client 解析）。"""
    t = (ref or "").strip()
    if not t:
        raise ValueError("empty chat ref")
    if "@" in t:
        user, server = t.split("@", 1)
        return build_jid(user.strip(), server.strip())
    digits = normalize_phone(t)
    if digits:
        return build_jid(digits, "s.whatsapp.net")
    raise ValueError(f"无法解析会话：{ref}")


def invite_code_from_link(ref: str) -> Optional[str]:
    low = (ref or "").lower()
    if "chat.whatsapp.com/" not in low:
        return None
    tail = ref.split("chat.whatsapp.com/", 1)[-1].split("?", 1)[0].strip().strip("/")
    return tail or None


def keys_for_match(jid: JID) -> List[str]:
    k = jid_to_key(jid)
    out = [k]
    if jid.Server == "g.us":
        user = (jid.User or "").strip()
        out.append(f"{user}@g.us")
        if "-" in user:
            out.append(f"{user.split('-', 1)[0]}@g.us")
    return list(dict.fromkeys(out))


def register_resolved_chat(ref: str, jid: JID) -> None:
    """邀请链接解析后登记，供监听暂停定时任务时匹配群。"""
    key = (ref or "").strip().lower()
    if key and isinstance(jid, JID) and jid_nonempty(jid):
        _resolved_chat_keys[key] = set(keys_for_match(jid))


def coerce_chat_key_set(value: Any) -> Set[str]:
    """将 chat_keys / 缓存值规范为字符串键集合（避免误把 JID 传给 set()）。"""
    if value is None:
        return set()
    if isinstance(value, JID):
        return set(keys_for_match(value))
    if isinstance(value, str):
        return keys_for_chat_ref(value)
    out: Set[str] = set()
    if isinstance(value, (list, set, frozenset, tuple)):
        for item in value:
            out.update(coerce_chat_key_set(item))
    return out


def keys_for_chat_ref(ref: str) -> Set[str]:
    """JID / 手机号 / 已解析邀请链接 → 匹配键集合。"""
    cr = (ref or "").strip()
    if not cr:
        return set()
    low = cr.lower()
    if low in _resolved_chat_keys:
        return coerce_chat_key_set(_resolved_chat_keys[low])
    if invite_code_from_link(cr):
        return set()
    try:
        return set(keys_for_match(parse_chat_ref_to_jid(cr)))
    except ValueError:
        return set()


def chat_keys_from_ref(ref: str) -> Set[str]:
    return keys_for_chat_ref(ref)


def jid_from_chat_key(chat_key: str) -> Optional[JID]:
    ck = (chat_key or "").strip().lower()
    if "@" not in ck:
        return None
    user, server = ck.split("@", 1)
    return build_jid(user.strip(), server.strip())


def _gus_base(jid_key: str) -> str:
    ck = (jid_key or "").lower()
    if "@g.us" not in ck:
        return ""
    return ck.split("@", 1)[0].split("-", 1)[0]


def chat_matches_keys(chat_keys: Set[str], event_chat: JID) -> bool:
    if not chat_keys:
        return False
    ev_keys = set(keys_for_match(event_chat))
    if ev_keys.intersection(chat_keys):
        return True
    ev_base = {_gus_base(k) for k in ev_keys if _gus_base(k)}
    for ck in chat_keys:
        if ck in ev_keys:
            return True
        base = _gus_base(ck)
        if base and base in ev_base:
            return True
    return False


def chat_matches_ref(chat_ref: str, event_chat: JID, *, event_title: Optional[str] = None) -> bool:
    """兼容旧逻辑；邀请链接请使用 resolve 后的 chat_keys。"""
    keys = chat_keys_from_ref(chat_ref)
    if keys and chat_matches_keys(keys, event_chat):
        return True
    cr = (chat_ref or "").strip()
    if invite_code_from_link(cr):
        return False
    if event_title and cr.lower() == event_title.strip().lower():
        return True
    return False


def sender_phone_from_jid(jid: JID) -> str:
    return normalize_phone(jid.User)


def user_matches_watch(sender: JID, watch_phone: str) -> bool:
    return phones_equivalent(watch_phone, sender_phone_from_jid(sender))


def _phone_from_jid(jid: JID) -> str:
    return normalize_phone(jid.User)


async def _phone_for_sender_jid(client: Any, jid: JID) -> Optional[str]:
    if not jid_nonempty(jid):
        return None
    if jid.Server == "s.whatsapp.net":
        return _phone_from_jid(jid)
    if jid.Server == "lid":
        ck = jid_to_key(jid)
        cached = _lid_to_phone.get(ck)
        if cached:
            return cached
        try:
            pn = await client.get_pn_from_lid(jid)
            if jid_nonempty(pn) and pn.Server == "s.whatsapp.net":
                digits = _phone_from_jid(pn)
                if digits:
                    _lid_to_phone[ck] = digits
                    return digits
        except Exception as exc:
            log_warning(f"LID→手机号解析失败 {ck}：{exc}")
        return None
    digits = _phone_from_jid(jid)
    return digits or None


def sender_matches_watch_keys(
    sender: Optional[JID],
    sender_alt: Optional[JID],
    extra_jid: Optional[JID],
    watch_sender_keys: Set[str],
) -> bool:
    if not watch_sender_keys:
        return False
    for jid in (sender, sender_alt, extra_jid):
        if not jid_nonempty(jid):
            continue
        if set(keys_for_match(jid)).intersection(watch_sender_keys):
            return True
    return False


async def resolve_sender_phones_debug(
    client: Any,
    sender: Optional[JID],
    sender_alt: Optional[JID],
    extra_jid: Optional[JID] = None,
) -> List[str]:
    out: List[str] = []
    for jid in (sender_alt, sender, extra_jid):
        if not jid_nonempty(jid):
            continue
        phone = await _phone_for_sender_jid(client, jid)
        out.append(phone or jid_to_key(jid))
    return out


async def user_matches_watch_async(
    client: Any,
    sender: Optional[JID],
    sender_alt: Optional[JID],
    watch_phone: str,
    *,
    extra_jid: Optional[JID] = None,
) -> bool:
    """群消息发送者可能是 @lid；LID 模式下 SenderAlt 常为手机号。"""
    from neonize.utils.jid import jid_is_lid

    ordered: List[Optional[JID]] = []
    if sender and jid_is_lid(sender):
        ordered.extend([sender_alt, sender, extra_jid])
    else:
        ordered.extend([sender, sender_alt, extra_jid])

    seen: Set[str] = set()
    for jid in ordered:
        if not jid_nonempty(jid):
            continue
        ck = jid_to_key(jid)
        if ck in seen:
            continue
        seen.add(ck)
        phone = await _phone_for_sender_jid(client, jid)
        if phone and phones_equivalent(watch_phone, phone):
            return True
    return False
