"""单线程 asyncio：监听与定时任务共享同一套 TelegramClient，避免 .session 数据库锁冲突。"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from telethon import TelegramClient
from telethon.errors import RPCError

from .compat_config import Account, AppConfig
from .listener import (
    AddressBookResolver,
    ListenerController,
    _resolve_watch_rules,
    resolve_address_book_refs_with_owner_retry,
)
from .logger_util import error, info, warning
from .scheduler import ScheduleRunner, load_jobs
from .watch_membership_audit import WatchAuditRow, audit_address_book_watch_users

# 与登录前停服务一致：重载/关闭须等旧 asyncio 线程退出，避免双 loop 共用 client 导致发送失败与重复重试。
DEFAULT_JOIN_TIMEOUT = 28.0


class TelethonCoordinator:
    def __init__(self, listener: ListenerController, scheduler: ScheduleRunner) -> None:
        self._listener = listener
        self._scheduler = scheduler
        scheduler.bind_read_tracker(listener.read_tracker)
        self._thread: Optional[threading.Thread] = None
        self._orphan_thread: Optional[threading.Thread] = None
        self._svc_running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._clients_by_id: Dict[str, TelegramClient] = {}
        self._address_resolver = AddressBookResolver()

    def has_connected_clients(self) -> bool:
        return bool(self._clients_by_id)

    def apply_config_hot(self, cfg: AppConfig) -> None:
        """通讯录/监听变更后在线解析群并刷新监听，无需整程序重启。"""
        if self._orphan_thread is not None and self._orphan_thread.is_alive():
            warning(
                "无法热更新：上一 Telegram 会话线程仍在退出中。"
                "请稍候再试，或点「保存并重载服务」。"
            )
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            info("通讯录已保存；请点侧栏「保存并重载服务」，或等待 Telegram 连接完成后再试。")
            return
        if not self._clients_by_id:
            info("通讯录已保存；当前无在线账号，请先登录账号或「保存并重载服务」。")
            return
        asyncio.run_coroutine_threadsafe(self._apply_config_async(cfg), loop)

    async def _apply_config_async(self, cfg: AppConfig) -> None:
        clients = self._clients_by_id
        if not clients:
            return
        client = next(iter(clients.values()))
        await resolve_address_book_refs_with_owner_retry(
            client, cfg, clients, resolver=self._address_resolver, full=False
        )
        self._scheduler.refresh_config(cfg)
        if not self._listener.listen_enabled():
            info("通讯录已更新：群标识已解析（监听未开启）")
            return
        rules = await _resolve_watch_rules(
            client, cfg, clients, resolver=self._address_resolver, full=False
        )
        self._listener.set_watch_rules(rules)
        if rules:
            info(f"通讯录已更新：已解析 {len(rules)} 条监听绑定并立即生效")
        else:
            warning("通讯录已更新，但未能解析任何「参与监听」的群/用户绑定")

    def request_watch_membership_audit(
        self,
        cfg: AppConfig,
        on_done: Callable[[Dict[str, WatchAuditRow]], None],
    ) -> bool:
        loop = self._loop
        clients = dict(self._clients_by_id)
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

        threading.Thread(target=worker, name="tg-watch-audit", daemon=True).start()
        return True

    def _session_threads(self) -> List[threading.Thread]:
        out: List[threading.Thread] = []
        for t in (self._thread, self._orphan_thread):
            if t is not None and t not in out:
                out.append(t)
        return out

    def session_thread_alive(self) -> bool:
        return any(t.is_alive() for t in self._session_threads())

    def start(self, cfg: AppConfig) -> bool:
        if not self.stop(join_timeout=DEFAULT_JOIN_TIMEOUT):
            error(
                "无法启动 Telegram 统一会话：上一会话线程仍在运行。"
                "请先暂停全部定时任务，等待约半分钟后再点「保存并重载服务」；"
                "若仍失败，请完全退出程序后重新打开。"
            )
            self._svc_running = False
            return False
        self._svc_running = True

        def run() -> None:
            try:
                asyncio.run(self._async_run(cfg))
            finally:
                if self._thread is threading.current_thread():
                    self._thread = None

        t = threading.Thread(target=run, name="telethon-coordinator", daemon=True)
        self._thread = t
        self._orphan_thread = None
        t.start()
        info("Telegram 统一会话线程已启动（监听 + 定时任务）")
        return True

    def stop(self, join_timeout: float = DEFAULT_JOIN_TIMEOUT) -> bool:
        """请求停止并等待后台线程结束。返回 True 表示可安全再次 start。"""
        self._svc_running = False
        self._listener.stop()
        self._scheduler.stop()
        to_join = [t for t in self._session_threads() if t.is_alive()]
        self._thread = None
        self._orphan_thread = None
        self._loop = None
        self._clients_by_id = {}
        self._address_resolver.clear()

        still_alive: List[threading.Thread] = []
        deadline = time.monotonic() + max(0.05, float(join_timeout))
        for t in to_join:
            if t is threading.current_thread():
                continue
            while t.is_alive() and time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                t.join(timeout=min(1.0, max(0.05, remaining)))
            if t.is_alive():
                still_alive.append(t)

        if still_alive:
            self._orphan_thread = still_alive[0]
            names = ", ".join(t.name or "telethon-coordinator" for t in still_alive)
            warning(
                f"Telegram 统一会话线程在 {join_timeout:.0f}s 内仍未退出（{names}）。"
                "已禁止启动新会话，以免 event loop 冲突导致发送失败或重复消息。"
                "请先暂停定时任务，稍候再重载；或完全退出程序后重开。"
            )
            return False

        info("Telegram 统一会话线程已停止")
        return True

    def is_running(self) -> bool:
        return bool(self._svc_running and self.session_thread_alive())

    async def _async_run(self, cfg: AppConfig) -> None:
        self._loop = asyncio.get_running_loop()
        self._clients_by_id = {}
        self._listener._running = True  # noqa: SLF001
        self._scheduler._running = True  # noqa: SLF001

        accounts = [a for a in cfg.accounts if a.enabled]
        connected: List[Tuple[Account, TelegramClient]] = []

        if not int(cfg.api_id) or not str(cfg.api_hash).strip():
            warning("未配置 api_id / api_hash：无法连接 Telegram，定时任务将只能排队无法发送")
            self._scheduler.bind_telegram_clients({})
            try:
                await asyncio.gather(
                    self._listener.idle_forever(),
                    self._scheduler._async_main(),  # noqa: SLF001
                )
            finally:
                self._listener._running = False  # noqa: SLF001
                self._scheduler.unbind_telegram_clients()
                self._scheduler._running = False  # noqa: SLF001
                self._loop = None
                self._clients_by_id = {}
            return

        info(f"Telegram：准备依次连接 {len(accounts)} 个已启用账号…")
        for acc in accounts:
            c = TelegramClient(acc.session_path(), cfg.api_id, cfg.api_hash)
            try:
                info(f"Telegram：正在连接「{acc.id}」…")
                await c.connect()
            except RPCError as exc:
                warning(f"连接失败 {acc.id}：{exc}")
                try:
                    await c.disconnect()
                except Exception:
                    pass
                continue
            except Exception as exc:
                error(
                    f"连接失败（非 RPC）{acc.id}：{exc}"
                    "（若提示 database is locked，请关闭另一份本程序或占用该 .session 的其它工具）"
                )
                try:
                    await c.disconnect()
                except Exception:
                    pass
                continue
            if not await c.is_user_authorized():
                warning(f"账号未登录，跳过：{acc.id}")
                try:
                    await c.disconnect()
                except Exception:
                    pass
                continue
            connected.append((acc, c))
            info(f"已连接账号：{acc.id}")

        clients_by_id: Dict[str, TelegramClient] = {acc.id: c for acc, c in connected}
        self._clients_by_id = dict(clients_by_id)
        if not clients_by_id:
            error(
                "Telegram：没有任何账号连接成功，定时任务将不会发出消息。"
                "请在本软件完成各账号登录；并确认未同时打开第二份本程序、未用其它工具占用 sessions 目录下同名 .session。"
            )

        try:
            self._scheduler.bind_telegram_clients(clients_by_id)
            try:
                nj = len(load_jobs())
                n_run = sum(1 for x in load_jobs() if x.enabled and x.state == "running")
                n_steps = sum(x.item_count() for x in load_jobs())
                info(f"定时载入：文档任务 {nj} 个 / {n_steps} 步，运行中 {n_run} 个。")
            except Exception:
                pass

            if connected:
                primary = connected[0][1]
                await resolve_address_book_refs_with_owner_retry(
                    primary, cfg, clients_by_id, resolver=self._address_resolver, full=True
                )

            if self._listener.listen_enabled() and connected:
                resolved = await _resolve_watch_rules(
                    connected[0][1], cfg, clients_by_id, resolver=self._address_resolver, full=True
                )
                if resolved:
                    rate = float(cfg.rate_limit_seconds)
                    for _, client in connected:
                        self._listener.register_client_handlers(client, resolved, rate)
                else:
                    warning("没有可用的群组绑定，未注册监听事件（定时任务仍可用）")

            await asyncio.gather(
                self._listener.idle_forever(),
                self._scheduler._async_main(),  # noqa: SLF001
            )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            error(f"Telegram 统一会话异常：{exc}")
        finally:
            for _, c in connected:
                try:
                    await c.disconnect()
                except Exception:
                    pass
            self._listener._running = False  # noqa: SLF001
            self._scheduler.unbind_telegram_clients()
            self._scheduler._running = False  # noqa: SLF001
            self._loop = None
            self._clients_by_id = {}
            self._address_resolver.clear()
            info("Telegram 会话已断开")
