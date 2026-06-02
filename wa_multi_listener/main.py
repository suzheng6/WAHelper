"""超群小帮手：WhatsApp + Telegram 整合入口。"""
from __future__ import annotations

import os
import traceback

from config import LOGS_DIR, ensure_dirs
from startup_bootstrap import bootstrap_wa_logging, bootstrap_wa_runtime


def main() -> None:
    bootstrap_wa_runtime()
    bootstrap_wa_logging()

    from wa_ui.shell import run_shell

    app = run_shell()
    coord_holder: list = [None]
    coord_holder[0] = app._wa_coord_holder[0] if app._wa_coord_holder else None

    try:
        app.mainloop()
    finally:
        from shutdown import force_process_exit, shutdown_application

        shutdown_application(
            coord=coord_holder[0],
            listener=app._wa_listener,
            schedule2=app._wa_schedule2,
            login_cancel=getattr(app._wa_panel, "_login_cancel", None) if app._wa_panel else None,
            join_timeout=10.0,
        )
        if app._tg_shutdown_fn:
            try:
                app._tg_shutdown_fn()
            except Exception:
                pass
        force_process_exit()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        try:
            ensure_dirs()
            with open(os.path.join(LOGS_DIR, "startup_error.log"), "a", encoding="utf-8") as f:
                f.write("\n---\n")
                traceback.print_exc(file=f)
        except OSError:
            pass
        raise
