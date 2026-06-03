"""主窗口：侧边栏 + 多页面（暗色主题）。"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许直接 `python .../tg_multi_listener/ui/app.py`：此时 sys.path[0] 为 ui/，需先加入项目根。
_pkg_root = Path(__file__).resolve().parent.parent
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))
_wa_pkg = _pkg_root.parent / "wa_multi_listener"
if _wa_pkg.is_dir() and str(_wa_pkg) not in sys.path:
    sys.path.insert(0, str(_wa_pkg))
from wa_ui.card_grid import TASKMGR_COLS, TG_ACCT_COLS, configure_equal_columns, grid_place
from wa_ui.taskmgr_tile_theme import (
    format_taskmgr_count_summary,
    taskmgr_card_status_text,
    taskmgr_count_jobs,
    taskmgr_fonts,
    taskmgr_tile_palette,
)
from wa_ui.log_textbox_util import (
    DASH_LOG_MAX_LINES,
    LOG_PUMP_MS,
    LOG_TEXTBOX_MAX_LINES,
    append_log_line_capped,
    bind_log_textbox_wheel,
    reload_log_textbox_from_memory,
)
from wa_ui.scroll_util import bind_scroll_tree_once, mount_page_scroll, scroll_wheel

TASKMGR_TICK_MS = 5000

import os
import queue
import random
import re
import threading
import time
import uuid
import webbrowser
from collections import Counter
import tkinter as tk
from tkinter import TclError, filedialog, messagebox
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

import customtkinter as ctk

from ..compat_config import (
    Account,
    AddressEntry,
    apply_last_schedule_from_current_jobs,
    chat_ref_to_optional_int,
    format_job_targets_label,
    format_listener_chat_label,
    load_config,
    parse_chat_ref_input,
    parse_watch_user_input,
    save_config,
    sync_last_schedule_from_disk,
)
from ..listener import ListenerController
from ..paths import app_root, resource_path
from ..logger_util import add_memory_listener, error, get_recent_lines, info, remove_memory_listener
from ..notifier import AlertPopup, show_stage_reminder
from ..scheduler import (
    LISTEN_HIT_PAUSE_REASON,
    DocMessage,
    ScheduledJob,
    ScheduleRunner,
    load_jobs,
    save_jobs,
    save_jobs_patch,
)
from ..group_owner import (
    apply_main_account_mapping,
    clone_doc_items,
    doc_has_main_account_placeholder,
    is_main_account_placeholder,
)
from ..schedule_txt_import import import_doc_items, items_have_any_txt_interval, items_use_txt_intervals
from ..session_check import is_session_authorized_sync
from ..stats import record_alert, today_alert_count
from ..telethon_auth import run_login_in_thread
from ..telethon_coordinator import DEFAULT_JOIN_TIMEOUT, TelethonCoordinator
from .theme import COLORS, SIDEBAR_WIDTH

# 固定窗口比例 16:10：禁止拖拽边缘改尺寸；略增高以减少纵向遮挡。
MAIN_WINDOW_W = 1152
MAIN_WINDOW_H = 720
MAIN_WINDOW_GEOMETRY = f"{MAIN_WINDOW_W}x{MAIN_WINDOW_H}"

NavId = str


class MainWindow(ctk.CTkFrame):
    def __init__(
        self,
        master: ctk.CTk | ctk.CTkFrame,
        listener: ListenerController,
        scheduler: ScheduleRunner,
        *,
        embedded: bool = False,
    ) -> None:
        super().__init__(master)
        self._embedded = embedded
        self._listener = listener
        self._scheduler = scheduler
        self._coord: Optional[TelethonCoordinator] = None
        self._service_reload_busy = False
        self._scheduler.set_reminder_callback(self._doc_schedule_reminder)
        self._cfg = load_config()
        sync_last_schedule_from_disk(self._cfg)
        self._nav: Dict[NavId, ctk.CTkFrame] = {}
        self._content: Optional[ctk.CTkFrame] = None
        self._login_status: Dict[str, Optional[bool]] = {}
        self._login_probe_gen = 0
        self._pages_ready = False
        self._sched_edit_job_id: Optional[str] = None
        self._sched_job_label_to_id: Dict[str, str] = {}
        self._sched_job_row_widgets: Dict[str, Dict[str, Any]] = {}
        self._sched_listed_job_ids: List[str] = []
        self._sched_job_fingerprints: Dict[str, tuple] = {}
        self._taskmgr_widgets: Dict[str, Dict[str, Any]] = {}
        self._taskmgr_listed_ids: List[str] = []
        self._taskmgr_fp_cache: Dict[str, tuple] = {}
        self._taskmgr_toggle_busy = False
        self._sched_target_rows: List[Tuple[AddressEntry, ctk.BooleanVar]] = []
        self._grp_row_widgets: Dict[str, Dict[str, Any]] = {}
        self._grp_row_ids: List[str] = []
        self._grp_scroll_bound = False
        self._grp_listen_save_after: Optional[str] = None
        self._acc_widgets: Dict[str, Dict[str, Any]] = {}
        self._acc_listed_ids: List[str] = []
        self._log_ui_queue: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._log_ui_pump_on = True

        if not embedded:
            root = master if isinstance(master, ctk.CTk) else master.winfo_toplevel()
            try:
                root.title("超群小帮手")
                root.geometry(MAIN_WINDOW_GEOMETRY)
                root.resizable(False, False)
            except Exception:
                pass
        self.configure(fg_color=COLORS["bg"])

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        # 先只铺侧栏 +「加载中」，避免主线程在 mainloop 前一次性创建全部页面导致长时间白屏/无响应
        self._content = ctk.CTkFrame(self, fg_color=COLORS["bg"])
        self._content.grid(row=0, column=1, sticky="nsew", padx=18, pady=18)
        self._content.grid_rowconfigure(0, weight=1)
        self._content.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self._content,
            text="正在加载界面，请稍候…",
            font=ctk.CTkFont(size=16),
            text_color=COLORS["muted"],
        ).grid(row=0, column=0)
        self.after(1, self._deferred_bootstrap_ui)
        self.after(LOG_PUMP_MS, self._pump_log_ui_queue)
        self._log_listener: Optional[Callable[[str], None]] = self._on_log_line
        add_memory_listener(self._on_log_line)
        if not embedded:
            root = master if isinstance(master, ctk.CTk) else master.winfo_toplevel()
            try:
                root.protocol("WM_DELETE_WINDOW", self._on_exit)
            except Exception:
                pass

    def _deferred_bootstrap_ui(self) -> None:
        """首帧后再创建各功能页；分多帧构建，避免单帧内创建全部页面导致长时间未响应。"""
        if self._pages_ready:
            return
        try:
            if self._content is not None:
                self._content.destroy()
            self._content = ctk.CTkFrame(self, fg_color=COLORS["bg"])
            self._content.grid(row=0, column=1, sticky="nsew", padx=18, pady=18)
            self._content.grid_rowconfigure(0, weight=1)
            self._content.grid_columnconfigure(0, weight=1)
            self._nav.clear()
            self._page_build_queue: List[NavId] = ["dash", "acct", "grp", "rules", "sched", "taskmgr", "logs"]
            self._page_build_tick()
        except Exception as exc:
            error(f"主界面加载失败：{exc}")

    def _page_build_tick(self) -> None:
        if self._pages_ready:
            return
        try:
            if not self._page_build_queue:
                for f in self._nav.values():
                    f.grid(row=0, column=0, sticky="nsew")
                self._pages_ready = True
                self._show_nav("dash")
                self.after(80, self._raise_main_window)
                self.after(5000, self._tick_schedule_job_lists)
                info("主界面已就绪")
                return
            nav_id = self._page_build_queue.pop(0)
            builders = {
                "dash": self._page_dashboard,
                "acct": self._page_accounts,
                "grp": self._page_groups,
                "rules": self._page_rules,
                "sched": self._page_schedule,
                "taskmgr": self._page_task_manager,
                "logs": self._page_logs,
            }
            try:
                self._nav[nav_id] = builders[nav_id]()
            except Exception as exc:
                error(f"主界面加载失败（{nav_id}）：{exc}")
                fail = ctk.CTkFrame(self._content, fg_color="transparent")
                ctk.CTkLabel(
                    fail,
                    text=f"页面加载失败（{nav_id}）\n{exc}\n请查看日志中心或 logs/app.log",
                    text_color=COLORS["danger"],
                    wraplength=480,
                    justify="left",
                ).pack(anchor="w", padx=16, pady=16)
                self._nav[nav_id] = fail
            try:
                self.update_idletasks()
            except Exception:
                pass
            self.after(1, self._page_build_tick)
        except Exception as exc:
            error(f"主界面加载失败：{exc}")
            self._show_ui_bootstrap_error(str(exc))

    def shutdown_ui(self) -> None:
        self._log_ui_pump_on = False
        if self._log_listener is not None:
            try:
                remove_memory_listener(self._log_listener)
            except Exception:
                pass
            self._log_listener = None

    def _raise_main_window(self) -> None:
        if self._embedded:
            return
        try:
            self.deiconify()
        except TclError:
            pass

    def _tg_session_running(self) -> bool:
        return self._coord is not None and self._coord.is_running()

    def _warn_if_tg_session_down(self) -> None:
        if self._coord is not None and not self._coord.is_running():
            warning("任务状态已更新，但 Telegram 统一会话未在运行，请点侧栏「保存并重载服务」。")

    def _show_ui_bootstrap_error(self, msg: str) -> None:
        """界面分步构建失败时，在右侧显示错误而不是留黑屏。"""
        try:
            for ch in self._content.winfo_children():
                ch.destroy()
            ctk.CTkLabel(
                self._content,
                text=f"界面加载失败\n{msg}\n\n请重启程序；若仍失败请查看 logs/app.log",
                font=ctk.CTkFont(size=14),
                text_color=COLORS["danger"],
                wraplength=520,
                justify="left",
            ).grid(row=0, column=0, padx=20, pady=20, sticky="nw")
        except Exception:
            pass

    def _account_id_values(self) -> List[str]:
        ids = [a.id for a in self._cfg.accounts]
        return ids if ids else ["default"]

    def _owner_account_values(self) -> List[str]:
        """通讯录归属账号：列出账号管理中全部账号（不限于勾选启用）。"""
        return [a.id for a in self._cfg.accounts]

    def _refresh_owner_account_combos(self) -> None:
        vals = self._owner_account_values()
        if getattr(self, "_addr_owner", None) is None:
            return
        cur = self._addr_owner.get().strip()
        if vals:
            self._addr_owner.configure(values=vals)
            self._addr_owner.set(cur if cur in vals else vals[0])
        else:
            self._addr_owner.configure(values=["—"])
            self._addr_owner.set("—")

    def _after_address_book_order_changed(self) -> None:
        self._render_group_rows(force=True)
        if getattr(self, "_sched_targets", None) is not None:
            self._refresh_schedule_target_checks()

    def _move_address_book_entry(self, idx: int, delta: int) -> None:
        book = self._cfg.address_book
        j = idx + delta
        if idx < 0 or idx >= len(book) or j < 0 or j >= len(book):
            return
        book[idx], book[j] = book[j], book[idx]
        self._optional_merge_global_api_from_ui()
        save_config(self._cfg)
        self._after_address_book_order_changed()

    def _persist_address_remark(self, idx: int, remark: str) -> None:
        text = (remark or "").strip()
        if not text:
            info("备注不能为空。")
            return
        if idx < 0 or idx >= len(self._cfg.address_book):
            return
        ent = self._cfg.address_book[idx]
        if ent.remark == text:
            return
        ent.remark = text
        self._optional_merge_global_api_from_ui()
        save_config(self._cfg)
        if getattr(self, "_sched_targets", None) is not None:
            self._refresh_schedule_target_checks()
        w = self._grp_row_widgets.get(ent.id)
        if w:
            w["title"].configure(text=f"{idx + 1}. {text}")
        else:
            self._render_group_rows(force=True)
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
        self._optional_merge_global_api_from_ui()
        save_config(self._cfg)
        self._refresh_schedule_target_checks()
        self._patch_group_rows()

    def _mount_main_scroll(
        self, page: ctk.CTkFrame, footer: Optional[ctk.CTkFrame] = None
    ) -> tuple[ctk.CTkFrame, Callable[[], None]]:
        """主内容区：Canvas 滚动 + 全页滚轮绑定。返回 (inner, finish_scroll_bind)。"""
        inner, canvas, finish = mount_page_scroll(page, footer=footer, bg=COLORS["bg"])
        self._scroll_wheel_handler = lambda e, c=canvas: scroll_wheel(c, e)
        return inner, finish

    def _elastic_wraplabels(self, scroll_widget: ctk.CTkFrame, labels: List[ctk.CTkLabel], inset: int = 56) -> None:
        """说明文字随滚动区宽度自动折行，避免固定 wraplength 在窄窗口下溢出。"""
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
        self.after_idle(sync)

    def _bind_label_wrap_to_card_width(self, card: ctk.CTkFrame, label: ctk.CTkLabel, inset: int = 28) -> None:
        """卡片变窄时自动收紧 Label 折行宽度，避免横向溢出。"""
        def sync(_e: Any = None) -> None:
            try:
                w = int(card.winfo_width())
                if w > inset + 80:
                    label.configure(wraplength=max(200, w - inset))
            except Exception:
                pass

        card.bind("<Configure>", sync)
        self.after_idle(sync)

    def bind_coordinator(self, coord: TelethonCoordinator) -> None:
        self._coord = coord

    def _doc_schedule_reminder(
        self, job: ScheduledJob, item: DocMessage, step_one_based: int, paused_count: int = 0
    ) -> None:
        grp = format_job_targets_label(load_config(), job)

        def show() -> None:
            try:
                body = item.reminder_note.strip() if item.reminder_note else "请关注当前任务进度。"
                if paused_count > 0:
                    body += f"\n\n已自动暂停 {paused_count} 个相关定时任务，请在「任务管理」页点卡片或「一键开始全部任务」。"
                show_stage_reminder(
                    self,
                    title="定时任务 · 阶段提醒",
                    subtitle=f"群：{grp} · 任务「{job.source_name}」· 第 {step_one_based} 步",
                    body=body,
                )
            except Exception:
                pass
            self._render_taskmgr_cards()

        self.after(0, show)

    def alert_callback(self, payload: Dict) -> None:
        """由 Telethon 后台线程调用：必须经 after 投递到 Tk 主线程，否则暂停/刷新可能不生效。"""
        p = dict(payload)

        def run_on_main() -> None:
            try:
                record_alert()
                chat_id = int(p.get("chat_id"))
                raw = p.get("chat_id_raw")
                raw_i = int(raw) if raw is not None else None
                ev_u = str(p.get("chat_username") or "").strip() or None
                ev_t = str(p.get("chat_title") or "").strip() or None
                chat_disp = format_listener_chat_label(
                    load_config(),
                    peer_id=chat_id,
                    chat_title=ev_t or "",
                    chat_id_raw=raw_i,
                    chat_username=ev_u,
                )
                paused = self._scheduler.pause_jobs_by_chat(
                    chat_id,
                    LISTEN_HIT_PAUSE_REASON,
                    raw_chat_id=raw_i,
                    event_username=ev_u,
                    event_title=ev_t,
                )
                if paused == 0 and raw_i is not None and raw_i != chat_id:
                    paused += self._scheduler.pause_jobs_by_chat(
                        raw_i,
                        LISTEN_HIT_PAUSE_REASON,
                        raw_chat_id=chat_id,
                        event_username=ev_u,
                        event_title=ev_t,
                    )
                if paused > 0:
                    info(f"群 {chat_disp} 触发监听提醒：已自动暂停 {paused} 个文档任务，等待手动继续")
                    self._render_taskmgr_cards()
            except Exception as exc:
                error(f"监听命中后暂停定时任务失败：{exc}")
            try:
                AlertPopup(
                    self,
                    chat_title=str(p.get("chat_title", "")),
                    chat_meta=chat_disp,
                    sender_name=str(p.get("sender_name", "")),
                    message_text=str(p.get("message_text", "")),
                    chat_username=p.get("chat_username"),
                )
            except Exception as exc:
                error(f"显示监听弹窗失败：{exc}")

        try:
            self.after(0, run_on_main)
        except Exception as exc:
            error(f"投递监听提醒到主线程失败：{exc}")

    def _on_exit(self) -> None:
        # 不在此线程 join Telethon（会卡窗体数秒）；释放 UI 后由 main() 的 finally 统一 stop。
        self._log_ui_pump_on = False
        if self._log_listener:
            remove_memory_listener(self._log_listener)
        self.destroy()

    def _on_log_line(self, line: str) -> None:
        try:
            self._log_ui_queue.put_nowait(line)
        except Exception:
            pass
        dash = getattr(self, "_dash_logs", None)
        if dash is not None:
            self.after(0, lambda l=line: append_log_line_capped(dash, l, max_lines=DASH_LOG_MAX_LINES))

    def _pump_log_ui_queue(self) -> None:
        """主线程消费日志队列；Telethon/重载等后台线程只入队，避免跨线程直接改 Tk 卡死。"""
        if not getattr(self, "_log_ui_pump_on", True):
            return
        lb = getattr(self, "_log_box", None)
        for _ in range(300):
            try:
                line = self._log_ui_queue.get_nowait()
            except queue.Empty:
                break
            if lb is None:
                continue
            try:
                append_log_line_capped(lb, line)
            except Exception:
                break
        if self._log_ui_pump_on:
            try:
                if self.winfo_exists():
                    self.after(LOG_PUMP_MS, self._pump_log_ui_queue)
            except Exception:
                pass

    def _restart_services(self) -> bool:
        """停止并重新启动监听/定时后台线程。可能阻塞数十秒（内部 join），勿在主 UI 线程直接调用。"""
        if self._coord is not None:
            if not self._coord.stop(join_timeout=DEFAULT_JOIN_TIMEOUT):
                error(
                    "重载已中止：Telegram 会话线程未在时限内退出。"
                    "请先暂停全部定时任务，等待约半分钟后再试，或完全退出程序后重开。"
                )
                return False
        else:
            self._listener.stop()
            self._scheduler.stop()
        self._cfg = load_config()
        self._listener.start(self._cfg, self.alert_callback)
        self._scheduler.start(self._cfg)
        if self._coord is not None:
            if not self._coord.start(self._cfg):
                return False
        return True

    def _invoke_restart_in_background(self, on_main_thread: Optional[Callable[[], None]] = None) -> None:
        """在后台线程执行重载，避免主界面在 join Telethon 线程时卡死。"""
        if self._service_reload_busy:
            info("重载服务进行中，请稍候再试")
            return
        self._service_reload_busy = True

        def worker() -> None:
            ok = False
            try:
                ok = self._restart_services()
            except Exception as exc:
                error(f"重载服务失败：{exc}")
            finally:
                def done() -> None:
                    self._service_reload_busy = False
                    if not ok:
                        info("服务重载未完成，请查看日志并按提示操作。")
                    if on_main_thread is not None and ok:
                        try:
                            on_main_thread()
                        except Exception:
                            pass

                self.after(0, done)

        threading.Thread(target=worker, daemon=True, name="tg-restart-services").start()

    def _toggle_window_zoom(self) -> None:
        """仅在「固定小窗口」与「最大化」之间切换，不提供任意拉伸。"""
        try:
            if self.state() == "zoomed":
                self.state("normal")
                self.after(80, lambda: self.geometry(MAIN_WINDOW_GEOMETRY))
            else:
                self.state("zoomed")
        except TclError:
            try:
                z = bool(self.attributes("-zoomed"))
                self.attributes("-zoomed", not z)
                if z:
                    self.after(80, lambda: self.geometry(MAIN_WINDOW_GEOMETRY))
            except Exception:
                info("当前环境无法程序化最大化，请使用窗口标题栏按钮")

    def _build_sidebar(self) -> None:
        side = ctk.CTkFrame(self, width=SIDEBAR_WIDTH, fg_color=COLORS["sidebar"], corner_radius=0)
        side.grid(row=0, column=0, sticky="nsew")

        logo = ctk.CTkLabel(
            side,
            text="TG Listener",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=COLORS["text"],
        )
        logo.pack(anchor="w", padx=18, pady=(24, 8))

        sub = ctk.CTkLabel(side, text="监听 · 提醒 · 登录一体化", font=ctk.CTkFont(size=12), text_color=COLORS["muted"])
        sub.pack(anchor="w", padx=18, pady=(0, 20))

        for nav_id, label in (
            ("dash", "仪表盘"),
            ("acct", "账号管理"),
            ("grp", "通讯录"),
            ("rules", "监听规则"),
            ("sched", "定时任务"),
            ("taskmgr", "任务管理"),
            ("logs", "日志中心"),
        ):
            b = ctk.CTkButton(
                side,
                text=label,
                anchor="w",
                height=36,
                fg_color="transparent",
                text_color=COLORS["muted"],
                hover_color=COLORS["card"],
                command=lambda n=nav_id: self._show_nav(n),
            )
            b.pack(fill="x", padx=10, pady=4)

        save_btn = ctk.CTkButton(
            side,
            text="保存并重载服务",
            fg_color=COLORS["accent"],
            hover_color="#3d7ae6",
            command=self._save_all_and_restart,
        )
        save_btn.pack(side="bottom", fill="x", padx=14, pady=(8, 16))

        ctk.CTkButton(
            side,
            text="最大化 / 还原",
            fg_color=COLORS["border"],
            command=self._toggle_window_zoom,
        ).pack(side="bottom", fill="x", padx=14, pady=(0, 8))

    def _wrap_page(self, parent: ctk.CTkFrame, title: str) -> ctk.CTkFrame:
        wrap, finish = self._mount_main_scroll(parent)
        wrap.grid_columnconfigure(0, weight=1)
        t = ctk.CTkLabel(
            wrap,
            text=title,
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color=COLORS["text"],
        )
        t.grid(row=0, column=0, sticky="w", padx=4, pady=(8, 16))
        finish()
        return wrap

    def _tick_schedule_job_lists(self) -> None:
        """任务管理页可见时增量刷新任务卡片（不整页重建，避免闪烁）。"""
        if not getattr(self, "_log_ui_pump_on", True):
            return
        try:
            if self.winfo_exists() and self._pages_ready:
                fr = self._nav.get("taskmgr")
                if fr is not None and fr.winfo_ismapped():
                    self._render_taskmgr_cards(force=False)
        except Exception:
            pass
        if self._log_ui_pump_on:
            try:
                if self.winfo_exists():
                    self.after(TASKMGR_TICK_MS, self._tick_schedule_job_lists)
            except Exception:
                pass

    def _format_send_progress(self, total: int, done: int, remain: int, *, step_total: int = 0) -> str:
        if total <= 0:
            return "无发送条目"
        if remain <= 0:
            base = f"已发 {done} 条，已全部发完"
        else:
            base = f"已发 {done} 条，还剩 {remain} 条"
        if step_total > total:
            return f"{base}（文档 {step_total} 步，含 {step_total - total} 个提醒）"
        return base

    def _show_nav(self, nav_id: NavId) -> None:
        if not self._pages_ready or not self._nav:
            return
        for k, fr in self._nav.items():
            if k == nav_id:
                fr.grid()
            else:
                fr.grid_remove()
        if nav_id == "dash":
            self._refresh_dashboard()
            self._schedule_login_probe()
        elif nav_id == "acct":
            self._schedule_login_probe()
            self._render_account_rows()
        elif nav_id == "grp":
            self._refresh_owner_account_combos()
            self._patch_group_rows()
        elif nav_id == "sched":
            self._refresh_schedule_target_checks()
        elif nav_id == "taskmgr":
            self._render_taskmgr_cards()
        elif nav_id == "logs":
            self._flush_logs_ui()

    def _schedule_login_probe(self) -> None:
        """后台检测各账号 session 是否已授权；结果用于账号行样式与仪表盘。"""
        self._login_probe_gen += 1
        gen = self._login_probe_gen

        def worker() -> None:
            # 必须等 Telegram 统一会话线程把 listener._running 置为 True 后再碰 .session，
            # 否则与 TelethonCoordinator 同时 asyncio.run 连同一库会导致长时间锁死，表现为「有进程无界面」。
            deadline = time.time() + 12.0
            while not self._listener.is_running() and time.time() < deadline:
                time.sleep(0.05)
            if not self._listener.is_running():
                info("统一会话尚未就绪，跳过本次登录预检测（避免与后台连接争用 session 文件）")

                def apply_skip() -> None:
                    if gen != self._login_probe_gen:
                        return
                    if getattr(self, "_pages_ready", False):
                        self._refresh_dashboard()

                self.after(0, apply_skip)
                return
            cfg = load_config()
            status: Dict[str, Optional[bool]] = {}
            for a in cfg.accounts:
                try:
                    # 监听已在跑时，不再并发探测同一 session，避免偶发锁冲突导致误判未登录。
                    if a.enabled and self._listener.is_running():
                        status[a.id] = True
                    else:
                        status[a.id] = bool(is_session_authorized_sync(a, cfg))
                except Exception:
                    status[a.id] = False

            def apply() -> None:
                if gen != self._login_probe_gen:
                    return
                self._login_status = status
                if not getattr(self, "_pages_ready", False):
                    return
                if self._nav.get("acct") and self._nav["acct"].winfo_ismapped():
                    self._patch_account_rows()
                self._refresh_dashboard()

            self.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _card(self, parent: Any, row: int) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        card.grid(row=row, column=0, sticky="ew", pady=8)
        card.grid_columnconfigure(0, weight=1)
        return card

    def _page_dashboard(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._content, fg_color="transparent")
        wrap = self._wrap_page(page, "仪表盘")

        c1 = self._card(wrap, 1)
        c1.grid_columnconfigure(0, weight=1)
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
            wraplength=840,
        )
        self._dash_acct.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 12))

        c4 = self._card(wrap, 4)
        ctk.CTkLabel(c4, text="最近日志", font=ctk.CTkFont(size=13, weight="bold"), text_color=COLORS["text"]).grid(
            row=0, column=0, sticky="w", padx=16, pady=(12, 4)
        )
        self._dash_logs = ctk.CTkTextbox(
            c4,
            height=180,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color=COLORS["bg"],
            text_color=COLORS["muted"],
            border_width=1,
            border_color=COLORS["border"],
        )
        self._dash_logs.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 12))
        bind_log_textbox_wheel(self._dash_logs)
        reload_log_textbox_from_memory(
            self._dash_logs,
            get_recent_lines,
            limit=DASH_LOG_MAX_LINES,
            max_lines=DASH_LOG_MAX_LINES,
        )

        self._elastic_wraplabels(wrap, [self._dash_listen, self._dash_acct])
        return page

    def _refresh_dashboard(self) -> None:
        n = today_alert_count()
        self._dash_today.configure(text=f"今日已提醒 {n} 次")
        listen_on = bool(self._cfg.listening_enabled)
        conn = self._listener.is_running()
        parts = [
            f"监听总开关：{'开' if listen_on else '关'}",
            f"Telegram 连接：{'已连接' if conn else '未连接'}",
        ]
        if listen_on and conn:
            parts.append("（正在监听配置中的群与用户）")
        elif listen_on and not conn:
            parts.append("（总开关已开但未连上，可检查网络与账号登录）")
        self._dash_listen.configure(text="  ·  ".join(parts))
        lines = []
        for a in self._cfg.accounts:
            st = self._login_status.get(a.id)
            if st is True:
                st_txt = "已登录"
            elif st is False:
                st_txt = "未登录"
            else:
                st_txt = "登录状态检测中…"
            extra = f"，文件:{a.session_name}" if a.session_name != a.id else ""
            lines.append(f"{a.id} · {st_txt} · {'启用' if a.enabled else '停用'}{extra}")
        self._dash_acct.configure(text="\n".join(lines) if lines else "（未配置账号）")
        if hasattr(self, "_dash_logs"):
            reload_log_textbox_from_memory(
                self._dash_logs,
                get_recent_lines,
                limit=DASH_LOG_MAX_LINES,
                max_lines=DASH_LOG_MAX_LINES,
            )

    # --- accounts ---
    def _page_accounts(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._content, fg_color="transparent")

        acct_foot = ctk.CTkFrame(page, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        af = ctk.CTkFrame(acct_foot, fg_color="transparent")
        af.pack(fill="x", padx=12, pady=10)
        ctk.CTkButton(
            af,
            text="打开 my.telegram.org（查看 / 创建应用）",
            fg_color=COLORS["border"],
            command=lambda: webbrowser.open("https://my.telegram.org/apps"),
        ).pack(fill="x", pady=(0, 6))
        ctk.CTkButton(af, text="仅保存到列表", fg_color=COLORS["border"], command=self._add_account_row_ui).pack(fill="x", pady=(0, 6))
        ctk.CTkButton(af, text="登录此账号", fg_color=COLORS["accent"], hover_color="#3d7ae6", command=self._login_from_form).pack(fill="x")

        wrap, finish_scroll = self._mount_main_scroll(page, footer=acct_foot)

        ctk.CTkLabel(wrap, text="账号管理", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLORS["text"]).pack(anchor="w", pady=(8, 4))
        intro_acct = ctk.CTkLabel(
            wrap,
            text="Telegram 规定每个「应用」有一套 API；您所有账号共用这一套即可。下面重点是：给每个号起名 → 登录（弹窗里依次输入手机号、验证码，如有则二步验证密码）。",
            text_color=COLORS["muted"],
            wraplength=520,
            justify="left",
        )
        intro_acct.pack(anchor="w", pady=(0, 14))

        api_card = ctk.CTkFrame(wrap, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        api_card.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(api_card, text="共用接口（全局只填一次）", font=ctk.CTkFont(size=14, weight="bold"), text_color=COLORS["text"]).pack(
            anchor="w", padx=14, pady=(12, 6)
        )
        api_hint = ctk.CTkLabel(
            api_card,
            text="来自 my.telegram.org，与登录哪个 Telegram 账号无关；填好后侧栏「保存并重载」会写入配置文件。",
            text_color=COLORS["muted"],
            wraplength=520,
            justify="left",
        )
        api_hint.pack(anchor="w", padx=14, pady=(0, 10))

        api_form = ctk.CTkFrame(api_card, fg_color="transparent")
        api_form.pack(fill="x", padx=14, pady=(0, 12))
        api_form.grid_columnconfigure(0, weight=1)
        self._glob_api_id = ctk.CTkEntry(api_form, placeholder_text="API ID（数字）")
        self._glob_api_hash = ctk.CTkEntry(api_form, placeholder_text="API Hash（长字符串）")
        self._glob_api_id.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self._glob_api_hash.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        if self._cfg.api_id:
            self._glob_api_id.insert(0, str(self._cfg.api_id))
        if self._cfg.api_hash:
            self._glob_api_hash.insert(0, self._cfg.api_hash)

        login_card = ctk.CTkFrame(wrap, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        login_card.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(login_card, text="登录流程：添加或选中一个账号身份", font=ctk.CTkFont(size=14, weight="bold"), text_color=COLORS["text"]).pack(
            anchor="w", padx=14, pady=(12, 6)
        )
        login_intro = ctk.CTkLabel(
            login_card,
            text="① 起一个账号简称（英文/数字）② 点「登录此账号」→ 弹窗里依次输入手机号 → 验证码 →（如有）二步验证密码。登录文件会自动保存为 sessions/简称.session，无需再填文件名。",
            text_color=COLORS["muted"],
            wraplength=520,
            justify="left",
        )
        login_intro.pack(anchor="w", padx=14, pady=(0, 10))

        form = ctk.CTkFrame(login_card, fg_color="transparent")
        form.pack(fill="x", padx=14, pady=(0, 14))
        form.grid_columnconfigure(0, weight=1)
        self._acc_id = ctk.CTkEntry(form, placeholder_text="例如 work、a1（同时作为登录文件名）")
        self._acc_phone = ctk.CTkEntry(form, placeholder_text="备注手机号（可选）")

        rows = (
            ("账号简称", self._acc_id, "定时任务与 TXT 里的「账号」填这个名；磁盘上对应 sessions/该名.session"),
            ("备注", self._acc_phone, "仅备忘，不参与登录"),
        )
        r = 0
        form_hints: List[ctk.CTkLabel] = []
        for lab, widget, hint in rows:
            ctk.CTkLabel(form, text=lab, text_color=COLORS["text"]).grid(row=r, column=0, padx=0, pady=(8, 4), sticky="w")
            widget.grid(row=r + 1, column=0, sticky="ew", pady=(0, 4))
            hl = ctk.CTkLabel(form, text=hint, text_color=COLORS["muted"], font=ctk.CTkFont(size=11), wraplength=360, justify="left")
            hl.grid(row=r + 2, column=0, sticky="w", pady=(0, 8))
            form_hints.append(hl)
            r += 3

        self._elastic_wraplabels(wrap, [intro_acct, api_hint, login_intro, *form_hints])

        self._acc_rows_title = ctk.CTkLabel(wrap, text="已保存的账号（勾选参与监听；未登录时点「登录」）", font=ctk.CTkFont(size=14, weight="bold"), text_color=COLORS["text"])
        self._acc_rows_title.pack(anchor="w", pady=(10, 8))
        self._acc_rows = ctk.CTkFrame(wrap, fg_color="transparent")
        self._acc_rows.pack(fill="both", expand=True)

        self._render_account_rows(force=True)
        finish_scroll()
        return page

    def _account_login_style(self, account_id: str) -> tuple:
        st = self._login_status.get(account_id)
        if st is True:
            return COLORS["success"], 2, "已登录", COLORS["success"]
        if st is False:
            return COLORS["danger"], 2, "未登录", COLORS["danger"]
        return COLORS["border"], 1, "检测中…", COLORS["muted"]

    def _apply_account_tile(self, w: Dict[str, Any], a: Account) -> None:
        bcol, bw, badge, bfg = self._account_login_style(a.id)
        w["card"].configure(border_width=bw, border_color=bcol)
        w["badge"].configure(text=badge, text_color=bfg)

    def _build_account_tile(self, a: Account, index: int, acc_index: int) -> Dict[str, Any]:
        bcol, bw, badge, bfg = self._account_login_style(a.id)
        card = ctk.CTkFrame(
            self._acc_rows,
            fg_color=COLORS["card"],
            corner_radius=10,
            border_width=bw,
            border_color=bcol,
            height=150,
        )
        grid_place(card, index, TG_ACCT_COLS, padx=6, pady=6)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=10, pady=10)
        v = ctk.BooleanVar(value=a.enabled)

        def make_toggle(idx: int, var: ctk.BooleanVar):
            def on_change(*_a: object) -> None:
                self._cfg.accounts[idx].enabled = bool(var.get())
                save_config(self._cfg)
                self._refresh_owner_account_combos()

            return on_change

        disp = a.id
        if a.session_name != a.id:
            disp = f"{a.id}\n({a.session_name})"
        cb = ctk.CTkCheckBox(
            inner,
            text=disp,
            variable=v,
            command=make_toggle(acc_index, v),
            text_color=COLORS["text"],
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        cb.pack(anchor="w", pady=(0, 6))
        badge_lbl = ctk.CTkLabel(inner, text=badge, font=ctk.CTkFont(size=12, weight="bold"), text_color=bfg, anchor="w")
        badge_lbl.pack(anchor="w", pady=(0, 8))
        ctk.CTkButton(
            inner,
            text="登录",
            height=28,
            fg_color=COLORS["accent"],
            hover_color="#3d7ae6",
            command=lambda idx=acc_index: self._login_account_idx(idx),
        ).pack(fill="x", pady=(0, 4))
        ctk.CTkButton(
            inner,
            text="删除",
            height=26,
            fg_color=COLORS["border"],
            command=lambda idx=acc_index: self._del_account(idx),
        ).pack(fill="x")
        return {"card": card, "badge": badge_lbl, "var": v, "cb": cb}

    def _full_rebuild_account_grid(self) -> None:
        for ch in self._acc_rows.winfo_children():
            ch.destroy()
        self._acc_widgets.clear()
        self._acc_vars = []
        configure_equal_columns(self._acc_rows, TG_ACCT_COLS, uniform="tg_acct")
        for i, a in enumerate(self._cfg.accounts):
            self._acc_widgets[a.id] = self._build_account_tile(a, i, i)
            self._acc_vars.append(self._acc_widgets[a.id]["var"])
        self._acc_listed_ids = [a.id for a in self._cfg.accounts]

    def _patch_account_rows(self) -> None:
        if not getattr(self, "_acc_widgets", None):
            self._render_account_rows(force=True)
            return
        ids = [a.id for a in self._cfg.accounts]
        if ids != self._acc_listed_ids:
            self._render_account_rows(force=True)
            return
        for a in self._cfg.accounts:
            w = self._acc_widgets.get(a.id)
            if w:
                self._apply_account_tile(w, a)

    def _render_account_rows(self, *, force: bool = False) -> None:
        if getattr(self, "_acc_rows", None) is None:
            return
        ids = [a.id for a in self._cfg.accounts]
        if not force and ids == self._acc_listed_ids and self._acc_widgets:
            self._patch_account_rows()
            self._refresh_schedule_combo()
            return
        self._full_rebuild_account_grid()
        self._refresh_schedule_combo()
        handler = getattr(self, "_scroll_wheel_handler", None)
        if handler:
            bind_scroll_tree_once(self._acc_rows, handler)

    def _merge_global_api_from_ui_into_cfg(self) -> bool:
        """将顶部共用 API 写入内存配置；失败返回 False。"""
        if getattr(self, "_glob_api_id", None) is None:
            return bool(self._cfg.api_id and str(self._cfg.api_hash).strip())
        try:
            aid = int(self._glob_api_id.get().strip())
        except ValueError:
            return False
        h = self._glob_api_hash.get().strip()
        if aid <= 0 or not h:
            return False
        self._cfg.api_id = aid
        self._cfg.api_hash = h
        return True

    def _optional_merge_global_api_from_ui(self) -> None:
        """保存配置时尽量带上顶部 API；格式不对则保留原配置。"""
        if getattr(self, "_glob_api_id", None) is None:
            return
        try:
            aid = int(self._glob_api_id.get().strip())
            h = self._glob_api_hash.get().strip()
            if aid > 0 and h:
                self._cfg.api_id = aid
                self._cfg.api_hash = h
        except ValueError:
            pass

    def _parse_account_form(self) -> Optional[Account]:
        if not self._merge_global_api_from_ui_into_cfg():
            info("请先在上方填写共用的 API ID 与 API Hash（所有账号共用这一套）。")
            return None
        aid = self._acc_id.get().strip() or "default"
        phone = self._acc_phone.get().strip()
        return Account(id=aid, session_name=aid, enabled=True, phone=phone)

    def _login_account_idx(self, idx: int) -> None:
        try:
            acc = self._cfg.accounts[idx]
        except IndexError:
            return
        if not self._merge_global_api_from_ui_into_cfg():
            info("请先在页面顶部填写共用的 API ID 与 API Hash。")
            return
        save_config(self._cfg)
        self._begin_login(acc)

    def _login_from_form(self) -> None:
        acc = self._parse_account_form()
        if acc is None:
            return
        replaced = False
        for i, x in enumerate(self._cfg.accounts):
            if x.id == acc.id:
                self._cfg.accounts[i] = acc
                replaced = True
                break
        if not replaced:
            self._cfg.accounts.append(acc)
        save_config(self._cfg)
        self._render_account_rows()
        self._begin_login(acc)

    def _stop_backend_for_exclusive_login(self) -> None:
        """在登录专用线程内调用：必须等统一会话彻底释放 .session，否则会 sqlite/database locked 卡死。"""
        info("正在停止监听与定时任务并等待会话释放，用于安全登录…")
        if self._coord is not None:
            if not self._coord.stop(join_timeout=DEFAULT_JOIN_TIMEOUT):
                raise RuntimeError(
                    "Telegram 会话线程未在时限内退出，请暂停定时任务后稍候再试，或完全退出程序后重开。"
                )
        else:
            self._listener.stop()
            self._scheduler.stop()

    def _begin_login(self, acc: Account) -> None:
        if not self._merge_global_api_from_ui_into_cfg():
            info("请先在页面顶部填写共用的 API ID 与 API Hash。")
            return
        save_config(self._cfg)
        info(f"正在为账号「{acc.id}」登录…（将短暂停止监听与定时任务；请按弹窗依次完成手机号 / 验证码 / 二步验证）")

        login_id = acc.id

        def on_done(ok: bool, msg: str) -> None:
            info(msg)
            self._cfg = load_config()
            self._sync_global_api_inputs_after_cfg_reload()
            self._login_status[login_id] = ok

            def after_restart() -> None:
                self._render_account_rows()
                self._refresh_owner_account_combos()
                self._refresh_schedule_combo()
                self._refresh_schedule_target_checks()
                self._render_taskmgr_cards()
                self._refresh_dashboard()
                self._schedule_login_probe()
                info("服务已根据配置重启")

            self._invoke_restart_in_background(after_restart)

        run_login_in_thread(self, acc, self._cfg, on_done, pre_login=self._stop_backend_for_exclusive_login)

    def _sync_global_api_inputs_after_cfg_reload(self) -> None:
        if getattr(self, "_glob_api_id", None) is None:
            return
        self._glob_api_id.delete(0, "end")
        self._glob_api_hash.delete(0, "end")
        if self._cfg.api_id:
            self._glob_api_id.insert(0, str(self._cfg.api_id))
        if self._cfg.api_hash:
            self._glob_api_hash.insert(0, self._cfg.api_hash)

    def _add_account_row_ui(self) -> None:
        self._optional_merge_global_api_from_ui()
        acc = self._parse_account_form()
        if acc is None:
            return
        self._cfg.accounts.append(acc)
        save_config(self._cfg)
        self._acc_id.delete(0, "end")
        self._acc_phone.delete(0, "end")
        self._render_account_rows()
        self._refresh_owner_account_combos()
        if self._coord is not None and self._coord.has_connected_clients():
            self._coord.apply_config_hot(self._cfg)
        else:
            self._scheduler.start(self._cfg)
        info(f"已保存账号「{acc.id}」；可勾选启用后点「登录」，或「保存并重载服务」。")

    def _refresh_schedule_combo(self) -> None:
        if getattr(self, "_jacc", None) is None:
            return
        ids = [a.id for a in self._cfg.accounts] or ["default"]
        self._jacc.configure(values=ids)

    def _del_account(self, idx: int) -> None:
        try:
            dead = self._cfg.accounts.pop(idx)
        except IndexError:
            return
        self._login_status.pop(dead.id, None)
        for ent in self._cfg.address_book:
            if ent.owner_account_id == dead.id:
                ent.owner_account_id = ""
        self._optional_merge_global_api_from_ui()
        save_config(self._cfg)
        self._render_account_rows()
        self._refresh_owner_account_combos()
        self._refresh_schedule_combo()
        info(f"已删除账号「{dead.id}」并已保存到配置。")
        if self._coord is not None and self._coord.has_connected_clients():
            def after_reload() -> None:
                self._render_account_rows()
                self._refresh_owner_account_combos()
                self._refresh_dashboard()
                self._schedule_login_probe()

            self._invoke_restart_in_background(after_reload)

    # --- 通讯录（群 + 用户，一处保存后监听/定时任务里选用） ---
    def _page_groups(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._content, fg_color="transparent")

        grp_foot = ctk.CTkFrame(page, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        gf = ctk.CTkFrame(grp_foot, fg_color="transparent")
        gf.pack(fill="x", padx=12, pady=10)
        ctk.CTkButton(gf, text="添加到通讯录", fg_color=COLORS["accent"], command=self._add_address_entry).pack(fill="x", pady=(0, 6))
        ctk.CTkButton(gf, text="保存通讯录", fg_color=COLORS["border"], command=self._save_address_book).pack(fill="x")

        wrap, finish_scroll = self._mount_main_scroll(page, footer=grp_foot)
        ctk.CTkLabel(wrap, text="通讯录（群与用户）", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLORS["text"]).pack(
            anchor="w", pady=(8, 8)
        )
        intro_grp = ctk.CTkLabel(
            wrap,
            text="填写「备注、群、监听用户」后点「添加到通讯录」或本页「保存通讯录」写入 config.json。"
            "修改监听参与状态也会自动保存。每条可用 ↑↓ 调整顺序，定时任务页群发目标顺序与此一致。"
            "若要让 Telegram 监听立刻按新通讯录生效，请在侧栏再点「保存并重载服务」。",
            text_color=COLORS["muted"],
            wraplength=520,
            justify="left",
        )
        intro_grp.pack(anchor="w", pady=(0, 10))

        hint = ctk.CTkFrame(wrap, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        hint.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(hint, text="填写说明", font=ctk.CTkFont(size=13, weight="bold"), text_color=COLORS["text"]).pack(
            anchor="w", padx=14, pady=(12, 6)
        )
        gid_help = (
            "群：可填 -100… 数字 ID、公开 @群用户名或 t.me 链接。\n"
            "监听用户：填数字 ID 或 @用户名；若本条仅用于定时群发、不参与关键字监听，请取消勾选「参与监听」，用户可留空。\n"
            "勾选「参与监听」时，必须填写监听用户。"
        )
        gid_lbl = ctk.CTkLabel(hint, text=gid_help, text_color=COLORS["muted"], justify="left", wraplength=520)
        gid_lbl.pack(anchor="w", padx=14, pady=(0, 14))

        self._elastic_wraplabels(wrap, [intro_grp, gid_lbl])

        self._grp_rows = ctk.CTkFrame(wrap, fg_color="transparent")
        self._grp_rows.pack(fill="x")

        form = ctk.CTkFrame(wrap, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        form.pack(fill="x")
        ctk.CTkLabel(form, text="备注（显示名）", text_color=COLORS["muted"]).pack(anchor="w", padx=12, pady=(12, 4))
        self._addr_remark = ctk.CTkEntry(form, placeholder_text="如：客户群A")
        self._addr_remark.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(form, text="群", text_color=COLORS["muted"]).pack(anchor="w", padx=12, pady=(4, 4))
        self._addr_chat = ctk.CTkEntry(form, placeholder_text="数字 ID / @群名 / t.me 链接")
        self._addr_chat.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(form, text="监听用户（不参与监听可留空）", text_color=COLORS["muted"]).pack(anchor="w", padx=12, pady=(4, 4))
        self._addr_user = ctk.CTkEntry(form, placeholder_text="数字 ID 或 @用户名")
        self._addr_user.pack(fill="x", padx=12, pady=(0, 8))
        own_row = ctk.CTkFrame(form, fg_color="transparent")
        own_row.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(own_row, text="归属账号（必选，重启后保留）", text_color=COLORS["muted"]).pack(side="left", padx=(0, 8))
        acc_own = self._owner_account_values()
        if not acc_own:
            ctk.CTkLabel(own_row, text="请先在账号管理添加账号", text_color=COLORS["danger"]).pack(side="left")
        self._addr_owner = ctk.CTkComboBox(own_row, width=160, values=acc_own or ["—"])
        self._addr_owner.pack(side="left")
        if acc_own:
            self._addr_owner.set(acc_own[0])
        self._addr_listen = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            form,
            text="参与监听",
            variable=self._addr_listen,
            text_color=COLORS["text"],
        ).pack(anchor="w", padx=12, pady=(4, 12))

        self._render_group_rows()
        finish_scroll()
        return page

    def _commit_address_book(self, *, resolve_online: bool = True) -> None:
        """保存通讯录；在线时解析群/用户并刷新监听，无需整程序重启。"""
        self._optional_merge_global_api_from_ui()
        save_config(self._cfg)
        self._render_group_rows(force=True)
        self._refresh_schedule_target_checks()
        if not resolve_online:
            info("通讯录已保存到配置文件。")
            return
        if self._coord is not None and self._coord.has_connected_clients():
            self._coord.apply_config_hot(self._cfg)
            self.after(800, self._patch_group_rows)
        else:
            info("通讯录已保存；账号在线后将自动解析群标识，或请点「保存并重载服务」。")

    def _save_address_book(self) -> None:
        self._commit_address_book(resolve_online=True)

    def _group_row_summary(self, ent: AddressEntry) -> str:
        listen_txt = "监听 ✓" if ent.listen_enabled else "仅定时"
        user_txt = ent.watch_user.strip() or "—"
        owner_txt = (ent.owner_account_id or "").strip() or "未选择"
        return f"群 {ent.chat_ref}  ·  用户 {user_txt}  ·  主号→{owner_txt}  ·  {listen_txt}"

    def _address_book_ids(self) -> List[str]:
        return [e.id for e in self._cfg.address_book]

    def _schedule_listen_config_save(self) -> None:
        aid = getattr(self, "_grp_listen_save_after", None)
        if aid:
            try:
                self.after_cancel(aid)
            except Exception:
                pass
        self._grp_listen_save_after = self.after(400, self._flush_listen_config_save)

    def _flush_listen_config_save(self) -> None:
        self._grp_listen_save_after = None
        self._optional_merge_global_api_from_ui()
        save_config(self._cfg)
        self._patch_group_rows()

    def _patch_group_row_widget(self, i: int, ent: AddressEntry, w: Dict[str, Any], n_book: int) -> None:
        w["title"].configure(text=f"{i + 1}. {ent.remark.strip() or ent.id}")
        w["summary"].configure(text=self._group_row_summary(ent))
        w["up_btn"].configure(state="normal" if i > 0 else "disabled")
        w["down_btn"].configure(state="normal" if i < n_book - 1 else "disabled")
        acc_vals: List[str] = w.get("_acc_vals") or []
        cur_own = (ent.owner_account_id or "").strip()
        if cur_own and cur_own in acc_vals:
            w["own_combo"].set(cur_own)
        else:
            w["own_combo"].set("请选择")
        try:
            w["listen_var"].set(bool(ent.listen_enabled))
        except Exception:
            pass

    def _patch_group_rows(self) -> None:
        if not getattr(self, "_grp_rows", None):
            return
        ids = self._address_book_ids()
        if ids != self._grp_row_ids or len(ids) != len(self._grp_row_widgets):
            self._render_group_rows(force=True)
            return
        n = len(ids)
        for i, ent in enumerate(self._cfg.address_book):
            w = self._grp_row_widgets.get(ent.id)
            if w:
                self._patch_group_row_widget(i, ent, w, n)

    def _render_group_rows(self, *, force: bool = False) -> None:
        if not getattr(self, "_grp_rows", None):
            return
        ids = self._address_book_ids()
        if not force and ids == self._grp_row_ids and len(self._grp_row_widgets) == len(ids):
            self._patch_group_rows()
            return
        for ch in self._grp_rows.winfo_children():
            ch.destroy()
        self._grp_row_widgets.clear()
        self._grp_row_ids = list(ids)
        acc_vals = self._owner_account_values()
        owner_vals = (["请选择"] + acc_vals) if acc_vals else ["请选择"]
        n_book = len(self._cfg.address_book)
        for i, ent in enumerate(self._cfg.address_book):
            row = ctk.CTkFrame(self._grp_rows, fg_color=COLORS["card"], corner_radius=10, border_width=1, border_color=COLORS["border"])
            row.pack(fill="x", pady=4)
            head = ctk.CTkFrame(row, fg_color="transparent")
            head.pack(fill="x", padx=12, pady=(8, 0))
            title_lb = ctk.CTkLabel(
                head,
                text=f"{i + 1}. {ent.remark.strip() or ent.id}",
                font=ctk.CTkFont(size=14, weight="bold"),
                text_color=COLORS["text"],
            )
            title_lb.pack(side="left")
            order_btns = ctk.CTkFrame(head, fg_color="transparent")
            order_btns.pack(side="right")
            up_btn = ctk.CTkButton(
                order_btns,
                text="↑",
                width=36,
                height=28,
                fg_color=COLORS["border"],
                state="normal" if i > 0 else "disabled",
                command=lambda idx=i: self._move_address_book_entry(idx, -1),
            )
            up_btn.pack(side="left", padx=(0, 4))
            down_btn = ctk.CTkButton(
                order_btns,
                text="↓",
                width=36,
                height=28,
                fg_color=COLORS["border"],
                state="normal" if i < n_book - 1 else "disabled",
                command=lambda idx=i: self._move_address_book_entry(idx, 1),
            )
            down_btn.pack(side="left")
            summary_lb = ctk.CTkLabel(
                row,
                text=self._group_row_summary(ent),
                text_color=COLORS["text"],
                justify="left",
                wraplength=640,
            )
            summary_lb.pack(anchor="w", padx=12, pady=(10, 4))
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

            listen_var = ctk.BooleanVar(value=ent.listen_enabled)

            def make_listen_toggle(idx: int, var: ctk.BooleanVar):
                def on_change(*_a: object) -> None:
                    if idx < len(self._cfg.address_book):
                        self._cfg.address_book[idx].listen_enabled = bool(var.get())
                    self._schedule_listen_config_save()

                return on_change

            ctk.CTkCheckBox(
                ctrl,
                text="参与监听",
                variable=listen_var,
                command=make_listen_toggle(i, listen_var),
                text_color=COLORS["muted"],
                width=100,
            ).pack(anchor="w")
            own_row = ctk.CTkFrame(ctrl, fg_color="transparent")
            own_row.pack(fill="x", pady=(6, 0))
            ctk.CTkLabel(own_row, text="归属账号", text_color=COLORS["muted"]).pack(side="left", padx=(0, 8))

            def make_owner_change(idx: int):
                def on_pick(choice: str) -> None:
                    self._persist_address_owner(idx, choice)

                return on_pick

            own_combo = ctk.CTkComboBox(own_row, width=140, values=owner_vals, command=make_owner_change(i))
            cur_own = (ent.owner_account_id or "").strip()
            if cur_own and cur_own in acc_vals:
                own_combo.set(cur_own)
            else:
                own_combo.set("请选择")
            own_combo.pack(side="left")
            ctk.CTkButton(ctrl, text="删除", fg_color=COLORS["border"], command=lambda eid=ent.id: self._del_address_entry(eid)).pack(
                fill="x", pady=(8, 0)
            )
            self._grp_row_widgets[ent.id] = {
                "row": row,
                "title": title_lb,
                "summary": summary_lb,
                "up_btn": up_btn,
                "down_btn": down_btn,
                "listen_var": listen_var,
                "own_combo": own_combo,
                "_acc_vals": acc_vals,
            }
        if not self._grp_scroll_bound:
            self._grp_scroll_bound = True
            handler = getattr(self, "_scroll_wheel_handler", None)
            if handler:
                bind_scroll_tree_once(self._grp_rows, handler)

    def _add_address_entry(self) -> None:
        remark = self._addr_remark.get().strip()
        g = self._addr_chat.get().strip()
        u = self._addr_user.get().strip()
        listen_on = bool(self._addr_listen.get())
        if not remark or not g:
            info("请填写备注与群标识。")
            return
        try:
            parse_chat_ref_input(g)
        except ValueError as exc:
            info(str(exc) or "群或频道标识无效。")
            return
        if listen_on:
            if not u:
                info("参与监听时，请填写要监听的用户（数字 ID 或 @用户名）。")
                return
            try:
                parse_watch_user_input(u)
            except ValueError:
                info("监听用户无效：请填写数字 ID，或对方已设置的公开 @用户名。")
                return
        elif u:
            try:
                parse_watch_user_input(u)
            except ValueError:
                info("用户格式无效。")
                return
        acc_vals = self._owner_account_values()
        if not acc_vals:
            info("请先在「账号管理」添加至少一个账号。")
            return
        owner = ""
        if getattr(self, "_addr_owner", None) is not None:
            owner = self._addr_owner.get().strip()
        if owner in ("", "—", "请选择"):
            owner = acc_vals[0]
        if owner not in acc_vals:
            info("请选择该群的归属账号。")
            return
        ent = AddressEntry(
            id=uuid.uuid4().hex[:12],
            remark=remark,
            chat_ref=g.strip(),
            watch_user=u.strip() if u else "",
            listen_enabled=listen_on,
            owner_account_id=owner,
        )
        self._cfg.address_book.append(ent)
        self._addr_remark.delete(0, "end")
        self._addr_chat.delete(0, "end")
        self._addr_user.delete(0, "end")
        self._addr_listen.set(True)
        if acc_vals:
            self._addr_owner.set(acc_vals[0])
        try:
            self._commit_address_book()
            info(f"已添加通讯录：{remark}（归属→{owner}）")
        except Exception as exc:
            error(f"添加通讯录失败：{exc}")
            self._cfg.address_book = [e for e in self._cfg.address_book if e.id != ent.id]

    def _del_address_entry(self, entry_id: str) -> None:
        removed = next((e for e in self._cfg.address_book if e.id == entry_id), None)
        self._cfg.address_book = [e for e in self._cfg.address_book if e.id != entry_id]
        self._sync_jobs_after_address_entry_removed(entry_id)
        self._commit_address_book()
        if removed is not None:
            info(f"已删除通讯录：{removed.remark or removed.id}，并同步更新定时任务。")
        else:
            info("已删除该条目并保存到配置文件。")

    def _sync_jobs_after_address_entry_removed(self, entry_id: str) -> None:
        """通讯录删群后，同步清理任务目标并移除空目标任务。"""
        eid = (entry_id or "").strip()
        if not eid:
            return
        jobs = load_jobs()
        if not jobs:
            return
        kept: List[ScheduledJob] = []
        changed = 0
        removed = 0
        for j in jobs:
            before = [str(x) for x in (j.chat_entry_ids or []) if str(x).strip()]
            after = [x for x in before if x != eid]
            if len(after) != len(before):
                j.chat_entry_ids = after
                changed += 1
            # 仅按通讯录目标发送的任务，如果条目被删后无目标，则删除该任务
            if not j.chat_entry_ids and not j.chat_ids:
                removed += 1
                continue
            kept.append(j)
        if changed == 0 and removed == 0:
            return
        save_jobs(kept)
        if removed > 0:
            info(f"通讯录变更已同步：更新 {changed} 个任务，删除 {removed} 个空目标任务。")
        else:
            info(f"通讯录变更已同步：更新 {changed} 个任务目标。")
        if self._sched_edit_job_id and all(j.id != self._sched_edit_job_id for j in kept):
            self._sched_edit_job_id = None
        self._render_jobs(full=True)
        self._render_taskmgr_cards(force=True)

    def _clear_sched_target_selection(self) -> None:
        for _ent, var in getattr(self, "_sched_target_rows", []):
            var.set(False)
        for v in getattr(self, "_sched_target_vars", []):
            v.set(False)

    def _refresh_schedule_target_checks(self) -> None:
        """定时任务页：按通讯录生成群发目标勾选框。"""
        if getattr(self, "_sched_targets", None) is None:
            return
        sync_last_schedule_from_disk(self._cfg)
        for ch in self._sched_targets.winfo_children():
            ch.destroy()
        self._sched_target_vars = []
        self._sched_target_rows = []
        if not self._cfg.address_book:
            ctk.CTkLabel(self._sched_targets, text="请先在「通讯录」添加群。", text_color=COLORS["muted"]).pack(anchor="w", padx=4, pady=6)
            return
        for ent in self._cfg.address_book:
            v = ctk.BooleanVar(value=False)
            self._sched_target_vars.append(v)
            self._sched_target_rows.append((ent, v))
            owner = (ent.owner_account_id or "").strip()
            own_hint = f" · 主号→{owner}" if owner else ""
            last_fn = (getattr(ent, "last_schedule_source_name", "") or "").strip()
            last_hint = f" · 上次任务→{last_fn}" if last_fn else ""
            disp = f"{ent.remark.strip() or ent.id}{own_hint}   （{ent.chat_ref}{last_hint}）"
            ctk.CTkCheckBox(
                self._sched_targets,
                text=disp,
                variable=v,
                text_color=COLORS["text"],
            ).pack(anchor="w", padx=4, pady=4)

    # --- rules ---
    def _page_rules(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._content, fg_color="transparent")
        wrap, finish_scroll = self._mount_main_scroll(page)
        ctk.CTkLabel(wrap, text="监听与限流", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLORS["text"]).pack(anchor="w", pady=(8, 16))

        box = ctk.CTkFrame(wrap, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        box.pack(fill="x")
        self._listen_var = ctk.BooleanVar(value=self._cfg.listening_enabled)
        ctk.CTkCheckBox(
            box,
            text="启用消息监听（关闭后仍保留配置，但不连接 Telegram）",
            variable=self._listen_var,
            command=lambda: setattr(self._cfg, "listening_enabled", bool(self._listen_var.get())),
            text_color=COLORS["text"],
        ).pack(anchor="w", padx=14, pady=12)

        ctk.CTkLabel(box, text="按群限流（秒）", text_color=COLORS["muted"]).pack(anchor="w", padx=14, pady=(4, 6))
        self._rate_entry = ctk.CTkEntry(box, placeholder_text="秒")
        self._rate_entry.insert(0, str(self._cfg.rate_limit_seconds))
        self._rate_entry.pack(fill="x", padx=14, pady=(0, 14))

        def save_rate(*_a: object) -> None:
            try:
                self._cfg.rate_limit_seconds = float(self._rate_entry.get().strip())
            except ValueError:
                pass

        self._rate_entry.bind("<FocusOut>", save_rate)
        finish_scroll()
        return page

    # --- schedule ---
    def _schedule_doc_path(self) -> str:
        p = resource_path("docs", "定时任务导入说明与示例.txt")
        if os.path.isfile(p):
            return p
        alt = os.path.join(app_root(), "docs", "定时任务导入说明与示例.txt")
        return alt if os.path.isfile(alt) else p

    def _open_schedule_import_doc(self) -> None:
        path = self._schedule_doc_path()
        if os.path.isfile(path):
            webbrowser.open(Path(path).as_uri())
        else:
            info(f"未找到示例文档：{path}")

    def _page_schedule(self) -> ctk.CTkFrame:
        """布局对齐 WA 助手：单页可滚动 + 表单与任务列表。"""
        page = ctk.CTkFrame(self._content, fg_color="transparent")
        wrap, finish_scroll = self._mount_main_scroll(page)
        ctk.CTkLabel(wrap, text="定时任务", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLORS["text"]).pack(anchor="w", pady=8)
        ctk.CTkLabel(
            wrap,
            text="可添加多个文档任务并行。TXT 里 账号=主号 表示由通讯录中该群选择的「归属账号」发送；"
            "其它账号名保持不变。勾选几个群就创建几个独立任务，可分别开始/暂停；含主号时按各群归属账号映射。",
            text_color=COLORS["muted"],
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))
        ctk.CTkButton(
            wrap,
            text="打开 TXT 格式说明与示例",
            fg_color=COLORS["border"],
            command=self._open_schedule_import_doc,
        ).pack(anchor="w", pady=4)

        form = ctk.CTkFrame(wrap, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        form.pack(fill="x", pady=8)
        ctk.CTkLabel(form, text="勾选群发目标", text_color=COLORS["muted"]).pack(anchor="w", padx=12, pady=(10, 4))
        self._sched_targets = ctk.CTkFrame(form, fg_color="transparent")
        self._sched_targets.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(
            form,
            text="固定间隔（分钟，如 5-10；TXT 未写 间隔= 时使用）",
            text_color=COLORS["muted"],
        ).pack(anchor="w", padx=12, pady=(8, 4))
        self._sched_interval = ctk.CTkEntry(form, placeholder_text="如 5-10")
        self._sched_interval.insert(0, "5-10")
        self._sched_interval.pack(fill="x", padx=12, pady=4)
        self._jfile = ctk.CTkEntry(form, placeholder_text="选择 TXT（可含未添加的原文账号，导入后再批量替换）")
        self._jfile.pack(fill="x", padx=12, pady=4)
        ctk.CTkButton(form, text="选择 TXT", fg_color=COLORS["border"], command=self._pick_schedule_txt).pack(fill="x", padx=12, pady=4)
        ctk.CTkButton(form, text="添加文档任务", fg_color=COLORS["accent"], command=self._add_job).pack(fill="x", padx=12, pady=12)

        edit_card = ctk.CTkFrame(wrap, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        edit_card.pack(fill="x", pady=8)
        ctk.CTkLabel(
            edit_card,
            text="批量改发送账号（原文 → 实际发送）",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=COLORS["text"],
        ).pack(anchor="w", padx=12, pady=(12, 8))
        pick_row = ctk.CTkFrame(edit_card, fg_color="transparent")
        pick_row.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(pick_row, text="选定任务", text_color=COLORS["muted"]).pack(side="left", padx=(0, 8))
        self._sched_job_pick = ctk.CTkComboBox(pick_row, width=280, values=["—"], command=self._on_sched_job_pick_changed)
        self._sched_job_pick.pack(side="left", fill="x", expand=True)
        bulk_row = ctk.CTkFrame(edit_card, fg_color="transparent")
        bulk_row.pack(fill="x", padx=12, pady=(4, 12))
        ctk.CTkLabel(bulk_row, text="原文账号", text_color=COLORS["muted"]).grid(row=0, column=0, padx=(0, 8), pady=4, sticky="w")
        self._sched_bulk_from_combo = ctk.CTkComboBox(bulk_row, width=160, values=["—"])
        self._sched_bulk_from_combo.grid(row=0, column=1, pady=4, sticky="w")
        ctk.CTkLabel(bulk_row, text="改由账号发送", text_color=COLORS["muted"]).grid(row=1, column=0, padx=(0, 8), pady=4, sticky="w")
        acc0 = self._account_id_values()
        self._sched_bulk_to_combo = ctk.CTkComboBox(bulk_row, width=160, values=acc0)
        self._sched_bulk_to_combo.grid(row=1, column=1, pady=4, sticky="w")
        ctk.CTkButton(bulk_row, text="批量替换", fg_color=COLORS["border"], command=self._schedule_bulk_replace_by_original).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=8
        )

        self._sched_target_vars = []
        self._refresh_schedule_target_checks()

        ctk.CTkLabel(
            wrap,
            text="任务运行状态、暂停/继续请在侧栏「任务管理」中查看与控制；本页仅用于添加任务与批量改账号。",
            text_color=COLORS["muted"],
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(8, 4))
        finish_scroll()
        return page

    def _page_task_manager(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._content, fg_color="transparent")
        wrap, finish_scroll = self._mount_main_scroll(page)
        ctk.CTkLabel(wrap, text="任务管理", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLORS["text"]).pack(
            anchor="w", pady=(8, 4)
        )
        ctk.CTkLabel(
            wrap,
            text="每个任务一张卡片：绿色=运行中，金色=监听暂停，红色=其它暂停，灰色=已停止。点击卡片可切换运行/暂停。",
            text_color=COLORS["muted"],
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        self._taskmgr_count_lbl = ctk.CTkLabel(
            wrap,
            text="任务数量：0",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=COLORS["text"],
            anchor="w",
        )
        self._taskmgr_count_lbl.pack(anchor="w", pady=(0, 10))
        ctk.CTkButton(
            wrap,
            text="一键开始全部任务",
            fg_color=COLORS["accent"],
            hover_color="#3d7ae6",
            height=40,
            command=self._resume_all_doc_jobs,
        ).pack(fill="x", pady=(0, 10))
        ctk.CTkButton(
            wrap,
            text="一键删除全部任务",
            fg_color=COLORS["danger"],
            hover_color="#b63a3a",
            height=38,
            command=self._delete_all_jobs,
        ).pack(fill="x", pady=(0, 10))
        self._taskmgr_cards = ctk.CTkFrame(wrap, fg_color="transparent")
        self._taskmgr_cards.pack(fill="both", expand=True, pady=4)
        self._render_taskmgr_cards(force=True)
        finish_scroll()
        return page

    def _doc_job_is_running(self, j: ScheduledJob) -> bool:
        return bool(j.enabled) and j.state == "running"

    def _doc_job_status_label(self, j: ScheduledJob) -> str:
        if not j.enabled:
            reason = (j.pause_reason or "").strip()
            if reason:
                return f"已停止 · {reason[:24]}"
            return "已停止"
        if j.state == "paused":
            return f"暂停 · {(j.pause_reason or '已暂停')[:28]}"
        return "运行中"

    def _doc_job_step_label(self, j: ScheduledJob) -> str:
        total = j.item_count()
        if total <= 0:
            return "无发送步骤"
        if j.cursor >= total:
            return f"已完成（{total}/{total} 步）"
        step_no = j.cursor + 1
        item = j.current_item()
        if item is None:
            return f"第 {step_no}/{total} 步"
        if item.is_reminder:
            return f"第 {step_no}/{total} 步 · 阶段提醒"
        preview = (item.content or "").replace("\n", " ").strip()[:28]
        return f"第 {step_no}/{total} 步 · {preview or '…'}"

    def _bind_taskmgr_card_click(self, widget: ctk.CTkBaseClass, job_id: str) -> None:
        if isinstance(widget, ctk.CTkButton):
            return

        def on_click(_event: Any = None) -> None:
            self._toggle_job_run_by_id(job_id)

        widget.bind("<Button-1>", on_click)
        for child in widget.winfo_children():
            self._bind_taskmgr_card_click(child, job_id)

    def _taskmgr_fingerprint(self, j: ScheduledJob) -> tuple:
        return (
            self._doc_job_is_running(j),
            j.enabled,
            j.state,
            (j.pause_reason or "")[:40],
            j.cursor,
            j.source_name,
            self._doc_job_step_label(j),
            self._job_target_short(j),
            self._task_reminder_summary(j),
        )

    def _task_reminder_summary(self, j: ScheduledJob) -> str:
        notes: List[str] = []
        for it in j.items:
            if not getattr(it, "is_reminder", False):
                continue
            t = (getattr(it, "reminder_note", "") or "").strip()
            if t:
                notes.append(t)
        if not notes:
            return ""
        idx = max(0, j.cursor - 1)
        for k in range(idx, -1, -1):
            if k >= len(j.items):
                continue
            it = j.items[k]
            if getattr(it, "is_reminder", False):
                t = (getattr(it, "reminder_note", "") or "").strip()
                if t:
                    return t[:56]
        return notes[0][:56]

    def _apply_taskmgr_tile(self, w: Dict[str, Any], j: ScheduledJob) -> None:
        running = self._doc_job_is_running(j)
        pal = taskmgr_tile_palette(running=running, enabled=bool(j.enabled), pause_reason=j.pause_reason or "")
        w["card"].configure(fg_color=pal["fg"], border_color=pal["border"])
        w["title"].configure(text=self._job_target_short(j), text_color=pal["title"])
        w["status"].configure(
            text=taskmgr_card_status_text(
                self._doc_job_status_label(j),
                self._doc_job_step_label(j),
            ),
            text_color=pal["status"],
        )
        w["file"].configure(
            text=f"文档：{j.source_name or '未命名'}",
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

    def _build_taskmgr_tile(self, j: ScheduledJob, index: int) -> Dict[str, Any]:
        running = self._doc_job_is_running(j)
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
            text=self._job_target_short(j),
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
            command=lambda jid=j.id: self._del_job_by_id(jid),
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
        self._apply_taskmgr_tile(widgets, j)
        self._bind_taskmgr_card_click(card, j.id)
        return widgets

    def _full_rebuild_taskmgr_grid(self, jobs: List[ScheduledJob]) -> None:
        for w in self._taskmgr_cards.winfo_children():
            w.destroy()
        self._taskmgr_widgets.clear()
        self._taskmgr_fp_cache.clear()
        configure_equal_columns(self._taskmgr_cards, TASKMGR_COLS, uniform="tg_task")
        for i, j in enumerate(jobs):
            self._taskmgr_widgets[j.id] = self._build_taskmgr_tile(j, i)
            self._taskmgr_fp_cache[j.id] = self._taskmgr_fingerprint(j)

    def _patch_taskmgr_grid(self, jobs: List[ScheduledJob]) -> None:
        for j in jobs:
            fp = self._taskmgr_fingerprint(j)
            if self._taskmgr_fp_cache.get(j.id) == fp:
                continue
            self._taskmgr_fp_cache[j.id] = fp
            w = self._taskmgr_widgets.get(j.id)
            if w:
                self._apply_taskmgr_tile(w, j)

    def _refresh_taskmgr_card(self, job_id: str) -> None:
        """仅刷新单张任务卡片（点击切换状态时用，避免全页重建）。"""
        if getattr(self, "_taskmgr_cards", None) is None:
            return
        jobs = load_jobs()
        j = next((x for x in jobs if x.id == job_id), None)
        if j is None:
            self._render_taskmgr_cards(force=True)
            return
        w = self._taskmgr_widgets.get(job_id)
        if w is None:
            self._render_taskmgr_cards(force=False)
            return
        self._taskmgr_fp_cache[job_id] = self._taskmgr_fingerprint(j)
        self._apply_taskmgr_tile(w, j)
        self._update_taskmgr_count_label(jobs)

    def _update_taskmgr_count_label(self, jobs: List[ScheduledJob]) -> None:
        lbl = getattr(self, "_taskmgr_count_lbl", None)
        if lbl is None:
            return
        counts = taskmgr_count_jobs(jobs, is_running=self._doc_job_is_running)
        lbl.configure(text=format_taskmgr_count_summary(counts))

    def _refresh_job_run_ui(self, job_id: str) -> None:
        """任务卡片 + 定时任务页控件同步刷新（暂停/继续后）。"""
        self._refresh_taskmgr_card(job_id)
        j = next((x for x in load_jobs() if x.id == job_id), None)
        if j is None:
            return
        w = self._sched_job_row_widgets.get(job_id)
        if not w:
            return
        self._sched_job_fingerprints[job_id] = self._sched_job_fingerprint(j)
        w["detail"].configure(text=self._job_detail_text(j))
        w["btn_run"].configure(text=("继续" if j.state == "paused" else "暂停"))
        w["btn_en"].configure(text=("停用" if j.enabled else "启用"))

    def _render_taskmgr_cards(self, *, force: bool = False) -> None:
        if getattr(self, "_taskmgr_cards", None) is None:
            return
        jobs = load_jobs()
        ids = [j.id for j in jobs]
        if not jobs:
            if self._taskmgr_listed_ids:
                for w in self._taskmgr_cards.winfo_children():
                    w.destroy()
                self._taskmgr_widgets.clear()
                self._taskmgr_fp_cache.clear()
                self._taskmgr_listed_ids = []
            if not self._taskmgr_cards.winfo_children():
                ctk.CTkLabel(
                    self._taskmgr_cards,
                    text="尚无文档任务，请先在「定时任务」页添加。",
                    text_color=COLORS["muted"],
                ).grid(row=0, column=0, columnspan=TASKMGR_COLS, sticky="w", padx=8, pady=12)
            self._update_taskmgr_count_label(jobs)
            self._sync_sched_job_pick_combo()
            return
        if force or ids != self._taskmgr_listed_ids:
            self._taskmgr_listed_ids = ids
            self._full_rebuild_taskmgr_grid(jobs)
        else:
            self._patch_taskmgr_grid(jobs)
        self._update_taskmgr_count_label(jobs)
        self._sync_sched_job_pick_combo()
        handler = getattr(self, "_scroll_wheel_handler", None)
        if handler:
            bind_scroll_tree_once(self._taskmgr_cards, handler)

    def _resume_all_doc_jobs(self) -> None:
        n = self._scheduler.resume_all_jobs()
        self._cfg = load_config()
        self._refresh_schedule_target_checks()
        self._render_taskmgr_cards()
        self._render_jobs()
        if n > 0:
            self._warn_if_tg_session_down()
            info(f"已恢复 {n} 个文档任务为运行中")
            return
        jobs = load_jobs()
        running = sum(1 for j in jobs if self._doc_job_is_running(j))
        if running > 0:
            info(
                f"当前 {running} 个任务已是运行中；若仍不发送，请确认账号已连接后点「保存并重载服务」。"
            )
        else:
            info("没有可恢复的暂停或已停止任务")

    def _pick_schedule_txt(self) -> None:
        doc_path = self._schedule_doc_path()
        start_dir = os.path.dirname(doc_path) if os.path.isfile(doc_path) else app_root()
        path = filedialog.askopenfilename(
            parent=self,
            title="选择 UTF-8 编码 TXT（仅账号+消息）",
            filetypes=[("文本", "*.txt"), ("所有文件", "*.*")],
            initialdir=start_dir,
        )
        if path:
            self._jfile.delete(0, "end")
            self._jfile.insert(0, path)

    def _parse_interval_minutes(self, s: str) -> Optional[tuple[float, float]]:
        t = (s or "").strip()
        if not t:
            return None
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*$", t)
        if m:
            a = float(m.group(1))
            b = float(m.group(2))
            if a <= 0 or b <= 0:
                return None
            if b < a:
                a, b = b, a
            return (a, b)
        if re.match(r"^\d+(?:\.\d+)?$", t):
            x = float(t)
            if x <= 0:
                return None
            return (x, x)
        return None

    def _sched_add_fail(self, msg: str) -> None:
        info(msg)
        try:
            messagebox.showwarning("无法添加文档任务", msg, parent=self)
        except Exception:
            pass

    def _add_job(self) -> None:
        selected_ids: List[str] = []
        rows = getattr(self, "_sched_target_rows", None) or []
        if rows:
            for ent, var in rows:
                if var.get():
                    selected_ids.append(ent.id)
        else:
            vars_list = getattr(self, "_sched_target_vars", []) or []
            for i, ent in enumerate(self._cfg.address_book):
                if i < len(vars_list) and vars_list[i].get():
                    selected_ids.append(ent.id)
        if not selected_ids:
            self._sched_add_fail("请至少勾选一个通讯录中的群发目标。")
            return
        path = self._jfile.get().strip()
        if not path:
            self._sched_add_fail("请先选择 TXT 文件。")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except UnicodeDecodeError:
            self._sched_add_fail("TXT 解码失败，请另存为 UTF-8 编码。")
            return
        except OSError as exc:
            self._sched_add_fail(f"读取 TXT 失败：{exc}")
            return
        valid = {a.id for a in self._cfg.accounts}
        if not valid:
            self._sched_add_fail("请先在「账号管理」添加至少一个账号。")
            return
        items, errors = import_doc_items(text, valid_accounts=valid, require_per_item_interval=False)
        sends = [it for it in items if not it.is_reminder]
        if not sends:
            hint = "TXT 中没有可发送条目（需 账号= 与 消息=，可用 [条目] 分段）。"
            if errors:
                hint += "\n" + "\n".join(errors[:8])
            self._sched_add_fail(hint)
            return
        if errors:
            for e in errors[:10]:
                info(e)
        all_txt = items_use_txt_intervals(items)
        any_txt = items_have_any_txt_interval(items)
        min_m = 0.0
        max_m = 0.0
        if all_txt:
            mode = "txt"
            interval_note = "间隔：TXT 每条 间隔="
        else:
            interval = self._parse_interval_minutes(
                self._sched_interval.get().strip() if getattr(self, "_sched_interval", None) else ""
            )
            if interval is None:
                if any_txt:
                    self._sched_add_fail(
                        "部分条目未写 间隔=，请在底部「固定间隔」填写补充间隔（分钟），格式：5 或 5-10。"
                    )
                else:
                    self._sched_add_fail(
                        "TXT 未写每条 间隔= 时，请在底部「固定间隔」填写分钟数，格式：5 或 5-10。"
                    )
                return
            min_m, max_m = interval
            mode = "mixed" if any_txt else "fixed"
            interval_note = (
                f"间隔：TXT 优先 + 固定 {min_m:g}-{max_m:g} 分/条"
                if any_txt
                else f"间隔：固定 {min_m:g}-{max_m:g} 分钟"
            )
        has_main = doc_has_main_account_placeholder(items)
        by_eid = {e.id: e for e in self._cfg.address_book}
        jobs = load_jobs()
        created: List[ScheduledJob] = []
        skipped: List[str] = []

        def _owner_for_entry(ent: AddressEntry) -> str:
            return (ent.owner_account_id or "").strip()

        targets: List[AddressEntry] = []
        for eid in selected_ids:
            ent = by_eid.get(eid)
            if ent:
                targets.append(ent)

        # 与 WA 一致：每勾选一个群 = 一个独立任务，可分别开始/暂停
        for ent in targets:
            if has_main:
                owner = _owner_for_entry(ent)
                if not owner:
                    skipped.append(ent.remark.strip() or ent.id)
                    continue
                if owner not in valid:
                    self._sched_add_fail(
                        f"群「{ent.remark}」归属账号「{owner}」未在账号管理中启用，请先登录该账号。"
                    )
                    return
                job_items = clone_doc_items(items)
                mapped = apply_main_account_mapping(job_items, owner)
                if mapped == 0:
                    self._sched_add_fail("文档中未找到 账号=主号（或主账号）条目。")
                    return
            else:
                job_items = clone_doc_items(items)

            chat_nums: List[int] = []
            n = chat_ref_to_optional_int(ent.chat_ref)
            if n is not None:
                chat_nums.append(n)
            job = ScheduledJob.new(
                chat_ids=chat_nums,
                source_path=path,
                items=job_items,
                chat_entry_ids=[ent.id],
                interval_min_minutes=min_m,
                interval_max_minutes=max_m,
                interval_mode=mode,
                start_paused=True,
            )
            jobs.append(job)
            created.append(job)

        if skipped:
            info("以下群未在通讯录选择归属账号，已跳过：" + "、".join(skipped))
        if not created:
            if has_main:
                self._sched_add_fail("未能为任何所选群创建任务：请先在通讯录为各群选择「归属账号」。")
            else:
                self._sched_add_fail("未能创建任务。")
            return

        save_jobs(jobs)
        apply_last_schedule_from_current_jobs(self._cfg)
        self._sched_edit_job_id = created[-1].id
        self._render_taskmgr_cards()
        main_note = ""
        if has_main:
            main_note = "；账号=主号 已按各群归属账号自动映射"
        j0 = created[0]
        if len(created) > 1:
            info(
                f"已为 {len(created)} 个群各添加 1 个文档任务（默认暂停）：{j0.source_name}（{len(items)} 步，{interval_note}）{main_note}"
            )
        else:
            info(
                f"已添加文档任务（默认暂停）：{self._job_target_short(j0)} · {j0.source_name}（{len(items)} 步，{interval_note}）{main_note}"
            )
        self._clear_sched_target_selection()
        self._refresh_schedule_target_checks()

    def _sched_job_fingerprint(self, j: ScheduledJob) -> tuple:
        stotal, sdone, sremain = j.send_progress()
        return (
            j.state,
            j.enabled,
            j.pause_reason or "",
            sdone,
            sremain,
            stotal,
            j.item_count(),
            j.interval_mode,
            j.interval_min_minutes,
            j.interval_max_minutes,
        )

    def _sched_jobs_structure_changed(self, jobs: List[ScheduledJob]) -> bool:
        ids = [j.id for j in jobs]
        return ids != getattr(self, "_sched_listed_job_ids", [])

    def _job_detail_text(self, j: ScheduledJob) -> str:
        if not j.enabled:
            status = "已停用"
        elif j.state == "paused":
            status = f"暂停（{j.pause_reason or '手动'}）"
        else:
            status = "运行中"
        emap = {e.id: e for e in self._cfg.address_book}
        if j.chat_entry_ids:
            names = [emap[eid].remark.strip() or emap[eid].id for eid in j.chat_entry_ids if eid in emap]
            tgt_desc = "、".join(names) if names else f"{len(j.chat_entry_ids)} 个目标"
        elif j.chat_ids:
            tgt_desc = f"{len(j.chat_ids)} 个群(ID)"
        else:
            tgt_desc = "未设群"
        delay_hint = ""
        if j.interval_mode == "txt" or items_use_txt_intervals(j.items):
            delays = [
                it.delay_after_minutes
                for it in j.items
                if not it.is_reminder and getattr(it, "interval_from_txt", False)
            ]
            if delays:
                delay_hint = f" · 间隔 TXT {min(delays):g}–{max(delays):g} 分/条"
        elif j.interval_mode == "mixed" or items_have_any_txt_interval(j.items):
            delay_hint = f" · 间隔 TXT优先 + 固定 {j.interval_min_minutes:g}-{j.interval_max_minutes:g} 分"
        elif j.interval_max_minutes > 0 or j.interval_min_minutes > 0:
            delay_hint = f" · 间隔固定 {j.interval_min_minutes:g}-{j.interval_max_minutes:g} 分"
        oc = Counter(it.original_label() for it in j.items if not it.is_reminder and it.original_label())
        dist = ""
        if oc:
            dist = " · 原文：" + "、".join(f"{k}×{v}" for k, v in sorted(oc.items()))
        main_mapped = next(
            (
                it.effective_send_account_id()
                for it in j.items
                if not it.is_reminder
                and is_main_account_placeholder(it.original_label())
                and it.effective_send_account_id()
            ),
            None,
        )
        if main_mapped:
            dist += f" · 主号→{main_mapped}"
        stotal, sdone, sremain = j.send_progress()
        prog = self._format_send_progress(stotal, sdone, sremain, step_total=j.item_count())
        return f"{tgt_desc} · 文档 {j.source_name} · {prog}{delay_hint}{dist} · 状态：{status}"

    def _create_sched_job_row(self, j: ScheduledJob) -> None:
        row = ctk.CTkFrame(self._job_rows, fg_color=COLORS["card"], corner_radius=10, border_width=1, border_color=COLORS["border"])
        row.pack(fill="x", pady=4)
        detail_lbl = ctk.CTkLabel(
            row,
            text=self._job_detail_text(j),
            text_color=COLORS["text"],
            justify="left",
            wraplength=680,
        )
        detail_lbl.pack(anchor="w", padx=12, pady=(10, 6))
        acts = ctk.CTkFrame(row, fg_color="transparent")
        acts.pack(fill="x", padx=12, pady=(0, 10))
        btn_run = ctk.CTkButton(
            acts,
            text=("继续" if j.state == "paused" else "暂停"),
            fg_color=COLORS["border"],
            command=lambda jid=j.id: self._toggle_job_run_by_id(jid),
        )
        btn_run.pack(fill="x", pady=2)
        btn_en = ctk.CTkButton(
            acts,
            text=("停用" if j.enabled else "启用"),
            fg_color=COLORS["border"],
            command=lambda jid=j.id: self._toggle_job_enabled_by_id(jid),
        )
        btn_en.pack(fill="x", pady=2)
        ctk.CTkButton(
            acts,
            text="选为编辑",
            fg_color=COLORS["border"],
            command=lambda jid=j.id: self._select_sched_job_for_edit(jid),
        ).pack(fill="x", pady=2)
        ctk.CTkButton(
            acts,
            text="删除",
            fg_color=COLORS["danger"],
            command=lambda jid=j.id: self._del_job_by_id(jid),
        ).pack(fill="x", pady=2)
        self._sched_job_row_widgets[j.id] = {"row": row, "detail": detail_lbl, "btn_run": btn_run, "btn_en": btn_en}
        self._sched_job_fingerprints[j.id] = self._sched_job_fingerprint(j)

    def _full_rebuild_sched_jobs(self, jobs: List[ScheduledJob]) -> None:
        for ch in self._job_rows.winfo_children():
            ch.destroy()
        self._sched_job_row_widgets.clear()
        self._sched_job_fingerprints.clear()
        if not jobs:
            ctk.CTkLabel(self._job_rows, text="尚未添加任务。", text_color=COLORS["muted"]).pack(anchor="w", pady=8)
            return
        for j in jobs:
            self._create_sched_job_row(j)

    def _patch_sched_job_rows(self, jobs: List[ScheduledJob]) -> None:
        if self._sched_jobs_structure_changed(jobs):
            self._sched_listed_job_ids = [j.id for j in jobs]
            self._full_rebuild_sched_jobs(jobs)
            return
        for j in jobs:
            fp = self._sched_job_fingerprint(j)
            if self._sched_job_fingerprints.get(j.id) == fp:
                continue
            self._sched_job_fingerprints[j.id] = fp
            w = self._sched_job_row_widgets.get(j.id)
            if not w:
                continue
            w["detail"].configure(text=self._job_detail_text(j))
            w["btn_run"].configure(text=("继续" if j.state == "paused" else "暂停"))
            w["btn_en"].configure(text=("停用" if j.enabled else "启用"))

    def _render_jobs(self, *, full: Optional[bool] = None) -> None:
        if getattr(self, "_job_rows", None) is None:
            return
        jobs = load_jobs()
        if full is None:
            full = self._sched_jobs_structure_changed(jobs)
        if full:
            self._sched_listed_job_ids = [j.id for j in jobs]
            self._full_rebuild_sched_jobs(jobs)
        else:
            self._patch_sched_job_rows(jobs)
        self._sync_sched_job_pick_combo()
        handler = getattr(self, "_scroll_wheel_handler", None)
        if handler:
            bind_scroll_tree_once(self._job_rows, handler)

    def _job_target_short(self, j: ScheduledJob) -> str:
        cfg = load_config()
        emap = {e.id: e for e in cfg.address_book}
        if j.chat_entry_ids:
            names: List[str] = []
            for eid in j.chat_entry_ids[:4]:
                ent = emap.get(str(eid))
                if ent:
                    names.append((ent.remark or "").strip() or ent.id)
            if names:
                s = "、".join(names)
                if len(j.chat_entry_ids) > 4:
                    s += f"等{len(j.chat_entry_ids)}群"
                return s
        if j.chat_ids:
            if len(j.chat_ids) == 1:
                return f"群{j.chat_ids[0]}"
            return f"{len(j.chat_ids)}个群ID"
        return "未设群"

    def _sched_job_label(self, j: ScheduledJob) -> str:
        return f"{j.source_name} · {self._job_target_short(j)} · {j.item_count()}步 · #{j.id[:8]}"

    def _on_sched_job_pick_changed(self, _value: str) -> None:
        label = self._sched_job_pick.get().strip()
        jid = self._sched_job_label_to_id.get(label)
        if jid:
            self._sched_edit_job_id = jid
            jobs = load_jobs()
            job = next((x for x in jobs if x.id == jid), None)
            self._refresh_sched_bulk_combos(job)
            return

    def _refresh_sched_bulk_combos(self, job: Optional[ScheduledJob] = None) -> None:
        if job is None and self._sched_edit_job_id:
            for j in load_jobs():
                if j.id == self._sched_edit_job_id:
                    job = j
                    break
        acc_vals = self._account_id_values()
        if getattr(self, "_sched_bulk_to_combo", None) is not None:
            prev_t = self._sched_bulk_to_combo.get().strip()
            self._sched_bulk_to_combo.configure(values=acc_vals)
            if prev_t in acc_vals:
                self._sched_bulk_to_combo.set(prev_t)
            elif acc_vals:
                self._sched_bulk_to_combo.set(acc_vals[0])
        if getattr(self, "_sched_bulk_from_combo", None) is None:
            return
        originals: List[str] = []
        if job:
            originals = sorted(
                {it.original_label() for it in job.items if not it.is_reminder and it.original_label()}
            )
        vals_f = originals if originals else ["—"]
        prev_f = self._sched_bulk_from_combo.get().strip()
        self._sched_bulk_from_combo.configure(values=vals_f)
        if prev_f in vals_f:
            self._sched_bulk_from_combo.set(prev_f)
        else:
            self._sched_bulk_from_combo.set(vals_f[0])

    def _sync_sched_job_pick_combo(self) -> None:
        jobs = load_jobs()
        if getattr(self, "_sched_job_pick", None) is None:
            return
        self._sched_job_label_to_id = {}
        labels: List[str] = []
        for j in jobs:
            lab = self._sched_job_label(j)
            labels.append(lab)
            self._sched_job_label_to_id[lab] = j.id
        if not labels:
            labels = ["—"]
            self._sched_edit_job_id = None
        elif self._sched_edit_job_id:
            found = False
            for j in jobs:
                if j.id == self._sched_edit_job_id:
                    self._sched_job_pick.set(self._sched_job_label(j))
                    found = True
                    break
            if not found:
                self._sched_edit_job_id = jobs[0].id
                self._sched_job_pick.set(self._sched_job_label(jobs[0]))
        else:
            self._sched_edit_job_id = jobs[0].id
            self._sched_job_pick.set(self._sched_job_label(jobs[0]))
        self._sched_job_pick.configure(values=labels)
        sel = None
        for j in jobs:
            if j.id == self._sched_edit_job_id:
                sel = j
                break
        self._refresh_sched_bulk_combos(sel)

    def _select_sched_job_for_edit(self, job_id: str) -> None:
        self._sched_edit_job_id = job_id
        self._sync_sched_job_pick_combo()
        job = next((j for j in load_jobs() if j.id == job_id), None)
        if job:
            info(
                f"已选定任务 #{job.id[:8]} · 目标={self._job_target_short(job)} · {job.source_name}；"
                "批量替换仅影响本任务，不影响其他任务。"
            )
        else:
            info("已选定该任务，可在上方批量替换发送账号。")

    def _schedule_bulk_replace_by_original(self) -> None:
        jid = (self._sched_edit_job_id or "").strip()
        if not jid:
            info("请先在任务列表点「选为编辑」，或在下拉框选定任务（请看名称里的群与 #编号）。")
            return
        jobs = load_jobs()
        job = next((j for j in jobs if j.id == jid), None)
        if job is None:
            info("选定任务不存在，请重新选择。")
            return
        from_acc = self._sched_bulk_from_combo.get().strip()
        to_acc = self._sched_bulk_to_combo.get().strip()
        if from_acc in ("", "—") or not to_acc:
            info("请先选择「原文账号」与「改为由…发送」。")
            return
        if from_acc == to_acc:
            info("原文账号与目标发送账号相同，无需替换。")
            return
        valid = {a.id for a in self._cfg.accounts}
        if valid and to_acc not in valid:
            info(f"目标账号「{to_acc}」未在账号管理中添加。")
            return
        n = 0
        for it in job.items:
            if it.is_reminder:
                continue
            if it.original_label() == from_acc:
                it.send_as_account_id = to_acc
                n += 1
        if n == 0:
            info(f"没有原文为「{from_acc}」的发送条目（提醒步不计入）。")
            return
        save_jobs_patch([job])
        self._refresh_sched_bulk_combos(job)
        self._render_taskmgr_cards()
        info(
            f"仅任务 #{job.id[:8]}（目标={self._job_target_short(job)} · {job.source_name}）："
            f"已将原文「{from_acc}」的 {n} 条改为由「{to_acc}」发送；其他任务未改动。"
        )

    def _toggle_job_run_by_id(self, job_id: str) -> None:
        if self._taskmgr_toggle_busy:
            return
        self._taskmgr_toggle_busy = True
        try:
            j = next((x for x in load_jobs() if x.id == job_id), None)
            if j is None:
                return
            if self._doc_job_is_running(j):
                self._scheduler.pause_job(j.id, "手动暂停")
            elif self._scheduler.resume_job(j.id):
                self._warn_if_tg_session_down()
            else:
                info("无法启动该任务")
            self._refresh_job_run_ui(job_id)
        finally:
            self._taskmgr_toggle_busy = False

    def _toggle_job_enabled_by_id(self, job_id: str) -> None:
        job = next((j for j in load_jobs() if j.id == job_id), None)
        if job is None:
            return
        job.enabled = not job.enabled
        if job.enabled and job.state == "paused":
            job.pause_reason = job.pause_reason or "手动暂停"
        save_jobs_patch([job])
        self._refresh_job_run_ui(job_id)

    def _del_job_by_id(self, job_id: str) -> None:
        jobs = load_jobs()
        dead = next((j for j in jobs if j.id == job_id), None)
        if dead is None:
            return
        jobs = [j for j in jobs if j.id != job_id]
        if dead.id == self._sched_edit_job_id:
            self._sched_edit_job_id = None
        save_jobs(jobs)
        info(f"已删除任务：{dead.source_name}")
        self._render_taskmgr_cards(force=True)
        self._sync_sched_job_pick_combo()

    def _delete_all_jobs(self) -> None:
        jobs = load_jobs()
        if not jobs:
            info("当前没有可删除的任务。")
            return
        ok = messagebox.askyesno(
            "确认删除",
            f"确定删除全部 {len(jobs)} 个任务吗？此操作不可恢复。",
            parent=self,
        )
        if not ok:
            return
        save_jobs([])
        self._sched_edit_job_id = None
        self._render_jobs(full=True)
        self._render_taskmgr_cards(force=True)
        self._sync_sched_job_pick_combo()
        info(f"已删除全部 {len(jobs)} 个任务。")

    def _page_logs(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._content, fg_color="transparent")

        log_foot = ctk.CTkFrame(page, fg_color=COLORS["card"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        lf = ctk.CTkFrame(log_foot, fg_color="transparent")
        lf.pack(fill="x", padx=12, pady=10)
        ctk.CTkButton(lf, text="刷新日志", command=self._flush_logs_ui, fg_color=COLORS["accent"]).pack(fill="x")

        wrap, finish_scroll = self._mount_main_scroll(page, footer=log_foot)
        ctk.CTkLabel(wrap, text="日志中心", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLORS["text"]).pack(anchor="w", pady=(8, 12))

        self._log_box = ctk.CTkTextbox(
            wrap,
            height=520,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color=COLORS["bg"],
            text_color=COLORS["muted"],
            border_width=1,
            border_color=COLORS["border"],
        )
        self._log_box.pack(fill="both", expand=True, pady=(0, 8))
        bind_log_textbox_wheel(self._log_box)
        reload_log_textbox_from_memory(self._log_box, get_recent_lines, limit=LOG_TEXTBOX_MAX_LINES)

        finish_scroll()
        return page

    def _flush_logs_ui(self) -> None:
        lb = getattr(self, "_log_box", None)
        if lb is None:
            return
        reload_log_textbox_from_memory(lb, get_recent_lines, limit=500, max_lines=LOG_TEXTBOX_MAX_LINES)

    def _save_all_and_restart(self) -> None:
        if not getattr(self, "_pages_ready", False):
            info("界面仍在加载，请稍候再点「保存并重载服务」。")
            return
        self._optional_merge_global_api_from_ui()
        try:
            if getattr(self, "_rate_entry", None) is not None:
                self._cfg.rate_limit_seconds = float(self._rate_entry.get().strip())
        except Exception:
            pass
        save_config(self._cfg)
        save_jobs(load_jobs())
        info("配置已写入 config.json")

        def after_restart() -> None:
            self._render_account_rows()
            self._render_group_rows(force=True)
            self._refresh_schedule_combo()
            self._refresh_schedule_target_checks()
            self._render_taskmgr_cards()
            self._refresh_dashboard()
            self._schedule_login_probe()
            info("服务已根据配置重启")

        self._invoke_restart_in_background(after_restart)


if __name__ == "__main__":
    from main import main

    main()
