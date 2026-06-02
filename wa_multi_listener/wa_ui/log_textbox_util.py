"""日志 Textbox 辅助：限长、刷新、本地滚轮（避免被页面 Canvas 滚轮抢走）。"""
from __future__ import annotations

import tkinter as tk
from typing import Any, Callable, List, Sequence

import customtkinter as ctk

LOG_TEXTBOX_MAX_LINES = 400
DASH_LOG_MAX_LINES = 80
LOG_PUMP_MS = 200


def reload_log_textbox(
    textbox: Any,
    lines: Sequence[str],
    *,
    max_lines: int = LOG_TEXTBOX_MAX_LINES,
) -> None:
    """清空并写入最近若干行。"""
    try:
        textbox.configure(state="normal")
        textbox.delete("1.0", "end")
        chunk = list(lines)[-max_lines:]
        if chunk:
            textbox.insert("1.0", "\n".join(chunk) + "\n")
        textbox.see("end")
        textbox.configure(state="disabled")
    except Exception:
        pass


def reload_log_textbox_from_memory(
    textbox: Any,
    get_recent_lines: Callable[[int], List[str]],
    *,
    limit: int = LOG_TEXTBOX_MAX_LINES,
    max_lines: int = LOG_TEXTBOX_MAX_LINES,
) -> None:
    reload_log_textbox(textbox, get_recent_lines(limit), max_lines=max_lines)


def append_log_line_capped(
    textbox: Any,
    line: str,
    *,
    max_lines: int = LOG_TEXTBOX_MAX_LINES,
) -> None:
    """向 CTkTextbox 追加一行并截断超出部分。"""
    try:
        textbox.configure(state="normal")
        textbox.insert("end", line + "\n")
        end_index = textbox.index("end-1c")
        end_line = int(str(end_index).split(".")[0])
        if end_line > max_lines:
            textbox.delete("1.0", f"{end_line - max_lines + 1}.0")
        textbox.see("end")
        textbox.configure(state="disabled")
    except Exception:
        pass


def bind_log_textbox_wheel(textbox: Any) -> None:
    """日志框内滚轮只滚动文本，不触发外层页面滚动。"""
    inner = getattr(textbox, "_textbox", None)

    def on_wheel(event: Any) -> str:
        delta = getattr(event, "delta", 0) or 0
        if delta == 0:
            return "break"
        target = inner if inner is not None else textbox
        try:
            target.yview_scroll(int(-1 * (delta / 120)), "units")
        except Exception:
            pass
        return "break"

    try:
        textbox.bind("<MouseWheel>", on_wheel, add="+")
    except tk.TclError:
        pass
    if inner is not None:
        try:
            inner.bind("<MouseWheel>", on_wheel, add="+")
        except tk.TclError:
            pass


def pointer_over_tk_text(toplevel: tk.Misc) -> bool:
    """鼠标是否位于可滚动的 Tk Text 上（含 CTkTextbox 内部）。"""
    try:
        px, py = toplevel.winfo_pointerx(), toplevel.winfo_pointery()
        w: Any = toplevel.winfo_containing(px, py)
        while w is not None:
            if isinstance(w, tk.Text):
                return True
            w = w.master
    except tk.TclError:
        pass
    return False
