"""仪表盘：今日提醒计数。"""
from __future__ import annotations

import json
import os
from datetime import date
from threading import Lock

from config import DATA_DIR, ensure_dirs

STATS_FILE = os.path.join(DATA_DIR, "stats.json")
_lock = Lock()


def _path() -> str:
    ensure_dirs()
    return STATS_FILE


def record_alert() -> None:
    today = str(date.today())
    with _lock:
        data = {"day": today, "count": 0}
        if os.path.isfile(_path()):
            try:
                with open(_path(), "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        if data.get("day") != today:
            data = {"day": today, "count": 0}
        data["count"] = int(data.get("count", 0)) + 1
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _path())


def today_alert_count() -> int:
    today = str(date.today())
    with _lock:
        if not os.path.isfile(_path()):
            return 0
        try:
            with open(_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return 0
        if data.get("day") != today:
            return 0
        return int(data.get("count", 0))
