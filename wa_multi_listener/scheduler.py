"""文档式循环定时任务（TXT 账号+消息，随机间隔）。"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

from neonize.aioze.client import NewAClient

from config import (
    Account,
    AppConfig,
    SCHEDULE_FILE,
    ensure_dirs,
    load_config,
    resolve_job_chat_targets,
)
from logger_util import error, info, warning
from wa_jid import chat_matches_ref, parse_chat_ref_to_jid
from wa_send import send_text_to_chats


@dataclass
class DocMessage:
    account_id: str
    content: str
    is_reminder: bool = False
    reminder_note: str = ""
    """本条执行完后的等待分钟数；None 表示使用任务界面上的默认间隔。"""
    delay_after_minutes: Optional[float] = None


@dataclass
class ScheduledJob:
    id: str
    enabled: bool
    chat_ids: List[str]
    interval_min_minutes: float
    interval_max_minutes: float
    source_path: str
    source_name: str
    items: List[DocMessage] = field(default_factory=list)
    chat_entry_ids: List[str] = field(default_factory=list)
    cursor: int = 0
    state: str = "running"
    pause_reason: str = ""
    next_send_ts: float = 0.0
    remaining_seconds: float = 0.0
    last_send_ts: float = 0.0
    last_error: str = ""

    @staticmethod
    def new(
        chat_ids: List[str],
        min_minutes: float,
        max_minutes: float,
        source_path: str,
        items: List[DocMessage],
        chat_entry_ids: Optional[List[str]] = None,
    ) -> "ScheduledJob":
        source_name = os.path.basename(source_path) or "未命名文档"
        ce = [str(x) for x in (chat_entry_ids or []) if str(x).strip()]
        return ScheduledJob(
            id=uuid.uuid4().hex[:12],
            enabled=True,
            chat_ids=[str(x) for x in chat_ids],
            interval_min_minutes=float(min_minutes),
            interval_max_minutes=float(max_minutes),
            source_path=source_path,
            source_name=source_name,
            items=items,
            chat_entry_ids=ce,
            cursor=0,
            state="running",
            next_send_ts=time.time() + random.uniform(float(min_minutes), float(max_minutes)) * 60.0,
        )

    def current_item(self) -> Optional[DocMessage]:
        if not self.items or self.cursor < 0 or self.cursor >= len(self.items):
            return None
        return self.items[self.cursor]


def _job_to_dict(j: ScheduledJob) -> Dict:
    d = asdict(j)
    d["items"] = [asdict(x) for x in j.items]
    return d


def _doc_from_dict(x: Dict[str, Any]) -> Optional[DocMessage]:
    if not isinstance(x, dict):
        return None
    acc = str(x.get("account_id", "")).strip()
    txt = str(x.get("content", "")).strip()
    is_rem = bool(x.get("is_reminder", False))
    note = str(x.get("reminder_note", "") or "").strip()
    if is_rem:
        return DocMessage(account_id=acc, content=txt, is_reminder=True, reminder_note=note)
    if acc and txt:
        return DocMessage(account_id=acc, content=txt, is_reminder=False, reminder_note="")
    return None


def load_jobs() -> List[ScheduledJob]:
    ensure_dirs()
    if not os.path.isfile(SCHEDULE_FILE):
        return []
    try:
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    jobs: List[ScheduledJob] = []
    if not isinstance(raw, list):
        return jobs
    for row in raw:
        if not isinstance(row, dict) or "items" not in row:
            continue
        try:
            items: List[DocMessage] = []
            for x in row.get("items", []):
                dm = _doc_from_dict(x if isinstance(x, dict) else {})
                if dm is not None:
                    items.append(dm)
            if not items:
                continue
            min_m = float(row.get("interval_min_minutes", 5.0))
            max_m = float(row.get("interval_max_minutes", min_m))
            if min_m <= 0:
                min_m = 0.1
            if max_m < min_m:
                max_m = min_m
            chats_raw = row.get("chat_ids", [])
            chat_ids: List[str] = []
            if isinstance(chats_raw, list):
                chat_ids = [str(x) for x in chats_raw if str(x).strip()]
            ce_raw = row.get("chat_entry_ids")
            chat_entry_ids: List[str] = []
            if isinstance(ce_raw, list):
                chat_entry_ids = [str(x) for x in ce_raw if str(x).strip()]
            if not chat_ids and not chat_entry_ids:
                continue
            jobs.append(
                ScheduledJob(
                    id=str(row.get("id", uuid.uuid4().hex[:12])),
                    enabled=bool(row.get("enabled", True)),
                    chat_ids=chat_ids,
                    interval_min_minutes=min_m,
                    interval_max_minutes=max_m,
                    source_path=str(row.get("source_path", "")),
                    source_name=str(row.get("source_name", "")) or os.path.basename(str(row.get("source_path", ""))),
                    items=items,
                    chat_entry_ids=chat_entry_ids,
                    cursor=max(0, int(row.get("cursor", 0))),
                    state=str(row.get("state", "running")) if str(row.get("state", "running")) in ("running", "paused") else "running",
                    pause_reason=str(row.get("pause_reason", "")),
                    next_send_ts=float(row.get("next_send_ts", 0.0)),
                    remaining_seconds=max(0.0, float(row.get("remaining_seconds", 0.0))),
                    last_send_ts=float(row.get("last_send_ts", 0.0)),
                    last_error=str(row.get("last_error", "")),
                )
            )
        except (TypeError, ValueError, KeyError):
            continue
    return jobs


def save_jobs(jobs: List[ScheduledJob]) -> None:
    ensure_dirs()
    tmp = SCHEDULE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump([_job_to_dict(j) for j in jobs], f, ensure_ascii=False, indent=2)
    os.replace(tmp, SCHEDULE_FILE)


STARTUP_PAUSE_REASON = "程序启动后已自动暂停，请在定时任务中点「继续」后再运行。"


def pause_all_doc_jobs_on_startup(reason: str = STARTUP_PAUSE_REASON) -> int:
    jobs = load_jobs()
    now = time.time()
    cnt = 0
    for j in jobs:
        if not j.enabled or not j.items or j.state == "paused":
            continue
        remain = j.remaining_seconds
        if remain <= 0:
            if j.next_send_ts > 0:
                remain = max(1.0, j.next_send_ts - now)
            else:
                remain = _next_delay_seconds(j)
        j.remaining_seconds = _clamp_secs(j, remain)
        j.state = "paused"
        j.pause_reason = reason
        cnt += 1
    if cnt > 0:
        save_jobs(jobs)
        info(f"启动时已暂停 {cnt} 个定时任务（需手动点「继续」）。")
    return cnt


def _clamp_secs(job: ScheduledJob, secs: float) -> float:
    min_s = max(1.0, job.interval_min_minutes * 60.0)
    max_s = max(min_s, job.interval_max_minutes * 60.0)
    return min(max(secs, min_s), max_s)


def _next_delay_seconds(job: ScheduledJob) -> float:
    min_s = max(1.0, job.interval_min_minutes * 60.0)
    max_s = max(min_s, job.interval_max_minutes * 60.0)
    return random.uniform(min_s, max_s)


class ScheduleRunner:
    def __init__(self) -> None:
        self._running = False
        self._accounts: Dict[str, Account] = {}
        self._shared_clients: Optional[Dict[str, NewAClient]] = None
        self._account_locks: Dict[str, asyncio.Lock] = {}
        self._reminder_callback: Optional[Callable[[ScheduledJob, DocMessage, int], None]] = None

    def set_reminder_callback(self, fn: Optional[Callable[[ScheduledJob, DocMessage, int], None]]) -> None:
        self._reminder_callback = fn

    def bind_clients(self, clients: Dict[str, NewAClient]) -> None:
        self._shared_clients = dict(clients)
        self._account_locks = {k: asyncio.Lock() for k in self._shared_clients}

    def unbind_clients(self) -> None:
        self._shared_clients = None
        self._account_locks = {}

    def start(self, cfg: AppConfig) -> None:
        self.stop()
        self._accounts = {a.id: a for a in cfg.accounts}
        info("定时任务已就绪")

    def stop(self) -> None:
        self._running = False

    def pause_job(self, job_id: str, reason: str = "手动暂停") -> bool:
        jobs = load_jobs()
        now = time.time()
        changed = False
        for j in jobs:
            if j.id != job_id or not j.enabled or j.state == "paused":
                continue
            remain = j.remaining_seconds
            if remain <= 0:
                remain = max(1.0, j.next_send_ts - now) if j.next_send_ts > 0 else _next_delay_seconds(j)
            j.remaining_seconds = _clamp_secs(j, remain)
            j.state = "paused"
            j.pause_reason = reason
            changed = True
        if changed:
            save_jobs(jobs)
        return changed

    def resume_job(self, job_id: str) -> bool:
        jobs = load_jobs()
        now = time.time()
        changed = False
        for j in jobs:
            if j.id != job_id or not j.enabled or j.state != "paused":
                continue
            remain = j.remaining_seconds if j.remaining_seconds > 0 else _next_delay_seconds(j)
            j.next_send_ts = now + _clamp_secs(j, remain)
            j.remaining_seconds = 0.0
            j.pause_reason = ""
            j.state = "running"
            changed = True
        if changed:
            save_jobs(jobs)
        return changed

    def pause_jobs_by_chat(self, chat_key: str, reason: str, *, event_title: Optional[str] = None) -> int:
        jobs = load_jobs()
        cfg = load_config()
        now = time.time()
        cnt = 0
        try:
            ev_jid = parse_chat_ref_to_jid(chat_key) if "@" in chat_key else None
        except ValueError:
            ev_jid = None

        def job_has_chat(j: ScheduledJob) -> bool:
            targets = resolve_job_chat_targets(cfg, j)
            for t in targets:
                if ev_jid and chat_matches_ref(t, ev_jid, event_title=event_title):
                    return True
                if str(t).strip().lower() == str(chat_key).strip().lower():
                    return True
            for x in j.chat_ids:
                if str(x).strip().lower() == str(chat_key).strip().lower():
                    return True
            return False

        for j in jobs:
            if not j.enabled or not job_has_chat(j) or j.state == "paused":
                continue
            remain = j.remaining_seconds
            if remain <= 0:
                remain = max(1.0, j.next_send_ts - now) if j.next_send_ts > 0 else _next_delay_seconds(j)
            j.remaining_seconds = _clamp_secs(j, remain)
            j.state = "paused"
            j.pause_reason = reason
            cnt += 1
        if cnt > 0:
            save_jobs(jobs)
        return cnt

    def _emit_reminder(self, job: ScheduledJob, item: DocMessage, step: int) -> None:
        cb = self._reminder_callback
        if cb:
            try:
                cb(job, item, step)
            except Exception as exc:
                error(f"阶段提醒回调异常：{exc}")

    async def _async_main(self) -> None:
        while self._running:
            try:
                now = time.time()
                jobs = load_jobs()
                changed = False
                for j in jobs:
                    if not self._running or not j.enabled or not j.items or j.state == "paused":
                        continue
                    if j.next_send_ts <= 0:
                        j.next_send_ts = now + _next_delay_seconds(j)
                        changed = True
                        continue
                    if now < j.next_send_ts:
                        continue
                    item = j.current_item()
                    if item is None:
                        j.enabled = False
                        j.state = "paused"
                        j.pause_reason = "一轮发送完成，任务自动停止"
                        j.next_send_ts = 0.0
                        changed = True
                        continue
                    if item.is_reminder:
                        self._emit_reminder(j, item, j.cursor + 1)
                        j.cursor += 1
                        j.last_send_ts = time.time()
                        j.last_error = ""
                        if j.cursor >= len(j.items):
                            j.enabled = False
                            j.state = "paused"
                            j.pause_reason = "一轮发送完成，任务自动停止"
                            j.next_send_ts = 0.0
                        else:
                            j.next_send_ts = time.time() + _next_delay_seconds(j)
                        changed = True
                        continue
                    cfg = load_config()
                    targets = resolve_job_chat_targets(cfg, j)
                    if not targets:
                        j.last_error = "发送目标无效"
                        j.next_send_ts = time.time() + _next_delay_seconds(j)
                        changed = True
                        continue
                    ok = await self._send_one_to_many(targets, item.account_id, item.content)
                    if ok:
                        j.cursor += 1
                        j.last_send_ts = time.time()
                        j.last_error = ""
                        info(f"定时任务已发送：{j.id} 文档={j.source_name} 账号={item.account_id}")
                        if j.cursor >= len(j.items):
                            j.enabled = False
                            j.state = "paused"
                            j.pause_reason = "一轮发送完成，任务自动停止"
                            j.next_send_ts = 0.0
                            changed = True
                            continue
                    else:
                        j.last_error = "发送失败，已按下一轮间隔重试"
                    j.next_send_ts = time.time() + _next_delay_seconds(j)
                    changed = True
                if changed:
                    save_jobs(jobs)
                await asyncio.sleep(0.8)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                error(f"定时任务循环异常：{exc}")
                await asyncio.sleep(1.5)

    async def _send_one_to_many(self, chat_refs: List[str], account_id: str, content: str) -> bool:
        account_id = (account_id or "").strip()
        acc = self._accounts.get(account_id)
        if acc is None or not acc.enabled:
            warning(f"定时任务发送跳过：账号 {account_id} 不存在或未启用")
            return False
        shared = self._shared_clients
        if not shared:
            error("定时任务发送失败：尚未连接 WhatsApp")
            return False
        client = shared.get(account_id)
        if client is None:
            warning(f"定时任务发送跳过：账号 {account_id} 未连接")
            return False
        lock = self._account_locks.get(account_id) or asyncio.Lock()
        async with lock:
            try:
                return await send_text_to_chats(client, chat_refs, content)
            except Exception as exc:
                error(f"定时任务发送异常：账号={account_id} 错误={exc}")
                return False
