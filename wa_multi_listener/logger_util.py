"""文件日志与内存环形缓冲。"""
from __future__ import annotations

import logging
import os
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from threading import Lock
from typing import Callable, Deque, List, Optional

from config import LOGS_DIR, ensure_dirs

_memory: Deque[str] = deque(maxlen=250)
_listeners: List[Callable[[str], None]] = []
_lock = Lock()

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"
DATE_FMT = "%Y-%m-%d %H:%M:%S"


def setup_file_logging() -> logging.Logger:
    ensure_dirs()
    log_path = os.path.join(LOGS_DIR, "app.log")
    root = logging.getLogger("wa_multi_listener")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    fh = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FMT))
    root.addHandler(fh)
    return root


def log_line(level: int, msg: str) -> None:
    line = f"{datetime.now().strftime(DATE_FMT)} | {logging.getLevelName(level)} | {msg}"
    with _lock:
        _memory.append(line)
        targets = list(_listeners)
    for cb in targets:
        try:
            cb(line)
        except Exception:
            pass
    logging.getLogger("wa_multi_listener").log(level, msg)


def debug(msg: str) -> None:
    """仅写入 app.log（DEBUG），不刷到界面日志面板。"""
    logging.getLogger("wa_multi_listener").debug(msg)


def info(msg: str) -> None:
    log_line(logging.INFO, msg)


def warning(msg: str) -> None:
    log_line(logging.WARNING, msg)


def error(msg: str) -> None:
    log_line(logging.ERROR, msg)


def get_recent_lines(limit: int = 200) -> List[str]:
    with _lock:
        return list(_memory)[-limit:]


def add_memory_listener(cb: Callable[[str], None]) -> None:
    with _lock:
        if cb not in _listeners:
            _listeners.append(cb)


def remove_memory_listener(cb: Callable[[str], None]) -> None:
    with _lock:
        try:
            _listeners.remove(cb)
        except ValueError:
            pass
