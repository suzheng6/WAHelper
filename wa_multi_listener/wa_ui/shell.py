"""整合壳：WhatsApp + Telegram 标签页。"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable, List, Optional

import customtkinter as ctk

_pkg_root = Path(__file__).resolve().parent.parent
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

from config import load_config
from legacy_tg_import import (
    default_legacy_tg_candidates,
    ensure_tg_data_ready,
    import_from_legacy_dir,
)
from logger_util import info
from platform_paths import combo_root, tg_data_root, wa_data_root
from startup_bootstrap import bootstrap_wa_logging, bootstrap_wa_runtime
from wa_ui.app import WaPanel
from wa_ui.theme import COLORS

MAIN_GEOMETRY = "1180x760"
APP_VERSION = "v2.0.3"
_AI_ROOT = _pkg_root.parent


class MessengerShell(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"超群小帮手 {APP_VERSION} · WhatsApp + Telegram")
        self.geometry(MAIN_GEOMETRY)
        self.resizable(False, False)
        self.configure(fg_color=COLORS["bg"])

        self._wa_listener: Any = None
        self._wa_schedule2: Any = None
        self._wa_coord_holder: List[Any] = [None]
        self._wa_panel: Optional[WaPanel] = None

        self._tg_listener: Any = None
        self._tg_scheduler: Any = None
        self._tg_coord_holder: List[Any] = [None]
        self._tg_panel: Any = None
        self._tg_mounted = False
        self._tg_shutdown_fn: Optional[Callable[[], None]] = None

        self._build_chrome()
        self.protocol("WM_DELETE_WINDOW", self._on_exit)

    def _build_chrome(self) -> None:
        top = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=0)
        top.pack(fill="x")
        ctk.CTkLabel(
            top,
            text="超群小帮手",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=COLORS["text"],
        ).pack(side="left", padx=16, pady=10)
        ctk.CTkButton(
            top,
            text="导入 TG 登录与配置…",
            width=160,
            fg_color=COLORS["border"],
            command=self._import_legacy_tg,
        ).pack(side="right", padx=8, pady=8)
        ctk.CTkLabel(
            top,
            text=f"数据：{combo_root()}",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["muted"],
        ).pack(side="right", padx=8)

        self._tabs = ctk.CTkTabview(self, fg_color=COLORS["bg"])
        self._tabs.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._tab_wa = self._tabs.add("WhatsApp")
        self._tab_tg = self._tabs.add("Telegram")
        self._tabs.set("WhatsApp")

        self._tab_wa.grid_rowconfigure(0, weight=1)
        self._tab_wa.grid_columnconfigure(0, weight=1)
        self._tab_tg.grid_rowconfigure(0, weight=1)
        self._tab_tg.grid_columnconfigure(0, weight=1)

        try:
            self._tabs.configure(command=self._on_tab_changed)
        except Exception:
            try:
                self._tabs._segmented_button.configure(command=self._on_tab_changed)
            except Exception:
                pass

    def _on_tab_changed(self, _value: str = "") -> None:
        try:
            if self._tabs.get() == "Telegram":
                self.after(50, self._ensure_tg_tab)
        except Exception:
            pass

    def _import_legacy_tg(self) -> None:
        initial = default_legacy_tg_candidates()
        start = initial[0] if initial and os.path.isdir(initial[0]) else combo_root()
        path = filedialog.askdirectory(
            title="选择文件夹（内含 config.json 与 sessions 登录文件）",
            initialdir=start,
            mustexist=True,
        )
        if not path:
            return
        ok, msg = import_from_legacy_dir(path)
        if ok:
            messagebox.showinfo("导入完成", msg, parent=self)
            info(msg)
            self._mount_tg_tab(force=True)
        else:
            messagebox.showerror("导入失败", msg, parent=self)
            info(f"导入失败：{msg}")

    def start_wa_backend(self) -> None:
        from listener import ListenerController
        from schedule2_runner import Schedule2Runner, pause_all_schedule2_jobs_on_startup
        from wa_coordinator import WaCoordinator

        pause_all_schedule2_jobs_on_startup()
        self._wa_listener = ListenerController()
        self._wa_schedule2 = Schedule2Runner()
        self._wa_panel = WaPanel(self._tab_wa, self._wa_listener, self._wa_schedule2)
        self._wa_panel.grid(row=0, column=0, sticky="nsew")

        cfg = load_config()

        def start() -> None:
            self._wa_listener.start(cfg, self._wa_panel.alert_callback)
            self._wa_schedule2.start(cfg)
            coord = WaCoordinator(self._wa_listener, self._wa_schedule2)
            self._wa_panel.bind_coordinator(coord)
            coord.start(cfg)
            self._wa_coord_holder[0] = coord

        self._wa_panel.after(0, start)

    def _ensure_tg_tab(self) -> None:
        if self._tg_mounted:
            return
        self._mount_tg_tab()

    def _show_tg_mount_error(self, msg: str) -> None:
        for w in self._tab_tg.winfo_children():
            try:
                w.destroy()
            except Exception:
                pass
        box = ctk.CTkFrame(self._tab_tg, fg_color=COLORS["card"])
        box.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)
        self._tab_tg.grid_rowconfigure(0, weight=1)
        self._tab_tg.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            box,
            text="Telegram 界面加载失败",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=COLORS["danger"],
        ).pack(anchor="w", padx=16, pady=(16, 8))
        ctk.CTkLabel(
            box,
            text=msg
            + "\n\n请查看 telegram/logs/app.log。\n"
            "新用户可直接在 Telegram 标签添加账号并登录；"
            "有旧版数据时可用右上角「导入 TG 登录与配置」。",
            font=ctk.CTkFont(size=13),
            text_color=COLORS["text"],
            justify="left",
            wraplength=720,
        ).pack(anchor="w", padx=16, pady=(0, 16))

    def _mount_tg_tab(self, *, force: bool = False) -> None:
        if self._tg_mounted and not force:
            return
        if force and self._tg_panel is not None:
            try:
                self._tg_panel.destroy()
            except Exception:
                pass
            self._tg_panel = None
            self._tg_mounted = False
            if self._tg_shutdown_fn:
                try:
                    self._tg_shutdown_fn()
                except Exception:
                    pass
            self._tg_shutdown_fn = None

        ensure_tg_data_ready()
        if str(_AI_ROOT) not in sys.path:
            sys.path.insert(0, str(_AI_ROOT))

        try:
            from tg_multi_listener.ui.embed import mount_tg_panel

            def register_shutdown(fn: Callable[[], None]) -> None:
                self._tg_shutdown_fn = fn

            panel, listener, scheduler, coord_holder = mount_tg_panel(
                self._tab_tg,
                tg_data_root(),
                on_register_shutdown=register_shutdown,
            )
            self._tg_panel = panel
            self._tg_listener = listener
            self._tg_scheduler = scheduler
            self._tg_coord_holder = coord_holder
            self._tg_mounted = True
            info("Telegram 标签页已挂载")
        except Exception:
            from logger_util import error

            err = traceback.format_exc()
            error(f"Telegram 标签加载失败：{err}")
            self._show_tg_mount_error(err.strip().splitlines()[-1] if err else "未知错误")

    def _on_exit(self) -> None:
        if self._wa_panel is not None:
            try:
                self._wa_panel._on_exit()
            except Exception as exc:
                from logger_util import error

                error(f"退出 WA 时：{exc}")
        if self._tg_shutdown_fn is not None:
            try:
                self._tg_shutdown_fn()
            except Exception as exc:
                from logger_util import error

                error(f"退出 TG 时：{exc}")
        self.destroy()


def run_shell() -> MessengerShell:
    bootstrap_wa_runtime()
    ensure_tg_data_ready()
    bootstrap_wa_logging()
    from neonize_bootstrap import install_patched_neonize_dll_if_present

    install_patched_neonize_dll_if_present()
    info(f"整合助手启动（WA 数据：{wa_data_root()}，TG 数据：{tg_data_root()}）")

    app = MessengerShell()
    app.after(0, app.start_wa_backend)
    # TG 仅在用户切换到 Telegram 标签时挂载（见 _on_tab_changed / _poll_active_tab）
    return app
