"""定时任务：主号占位符与通讯录归属账号（手动选择，写入 config 持久化）。"""
from __future__ import annotations

from typing import Dict, List

from config import Account, AppConfig

# TXT 中「本群主号」占位：实际发送账号取自通讯录 owner_account_id
PRIMARY_ACCOUNT_ALIASES = frozenset(
    {
        "主号",
        "群主",
        "本号",
        "本群主号",
        "owner",
        "host",
        "main",
    }
)


def is_primary_account_label(label: str) -> bool:
    t = (label or "").strip()
    if not t:
        return False
    low = t.lower().replace(" ", "")
    if low in {x.lower() for x in PRIMARY_ACCOUNT_ALIASES}:
        return True
    return t in PRIMARY_ACCOUNT_ALIASES


def mark_row_primary_auto(row_original: str, row_send_as: str) -> tuple[str, str]:
    """导入 TXT：主号条目保留原文，发送账号在运行时从通讯录读取。"""
    orig = (row_original or "").strip()
    if is_primary_account_label(orig):
        return orig, ""
    send = (row_send_as or orig).strip()
    if is_primary_account_label(send):
        return orig or send, ""
    return orig, send or orig


def row_needs_per_group_owner(rows: List) -> bool:
    for r in rows:
        orig = getattr(r, "original_account_id", "") or ""
        if is_primary_account_label(orig):
            return True
    return False


def address_owner_map(cfg: AppConfig) -> Dict[str, str]:
    """通讯录条目 id → 用户选择的主号/归属账号（重启后从 config.json 恢复）。"""
    return {str(e.id): (e.owner_account_id or "").strip() for e in cfg.address_book}


def resolve_send_account_id(
    row,
    entry_id: str,
    owner_by_entry: Dict[str, str],
    accounts: Dict[str, Account],
) -> str:
    orig = (getattr(row, "original_account_id", "") or "").strip()
    send = (getattr(row, "send_as_account_id", "") or "").strip()

    if is_primary_account_label(orig) or is_primary_account_label(send):
        return (owner_by_entry.get(str(entry_id)) or "").strip()

    if send:
        return send
    if orig in accounts:
        return orig
    return orig
