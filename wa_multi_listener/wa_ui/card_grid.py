"""网格方块布局：任务管理 3 列、账号 4 列。"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import customtkinter as ctk

TASKMGR_COLS = 3
TG_ACCT_COLS = 4


def configure_equal_columns(parent: ctk.CTkFrame, cols: int, *, uniform: str = "col") -> None:
    for c in range(cols):
        parent.grid_columnconfigure(c, weight=1, uniform=uniform)


def grid_place(widget: ctk.CTkBaseClass, index: int, cols: int, *, padx: int = 8, pady: int = 8) -> None:
    row, col = divmod(index, cols)
    widget.grid(row=row, column=col, sticky="nsew", padx=padx, pady=pady)


def reorder_taskmgr_grid(
    widgets_by_id: dict[str, dict[str, Any]],
    jobs_in_order: Iterable[Any],
    *,
    cols: int = TASKMGR_COLS,
    padx: int = 6,
    pady: int = 6,
    card_key: str = "card",
) -> None:
    """按展示顺序重排已有任务卡片（不销毁控件）。"""
    for i, j in enumerate(jobs_in_order):
        jid = getattr(j, "id", None)
        if not jid:
            continue
        w = widgets_by_id.get(jid)
        if not w:
            continue
        card = w.get(card_key)
        if card is None:
            continue
        card.grid_forget()
        grid_place(card, i, cols, padx=padx, pady=pady)
