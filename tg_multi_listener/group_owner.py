"""定时任务：「主号」占位符与各群归属账号的识别与映射。"""
from __future__ import annotations

from typing import Any, List

from .scheduler import DocMessage

# TXT 中 账号= 为这些值时，表示由通讯录所选 owner_account_id 发送
_MAIN_ACCOUNT_PLACEHOLDERS = frozenset(
    {
        "主号",
        "主账号",
        "main",
        "mainaccount",
        "host",
        "主机",
    }
)


def is_main_account_placeholder(label: str) -> bool:
    t = (label or "").strip()
    if not t:
        return False
    if t in _MAIN_ACCOUNT_PLACEHOLDERS:
        return True
    key = t.lower().replace(" ", "").replace("_", "")
    return key in {x.lower().replace(" ", "").replace("_", "") for x in _MAIN_ACCOUNT_PLACEHOLDERS}


def clone_doc_items(items: List[DocMessage]) -> List[DocMessage]:
    out: List[DocMessage] = []
    for it in items:
        out.append(
            DocMessage(
                account_id=it.account_id,
                content=it.content,
                is_reminder=it.is_reminder,
                reminder_note=it.reminder_note,
                delay_after_minutes=it.delay_after_minutes,
                original_account_id=it.original_account_id,
                send_as_account_id=it.send_as_account_id,
                interval_from_txt=it.interval_from_txt,
                want_reactions=it.want_reactions,
            )
        )
    return out


def apply_main_account_mapping(items: List[DocMessage], owner_account_id: str) -> int:
    """将原文为「主号」等占位符的条目改为由 owner_account_id 实际发送。返回映射条数。"""
    owner = (owner_account_id or "").strip()
    if not owner:
        return 0
    n = 0
    for it in items:
        if it.is_reminder:
            continue
        if is_main_account_placeholder(it.original_label()):
            it.send_as_account_id = owner
            n += 1
    return n


def doc_has_main_account_placeholder(items: List[DocMessage]) -> bool:
    return any(
        not it.is_reminder and is_main_account_placeholder(it.original_label()) for it in items
    )


def send_account_for_job_item(cfg: Any, job: Any, item: DocMessage) -> str:
    """发送时从通讯录读取主号归属（修改通讯录后重启/继续即生效）。"""
    if not is_main_account_placeholder(item.original_label()):
        return item.effective_send_account_id()
    from .compat_config import AppConfig

    if not isinstance(cfg, AppConfig):
        return item.effective_send_account_id()
    emap = {e.id: e for e in cfg.address_book}
    for eid in getattr(job, "chat_entry_ids", None) or []:
        ent = emap.get(str(eid))
        if ent:
            owner = (ent.owner_account_id or "").strip()
            if owner:
                return owner
    return item.effective_send_account_id()


