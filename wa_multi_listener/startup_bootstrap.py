"""进程内一次性启动初始化，避免 shell / main / TG 嵌入重复迁移与日志配置。"""
from __future__ import annotations

_wa_runtime_done = False
_wa_logging_done = False
_tg_runtime_done = False
_tg_logging_done = False


def bootstrap_wa_runtime() -> None:
    global _wa_runtime_done
    if _wa_runtime_done:
        return
    from config import ensure_runtime

    ensure_runtime()
    _wa_runtime_done = True


def bootstrap_wa_logging() -> None:
    global _wa_logging_done
    if _wa_logging_done:
        return
    from logger_util import setup_file_logging

    setup_file_logging()
    _wa_logging_done = True


def bootstrap_tg_runtime() -> None:
    global _tg_runtime_done
    if _tg_runtime_done:
        return
    from tg_multi_listener.compat_config import ensure_runtime

    ensure_runtime()
    _tg_runtime_done = True


def bootstrap_tg_logging() -> None:
    global _tg_logging_done
    if _tg_logging_done:
        return
    from tg_multi_listener.logger_util import setup_file_logging

    setup_file_logging()
    _tg_logging_done = True
