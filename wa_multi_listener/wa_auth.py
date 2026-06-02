"""后台线程扫码登录 WhatsApp（始终清除旧会话后重新扫码）。"""

from __future__ import annotations



import asyncio

import threading

import time

from typing import Callable, Optional



from neonize.aioze.client import NewAClient

from neonize.aioze.events import ConnectedEv, ConnectFailureEv, DisconnectedEv, LoggedOutEv, PairStatusEv

from neonize.proto.Neonize_pb2 import PairStatus as PairStatusMsg



from config import Account

from logger_util import error, info, warning

from session_check import has_saved_session

from wa_client import (

    clear_session_files,

    connect_failure_message,

    new_client,

    pair_error_message,

)



_SESSION_WAIT_SEC = 25.0

_CLIENT_STOP_TIMEOUT_SEC = 8.0





async def _wait_session_persisted(acc: Account, timeout: float = _SESSION_WAIT_SEC) -> bool:

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:

        if has_saved_session(acc):

            return True

        await asyncio.sleep(0.35)

    return False





async def _release_login_client(client: NewAClient, timeout: float = _CLIENT_STOP_TIMEOUT_SEC) -> None:

    task = getattr(client, "connect_task", None)

    if task is not None and not task.done():

        task.cancel()

        try:

            await asyncio.wait_for(task, timeout=2.0)

        except Exception:

            pass

    try:

        await asyncio.wait_for(client.stop(), timeout=timeout)

    except asyncio.TimeoutError:

        warning("登录客户端停止超时（会话已保存，可继续上线）")

    except Exception as exc:

        warning(f"停止登录客户端时：{exc}")





def _release_client_background(client: NewAClient, account_id: str) -> None:

    def _run() -> None:

        try:

            asyncio.run(_release_login_client(client))

        except Exception as exc:

            warning(f"后台释放登录连接「{account_id}」：{exc}")



    threading.Thread(

        target=_run, name=f"wa-login-release-{account_id}", daemon=True

    ).start()





def run_qr_login_in_thread(

    acc: Account,

    on_qr: Callable[[bytes], None],

    on_done: Callable[[bool, str], None],

    *,

    cancel_event: Optional[threading.Event] = None,

    on_status: Optional[Callable[[str], None]] = None,

    before_connect: Optional[Callable[[], None]] = None,

) -> threading.Thread:

    def _status(s: str) -> None:

        if on_status:

            try:

                on_status(s)

            except Exception:

                pass



    def work() -> None:

        ok = False

        msg = ""

        fail_msg: list[str] = [""]

        client: Optional[NewAClient] = None

        ui_notified = False



        def notify_ui(success: bool, text: str) -> None:

            nonlocal ui_notified

            if ui_notified:

                return

            ui_notified = True

            try:

                on_done(success, text)

            except Exception as exc:

                error(f"登录完成回调异常：{exc}")



        async def main() -> None:

            nonlocal ok, msg, client

            if before_connect:

                try:

                    before_connect()

                except Exception as exc:

                    info(f"登录准备：{exc}")



            clear_session_files(acc)

            _status("正在准备扫码…")



            if cancel_event and cancel_event.is_set():

                msg = "已取消"

                return



            client = new_client(acc)

            _status("等待 WhatsApp 下发二维码…")



            connected_ev = asyncio.Event()

            failed_ev = asyncio.Event()



            @client.qr

            async def _qr(_c, data: bytes) -> None:

                if cancel_event and cancel_event.is_set():

                    return

                _status("请用手机 WhatsApp 扫码（设置 → 已连接的设备 → 连接设备）")

                try:

                    on_qr(data)

                except Exception:

                    pass



            @client.event(PairStatusEv)

            async def _pair(_c, ev: PairStatusEv) -> None:

                if ev.Status == PairStatusMsg.SUCCESS:

                    _status("手机已确认，正在完成关联…")

                    info(f"账号「{acc.id}」配对成功")

                elif ev.Status == PairStatusMsg.ERROR:

                    fail_msg[0] = pair_error_message(ev.Error)

                    failed_ev.set()

                    error(f"账号「{acc.id}」配对失败：{ev.Error}")



            @client.event(ConnectedEv)

            async def _connected(_c, _ev: ConnectedEv) -> None:

                nonlocal ok, msg

                ok = True

                msg = "登录成功"

                connected_ev.set()

                info(f"账号「{acc.id}」已连接 WhatsApp")



            @client.event(ConnectFailureEv)

            async def _cf(_c, ev: ConnectFailureEv) -> None:

                fail_msg[0] = connect_failure_message(ev.Reason, ev.Message or "")

                failed_ev.set()



            @client.event(LoggedOutEv)

            async def _lo(_c, ev: LoggedOutEv) -> None:

                fail_msg[0] = connect_failure_message(ev.Reason, "已登出")

                failed_ev.set()



            @client.event(DisconnectedEv)

            async def _dc(_c, _ev: DisconnectedEv) -> None:

                pass



            _status("正在连接 WhatsApp 服务器…")

            await client.connect()



            deadline = asyncio.get_event_loop().time() + 180.0

            while asyncio.get_event_loop().time() < deadline:

                if cancel_event and cancel_event.is_set():

                    msg = "已取消"

                    return

                if failed_ev.is_set():

                    msg = fail_msg[0] or "无法关联设备，请重试"

                    return

                if connected_ev.is_set() or client.connected:

                    ok = True

                    msg = "登录成功"

                    _status("关联成功，正在保存会话…")

                    if await _wait_session_persisted(acc):

                        info(f"账号「{acc.id}」会话已写入本地")

                    else:

                        warning(f"账号「{acc.id}」会话文件尚未检测到，仍将尝试上线")

                    # 先通知 UI 关闭弹窗，再在后台释放连接（避免 stop 卡住界面）

                    notify_ui(True, msg)

                    return

                await asyncio.sleep(0.2)

            msg = fail_msg[0] or "扫码超时，请重试"



        try:

            asyncio.run(main())

        except Exception as exc:

            if not ok:

                msg = fail_msg[0] or str(exc) or "登录失败"

                error(f"登录异常：{exc}")

        finally:

            if client is not None:

                _release_client_background(client, acc.id)



        if cancel_event and cancel_event.is_set() and not ok:

            msg = "已取消"

        if not ui_notified:
            notify_ui(ok, msg or ("登录成功" if ok else "登录失败"))



    from shutdown import track_background_thread



    t = track_background_thread(

        threading.Thread(target=work, name=f"wa-login-{acc.id}", daemon=True)

    )

    t.start()

    return t


