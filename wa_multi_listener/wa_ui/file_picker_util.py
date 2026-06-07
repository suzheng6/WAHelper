"""文件选择对话框辅助。"""
from __future__ import annotations

import os
from tkinter import filedialog
from typing import Optional

import customtkinter as ctk


def txt_open_initial_dir(current_path: str = "") -> Optional[str]:
    """定时任务选 TXT：优先打开输入框里上次所选文件的目录；无则返回 None，由系统记住上次位置。"""
    cur = (current_path or "").strip()
    if not cur:
        return None
    d = os.path.dirname(os.path.abspath(cur))
    if os.path.isdir(d):
        return d
    if os.path.isdir(cur):
        return os.path.abspath(cur)
    return None


def pick_txt_or_folder(parent, *, current_path: str = "") -> Optional[str]:
    """弹出选择：TXT 文件或文件夹。返回绝对路径，取消则 None。"""
    result: list[str] = []
    initialdir = txt_open_initial_dir(current_path)

    dlg = ctk.CTkToplevel(parent)
    dlg.title("选择 TXT 或文件夹")
    dlg.resizable(False, False)
    dlg.transient(parent.winfo_toplevel())
    dlg.grab_set()

    ctk.CTkLabel(
        dlg,
        text="请选择定时任务文档来源：",
        font=ctk.CTkFont(size=14),
    ).pack(padx=24, pady=(20, 12))

    btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
    btn_row.pack(padx=24, pady=(0, 12))

    def _close() -> None:
        try:
            dlg.grab_release()
        except Exception:
            pass
        dlg.destroy()

    def _pick_file() -> None:
        kwargs = {
            "parent": dlg,
            "title": "选择 UTF-8 编码 TXT",
            "filetypes": [("文本", "*.txt"), ("所有文件", "*.*")],
        }
        if initialdir:
            kwargs["initialdir"] = initialdir
        path = filedialog.askopenfilename(**kwargs)
        if path:
            result.append(os.path.abspath(path))
        _close()

    def _pick_dir() -> None:
        kwargs = {"parent": dlg, "title": "选择包含按天 TXT 的文件夹"}
        if initialdir:
            kwargs["initialdir"] = initialdir
        path = filedialog.askdirectory(**kwargs)
        if path:
            result.append(os.path.abspath(path))
        _close()

    ctk.CTkButton(btn_row, text="选择 TXT 文件", width=140, command=_pick_file).pack(side="left", padx=(0, 8))
    ctk.CTkButton(btn_row, text="选择文件夹", width=140, command=_pick_dir).pack(side="left")
    ctk.CTkButton(dlg, text="取消", fg_color="#555", command=_close).pack(pady=(0, 16))

    dlg.update_idletasks()
    dlg.wait_window()
    return result[0] if result else None
