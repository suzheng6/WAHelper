"""WhatsApp 界面日志展示过滤（整合程序中 TG 相关日志仍可能写入 WA 缓冲）。"""
from __future__ import annotations

from typing import List, Sequence

_TG_REACTION_MARKERS = ("定时点赞", "文档任务点赞")


def _message_part(line: str) -> str:
    text = (line or "").strip()
    if " | " in text:
        return text.split(" | ", 2)[-1].strip()
    return text


def is_visible_in_wa_log_view(line: str) -> bool:
    """WA 标签页日志面板是否展示该行。"""
    msg = _message_part(line)
    if not msg:
        return True
    if msg.startswith("[WA]"):
        return True
    if msg.startswith("[TG]") and "点赞" in msg:
        return False
    if any(m in msg for m in _TG_REACTION_MARKERS):
        return False
    return True


def filter_lines_for_wa_log_view(lines: Sequence[str]) -> List[str]:
    return [ln for ln in lines if is_visible_in_wa_log_view(ln)]
