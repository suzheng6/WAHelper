"""单 asyncio 线程：多账号连接、监听与定时任务共享会话。"""

from __future__ import annotations



import asyncio

import threading

import time

from typing import Any, Callable, Dict, List, Optional, Set, Tuple



from neonize.aioze.client import NewAClient



from config import Account, AppConfig, save_config
from invite_resolve import resolve_invite_ref
from listener import ListenerController, resolve_listen_rules, warm_invite_cache
from wa_jid import invite_code_from_link, jid_nonempty
from watch_membership_audit import WatchAuditRow, audit_address_book_watch_users

from logger_util import error, info, warning

from schedule2_runner import Schedule2Runner, load_schedule2_jobs

from session_check import has_saved_session

from wa_client import establish_connection, new_client, stop_all_clients



_STAGGER_SEC = 1.2

_MAX_CONNECT_ATTEMPTS = 1





class WaCoordinator:

    def __init__(self, listener: ListenerController, schedule2: Schedule2Runner) -> None:

        self._listener = listener

        self._schedule2 = schedule2
        schedule2.bind_read_tracker(listener.read_tracker)

        self._thread: Optional[threading.Thread] = None

        self._svc_running = False

        self._connected_ids: Set[str] = set()

        self._lock = threading.Lock()

        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._clients: Dict[str, NewAClient] = {}

        self._listen_refresh_lock: Optional[asyncio.Lock] = None

        self._connecting = False

        self._pending_listen_cfg: Optional[AppConfig] = None

        self._session_id: int = 0



    def is_running(self) -> bool:
        t = self._thread
        return bool(self._svc_running and t is not None and t.is_alive())

    def connected_account_ids(self) -> Set[str]:

        with self._lock:

            return set(self._connected_ids)



    def is_account_online(self, account_id: str) -> bool:

        with self._lock:

            return account_id in self._connected_ids

    def has_connected_clients(self) -> bool:

        with self._lock:

            return bool(self._clients)

    def _primary_client(self) -> Optional[NewAClient]:

        with self._lock:

            if not self._clients:

                return None

            return next(iter(self._clients.values()))

    def apply_config_hot(self, cfg: AppConfig) -> None:

        """在线时更新监听/邀请解析，无需整服务重载。"""

        loop = self._loop

        if loop is None or not loop.is_running():

            return

        if self._connecting:
            self._pending_listen_cfg = cfg
            info("连接进行中，已记录通讯录变更，连接完成后将自动刷新监听")
            return

        asyncio.run_coroutine_threadsafe(self._apply_config_async(cfg), loop)

    async def _apply_config_async(self, cfg: AppConfig) -> None:

        if not self._clients:

            return

        await self._refresh_listen(cfg)

    async def _resolve_address_book_refs(self, cfg: AppConfig) -> None:

        client = self._primary_client()

        if client is None:

            return

        changed = False

        for ent in cfg.address_book:

            cr = (ent.chat_ref or "").strip()

            if not invite_code_from_link(cr):

                continue

            resolved = await resolve_invite_ref(client, cr, log_label="通讯录")

            if resolved and resolved != cr:

                ent.chat_ref = resolved

                changed = True

        if changed:

            try:

                save_config(cfg)

            except Exception as exc:

                warning(f"保存解析后的群 JID 失败：{exc}")

    async def _deferred_refresh_listen(
        self, cfg: AppConfig, account_id: str, client: NewAClient
    ) -> None:
        await asyncio.sleep(10.0)
        if not self._svc_running:
            return
        with self._lock:
            if account_id not in self._clients:
                return
        if await self._wait_client_me(client, 45.0):
            await self._refresh_listen(cfg)
            info(f"账号「{account_id}」监听规则延迟刷新完成")

    async def _wait_client_me(self, client: NewAClient, timeout: float = 45.0) -> bool:
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            me = client.me
            if me is not None and jid_nonempty(me):
                return True
            await asyncio.sleep(0.5)
        return False

    async def _refresh_listen(self, cfg: AppConfig) -> None:

        if self._listen_refresh_lock is None:
            self._listen_refresh_lock = asyncio.Lock()

        async with self._listen_refresh_lock:
            await self._resolve_address_book_refs(cfg)

            cb = self._listener._alert_cb  # noqa: SLF001
            if cb is None:
                return

            if not self._listener.load_config(cfg, cb):
                return

            clients = list(self._clients.values())
            if not clients:
                return

            primary = clients[0]
            if not await self._wait_client_me(primary):
                warning("账号信息尚未同步，将稍后重试解析监听群（不影响收消息）")
                return

            for c in clients:
                await warm_invite_cache(c, cfg)

            raw_rules = list(self._listener._rules)  # noqa: SLF001
            if not raw_rules:
                warning("未配置有效监听绑定")
                return

            rate = float(cfg.rate_limit_seconds)
            try:
                rules = await resolve_listen_rules(primary, raw_rules)
            except Exception as exc:
                import traceback

                error(f"解析监听规则失败：{exc}\n{traceback.format_exc()}")
                return
            self._listener.apply_resolved_rules(rules, rate, primary)
            info("监听规则已刷新并生效")



    def start(self, cfg: AppConfig) -> None:

        self.stop(join_timeout=12.0)

        self._svc_running = True



        def run() -> None:

            asyncio.run(self._async_run(cfg))



        from shutdown import track_background_thread

        t = track_background_thread(
            threading.Thread(target=run, name="wa-coordinator", daemon=True)
        )

        self._thread = t

        t.start()

        info("WhatsApp 统一会话线程已启动")



    def prepare_for_login(self, account_id: str, timeout: float = 25.0) -> None:
        """登录前仅断开待登录账号，其它账号保持在线。"""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._disconnect_account_only(account_id), loop
            )
            fut.result(timeout=max(1.0, timeout))
        except Exception as exc:
            warning(f"登录前断开账号「{account_id}」时：{exc}")

    def connect_account_after_login(self, cfg: AppConfig, account_id: str) -> None:
        """扫码登录成功后只连接该账号（后台执行，不阻塞 UI）。"""
        if not self.is_running():
            self.start(cfg)
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            self.start(cfg)
            return
        acc = next((a for a in cfg.accounts if a.id == account_id), None)
        if acc is None:
            warning(f"未找到账号「{account_id}」")
            return
        asyncio.run_coroutine_threadsafe(
            self._connect_account_incremental(cfg, acc, self._session_id), loop
        )
        info(f"账号「{account_id}」正在后台连接（其它账号保持原连接）")

    async def _disconnect_account_only(self, account_id: str) -> None:
        with self._lock:
            client = self._clients.pop(account_id, None)
            self._connected_ids.discard(account_id)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
            info(f"已断开账号「{account_id}」以便重新登录")

    async def _connect_account_incremental(
        self, cfg: AppConfig, acc: Account, session: int
    ) -> None:
        if session != self._session_id:
            return
        with self._lock:
            already = acc.id in self._connected_ids and acc.id in self._clients
        if already:
            info(f"账号「{acc.id}」已在线")
            await self._refresh_listen(cfg)
            return

        old = None
        with self._lock:
            old = self._clients.pop(acc.id, None)
            self._connected_ids.discard(acc.id)
        if old is not None:
            try:
                await old.disconnect()
            except Exception:
                pass

        r = await self._connect_one(acc, session)
        if r is None or session != self._session_id:
            warning(f"账号「{acc.id}」未能加入在线会话")
            return

        _, client = r
        with self._lock:
            self._connected_ids.add(acc.id)
            self._clients[acc.id] = client
            clients_by_id = dict(self._clients)

        self._schedule2.bind_clients(clients_by_id)
        if not await self._wait_client_me(client, 60.0):
            warning(f"账号「{acc.id}」资料同步较慢，10 秒后将重试解析监听群")
            asyncio.create_task(self._deferred_refresh_listen(cfg, acc.id, client))
        else:
            await self._refresh_listen(cfg)
        info(f"账号「{acc.id}」已上线（其它账号保持原连接）")

    def stop(self, join_timeout: float = 3.0) -> None:

        self._svc_running = False
        self._session_id += 1

        self._listener._running = False  # noqa: SLF001
        self._listener._attached_client_ids.clear()  # noqa: SLF001
        self._listener._attached_factory_ids.clear()  # noqa: SLF001
        self._listener._wrapped_execute_ids.clear()  # noqa: SLF001
        self._schedule2._running = False  # noqa: SLF001

        t = self._thread

        self._thread = None

        with self._lock:

            self._connected_ids.clear()

            self._clients.clear()

        loop = self._loop
        if t and t.is_alive() and loop is not None and loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(stop_all_clients(), loop)
                fut.result(timeout=min(5.0, max(1.0, join_timeout)))
            except Exception:
                pass

        if t and t.is_alive() and t is not threading.current_thread():

            deadline = time.monotonic() + max(0.05, join_timeout)

            while t.is_alive() and time.monotonic() < deadline:

                t.join(timeout=min(1.0, max(0.05, deadline - time.monotonic())))

        if t and t.is_alive():
            try:
                asyncio.run(stop_all_clients())
            except Exception:
                pass



    async def _connect_one(
        self, acc: Account, session: int
    ) -> Optional[Tuple[Account, NewAClient]]:

        if not self._svc_running or session != self._session_id:
            return None

        if not has_saved_session(acc):

            warning(f"账号「{acc.id}」尚无登录会话，请先在账号管理扫码登录")

            return None



        client: Optional[NewAClient] = None

        last_err = ""

        for attempt in range(1, _MAX_CONNECT_ATTEMPTS + 1):
            if not self._svc_running or session != self._session_id:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                return None

            if client is not None:

                try:

                    await client.disconnect()

                except Exception:

                    pass

                await asyncio.sleep(1.0)

            client = new_client(acc, self._listener)

            try:

                if attempt > 1:

                    info(f"WhatsApp：正在重试连接「{acc.id}」…")

                else:

                    info(f"WhatsApp：正在连接「{acc.id}」…")

                ok, last_err = await establish_connection(client, acc)

                if ok:

                    info(f"已连接账号：{acc.id}")

                    return acc, client

                warning(f"账号「{acc.id}」{last_err}")

            except Exception as exc:

                last_err = str(exc) or "连接失败"

                warning(f"连接失败 {acc.id}：{last_err}")



        if client is not None:

            try:

                await client.disconnect()

            except Exception:

                pass

        return None



    async def _connect_staggered(
        self, acc: Account, index: int, session: int
    ) -> Optional[Tuple[Account, NewAClient]]:

        if index > 0:

            await asyncio.sleep(index * _STAGGER_SEC)

        return await self._connect_one(acc, session)



    async def _async_run(self, cfg: AppConfig) -> None:

        session = self._session_id
        self._loop = asyncio.get_running_loop()
        self._listen_refresh_lock = asyncio.Lock()
        self._listener.set_async_loop(self._loop)

        self._listener._running = True  # noqa: SLF001

        self._schedule2._running = True  # noqa: SLF001

        self._connecting = True

        accounts = [a for a in cfg.accounts if a.enabled]

        if not accounts:

            error("没有启用的账号。")
            self._connecting = False
            return



        info("正在释放旧连接…")
        await stop_all_clients()
        await asyncio.sleep(0.8)



        connected: List[Tuple[Account, Any]] = []
        clients_by_id: Dict[str, NewAClient] = {}

        for i, acc in enumerate(accounts):
            if not self._svc_running or session != self._session_id:
                break
            try:
                r = await self._connect_staggered(acc, i, session)
            except Exception as exc:
                warning(f"连接任务异常 {acc.id}：{exc}")
                continue
            if r is None:
                continue
            connected.append(r)
            clients_by_id[r[0].id] = r[1]
            with self._lock:
                self._connected_ids.add(r[0].id)
                self._clients[r[0].id] = r[1]

        self._connecting = False

        if not clients_by_id:

            error("没有任何 WhatsApp 账号在线；请检查代理/网络，或在账号管理中重新扫码。")
            if self._pending_listen_cfg is not None:
                info("连接未成功，已保留通讯录变更，下次连接成功后将自动刷新监听")
            if session == self._session_id:
                self._listener._running = False  # noqa: SLF001
                self._schedule2._running = False  # noqa: SLF001
            return

        try:

            self._schedule2.bind_clients(clients_by_id)

            try:

                jobs = load_schedule2_jobs()

                running = sum(1 for x in jobs if x.enabled and x.state == "running")

                info(f"定时载入：共 {len(jobs)} 个任务，运行中 {running} 个")

            except Exception:

                pass



            refresh_cfg = self._pending_listen_cfg or cfg
            self._pending_listen_cfg = None
            await self._refresh_listen(refresh_cfg)

            await asyncio.gather(
                self._listener.idle_forever(),
                self._schedule2._async_main(),  # noqa: SLF001
            )

        except asyncio.CancelledError:

            pass

        except Exception as exc:

            error(f"WhatsApp 会话异常：{exc}")

        finally:

            self._connecting = False

            for _, c in connected:

                try:

                    await c.disconnect()

                except Exception:

                    pass

            await stop_all_clients()

            with self._lock:

                self._connected_ids.clear()

                self._clients.clear()

            self._loop = None

            self._schedule2.unbind_clients()

            if session == self._session_id:
                self._listener._running = False  # noqa: SLF001
                self._schedule2._running = False  # noqa: SLF001

    def request_watch_membership_audit(
        self,
        cfg: AppConfig,
        on_done: Callable[[Dict[str, WatchAuditRow]], None],
    ) -> bool:
        loop = self._loop
        with self._lock:
            clients = dict(self._clients)
        if loop is None or not loop.is_running() or not clients:
            return False

        def worker() -> None:
            fut = asyncio.run_coroutine_threadsafe(
                audit_address_book_watch_users(cfg, clients), loop
            )
            try:
                result = fut.result(timeout=600)
            except Exception as exc:
                warning(f"群成员检测失败：{exc}")
                result = {}
            try:
                on_done(result)
            except Exception:
                pass

        threading.Thread(target=worker, name="wa-watch-audit", daemon=True).start()
        return True

