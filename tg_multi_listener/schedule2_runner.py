"""定时任务2：多文档任务轮播，支持「发送账号」覆盖 TXT 中的原文账号。"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
import uuid
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Union

from telethon import TelegramClient

from .compat_config import (
    Account,
    AppConfig,
    SCHEDULE2_FILE,
    chat_peer_ids_for_match,
    ensure_dirs,
    format_job_targets_label,
    load_config,
    resolve_job_chat_targets,
    telegram_chat_matches_address_ref,
)
from .logger_util import error, info, warning
from .scheduler import DocMessage, _resolve_entity_for_send, send_telegram_message_resilient
from .watch_read_tracker import TgWatchReadTracker, mark_watch_read_before_send


@dataclass
class Schedule2Row:
    id: str
    original_account_id: str
    send_as_account_id: str
    content: str
    is_reminder: bool = False
    reminder_note: str = ""


@dataclass
class Schedule2Job:
    id: str
    enabled: bool
    chat_ids: List[int]
    interval_min_minutes: float
    interval_max_minutes: float
    source_path: str
    source_name: str
    rows: List[Schedule2Row] = field(default_factory=list)
    chat_entry_ids: List[str] = field(default_factory=list)
    cursor: int = 0
    state: str = "running"  # running | paused
    pause_reason: str = ""
    next_send_ts: float = 0.0
    remaining_seconds: float = 0.0
    last_send_ts: float = 0.0
    last_error: str = ""

    @staticmethod
    def new(
        chat_ids: List[int],
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
            chat_ids=[int(x) for x in chat_ids],
            interval_min_minutes=float(min_minutes),
            interval_max_minutes=float(max_minutes),
            source_path=source_path,
            source_name=source_name,
            rows=rows,
            chat_entry_ids=ce,
            cursor=0,
            state="running",
            next_send_ts=time.time() + random.uniform(float(min_minutes), float(max_minutes)) * 60.0,
        )

    def row_count(self) -> int:
        return len(self.rows)

    def send_progress(self) -> tuple[int, int, int]:
        """返回 (待发消息总数, 已发消息数, 剩余待发消息数)；不含阶段提醒步。"""
        total = sum(1 for r in self.rows if not r.is_reminder)
        c = max(0, min(int(self.cursor), len(self.rows)))
        done = sum(1 for r in self.rows[:c] if not r.is_reminder)
        return total, done, max(0, total - done)


def _job_to_dict(j: Schedule2Job) -> Dict[str, Any]:
    d = asdict(j)
    d["rows"] = [asdict(r) for r in j.rows]
    return d


def _row_from_dict(d: Dict[str, Any]) -> Optional[Schedule2Row]:
    if not isinstance(d, dict):
        return None
    rid = str(d.get("id", "")).strip()
    if not rid:
        return None
    return Schedule2Row(
        id=rid,
        original_account_id=str(d.get("original_account_id", "") or ""),
        send_as_account_id=str(d.get("send_as_account_id", "") or "").strip()
        or str(d.get("original_account_id", "") or ""),
        content=str(d.get("content", "") or ""),
        is_reminder=bool(d.get("is_reminder", False)),
        reminder_note=str(d.get("reminder_note", "") or ""),
    )


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
    cid_raw = row.get("chat_ids")
    cids: List[int] = []
    if isinstance(cid_raw, list):
        try:
            cids = [int(x) for x in cid_raw]
        except (TypeError, ValueError):
            cids = []
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
        cursor=max(0, int(row.get("cursor", 0))),
        state=str(row.get("state", "running")) if str(row.get("state", "running")) in ("running", "paused") else "paused",
        pause_reason=str(row.get("pause_reason", "")),
        next_send_ts=float(row.get("next_send_ts", 0.0)),
        remaining_seconds=max(0.0, float(row.get("remaining_seconds", 0.0))),
        last_send_ts=float(row.get("last_send_ts", 0.0)),
        last_error=str(row.get("last_error", "")),
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
    if isinstance(raw, dict) and raw.get("rows"):
        j = _job_from_dict(raw)
        if j is not None:
            if not str(raw.get("id", "")).strip():
                j.id = uuid.uuid4().hex[:12]
            jobs.append(j)
    return jobs


def save_schedule2_jobs(jobs: List[Schedule2Job]) -> None:
    from json_atomic import atomic_write_json

    ensure_dirs()
    atomic_write_json(SCHEDULE2_FILE, [_job_to_dict(j) for j in jobs])


# 兼容旧代码引用（单任务时代）
def load_schedule2_state() -> Schedule2Job:
    jobs = load_schedule2_jobs()
    if jobs:
        return jobs[0]
    return Schedule2Job(
        id="",
        enabled=False,
        chat_ids=[],
        interval_min_minutes=5.0,
        interval_max_minutes=10.0,
        source_path="",
        source_name="",
    )


def save_schedule2_state(s: Schedule2Job) -> None:
    jobs = load_schedule2_jobs()
    if not s.id:
        if s.rows:
            jobs.append(s)
        save_schedule2_jobs(jobs)
        return
    found = False
    for i, j in enumerate(jobs):
        if j.id == s.id:
            jobs[i] = s
            found = True
            break
    if not found and s.rows:
        jobs.append(s)
    save_schedule2_jobs(jobs)


STARTUP_PAUSE_REASON_S2 = "程序启动后已自动暂停，请在「定时任务2」中点「继续」后再运行。"


def pause_all_schedule2_jobs_on_startup(reason: str = STARTUP_PAUSE_REASON_S2) -> int:
    """启动时暂停所有仍在运行中的定时任务2（与定时任务1一致，保留 enabled）。"""
    jobs = load_schedule2_jobs()
    now = time.time()
    cnt = 0
    for j in jobs:
        if not j.enabled or not j.rows:
            continue
        if j.state == "paused":
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
        save_schedule2_jobs(jobs)
        info(f"启动时已暂停 {cnt} 个定时任务2（需手动点「继续」）。")
    return cnt


# 旧名保留
pause_schedule2_on_startup = pause_all_schedule2_jobs_on_startup


def _clamp_secs(job: Schedule2Job, secs: float) -> float:
    min_s = max(1.0, job.interval_min_minutes * 60.0)
    max_s = max(min_s, job.interval_max_minutes * 60.0)
    return min(max(secs, min_s), max_s)


def _next_delay_seconds(job: Schedule2Job) -> float:
    min_s = max(1.0, job.interval_min_minutes * 60.0)
    max_s = max(min_s, job.interval_max_minutes * 60.0)
    return random.uniform(min_s, max_s)


def _job_has_chat(
    j: Schedule2Job,
    cfg: AppConfig,
    variants: set[int],
    *,
    peer_id: int,
    raw_chat_id: int,
    event_username: Optional[str],
    event_title: Optional[str],
) -> bool:
    fake = SimpleNamespace(chat_entry_ids=j.chat_entry_ids, chat_ids=j.chat_ids)
    targets = resolve_job_chat_targets(cfg, fake)
    nums: List[int] = []
    for t in targets:
        if isinstance(t, int):
            nums.append(int(t))
        else:
            try:
                nums.append(int(str(t).strip()))
            except ValueError:
                pass
    for n in nums + [int(x) for x in j.chat_ids]:
        if variants.intersection(chat_peer_ids_for_match(int(n))):
            return True
    if event_username or event_title:
        by_eid = {e.id: e for e in cfg.address_book}
        for eid in j.chat_entry_ids:
            ent = by_eid.get(str(eid))
            if not ent:
                continue
            if telegram_chat_matches_address_ref(
                ent.chat_ref,
                peer_id=peer_id,
                raw_chat_id=raw_chat_id,
                event_username=event_username,
                event_title=event_title,
            ):
                return True
    return False


class Schedule2Runner:
    """与 ScheduleRunner 共用 Telethon 会话，独立 JSON 任务列表。"""

    def __init__(self) -> None:
        self._running = False
        self._accounts: Dict[str, Account] = {}
        self._api_id: int = 0
        self._api_hash: str = ""
        self._shared_clients: Optional[Dict[str, TelegramClient]] = None
        self._account_locks: Dict[str, asyncio.Lock] = {}
        self._reminder_callback: Optional[Callable[["Schedule2Job", int, str], None]] = None
        self._read_tracker: Optional[TgWatchReadTracker] = None

    def bind_read_tracker(self, tracker: Optional[TgWatchReadTracker]) -> None:
        self._read_tracker = tracker

    def set_reminder_callback(self, fn: Optional[Callable[["Schedule2Job", int, str], None]]) -> None:
        self._reminder_callback = fn

    def bind_telegram_clients(self, clients: Dict[str, TelegramClient]) -> None:
        self._shared_clients = dict(clients)
        self._account_locks = {k: asyncio.Lock() for k in self._shared_clients}

    def unbind_telegram_clients(self) -> None:
        self._shared_clients = None
        self._account_locks = {}

    def start(self, cfg: AppConfig) -> None:
        self.stop()
        self._accounts = {a.id: a for a in cfg.accounts}
        self._api_id = int(cfg.api_id)
        self._api_hash = str(cfg.api_hash or "")
        info("定时任务2 已就绪（由统一会话线程执行）")

    def stop(self) -> None:
        self._running = False

    def pause_job(self, job_id: str, reason: str = "手动暂停") -> bool:
        jobs = load_schedule2_jobs()
        now = time.time()
        changed = False
        for j in jobs:
            if j.id != job_id or not j.enabled:
                continue
            if j.state != "paused":
                remain = j.remaining_seconds
                if remain <= 0:
                    if j.next_send_ts > 0:
                        remain = max(1.0, j.next_send_ts - now)
                    else:
                        remain = _next_delay_seconds(j)
                j.remaining_seconds = _clamp_secs(j, remain)
                j.state = "paused"
                j.pause_reason = reason
                changed = True
        if changed:
            save_schedule2_jobs(jobs)
        return changed

    def resume_job(self, job_id: str) -> bool:
        jobs = load_schedule2_jobs()
        now = time.time()
        changed = False
        for j in jobs:
            if j.id != job_id or not j.enabled:
                continue
            if j.state == "paused":
                remain = j.remaining_seconds if j.remaining_seconds > 0 else _next_delay_seconds(j)
                j.next_send_ts = now + _clamp_secs(j, remain)
                j.remaining_seconds = 0.0
                j.pause_reason = ""
                j.state = "running"
                changed = True
        if changed:
            save_schedule2_jobs(jobs)
        return changed

    # 兼容旧 UI / 调用
    def pause_session(self, reason: str = "手动暂停") -> bool:
        jobs = load_schedule2_jobs()
        ok = False
        for j in jobs:
            if self.pause_job(j.id, reason):
                ok = True
        return ok

    def resume_session(self) -> bool:
        jobs = load_schedule2_jobs()
        ok = False
        for j in jobs:
            if self.resume_job(j.id):
                ok = True
        return ok

    def pause_by_chat(
        self,
        chat_id: int,
        reason: str,
        *,
        raw_chat_id: Optional[int] = None,
        event_username: Optional[str] = None,
        event_title: Optional[str] = None,
    ) -> int:
        jobs = load_schedule2_jobs()
        cfg = load_config()
        now = time.time()
        cnt = 0
        cid = int(chat_id)
        raw_c = int(raw_chat_id) if raw_chat_id is not None else cid
        variants = set(chat_peer_ids_for_match(cid)) | set(chat_peer_ids_for_match(raw_c))

        for j in jobs:
            if not j.enabled or j.state == "paused":
                continue
            if not _job_has_chat(
                j,
                cfg,
                variants,
                peer_id=cid,
                raw_chat_id=raw_c,
                event_username=event_username,
                event_title=event_title,
            ):
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
            save_schedule2_jobs(jobs)
        return cnt

    def _emit_reminder(self, job: "Schedule2Job", step: int, note: str) -> None:
        cb = self._reminder_callback
        if cb:
            try:
                cb(job, step, note)
            except Exception as exc:
                error(f"定时任务2 提醒回调异常：{exc}")

    async def _async_main(self) -> None:
        while self._running:
            try:
                now = time.time()
                jobs = load_schedule2_jobs()
                changed = False
                for j in jobs:
                    if not self._running:
                        break
                    if not j.enabled or not j.rows:
                        continue
                    if j.state == "paused":
                        continue

                    if j.next_send_ts <= 0:
                        j.next_send_ts = now + _next_delay_seconds(j)
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
                    if row.is_reminder:
                        self._emit_reminder(j, j.cursor + 1, row.reminder_note)
                        cfg_rem = load_config()
                        grp = format_job_targets_label(cfg_rem, j)
                        info(
                            f"定时任务2 阶段提醒：{j.id} 群={grp} 文档={j.source_name} 步={j.cursor + 1}"
                        )
                        j.cursor += 1
                        j.last_send_ts = time.time()
                        j.last_error = ""
                        if j.cursor >= len(j.rows):
                            j.enabled = False
                            j.state = "paused"
                            j.pause_reason = "一轮发送完成，任务自动停止"
                            j.next_send_ts = 0.0
                        else:
                            j.next_send_ts = time.time() + _next_delay_seconds(j)
                        changed = True
                        continue

                    cfg = load_config()
                    fake = SimpleNamespace(chat_entry_ids=j.chat_entry_ids, chat_ids=j.chat_ids)
                    targets = resolve_job_chat_targets(cfg, fake)
                    if not targets:
                        warning(f"定时任务2 跳过：任务 {j.id} 无法解析发送目标")
                        j.last_error = "发送目标无效"
                        j.next_send_ts = time.time() + _next_delay_seconds(j)
                        changed = True
                        continue

                    acc_send = (row.send_as_account_id or row.original_account_id).strip()
                    ok = await self._send_one_to_many(targets, acc_send, row.content)
                    if ok:
                        j.cursor += 1
                        j.last_send_ts = time.time()
                        j.last_error = ""
                        info(
                            f"定时任务2 已发送：任务={j.id} 文档={j.source_name} "
                            f"实际账号={acc_send}（原文={row.original_account_id}）"
                        )
                        if j.cursor >= len(j.rows):
                            j.enabled = False
                            j.state = "paused"
                            j.pause_reason = "一轮发送完成，任务自动停止"
                            j.next_send_ts = 0.0
                    else:
                        j.last_error = "发送失败，已按下一轮间隔重试"
                    j.next_send_ts = time.time() + _next_delay_seconds(j)
                    changed = True

                if changed:
                    save_schedule2_jobs(jobs)
                await asyncio.sleep(0.8)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                error(f"定时任务2 循环异常：{exc}")
                await asyncio.sleep(1.5)

    async def _send_one_to_many(self, chat_refs: List[Union[int, str]], account_id: str, content: str) -> bool:
        account_id = (account_id or "").strip()
        if not self._api_id or not self._api_hash.strip():
            warning("定时任务2 发送跳过：未配置 api_id / api_hash")
            return False
        acc = self._accounts.get(account_id)
        if acc is None or not acc.enabled:
            warning(f"定时任务2 发送跳过：账号 {account_id} 不存在或未启用")
            return False
        shared = self._shared_clients
        if not shared:
            error("定时任务2 发送失败：尚未绑定 Telegram 会话")
            return False
        client = shared.get(account_id)
        if client is None:
            warning(f"定时任务2 发送跳过：账号 {account_id} 未连接")
            return False
        lock = self._account_locks.get(account_id)
        if lock is None:
            lock = asyncio.Lock()
            self._account_locks[account_id] = lock
        async with lock:
            try:
                if not await client.is_user_authorized():
                    warning(f"定时任务2 发送跳过：账号 {account_id} 未登录")
                    return False
                ok = False
                for cref in chat_refs:
                    try:
                        entity = await _resolve_entity_for_send(client, cref)
                        await mark_watch_read_before_send(
                            client, account_id, entity, self._read_tracker
                        )
                        await send_telegram_message_resilient(client, entity, content)
                        ok = True
                    except Exception as exc:
                        error(f"定时任务2 发送失败：账号={account_id} 群={cref} 错误={exc}")
                    if len(chat_refs) > 1:
                        await asyncio.sleep(0.2)
                return ok
            except Exception as exc:
                error(f"定时任务2 发送异常：账号={account_id} 错误={exc}")
                return False
