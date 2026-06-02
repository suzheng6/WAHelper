"""应用退出：停止监听/定时/登录后台并清理 WhatsApp 连接与子进程。"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Optional

from logger_util import info, warning

if TYPE_CHECKING:
    from listener import ListenerController
    from schedule2_runner import Schedule2Runner
    from wa_coordinator import WaCoordinator

_shutdown_lock = threading.Lock()
_shutdown_done = False

_WA_THREAD_PREFIXES = (
    "wa-coordinator",
    "wa-login",
    "wa-post-login",
    "wa-reload",
)


def track_background_thread(t: threading.Thread) -> threading.Thread:
    if not t.name or t.name.startswith("Thread-"):
        t.name = f"wa-bg-{t.ident or id(t)}"
    return t


def _cancel_login(login_cancel: Optional[threading.Event]) -> None:
    if login_cancel is not None:
        login_cancel.set()


def _join_wa_threads(timeout: float) -> None:
    targets = [
        t
        for t in threading.enumerate()
        if t is not threading.current_thread() and t.is_alive()
        and (t.name or "").startswith(_WA_THREAD_PREFIXES)
    ]
    if not targets:
        return
    deadline = time.monotonic() + max(0.5, timeout)
    per = max(0.2, (deadline - time.monotonic()) / len(targets))
    for t in targets:
        remain = deadline - time.monotonic()
        if remain <= 0:
            break
        t.join(timeout=min(per, remain))
        if t.is_alive():
            warning(f"后台线程未在时限内结束：{t.name}")


def _kill_child_processes_windows() -> None:
    if sys.platform != "win32":
        return
    pid = os.getpid()
    try:
        ps = (
            f"Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.ParentProcessId -eq {pid} }} | "
            f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            timeout=8,
            check=False,
        )
    except Exception as exc:
        warning(f"清理子进程时：{exc}")


def _stop_neonize_clients() -> None:
    from wa_client import stop_all_clients_sync

    try:
        stop_all_clients_sync()
    except Exception as exc:
        warning(f"停止 WhatsApp 连接时：{exc}")


def shutdown_application(
    *,
    coord: Optional["WaCoordinator"] = None,
    listener: Optional["ListenerController"] = None,
    schedule2: Optional["Schedule2Runner"] = None,
    login_cancel: Optional[threading.Event] = None,
    join_timeout: float = 10.0,
) -> None:
    """幂等：停止所有本程序启动的后台任务与连接。"""
    global _shutdown_done
    with _shutdown_lock:
        if _shutdown_done:
            return
        _shutdown_done = True

    info("正在关闭 WhatsApp 助手…")
    _cancel_login(login_cancel)

    if listener is not None:
        try:
            listener.stop()
        except Exception as exc:
            warning(f"停止监听时：{exc}")

    if schedule2 is not None:
        try:
            schedule2.stop()
        except Exception as exc:
            warning(f"停止定时任务时：{exc}")
        try:
            from schedule2_runner import SHUTDOWN_PAUSE_REASON_S2, pause_all_schedule2_jobs_on_startup

            pause_all_schedule2_jobs_on_startup(SHUTDOWN_PAUSE_REASON_S2)
        except Exception as exc:
            warning(f"保存定时任务暂停状态时：{exc}")

    if coord is not None:
        try:
            coord.stop(join_timeout=join_timeout)
        except Exception as exc:
            warning(f"停止会话协调器时：{exc}")

    _stop_neonize_clients()
    _join_wa_threads(join_timeout)
    _kill_child_processes_windows()
    info("后台服务已停止")


def force_process_exit() -> None:
    """确保进程立即退出（避免 neonize/Go 非守护线程挂住）。"""
    os._exit(0)
