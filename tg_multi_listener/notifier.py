"""顶置弹窗提醒：群名 / 用户 / 内容，淡入与强提醒。"""
from __future__ import annotations

from typing import Callable, Optional

import customtkinter as ctk

from .ui.theme import COLORS

_ALERT_GEOMETRY = "460x260"
_ALERT_MINSIZE = (400, 220)
_STAGE_GEOMETRY = "460x240"
_STAGE_MINSIZE = (400, 200)
_LABEL_WRAP = 400


def _setup_popup_window(win: ctk.CTkToplevel, master: ctk.CTk) -> None:
    """弹窗跟随主窗口并保持置顶。"""
    try:
        win.transient(master.winfo_toplevel())
    except Exception:
        pass
    try:
        win.attributes("-topmost", True)
    except Exception:
        pass
    try:
        win.lift()
    except Exception:
        pass
    try:
        win.bell()
    except Exception:
        pass


class AlertPopup(ctk.CTkToplevel):
    def __init__(
        self,
        master: ctk.CTk,
        *,
        chat_title: str,
        sender_name: str,
        message_text: str,
        chat_username: Optional[str] = None,
        chat_meta: Optional[str] = None,
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(master)
        self._on_close = on_close
        self.title("消息提醒")
        self.configure(fg_color=COLORS["bg"])
        self.attributes("-alpha", 0.0)
        self.resizable(True, True)
        self.minsize(*_ALERT_MINSIZE)
        self.geometry(_ALERT_GEOMETRY)
        _setup_popup_window(self, master)

        meta = (chat_meta or "").strip()

        wrap = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        wrap.pack(fill="both", expand=True, padx=12, pady=12)
        wrap.grid_columnconfigure(0, weight=1)

        row = 0
        ctk.CTkLabel(
            wrap,
            text=chat_title or "未知群组",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=COLORS["text"],
            anchor="w",
            justify="left",
            wraplength=_LABEL_WRAP,
        ).grid(row=row, column=0, sticky="ew", padx=14, pady=(12, 2))
        row += 1

        if meta:
            ctk.CTkLabel(
                wrap,
                text=f"群：{meta}",
                font=ctk.CTkFont(size=11),
                text_color=COLORS["accent"],
                anchor="w",
                justify="left",
                wraplength=_LABEL_WRAP,
            ).grid(row=row, column=0, sticky="ew", padx=14, pady=(0, 2))
            row += 1

        ctk.CTkLabel(
            wrap,
            text=sender_name or "用户",
            font=ctk.CTkFont(size=12),
            text_color=COLORS["muted"],
            anchor="w",
            justify="left",
            wraplength=_LABEL_WRAP,
        ).grid(row=row, column=0, sticky="ew", padx=14, pady=(0, 6))
        row += 1

        box = ctk.CTkTextbox(
            wrap,
            height=88,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color=COLORS["bg"],
            text_color=COLORS["text"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=8,
        )
        box.grid(row=row, column=0, sticky="ew", padx=14, pady=(0, 8))
        box.insert("1.0", message_text or "")
        box.configure(state="disabled")
        row += 1

        ctk.CTkButton(
            wrap,
            text="关闭",
            width=100,
            fg_color=COLORS["border"],
            hover_color=COLORS["card"],
            command=self._close,
        ).grid(row=row, column=0, sticky="w", padx=14, pady=(0, 12))

        self._fade_in_step(0)

    def _fade_in_step(self, step: int) -> None:
        alpha = min(1.0, 0.0 + step * 0.12)
        self.attributes("-alpha", alpha)
        if alpha < 1.0:
            self.after(20, lambda: self._fade_in_step(step + 1))

    def _close(self) -> None:
        if self._on_close:
            self._on_close()
        self.destroy()


def show_alert(
    master: ctk.CTk,
    *,
    chat_title: str,
    sender_name: str,
    message_text: str,
    chat_username: Optional[str] = None,
    chat_meta: Optional[str] = None,
) -> None:
    try:
        AlertPopup(
            master,
            chat_title=chat_title,
            sender_name=sender_name,
            message_text=message_text,
            chat_username=chat_username,
            chat_meta=chat_meta,
        )
    except Exception:
        pass


class StageReminderPopup(ctk.CTkToplevel):
    """定时任务阶段提醒：与监听弹窗相同置顶、响铃、淡入。"""

    def __init__(
        self,
        master: ctk.CTk,
        *,
        title: str,
        subtitle: str,
        body: str,
    ) -> None:
        super().__init__(master)
        self.title(title or "阶段提醒")
        self.configure(fg_color=COLORS["bg"])
        self.attributes("-alpha", 0.0)
        self.resizable(True, True)
        self.minsize(*_STAGE_MINSIZE)
        self.geometry(_STAGE_GEOMETRY)
        _setup_popup_window(self, master)

        wrap = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        wrap.pack(fill="both", expand=True, padx=12, pady=12)
        wrap.grid_columnconfigure(0, weight=1)

        row = 0
        ctk.CTkLabel(
            wrap,
            text=title or "阶段提醒",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=COLORS["accent"],
            anchor="w",
            justify="left",
            wraplength=_LABEL_WRAP,
        ).grid(row=row, column=0, sticky="ew", padx=14, pady=(12, 2))
        row += 1

        if subtitle:
            ctk.CTkLabel(
                wrap,
                text=subtitle,
                font=ctk.CTkFont(size=12),
                text_color=COLORS["muted"],
                anchor="w",
                justify="left",
                wraplength=_LABEL_WRAP,
            ).grid(row=row, column=0, sticky="ew", padx=14, pady=(0, 6))
            row += 1

        box = ctk.CTkTextbox(
            wrap,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color=COLORS["bg"],
            text_color=COLORS["text"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=8,
            height=88,
        )
        box.grid(row=row, column=0, sticky="ew", padx=14, pady=(0, 8))
        box.insert("1.0", body or "")
        box.configure(state="disabled")
        row += 1

        ctk.CTkButton(
            wrap,
            text="知道了",
            width=100,
            fg_color=COLORS["accent"],
            hover_color="#3d7ae6",
            command=self._close,
        ).grid(row=row, column=0, sticky="w", padx=14, pady=(0, 12))

        self._fade_in_step(0)

    def _fade_in_step(self, step: int) -> None:
        alpha = min(1.0, step * 0.12)
        self.attributes("-alpha", alpha)
        if alpha < 1.0:
            self.after(20, lambda: self._fade_in_step(step + 1))

    def _close(self) -> None:
        self.destroy()


def show_stage_reminder(master: ctk.CTk, *, title: str, subtitle: str, body: str) -> None:
    try:
        StageReminderPopup(master, title=title, subtitle=subtitle, body=body)
    except Exception:
        pass
