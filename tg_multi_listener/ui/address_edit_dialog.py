"""通讯录单条编辑弹窗（列表页只读，详情在此修改）。"""
from __future__ import annotations

from dataclasses import replace
from tkinter import messagebox
from typing import Callable, List, Optional

import customtkinter as ctk

from ..compat_config import AddressEntry, parse_chat_ref_input, parse_watch_user_input
from .theme import COLORS


class AddressEditDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: ctk.CTk | ctk.CTkFrame,
        *,
        entry: AddressEntry,
        owner_values: List[str],
        on_save: Callable[[AddressEntry], None],
        on_delete: Callable[[], None],
    ) -> None:
        super().__init__(master)
        self._entry = entry
        self._entry_id = entry.id
        self._on_save = on_save
        self._on_delete = on_delete
        title_name = (entry.remark or entry.id).strip() or entry.id
        self.title(f"编辑通讯录 · {title_name}")
        self.configure(fg_color=COLORS["bg"])
        self.geometry("480x520")
        self.attributes("-topmost", True)
        self.resizable(False, False)

        outer = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        outer.pack(fill="both", expand=True, padx=14, pady=14)
        frm = outer

        pad = {"anchor": "w", "padx": 14}
        ctk.CTkLabel(frm, text="备注（显示名）", text_color=COLORS["muted"]).pack(**pad, pady=(14, 4))
        self._remark = ctk.CTkEntry(frm, placeholder_text="如：客户群A")
        self._remark.insert(0, entry.remark or "")
        self._remark.pack(fill="x", padx=14, pady=(0, 8))

        ctk.CTkLabel(frm, text="群", text_color=COLORS["muted"]).pack(**pad, pady=(4, 4))
        self._chat = ctk.CTkEntry(frm, placeholder_text="数字 ID / @群名 / t.me 链接")
        self._chat.insert(0, entry.chat_ref or "")
        self._chat.pack(fill="x", padx=14, pady=(0, 8))

        ctk.CTkLabel(frm, text="监听用户（不参与监听可留空）", text_color=COLORS["muted"]).pack(**pad, pady=(4, 4))
        self._user = ctk.CTkEntry(frm, placeholder_text="数字 ID 或 @用户名")
        self._user.insert(0, entry.watch_user or "")
        self._user.pack(fill="x", padx=14, pady=(0, 8))

        self._listen_var = ctk.BooleanVar(value=bool(entry.listen_enabled))
        ctk.CTkCheckBox(
            frm,
            text="参与监听",
            variable=self._listen_var,
            text_color=COLORS["text"],
        ).pack(anchor="w", padx=14, pady=(4, 8))

        own_row = ctk.CTkFrame(frm, fg_color="transparent")
        own_row.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(own_row, text="归属账号", text_color=COLORS["muted"]).pack(side="left", padx=(0, 8))
        owner_vals = (["请选择"] + owner_values) if owner_values else ["请选择"]
        self._owner = ctk.CTkComboBox(own_row, width=180, values=owner_vals)
        cur = (entry.owner_account_id or "").strip()
        if cur and cur in owner_values:
            self._owner.set(cur)
        elif owner_values:
            self._owner.set(owner_values[0])
        else:
            self._owner.set("请选择")
        self._owner.pack(side="left")

        btn_row = ctk.CTkFrame(frm, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=(12, 14))
        ctk.CTkButton(
            btn_row,
            text="删除本条",
            width=100,
            fg_color=COLORS["danger"],
            hover_color="#b63a3a",
            command=self._confirm_delete,
        ).pack(side="left")
        ctk.CTkButton(btn_row, text="取消", width=88, fg_color=COLORS["border"], command=self.destroy).pack(side="right", padx=(8, 0))
        ctk.CTkButton(btn_row, text="保存", width=88, fg_color=COLORS["accent"], command=self._save).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        try:
            self.grab_set()
            self._remark.focus()
        except Exception:
            pass

    def _validate(self) -> Optional[AddressEntry]:
        remark = self._remark.get().strip()
        chat = self._chat.get().strip()
        user = self._user.get().strip()
        listen_on = bool(self._listen_var.get())
        if not remark or not chat:
            messagebox.showwarning("无法保存", "请填写备注与群标识。", parent=self)
            return None
        try:
            parse_chat_ref_input(chat)
        except ValueError as exc:
            messagebox.showwarning("无法保存", str(exc) or "群或频道标识无效。", parent=self)
            return None
        if listen_on:
            if not user:
                messagebox.showwarning("无法保存", "参与监听时，请填写要监听的用户。", parent=self)
                return None
            try:
                parse_watch_user_input(user)
            except ValueError:
                messagebox.showwarning("无法保存", "监听用户无效：请填写数字 ID 或 @用户名。", parent=self)
                return None
        elif user:
            try:
                parse_watch_user_input(user)
            except ValueError:
                messagebox.showwarning("无法保存", "用户格式无效。", parent=self)
                return None
        owner = self._owner.get().strip()
        if owner in ("", "—", "请选择"):
            messagebox.showwarning("无法保存", "请选择归属账号。", parent=self)
            return None
        return replace(
            self._entry,
            remark=remark,
            chat_ref=chat,
            watch_user=user,
            listen_enabled=listen_on,
            owner_account_id=owner,
        )

    def _save(self) -> None:
        updated = self._validate()
        if updated is None:
            return
        try:
            self.grab_release()
        except Exception:
            pass
        self._on_save(updated)
        self.destroy()

    def _confirm_delete(self) -> None:
        if not messagebox.askyesno("确认删除", "确定删除本条通讯录？相关定时任务目标会同步更新。", parent=self):
            return
        try:
            self.grab_release()
        except Exception:
            pass
        self._on_delete()
        self.destroy()
