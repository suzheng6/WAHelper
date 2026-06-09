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
    kind = str(getattr(job, "source_kind", "file") or "file").strip()
    if kind == "folder":
        return True
    # 兼容旧数据：有 folder_files 即视为文件夹任务
    files = getattr(job, "folder_files", None) or []
    path = str(getattr(job, "folder_path", "") or "").strip()
    return bool(files) and bool(path)


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


def bulk_delete_job_summary(jobs: List[Any]) -> tuple[int, int, int, List[Any], List[Any]]:
    """返回 (单TXT数, 已完成文件夹数, 保留文件夹数, 待删列表, 保留列表)。"""
    to_delete: List[Any] = []
    kept: List[Any] = []
    single_txt = 0
    folder_done = 0
    folder_kept = 0
    for j in jobs:
        if job_eligible_for_bulk_delete(j):
            to_delete.append(j)
            if is_folder_job(j):
                folder_done += 1
            else:
                single_txt += 1
        else:
            kept.append(j)
            if is_folder_job(j):
                folder_kept += 1
    return single_txt, folder_done, folder_kept, to_delete, kept


def format_bulk_delete_confirm_message(
    *,
    single_txt: int,
    folder_done: int,
    folder_kept: int,
    total_delete: int,
) -> str:
    parts: List[str] = [f"将删除 {total_delete} 个任务："]
    if single_txt > 0:
        parts.append(f"· {single_txt} 个单 TXT 文档任务")
    if folder_done > 0:
        parts.append(f"· {folder_done} 个已全部发完的文件夹任务")
    msg = "\n".join(parts)
    if folder_kept > 0:
        msg += f"\n\n保留 {folder_kept} 个进行中的文件夹任务（未发完所有天数）。"
    msg += "\n\n确定继续？"
    return msg


def schedule_kind_badge(job: Any) -> str:
    """任务类型短标签，用于群名后区分单 TXT 与文件夹任务。"""
    if is_folder_job(job):
        total = folder_day_total(job)
        if total > 0:
            idx = max(0, int(getattr(job, "folder_day_index", 0) or 0))
            return f" [文件夹·第{idx + 1}/{total}天]"
        return " [文件夹]"
    return " [单TXT]"


def _job_has_chat_entry(job: Any, entry_id: str) -> bool:
    eid = str(entry_id or "").strip()
    if not eid:
        return False
    ids = {str(x).strip() for x in (getattr(job, "chat_entry_ids", None) or []) if str(x).strip()}
    return eid in ids


def entry_schedule_kind_hint(jobs: List[Any], entry_id: str) -> str:
    """定时任务页：某通讯录群若有对应任务，返回类型标签；无任务则返回空串。"""
    for j in jobs:
        if _job_has_chat_entry(j, entry_id):
            return schedule_kind_badge(j)
    return ""


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
