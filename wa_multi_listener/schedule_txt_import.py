"""解析文档任务 TXT：账号 + 消息 + 可选每条间隔（分钟）。"""
from __future__ import annotations

import re
from typing import List, Optional, Set, Tuple

from scheduler import DocMessage


def _norm_key(raw: str) -> str:
    return raw.strip().lower().replace(" ", "").replace("_", "")


def _split_blocks(text: str) -> List[str]:
    lines = text.splitlines()
    blocks: List[List[str]] = []
    cur: List[str] = []
    sep = re.compile(r"^\s*\[(?:条目|item|消息|msg|提醒|reminder)\]\s*$", re.IGNORECASE)
    for line in lines:
        if sep.match(line):
            if cur:
                blocks.append(cur)
            cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append(cur)
    out = ["\n".join(x).strip() for x in blocks if "\n".join(x).strip()]
    if not out and text.strip():
        return [text.strip()]
    return out


_KV_LINE = re.compile(r"^([^:：=＝]+)\s*[:：=＝]\s*(.*)$")
_REMINDER_HEAD = re.compile(r"^[!！]\s*提醒\s*[!！]\s*$", re.IGNORECASE)
_DELAY_KEYS = frozenset({"间隔", "interval", "延迟", "delay", "间隔分钟", "等待", "等待分钟"})
def _parse_delay_minutes_value(v: str) -> Optional[float]:
    """单条间隔：非负分钟；0 表示立刻进入下一条。"""
    t = (v or "").strip()
    if not t:
        return None
    if re.match(r"^\s*\d+(?:\.\d+)?\s*-\s*\d", t):
        return None
    try:
        x = float(t)
    except ValueError:
        return None
    if x < 0:
        return None
    return x


def _try_parse_reminder_block(block: str) -> Optional[DocMessage]:
    account_val = ""
    message_val = ""
    note = ""
    delay_m: Optional[float] = None
    has_marker = False
    for raw in block.splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        if _REMINDER_HEAD.match(s):
            has_marker = True
            continue
        m = _KV_LINE.match(s)
        if not m:
            return None
        k = _norm_key(m.group(1))
        v = m.group(2).strip()
        if k in ("账号", "account", "发送账号"):
            account_val = v
        elif k in ("消息", "内容", "message", "text", "msg"):
            message_val = v
        elif k in ("备注", "说明", "提示", "note"):
            note = v
        elif _norm_key(k) in _DELAY_KEYS:
            parsed = _parse_delay_minutes_value(v)
            if parsed is None:
                return None
            delay_m = parsed
        elif k in ("类型", "kind"):
            if v.strip().lower() in ("提醒", "reminder") or _norm_key(v) in ("提醒", "reminder"):
                has_marker = True
        elif k == "提醒":
            if v.strip().lower() in ("1", "yes", "true", "是", "y", "on"):
                has_marker = True
        else:
            return None
    if account_val.strip():
        return None
    if not has_marker:
        return None
    if message_val.strip():
        note = f"{note}\n{message_val}".strip() if note else message_val
    return DocMessage(
        account_id="",
        content="",
        is_reminder=True,
        reminder_note=note,
        delay_after_minutes=delay_m,
    )


def _parse_block(block: str) -> Tuple[Optional[DocMessage], Optional[str]]:
    rem = _try_parse_reminder_block(block)
    if rem is not None:
        return rem, None
    lines = block.splitlines()
    account = ""
    content = ""
    delay_m: Optional[float] = None
    i = 0
    in_multi = False
    multi: List[str] = []
    matched_assignment = False
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            i += 1
            continue
        if in_multi:
            if s == ">>>":
                in_multi = False
                content = "\n".join(multi).strip()
                multi = []
                i += 1
                continue
            multi.append(raw)
            i += 1
            continue
        if s.endswith("<<<"):
            k = _norm_key(s.replace("<<<", ""))
            if k in ("消息", "内容", "message", "text", "msg"):
                in_multi = True
            i += 1
            continue
        m = _KV_LINE.match(s)
        if not m:
            i += 1
            continue
        matched_assignment = True
        k = _norm_key(m.group(1))
        v = m.group(2).strip()
        if k in ("账号", "account", "发送账号"):
            account = v
        elif k in ("消息", "内容", "message", "text", "msg"):
            content = v
        elif _norm_key(k) in _DELAY_KEYS:
            parsed = _parse_delay_minutes_value(v)
            if parsed is None:
                return None, "间隔格式无效（请填非负数字，单位：分钟，如 间隔=5）"
            delay_m = parsed
        i += 1
    if not account:
        if not content:
            if not matched_assignment:
                return None, None
            return None, "缺少账号字段（账号=xxx）"
        return None, "缺少账号字段（账号=xxx）"
    if not content:
        return None, "缺少消息字段（消息=xxx 或 消息<<< >>>）"
    return (
        DocMessage(
            account_id=account,
            content=content,
            is_reminder=False,
            reminder_note="",
            delay_after_minutes=delay_m,
        ),
        None,
    )


def import_doc_items(text: str, valid_accounts: Optional[Set[str]] = None) -> Tuple[List[DocMessage], List[str]]:
    text = (text or "").lstrip("\ufeff")
    blocks = _split_blocks(text)
    out: List[DocMessage] = []
    errs: List[str] = []
    for idx, b in enumerate(blocks, start=1):
        item, err = _parse_block(b)
        if err:
            errs.append(f"第 {idx} 段：{err}")
            continue
        if item is None:
            continue
        if item.is_reminder:
            out.append(item)
            continue
        if valid_accounts is not None and item.account_id not in valid_accounts:
            errs.append(f"第 {idx} 段：账号「{item.account_id}」未在账号管理中添加")
            continue
        out.append(item)
    return out, errs
