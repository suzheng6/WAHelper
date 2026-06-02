"""嵌入整合版标签页：在子 Frame 内启动 Telegram 助手。"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

import customtkinter as ctk

_TG_ROOT = Path(__file__).resolve().parent.parent
_AI_ROOT = _TG_ROOT.parent
_WA_ROOT = _AI_ROOT / "wa_multi_listener"


def _prepare_tg_import_path() -> None:
    """TG 包优先；排除 WA 根目录，避免 `ui` / `config` 与 WA 冲突（尤其打包后）。"""
    tg_dir = str(_TG_ROOT)
    ai_root = str(_AI_ROOT)
    wa_dir = os.path.normpath(str(_WA_ROOT))
    ordered: List[str] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            ordered.append(meipass)
    for p in (tg_dir, ai_root):
        np = os.path.normpath(p)
        if np not in (os.path.normpath(x) for x in ordered):
            ordered.append(p)
    for p in sys.path:
        np = os.path.normpath(p)
        if np == wa_dir:
            continue
        if np in (os.path.normpath(x) for x in ordered):
            continue
        ordered.append(p)
    sys.path[:] = ordered


def mount_tg_panel(
    parent: ctk.CTkFrame,
    tg_data_root: str,
    *,
    on_register_shutdown: Optional[Callable[[Callable[[], None]], None]] = None,
) -> Tuple[Any, Any, Any, List[Any]]:
    """
    在 parent 内挂载 TG UI。
    返回 (panel, listener, scheduler, coord_holder_list)。
    """
    os.environ["TG_HELPER_DATA_ROOT"] = tg_data_root
    wa_root = str(_WA_ROOT)
    if wa_root not in sys.path:
        sys.path.insert(0, wa_root)
    try:
        from legacy_tg_import import ensure_tg_data_ready

        ensure_tg_data_ready()
    except ImportError:
        os.makedirs(tg_data_root, exist_ok=True)
        os.makedirs(os.path.join(tg_data_root, "sessions"), exist_ok=True)

    _prepare_tg_import_path()

    from tg_multi_listener.compat_config import load_config
    from tg_multi_listener.listener import ListenerController
    from tg_multi_listener.logger_util import error, info
    from tg_multi_listener.scheduler import ScheduleRunner, pause_all_doc_jobs_on_startup
    from tg_multi_listener.ui.app import MainWindow

    from startup_bootstrap import bootstrap_tg_logging, bootstrap_tg_runtime

    bootstrap_tg_runtime()
    bootstrap_tg_logging()
    info("Telegram 助手（整合标签）")

    pause_all_doc_jobs_on_startup()
    listener = ListenerController()
    scheduler = ScheduleRunner()
    panel = MainWindow(parent, listener, scheduler, embedded=True)
    panel.pack(fill="both", expand=True)

    coord_holder: List[Any] = [None]

    def start_backend() -> None:
        try:
            from tg_multi_listener.telethon_coordinator import TelethonCoordinator

            cfg = load_config()
            listener.start(cfg, panel.alert_callback)
            scheduler.start(cfg)
            coord = TelethonCoordinator(listener, scheduler)
            panel.bind_coordinator(coord)
            if not coord.start(cfg):
                error("Telegram 后台未能启动：上一会话线程未退出，请稍候或重启程序。")
            coord_holder[0] = coord
        except Exception as exc:
            error(f"Telegram 后台启动失败：{exc}\n{traceback.format_exc()}")

    panel.after(0, start_backend)

    if on_register_shutdown is not None:

        def _shutdown() -> None:
            c = coord_holder[0]
            if c is not None:
                c.stop()
            else:
                listener.stop()
                scheduler.stop()
            panel.shutdown_ui()

        on_register_shutdown(_shutdown)

    return panel, listener, scheduler, coord_holder
