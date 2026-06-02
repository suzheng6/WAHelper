"""网格方块布局：任务管理 3 列、账号 4 列。"""
from __future__ import annotations

import customtkinter as ctk

TASKMGR_COLS = 3
TG_ACCT_COLS = 4


def configure_equal_columns(parent: ctk.CTkFrame, cols: int, *, uniform: str = "col") -> None:
    for c in range(cols):
        parent.grid_columnconfigure(c, weight=1, uniform=uniform)


def grid_place(widget: ctk.CTkBaseClass, index: int, cols: int, *, padx: int = 8, pady: int = 8) -> None:
    row, col = divmod(index, cols)
    widget.grid(row=row, column=col, sticky="nsew", padx=padx, pady=pady)
