"""定时任务文件夹源：扫描、排序、展示文案。"""
from __future__ import annotations

import os
import random
import re
from typing import Any, List, Tuple

_FOLDER_TXT_NUM = re.compile(r"^(\d+)")

# 「一键开始下一天」后各任务独立随机延迟，避免同时发送
FOLDER_ADVANCE_DELAY_MIN_SEC = 10.0
FOLDER_ADVANCE_DELAY_MAX_SEC = 60.0


def random_folder_advance_delay_seconds() -> float:
    return random.uniform(FOLDER_ADVANCE_DELAY_MIN_SEC, FOLDER_ADVANCE_DELAY_MAX_SEC)


def scan_schedule_folder(folder_path: str) -> Tuple[List[str], List[str]]:
    """扫描文件夹内 TXT：仅保留文件名以数字开头的，按数字升序。"""
    root = os.path.abspath((folder_path or "").strip())
    if not root or not os.path.isdir(root):
        return [], ["路径不是有效文件夹"]
    numbered: dict[int, str] = {}
    errors: List[str] = []
    for name in os.listdir(root):
        if not name.lower().endswith(".txt"):
            continue
        full = os.path.join(root, name)
        if not os.path.isfile(full):
            continue
        stem = os.path.splitext(name)[0]
        m = _FOLDER_TXT_NUM.match(stem)
        if not m:
            continue
        num = int(m.group(1))
        if num in numbered:
            errors.append(f"重复天数编号 {num}：{numbered[num]} 与 {name}")
        else:
            numbered[num] = name
    if errors:
        return [], errors
    if not numbered:
        return [], ["文件夹内没有带数字前缀的 TXT（如 4.txt、11女二退.txt）；无数字前缀的文件已忽略"]
    ordered = [numbered[k] for k in sorted(numbered.keys())]
    return ordered, []

def folder_txt_abs_path(folder_path: str, rel_name: str) -> str:
    return os.path.join(os.path.abspath(folder_path), rel_name)


def read_folder_txt_utf8(folder_path: str, rel_name: str) -> Tuple[str, str]:
    """返回 (文本内容, 错误信息)。"""
    path = folder_txt_abs_path(folder_path, rel_name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), ""
    except UnicodeDecodeError:
        return "", "TXT 解码失败，请另存为 UTF-8 编码"
    except OSError as exc:
        return "", f"读取 TXT 失败：{exc}"


def is_folder_job(job: Any) -> bool:
    return str(getattr(job, "source_kind", "file") or "file").strip() == "folder"


def folder_day_total(job: Any) -> int:
    files = getattr(job, "folder_files", None) or []
    return len(files)


def can_advance_folder_day(job: Any) -> bool:
    if not is_folder_job(job):
        return False
    total = folder_day_total(job)
    if total <= 0:
        return False
    idx = int(getattr(job, "folder_day_index", 0) or 0)
    return idx < total - 1


def _job_step_count(job: Any) -> int:
    items = getattr(job, "items", None)
    if items is not None:
        return len(items)
    rows = getattr(job, "rows", None)
    if rows is not None:
        return len(rows)
    return 0


def is_folder_job_fully_completed(job: Any) -> bool:
    """文件夹任务：已到最后一天且当天条目已全部发完。"""
    if not is_folder_job(job):
        return False
    total = folder_day_total(job)
    if total <= 0:
        return False
    idx = int(getattr(job, "folder_day_index", 0) or 0)
    if idx < total - 1:
        return False
    steps = _job_step_count(job)
    if steps <= 0:
        return False
    cursor = max(0, int(getattr(job, "cursor", 0) or 0))
    return cursor >= steps


def job_eligible_for_bulk_delete(job: Any) -> bool:
    """一键删除：单 TXT 任务；或文件夹任务且全部天数已发完。"""
    if not is_folder_job(job):
        return True
    return is_folder_job_fully_completed(job)


def taskmgr_job_file_label(job: Any) -> str:
    """任务卡片「文档：」行文案（位置不变，文件夹任务附加天数）。"""
    name = (getattr(job, "source_name", None) or "").strip() or "未命名"
    if is_folder_job(job):
        total = folder_day_total(job)
        if total > 0:
            idx = max(0, int(getattr(job, "folder_day_index", 0) or 0))
            return f"文档：{name}（第 {idx + 1}/{total} 天）"
    return f"文档：{name}"


def format_folder_advance_line(
    *,
    target_label: str,
    current_name: str,
    next_name: str,
    next_day_one_based: int,
    total_days: int,
) -> str:
    return f"· {target_label}：{current_name} → {next_name}（第 {next_day_one_based}/{total_days} 天）"
