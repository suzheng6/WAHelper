"""入口：初始化日志、启动监听与定时线程、打开桌面 UI。"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

_pkg_root = Path(__file__).resolve().parent
if str(_pkg_root.parent) not in sys.path:
    sys.path.insert(0, str(_pkg_root.parent))

from tg_multi_listener.compat_config import LOGS_DIR, ensure_dirs, load_config
from tg_multi_listener.logger_util import info

if TYPE_CHECKING:
    from tg_multi_listener.telethon_coordinator import TelethonCoordinator


def main() -> None:
    import sys
    from pathlib import Path

    _wa_root = Path(__file__).resolve().parent.parent / "wa_multi_listener"
    if str(_wa_root) not in sys.path:
        sys.path.insert(0, str(_wa_root))
    from startup_bootstrap import bootstrap_tg_logging, bootstrap_tg_runtime

    bootstrap_tg_runtime()
    bootstrap_tg_logging()
    info("应用启动")
    from tg_multi_listener.listener import ListenerController
    from tg_multi_listener.scheduler import ScheduleRunner, pause_all_doc_jobs_on_startup

    pause_all_doc_jobs_on_startup()

    listener = ListenerController()
    scheduler = ScheduleRunner()

    import customtkinter as ctk
    from tg_multi_listener.ui.app import MainWindow

    root = ctk.CTk()
    app = MainWindow(root, listener, scheduler, embedded=False)
    app.pack(fill="both", expand=True)
    try:
        root.update_idletasks()
    except Exception:
        pass

    coord_holder: list[TelethonCoordinator | None] = [None]

    def start_backend() -> None:
        from tg_multi_listener.telethon_coordinator import TelethonCoordinator

        cfg = load_config()
        listener.start(cfg, app.alert_callback)
        scheduler.start(cfg)
        coord = TelethonCoordinator(listener, scheduler)
        app.bind_coordinator(coord)
        if not coord.start(cfg):
            from tg_multi_listener.logger_util import error

            error("Telegram 后台未能启动：请查看日志。")
        coord_holder[0] = coord

    app.after(0, start_backend)

    try:
        root.mainloop()
    finally:
        c = coord_holder[0]
        if c is not None:
            c.stop()
        else:
            listener.stop()
            scheduler.stop()
        info("应用退出")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:
        try:
            ensure_dirs()
            err_path = os.path.join(LOGS_DIR, "startup_error.log")
            with open(err_path, "a", encoding="utf-8") as f:
                f.write("\n---\n")
                traceback.print_exc(file=f)
        except OSError:
            pass
        raise
