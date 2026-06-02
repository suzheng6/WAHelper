"""JSON 原子写入：进程内锁 + 重试，缓解 Windows 上 os.replace 偶发 WinError 5。"""
from __future__ import annotations

import json
import os
import time
from threading import Lock
from typing import Any

_locks: dict[str, Lock] = {}


def _path_lock(path: str) -> Lock:
    key = os.path.abspath(path)
    if key not in _locks:
        _locks[key] = Lock()
    return _locks[key]


def atomic_write_json(path: str, data: Any, *, indent: int = 2) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    lock = _path_lock(path)
    last_err: OSError | None = None
    with lock:
        for attempt in range(6):
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=indent)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
                return
            except OSError as exc:
                last_err = exc
                time.sleep(0.04 * (attempt + 1))
                try:
                    if os.path.isfile(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
    if last_err is not None:
        raise last_err
