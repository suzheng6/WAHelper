"""多账号 Telethon 监听：群过滤、用户绑定、按群限流、去重。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from telethon import TelegramClient, events, utils

from .compat_config import (
    AddressEntry,
    AppConfig,
    ChatRef,
    WatchTarget,
    chat_peer_ids_for_match,
    chat_ref_to_optional_int,
    format_listener_chat_label,
    iter_listen_bindings,
    load_config,
    parse_chat_ref_input,
    parse_watch_user_input,
    save_config,
)
from .config import _parse_watch_rule_value
from .logger_util import error, info, warning

AlertPayload = Dict[str, Any]

_last_alert_ts: Dict[int, float] = {}
_seen_msgs: Dict[Tuple[int, int], float] = {}
_seen_leaves: Dict[Tuple[int, int], float] = {}
_DEDUP_TTL = 30.0


def _cleanup_seen(now: float) -> None:
    dead = [k for k, t in _seen_msgs.items() if now - t > _DEDUP_TTL]
    for k in dead:
        del _seen_msgs[k]


def _cleanup_leave_seen(now: float) -> None:
    dead = [k for k, t in _seen_leaves.items() if now - t > _DEDUP_TTL]
    for k in dead:
        del _seen_leaves[k]


def _should_notify_leave(chat_id: int, user_id: int, rate_seconds: float) -> bool:
    now = time.time()
    _cleanup_leave_seen(now)
    key = (int(chat_id), int(user_id))
    if key in _seen_leaves:
        return False
    _seen_leaves[key] = now
    prev = _last_alert_ts.get(int(chat_id), 0.0)
    if now - prev < rate_seconds:
        warning(f"限流跳过（退群）：群 {chat_id}（间隔 {rate_seconds}s）")
        return False
    _last_alert_ts[int(chat_id)] = now
    return True


def _should_notify(chat_id: int, msg_id: int, rate_seconds: float) -> bool:
    now = time.time()
    _cleanup_seen(now)
    key = (chat_id, msg_id)
    if key in _seen_msgs:
        return False
    _seen_msgs[key] = now

    prev = _last_alert_ts.get(chat_id, 0.0)
    if now - prev < rate_seconds:
        warning(f"限流跳过：群 {chat_id}（间隔 {rate_seconds}s）")
        return False
    _last_alert_ts[chat_id] = now
    return True


async def _sender_name(event: events.NewMessage.Event) -> str:
    try:
        s = await event.get_sender()
    except Exception:
        return str(event.sender_id or "")
    if s is None:
        return str(event.sender_id or "")
    if getattr(s, "first_name", None) is not None or getattr(s, "last_name", None) is not None:
        parts = [getattr(s, "first_name", "") or "", getattr(s, "last_name", "") or ""]
        name = " ".join(p for p in parts if p).strip()
        if name:
            return name
    if getattr(s, "username", None):
        return "@" + str(s.username)
    if getattr(s, "title", None):
        return str(s.title)
    return str(getattr(s, "id", event.sender_id))


async def _chat_title(event: events.NewMessage.Event) -> Tuple[str, Optional[str]]:
    try:
        chat = await event.get_chat()
    except Exception:
        return (str(event.chat_id or ""), None)
    title = getattr(chat, "title", None) or getattr(chat, "username", None)
    username = getattr(chat, "username", None)
    return ((title and str(title)) or str(event.chat_id), username)


async def _chat_title_from_chat_action(event: events.ChatAction.Event) -> Tuple[str, Optional[str]]:
    try:
        chat = await event.get_chat()
    except Exception:
        return (str(event.chat_id or ""), None)
    title = getattr(chat, "title", None) or getattr(chat, "username", None)
    username = getattr(chat, "username", None)
    return ((title and str(title)) or str(event.chat_id), username)


async def _sender_name_from_user(user: Any, fallback_id: int) -> str:
    if user is None:
        return str(fallback_id)
    if getattr(user, "first_name", None) is not None or getattr(user, "last_name", None) is not None:
        parts = [getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or ""]
        name = " ".join(p for p in parts if p).strip()
        if name:
            return name
    if getattr(user, "username", None):
        return "@" + str(user.username)
    return str(getattr(user, "id", fallback_id))


def _address_entry_for_listen_key(cfg: AppConfig, key_str: str) -> Optional[AddressEntry]:
    ks = str(key_str).strip()
    if not ks:
        return None
    ks_int = chat_ref_to_optional_int(ks)
    for ent in cfg.address_book:
        if not ent.listen_enabled:
            continue
        cr = (ent.chat_ref or "").strip()
        if cr == ks:
            return ent
        if ks_int is not None:
            ent_int = chat_ref_to_optional_int(cr)
            if ent_int is not None:
                a = set(chat_peer_ids_for_match(int(ks_int)))
                b = set(chat_peer_ids_for_match(int(ent_int)))
                if a.intersection(b):
                    return ent
    return None


def _resolution_clients_ordered(
    primary: TelegramClient,
    clients_by_id: Dict[str, TelegramClient],
    owner_account_id: str,
) -> List[Tuple[str, TelegramClient]]:
    """归属账号优先，其次主连接账号（与 coordinator 传入的 primary 一致）。"""
    ordered: List[Tuple[str, TelegramClient]] = []
    seen: set[int] = set()
    oid = (owner_account_id or "").strip()
    if oid:
        owner_client = clients_by_id.get(oid)
        if owner_client is not None:
            ordered.append((oid, owner_client))
            seen.add(id(owner_client))
    if id(primary) not in seen:
        label = "主连接"
        for aid, cl in clients_by_id.items():
            if cl is primary:
                label = aid
                break
        ordered.append((label, primary))
    return ordered


async def _resolve_chat_ref(
    client: TelegramClient,
    ref: ChatRef,
    *,
    quiet: bool = False,
    skip_dialogs_fallback: bool = False,
) -> Optional[int]:
    """解析群/频道标识为与消息事件一致的 peer_id（与 NewMessage 里 get_peer_id(chat) 对齐）。"""
    s0: Optional[str] = None
    if isinstance(ref, int):
        s0 = str(int(ref))
    else:
        s0 = str(ref).strip()
    if not s0:
        return None
    if (s0.startswith("-") and len(s0) > 1 and s0[1:].isdigit()) or s0.isdigit():
        try:
            n = int(s0)
        except ValueError:
            return None
        try:
            ent = await client.get_entity(n)
            return int(utils.get_peer_id(ent))
        except Exception:
            if skip_dialogs_fallback:
                return int(n)
            try:
                await client.get_dialogs(limit=500)
                ent = await client.get_entity(n)
                return int(utils.get_peer_id(ent))
            except Exception as exc:
                if not quiet:
                    warning(f"解析群/频道 ID「{s0}」失败：{exc}")
                return None
    s = s0
    uname = s[1:] if s.startswith("@") else s
    uname = uname.strip()
    if not uname:
        return None
    try:
        ent = await client.get_entity(uname)
        return int(utils.get_peer_id(ent))
    except Exception:
        if skip_dialogs_fallback:
            if not quiet:
                warning(f"解析群/频道「@{uname}」失败（会话列表已预加载）")
            return None
        try:
            await client.get_dialogs(limit=500)
            ent = await client.get_entity(uname)
            return int(utils.get_peer_id(ent))
        except Exception as exc:
            if not quiet:
                warning(f"解析群/频道「@{uname}」失败：{exc}")
            return None


async def _resolve_chat_ref_with_clients(
    clients_ordered: List[Tuple[str, TelegramClient]],
    ref: ChatRef,
    *,
    context: str = "",
    skip_dialogs_fallback: bool = False,
) -> Optional[int]:
    if not clients_ordered:
        return None
    for i, (label, cl) in enumerate(clients_ordered):
        quiet = i < len(clients_ordered) - 1
        cid = await _resolve_chat_ref(
            cl,
            ref,
            quiet=quiet,
            skip_dialogs_fallback=skip_dialogs_fallback,
        )
        if cid is not None:
            if i > 0 and context:
                info(f"{context}已由账号「{label}」解析成功")
            return cid
    return None


async def _resolve_user_target(client: TelegramClient, target: WatchTarget) -> Optional[int]:
    """将配置中的用户目标解析为数字 ID：整数直接返回；@用户名在连接后通过 API 解析（无需对方在群内发过言）。"""
    if isinstance(target, int):
        return int(target)
    s = str(target).strip()
    if not s:
        return None
    if (s.startswith("-") and len(s) > 1 and s[1:].isdigit()) or s.isdigit():
        try:
            return int(s)
        except ValueError:
            return None
    uname = s[1:] if s.startswith("@") else s
    uname = uname.strip()
    if not uname:
        return None
    try:
        ent = await client.get_entity(uname)
        return int(utils.get_peer_id(ent))
    except Exception as exc:
        warning(f"解析用户名「@{uname}」失败：{exc}")
        return None


@dataclass
class _EntryResolveCache:
    peer_id: Optional[int] = None
    user_id: Optional[int] = None
    chat_fp: str = ""
    watch_fp: str = ""


class AddressBookResolver:
    """通讯录群/用户解析缓存：启动全量一次，热更新只处理新增或变更条目。"""

    DIALOGS_LIMIT = 500

    def __init__(self) -> None:
        self._entry_cache: Dict[str, _EntryResolveCache] = {}
        self._legacy_cache: Dict[str, _EntryResolveCache] = {}
        self._dialogs_warmed = False

    def clear(self) -> None:
        self._entry_cache.clear()
        self._legacy_cache.clear()
        self._dialogs_warmed = False

    @staticmethod
    def _chat_fp(ent: AddressEntry) -> str:
        return f"{(ent.chat_ref or '').strip()}|{(ent.owner_account_id or '').strip()}"

    @staticmethod
    def _watch_fp(ent: AddressEntry) -> str:
        return f"{int(bool(ent.listen_enabled))}|{(ent.watch_user or '').strip()}"

    @staticmethod
    def _legacy_fp(key_str: str, target: WatchTarget) -> str:
        return f"{str(key_str).strip()}|{target!r}"

    def _prune_entry_cache(self, cfg: AppConfig) -> None:
        valid = {e.id for e in cfg.address_book}
        for eid in list(self._entry_cache):
            if eid not in valid:
                del self._entry_cache[eid]

    async def warm_dialogs(self, client: TelegramClient) -> None:
        if self._dialogs_warmed:
            return
        try:
            await client.get_dialogs(limit=self.DIALOGS_LIMIT)
            self._dialogs_warmed = True
            info("Telegram：已预加载会话列表（后续添加通讯录不再重复拉取）")
        except Exception as exc:
            warning(f"预加载会话列表失败（仍将逐条尝试解析）：{exc}")

    def _needs_address_resolve(self, ent: AddressEntry, *, full: bool = False) -> bool:
        ref = (ent.chat_ref or "").strip()
        if not ref:
            return False
        # 已是数字 ID 则无需再解析；@ / 链接 须转成数字并写回 config
        return chat_ref_to_optional_int(ref) is None

    async def _resolve_entry_chat(
        self,
        client: TelegramClient,
        ent: AddressEntry,
        clients_by_id: Dict[str, TelegramClient],
        *,
        context: str,
        log_owner_hint: bool = True,
    ) -> Optional[int]:
        ref = (ent.chat_ref or "").strip()
        if not ref:
            return None
        n = chat_ref_to_optional_int(ref)
        if n is not None:
            return int(n)
        try:
            chat_ref = parse_chat_ref_input(ref)
        except ValueError as exc:
            warning(f"通讯录群标识无效「{ent.remark or ent.id}」：{exc}")
            return None
        owner_id = (ent.owner_account_id or "").strip()
        clients = _resolution_clients_ordered(client, clients_by_id, owner_id)
        if log_owner_hint and owner_id and owner_id in clients_by_id and clients and clients[0][0] == owner_id:
            info(f"{context}：优先使用归属账号「{owner_id}」解析群…")
        return await _resolve_chat_ref_with_clients(
            clients,
            chat_ref,
            context=context,
            skip_dialogs_fallback=self._dialogs_warmed,
        )

    async def resolve_address_book_refs(
        self,
        client: TelegramClient,
        cfg: AppConfig,
        clients_by_id: Dict[str, TelegramClient],
        *,
        full: bool = False,
    ) -> bool:
        """将 @用户名 / 链接 转为数字群 ID 并写回 config；full=False 时只解析新增或变更条目。"""
        self._prune_entry_cache(cfg)
        if full and not self._dialogs_warmed:
            await self.warm_dialogs(client)

        changed = False
        for ent in cfg.address_book:
            if not self._needs_address_resolve(ent, full=full):
                continue
            label = ent.remark or ent.id
            cid = await self._resolve_entry_chat(
                client,
                ent,
                clients_by_id,
                context=f"通讯录群「{label}」",
                log_owner_hint=False,
            )
            if cid is None:
                continue
            new_ref = str(int(cid))
            old_ref = (ent.chat_ref or "").strip()
            if new_ref != old_ref:
                ent.chat_ref = new_ref
                changed = True
                info(f"通讯录「{label}」群已解析为 ID {new_ref}")
            cached = self._entry_cache.get(ent.id) or _EntryResolveCache()
            cached.peer_id = cid
            cached.chat_fp = self._chat_fp(ent)
            self._entry_cache[ent.id] = cached

        if changed:
            try:
                save_config(cfg)
            except Exception as exc:
                warning(f"保存解析后的群 ID 失败：{exc}")
        return changed

    async def _resolve_listen_entry(
        self,
        client: TelegramClient,
        ent: AddressEntry,
        clients_by_id: Dict[str, TelegramClient],
        *,
        full: bool,
    ) -> Optional[Tuple[int, int]]:
        try:
            target = parse_watch_user_input((ent.watch_user or "").strip())
        except ValueError:
            return None

        chat_fp = self._chat_fp(ent)
        watch_fp = self._watch_fp(ent)
        cached = self._entry_cache.get(ent.id)

        if (
            not full
            and cached
            and cached.chat_fp == chat_fp
            and cached.watch_fp == watch_fp
            and cached.peer_id is not None
            and cached.user_id is not None
        ):
            return int(cached.peer_id), int(cached.user_id)

        peer_id: Optional[int] = None
        if not full and cached and cached.chat_fp == chat_fp and cached.peer_id is not None:
            peer_id = cached.peer_id
        else:
            key_str = (ent.chat_ref or "").strip()
            peer_id = await self._resolve_entry_chat(
                client,
                ent,
                clients_by_id,
                context=f"监听群「{key_str or ent.remark or ent.id}」",
            )
            if peer_id is None:
                key_str = key_str or ent.id
                warning(f"无法解析群「{key_str}」（已用归属账号与主连接尝试），已跳过该条绑定")
                return None

        user_id: Optional[int] = None
        if not full and cached and cached.watch_fp == watch_fp and cached.user_id is not None and cached.chat_fp == chat_fp:
            user_id = cached.user_id
        else:
            owner_id = (ent.owner_account_id or "").strip()
            clients = _resolution_clients_ordered(client, clients_by_id, owner_id)
            uid_client = clients[0][1] if clients else client
            user_id = await _resolve_user_target(uid_client, target)
            if user_id is None and len(clients) > 1:
                user_id = await _resolve_user_target(client, target)
            if user_id is None:
                hint = repr(target) if isinstance(target, int) else str(target)
                warning(f"无法解析监听对象 {hint}（群 {ent.chat_ref}），已跳过该条绑定")
                return None

        row = self._entry_cache.get(ent.id) or _EntryResolveCache()
        row.peer_id = peer_id
        row.user_id = user_id
        row.chat_fp = chat_fp
        row.watch_fp = watch_fp
        self._entry_cache[ent.id] = row
        return int(peer_id), int(user_id)

    async def _resolve_legacy_binding(
        self,
        client: TelegramClient,
        cfg: AppConfig,
        clients_by_id: Dict[str, TelegramClient],
        key_str: str,
        target: WatchTarget,
        *,
        full: bool,
    ) -> Optional[Tuple[int, int]]:
        legacy_key = f"legacy:{key_str}"
        fp = self._legacy_fp(key_str, target)
        cached = self._legacy_cache.get(legacy_key)
        if not full and cached and cached.chat_fp == fp and cached.peer_id is not None and cached.user_id is not None:
            return int(cached.peer_id), int(cached.user_id)

        try:
            chat_ref = parse_chat_ref_input(str(key_str))
        except ValueError as exc:
            warning(f"群标识无效「{key_str}」：{exc}")
            return None
        ent = _address_entry_for_listen_key(cfg, str(key_str))
        owner_id = (ent.owner_account_id or "").strip() if ent else ""
        clients = _resolution_clients_ordered(client, clients_by_id, owner_id)
        if owner_id and owner_id in clients_by_id and clients and clients[0][0] == owner_id:
            info(f"监听绑定「{key_str}」：优先使用归属账号「{owner_id}」解析群…")
        cid = await _resolve_chat_ref_with_clients(
            clients,
            chat_ref,
            context=f"监听群「{key_str}」",
            skip_dialogs_fallback=self._dialogs_warmed,
        )
        uid_client = clients[0][1] if clients else client
        uid = await _resolve_user_target(uid_client, target)
        if uid is None and len(clients) > 1:
            uid = await _resolve_user_target(client, target)
        if cid is None:
            warning(f"无法解析群「{key_str}」（已用归属账号与主连接尝试），已跳过该条绑定")
            return None
        if uid is None:
            hint = repr(target) if isinstance(target, int) else str(target)
            warning(f"无法解析监听对象 {hint}（群 {key_str}），已跳过该条绑定")
            return None
        self._legacy_cache[legacy_key] = _EntryResolveCache(peer_id=cid, user_id=uid, chat_fp=fp, watch_fp=fp)
        return int(cid), int(uid)

    async def resolve_watch_rules(
        self,
        client: TelegramClient,
        cfg: AppConfig,
        clients_by_id: Optional[Dict[str, TelegramClient]] = None,
        *,
        full: bool = False,
    ) -> Dict[int, int]:
        """解析监听绑定；full=False 时复用缓存，仅处理新增或变更条目。"""
        clients_map = clients_by_id or {}
        self._prune_entry_cache(cfg)
        if full and not self._dialogs_warmed:
            await self.warm_dialogs(client)

        out: Dict[int, int] = {}
        for ent in cfg.address_book:
            if not ent.listen_enabled:
                continue
            if not (ent.watch_user or "").strip():
                continue
            pair = await self._resolve_listen_entry(client, ent, clients_map, full=full)
            if pair:
                out[pair[0]] = pair[1]

        legacy = getattr(cfg, "watch_rules", {}) or {}
        if isinstance(legacy, dict):
            for k, v in legacy.items():
                parsed = _parse_watch_rule_value(v)
                if parsed is None:
                    continue
                pair = await self._resolve_legacy_binding(
                    client,
                    cfg,
                    clients_map,
                    str(k),
                    parsed,
                    full=full,
                )
                if pair:
                    out[pair[0]] = pair[1]
        return out


async def resolve_address_book_refs(client: TelegramClient, cfg: AppConfig) -> bool:
    """在线解析通讯录中的 @用户名 / 链接 为数字群 ID，便于监听与定时任务立即生效。"""
    resolver = AddressBookResolver()
    return await resolver.resolve_address_book_refs(client, cfg, {}, full=True)


async def resolve_address_book_refs_with_owner_retry(
    client: TelegramClient,
    cfg: AppConfig,
    clients_by_id: Dict[str, TelegramClient],
    *,
    resolver: Optional[AddressBookResolver] = None,
    full: bool = True,
) -> bool:
    """在线解析通讯录群标识；主账号失败时，自动用该群归属账号重试一次。"""
    r = resolver or AddressBookResolver()
    return await r.resolve_address_book_refs(client, cfg, clients_by_id, full=full)


async def _resolve_watch_rules(
    client: TelegramClient,
    cfg: AppConfig,
    clients_by_id: Optional[Dict[str, TelegramClient]] = None,
    *,
    resolver: Optional[AddressBookResolver] = None,
    full: bool = True,
) -> Dict[int, int]:
    r = resolver or AddressBookResolver()
    return await r.resolve_watch_rules(client, cfg, clients_by_id, full=full)


class ListenerController:
    def __init__(self) -> None:
        self._running = False
        self._alert_cb: Optional[Callable[[AlertPayload], None]] = None
        self._listen_enabled: bool = False
        self._watch_rules: Dict[int, int] = {}
        self._rate_seconds: float = 3.0
        self._registered_client_ids: set[int] = set()
        from .watch_read_tracker import TgWatchReadTracker

        self.read_tracker = TgWatchReadTracker()

    def is_running(self) -> bool:
        return self._running

    def start(self, cfg: AppConfig, alert_cb: Callable[[AlertPayload], None]) -> None:
        """登记监听意图；实际连接与事件由 telethon_coordinator 在同一 asyncio 循环内完成。"""
        self.stop()
        self._alert_cb = alert_cb
        self._listen_enabled = False
        self._running = False
        if not cfg.listening_enabled:
            info("监听未开启（配置中 listening_enabled=false）")
            return
        accounts = [a for a in cfg.accounts if a.enabled]
        if not accounts:
            warning("没有已启用账号，未启动监听事件（仍可连接账号用于定时发送）")
            return
        ok_bind = False
        for _a, _b in iter_listen_bindings(cfg):
            ok_bind = True
            break
        if not ok_bind:
            warning("未配置监听绑定（通讯录中无「参与监听」条目或旧版 watch_rules 为空），不注册监听事件")
            return
        if not int(cfg.api_id) or not str(cfg.api_hash).strip():
            warning("未配置共用的 api_id / api_hash，无法注册监听")
            return
        self._listen_enabled = True
        info("已登记监听规则（与定时任务共享会话，由统一线程连接 Telegram）")

    def listen_enabled(self) -> bool:
        return self._listen_enabled

    def set_watch_rules(self, rules: Dict[int, int]) -> None:
        self._watch_rules = dict(rules)

    def register_client_handlers(self, client: TelegramClient, rules: Dict[int, int], rate_seconds: float) -> None:
        self.set_watch_rules(rules)
        self._rate_seconds = float(rate_seconds)
        client_key = id(client)
        if client_key in self._registered_client_ids:
            return
        self._registered_client_ids.add(client_key)

        @client.on(events.NewMessage(incoming=True))
        async def handler(event: events.NewMessage.Event) -> None:
            if not self._running:
                return
            rules_live = self._watch_rules
            rate_seconds = self._rate_seconds
            # 与通讯录解析、定时暂停逻辑统一：优先使用 get_peer_id(实体)，避免 event.chat_id 与 -100… 配置不一致导致「能监听但不暂停」。
            try:
                chat = await event.get_chat()
                peer_id = int(utils.get_peer_id(chat))
            except Exception:
                peer_id = int(event.chat_id)
            raw_cid = int(event.chat_id)
            target_user = rules_live.get(peer_id)
            if target_user is None and raw_cid != peer_id:
                target_user = rules_live.get(raw_cid)
            if target_user is None:
                return
            uid = int(event.sender_id) if event.sender_id is not None else None
            if uid is None or int(uid) != int(target_user):
                return
            msg_id = int(event.id)
            self.read_tracker.record(peer_id=peer_id, msg_id=msg_id)
            if not _should_notify(peer_id, msg_id, rate_seconds):
                return
            text = event.message.message or ""
            title, uname = await _chat_title(event)
            sender = await _sender_name(event)
            payload: AlertPayload = {
                "chat_title": title,
                "sender_name": sender,
                "message_text": text,
                "chat_username": uname,
                "chat_id": peer_id,
                "chat_id_raw": raw_cid,
            }
            chat_disp = format_listener_chat_label(
                load_config(),
                peer_id=peer_id,
                chat_title=title,
                chat_id_raw=raw_cid,
                chat_username=uname,
            )
            info(f"触发监听提醒：群 {chat_disp} | 用户 {sender}")
            if self._alert_cb:
                try:
                    self._alert_cb(payload)
                except Exception as exc:
                    error(f"提醒回调异常：{exc}")

        @client.on(events.ChatAction)
        async def leave_handler(event: events.ChatAction.Event) -> None:
            if not self._running:
                return
            if not (event.user_left or event.user_kicked):
                return
            rules_live = self._watch_rules
            rate_seconds = self._rate_seconds
            try:
                chat = await event.get_chat()
                peer_id = int(utils.get_peer_id(chat))
            except Exception:
                peer_id = int(event.chat_id)
            raw_cid = int(event.chat_id)
            target_user = rules_live.get(peer_id)
            if target_user is None and raw_cid != peer_id:
                target_user = rules_live.get(raw_cid)
            if target_user is None:
                return
            watch_uid = int(target_user)
            left_ids = [int(u) for u in event.user_ids if u is not None]
            if watch_uid not in left_ids:
                return
            if not _should_notify_leave(peer_id, watch_uid, rate_seconds):
                return
            title, uname = await _chat_title_from_chat_action(event)
            try:
                user = await event.get_user()
                sender = await _sender_name_from_user(user, watch_uid)
            except Exception:
                sender = str(watch_uid)
            payload: AlertPayload = {
                "chat_title": title,
                "sender_name": sender,
                "message_text": f"【退群】监听目标用户（ID {watch_uid}）已离开本群",
                "chat_username": uname,
                "chat_id": peer_id,
                "chat_id_raw": raw_cid,
                "alert_kind": "leave",
            }
            chat_disp = format_listener_chat_label(
                load_config(),
                peer_id=peer_id,
                chat_title=title,
                chat_id_raw=raw_cid,
                chat_username=uname,
            )
            info(f"触发退群提醒：群 {chat_disp} | 用户 {sender}（ID {watch_uid}）")
            if self._alert_cb:
                try:
                    self._alert_cb(payload)
                except Exception as exc:
                    error(f"退群提醒回调异常：{exc}")

    async def idle_forever(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        self._running = False
        info("监听已请求停止")
