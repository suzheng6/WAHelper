"""定时任务：多文档轮播，支持按原文账号批量改实际发送账号，多任务并行。"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
import uuid
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from neonize.aioze.client import NewAClient

from config import (
    Account,
    AppConfig,
    SCHEDULE2_FILE,
    ensure_dirs,
    format_job_targets_label,
    load_config,
    record_address_book_last_schedule,
    resolve_job_chat_targets,
)
from logger_util import error, info, warning
from schedule_account import (
    address_owner_map,
    resolve_send_account_id,
    row_needs_per_group_owner,
)
from scheduler import DocMessage
from wa_jid import chat_matches_keys, jid_from_chat_key, keys_for_chat_ref
from wa_send import send_text_to_chats
from watch_read_tracker import WaWatchReadTracker, mark_watch_read_before_send


@dataclass
class Schedule2Row:
    id: str
    original_account_id: str
    send_as_account_id: str
    content: str
    is_reminder: bool = False
    reminder_note: str = ""
    """本条执行完后的等待分钟数；None 表示使用任务界面默认间隔（随机）。"""
    delay_after_minutes: Optional[float] = None


@dataclass
class Schedule2Job:
    id: str
    enabled: bool
    chat_ids: List[str]
    interval_min_minutes: float
    interval_max_minutes: float
    source_path: str
    source_name: str
    rows: List[Schedule2Row] = field(default_factory=list)
    chat_entry_ids: List[str] = field(default_factory=list)
    """通讯录条目 id → 该群「主号」实际发送账号（多群任务自动填充）。"""
    owner_by_entry_id: Dict[str, str] = field(default_factory=dict)
    cursor: int = 0
    state: str = "running"
    pause_reason: str = ""
    next_send_ts: float = 0.0
    remaining_seconds: float = 0.0
    last_send_ts: float = 0.0
    last_error: str = ""
    source_kind: str = "file"
    folder_path: str = ""
    folder_files: List[str] = field(default_factory=list)
    folder_day_index: int = 0

    @staticmethod
    def new(
        chat_ids: List[str],
        min_minutes: float,
        max_minutes: float,
        source_path: str,
        rows: List[Schedule2Row],
        chat_entry_ids: Optional[List[str]] = None,
    ) -> "Schedule2Job":
        source_name = os.path.basename(source_path) or "未命名文档"
        ce = [str(x) for x in (chat_entry_ids or []) if str(x).strip()]
        return Schedule2Job(
            id=uuid.uuid4().hex[:12],
            enabled=True,
            chat_ids=[str(x) for x in chat_ids],
            interval_min_minutes=float(min_minutes),
            interval_max_minutes=float(max_minutes),
            source_path=source_path,
            source_name=source_name,
            rows=rows,
            chat_entry_ids=ce,
            cursor=0,
            state="paused",
            pause_reason=NEW_JOB_PAUSE_REASON_S2,
            next_send_ts=0.0,
            remaining_seconds=0.0,
        )

    def row_count(self) -> int:
        return len(self.rows)


def _job_to_dict(j: Schedule2Job) -> Dict[str, Any]:
    d = asdict(j)
    d["rows"] = [asdict(r) for r in j.rows]
    return d


def _row_from_dict(d: Dict[str, Any]) -> Optional[Schedule2Row]:
    if not isinstance(d, dict):
        return None
    rid = str(d.get("id", "")).strip() or uuid.uuid4().hex[:12]
    return Schedule2Row(
        id=rid,
        original_account_id=str(d.get("original_account_id", "") or ""),
        send_as_account_id=str(d.get("send_as_account_id", "") or "").strip()
        or str(d.get("original_account_id", "") or ""),
        content=str(d.get("content", "") or ""),
        is_reminder=bool(d.get("is_reminder", False)),
        reminder_note=str(d.get("reminder_note", "") or ""),
        delay_after_minutes=_delay_minutes_from_raw(d.get("delay_after_minutes")),
    )


def _delay_minutes_from_raw(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _job_from_dict(row: Dict[str, Any]) -> Optional[Schedule2Job]:
    if not isinstance(row, dict):
        return None
    rows: List[Schedule2Row] = []
    for x in row.get("rows", []):
        r = _row_from_dict(x if isinstance(x, dict) else {})
        if r is not None:
            rows.append(r)
    if not rows:
        return None
    jid = str(row.get("id", "")).strip() or uuid.uuid4().hex[:12]
    ce = [str(x) for x in row.get("chat_entry_ids", []) if str(x).strip()]
    cid_raw = row.get("chat_ids", [])
    cids: List[str] = []
    if isinstance(cid_raw, list):
        cids = [str(x) for x in cid_raw if str(x).strip()]
    if not cids and not ce:
        return None
    min_m = float(row.get("interval_min_minutes", 5.0))
    max_m = float(row.get("interval_max_minutes", min_m))
    if min_m <= 0:
        min_m = 0.1
    if max_m < min_m:
        max_m = min_m
    return Schedule2Job(
        id=jid,
        enabled=bool(row.get("enabled", True)),
        chat_ids=cids,
        interval_min_minutes=min_m,
        interval_max_minutes=max_m,
        source_path=str(row.get("source_path", "")),
        source_name=str(row.get("source_name", "")) or os.path.basename(str(row.get("source_path", ""))),
        rows=rows,
        chat_entry_ids=ce,
        owner_by_entry_id={
            str(k): str(v)
            for k, v in (row.get("owner_by_entry_id") or {}).items()
            if str(k).strip() and str(v).strip()
        },
        cursor=max(0, int(row.get("cursor", 0))),
        state=str(row.get("state", "running")) if str(row.get("state", "running")) in ("running", "paused") else "paused",
        pause_reason=str(row.get("pause_reason", "")),
        next_send_ts=float(row.get("next_send_ts", 0.0)),
        remaining_seconds=max(0.0, float(row.get("remaining_seconds", 0.0))),
        last_send_ts=float(row.get("last_send_ts", 0.0)),
        last_error=str(row.get("last_error", "")),
        source_kind=str(row.get("source_kind", "file") or "file"),
        folder_path=str(row.get("folder_path", "") or ""),
        folder_files=[str(x) for x in (row.get("folder_files") or []) if str(x).strip()],
        folder_day_index=max(0, int(row.get("folder_day_index", 0) or 0)),
    )


def load_schedule2_jobs() -> List[Schedule2Job]:
    ensure_dirs()
    if not os.path.isfile(SCHEDULE2_FILE):
        return []
    try:
        with open(SCHEDULE2_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    jobs: List[Schedule2Job] = []
    if isinstance(raw, list):
        for row in raw:
            j = _job_from_dict(row if isinstance(row, dict) else {})
            if j is not None:
                jobs.append(j)
    return jobs


def save_schedule2_jobs(jobs: List[Schedule2Job]) -> None:
    from json_atomic import atomic_write_json

    ensure_dirs()
    atomic_write_json(SCHEDULE2_FILE, [_job_to_dict(j) for j in jobs])


def save_schedule2_jobs_patch(updated: List[Schedule2Job]) -> None:
    """仅合并写入有变动的任务，避免全量覆盖与 UI/后台竞态。"""
    if not updated:
        return
    fresh = load_schedule2_jobs()
    by_id = {j.id: j for j in fresh}
    order = [j.id for j in fresh]
    for j in updated:
        by_id[j.id] = j
        if j.id not in order:
            order.append(j.id)
    save_schedule2_jobs([by_id[jid] for jid in order])


def _doc_items_to_s2_rows(items: List[DocMessage]) -> List[Schedule2Row]:
    from schedule_account import mark_row_primary_auto

    rows: List[Schedule2Row] = []
    for it in items:
        orig, send = mark_row_primary_auto(it.account_id, it.account_id)
        rows.append(
            Schedule2Row(
                id=uuid.uuid4().hex[:12],
                original_account_id=orig,
                send_as_account_id=send,
                content=it.content,
                is_reminder=it.is_reminder,
                reminder_note=it.reminder_note,
                delay_after_minutes=it.delay_after_minutes,
            )
        )
    return rows


def _reload_folder_day_items_s2(job: Schedule2Job, cfg: AppConfig, rel_name: str) -> tuple[bool, str]:
    from schedule_folder import read_folder_txt_utf8
    from schedule_txt_import import import_doc_items

    text, err = read_folder_txt_utf8(job.folder_path, rel_name)
    if err:
        return False, err
    items, _errs = import_doc_items(text, valid_accounts=None)
    if not items:
        return False, "TXT 无有效条目"
    rows = _doc_items_to_s2_rows(items)
    if row_needs_per_group_owner(rows):
        emap = {e.id: e for e in cfg.address_book}
        for eid in job.chat_entry_ids:
            ent = emap.get(eid)
            if not ent or not (ent.owner_account_id or "").strip():
                return False, "未在通讯录设置主号/归属账号"
    job.rows = rows
    abs_path = os.path.join(os.path.abspath(job.folder_path), rel_name)
    job.source_path = abs_path
    job.source_name = os.path.basename(abs_path) or rel_name
    return True, ""


def advance_schedule2_folder_day(job: Schedule2Job, cfg: AppConfig) -> tuple[bool, str]:
    from schedule_folder import can_advance_folder_day

    if not can_advance_folder_day(job):
        return False, "已是最后一天或不是文件夹任务"
    next_index = int(job.folder_day_index) + 1
    rel = job.folder_files[next_index]
    ok, err = _reload_folder_day_items_s2(job, cfg, rel)
    if not ok:
        return False, err
    job.folder_day_index = next_index
    job.cursor = 0
    job.enabled = True
    job.state = "running"
    job.pause_reason = ""
    from schedule_folder import random_folder_advance_delay_seconds

    job.next_send_ts = time.time() + random_folder_advance_delay_seconds()
    job.remaining_seconds = 0.0
    job.last_error = ""
    return True, ""


STARTUP_PAUSE_REASON_S2 = "程序启动后已自动暂停，请在「任务管理」点「一键开始全部任务」。"
SHUTDOWN_PAUSE_REASON_S2 = "程序已关闭，再次打开后请在「任务管理」点「一键开始全部任务」。"
NEW_JOB_PAUSE_REASON_S2 = "新建任务，请点「继续」后开始"
STAGE_REMINDER_PAUSE_REASON_S2 = "定时任务阶段提醒，已自动暂停"

RESUME_RANDOM_DELAY_MIN_SEC = 30.0
RESUME_RANDOM_DELAY_MAX_SEC = 60.0


def _random_resume_delay_seconds() -> float:
    return random.uniform(RESUME_RANDOM_DELAY_MIN_SEC, RESUME_RANDOM_DELAY_MAX_SEC)


def _first_send_row_index(job: Schedule2Job) -> Optional[int]:
    for i, row in enumerate(job.rows):
        if not row.is_reminder:
            return i
    return None


def _resume_delay_seconds(job: Schedule2Job) -> float:
    reason = (job.pause_reason or "").strip()
    if reason == STAGE_REMINDER_PAUSE_REASON_S2:
        return _random_resume_delay_seconds()
    if job.last_send_ts <= 0:
        fi = _first_send_row_index(job)
        if fi is not None and job.cursor == fi:
            return _random_resume_delay_seconds()
    remain = job.remaining_seconds if job.remaining_seconds > 0 else 0.0
    if remain <= 0:
        ri = _pending_delay_row_index(job)
        remain = _next_delay_seconds(job, ri if ri >= 0 else None)
    return _clamp_secs(job, remain, _pending_delay_row_index(job))


def schedule2_job_is_running(j: Schedule2Job) -> bool:
    return bool(j.enabled) and j.state == "running"


def schedule2_job_status_label(j: Schedule2Job) -> str:
    if not j.enabled:
        reason = (j.pause_reason or "").strip()
        if reason:
            return f"已停止 · {reason[:24]}"
        return "已停止"
    if j.state == "paused":
        reason = (j.pause_reason or "已暂停").strip()
        return f"暂停 · {reason[:28]}"
    return "运行中"


def schedule2_job_step_label(j: Schedule2Job) -> str:
    total = j.row_count()
    if total <= 0:
        return "无发送步骤"
    if j.cursor >= total:
        return f"已完成（{total}/{total} 步）"
    step_no = j.cursor + 1
    row = j.rows[j.cursor]
    if row.is_reminder:
        return f"第 {step_no}/{total} 步 · 阶段提醒"
    preview = (row.content or "").replace("\n", " ").strip()[:28]
    return f"第 {step_no}/{total} 步 · {preview or '…'}"


def schedule2_job_target_remarks(cfg: AppConfig, j: Schedule2Job) -> str:
    emap = {e.id: e for e in cfg.address_book}
    names = [(emap[eid].remark or eid).strip() for eid in j.chat_entry_ids if eid in emap]
    if names:
        if len(names) <= 4:
            return "、".join(names)
        return "、".join(names[:4]) + f" 等{len(names)}群"
    return format_job_targets_label(cfg, j) or "未设群"


def pause_all_schedule2_jobs_on_startup(reason: str = STARTUP_PAUSE_REASON_S2) -> int:
    jobs = load_schedule2_jobs()
    now = time.time()
    cnt = 0
    for j in jobs:
        if not j.enabled or not j.rows or j.state == "paused":
            continue
        remain = j.remaining_seconds
        if remain <= 0:
            remain = max(1.0, j.next_send_ts - now) if j.next_send_ts > 0 else _next_delay_seconds(j)
        j.remaining_seconds = _clamp_secs(j, remain, _pending_delay_row_index(j))
        j.state = "paused"
        j.pause_reason = reason
        cnt += 1
    if cnt > 0:
        save_schedule2_jobs(jobs)
        info(f"启动时已暂停 {cnt} 个定时任务（需手动点「继续」）。")
    return cnt


def _job_default_delay_seconds(job: Schedule2Job) -> float:
    min_s = max(1.0, job.interval_min_minutes * 60.0)
    max_s = max(min_s, job.interval_max_minutes * 60.0)
    return random.uniform(min_s, max_s)


def _delay_seconds_after_row(job: Schedule2Job, completed_row_index: int) -> float:
    """本条（已执行）指定的间隔；无 TXT 间隔时用界面默认随机间隔。"""
    if not job.rows or completed_row_index < 0 or completed_row_index >= len(job.rows):
        return _job_default_delay_seconds(job)
    row = job.rows[completed_row_index]
    if row.delay_after_minutes is not None:
        return max(0.0, float(row.delay_after_minutes)) * 60.0
    return _job_default_delay_seconds(job)


def _pending_delay_row_index(job: Schedule2Job) -> int:
    """下一动作前的等待取自上一条已执行条目；cursor=0 时尚未发过。"""
    return job.cursor - 1 if job.cursor > 0 else -1


def _delay_bounds_seconds(job: Schedule2Job, row_index: int) -> tuple[float, float]:
    if row_index < 0 or row_index >= len(job.rows):
        lo = max(1.0, job.interval_min_minutes * 60.0)
        hi = max(lo, job.interval_max_minutes * 60.0)
        return lo, hi
    if job.rows[row_index].delay_after_minutes is not None:
        s = max(0.0, float(job.rows[row_index].delay_after_minutes)) * 60.0
        return s, s
    lo = max(1.0, job.interval_min_minutes * 60.0)
    hi = max(lo, job.interval_max_minutes * 60.0)
    return lo, hi


def _clamp_secs(job: Schedule2Job, secs: float, row_index: int = -1) -> float:
    lo, hi = _delay_bounds_seconds(job, row_index)
    return min(max(secs, lo), hi)


def _next_delay_seconds(job: Schedule2Job, completed_row_index: Optional[int] = None) -> float:
    if completed_row_index is None:
        ri = _pending_delay_row_index(job)
        if ri < 0:
            return _job_default_delay_seconds(job)
        return _delay_seconds_after_row(job, ri)
    return _delay_seconds_after_row(job, completed_row_index)


def job_interval_mode_label(job: Schedule2Job) -> str:
    n_txt = sum(1 for r in job.rows if r.delay_after_minutes is not None)
    if n_txt >= len(job.rows) and job.rows:
        return "TXT 每条间隔"
    if n_txt > 0:
        return f"TXT+默认（{n_txt}/{len(job.rows)} 条有间隔=）"
    return f"默认 {job.interval_min_minutes:g}-{job.interval_max_minutes:g} 分"


def _job_targets(cfg: AppConfig, j: Schedule2Job) -> List[str]:
    fake = SimpleNamespace(chat_entry_ids=j.chat_entry_ids, chat_ids=j.chat_ids)
    return resolve_job_chat_targets(cfg, fake)


def _targets_share_chat(cfg: AppConfig, targets_a: List[str], targets_b: List[str]) -> bool:
    for a in targets_a:
        keys_a = keys_for_chat_ref(a)
        if not keys_a:
            continue
        for b in targets_b:
            if keys_a & keys_for_chat_ref(b):
                return True
    return False


def _job_has_chat(j: Schedule2Job, cfg: AppConfig, chat_key: str, *, event_title: Optional[str] = None) -> bool:
    ev_jid = jid_from_chat_key(chat_key)
    if ev_jid is None:
        return False
    job_targets = _job_targets(cfg, j)
    for t in job_targets:
        if chat_matches_keys(keys_for_chat_ref(t), ev_jid):
            return True
    for x in j.chat_ids:
        if chat_matches_keys(keys_for_chat_ref(str(x)), ev_jid):
            return True
    if event_title:
        for t in job_targets:
            if event_title.strip() and t.strip().lower() == event_title.strip().lower():
                return True
    return False


def pause_jobs_for_targets(
    targets: List[str], reason: str, *, except_job_id: Optional[str] = None
) -> int:
    """暂停所有发送到相同群目标的定时任务（含当前任务，阶段提醒后需手动点「继续」）。"""
    if not targets:
        return 0
    jobs = load_schedule2_jobs()
    cfg = load_config()
    now = time.time()
    cnt = 0
    touched: List[Schedule2Job] = []
    for j in jobs:
        if except_job_id and j.id == except_job_id:
            continue
        if not j.enabled or j.state == "paused":
            continue
        if not _targets_share_chat(cfg, targets, _job_targets(cfg, j)):
            continue
        remain = j.remaining_seconds
        if remain <= 0:
            remain = max(1.0, j.next_send_ts - now) if j.next_send_ts > 0 else _next_delay_seconds(j)
        j.remaining_seconds = _clamp_secs(j, remain, _pending_delay_row_index(j))
        j.state = "paused"
        j.pause_reason = reason
        touched.append(j)
        cnt += 1
    if touched:
        save_schedule2_jobs_patch(touched)
    return cnt


class Schedule2Runner:
    def __init__(self) -> None:
        self._running = False
        self._accounts: Dict[str, Account] = {}
        self._shared_clients: Optional[Dict[str, NewAClient]] = None
        self._account_locks: Dict[str, asyncio.Lock] = {}
        self._reminder_callback: Optional[Callable[..., None]] = None
        self._read_tracker: Optional[WaWatchReadTracker] = None

    def bind_read_tracker(self, tracker: Optional[WaWatchReadTracker]) -> None:
        self._read_tracker = tracker

    def set_reminder_callback(self, fn: Optional[Callable[..., None]]) -> None:
        self._reminder_callback = fn

    def refresh_accounts(self, cfg: Optional[AppConfig] = None) -> None:
        """从最新 config 刷新账号表（新登录/新添加账号后必须调用）。"""
        if cfg is None:
            cfg = load_config()
        self._accounts = {a.id: a for a in cfg.accounts}

    def bind_clients(self, clients: Dict[str, NewAClient]) -> None:
        self._shared_clients = dict(clients)
        self._account_locks = {k: asyncio.Lock() for k in self._shared_clients}
        self.refresh_accounts()

    def unbind_clients(self) -> None:
        self._shared_clients = None
        self._account_locks = {}

    async def _send_row_to_job_targets(self, job: Schedule2Job, row: Schedule2Row, cfg: AppConfig) -> bool:
        """按群发送：主号条目使用通讯录当前选择的归属账号。"""
        emap = {e.id: e for e in cfg.address_book}
        owners = address_owner_map(cfg)
        entry_ids = [str(eid) for eid in job.chat_entry_ids if str(eid).strip()]
        if not entry_ids:
            fake = SimpleNamespace(chat_entry_ids=[], chat_ids=job.chat_ids)
            targets = resolve_job_chat_targets(cfg, fake)
            if not targets:
                return False
            acc = resolve_send_account_id(row, "", owners, self._accounts)
            return await self._send_one_to_many(targets, acc, row.content)

        if row_needs_per_group_owner([row]):
            for eid in entry_ids:
                if not owners.get(eid):
                    ent = emap.get(eid)
                    name = (ent.remark if ent else eid) or eid
                    warning(f"定时任务「{job.source_name}」：群「{name}」未在通讯录选择主号/归属账号，跳过本轮")
                    return False
        all_ok = True
        any_ok = False
        for eid in entry_ids:
            ent = emap.get(eid)
            if not ent or not (ent.chat_ref or "").strip():
                all_ok = False
                continue
            acc_send = resolve_send_account_id(row, eid, owners, self._accounts)
            if not acc_send:
                warning(f"定时任务：群「{ent.remark}」未解析到发送账号（原文={row.original_account_id}）")
                all_ok = False
                continue
            ok = await self._send_one_to_many([ent.chat_ref.strip()], acc_send, row.content)
            any_ok = any_ok or ok
            if not ok:
                all_ok = False
        return all_ok and any_ok

    def start(self, cfg: AppConfig) -> None:
        self.stop()
        self.refresh_accounts(cfg)
        info("定时任务已就绪（多任务并行）")

    def stop(self) -> None:
        self._running = False

    def pause_job(self, job_id: str, reason: str = "手动暂停") -> bool:
        jobs = load_schedule2_jobs()
        now = time.time()
        touched: Optional[Schedule2Job] = None
        for j in jobs:
            if j.id != job_id or not j.enabled or j.state == "paused":
                continue
            remain = j.remaining_seconds
            if remain <= 0:
                remain = max(1.0, j.next_send_ts - now) if j.next_send_ts > 0 else _next_delay_seconds(j)
            j.remaining_seconds = _clamp_secs(j, remain, _pending_delay_row_index(j))
            j.state = "paused"
            j.pause_reason = reason
            touched = j
            break
        if touched is not None:
            save_schedule2_jobs_patch([touched])
        return touched is not None

    def resume_job(self, job_id: str) -> bool:
        jobs = load_schedule2_jobs()
        now = time.time()
        touched: Optional[Schedule2Job] = None
        changed = False
        for j in jobs:
            if j.id != job_id:
                continue
            touched = j
            if not j.enabled:
                j.enabled = True
                changed = True
            if j.row_count() > 0 and j.cursor >= j.row_count():
                j.cursor = 0
                changed = True
            if j.state != "paused":
                break
            delay = _resume_delay_seconds(j)
            j.next_send_ts = now + delay
            j.remaining_seconds = 0.0
            j.pause_reason = ""
            j.state = "running"
            changed = True
            break
        if touched is not None and changed:
            save_schedule2_jobs_patch([touched])
        return changed

    def _schedule_job_resume(self, j: Schedule2Job, now: float) -> None:
        if j.cursor >= j.row_count() and j.row_count() > 0:
            j.cursor = 0
        delay = _resume_delay_seconds(j)
        j.next_send_ts = now + delay
        j.remaining_seconds = 0.0
        j.pause_reason = ""
        j.state = "running"

    def resume_all_jobs(self) -> int:
        """恢复暂停/已停止任务；并修复异常退出后仍为 running 但未调度的任务。"""
        jobs = load_schedule2_jobs()
        now = time.time()
        n = 0
        for j in jobs:
            if j.row_count() <= 0:
                continue
            if not j.enabled:
                j.enabled = True
            if j.state == "paused":
                self._schedule_job_resume(j, now)
                n += 1
                continue
            if j.state == "running":
                # 强关进程时可能未写入 paused，界面仍显示运行中但一键开始会跳过
                if j.next_send_ts <= 0 or j.next_send_ts <= now:
                    self._schedule_job_resume(j, now)
                    n += 1
        if n > 0:
            save_schedule2_jobs(jobs)
        if n > 0 and not self._running:
            warning(
                "任务状态已保存为运行中，但后台调度线程尚未就绪；"
                "请等待 WhatsApp 账号连接成功，或点「保存并重载服务」。"
            )
        return n

    def pause_by_chat(self, chat_key: str, reason: str, *, event_title: Optional[str] = None) -> int:
        jobs = load_schedule2_jobs()
        cfg = load_config()
        now = time.time()
        cnt = 0
        touched: List[Schedule2Job] = []
        for j in jobs:
            if not _job_has_chat(j, cfg, chat_key, event_title=event_title):
                continue
            changed = False
            if j.state != "paused":
                # 运行中任务按剩余时间进入暂停；已停止任务仅更新暂停原因与卡片颜色
                if j.enabled:
                    remain = j.remaining_seconds
                    if remain <= 0:
                        remain = max(1.0, j.next_send_ts - now) if j.next_send_ts > 0 else _next_delay_seconds(j)
                    j.remaining_seconds = _clamp_secs(j, remain, _pending_delay_row_index(j))
                j.state = "paused"
                changed = True
            if j.pause_reason != reason:
                j.pause_reason = reason
                changed = True
            if changed:
                touched.append(j)
                cnt += 1
        if touched:
            save_schedule2_jobs_patch(touched)
        return cnt

    def _emit_reminder(self, job: Schedule2Job, step: int, note: str, *, paused_count: int = 0) -> None:
        cb = self._reminder_callback
        if not cb:
            return
        try:
            cb(job, step, note, paused_count)
        except TypeError:
            try:
                cb(job.source_name, step, note, paused_count)
            except TypeError:
                try:
                    cb(job.source_name, step, note)
                except Exception as exc:
                    error(f"阶段提醒回调异常：{exc}")
            except Exception as exc:
                error(f"阶段提醒回调异常：{exc}")
        except Exception as exc:
            error(f"阶段提醒回调异常：{exc}")

    async def _async_main(self) -> None:
        while self._running:
            try:
                now = time.time()
                jobs = load_schedule2_jobs()
                changed = False
                for j in jobs:
                    if not self._running:
                        break
                    if not j.enabled or not j.rows or j.state == "paused":
                        continue
                    if j.next_send_ts <= 0:
                        j.next_send_ts = now + _random_resume_delay_seconds()
                        changed = True
                        continue
                    if now < j.next_send_ts:
                        continue
                    if j.cursor < 0 or j.cursor >= len(j.rows):
                        j.enabled = False
                        j.state = "paused"
                        j.pause_reason = "一轮发送完成，任务自动停止"
                        j.next_send_ts = 0.0
                        changed = True
                        continue
                    row = j.rows[j.cursor]
                    done_idx = j.cursor
                    if row.is_reminder:
                        step = j.cursor + 1
                        note = (row.reminder_note or "").strip()
                        j.cursor += 1
                        j.last_send_ts = time.time()
                        j.last_error = ""
                        if j.cursor >= len(j.rows):
                            j.enabled = False
                            j.state = "paused"
                            j.pause_reason = "一轮发送完成，任务自动停止"
                            j.next_send_ts = 0.0
                            j.remaining_seconds = 0.0
                        else:
                            remain = (
                                max(1.0, j.next_send_ts - now)
                                if j.next_send_ts > now
                                else _next_delay_seconds(j, done_idx)
                            )
                            j.remaining_seconds = _clamp_secs(
                                j, remain, _pending_delay_row_index(j)
                            )
                            j.state = "paused"
                            j.pause_reason = STAGE_REMINDER_PAUSE_REASON_S2
                            j.next_send_ts = 0.0
                        changed = True
                        cfg_rem = load_config()
                        targets = _job_targets(cfg_rem, j)
                        n = pause_jobs_for_targets(
                            targets,
                            STAGE_REMINDER_PAUSE_REASON_S2,
                        )
                        if n > 0:
                            info(f"阶段提醒：已暂停 {n} 个相关定时任务")
                        grp = format_job_targets_label(cfg_rem, j)
                        info(f"定时任务阶段提醒：群={grp} 文档={j.source_name} 步={step}")
                        self._emit_reminder(j, step, note, paused_count=n)
                        continue
                    cfg = load_config()
                    if not j.chat_entry_ids and not j.chat_ids:
                        j.last_error = "发送目标无效"
                        j.next_send_ts = time.time() + _next_delay_seconds(j, done_idx)
                        changed = True
                        continue
                    ok = await self._send_row_to_job_targets(j, row, cfg)
                    if ok:
                        j.cursor += 1
                        j.last_send_ts = time.time()
                        j.last_error = ""
                        record_address_book_last_schedule(j.chat_entry_ids, j.source_name)
                        owners_now = address_owner_map(cfg)
                        acc_hint = (
                            "主号按群→" + "、".join(f"{owners_now.get(e, '?')}" for e in j.chat_entry_ids[:5])
                            if row_needs_per_group_owner([row])
                            else (row.send_as_account_id or row.original_account_id)
                        )
                        info(
                            f"定时任务已发送：{j.source_name} 发送方={acc_hint} 原文={row.original_account_id}"
                        )
                        if j.cursor >= len(j.rows):
                            j.enabled = False
                            j.state = "paused"
                            j.pause_reason = "一轮发送完成，任务自动停止"
                            j.next_send_ts = 0.0
                    else:
                        j.last_error = "发送失败，已按下一轮间隔重试"
                    if j.next_send_ts != 0.0:
                        j.next_send_ts = time.time() + _next_delay_seconds(j, done_idx)
                    changed = True
                if changed:
                    save_schedule2_jobs(jobs)
                await asyncio.sleep(0.8)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                error(f"定时任务循环异常：{exc}")
                await asyncio.sleep(1.5)

    async def _send_one_to_many(self, chat_refs: List[str], account_id: str, content: str) -> bool:
        account_id = (account_id or "").strip()
        self.refresh_accounts()
        acc = self._accounts.get(account_id)
        if acc is None:
            warning(
                f"定时任务发送跳过：账号「{account_id}」不在账号列表"
                "（请先在「账号管理」添加该简称）"
            )
            return False
        if not acc.enabled:
            warning(f"定时任务发送跳过：账号「{account_id}」已取消勾选启用")
            return False
        shared = self._shared_clients
        if not shared:
            error("定时任务发送失败：尚未连接 WhatsApp")
            return False
        client = shared.get(account_id)
        if client is None:
            warning(f"定时任务发送跳过：账号「{account_id}」未在线（请先扫码登录并保存重载）")
            return False
        lock = self._account_locks.get(account_id) or asyncio.Lock()
        async with lock:
            try:
                ok = False
                for cref in chat_refs:
                    await mark_watch_read_before_send(client, account_id, cref, self._read_tracker)
                    if await send_text_to_chats(client, [cref], content):
                        ok = True
                return ok
            except Exception as exc:
                error(f"定时任务发送异常：账号={account_id} 错误={exc}")
                return False
