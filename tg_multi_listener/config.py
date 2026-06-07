"""加载与保存应用配置。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .paths import app_root, resource_path

# 数据目录：整合版在 telegram/；独立运行仍在 exe 旁
BASE_DIR = os.environ.get("TG_HELPER_DATA_ROOT", "").strip() or app_root()
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CONFIG_EXAMPLE_NAME = "config.example.json"
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
DATA_DIR = os.path.join(BASE_DIR, "data")
SCHEDULE_FILE = os.path.join(DATA_DIR, "schedules.json")
SCHEDULE2_FILE = os.path.join(DATA_DIR, "schedule2.json")

_lock = threading.RLock()

WatchTarget = Union[int, str]
"""群侧：整数 chat_id，或公开群/频道的 @用户名（启动监听时解析）。"""
ChatRef = Union[int, str]


def ensure_dirs() -> None:
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)


def ensure_first_run_config() -> None:
    """首次运行：若无 config.json，从示例复制到程序旁（便于用户直接改）。"""
    if os.path.isfile(CONFIG_PATH):
        return
    candidates = [
        os.path.join(BASE_DIR, CONFIG_EXAMPLE_NAME),
        resource_path(CONFIG_EXAMPLE_NAME),
    ]
    for src in candidates:
        if os.path.isfile(src):
            try:
                shutil.copyfile(src, CONFIG_PATH)
                return
            except OSError:
                continue


def ensure_runtime() -> None:
    """启动时调用：建目录 + 首启配置。"""
    ensure_dirs()
    ensure_first_run_config()


def parse_chat_ref_input(g: str) -> ChatRef:
    """群或频道：数字 ID（含 -100…）直接可用；否则视为公开用户名或 t.me 链接（无需群内已有消息）。"""
    t = (g or "").strip()
    if not t:
        raise ValueError("群或频道不能为空")
    low = t.lower()
    if "t.me/" in low or "telegram.me/" in low:
        split_on = "telegram.me/" if "telegram.me/" in low else "t.me/"
        tail = t.split(split_on, 1)[-1]
        tail = tail.split("?", 1)[0].strip().strip("/")
        if tail.startswith("+") or "joinchat" in low:
            raise ValueError("私密邀请链接无法在不入群时解析；请先加入该群，再用转发机器人取数字 ID")
        segs = [s for s in tail.split("/") if s and s not in ("s", "c")]
        if not segs:
            raise ValueError("无法从链接中识别公开群名")
        t = segs[-1]
    if t.startswith("-") and len(t) > 1 and t[1:].isdigit():
        return int(t)
    if t.isdigit():
        return int(t)
    name = t[1:].strip() if t.startswith("@") else t.strip()
    if not name:
        raise ValueError("群用户名无效")
    return f"@{name}"


def parse_watch_user_input(u: str) -> WatchTarget:
    """界面或配置中的「用户」：纯数字为用户 ID；否则视为 @ 用户名（不要求对方在目标群发过言）。"""
    t = (u or "").strip()
    if not t:
        raise ValueError("用户绑定不能为空")
    if t.startswith("-") and len(t) > 1 and t[1:].isdigit():
        return int(t)
    if t.isdigit():
        return int(t)
    name = t[1:].strip() if t.startswith("@") else t
    if not name:
        raise ValueError("用户名无效")
    return f"@{name}"


def _parse_watch_rule_value(v: Any) -> Optional[WatchTarget]:
    """从 JSON 读出：整数 / 数字字符串 → int；其余字符串 → 用户名（规范为 @xxx）。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        try:
            return int(v)
        except (ValueError, OverflowError):
            return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return parse_watch_user_input(s)
    except ValueError:
        return None


@dataclass
class AddressEntry:
    """通讯录条目：备注 + 群标识 + 监听用户（可空）；监听与定时任务均通过 id 选用。"""

    id: str
    remark: str
    chat_ref: str
    watch_user: str = ""
    listen_enabled: bool = True
    """该群在定时任务中「账号=主号」时实际使用的已登录账号（群归属/创建方）。"""
    owner_account_id: str = ""
    """该群上一次成功发出的定时任务 TXT 文件名（仅文件名，持久化到 config）。"""
    last_schedule_source_name: str = ""


def chat_peer_ids_for_match(chat_id: int) -> List[int]:
    """监听/暂停比对用：同一超级群可能出现的不同整型写法（与 Telethon get_peer_id 一致为主）。"""
    cid = int(chat_id)
    out: List[int] = []
    seen: set[int] = set()

    def add(x: int) -> None:
        if x not in seen:
            seen.add(x)
            out.append(x)

    add(cid)
    s = str(cid)
    if s.startswith("-100") and len(s) > 4 and s[4:].isdigit():
        try:
            inner = int(s[4:])
            add(inner)
        except ValueError:
            pass
    return out


def _telegram_handle_fragment(s: str) -> str:
    """从 t.me 链接、@用户名等提取可比对的 handle 片段（小写、无 @）。"""
    t = (s or "").strip().lower()
    if not t:
        return ""
    if "t.me/" in t:
        t = t.split("t.me/", 1)[-1].split("?", 1)[0].strip()
    if t.startswith("@"):
        t = t[1:]
    return t.split("/", 1)[0].strip()


def telegram_chat_matches_address_ref(
    chat_ref: str,
    *,
    peer_id: int,
    raw_chat_id: int,
    event_username: Optional[str] = None,
    event_title: Optional[str] = None,
) -> bool:
    """判断通讯录里的 chat_ref 是否与监听命中的群为同一会话（支持纯数字 ID 与 @用户名 / t.me）。"""
    cr = (chat_ref or "").strip()
    if not cr:
        return False
    n = chat_ref_to_optional_int(cr)
    peers = set(chat_peer_ids_for_match(int(peer_id))) | set(chat_peer_ids_for_match(int(raw_chat_id)))
    if n is not None:
        return bool(peers.intersection(set(chat_peer_ids_for_match(int(n)))))
    rf = _telegram_handle_fragment(cr)
    ev_u = _telegram_handle_fragment(str(event_username or ""))
    ev_t = (event_title or "").strip().lower()
    if ev_u and rf and rf == ev_u:
        return True
    if ev_t and rf and (rf == ev_t or cr.strip().lower() == ev_t):
        return True
    return False


def chat_ref_to_optional_int(ref: str) -> Optional[int]:
    """若 chat_ref 为纯数字群 ID，返回 int；否则（@用户名等）返回 None。"""
    t = (ref or "").strip()
    if not t:
        return None
    if t.startswith("-") and len(t) > 1 and t[1:].isdigit():
        try:
            return int(t)
        except ValueError:
            return None
    if t.isdigit():
        try:
            return int(t)
        except ValueError:
            return None
    return None


def resolve_job_chat_targets(cfg: "AppConfig", job: Any) -> List[Union[int, str]]:
    """解析定时任务的目标会话列表（供发送）。job 须有 chat_entry_ids 或 chat_ids。"""
    entry_ids = getattr(job, "chat_entry_ids", None) or []
    if entry_ids:
        by_eid = {e.id: e for e in cfg.address_book}
        out: List[Union[int, str]] = []
        for eid in entry_ids:
            ent = by_eid.get(str(eid))
            if not ent or not (ent.chat_ref or "").strip():
                continue
            s = ent.chat_ref.strip()
            n = chat_ref_to_optional_int(s)
            out.append(n if n is not None else s)
        return out
    raw = getattr(job, "chat_ids", None) or []
    return [int(x) for x in raw]


def find_address_entry_for_peer(
    cfg: "AppConfig",
    peer_id: int,
    *,
    raw_chat_id: Optional[int] = None,
    event_username: Optional[str] = None,
    event_title: Optional[str] = None,
) -> Optional[AddressEntry]:
    """按群 peer / 标题 / @用户名 在通讯录中查找对应条目。"""
    raw = int(raw_chat_id) if raw_chat_id is not None else int(peer_id)
    pid = int(peer_id)
    for e in cfg.address_book:
        if telegram_chat_matches_address_ref(
            e.chat_ref,
            peer_id=pid,
            raw_chat_id=raw,
            event_username=event_username,
            event_title=event_title,
        ):
            return e
    return None


def _peer_display_id(peer_id: int, chat_ref: str = "") -> str:
    ref = (chat_ref or "").strip()
    n = chat_ref_to_optional_int(ref) if ref else None
    if n is not None:
        return str(n)
    return str(int(peer_id))


def format_job_targets_label(cfg: "AppConfig", job: Any) -> str:
    """定时任务目标群：备注优先，并附带群 ID 或通讯录 chat_ref。"""
    parts: List[str] = []
    emap = {e.id: e for e in cfg.address_book}
    for eid in getattr(job, "chat_entry_ids", None) or []:
        ent = emap.get(str(eid))
        if not ent:
            parts.append(f"通讯录条目 {eid}")
            continue
        remark = (ent.remark or "").strip()
        ref = (ent.chat_ref or "").strip()
        pid = _peer_display_id(0, ref) if ref else eid
        if remark:
            parts.append(f"{remark}（ID {pid}）" if pid else remark)
        elif ref:
            parts.append(f"ID {ref}")
        else:
            parts.append(eid)
    for cid in getattr(job, "chat_ids", None) or []:
        cid_int = int(cid)
        matched: Optional[AddressEntry] = None
        for e in cfg.address_book:
            n = chat_ref_to_optional_int(e.chat_ref)
            if n is not None and set(chat_peer_ids_for_match(n)).intersection(chat_peer_ids_for_match(cid_int)):
                matched = e
                break
        if matched:
            remark = (matched.remark or "").strip()
            parts.append(f"{remark}（ID {cid_int}）" if remark else f"ID {cid_int}")
        else:
            parts.append(f"ID {cid_int}")
    return "、".join(parts) if parts else "未设群"


def format_listener_chat_label(
    cfg: "AppConfig",
    *,
    peer_id: int,
    chat_title: str = "",
    chat_id_raw: Optional[int] = None,
    chat_username: Optional[str] = None,
) -> str:
    """监听命中时的群标注：通讯录备注优先，附带 Telegram 群 ID。"""
    pid = int(peer_id)
    ent = find_address_entry_for_peer(
        cfg,
        pid,
        raw_chat_id=chat_id_raw,
        event_username=chat_username,
        event_title=chat_title,
    )
    title = (chat_title or "").strip()
    if ent:
        remark = (ent.remark or "").strip()
        ref = (ent.chat_ref or "").strip()
        disp_id = _peer_display_id(pid, ref)
        name = remark or title or ref or f"群"
        if title and remark and title.lower() != remark.lower():
            return f"{name} / {title} · ID {disp_id}"
        return f"{name} · ID {disp_id}"
    if title:
        return f"{title} · ID {pid}"
    uname = (chat_username or "").strip().lstrip("@")
    if uname:
        return f"@{uname} · ID {pid}"
    return f"ID {pid}"


def iter_listen_bindings(cfg: "AppConfig") -> List[Tuple[str, WatchTarget]]:
    """生成监听绑定：(群原始标识字符串, 目标用户)。含通讯录勾选项与旧版 watch_rules。"""
    pairs: List[Tuple[str, WatchTarget]] = []
    for e in cfg.address_book:
        if not e.listen_enabled:
            continue
        wu = (e.watch_user or "").strip()
        if not wu:
            continue
        cr = (e.chat_ref or "").strip()
        if not cr:
            continue
        try:
            tgt = parse_watch_user_input(wu)
        except ValueError:
            continue
        pairs.append((cr, tgt))
    legacy = getattr(cfg, "watch_rules", {}) or {}
    if isinstance(legacy, dict):
        for k, v in legacy.items():
            parsed = _parse_watch_rule_value(v)
            if parsed is not None:
                pairs.append((str(k), parsed))
    return pairs


@dataclass
class Account:
    """单个 Telegram 登录身份（共用应用级 api_id / api_hash）。"""

    id: str
    session_name: str
    enabled: bool = True
    phone: str = ""

    def session_path(self) -> str:
        ensure_dirs()
        return os.path.join(SESSIONS_DIR, f"{self.session_name}.session")


@dataclass
class AppConfig:
    api_id: int = 0
    api_hash: str = ""
    accounts: List[Account] = field(default_factory=list)
    """旧版：群键 → 监听用户；保留读写以兼容，启动时可迁移到 address_book。"""
    watch_rules: Dict[str, WatchTarget] = field(default_factory=dict)
    address_book: List[AddressEntry] = field(default_factory=list)
    rate_limit_seconds: float = 10.0
    listening_enabled: bool = True


def default_config() -> AppConfig:
    return AppConfig(
        api_id=0,
        api_hash="",
        accounts=[],
        watch_rules={},
        address_book=[],
        rate_limit_seconds=10.0,
        listening_enabled=True,
    )


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
    """读取 config.json 中各通讯录条目的 last_schedule_source_name（含空字符串）。"""
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
        if not eid:
            continue
        out[eid] = str(row.get("last_schedule_source_name", "") or "").strip()
    return out


def sync_last_schedule_from_disk(cfg: AppConfig) -> None:
    """从磁盘同步「上次任务文件名」到内存。"""
    disk = _last_schedule_names_on_disk()
    for ent in cfg.address_book:
        ent.last_schedule_source_name = disk.get(ent.id, "")


def merge_last_schedule_from_disk(cfg: AppConfig) -> None:
    """已弃用：保存时以内存为准，避免清空「上次任务」时被磁盘旧值回填。"""
    return


def _schedule_name_map_for_jobs(cfg: AppConfig, jobs: Sequence[Any]) -> Dict[str, str]:
    """将任务列表映射为 通讯录条目 id -> 任务 TXT 文件名。"""
    job_map: Dict[str, str] = {}
    for j in jobs:
        name = (getattr(j, "source_name", None) or "").strip()
        entry_ids = [str(x).strip() for x in (getattr(j, "chat_entry_ids", None) or []) if str(x).strip()]
        if entry_ids:
            for eid in entry_ids:
                job_map[eid] = name
            continue
        for cid in getattr(j, "chat_ids", None) or []:
            try:
                cid_int = int(cid)
            except (TypeError, ValueError):
                continue
            for ent in cfg.address_book:
                n = chat_ref_to_optional_int(ent.chat_ref)
                if n is None:
                    continue
                if set(chat_peer_ids_for_match(n)).intersection(chat_peer_ids_for_match(cid_int)):
                    job_map[ent.id] = name
    return job_map


def apply_last_schedule_for_jobs(cfg: AppConfig, jobs: Sequence[Any]) -> bool:
    """仅更新指定任务对应群的「上次任务文件名」，不清空其它群的历史标记。"""
    job_map = _schedule_name_map_for_jobs(cfg, jobs)
    if not job_map:
        return False
    changed = False
    for ent in cfg.address_book:
        if ent.id not in job_map:
            continue
        new_val = job_map[ent.id]
        if ent.last_schedule_source_name != new_val:
            ent.last_schedule_source_name = new_val
            changed = True
    if changed:
        save_config(cfg)
    return changed


def apply_last_schedule_from_current_jobs(cfg: AppConfig) -> bool:
    """按任务管理当前列表刷新「上次任务文件名」：有任务→文件名；无任务→空。

    仅由用户点「从任务管理同步上次任务」时调用；添加任务请用 apply_last_schedule_for_jobs。
    """
    from .scheduler import load_jobs

    job_map = _schedule_name_map_for_jobs(cfg, load_jobs())
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
    """文档任务成功发送到群后，更新通讯录条目的「上次任务文件」。"""
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
    sn = str(d.get("session_name") or "").strip()
    if not sn:
        sn = aid
    return Account(
        id=aid,
        session_name=sn,
        enabled=bool(d.get("enabled", True)),
        phone=str(d.get("phone", "")),
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

    g_api = int(raw.get("api_id") or 0)
    g_hash = str(raw.get("api_hash") or "").strip()

    accounts: List[Account] = []
    for row in raw.get("accounts", []):
        if not isinstance(row, dict):
            continue
        if g_api == 0 and row.get("api_id") is not None:
            try:
                g_api = int(row["api_id"])
            except (TypeError, ValueError):
                pass
        if not g_hash and row.get("api_hash"):
            g_hash = str(row["api_hash"]).strip()

        try:
            accounts.append(_account_from_dict(row))
        except (KeyError, TypeError, ValueError):
            continue

    wr = raw.get("watch_rules", {})
    watch_rules: Dict[str, WatchTarget] = {}
    if isinstance(wr, dict):
        for k, v in wr.items():
            parsed = _parse_watch_rule_value(v)
            if parsed is not None:
                watch_rules[str(k)] = parsed

    address_book: List[AddressEntry] = []
    for row in raw.get("address_book", []):
        if isinstance(row, dict):
            ent = _address_entry_from_dict(row)
            if ent is not None:
                address_book.append(ent)

    migrated_from_legacy = False
    if not address_book and watch_rules:
        migrated_from_legacy = True
        for i, (k, v) in enumerate(watch_rules.items()):
            vu: Union[int, str] = v  # type: ignore[assignment]
            ws = str(vu) if not isinstance(vu, str) else vu
            stable = hashlib.md5(f"{k}|{ws}".encode("utf-8")).hexdigest()[:12]
            address_book.append(
                AddressEntry(
                    id=stable,
                    remark=f"绑定{i + 1}",
                    chat_ref=str(k),
                    watch_user=ws,
                    listen_enabled=True,
                )
            )
    if migrated_from_legacy:
        watch_rules = {}

    return AppConfig(
        api_id=g_api,
        api_hash=g_hash,
        accounts=accounts,
        watch_rules=watch_rules,
        address_book=address_book,
        rate_limit_seconds=float(raw.get("rate_limit_seconds", 10.0)),
        listening_enabled=bool(raw.get("listening_enabled", True)),
    )


def save_config(cfg: AppConfig) -> None:
    ensure_dirs()
    data: Dict[str, Any] = {
        "api_id": int(cfg.api_id),
        "api_hash": str(cfg.api_hash or ""),
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
