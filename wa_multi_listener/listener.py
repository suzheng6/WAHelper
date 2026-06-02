"""WhatsApp 消息监听：群/会话 + 指定用户 + 限流。"""

from __future__ import annotations



import asyncio
import ctypes

import time

from dataclasses import dataclass, field

from typing import Any, Callable, Dict, List, Optional, Set, Tuple



from neonize.aioze.client import ClientFactory, NewAClient
from neonize.aioze import events as neonize_events
from neonize.aioze.events import EVENT_TO_INT, INT_TO_EVENT, GroupInfoEv, MessageEv

from neonize.proto.Neonize_pb2 import GroupInfoEvent as GroupInfoEventMsg, JID
from neonize.utils import extract_text

from neonize.utils.jid import Jid2String, JIDToNonAD



from config import AppConfig, iter_listen_bindings, parse_chat_ref_input, parse_watch_user_input
from group_membership import (
    extra_group_chat_keys,
    resolve_watch_user_keys_in_group,
    verify_account_in_group,
)

from logger_util import debug, error, info, warning
from watch_read_tracker import WaWatchReadTracker

from wa_jid import (
    chat_keys_from_ref,
    chat_matches_keys,
    coerce_chat_key_set,
    invite_code_from_link,
    jid_to_key,
    jid_from_chat_key,
    jid_nonempty,
    keys_for_match,
    normalize_phone,
    parse_chat_ref_to_jid,
    phones_equivalent,
    register_resolved_chat,
    resolve_sender_phones_debug,
    sender_matches_watch_keys,
    user_matches_watch_async,
)



AlertPayload = Dict[str, Any]



_last_alert_ts: Dict[str, float] = {}

_seen_msgs: Dict[Tuple[str, str], float] = {}

_DEDUP_TTL = 30.0
_MISS_LOG_INTERVAL = 180.0
_last_miss_log: Dict[str, float] = {}





@dataclass

class ListenRule:

    watch_phone: str

    chat_refs: List[str] = field(default_factory=list)

    chat_keys: Set[str] = field(default_factory=set)

    watch_sender_keys: Set[str] = field(default_factory=set)

    label: str = ""





def _cleanup_seen(now: float) -> None:

    dead = [k for k, t in _seen_msgs.items() if now - t > _DEDUP_TTL]

    for k in dead:

        del _seen_msgs[k]





def _should_notify(chat_key: str, msg_id: Any, rate_seconds: float) -> bool:

    now = time.time()

    _cleanup_seen(now)

    key = (chat_key, msg_id)

    if key in _seen_msgs:

        return False

    _seen_msgs[key] = now

    prev = _last_alert_ts.get(chat_key, 0.0)

    if now - prev < rate_seconds:

        warning(f"限流跳过：会话 {chat_key}")

        return False

    _last_alert_ts[chat_key] = now

    return True





def build_listen_rules(cfg: AppConfig) -> List[ListenRule]:

    rules: List[ListenRule] = []

    for chat_raw, target in iter_listen_bindings(cfg):

        try:

            parse_chat_ref_input(str(chat_raw))

            phone = parse_watch_user_input(str(target))

        except ValueError as exc:

            warning(f"监听规则无效「{chat_raw}」：{exc}")

            continue

        cref = str(chat_raw).strip()

        keys = chat_keys_from_ref(cref)

        rules.append(

            ListenRule(

                watch_phone=phone,

                chat_refs=[cref],

                chat_keys=keys,

                label=cref[:60],

            )

        )

    return rules





async def resolve_listen_rules(client: NewAClient, rules: List[ListenRule]) -> List[ListenRule]:

    """将群邀请链接解析为 g.us JID，供消息匹配。"""

    out: List[ListenRule] = []

    for rule in rules:

        keys: Set[str] = coerce_chat_key_set(rule.chat_keys)

        for cref in rule.chat_refs:
            cr = cref.strip()
            code = invite_code_from_link(cr)
            if code:
                try:
                    gi = await client.get_group_info_from_link(code)
                    if gi and gi.JID and not gi.JID.IsEmpty:
                        keys.update(extra_group_chat_keys(gi))
                        register_resolved_chat(cref, gi.JID)
                        gname = gi.GroupName.Name if gi.GroupName else ""
                        info(
                            f"群邀请已解析：{cref[:50]}… → {jid_to_key(gi.JID)}"
                            f"（{gname}）"
                        )
                        await verify_account_in_group(
                            client, gi.JID, label=gname or jid_to_key(gi.JID)
                        )
                    else:
                        warning(f"无法从邀请链接取得群 JID：{cref}")
                except Exception as exc:
                    warning(f"解析群邀请失败「{cref}」：{exc}")
            elif "@g.us" in cr.lower():
                try:
                    gj = parse_chat_ref_to_jid(cr)
                    await verify_account_in_group(client, gj, label=cr[:48])
                except ValueError:
                    pass

        if not keys:

            warning(f"监听规则无有效群标识：{rule.label or rule.chat_refs}")

            continue

        group_jid: Optional[JID] = None
        for ck in keys:
            if "@g.us" in ck:
                group_jid = jid_from_chat_key(ck)
                if group_jid is not None:
                    break

        watch_sender_keys: Set[str] = set()
        if group_jid is not None:
            watch_sender_keys = await resolve_watch_user_keys_in_group(
                client,
                group_jid,
                rule.watch_phone,
                label=rule.label or jid_to_key(group_jid),
            )

        out.append(
            ListenRule(
                watch_phone=rule.watch_phone,
                chat_refs=list(rule.chat_refs),
                chat_keys=keys,
                watch_sender_keys=watch_sender_keys,
                label=rule.label,
            )
        )

    return out


async def warm_invite_cache(client: NewAClient, cfg: AppConfig) -> None:
    """预解析通讯录/任务中的群邀请链接，供定时任务暂停匹配。"""
    refs: Set[str] = set()
    for e in cfg.address_book:
        cr = (e.chat_ref or "").strip()
        if cr and invite_code_from_link(cr):
            refs.add(cr)
    for chat_raw, _ in iter_listen_bindings(cfg):
        cr = str(chat_raw).strip()
        if cr and invite_code_from_link(cr):
            refs.add(cr)
    for cref in refs:
        code = invite_code_from_link(cref)
        if not code:
            continue
        try:
            gi = await client.get_group_info_from_link(code)
            if gi and gi.JID and not gi.JID.IsEmpty:
                register_resolved_chat(cref, gi.JID)
        except Exception:
            pass


def _chat_title_from_event(event: MessageEv) -> str:

    try:

        p = event.Info.Pushname

        if p:

            return str(p)

    except Exception:

        pass

    return jid_to_key(event.Info.MessageSource.Chat)





class ListenerController:

    def __init__(self) -> None:

        self._running = False

        self._alert_cb: Optional[Callable[[AlertPayload], None]] = None

        self._listen_enabled = False

        self._rules: List[ListenRule] = []

        self._rate_seconds = 10.0

        self._attached_client_ids: Set[int] = set()
        self._attached_factory_ids: Set[int] = set()
        self._attached_groupinfo_factory_ids: Set[int] = set()
        self._wrapped_execute_ids: Set[int] = set()
        self._probe_msg_left = 0
        self._async_loop: Optional[asyncio.AbstractEventLoop] = None
        self.read_tracker = WaWatchReadTracker()

    def set_async_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._async_loop = loop

    def listen_enabled(self) -> bool:
        return self._listen_enabled

    def is_running(self) -> bool:
        return self._running

    def start(self, cfg: AppConfig, alert_cb: Callable[[AlertPayload], None]) -> None:
        """启动时加载监听配置（勿改 _running，由 WaCoordinator 控制）。"""
        self.load_config(cfg, alert_cb)

    def load_config(
        self,
        cfg: AppConfig,
        alert_cb: Optional[Callable[[AlertPayload], None]] = None,
    ) -> bool:
        """加载/刷新监听配置；成功返回 True。不会把已启用的监听误关断。"""
        if alert_cb is not None:
            self._alert_cb = alert_cb

        if not cfg.listening_enabled:
            self._listen_enabled = False
            info("监听未开启")
            return False

        accounts = [a for a in cfg.accounts if a.enabled]
        if not accounts:
            warning("没有已启用账号")
            self._listen_enabled = False
            return False

        raw = build_listen_rules(cfg)
        if not raw:
            warning("未配置监听绑定")
            self._listen_enabled = False
            return False

        self._rate_seconds = float(cfg.rate_limit_seconds)
        self._rules = raw
        self._listen_enabled = True
        info(f"已登记 {len(raw)} 条监听规则（连接前注册消息事件，连接后解析群）")
        return True

    def ensure_message_subscription(self, factory: ClientFactory) -> None:
        """在 factory.new_client() 之前注册 MessageEv（与 neonize 官方用法一致）。"""
        fid = id(factory)
        if fid in self._attached_factory_ids:
            return
        self._attached_factory_ids.add(fid)
        msg_code = EVENT_TO_INT[MessageEv]

        @factory.event(MessageEv)
        async def on_message(c: NewAClient, event: MessageEv) -> None:
            await self._handle_message(c, event)

        if msg_code not in factory.event.list_func:
            warning("MessageEv 未写入 factory 事件表")

    def ensure_group_info_subscription(self, factory: ClientFactory) -> None:
        """在 factory.new_client() 之前注册 GroupInfoEv（成员退群等）。"""
        fid = id(factory)
        if fid in self._attached_groupinfo_factory_ids:
            return
        self._attached_groupinfo_factory_ids.add(fid)

        @factory.event(GroupInfoEv)
        async def on_group_info(c: NewAClient, event: GroupInfoEventMsg) -> None:
            await self._handle_group_info(c, event)

    def wrap_client_execute(self, client: NewAClient) -> None:
        """安全派发 Go 事件到 asyncio 循环，并记录 MessageEv / 回调异常。"""
        cid = id(client)
        if cid in self._wrapped_execute_ids:
            return
        self._wrapped_execute_ids.add(cid)
        msg_code = EVENT_TO_INT[MessageEv]

        def execute_wrapper(uuid: int, binary: int, size: int, code: int) -> None:
            if code not in INT_TO_EVENT:
                return
            if code not in client.event.list_func:
                if code == msg_code:
                    error(
                        f"MessageEv 未注册到事件表，已注册={sorted(client.event.list_func.keys())}"
                    )
                return
            message = INT_TO_EVENT[code].FromString(ctypes.string_at(binary, size))
            if code == 0:
                client.me = message
                return
            if code == 3:
                client.connected = True
            loop = self._async_loop or neonize_events.event_global_loop
            if loop is None:
                error("event loop 未就绪，无法处理 WhatsApp 消息事件")
                return

            def _done(fut: asyncio.Future) -> None:
                try:
                    exc = fut.exception()
                    if exc is not None:
                        error(f"WhatsApp 事件 {code} 回调异常：{exc}")
                except Exception:
                    pass

            fut = asyncio.run_coroutine_threadsafe(
                client.event.list_func[code](client, message),
                loop,
            )
            fut.add_done_callback(_done)

        client.event.execute = execute_wrapper  # type: ignore[method-assign]

    def attach_client_before_connect(self, client: NewAClient) -> None:
        """兼容旧调用；new_client 已通过 factory 注册。"""
        self.wrap_client_execute(client)

    def apply_resolved_rules(
        self,
        rules: List[ListenRule],
        rate_seconds: float,
        client: Optional[NewAClient] = None,
    ) -> None:
        if not rules:
            warning("无已解析的监听规则")
            self._rules = []
            return
        self._rules = rules
        self._rate_seconds = rate_seconds
        self._probe_msg_left = 0
        for rule in rules:
            keys_txt = ", ".join(sorted(rule.chat_keys)[:3])
            lock = ""
            if rule.watch_sender_keys:
                lock = f" · 已锁定发送者 {len(rule.watch_sender_keys)} 键"
            info(f"监听就绪：目标用户 {rule.watch_phone} · 群 {keys_txt}{lock}")
        if client is not None:
            self._warn_if_watching_self(rules, client)

    def _warn_if_watching_self(self, rules: List[ListenRule], client: NewAClient) -> None:
        me = client.me
        if me is None or not jid_nonempty(me):
            return
        my_phone = normalize_phone(me.User)
        if not my_phone:
            return
        acc = client.uuid.decode() if isinstance(client.uuid, bytes) else str(client.uuid)
        for rule in rules:
            if phones_equivalent(rule.watch_phone, my_phone):
                warning(
                    f"账号「{acc}」已登录号码为 {my_phone}，与监听目标 {rule.watch_phone} 相同。"
                    "该号在群里自己发的消息会被 WhatsApp 标记为「本人」而不会触发提醒。"
                    "请改用另一个 WhatsApp 账号登录本程序，专门用来监听此号码。"
                )

    async def _handle_group_info(self, c: NewAClient, event: GroupInfoEventMsg) -> None:
        try:
            await self._handle_group_info_inner(c, event)
        except Exception as exc:
            error(f"监听退群处理异常：{exc}")

    async def _handle_group_info_inner(self, c: NewAClient, event: GroupInfoEventMsg) -> None:
        if not self._running or not self._listen_enabled or not self._rules:
            return
        chat = event.JID
        if not jid_nonempty(chat):
            return
        leaves = list(event.Leave) if event.Leave else []
        if not leaves:
            return
        chat_key = jid_to_key(chat)
        if not any(chat_matches_keys(rule.chat_keys, chat) for rule in self._rules):
            return
        gname = ""
        if event.Name and (event.Name.Name or "").strip():
            gname = (event.Name.Name or "").strip()
        title = gname or chat_key
        rate_seconds = self._rate_seconds
        for leave_jid in leaves:
            if not jid_nonempty(leave_jid):
                continue
            matched_rule: Optional[ListenRule] = None
            for rule in self._rules:
                if not chat_matches_keys(rule.chat_keys, chat):
                    continue
                if sender_matches_watch_keys(leave_jid, None, None, rule.watch_sender_keys) or (
                    await user_matches_watch_async(c, leave_jid, None, rule.watch_phone)
                ):
                    matched_rule = rule
                    break
            if matched_rule is None:
                continue
            dedup_id = f"leave:{jid_to_key(leave_jid)}"
            if not _should_notify(chat_key, dedup_id, rate_seconds):
                continue
            who = Jid2String(JIDToNonAD(leave_jid))
            payload: AlertPayload = {
                "chat_title": title,
                "sender_name": who,
                "message_text": f"【退群】监听目标 {matched_rule.watch_phone} 已离开本群",
                "chat_key": chat_key,
                "chat_jid": Jid2String(JIDToNonAD(chat)),
                "alert_kind": "leave",
            }
            info(f"触发退群提醒：{title} | {who} | 监听号 {matched_rule.watch_phone}")
            if self._alert_cb:
                try:
                    self._alert_cb(payload)
                except Exception as exc:
                    error(f"退群提醒回调异常：{exc}")

    async def _handle_message(self, c: NewAClient, event: MessageEv) -> None:
        try:
            await self._handle_message_inner(c, event)
        except Exception as exc:
            error(f"监听消息处理异常：{exc}")

    async def _handle_message_inner(self, c: NewAClient, event: MessageEv) -> None:
        if not self._running or not self._listen_enabled or not self._rules:
            return

        src = event.Info.MessageSource
        chat = src.Chat
        chat_key = jid_to_key(chat) if jid_nonempty(chat) else "?"
        rules = self._rules
        rate_seconds = self._rate_seconds
        in_watched_chat = bool(
            jid_nonempty(chat) and any(chat_matches_keys(rule.chat_keys, chat) for rule in rules)
        )

        if self._probe_msg_left > 0:
            self._probe_msg_left -= 1
            acc = c.uuid.decode() if isinstance(c.uuid, bytes) else str(c.uuid)
            debug(
                f"监听调试：账号={acc} 群={src.IsGroup} chat={chat_key} "
                f"命中监听群={in_watched_chat} 本人={src.IsFromMe} mode={src.AddressingMode}"
            )

        if src.IsFromMe:
            return

        if not jid_nonempty(chat):
            return

        sender = src.Sender if jid_nonempty(src.Sender) else None
        sender_alt = src.SenderAlt if jid_nonempty(src.SenderAlt) else None
        recipient_alt = src.RecipientAlt if jid_nonempty(src.RecipientAlt) else None
        if not sender and not sender_alt and not recipient_alt:
            return

        msg_id = str(event.Info.ID or "")

        if not in_watched_chat:
            return

        matched_rule: Optional[ListenRule] = None
        for rule in rules:
            if not chat_matches_keys(rule.chat_keys, chat):
                continue
            if sender_matches_watch_keys(
                sender, sender_alt, recipient_alt, rule.watch_sender_keys
            ) or await user_matches_watch_async(
                c, sender, sender_alt, rule.watch_phone, extra_jid=recipient_alt
            ):
                matched_rule = rule
                break

        if matched_rule is None:
            for rule in rules:
                if not chat_matches_keys(rule.chat_keys, chat):
                    continue
                miss_key = f"{chat_key}|{rule.watch_phone}"
                now = time.time()
                if now - _last_miss_log.get(miss_key, 0.0) >= _MISS_LOG_INTERVAL:
                    _last_miss_log[miss_key] = now
                    sk = jid_to_key(sender) if jid_nonempty(sender) else "—"
                    sak = jid_to_key(sender_alt) if jid_nonempty(sender_alt) else "—"
                    phones_dbg = await resolve_sender_phones_debug(
                        c, sender, sender_alt, recipient_alt
                    )
                    debug(
                        f"监听：群内未命中（期望 {rule.watch_phone}，"
                        f"发送者 {sk} / 备用 {sak}，解析号 {phones_dbg}）"
                    )
                break
            return

        who = sender or sender_alt
        if who and jid_nonempty(who):
            self.read_tracker.record(
                chat_key=chat_key,
                msg_id=msg_id,
                chat_jid=JIDToNonAD(chat),
                sender_jid=JIDToNonAD(who),
            )

        if not _should_notify(chat_key, msg_id, rate_seconds):
            return

        text = extract_text(event.Message) or ""
        title = _chat_title_from_event(event)
        sender_name = Jid2String(JIDToNonAD(who)) if who else ""

        payload: AlertPayload = {
            "chat_title": title,
            "sender_name": sender_name,
            "message_text": text,
            "chat_key": chat_key,
            "chat_jid": Jid2String(JIDToNonAD(chat)),
        }
        info(f"触发提醒：{title} | {sender_name} | 监听号 {matched_rule.watch_phone}")

        if self._alert_cb:
            try:
                self._alert_cb(payload)
            except Exception as exc:
                error(f"提醒回调异常：{exc}")



    async def idle_forever(self) -> None:

        try:

            while self._running:

                await asyncio.sleep(0.25)

        except asyncio.CancelledError:

            pass



    def stop(self) -> None:
        self._running = False
        self._attached_client_ids.clear()
        self._attached_factory_ids.clear()
        self._wrapped_execute_ids.clear()
        info("监听已请求停止")


