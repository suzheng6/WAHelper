"""任务管理卡片配色：运行 / 监听暂停 / 阶段提醒+监听 / 已停止 / 其它暂停。"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypedDict

import customtkinter as ctk

TaskmgrBucket = str  # running | listen_pause | stage_listen_pause | stopped_listen_pause | other_pause | stopped

STOPPED_LISTEN_HIT_PAUSE_REASON = "已停止任务监听命中，请关注"

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

# 阶段提醒暂停后又被监听命中：紫色，与金/红/绿/灰均区分
_STAGE_LISTEN_PAUSE: TaskmgrTilePalette = {
    "fg": "#2a1848",
    "border": "#9b59b6",
    "status": "#e8c8ff",
    "body": "#f3e8ff",
    "file": "#d4b8f0",
    "title": "#ffffff",
    "hint": "#ececec",
}

# 已停止任务再被监听命中：青蓝，与金色「运行中监听」、紫色「提醒+监听」区分
_STOPPED_LISTEN_PAUSE: TaskmgrTilePalette = {
    "fg": "#123a4a",
    "border": "#1abc9c",
    "status": "#b8fff0",
    "body": "#e0faf5",
    "file": "#a8e8dc",
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


def _is_stopped_listen_pause(pause_reason: str) -> bool:
    r = (pause_reason or "").strip()
    if _is_stage_listen_pause(r):
        return False
    return "已停止" in r and "监听命中" in r


def _is_listen_hit_pause(pause_reason: str) -> bool:
    r = (pause_reason or "").strip()
    if _is_stage_listen_pause(r) or _is_stopped_listen_pause(r):
        return False
    return "监听命中" in r


def _is_stage_listen_pause(pause_reason: str) -> bool:
    r = (pause_reason or "").strip()
    return "阶段提醒" in r and "监听命中" in r


def _is_stage_reminder_pause(pause_reason: str) -> bool:
    r = (pause_reason or "").strip()
    if _is_stage_listen_pause(r):
        return False
    return "阶段提醒" in r and "已自动暂停" in r


def compose_listen_pause_reason(previous_reason: str, listen_reason: str) -> str:
    """阶段提醒已暂停的任务再被监听命中时，使用组合原因以触发专用卡片色。"""
    prev = (previous_reason or "").strip()
    if "阶段提醒" in prev and "已自动暂停" in prev:
        return "阶段提醒后监听命中，自动暂停"
    return (listen_reason or "").strip() or "监听命中目标用户，自动暂停"


def resolve_listen_pause_reason(*, enabled: bool, previous_reason: str, listen_reason: str) -> str:
    """根据任务是否仍在启用，生成监听命中后的暂停原因文案。"""
    if not enabled:
        return STOPPED_LISTEN_HIT_PAUSE_REASON
    return compose_listen_pause_reason(previous_reason, listen_reason)


def taskmgr_display_sort_tier(*, running: bool, enabled: bool, pause_reason: str = "") -> int:
    """任务管理页排序层级（越小越靠前）：监听/提醒+监听 > 阶段提醒 > 其它暂停 > 其余。"""
    r = (pause_reason or "").strip()
    if _is_stage_listen_pause(r) or _is_stopped_listen_pause(r) or _is_listen_hit_pause(r):
        return 0
    if running:
        return 3
    if not enabled:
        return 3
    if _is_stage_reminder_pause(r):
        return 1
    return 2


def taskmgr_needs_attention(*, running: bool, enabled: bool, pause_reason: str = "") -> bool:
    """是否置顶（监听命中含已停止；阶段提醒/其它暂停仅 enabled 且未运行）。"""
    return taskmgr_display_sort_tier(
        running=running,
        enabled=enabled,
        pause_reason=pause_reason,
    ) < 3


def taskmgr_sort_jobs_for_display(
    jobs: Iterable[Any],
    *,
    is_running: Callable[[Any], bool],
) -> list[Any]:
    """置顶需人工处理的任务；同层内保持 schedules.json 原顺序。"""
    indexed = list(enumerate(jobs))

    def _key(item: tuple[int, Any]) -> tuple[int, int]:
        i, j = item
        tier = taskmgr_display_sort_tier(
            running=is_running(j),
            enabled=bool(getattr(j, "enabled", False)),
            pause_reason=(getattr(j, "pause_reason", None) or ""),
        )
        return (tier, i)

    return [j for _, j in sorted(indexed, key=_key)]


def taskmgr_job_bucket(*, running: bool, enabled: bool, pause_reason: str = "") -> TaskmgrBucket:
    if running:
        return "running"
    if _is_stage_listen_pause(pause_reason):
        return "stage_listen_pause"
    if _is_stopped_listen_pause(pause_reason):
        return "stopped_listen_pause"
    if _is_listen_hit_pause(pause_reason):
        return "listen_pause"
    if not enabled:
        return "stopped"
    return "other_pause"


def taskmgr_tile_palette(*, running: bool, enabled: bool, pause_reason: str = "") -> TaskmgrTilePalette:
    bucket = taskmgr_job_bucket(running=running, enabled=enabled, pause_reason=pause_reason)
    if bucket == "running":
        return dict(_RUNNING)
    if bucket == "stage_listen_pause":
        return dict(_STAGE_LISTEN_PAUSE)
    if bucket == "stopped_listen_pause":
        return dict(_STOPPED_LISTEN_PAUSE)
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
    counts = {
        "total": 0,
        "running": 0,
        "listen_pause": 0,
        "stage_listen_pause": 0,
        "stopped_listen_pause": 0,
        "other_pause": 0,
        "stopped": 0,
    }
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
        ("stage_listen_pause", "提醒+监听"),
        ("stopped_listen_pause", "已停+监听"),
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
