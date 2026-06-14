"""主窗口：监听 + 定时任务（WhatsApp）。"""
from __future__ import annotations

import copy
import os
import queue
import re
import sys
import threading
import uuid
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

import customtkinter as ctk

_pkg_root = Path(__file__).resolve().parent.parent
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

from config import (
    Account,
    AddressEntry,
    apply_last_schedule_for_jobs,
    apply_last_schedule_from_current_jobs,
    format_job_targets_label,
    load_config,
    parse_chat_ref_input,
    parse_watch_user_input,
    save_config,
    sync_last_schedule_from_disk,
)
from listener import ListenerController
from logger_util import add_memory_listener, error, get_recent_lines, info, remove_memory_listener
from notifier import AlertPopup, show_stage_reminder
from stats import record_alert, today_alert_count
from paths import app_root, resource_path
from schedule_txt_import import import_doc_items
from schedule_account import mark_row_primary_auto, row_needs_per_group_owner
from schedule_folder import (
    bulk_delete_job_summary,
    can_advance_folder_day,
    entry_schedule_kind_hint,
    format_bulk_delete_confirm_message,
    format_folder_advance_line,
    folder_txt_abs_path,
    is_folder_job,
    schedule_kind_badge,
    scan_schedule_folder,
    taskmgr_job_file_label,
)
from schedule2_runner import (
    Schedule2Job,
    Schedule2Row,
    Schedule2Runner,
    LISTEN_HIT_PAUSE_REASON_S2,
    advance_schedule2_folder_day,
    bulk_resume_schedule2_counts,
    load_schedule2_jobs,
    save_schedule2_jobs,
    save_schedule2_jobs_patch,
    schedule2_job_is_running,
    schedule2_job_status_label,
    schedule2_job_step_label,
    schedule2_job_target_remarks,
)
from session_check import has_saved_session
from watch_membership_audit import WatchAuditRow, WatchAuditStatus
from wa_ui.qr_dialog import QrLoginDialog
from wa_ui.card_grid import TASKMGR_COLS, configure_equal_columns, grid_place, reorder_taskmgr_grid
from wa_ui.taskmgr_tile_theme import (
    format_taskmgr_count_summary,
    taskmgr_card_status_text,
    taskmgr_count_jobs,
    taskmgr_fonts,
    taskmgr_sort_jobs_for_display,
    taskmgr_tile_palette,
)
from wa_ui.log_textbox_util import (
    LOG_PUMP_IDLE_MS,
    LOG_PUMP_MS,
    LOG_TEXTBOX_MAX_LINES,
    append_log_line_capped,
    bind_log_textbox_wheel,
    reload_log_textbox_from_memory,
)
from wa_ui.file_picker_util import pick_txt_or_folder, txt_open_initial_dir
from wa_ui.scroll_util import (
    ADDRESS_LIST_HEIGHT,
    bind_scroll_tree_once,
    mount_bounded_list_scroll,
    mount_page_scroll,
    scroll_wheel,
)

TASKMGR_TICK_MS = 5000

from wa_ui.theme import COLORS, SIDEBAR_WIDTH
from wa_auth import run_qr_login_in_thread
from wa_proxy import normalize_proxy_url, proxy_supported

if TYPE_CHECKING:
    from wa_coordinator import WaCoordinator

MAIN_GEOMETRY = "1152x720"


class WaPanel(ctk.CTkFrame):
    def __init__(
        self,
        master: ctk.CTk | ctk.CTkFrame,
        listener: ListenerController,
        schedule2: Schedule2Runner,
    ) -> None:
        super().__init__(master)
        self._listener = listener
        self._schedule2 = schedule2
        self._coord: Optional[WaCoordinator] = None
        self._cfg = load_config()
        sync_last_schedule_from_disk(self._cfg)
        self._nav: Dict[str, ctk.CTkFrame] = {}
        self._content: Optional[ctk.CTkFrame] = None
        self._schedule2.set_reminder_callback(self._schedule2_reminder)
        self._log_queue: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._log_pump_on = True
        self._current_nav: str = "dash"
        self._login_dlg: Optional[QrLoginDialog] = None
        self._login_cancel: Optional[threading.Event] = None
        self._login_busy = False
        self._login_account_id: Optional[str] = None
        self._s2_edit_job_id: Optional[str] = None
        self._sched2_target_vars: List[ctk.BooleanVar] = []
        self._sched2_target_rows: List[Tuple[AddressEntry, ctk.BooleanVar]] = []
        self._pages_ready = False
        self._taskmgr_widgets: Dict[str, Dict[str, Any]] = {}
        self._taskmgr_listed_ids: List[str] = []
        self._taskmgr_display_ids: List[str] = []
        self._taskmgr_fp_cache: Dict[str, tuple] = {}
        self._taskmgr_toggle_busy = False
        self._watch_audit_flags: Dict[str, str] = {}
        self._watch_audit_details: Dict[str, str] = {}
        self._watch_audit_busy = False

        self.configure(fg_color=COLORS["bg"])
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._content = ctk.CTkFrame(self, fg_color=COLORS["bg"])
        self._content.grid(row=0, column=1, sticky="nsew", padx=18, pady=18)
        self._build_pages()
        self._pages_ready = True
        self._show_nav("dash")
        add_memory_listener(self._on_log_line)
        self.after(LOG_PUMP_MS, self._pump_logs)
        self.after(5000, self._tick_taskmgr_cards)

    def _build_sidebar(self) -> None:
        side = ctk.CTkFrame(self, width=SIDEBAR_WIDTH, fg_color=COLORS["sidebar"], corner_radius=0)
        side.grid(row=0, column=0, sticky="ns")
        side.grid_propagate(False)
        ctk.CTkLabel(side, text="WA 助手", font=ctk.CTkFont(size=20, weight="bold"), text_color=COLORS["text"]).pack(
            anchor="w", padx=16, pady=(20, 4)
        )
        ctk.CTkLabel(side, text="监听 · 定时群发", font=ctk.CTkFont(size=12), text_color=COLORS["muted"]).pack(anchor="w", padx=16, pady=(0, 16))
        for nid, label in (
            ("dash", "仪表盘"),
            ("acct", "账号管理"),
            ("grp", "通讯录"),
            ("rules", "监听规则"),
            ("sched", "定时任务"),
            ("taskmgr", "任务管理"),
            ("logs", "日志"),
        ):
            ctk.CTkButton(
                side,
                text=label,
                anchor="w",
                fg_color="transparent",
                hover_color=COLORS["border"],
                text_color=COLORS["text"],
                command=lambda n=nid: self._show_nav(n),
            ).pack(fill="x", padx=10, pady=2)
        ctk.CTkButton(
            side,
            text="保存并重载",
            fg_color=COLORS["accent"],
            hover_color="#1da851",
            command=self._save_and_reload,
        ).pack(side="bottom", fill="x", padx=12, pady=16)

    def _build_pages(self) -> None:
        assert self._content is not None
        self._nav = {
            "dash": self._page_dash(),
            "acct": self._page_accounts(),
            "grp": self._page_address(),
            "rules": self._page_rules(),
            "sched": self._page_schedule(),
            "taskmgr": self._page_task_manager(),
            "logs": self._page_logs(),
        }
        for f in self._nav.values():
            f.grid(row=0, column=0, sticky="nsew")
        self._content.grid_rowconfigure(0, weight=1)
        self._content.grid_columnconfigure(0, weight=1)

    def _show_nav(self, nav_id: str) -> None:
        self._current_nav = nav_id
        for k, f in self._nav.items():
            if k == nav_id:
                f.tkraise()
            else:
                f.grid_remove()
        self._nav[nav_id].grid(row=0, column=0, sticky="nsew")
        if nav_id == "dash":
            self._refresh_dashboard()
        elif nav_id == "acct":
            self._render_accounts()
        elif nav_id == "grp":
            self._refresh_owner_account_combos()
            self._render_address()
        elif nav_id == "sched":
            self._refresh_s2_target_checks()
        elif nav_id == "taskmgr":
            self._render_taskmgr_cards()
        elif nav_id == "logs":
            self._reload_logs_page()

    def bind_coordinator(self, coord: WaCoordinator) -> None:
        self._coord = coord

    def _schedule2_reminder(self, job_or_name: Any, step: int, note: str, paused_count: int = 0) -> None:
        job = job_or_name if isinstance(job_or_name, Schedule2Job) else None
        source_name = job.source_name if job else str(job_or_name)

        def show() -> None:
            record_alert()
            self._refresh_dashboard()
            grp = format_job_targets_label(load_config(), job) if job else ""
            body = (note or "").strip() or "请关注当前任务进度。"
            if paused_count > 0:
                body += f"\n\n已自动暂停 {paused_count} 个相关定时任务，请在「任务管理」页点卡片或「一键开始全部任务」。"
            subtitle = f"群：{grp} · 文档「{source_name or '定时任务'}」· 第 {step} 步" if grp else f"文档「{source_name}」· 第 {step} 步"
            try:
                show_stage_reminder(
                    self,
                    title="定时任务 · 阶段提醒",
                    subtitle=subtitle,
                    body=body,
                )
            except Exception as exc:
                error(f"阶段提醒弹窗失败：{exc}")
            self._render_taskmgr_cards()

        self.after(0, show)

    def alert_callback(self, payload: Dict[str, Any]) -> None:
        p = dict(payload)

        def run() -> None:
            record_alert()
            chat_key = str(p.get("chat_key", ""))
            paused = self._schedule2.pause_by_chat(
                chat_key,
                LISTEN_HIT_PAUSE_REASON_S2,
                event_title=str(p.get("chat_title", "")),
            )
            if paused > 0:
                info(f"监听命中：已自动暂停 {paused} 个定时任务，请手动点「继续」")
                self._render_taskmgr_cards()
            self._refresh_dashboard()
            try:
                AlertPopup(
                    self,
                    chat_title=str(p.get("chat_title", "")),
                    sender_name=str(p.get("sender_name", "")),
                    message_text=str(p.get("message_text", "")),
                    chat_jid=p.get("chat_jid"),
                )
            except Exception as exc:
                error(f"弹窗失败：{exc}")

        self.after(0, run)

    def shutdown_services(self, join_timeout: float = 10.0) -> None:
        from shutdown import shutdown_application

        if self._login_dlg:
            try:
                self._login_dlg.destroy()
            except Exception:
                pass
            self._login_dlg = None
        shutdown_application(
            coord=self._coord,
            listener=self._listener,
            schedule2=self._schedule2,
            login_cancel=self._login_cancel,
            join_timeout=join_timeout,
        )

    def shutdown_ui(self) -> None:
        self._log_pump_on = False
        remove_memory_listener(self._on_log_line)

    def _on_exit(self) -> None:
        try:
            self.shutdown_services(join_timeout=10.0)
        except Exception as exc:
            error(f"退出清理时：{exc}")
        self.shutdown_ui()

    def _on_log_line(self, line: str) -> None:
        try:
            self._log_queue.put_nowait(line)
        except Exception:
            pass

    def _drain_log_queue_to_textbox(self) -> None:
        lb = getattr(self, "_log_box", None)
        if lb is None:
            return
        while True:
            try:
                line = self._log_queue.get_nowait()
            except queue.Empty:
                break
            append_log_line_capped(lb, line, max_lines=LOG_TEXTBOX_MAX_LINES)

    def _reload_logs_page(self) -> None:
        lb = getattr(self, "_log_box", None)
        if lb is None:
            return
        reload_log_textbox_from_memory(
            lb,
            get_recent_lines,
            limit=LOG_TEXTBOX_MAX_LINES,
            max_lines=LOG_TEXTBOX_MAX_LINES,
            log_queue=getattr(self, "_log_queue", None),
        )

    def _pump_logs(self) -> None:
        if not self._log_pump_on:
            return
        on_logs = getattr(self, "_current_nav", "") == "logs"
        if on_logs:
            self._drain_log_queue_to_textbox()
        if self._log_pump_on:
            delay = LOG_PUMP_MS if on_logs else LOG_PUMP_IDLE_MS
            self.after(delay, self._pump_logs)

    def _save_and_reload(self) -> None:
        save_config(self._cfg)
        info("配置已保存，正在重载服务…")
        threading.Thread(
            target=self._reload_services, name="wa-reload", daemon=True
        ).start()

    def _reload_services(self) -> None:
        if self._coord:
            self._coord.stop(join_timeout=12.0)
        self._cfg = load_config()
        self._listener.start(self._cfg, self.alert_callback)
        self._schedule2.start(self._cfg)
        if self._coord:
            self._coord.start(self._cfg)
        self.after(0, self._on_services_reloaded)

    def _on_services_reloaded(self) -> None:
        info("服务已重载")
        self._render_accounts()
        self._refresh_dashboard()

    def _card(self, parent: ctk.CTkFrame, row: int) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        card.grid(row=row, column=0, sticky="ew", pady=8)
        card.grid_columnconfigure(0, weight=1)
        return card

    def _wrap_page(self, parent: ctk.CTkFrame, title: str) -> ctk.CTkFrame:
        inner, canvas, finish = mount_page_scroll(parent, bg=COLORS["bg"])
        self._scroll_wheel_handler = lambda e, c=canvas: scroll_wheel(c, e)
        inner.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            inner,
            text=title,
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color=COLORS["text"],
        ).grid(row=0, column=0, sticky="w", padx=4, pady=(8, 16))
        finish()
        return inner

    def _elastic_wraplabels(self, scroll_widget: ctk.CTkFrame, labels: List[ctk.CTkLabel], inset: int = 56) -> None:
        if not labels:
            return

        def sync(_event: Any = None) -> None:
            try:
                w = int(scroll_widget.winfo_width())
                if w <= inset + 80:
                    return
                wl = max(260, w - inset)
                for lb in labels:
                    lb.configure(wraplength=wl)
            except Exception:
                pass

        scroll_widget.bind("<Configure>", sync)
        self.after(200, sync)

    def _refresh_dashboard(self) -> None:
        if not getattr(self, "_pages_ready", False):
            return
        n = today_alert_count()
        if hasattr(self, "_dash_today"):
            self._dash_today.configure(text=f"今日已提醒 {n} 次")
        listen_on = bool(self._cfg.listening_enabled)
        online = len(self._online_account_ids())
        conn = self._listener.is_running() and online > 0
        parts = [
            f"监听总开关：{'开' if listen_on else '关'}",
            f"WhatsApp 连接：{'已连接' if conn else '未连接'}",
        ]
        if listen_on and conn:
            parts.append("（正在监听配置中的群与用户）")
        elif listen_on and not conn:
            parts.append("（总开关已开但未连上，请检查账号/代理）")
        if hasattr(self, "_dash_listen"):
            self._dash_listen.configure(text="  ·  ".join(parts))
        lines = []
        for a in self._cfg.accounts:
            if a.id in self._online_account_ids():
                st_txt = "已连接"
            elif has_saved_session(a):
                st_txt = "已登录（未连接）"
            else:
                st_txt = "未登录"
            lines.append(f"{a.id} · {st_txt} · {'启用' if a.enabled else '停用'}")
        if hasattr(self, "_dash_acct"):
            self._dash_acct.configure(text="\n".join(lines) if lines else "（未配置账号）")

    # --- 仪表盘 ---
    def _page_dash(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._content, fg_color="transparent")
        wrap = self._wrap_page(page, "仪表盘")

        c1 = self._card(wrap, 1)
        ctk.CTkLabel(c1, text="今日提醒次数", font=ctk.CTkFont(size=13, weight="bold"), text_color=COLORS["text"]).grid(
            row=0, column=0, sticky="w", padx=16, pady=(12, 4)
        )
        self._dash_today = ctk.CTkLabel(c1, text="", font=ctk.CTkFont(size=14), text_color=COLORS["text"])
        self._dash_today.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 12))

        c2 = self._card(wrap, 2)
        ctk.CTkLabel(c2, text="监听状态", font=ctk.CTkFont(size=13, weight="bold"), text_color=COLORS["text"]).grid(
            row=0, column=0, sticky="w", padx=16, pady=(12, 4)
        )
        self._dash_listen = ctk.CTkLabel(c2, text="", font=ctk.CTkFont(size=14), text_color=COLORS["muted"], justify="left")
        self._dash_listen.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 12))

        c3 = self._card(wrap, 3)
        ctk.CTkLabel(c3, text="账号摘要", font=ctk.CTkFont(size=13, weight="bold"), text_color=COLORS["text"]).grid(
            row=0, column=0, sticky="w", padx=16, pady=(12, 4)
        )
        self._dash_acct = ctk.CTkLabel(
            c3,
            text="",
            font=ctk.CTkFont(size=14),
            text_color=COLORS["muted"],
            justify="left",
            anchor="w",
        )
        self._dash_acct.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 12))

        self._elastic_wraplabels(wrap, [self._dash_listen, self._dash_acct])
        self._refresh_dashboard()
        return page

    # --- 账号 ---
    def _page_accounts(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._content, fg_color="transparent")
        inner, canvas, finish = mount_page_scroll(page, bg=COLORS["bg"])
        self._scroll_wheel_handler = lambda e, c=canvas: scroll_wheel(c, e)
        ctk.CTkLabel(inner, text="账号管理", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLORS["text"]).pack(anchor="w", pady=8)
        ctk.CTkLabel(
            inner,
            text="账号简称用于定时 TXT 中的「账号=xxx」。点「登录」将清除该账号旧会话并扫码，会话保存在 sessions/。"
            "每账号可填独立 SOCKS5 代理（socks5://IP:端口:用户名:密码）。",
            text_color=COLORS["muted"],
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        proxy_hint = "代理：已支持（neonize 含 SetProxyAddress）" if proxy_supported() else (
            "代理：已保存配置；当前 neonize 未含 SetProxy 导出，请将补丁 DLL 放到程序目录后重启"
        )
        ctk.CTkLabel(inner, text=proxy_hint, text_color=COLORS["muted"], wraplength=700, justify="left").pack(
            anchor="w", pady=(0, 12)
        )

        form = ctk.CTkFrame(inner, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        form.pack(fill="x", pady=8)
        self._acc_id = ctk.CTkEntry(form, placeholder_text="账号简称，如 主号")
        self._acc_id.pack(fill="x", padx=12, pady=(12, 6))
        self._acc_phone = ctk.CTkEntry(form, placeholder_text="备注手机号（可选）")
        self._acc_phone.pack(fill="x", padx=12, pady=(0, 6))
        self._acc_proxy = ctk.CTkEntry(
            form,
            placeholder_text="SOCKS5 代理（可选）socks5://IP:端口:用户:密码",
        )
        self._acc_proxy.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(form, text="添加账号", fg_color=COLORS["accent"], command=self._add_account).pack(fill="x", padx=12, pady=(0, 12))

        self._acc_list = ctk.CTkFrame(inner, fg_color="transparent")
        self._acc_list.pack(fill="x")
        self._render_accounts()
        finish()
        return page

    def _online_account_ids(self) -> Set[str]:
        if self._coord is not None:
            return self._coord.connected_account_ids()
        return set()

    def _account_status_text(self, acc: Account) -> tuple[str, str]:
        if self._login_busy and acc.id == self._login_account_id:
            return "登录中…", COLORS["accent"]
        if acc.id in self._online_account_ids():
            return "已连接", COLORS["success"]
        if has_saved_session(acc):
            return "已登录（未连接）", "#e6c84f"
        return "未登录", COLORS["danger"]

    def _render_accounts(self) -> None:
        for w in self._acc_list.winfo_children():
            w.destroy()
        handler = getattr(self, "_scroll_wheel_handler", None)
        for acc in self._cfg.accounts:
            row = ctk.CTkFrame(self._acc_list, fg_color=COLORS["card"], corner_radius=10, border_width=1, border_color=COLORS["border"])
            row.pack(fill="x", pady=4)
            var = ctk.BooleanVar(value=acc.enabled)
            def _on_enabled_toggle(a: Account = acc, v: ctk.BooleanVar = var) -> None:
                a.enabled = bool(v.get())
                save_config(self._cfg)
                self._refresh_owner_account_combos()

            ctk.CTkCheckBox(row, text="", variable=var, width=24, command=_on_enabled_toggle).pack(side="left", padx=8)
            mid = ctk.CTkFrame(row, fg_color="transparent")
            mid.pack(side="left", fill="x", expand=True, padx=4, pady=6)
            ctk.CTkLabel(mid, text=acc.id, font=ctk.CTkFont(size=14, weight="bold"), text_color=COLORS["text"], anchor="w").pack(anchor="w")
            sub = acc.phone or "未填备注手机号"
            st_txt, st_color = self._account_status_text(acc)
            ctk.CTkLabel(mid, text=f"{sub} · {st_txt}", text_color=st_color, anchor="w").pack(anchor="w")
            proxy_row = ctk.CTkFrame(mid, fg_color="transparent")
            proxy_row.pack(fill="x", pady=(4, 0))
            proxy_ent = ctk.CTkEntry(
                proxy_row,
                placeholder_text="SOCKS5 代理（留空=直连）",
                height=28,
            )
            if acc.proxy:
                proxy_ent.insert(0, acc.proxy)
            proxy_ent.pack(side="left", fill="x", expand=True)

            def _save_proxy(a: Account = acc, ent: ctk.CTkEntry = proxy_ent) -> None:
                raw = ent.get().strip()
                if raw:
                    try:
                        normalize_proxy_url(raw)
                    except ValueError as exc:
                        info(f"账号「{a.id}」代理无效：{exc}")
                        return
                a.proxy = raw
                save_config(self._cfg)
                if raw:
                    info(f"账号「{a.id}」代理已保存")

            ctk.CTkButton(proxy_row, text="保存代理", width=72, height=28, command=_save_proxy).pack(side="right", padx=(6, 0))
            login_state = "normal"
            if self._login_busy:
                login_state = "disabled"
            ctk.CTkButton(
                row,
                text="登录",
                width=72,
                fg_color=COLORS["accent"],
                state=login_state,
                command=lambda a=acc: self._login_account(a),
            ).pack(side="right", padx=4, pady=6)
            ctk.CTkButton(row, text="删除", width=60, fg_color=COLORS["danger"], command=lambda a=acc: self._del_account(a)).pack(side="right", padx=4, pady=6)
        if handler:
            bind_scroll_tree_once(self._acc_list, handler)

    def _add_account(self) -> None:
        aid = self._acc_id.get().strip()
        if not aid:
            info("请填写账号简称")
            return
        if any(a.id == aid for a in self._cfg.accounts):
            info("账号已存在")
            return
        proxy_raw = self._acc_proxy.get().strip()
        if proxy_raw:
            try:
                normalize_proxy_url(proxy_raw)
            except ValueError as exc:
                info(f"代理格式无效：{exc}")
                return
        self._cfg.accounts.append(
            Account(
                id=aid,
                session_name=aid,
                enabled=True,
                phone=self._acc_phone.get().strip(),
                proxy=proxy_raw,
            )
        )
        save_config(self._cfg)
        self._schedule2.refresh_accounts(self._cfg)
        self._acc_id.delete(0, "end")
        self._acc_phone.delete(0, "end")
        self._acc_proxy.delete(0, "end")
        self._render_accounts()
        self._refresh_owner_account_combos()
        info(f"已添加账号 {aid}")

    def _del_account(self, acc: Account) -> None:
        self._cfg.accounts = [a for a in self._cfg.accounts if a.id != acc.id]
        save_config(self._cfg)
        self._render_accounts()

    def _login_account(self, acc: Account) -> None:
        if self._login_busy:
            info("已有登录进行中，请稍候")
            return
        if self._login_dlg:
            try:
                self._login_dlg.destroy()
            except Exception:
                pass
        self._login_cancel = threading.Event()
        self._login_busy = True
        self._login_account_id = acc.id
        self._render_accounts()
        login_acc = acc

        def on_qr(data: bytes) -> None:
            def show() -> None:
                if self._login_dlg:
                    self._login_dlg.update_qr(data)

            self.after(0, show)

        def on_status(text: str) -> None:
            self.after(0, lambda: self._login_dlg.set_status(text) if self._login_dlg else None)

        def on_done(ok: bool, msg: str) -> None:
            def finish() -> None:
                dlg = self._login_dlg
                self._login_dlg = None
                if dlg:
                    try:
                        dlg.grab_release()
                    except Exception:
                        pass
                    try:
                        dlg.withdraw()
                    except Exception:
                        pass
                    try:
                        dlg.destroy()
                    except Exception:
                        pass
                self._login_busy = False
                self._login_account_id = None
                self._render_accounts()
                self.update_idletasks()
                if ok:
                    info(f"账号「{login_acc.id}」{msg}，正在上线（其它账号保持连接）")
                    save_config(self._cfg)
                    # 等待后台释放登录客户端的数据库锁后再连接
                    self.after(1500, lambda: self._on_account_logged_in(login_acc.id))
                else:
                    info(f"登录未完成：{msg}")
                    if msg and msg != "已取消":
                        messagebox.showerror("登录失败", msg)

            try:
                self.after(0, finish)
            except Exception:
                finish()

        def on_cancel() -> None:
            if self._login_cancel:
                self._login_cancel.set()

        def before_connect() -> None:
            if self._coord:
                self._coord.prepare_for_login(login_acc.id)

        def start_login_thread() -> None:
            info(f"账号「{login_acc.id}」：清除旧会话并等待扫码…")
            run_qr_login_in_thread(
                login_acc,
                on_qr,
                on_done,
                cancel_event=self._login_cancel,
                on_status=on_status,
                before_connect=before_connect,
            )

        self._login_dlg = QrLoginDialog(self, account_id=login_acc.id, on_cancel=on_cancel)
        self._login_dlg.set_status("正在准备…")
        threading.Thread(
            target=start_login_thread, name="wa-login-prep", daemon=True
        ).start()

    def _on_account_logged_in(self, account_id: str) -> None:
        self._cfg = load_config()
        self._schedule2.refresh_accounts(self._cfg)
        if self._coord:
            self._coord.connect_account_after_login(self._cfg, account_id)
        self._render_accounts()
        self._refresh_owner_account_combos()
        self._refresh_dashboard()

    # --- 通讯录 ---
    def _page_address(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._content, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(page, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(header, text="通讯录", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLORS["text"]).pack(anchor="w", pady=(8, 4))
        ctk.CTkLabel(
            header,
            text="底部填写后点「添加」；列表内可改备注/归属、↑↓ 调序。群填邀请链接或 @g.us；监听填手机号（含国家码），仅定时可留空。",
            text_color=COLORS["muted"],
            wraplength=680,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))

        list_host = ctk.CTkFrame(page, fg_color="transparent")
        list_host.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        list_card = ctk.CTkFrame(list_host, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        list_card.pack(fill="both", expand=True)
        ctk.CTkLabel(list_card, text="通讯录列表", text_color=COLORS["muted"]).pack(anchor="w", padx=12, pady=(10, 4))
        list_inner, list_canvas, finish_list, _shell = mount_bounded_list_scroll(
            list_card, height=ADDRESS_LIST_HEIGHT, bg=COLORS["bg"]
        )
        self._ab_list_scroll_handler = lambda e, c=list_canvas: scroll_wheel(c, e)
        self._ab_rows = ctk.CTkFrame(list_inner, fg_color="transparent")
        self._ab_rows.pack(fill="x")
        self._ab_scroll_bound = False
        self._render_address()
        finish_list()

        addr_foot = ctk.CTkFrame(page, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        addr_foot.grid(row=2, column=0, sticky="ew")
        form = ctk.CTkFrame(addr_foot, fg_color="transparent")
        form.pack(fill="x", padx=10, pady=8)

        def _addr_field_row(label: str, placeholder: str) -> ctk.CTkEntry:
            row = ctk.CTkFrame(form, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=label, text_color=COLORS["muted"], width=52, anchor="w").pack(side="left", padx=(0, 6))
            ent = ctk.CTkEntry(row, placeholder_text=placeholder, height=28)
            ent.pack(side="left", fill="x", expand=True)
            return ent

        self._ab_remark = _addr_field_row("备注", "显示名")
        self._ab_chat = _addr_field_row("群", "JID 或邀请链接")
        self._ab_user = _addr_field_row("用户", "手机号，可不填")

        action = ctk.CTkFrame(form, fg_color="transparent")
        action.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(action, text="归属", text_color=COLORS["muted"], width=52, anchor="w").pack(side="left", padx=(0, 6))
        acc_own = self._owner_account_values()
        self._ab_owner = ctk.CTkComboBox(action, width=88, height=28, values=acc_own or ["—"])
        self._ab_owner.pack(side="left", padx=(0, 8))
        if acc_own:
            self._ab_owner.set(acc_own[0])
        else:
            self._ab_owner.set("—")
        self._ab_listen = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            action,
            text="监听",
            variable=self._ab_listen,
            text_color=COLORS["text"],
            width=56,
            checkbox_height=18,
            checkbox_width=18,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            action,
            text="添加",
            width=64,
            height=28,
            fg_color=COLORS["accent"],
            command=self._add_address,
        ).pack(side="left")
        return page

    def _owner_account_values(self) -> List[str]:
        """通讯录归属账号：列出账号管理中全部账号（不限于勾选启用）。"""
        return [a.id for a in self._cfg.accounts]

    def _refresh_owner_account_combos(self) -> None:
        vals = self._owner_account_values()
        if not getattr(self, "_ab_owner", None):
            return
        cur = self._ab_owner.get().strip()
        if vals:
            self._ab_owner.configure(values=vals)
            self._ab_owner.set(cur if cur in vals else vals[0])
        else:
            self._ab_owner.configure(values=["—"])
            self._ab_owner.set("—")

    def _after_address_book_order_changed(self) -> None:
        self._render_address()
        if hasattr(self, "_s2_targets"):
            self._refresh_s2_target_checks()

    def _move_address_book_entry(self, idx: int, delta: int) -> None:
        book = self._cfg.address_book
        j = idx + delta
        if idx < 0 or idx >= len(book) or j < 0 or j >= len(book):
            return
        book[idx], book[j] = book[j], book[idx]
        save_config(self._cfg)
        self._after_address_book_order_changed()

    def _persist_address_remark(self, idx: int, remark: str) -> None:
        text = (remark or "").strip() or "未命名"
        if idx < 0 or idx >= len(self._cfg.address_book):
            return
        ent = self._cfg.address_book[idx]
        if ent.remark == text:
            return
        ent.remark = text
        save_config(self._cfg)
        if hasattr(self, "_s2_targets"):
            self._refresh_s2_target_checks()
        info(f"备注已更新：{text}")

    def _persist_address_owner(self, idx: int, owner_id: str) -> None:
        """通讯录归属账号变更后立即写入 config.json。"""
        val = (owner_id or "").strip()
        if not val or val in ("—", "请选择"):
            return
        if idx < 0 or idx >= len(self._cfg.address_book):
            return
        ent = self._cfg.address_book[idx]
        if ent.owner_account_id == val:
            return
        ent.owner_account_id = val
        save_config(self._cfg)
        if hasattr(self, "_s2_targets"):
            self._refresh_s2_target_checks()
        self._render_address()

    def _render_address(self) -> None:
        for w in self._ab_rows.winfo_children():
            w.destroy()
        acc_vals = self._owner_account_values()
        n_book = len(self._cfg.address_book)
        for i, ent in enumerate(self._cfg.address_book):
            audit_extra, border_color = self._watch_audit_display(ent)
            row = ctk.CTkFrame(
                self._ab_rows,
                fg_color=COLORS["card"],
                corner_radius=10,
                border_width=1,
                border_color=border_color,
            )
            row.pack(fill="x", pady=4)
            tag = "监听" if ent.listen_enabled else "仅定时"
            owner_txt = (ent.owner_account_id or "").strip() or "未选择"
            head = ctk.CTkFrame(row, fg_color="transparent")
            head.pack(fill="x", padx=12, pady=(8, 0))
            title_txt = f"{i + 1}. {ent.remark or ent.id}"
            if self._watch_audit_flags.get(ent.id) == WatchAuditStatus.ABSENT.value:
                title_txt += "  ⚠不在群"
            ctk.CTkLabel(
                head,
                text=title_txt,
                font=ctk.CTkFont(size=14, weight="bold"),
                text_color=COLORS["danger"] if audit_extra else COLORS["text"],
            ).pack(side="left")
            order_btns = ctk.CTkFrame(head, fg_color="transparent")
            order_btns.pack(side="right")
            ctk.CTkButton(
                order_btns,
                text="↑",
                width=36,
                height=28,
                fg_color=COLORS["border"],
                state="normal" if i > 0 else "disabled",
                command=lambda idx=i: self._move_address_book_entry(idx, -1),
            ).pack(side="left", padx=(0, 4))
            ctk.CTkButton(
                order_btns,
                text="↓",
                width=36,
                height=28,
                fg_color=COLORS["border"],
                state="normal" if i < n_book - 1 else "disabled",
                command=lambda idx=i: self._move_address_book_entry(idx, 1),
            ).pack(side="left")
            ctk.CTkLabel(
                row,
                text=f"{tag}\n群: {ent.chat_ref}\n用户: {ent.watch_user or '—'} · 主号→{owner_txt}{audit_extra}",
                text_color=COLORS["danger"] if audit_extra else COLORS["text"],
                justify="left",
            ).pack(anchor="w", padx=12, pady=(10, 4))
            ctrl = ctk.CTkFrame(row, fg_color="transparent")
            ctrl.pack(fill="x", padx=12, pady=(0, 10))
            remark_row = ctk.CTkFrame(ctrl, fg_color="transparent")
            remark_row.pack(fill="x", pady=(0, 6))
            ctk.CTkLabel(remark_row, text="备注", text_color=COLORS["muted"]).pack(side="left", padx=(0, 8))
            remark_entry = ctk.CTkEntry(remark_row, width=220)
            remark_entry.insert(0, ent.remark or "")
            remark_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

            def make_remark_save(idx: int, entry: ctk.CTkEntry):
                def _save() -> None:
                    self._persist_address_remark(idx, entry.get())

                return _save

            ctk.CTkButton(
                remark_row,
                text="保存备注",
                width=88,
                fg_color=COLORS["border"],
                command=make_remark_save(i, remark_entry),
            ).pack(side="left")
            own_row = ctk.CTkFrame(ctrl, fg_color="transparent")
            own_row.pack(fill="x", pady=(0, 6))
            ctk.CTkLabel(own_row, text="归属账号", text_color=COLORS["muted"]).pack(side="left", padx=(0, 8))

            def make_owner_change(idx: int):
                def on_pick(choice: str) -> None:
                    self._persist_address_owner(idx, choice)

                return on_pick

            vals = (["请选择"] + acc_vals) if acc_vals else ["请选择"]
            own_combo = ctk.CTkComboBox(own_row, width=140, values=vals, command=make_owner_change(i))
            cur_own = (ent.owner_account_id or "").strip()
            if cur_own and cur_own in acc_vals:
                own_combo.set(cur_own)
            else:
                own_combo.set("请选择")
            own_combo.pack(side="left")
            ctk.CTkButton(ctrl, text="删除", fg_color=COLORS["danger"], command=lambda e=ent: self._del_address(e)).pack(
                fill="x", pady=(4, 0)
            )
        if not getattr(self, "_ab_scroll_bound", False):
            self._ab_scroll_bound = True
            handler = getattr(self, "_ab_list_scroll_handler", None)
            if handler:
                bind_scroll_tree_once(self._ab_rows, handler)

    def _commit_address_book(self, *, resolve_online: bool = True) -> None:
        save_config(self._cfg)
        self._render_address()
        if hasattr(self, "_s2_targets"):
            self._refresh_s2_target_checks()
        if not resolve_online:
            return
        if self._coord and self._coord.has_connected_clients():
            self._coord.apply_config_hot(self._cfg)
            self.after(800, self._render_address)
            info("通讯录已更新：已解析群链接并刷新监听")
        else:
            info("通讯录已保存；账号在线后将自动解析群链接并启用监听")

    def _add_address(self) -> None:
        try:
            chat_ref = parse_chat_ref_input(self._ab_chat.get())
            watch_user = ""
            if self._ab_listen.get():
                raw_u = self._ab_user.get().strip()
                if raw_u:
                    watch_user = parse_watch_user_input(raw_u)
                elif not self._ab_user.get().strip():
                    info("参与监听时请填写监听用户手机号")
                    return
        except ValueError as exc:
            info(str(exc))
            return
        acc_vals = self._owner_account_values()
        if not acc_vals:
            info("请先在「账号管理」添加至少一个账号。")
            return
        owner_acc = self._ab_owner.get().strip() if getattr(self, "_ab_owner", None) else ""
        if owner_acc in ("", "—", "请选择"):
            owner_acc = acc_vals[0]
        if owner_acc not in acc_vals:
            info("请选择该群的主号/归属账号。")
            return
        ent = AddressEntry(
            id=uuid.uuid4().hex[:12],
            remark=self._ab_remark.get().strip() or "未命名",
            chat_ref=chat_ref,
            watch_user=watch_user,
            listen_enabled=bool(self._ab_listen.get()),
            owner_account_id=owner_acc,
        )
        self._cfg.address_book.append(ent)
        self._ab_remark.delete(0, "end")
        self._ab_chat.delete(0, "end")
        self._ab_user.delete(0, "end")
        self._ab_listen.set(True)
        if acc_vals:
            self._ab_owner.set(acc_vals[0])
        try:
            self._commit_address_book()
            info(f"已添加通讯录：{ent.remark}（主号→{owner_acc}）")
        except Exception as exc:
            error(f"添加通讯录失败：{exc}")
            self._cfg.address_book = [e for e in self._cfg.address_book if e.id != ent.id]

    def _del_address(self, ent: AddressEntry) -> None:
        self._cfg.address_book = [e for e in self._cfg.address_book if e.id != ent.id]
        self._commit_address_book()

    # --- 监听规则 ---
    def _page_rules(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._content, fg_color="transparent")
        wrap = ctk.CTkScrollableFrame(page, fg_color="transparent")
        wrap.pack(fill="both", expand=True)
        ctk.CTkLabel(wrap, text="监听规则", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLORS["text"]).pack(anchor="w", pady=8)
        box = ctk.CTkFrame(wrap, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        box.pack(fill="x")
        self._listen_var = ctk.BooleanVar(value=self._cfg.listening_enabled)
        ctk.CTkCheckBox(
            box,
            text="启用消息监听",
            variable=self._listen_var,
            command=lambda: setattr(self._cfg, "listening_enabled", self._listen_var.get()),
            text_color=COLORS["text"],
        ).pack(anchor="w", padx=14, pady=12)
        ctk.CTkLabel(box, text="同群限流（秒）", text_color=COLORS["muted"]).pack(anchor="w", padx=14)
        self._rate_entry = ctk.CTkEntry(box)
        self._rate_entry.insert(0, str(self._cfg.rate_limit_seconds))
        self._rate_entry.pack(fill="x", padx=14, pady=(4, 14))
        self._rate_entry.bind("<FocusOut>", lambda _e: self._save_rate())

        sync_box = ctk.CTkFrame(wrap, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        sync_box.pack(fill="x", pady=(12, 0))
        ctk.CTkLabel(
            sync_box,
            text="「上次任务→」在添加定时任务时自动更新；删除任务管理卡片不会改动。"
            "需要与当前任务列表对齐时，点下方按钮手动同步（有任务写入文件名，无任务显示为空）。",
            text_color=COLORS["muted"],
            wraplength=640,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(12, 8))
        ctk.CTkButton(
            sync_box,
            text="从任务管理同步上次任务",
            fg_color=COLORS["accent"],
            hover_color="#1da851",
            command=self._sync_last_schedule_from_jobs,
        ).pack(fill="x", padx=14, pady=(0, 14))

        audit_box = ctk.CTkFrame(wrap, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        audit_box.pack(fill="x", pady=(12, 0))
        ctk.CTkLabel(
            audit_box,
            text="检测所有「参与监听」的群：若监听用户已不在成员列表中，会在「通讯录」对应条目标红提示，便于清理。",
            text_color=COLORS["muted"],
            wraplength=640,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(12, 8))
        ctk.CTkButton(
            audit_box,
            text="检测不在群内的监听用户",
            fg_color=COLORS["accent"],
            hover_color="#1da851",
            command=self._check_watch_memberships,
        ).pack(fill="x", padx=14, pady=(0, 14))
        return page

    def _watch_audit_display(self, ent: AddressEntry) -> Tuple[str, str]:
        st = self._watch_audit_flags.get(ent.id, "")
        if st == WatchAuditStatus.ABSENT.value:
            return (
                "\n⚠ 用户不在群内，建议清空监听用户或删除条目",
                COLORS["danger"],
            )
        if st == WatchAuditStatus.ERROR.value:
            detail = (self._watch_audit_details.get(ent.id) or "").strip()
            msg = detail or "群或用户解析失败"
            return (f"\n⚠ 未能检测：{msg}", COLORS["border"])
        if st == WatchAuditStatus.OFFLINE.value:
            return ("\n⚠ 归属账号未在线，未检测", COLORS["border"])
        return "", COLORS["border"]

    def _check_watch_memberships(self) -> None:
        if getattr(self, "_watch_audit_busy", False):
            info("成员检测进行中，请稍候…")
            return
        if not self._coord or not self._coord.has_connected_clients():
            info("请先登录 WhatsApp 账号并点「保存并重载服务」后再检测。")
            return
        self._watch_audit_busy = True
        info("正在检测各群监听用户是否在群内…")

        def on_done(result: Dict[str, WatchAuditRow]) -> None:
            def finish() -> None:
                self._watch_audit_busy = False
                self._apply_watch_membership_audit(result)

            self.after(0, finish)

        if not self._coord.request_watch_membership_audit(self._cfg, on_done):
            self._watch_audit_busy = False
            info("当前无在线账号，无法检测群成员。")

    def _apply_watch_membership_audit(self, result: Dict[str, WatchAuditRow]) -> None:
        flags: Dict[str, str] = {eid: row.status.value for eid, row in result.items()}
        details: Dict[str, str] = {
            eid: (row.detail or "").strip()
            for eid, row in result.items()
            if (row.detail or "").strip()
        }
        ok = sum(1 for r in result.values() if r.status == WatchAuditStatus.OK)
        absent = sum(1 for r in result.values() if r.status == WatchAuditStatus.ABSENT)
        err = sum(1 for r in result.values() if r.status == WatchAuditStatus.ERROR)
        offline = sum(1 for r in result.values() if r.status == WatchAuditStatus.OFFLINE)
        self._watch_audit_flags = flags
        self._watch_audit_details = details
        self._render_address()
        if not result:
            info("检测未完成：请确认 WhatsApp 已登录并点「保存并重载服务」后重试。")
            return
        parts = [f"已检测 {ok + absent} 条监听绑定"]
        if absent:
            parts.append(f"{absent} 条用户不在群内（通讯录已标红）")
        if err:
            parts.append(f"{err} 条检测失败")
            sample = [
                (ent.remark or ent.id, details.get(ent.id, ""))
                for ent in self._cfg.address_book
                if flags.get(ent.id) == WatchAuditStatus.ERROR.value and details.get(ent.id)
            ][:3]
            for name, det in sample:
                info(f"  失败示例：{name} → {det}")
        if offline:
            parts.append(f"{offline} 条归属账号未在线")
        info("；".join(parts) + "。请到「通讯录」查看。")

    def _sync_last_schedule_from_jobs(self) -> None:
        from schedule2_runner import load_schedule2_jobs

        before = {e.id: (e.last_schedule_source_name or "").strip() for e in self._cfg.address_book}
        n_jobs = len(load_schedule2_jobs())
        apply_last_schedule_from_current_jobs(self._cfg)
        n_changed = sum(
            1
            for e in self._cfg.address_book
            if before.get(e.id, "") != (e.last_schedule_source_name or "").strip()
        )
        n_filled = sum(1 for e in self._cfg.address_book if (e.last_schedule_source_name or "").strip())
        self._refresh_s2_target_checks()
        if n_changed:
            info(
                f"已同步「上次任务」：任务管理 {n_jobs} 个任务，"
                f"更新 {n_changed} 条通讯录（{n_filled} 条有标记，{len(self._cfg.address_book) - n_filled} 条为空）"
            )
        else:
            info(f"「上次任务」已与任务管理一致（{n_jobs} 个任务，{n_filled} 条有标记）")

    def _save_rate(self) -> None:
        try:
            self._cfg.rate_limit_seconds = float(self._rate_entry.get().strip())
            save_config(self._cfg)
        except ValueError:
            pass

    # --- 定时任务（多任务 + 发送账号覆盖） ---
    def _schedule_doc_path(self) -> str:
        for p in (resource_path("docs", "定时任务导入说明与示例.txt"), os.path.join(app_root(), "docs", "定时任务导入说明与示例.txt")):
            if os.path.isfile(p):
                return p
        return os.path.join(app_root(), "docs", "定时任务导入说明与示例.txt")

    def _account_id_values(self) -> List[str]:
        ids = [a.id for a in self._cfg.accounts]
        return ids if ids else ["—"]

    def _page_schedule(self) -> ctk.CTkFrame:
        """群勾选在滚动区；间隔/TXT/添加固定在页底，勾群时无需来回滚动。"""
        page = ctk.CTkFrame(self._content, fg_color="transparent")

        sched_foot = ctk.CTkFrame(page, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        sf = ctk.CTkFrame(sched_foot, fg_color="transparent")
        sf.pack(fill="x", padx=12, pady=10)
        ctk.CTkLabel(
            sf,
            text="默认发送间隔（分钟，如 5-10；TXT 未写 间隔= 时使用）",
            text_color=COLORS["muted"],
        ).pack(anchor="w", pady=(0, 4))
        self._s2_interval = ctk.CTkEntry(sf)
        self._s2_interval.insert(0, "5-10")
        self._s2_interval.pack(fill="x", pady=(0, 6))
        self._s2_file = ctk.CTkEntry(sf, placeholder_text="选择 TXT 或文件夹（文件夹内按数字前缀排序）")
        self._s2_file.pack(fill="x", pady=(0, 6))
        ctk.CTkButton(sf, text="选择 TXT / 文件夹", fg_color=COLORS["border"], command=self._pick_s2_source).pack(fill="x", pady=(0, 6))
        ctk.CTkButton(sf, text="添加文档任务", fg_color=COLORS["accent"], command=self._add_s2_job).pack(fill="x")

        inner, canvas, finish = mount_page_scroll(page, footer=sched_foot, bg=COLORS["bg"])
        self._scroll_wheel_handler = lambda e, c=canvas: scroll_wheel(c, e)
        ctk.CTkLabel(inner, text="定时任务", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLORS["text"]).pack(anchor="w", pady=8)
        ctk.CTkLabel(
            inner,
            text="勾选几个群就创建几个独立任务，可分别开始/暂停。TXT 里写 账号=主号 时，按各群通讯录归属账号发送；"
            "其它角色（如男二、女一）保持固定账号。",
            text_color=COLORS["muted"],
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))
        ctk.CTkButton(
            inner,
            text="打开 TXT 格式说明",
            fg_color=COLORS["border"],
            command=lambda: webbrowser.open(Path(self._schedule_doc_path()).as_uri()) if os.path.isfile(self._schedule_doc_path()) else info("未找到说明文档"),
        ).pack(anchor="w", pady=4)

        form = ctk.CTkFrame(inner, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        form.pack(fill="x", pady=8)
        ctk.CTkLabel(form, text="勾选群发目标", text_color=COLORS["muted"]).pack(anchor="w", padx=12, pady=(10, 4))
        self._s2_targets = ctk.CTkFrame(form, fg_color="transparent")
        self._s2_targets.pack(fill="x", padx=10, pady=(4, 12))
        self._refresh_s2_target_checks()

        edit_card = ctk.CTkFrame(inner, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        edit_card.pack(fill="x", pady=8)
        ctk.CTkLabel(edit_card, text="高级：手动批量改发送账号（一般不必用）", font=ctk.CTkFont(size=13, weight="bold"), text_color=COLORS["text"]).pack(
            anchor="w", padx=12, pady=(12, 8)
        )
        pr = ctk.CTkFrame(edit_card, fg_color="transparent")
        pr.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(pr, text="选定任务", text_color=COLORS["muted"]).pack(side="left", padx=(0, 8))
        self._s2_job_pick = ctk.CTkComboBox(pr, width=280, values=["—"], command=self._on_s2_job_pick_changed)
        self._s2_job_pick.pack(side="left", fill="x", expand=True)
        br = ctk.CTkFrame(edit_card, fg_color="transparent")
        br.pack(fill="x", padx=12, pady=(4, 12))
        ctk.CTkLabel(br, text="原文账号", text_color=COLORS["muted"]).grid(row=0, column=0, padx=(0, 8), pady=4, sticky="w")
        self._s2_bulk_from = ctk.CTkComboBox(br, width=160, values=["—"])
        self._s2_bulk_from.grid(row=0, column=1, pady=4, sticky="w")
        ctk.CTkLabel(br, text="改由账号发送", text_color=COLORS["muted"]).grid(row=1, column=0, padx=(0, 8), pady=4, sticky="w")
        self._s2_bulk_to = ctk.CTkComboBox(br, width=160, values=self._account_id_values())
        self._s2_bulk_to.grid(row=1, column=1, pady=4, sticky="w")
        ctk.CTkButton(br, text="批量替换", fg_color=COLORS["border"], command=self._s2_bulk_replace).grid(row=2, column=0, columnspan=2, sticky="w", pady=8)

        ctk.CTkLabel(
            inner,
            text="任务运行状态、暂停/继续请在侧栏「任务管理」中查看；本页仅用于添加任务与批量改账号。",
            text_color=COLORS["muted"],
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(8, 4))
        finish()
        return page

    def _page_task_manager(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._content, fg_color="transparent")
        inner, canvas, finish = mount_page_scroll(page, bg=COLORS["bg"])
        self._taskmgr_scroll_handler = lambda e, c=canvas: scroll_wheel(c, e)
        ctk.CTkLabel(inner, text="任务管理", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLORS["text"]).pack(
            anchor="w", pady=(8, 4)
        )
        ctk.CTkLabel(
            inner,
            text="每个任务一张卡片：绿色=运行中，金色=监听暂停，紫色=提醒+监听，红色=其它暂停，灰色=已停止。点击卡片可切换运行/暂停。",
            text_color=COLORS["muted"],
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        self._taskmgr_count_lbl = ctk.CTkLabel(
            inner,
            text="任务数量：0",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=COLORS["text"],
            anchor="w",
        )
        self._taskmgr_count_lbl.pack(anchor="w", pady=(0, 10))
        top = ctk.CTkFrame(inner, fg_color="transparent")
        top.pack(fill="x", pady=(0, 8))
        ctk.CTkButton(
            top,
            text="一键开始全部任务",
            fg_color=COLORS["accent"],
            hover_color="#1da851",
            height=40,
            command=self._resume_all_s2_jobs,
        ).pack(fill="x")
        ctk.CTkButton(
            top,
            text="一键开始下一天",
            fg_color="#5a4a12",
            hover_color="#6d5a18",
            height=38,
            command=self._start_next_s2_folder_day,
        ).pack(fill="x", pady=(8, 0))
        ctk.CTkButton(
            top,
            text="一键删除全部任务",
            fg_color=COLORS["danger"],
            hover_color="#b63a3a",
            height=38,
            command=self._delete_all_s2_jobs,
        ).pack(fill="x", pady=(8, 0))
        self._taskmgr_cards = ctk.CTkFrame(inner, fg_color="transparent")
        self._taskmgr_cards.pack(fill="both", expand=True, pady=4)
        self._render_taskmgr_cards(force=True)
        finish()
        return page

    def _bind_task_card_click(self, widget: ctk.CTkBaseClass, job_id: str) -> None:
        def on_click(_event: Any = None) -> None:
            self._toggle_s2_run_by_id(job_id)

        widget.bind("<Button-1>", on_click)
        for child in widget.winfo_children():
            self._bind_task_card_click(child, job_id)

    def _taskmgr_fingerprint(self, j: Schedule2Job, cfg: Any) -> tuple:
        return (
            schedule2_job_is_running(j),
            j.enabled,
            j.state,
            (j.pause_reason or "")[:40],
            j.cursor,
            j.source_name,
            getattr(j, "folder_day_index", 0),
            is_folder_job(j),
            schedule2_job_step_label(j),
            schedule2_job_target_remarks(cfg, j),
            self._task_reminder_summary(j),
        )

    def _task_reminder_summary(self, j: Schedule2Job) -> str:
        notes: List[str] = []
        for r in j.rows:
            if not r.is_reminder:
                continue
            t = (r.reminder_note or "").strip()
            if t:
                notes.append(t)
        if not notes:
            return ""
        idx = max(0, j.cursor - 1)
        # 优先展示最近一次已走到的提醒备注，其次展示第一条提醒备注
        for k in range(idx, -1, -1):
            rr = j.rows[k]
            if rr.is_reminder and (rr.reminder_note or "").strip():
                return (rr.reminder_note or "").strip()[:56]
        return notes[0][:56]

    def _apply_taskmgr_tile(self, w: Dict[str, Any], j: Schedule2Job, cfg: Any) -> None:
        running = schedule2_job_is_running(j)
        pal = taskmgr_tile_palette(running=running, enabled=bool(j.enabled), pause_reason=j.pause_reason or "")
        w["card"].configure(fg_color=pal["fg"], border_color=pal["border"])
        w["title"].configure(text=schedule2_job_target_remarks(cfg, j), text_color=pal["title"])
        w["status"].configure(
            text=taskmgr_card_status_text(
                schedule2_job_status_label(j),
                schedule2_job_step_label(j),
            ),
            text_color=pal["status"],
        )
        fname = taskmgr_job_file_label(j)
        w["file"].configure(
            text=fname,
            text_color=pal["file"],
        )
        rem = self._task_reminder_summary(j)
        rem_c = "#1f2328"
        rem_bg = "#ffd24a"
        w["reminder_box"].configure(fg_color=rem_bg)
        w["reminder"].configure(
            text=(f"提醒：{rem}" if rem else "提醒：无"),
            text_color=rem_c if rem else "#5c4a00",
        )
        hint = "点击暂停" if running else ("点击继续" if j.enabled else "点击重新开始")
        w["hint"].configure(text=hint, text_color=pal["hint"])

    def _build_taskmgr_tile(self, j: Schedule2Job, cfg: Any, index: int) -> Dict[str, Any]:
        running = schedule2_job_is_running(j)
        pal = taskmgr_tile_palette(running=running, enabled=bool(j.enabled), pause_reason=j.pause_reason or "")
        fonts = taskmgr_fonts()
        card = ctk.CTkFrame(
            self._taskmgr_cards,
            fg_color=pal["fg"],
            corner_radius=10,
            border_width=2,
            border_color=pal["border"],
            height=168,
        )
        grid_place(card, index, TASKMGR_COLS, padx=6, pady=6)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=10, pady=10)
        head = ctk.CTkFrame(inner, fg_color="transparent")
        head.pack(fill="x", pady=(0, 4))
        title = ctk.CTkLabel(
            head,
            text=schedule2_job_target_remarks(cfg, j),
            font=fonts["title"],
            text_color=pal["title"],
            anchor="w",
            wraplength=160,
            justify="left",
        )
        title.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            head,
            text="删除",
            width=52,
            height=24,
            font=fonts["btn"],
            fg_color="#8b2e2e",
            hover_color="#a83232",
            command=lambda jid=j.id: self._del_s2_job_by_id(jid),
        ).pack(side="right", padx=(4, 0))
        body = ctk.CTkFrame(inner, fg_color="transparent")
        body.pack(fill="both", expand=True)
        status = ctk.CTkLabel(
            body,
            text="",
            font=fonts["status"],
            anchor="w",
            wraplength=200,
            justify="left",
        )
        status.pack(fill="x", pady=(0, 4))
        file_lbl = ctk.CTkLabel(
            body,
            text="",
            font=fonts["body"],
            anchor="w",
            wraplength=200,
            justify="left",
        )
        file_lbl.pack(fill="x", pady=(0, 2))
        reminder_box = ctk.CTkFrame(body, corner_radius=6, fg_color="#ffd24a")
        reminder_box.pack(fill="x", pady=(2, 3))
        reminder_lbl = ctk.CTkLabel(
            reminder_box,
            text="",
            font=fonts["reminder"],
            anchor="w",
            wraplength=188,
            justify="left",
        )
        reminder_lbl.pack(fill="x", padx=6, pady=4)
        hint = ctk.CTkLabel(
            body,
            text="",
            font=fonts["hint"],
            text_color="#e9e9e9",
            anchor="w",
        )
        hint.pack(fill="x")
        widgets = {
            "card": card,
            "title": title,
            "status": status,
            "file": file_lbl,
            "reminder_box": reminder_box,
            "reminder": reminder_lbl,
            "hint": hint,
        }
        self._apply_taskmgr_tile(widgets, j, cfg)
        self._bind_task_card_click(body, j.id)
        return widgets

    def _full_rebuild_taskmgr_grid(self, jobs: List[Schedule2Job], cfg: Any) -> None:
        for w in self._taskmgr_cards.winfo_children():
            w.destroy()
        self._taskmgr_widgets.clear()
        self._taskmgr_fp_cache.clear()
        configure_equal_columns(self._taskmgr_cards, TASKMGR_COLS, uniform="wa_task")
        for i, j in enumerate(jobs):
            self._taskmgr_widgets[j.id] = self._build_taskmgr_tile(j, cfg, i)
            self._taskmgr_fp_cache[j.id] = self._taskmgr_fingerprint(j, cfg)

    def _patch_taskmgr_grid(self, jobs: List[Schedule2Job], cfg: Any) -> None:
        for j in jobs:
            fp = self._taskmgr_fingerprint(j, cfg)
            if self._taskmgr_fp_cache.get(j.id) == fp:
                continue
            self._taskmgr_fp_cache[j.id] = fp
            w = self._taskmgr_widgets.get(j.id)
            if w:
                self._apply_taskmgr_tile(w, j, cfg)

    def _refresh_taskmgr_card(self, job_id: str) -> None:
        """仅刷新单张任务卡片（点击切换状态时用，避免全页重建）。"""
        if not hasattr(self, "_taskmgr_cards"):
            return
        jobs = load_schedule2_jobs()
        j = next((x for x in jobs if x.id == job_id), None)
        if j is None:
            self._render_taskmgr_cards(force=True)
            return
        w = self._taskmgr_widgets.get(job_id)
        if w is None:
            self._render_taskmgr_cards(force=False)
            return
        self._taskmgr_fp_cache[job_id] = self._taskmgr_fingerprint(j, self._cfg)
        self._apply_taskmgr_tile(w, j, self._cfg)
        self._update_taskmgr_count_label(jobs)
        self._sync_taskmgr_display_order(jobs)

    def _sync_taskmgr_display_order(self, jobs: List[Schedule2Job]) -> None:
        display_jobs = taskmgr_sort_jobs_for_display(jobs, is_running=schedule2_job_is_running)
        display_ids = [j.id for j in display_jobs]
        if display_ids == self._taskmgr_display_ids:
            return
        reorder_taskmgr_grid(self._taskmgr_widgets, display_jobs, cols=TASKMGR_COLS, padx=6, pady=6)
        self._taskmgr_display_ids = display_ids

    def _update_taskmgr_count_label(self, jobs: List[Schedule2Job]) -> None:
        lbl = getattr(self, "_taskmgr_count_lbl", None)
        if lbl is None:
            return
        counts = taskmgr_count_jobs(jobs, is_running=schedule2_job_is_running)
        lbl.configure(text=format_taskmgr_count_summary(counts))

    def _render_taskmgr_cards(self, *, force: bool = False) -> None:
        if not hasattr(self, "_taskmgr_cards"):
            return
        jobs = load_schedule2_jobs()
        cfg = self._cfg
        ids = [j.id for j in jobs]
        if not jobs:
            if self._taskmgr_listed_ids:
                for w in self._taskmgr_cards.winfo_children():
                    w.destroy()
                self._taskmgr_widgets.clear()
                self._taskmgr_fp_cache.clear()
                self._taskmgr_listed_ids = []
                self._taskmgr_display_ids = []
            if not self._taskmgr_cards.winfo_children():
                ctk.CTkLabel(
                    self._taskmgr_cards,
                    text="尚无定时任务，请先在「定时任务」页添加。",
                    text_color=COLORS["muted"],
                ).grid(row=0, column=0, columnspan=TASKMGR_COLS, sticky="w", padx=8, pady=12)
            self._update_taskmgr_count_label(jobs)
            self._sync_s2_job_pick_combo()
            return
        display_jobs = taskmgr_sort_jobs_for_display(jobs, is_running=schedule2_job_is_running)
        display_ids = [j.id for j in display_jobs]
        if force or ids != self._taskmgr_listed_ids:
            self._taskmgr_listed_ids = ids
            self._full_rebuild_taskmgr_grid(display_jobs, cfg)
            self._taskmgr_display_ids = display_ids
        else:
            self._patch_taskmgr_grid(jobs, cfg)
            self._sync_taskmgr_display_order(jobs)
        self._update_taskmgr_count_label(jobs)
        self._sync_s2_job_pick_combo()
        handler = getattr(self, "_taskmgr_scroll_handler", None)
        if handler:
            bind_scroll_tree_once(self._taskmgr_cards, handler)

    def _tick_taskmgr_cards(self) -> None:
        if not self._log_pump_on:
            return
        try:
            if self.winfo_exists() and self._pages_ready:
                fr = self._nav.get("taskmgr")
                if fr is not None and fr.winfo_ismapped():
                    self._render_taskmgr_cards(force=False)
        except Exception:
            pass
        if self._log_pump_on:
            try:
                if self.winfo_exists():
                    self.after(TASKMGR_TICK_MS, self._tick_taskmgr_cards)
            except Exception:
                pass

    def _toggle_s2_run_by_id(self, job_id: str) -> None:
        if self._taskmgr_toggle_busy:
            return
        self._taskmgr_toggle_busy = True
        try:
            j = next((x for x in load_schedule2_jobs() if x.id == job_id), None)
            if j is None:
                return
            if schedule2_job_is_running(j):
                self._schedule2.pause_job(j.id, "手动暂停")
            elif self._schedule2.resume_job(j.id):
                pass
            else:
                info("无法启动该任务")
            self._refresh_taskmgr_card(job_id)
        finally:
            self._taskmgr_toggle_busy = False

    def _resume_all_s2_jobs(self) -> None:
        jobs = load_schedule2_jobs()
        resumable, skipped = bulk_resume_schedule2_counts(jobs)
        if resumable <= 0:
            running = sum(1 for j in jobs if schedule2_job_is_running(j))
            if running > 0:
                info(
                    f"当前 {running} 个任务已是运行中；若仍不发送，请确认账号已登录在线后点「保存并重载服务」。"
                )
            elif skipped > 0:
                info(f"没有可恢复的暂停任务（{skipped} 个已完成任务已跳过）。")
            else:
                info("没有可恢复的暂停或已停止任务")
            return
        msg = f"将恢复 {resumable} 个暂停中的定时任务继续发送。"
        if skipped > 0:
            msg += f"\n\n{skipped} 个已完成的任务将被跳过（需重跑请点对应卡片）。"
        msg += "\n\n确定继续？"
        if not messagebox.askyesno("一键开始全部任务", msg, parent=self):
            return
        n = self._schedule2.resume_all_jobs()
        self._cfg = load_config()
        self._refresh_s2_target_checks()
        self._render_taskmgr_cards(force=True)
        if n > 0:
            if skipped > 0:
                info(f"已恢复 {n} 个定时任务为运行中（已跳过 {skipped} 个已完成任务）")
            else:
                info(f"已恢复 {n} 个定时任务为运行中")
            return
        running = sum(1 for j in load_schedule2_jobs() if schedule2_job_is_running(j))
        if running > 0:
            info(
                f"当前 {running} 个任务已是运行中；若仍不发送，请确认账号已登录在线后点「保存并重载服务」。"
            )
        else:
            info("没有可恢复的暂停或已停止任务")

    def _clear_s2_target_selection(self) -> None:
        for _ent, var in getattr(self, "_sched2_target_rows", []):
            var.set(False)
        for v in getattr(self, "_sched2_target_vars", []):
            v.set(False)

    def _refresh_s2_target_checks(self) -> None:
        if not hasattr(self, "_s2_targets"):
            return
        sync_last_schedule_from_disk(self._cfg)
        jobs = load_schedule2_jobs()
        for w in self._s2_targets.winfo_children():
            w.destroy()
        self._sched2_target_vars = []
        self._sched2_target_rows = []
        if not self._cfg.address_book:
            ctk.CTkLabel(self._s2_targets, text="请先在「通讯录」添加群。", text_color=COLORS["muted"]).pack(anchor="w", padx=4)
            return
        for ent in self._cfg.address_book:
            v = ctk.BooleanVar(value=False)
            self._sched2_target_vars.append(v)
            self._sched2_target_rows.append((ent, v))
            cref = ent.chat_ref
            cref_disp = f"{cref[:48]}…" if len(cref) > 48 else cref
            kind_hint = entry_schedule_kind_hint(jobs, ent.id)
            last_fn = (getattr(ent, "last_schedule_source_name", "") or "").strip()
            last_hint = f" · 上次任务→{last_fn}" if last_fn else " · 上次任务→为空"
            disp = f"{ent.remark}{kind_hint}（{cref_disp}{last_hint}）"
            ctk.CTkCheckBox(self._s2_targets, text=disp, variable=v, text_color=COLORS["text"]).pack(anchor="w", padx=4, pady=2)

    def _pick_s2_source(self) -> None:
        cur = self._s2_file.get() if hasattr(self, "_s2_file") else ""
        path = pick_txt_or_folder(self, current_path=cur)
        if path and hasattr(self, "_s2_file"):
            self._s2_file.delete(0, "end")
            self._s2_file.insert(0, path)

    def _parse_interval(self, s: str) -> Optional[tuple[float, float]]:
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*$", (s or "").strip())
        if not m:
            return None
        a, b = float(m.group(1)), float(m.group(2))
        if a <= 0 or b <= 0:
            return None
        return (min(a, b), max(a, b))

    def _s2_job_label(self, j: Schedule2Job) -> str:
        tgt = schedule2_job_target_remarks(self._cfg, j)
        return f"{tgt}{schedule_kind_badge(j)} · {j.source_name}（{j.row_count()} 步）"

    def _on_s2_job_pick_changed(self, _value: str) -> None:
        for j in load_schedule2_jobs():
            if self._s2_job_label(j) == self._s2_job_pick.get().strip():
                self._s2_edit_job_id = j.id
                self._refresh_s2_bulk_combos(j)
                return

    def _refresh_s2_bulk_combos(self, job: Optional[Schedule2Job] = None) -> None:
        if job is None and self._s2_edit_job_id:
            job = next((j for j in load_schedule2_jobs() if j.id == self._s2_edit_job_id), None)
        acc_vals = self._account_id_values()
        if hasattr(self, "_s2_bulk_to"):
            self._s2_bulk_to.configure(values=acc_vals)
            if acc_vals and acc_vals[0] != "—":
                cur = self._s2_bulk_to.get().strip()
                if cur not in acc_vals:
                    self._s2_bulk_to.set(acc_vals[0])
        if not hasattr(self, "_s2_bulk_from") or job is None:
            return
        originals = sorted({r.original_account_id for r in job.rows if not r.is_reminder and r.original_account_id.strip()})
        vals = originals if originals else ["—"]
        self._s2_bulk_from.configure(values=vals)
        self._s2_bulk_from.set(vals[0])

    def _sync_s2_job_pick_combo(self) -> None:
        jobs = load_schedule2_jobs()
        labels = [self._s2_job_label(j) for j in jobs] or ["—"]
        if hasattr(self, "_s2_job_pick"):
            self._s2_job_pick.configure(values=labels)
            if jobs:
                if not self._s2_edit_job_id or not any(j.id == self._s2_edit_job_id for j in jobs):
                    self._s2_edit_job_id = jobs[0].id
                sel = next(j for j in jobs if j.id == self._s2_edit_job_id)
                self._s2_job_pick.set(self._s2_job_label(sel))
            else:
                self._s2_edit_job_id = None
        sel_job = next((j for j in jobs if j.id == self._s2_edit_job_id), None)
        self._refresh_s2_bulk_combos(sel_job)

    def _add_s2_job(self) -> None:
        selected: List[str] = []
        rows = getattr(self, "_sched2_target_rows", None) or []
        if rows:
            for ent, var in rows:
                if var.get():
                    selected.append(ent.id)
        else:
            for i, ent in enumerate(self._cfg.address_book):
                if i < len(self._sched2_target_vars) and self._sched2_target_vars[i].get():
                    selected.append(ent.id)
        if not selected:
            info("请至少勾选一个通讯录中的群")
            return
        interval = self._parse_interval(self._s2_interval.get())
        if not interval:
            info("间隔格式无效，请填 5-10")
            return
        path = self._s2_file.get().strip()
        if not path:
            info("请选择 TXT 文件或文件夹")
            return
        if os.path.isdir(path):
            self._add_s2_folder_jobs(path, selected, interval)
            return
        if not os.path.isfile(path):
            info("路径无效，请选择 TXT 文件或文件夹")
            return
        try:
            text = open(path, encoding="utf-8").read()
        except OSError as exc:
            info(f"读取失败：{exc}")
            return
        items, errs = import_doc_items(text, valid_accounts=None)
        if not items:
            info("TXT 无有效条目")
            for e in errs[:5]:
                info(e)
            return
        for e in errs[:10]:
            info(e)
        rows: List[Schedule2Row] = []
        for it in items:
            orig, send = mark_row_primary_auto(it.account_id, it.account_id)
            rows.append(
                Schedule2Row(
                    id=uuid.uuid4().hex[:12],
                    original_account_id=orig,
                    send_as_account_id=send,
                    content=it.content,
                    is_reminder=it.is_reminder,
                    reminder_note=it.reminder_note,
                    delay_after_minutes=it.delay_after_minutes,
                )
            )
        by_eid = {e.id: e for e in self._cfg.address_book}
        if row_needs_per_group_owner(rows):
            for eid in selected:
                ent = by_eid.get(eid)
                if not ent or not (ent.owner_account_id or "").strip():
                    info(f"群「{ent.remark if ent else eid}」未在通讯录选择主号/归属账号，请先在通讯录设置。")
                    return
        jobs = load_schedule2_jobs()
        created: List[Schedule2Job] = []
        for eid in selected:
            ent = by_eid.get(eid)
            if not ent:
                continue
            job_rows = copy.deepcopy(rows)
            job = Schedule2Job.new(
                [ent.chat_ref],
                interval[0],
                interval[1],
                path,
                job_rows,
                chat_entry_ids=[eid],
            )
            jobs.append(job)
            created.append(job)
        if not created:
            info("未能创建任务：所选群无效")
            return
        save_schedule2_jobs(jobs)
        apply_last_schedule_for_jobs(self._cfg, created)
        self._s2_edit_job_id = created[-1].id
        self._render_taskmgr_cards(force=True)
        if len(created) > 1:
            info(
                f"已为 {len(created)} 个群各添加 1 个任务：{created[0].source_name}，{len(rows)} 步"
                "（默认暂停，请在「任务管理」分别点卡片继续）"
            )
        else:
            info(
                f"已添加任务：{created[0].source_name}，{len(rows)} 步（默认暂停，请在「任务管理」点卡片继续）"
            )
        self._clear_s2_target_selection()
        self._refresh_s2_target_checks()

    def _add_s2_folder_jobs(self, folder_path: str, selected: List[str], interval: tuple[float, float]) -> None:
        rel_files, scan_errs = scan_schedule_folder(folder_path)
        if scan_errs:
            info("\n".join(scan_errs))
            return
        folder_abs = os.path.abspath(folder_path)
        first_path = folder_txt_abs_path(folder_abs, rel_files[0])
        try:
            text = open(first_path, encoding="utf-8").read()
        except OSError as exc:
            info(f"读取失败：{exc}")
            return
        items, errs = import_doc_items(text, valid_accounts=None)
        if not items:
            info("首份 TXT 无有效条目")
            for e in errs[:5]:
                info(e)
            return
        for e in errs[:10]:
            info(e)
        rows: List[Schedule2Row] = []
        for it in items:
            orig, send = mark_row_primary_auto(it.account_id, it.account_id)
            rows.append(
                Schedule2Row(
                    id=uuid.uuid4().hex[:12],
                    original_account_id=orig,
                    send_as_account_id=send,
                    content=it.content,
                    is_reminder=it.is_reminder,
                    reminder_note=it.reminder_note,
                    delay_after_minutes=it.delay_after_minutes,
                )
            )
        by_eid = {e.id: e for e in self._cfg.address_book}
        if row_needs_per_group_owner(rows):
            for eid in selected:
                ent = by_eid.get(eid)
                if not ent or not (ent.owner_account_id or "").strip():
                    info(f"群「{ent.remark if ent else eid}」未在通讯录选择主号/归属账号，请先在通讯录设置。")
                    return
        jobs = load_schedule2_jobs()
        created: List[Schedule2Job] = []
        for eid in selected:
            ent = by_eid.get(eid)
            if not ent:
                continue
            job_rows = copy.deepcopy(rows)
            job = Schedule2Job.new(
                [ent.chat_ref],
                interval[0],
                interval[1],
                first_path,
                job_rows,
                chat_entry_ids=[eid],
            )
            job.source_kind = "folder"
            job.folder_path = folder_abs
            job.folder_files = list(rel_files)
            job.folder_day_index = 0
            jobs.append(job)
            created.append(job)
        if not created:
            info("未能创建任务：所选群无效")
            return
        save_schedule2_jobs(jobs)
        apply_last_schedule_for_jobs(self._cfg, created)
        self._s2_edit_job_id = created[-1].id
        self._render_taskmgr_cards(force=True)
        if len(created) > 1:
            info(
                f"已为 {len(created)} 个群各添加 1 个文件夹任务（{len(rel_files)} 天，默认暂停）"
            )
        else:
            info(
                f"已添加文件夹任务（{len(rel_files)} 天，默认暂停）：第 1 天 {created[0].source_name}，{len(rows)} 步"
            )
        self._clear_s2_target_selection()
        self._refresh_s2_target_checks()

    def _start_next_s2_folder_day(self) -> None:
        jobs = load_schedule2_jobs()
        candidates = [j for j in jobs if can_advance_folder_day(j)]
        if not candidates:
            try:
                messagebox.showinfo(
                    "一键开始下一天",
                    "没有可进入下一天的文件夹任务（需为文件夹任务且尚未到最后一份 TXT）。",
                    parent=self,
                )
            except Exception:
                info("没有可进入下一天的文件夹任务。")
            return

        lines: List[str] = []
        running_n = 0
        for j in candidates:
            nxt = j.folder_day_index + 1
            lines.append(
                format_folder_advance_line(
                    target_label=schedule2_job_target_remarks(self._cfg, j),
                    current_name=j.source_name or "未命名",
                    next_name=j.folder_files[nxt],
                    next_day_one_based=nxt + 1,
                    total_days=len(j.folder_files),
                )
            )
            if schedule2_job_is_running(j):
                running_n += 1

        msg = "将中断当前天未发完的内容。\n\n"
        if running_n:
            msg += f"其中 {running_n} 个任务正在运行，切换后会立即停止当前天。\n\n"
        msg += "将为以下文件夹任务切换到下一天并开始发送：\n"
        msg += "\n".join(lines)
        msg += "\n\n确定继续？"
        try:
            if not messagebox.askyesno("一键开始下一天", msg, parent=self):
                return
        except Exception:
            return

        self._cfg = load_config()
        updated: List[Schedule2Job] = []
        for j in candidates:
            live = next((x for x in load_schedule2_jobs() if x.id == j.id), None)
            if live is None or not can_advance_folder_day(live):
                continue
            ok, err = advance_schedule2_folder_day(live, self._cfg)
            if ok:
                updated.append(live)
            else:
                info(f"任务 {schedule2_job_target_remarks(self._cfg, live)} 切换失败：{err}")
        if not updated:
            info("未能切换任何文件夹任务。")
            return
        save_schedule2_jobs_patch(updated)
        apply_last_schedule_for_jobs(self._cfg, updated)
        self._render_taskmgr_cards(force=True)
        info(f"已为 {len(updated)} 个文件夹任务切换到下一天并开始发送。")

    def _select_s2_job(self, job_id: str) -> None:
        self._s2_edit_job_id = job_id
        self._sync_s2_job_pick_combo()
        info("已选定该任务，可在上方批量替换发送账号。")

    def _toggle_s2_enabled(self, idx: int) -> None:
        jobs = load_schedule2_jobs()
        if idx < len(jobs):
            jobs[idx].enabled = not jobs[idx].enabled
            save_schedule2_jobs(jobs)
        self._render_taskmgr_cards()

    def _del_s2_job_by_id(self, job_id: str) -> None:
        jobs = load_schedule2_jobs()
        dead = next((x for x in jobs if x.id == job_id), None)
        if dead is None:
            return
        jobs = [x for x in jobs if x.id != job_id]
        if dead.id == self._s2_edit_job_id:
            self._s2_edit_job_id = None
        save_schedule2_jobs(jobs)
        info(f"已删除任务：{dead.source_name}")
        self._render_taskmgr_cards(force=True)
        self._sync_s2_job_pick_combo()
        if hasattr(self, "_s2_targets"):
            self._refresh_s2_target_checks()

    def _delete_all_s2_jobs(self) -> None:
        jobs = load_schedule2_jobs()
        if not jobs:
            info("当前没有可删除的任务。")
            return
        single_txt, folder_done, folder_kept, to_delete, kept = bulk_delete_job_summary(jobs)
        if not to_delete:
            info("没有可批量删除的任务（仅删除单 TXT，或文件夹任务全部天数已发完）。")
            if folder_kept > 0:
                info(f"保留 {folder_kept} 个进行中的文件夹任务。")
            return
        msg = format_bulk_delete_confirm_message(
            single_txt=single_txt,
            folder_done=folder_done,
            folder_kept=folder_kept,
            total_delete=len(to_delete),
        )
        if not messagebox.askyesno("一键删除任务", msg, parent=self):
            return
        save_schedule2_jobs(kept)
        deleted_ids = {j.id for j in to_delete}
        if getattr(self, "_s2_edit_job_id", None) in deleted_ids:
            self._s2_edit_job_id = kept[0].id if kept else None
        self._render_taskmgr_cards(force=True)
        self._sync_s2_job_pick_combo()
        if hasattr(self, "_s2_targets"):
            self._refresh_s2_target_checks()
        if folder_kept > 0:
            info(
                f"已删除 {len(to_delete)} 个任务"
                f"（单 TXT {single_txt}，已完成文件夹 {folder_done}），"
                f"保留 {folder_kept} 个进行中的文件夹任务。"
            )
        else:
            info(f"已删除 {len(to_delete)} 个任务（单 TXT {single_txt}，已完成文件夹 {folder_done}）。")

    def _s2_bulk_replace(self) -> None:
        jobs = load_schedule2_jobs()
        jid = self._s2_edit_job_id
        if not jid:
            info("请先选定任务（列表点「选为编辑」或下拉框选择）")
            return
        job = next((j for j in jobs if j.id == jid), None)
        if not job:
            info("任务不存在")
            return
        from_acc = self._s2_bulk_from.get().strip()
        to_acc = self._s2_bulk_to.get().strip()
        if from_acc in ("", "—") or to_acc in ("", "—"):
            info("请选择原文账号与目标发送账号")
            return
        if from_acc == to_acc:
            info("原文与目标相同，无需替换")
            return
        n = 0
        for r in job.rows:
            if not r.is_reminder and r.original_account_id == from_acc:
                r.send_as_account_id = to_acc
                n += 1
        if n == 0:
            info(f"没有原文为「{from_acc}」的条目")
            return
        save_schedule2_jobs(jobs)
        self._refresh_s2_bulk_combos(job)
        info(f"已将「{from_acc}」的 {n} 条改为由「{to_acc}」发送")

    # --- 日志 ---
    def _page_logs(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._content, fg_color="transparent")

        log_foot = ctk.CTkFrame(page, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        lf = ctk.CTkFrame(log_foot, fg_color="transparent")
        lf.pack(fill="x", padx=12, pady=10)
        ctk.CTkButton(lf, text="刷新日志", command=self._reload_logs_page, fg_color=COLORS["accent"]).pack(fill="x")

        inner, canvas, finish = mount_page_scroll(page, footer=log_foot, bg=COLORS["bg"])
        self._scroll_wheel_handler = lambda e, c=canvas: scroll_wheel(c, e)

        ctk.CTkLabel(inner, text="日志", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLORS["text"]).pack(
            anchor="w", pady=(8, 12)
        )
        self._log_box = ctk.CTkTextbox(
            inner,
            height=480,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color=COLORS["card"],
            text_color=COLORS["text"],
            border_width=1,
            border_color=COLORS["border"],
        )
        self._log_box.pack(fill="both", expand=True, pady=(0, 8))
        bind_log_textbox_wheel(self._log_box)
        reload_log_textbox_from_memory(
            self._log_box,
            get_recent_lines,
            limit=LOG_TEXTBOX_MAX_LINES,
            log_queue=getattr(self, "_log_queue", None),
        )
        finish()
        return page


# 兼容旧入口名
MainWindow = WaPanel
