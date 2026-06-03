"""任务管理卡片配色：运行 / 监听暂停 / 已停止 / 其它暂停。"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypedDict

import customtkinter as ctk

TaskmgrBucket = str  # running | listen_pause | other_pause | stopped

_TASKMGR_FONTS: dict[str, ctk.CTkFont] | None = None


class TaskmgrTilePalette(TypedDict):
    fg: str
    border: str
    status: str
    body: str
    file: str
    title: str
    hint: str


_RUNNING: TaskmgrTilePalette = {
    "fg": "#1a4d32",
    "border": "#2ecc71",
    "status": "#9dffca",
    "body": "#f5fffa",
    "file": "#def7e9",
    "title": "#ffffff",
    "hint": "#e9e9e9",
}

_STOPPED: TaskmgrTilePalette = {
    "fg": "#2a3038",
    "border": "#4a5160",
    "status": "#9aa3b2",
    "body": "#b0b7c3",
    "file": "#949bab",
    "title": "#c5cad3",
    "hint": "#8b919c",
}

_LISTEN_PAUSE: TaskmgrTilePalette = {
    "fg": "#3a3018",
    "border": "#c9a227",
    "status": "#ffe7a8",
    "body": "#fff3d6",
    "file": "#f0d998",
    "title": "#ffffff",
    "hint": "#ececec",
}

_OTHER_PAUSE: TaskmgrTilePalette = {
    "fg": "#5c2424",
    "border": "#e74c3c",
    "status": "#ffd7d7",
    "body": "#fff5f5",
    "file": "#ffe8e8",
    "title": "#ffffff",
    "hint": "#e9e9e9",
}


def _is_listen_hit_pause(pause_reason: str) -> bool:
    return "监听命中" in (pause_reason or "")


def taskmgr_job_bucket(*, running: bool, enabled: bool, pause_reason: str = "") -> TaskmgrBucket:
    if running:
        return "running"
    if _is_listen_hit_pause(pause_reason):
        return "listen_pause"
    if not enabled:
        return "stopped"
    return "other_pause"


def taskmgr_tile_palette(*, running: bool, enabled: bool, pause_reason: str = "") -> TaskmgrTilePalette:
    bucket = taskmgr_job_bucket(running=running, enabled=enabled, pause_reason=pause_reason)
    if bucket == "running":
        return dict(_RUNNING)
    if bucket == "listen_pause":
        return dict(_LISTEN_PAUSE)
    if bucket == "stopped":
        return dict(_STOPPED)
    return dict(_OTHER_PAUSE)


def taskmgr_count_jobs(
    jobs: Iterable[Any],
    *,
    is_running: Callable[[Any], bool],
) -> dict[str, int]:
    counts = {"total": 0, "running": 0, "listen_pause": 0, "other_pause": 0, "stopped": 0}
    for j in jobs:
        counts["total"] += 1
        bucket = taskmgr_job_bucket(
            running=is_running(j),
            enabled=bool(getattr(j, "enabled", False)),
            pause_reason=(getattr(j, "pause_reason", None) or ""),
        )
        counts[bucket] += 1
    return counts


def format_taskmgr_count_summary(counts: dict[str, int]) -> str:
    total = counts.get("total", 0)
    if total <= 0:
        return "任务数量：0"
    parts = [f"任务数量：{total}"]
    for key, label in (
        ("running", "运行中"),
        ("listen_pause", "监听暂停"),
        ("other_pause", "其它暂停"),
        ("stopped", "已停止"),
    ):
        n = counts.get(key, 0)
        if n:
            parts.append(f"{label} {n}")
    return " · ".join(parts)


def taskmgr_fonts() -> dict[str, ctk.CTkFont]:
    global _TASKMGR_FONTS
    if _TASKMGR_FONTS is None:
        family = "Microsoft YaHei UI"
        _TASKMGR_FONTS = {
            "title": ctk.CTkFont(family=family, size=15, weight="bold"),
            "status": ctk.CTkFont(family=family, size=12, weight="bold"),
            "body": ctk.CTkFont(family=family, size=12),
            "reminder": ctk.CTkFont(family=family, size=12, weight="bold"),
            "hint": ctk.CTkFont(family=family, size=11),
            "btn": ctk.CTkFont(family=family, size=11, weight="bold"),
        }
    return _TASKMGR_FONTS


def taskmgr_card_status_text(status: str, step: str) -> str:
    """状态 + 步骤合并为一行块，减少卡片内 Label 数量。"""
    status = (status or "").strip()
    step = (step or "").strip()
    if status and step:
        return f"{status}\n{step}"
    return status or step
