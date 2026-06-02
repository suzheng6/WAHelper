"""WhatsApp 多账号监听与定时任务配置。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from paths import resource_path
from platform_paths import migrate_legacy_wa_layout, wa_data_root

BASE_DIR = wa_data_root()
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CONFIG_EXAMPLE_NAME = "config.example.json"
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
DATA_DIR = os.path.join(BASE_DIR, "data")
SCHEDULE_FILE = os.path.join(DATA_DIR, "schedules.json")
SCHEDULE2_FILE = os.path.join(DATA_DIR, "schedule2.json")

_lock = threading.RLock()
WatchTarget = str  # 监听用户：手机号（含国家码，可带 +）


def ensure_dirs() -> None:
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)


def ensure_first_run_config() -> None:
    if os.path.isfile(CONFIG_PATH):
        return
    for src in (os.path.join(BASE_DIR, CONFIG_EXAMPLE_NAME), resource_path(CONFIG_EXAMPLE_NAME)):
        if os.path.isfile(src):
            try:
                shutil.copyfile(src, CONFIG_PATH)
                return
            except OSError:
                continue


def ensure_runtime() -> None:
    migrate_legacy_wa_layout()
    ensure_dirs()
    ensure_first_run_config()


def parse_chat_ref_input(g: str) -> str:
    """群/会话：完整 JID、手机号私聊，或 WhatsApp 群邀请链接。"""
    t = (g or "").strip()
    if not t:
        raise ValueError("群或会话不能为空")
    low = t.lower()
    if "chat.whatsapp.com/" in low:
        return t
    if "@" in t:
        return t
    digits = "".join(c for c in t if c.isdigit())
    if digits:
        return digits
    raise ValueError("请填写群 JID（xxx@g.us）、手机号或群邀请链接")


def parse_watch_user_input(u: str) -> str:
    t = (u or "").strip()
    if not t:
        raise ValueError("监听用户不能为空")
    digits = "".join(c for c in t if c.isdigit())
    if not digits:
        raise ValueError("请填写手机号（含国家码）")
    return digits


@dataclass
class AddressEntry:
    id: str
    remark: str
    chat_ref: str
    watch_user: str = ""
    listen_enabled: bool = True
    """定时任务 TXT「账号=主号」时由该账号发送；在通讯录选择后写入 config，重启仍保留。"""
    owner_account_id: str = ""
    """该群上一次成功发出的定时任务 TXT 文件名（持久化到 config）。"""
    last_schedule_source_name: str = ""


@dataclass
class Account:
    id: str
    session_name: str
    enabled: bool = True
    phone: str = ""
    proxy: str = ""  # socks5://host:port 或 socks5://host:port:user:pass

    def db_path(self) -> str:
        ensure_dirs()
        base = (self.session_name or self.id).strip() or self.id
        return os.path.join(SESSIONS_DIR, f"{base}.db")


@dataclass
class AppConfig:
    accounts: List[Account] = field(default_factory=list)
    watch_rules: Dict[str, WatchTarget] = field(default_factory=dict)
    address_book: List[AddressEntry] = field(default_factory=list)
    rate_limit_seconds: float = 10.0
    listening_enabled: bool = True


def default_config() -> AppConfig:
    return AppConfig()


def iter_listen_bindings(cfg: AppConfig) -> List[Tuple[str, WatchTarget]]:
    pairs: List[Tuple[str, WatchTarget]] = []
    for e in cfg.address_book:
        if not e.listen_enabled:
            continue
        wu = (e.watch_user or "").strip()
        cr = (e.chat_ref or "").strip()
        if not wu or not cr:
            continue
        try:
            pairs.append((cr, parse_watch_user_input(wu)))
        except ValueError:
            continue
    legacy = cfg.watch_rules or {}
    for k, v in legacy.items():
        if v:
            pairs.append((str(k), parse_watch_user_input(str(v))))
    return pairs


def format_job_targets_label(cfg: "AppConfig", job: Any) -> str:
    """定时任务目标群：通讯录备注优先。"""
    parts: List[str] = []
    emap = {e.id: e for e in cfg.address_book}
    for eid in getattr(job, "chat_entry_ids", None) or []:
        ent = emap.get(str(eid))
        if not ent:
            parts.append(f"通讯录条目 {eid}")
            continue
        remark = (ent.remark or "").strip()
        ref = (ent.chat_ref or "").strip()
        if remark:
            parts.append(f"{remark}（{ref}）" if ref else remark)
        elif ref:
            parts.append(ref)
        else:
            parts.append(str(eid))
    if not parts:
        for cid in getattr(job, "chat_ids", None) or []:
            s = str(cid).strip()
            if s:
                parts.append(s)
    return "、".join(parts) if parts else "未指定群"


def resolve_job_chat_targets(cfg: AppConfig, job: Any) -> List[str]:
    entry_ids = getattr(job, "chat_entry_ids", None) or []
    if entry_ids:
        by_eid = {e.id: e for e in cfg.address_book}
        out: List[str] = []
        for eid in entry_ids:
            ent = by_eid.get(str(eid))
            if ent and (ent.chat_ref or "").strip():
                out.append(ent.chat_ref.strip())
        return out
    raw = getattr(job, "chat_ids", None) or []
    return [str(x).strip() for x in raw if str(x).strip()]


def _address_entry_from_dict(d: Dict[str, Any]) -> Optional[AddressEntry]:
    if not isinstance(d, dict):
        return None
    eid = str(d.get("id", "")).strip()
    if not eid:
        return None
    return AddressEntry(
        id=eid,
        remark=str(d.get("remark", "") or ""),
        chat_ref=str(d.get("chat_ref", "") or ""),
        watch_user=str(d.get("watch_user", "") or ""),
        listen_enabled=bool(d.get("listen_enabled", True)),
        owner_account_id=str(d.get("owner_account_id", "") or "").strip(),
        last_schedule_source_name=str(d.get("last_schedule_source_name", "") or "").strip(),
    )


def _last_schedule_names_on_disk() -> Dict[str, str]:
    """读取 config.json 中各通讯录条目的 last_schedule_source_name（不覆盖内存其它字段）。"""
    with _lock:
        if not os.path.isfile(CONFIG_PATH):
            return {}
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    out: Dict[str, str] = {}
    for row in raw.get("address_book", []):
        if not isinstance(row, dict):
            continue
        eid = str(row.get("id", "")).strip()
        name = str(row.get("last_schedule_source_name", "") or "").strip()
        if eid and name:
            out[eid] = name
    return out


def sync_last_schedule_from_disk(cfg: AppConfig) -> None:
    """从磁盘同步「上次任务文件名」到内存（定时任务页刷新勾选列表时调用）。"""
    disk = _last_schedule_names_on_disk()
    if not disk:
        return
    for ent in cfg.address_book:
        name = disk.get(ent.id, "")
        if name:
            ent.last_schedule_source_name = name


def merge_last_schedule_from_disk(cfg: AppConfig) -> None:
    """保存前合并：仅当内存为空时用磁盘值，避免覆盖本次刚写入的更新。"""
    disk = _last_schedule_names_on_disk()
    if not disk:
        return
    for ent in cfg.address_book:
        if (ent.last_schedule_source_name or "").strip():
            continue
        name = disk.get(ent.id, "")
        if name:
            ent.last_schedule_source_name = name


def apply_last_schedule_from_current_jobs(cfg: AppConfig) -> bool:
    """按任务管理当前列表刷新「上次任务文件名」：有任务→该任务 TXT 名；无任务→空（行程结束）。

    删除任务时不调用；仅在添加任务后调用，以便先删光任务仍能看见上次的 14 等标记再决定加不加。
    """
    from schedule2_runner import load_schedule2_jobs

    job_map: Dict[str, str] = {}
    for j in load_schedule2_jobs():
        name = (getattr(j, "source_name", None) or "").strip()
        for eid in getattr(j, "chat_entry_ids", None) or []:
            s = str(eid).strip()
            if s:
                job_map[s] = name

    changed = False
    for ent in cfg.address_book:
        new_val = job_map.get(ent.id, "")
        if ent.last_schedule_source_name != new_val:
            ent.last_schedule_source_name = new_val
            changed = True
    if changed:
        save_config(cfg)
    return changed


def record_address_book_last_schedule(chat_entry_ids: List[str], source_name: str) -> None:
    """定时任务成功发送到群后，更新通讯录条目的「上次任务文件」。"""
    name = (source_name or "").strip()
    if not name:
        return
    ids = {str(x).strip() for x in chat_entry_ids if str(x).strip()}
    if not ids:
        return
    cfg = load_config()
    changed = False
    for ent in cfg.address_book:
        if ent.id in ids:
            ent.last_schedule_source_name = name
            changed = True
    if changed:
        save_config(cfg)


def _account_from_dict(d: Dict[str, Any]) -> Account:
    aid = str(d.get("id", "default"))
    sn = str(d.get("session_name") or "").strip() or aid
    return Account(
        id=aid,
        session_name=sn,
        enabled=bool(d.get("enabled", True)),
        phone=str(d.get("phone", "")),
        proxy=str(d.get("proxy", "") or ""),
    )


def load_config() -> AppConfig:
    ensure_dirs()
    with _lock:
        if not os.path.isfile(CONFIG_PATH):
            return default_config()
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError):
            return default_config()

    accounts: List[Account] = []
    for row in raw.get("accounts", []):
        if isinstance(row, dict):
            try:
                accounts.append(_account_from_dict(row))
            except (KeyError, TypeError, ValueError):
                continue

    watch_rules: Dict[str, WatchTarget] = {}
    wr = raw.get("watch_rules", {})
    if isinstance(wr, dict):
        for k, v in wr.items():
            if v is not None and str(v).strip():
                watch_rules[str(k)] = str(v).strip()

    address_book: List[AddressEntry] = []
    for row in raw.get("address_book", []):
        if isinstance(row, dict):
            ent = _address_entry_from_dict(row)
            if ent is not None:
                address_book.append(ent)

    if not address_book and watch_rules:
        for i, (k, v) in enumerate(watch_rules.items()):
            stable = hashlib.md5(f"{k}|{v}".encode("utf-8")).hexdigest()[:12]
            address_book.append(
                AddressEntry(id=stable, remark=f"绑定{i + 1}", chat_ref=str(k), watch_user=str(v), listen_enabled=True)
            )
        watch_rules = {}

    return AppConfig(
        accounts=accounts,
        watch_rules=watch_rules,
        address_book=address_book,
        rate_limit_seconds=float(raw.get("rate_limit_seconds", 10.0)),
        listening_enabled=bool(raw.get("listening_enabled", True)),
    )


def save_config(cfg: AppConfig) -> None:
    ensure_dirs()
    merge_last_schedule_from_disk(cfg)
    data: Dict[str, Any] = {
        "accounts": [asdict(a) for a in cfg.accounts],
        "watch_rules": {str(k): v for k, v in cfg.watch_rules.items()},
        "address_book": [asdict(e) for e in cfg.address_book],
        "rate_limit_seconds": cfg.rate_limit_seconds,
        "listening_enabled": cfg.listening_enabled,
    }
    tmp = CONFIG_PATH + ".tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
