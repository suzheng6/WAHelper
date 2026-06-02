"""主线程弹窗：供后台登录线程阻塞获取手机号 / 验证码 / 密码。"""
from __future__ import annotations

import threading
from typing import Callable, Optional

import customtkinter as ctk

from .theme import COLORS


class StringInputDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: ctk.CTk,
        *,
        title: str,
        prompt: str,
        placeholder: str = "",
        secret: bool = False,
        on_submit: Callable[[Optional[str]], None],
    ) -> None:
        super().__init__(master)
        self._on_submit = on_submit
        self.title(title)
        self.configure(fg_color=COLORS["bg"])
        self.geometry("440x200")
        self.attributes("-topmost", True)
        self.resizable(False, False)

        frm = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        frm.pack(fill="both", expand=True, padx=14, pady=14)

        ctk.CTkLabel(frm, text=prompt, text_color=COLORS["text"], wraplength=400, justify="left").pack(
            anchor="w", padx=14, pady=(14, 8)
        )
        show_char = "*" if secret else ""
        self._entry = ctk.CTkEntry(frm, width=380, placeholder_text=placeholder or None, show=show_char)
        self._entry.pack(padx=14, pady=4)
        self._entry.focus()

        btn = ctk.CTkFrame(frm, fg_color="transparent")
        btn.pack(fill="x", padx=14, pady=14)

        def ok() -> None:
            v = self._entry.get().strip()
            self._finish(v if v else None)

        def cancel() -> None:
            self._finish(None)

        ctk.CTkButton(btn, text="取消", width=100, fg_color=COLORS["border"], command=cancel).pack(side="right", padx=(8, 0))
        ctk.CTkButton(btn, text="确定", width=100, fg_color=COLORS["accent"], command=ok).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", cancel)
        self.bind("<Return>", lambda _e: ok())
        try:
            self.grab_set()
        except Exception:
            pass

    def _finish(self, value: Optional[str]) -> None:
        try:
            self.grab_release()
        except Exception:
            pass
        self._on_submit(value)
        self.destroy()


class LoginUIBridge:
    """在非主线程中调用 ask_*，通过主线程弹窗获取输入。"""

    def __init__(self, root: ctk.CTk) -> None:
        self._root = root

    def ask_string(self, title: str, prompt: str, placeholder: str = "", secret: bool = False, timeout_sec: float = 900.0) -> Optional[str]:
        holder: dict[str, Optional[str]] = {"v": "__pending__"}
        done = threading.Event()

        def on_submit(val: Optional[str]) -> None:
            holder["v"] = val
            done.set()

        def open_dlg() -> None:
            StringInputDialog(
                self._root,
                title=title,
                prompt=prompt,
                placeholder=placeholder,
                secret=secret,
                on_submit=on_submit,
            )

        self._root.after(0, open_dlg)
        done.wait(timeout=timeout_sec)
        v = holder["v"]
        if v == "__pending__":
            return None
        return v
