"""页面纵向滚动：Canvas + 滚轮（Windows 下比 CTkScrollableFrame 可靠）。"""
from __future__ import annotations

import time
import tkinter as tk
from typing import Any, Callable, Optional

import customtkinter as ctk

from wa_ui.log_textbox_util import pointer_over_tk_text

_WHEEL_THROTTLE_SEC = 0.016
_last_wheel_at: dict[int, float] = {}


def scroll_wheel(canvas: tk.Canvas, event: Any, *, throttle: bool = True) -> None:
    delta = getattr(event, "delta", 0) or 0
    if delta == 0:
        return
    if throttle:
        now = time.monotonic()
        key = id(canvas)
        last = _last_wheel_at.get(key, 0.0)
        if now - last < _WHEEL_THROTTLE_SEC:
            return
        _last_wheel_at[key] = now
    bb = canvas.bbox("all")
    if not bb or bb[3] <= canvas.winfo_height():
        return
    canvas.yview_scroll(int(-1 * (delta / 120)), "units")


def _pointer_over_scrolling_text(toplevel: tk.Misc) -> bool:
    return pointer_over_tk_text(toplevel)


def _pointer_inside(widget: Any) -> bool:
    """鼠标是否在 widget 矩形内（含其内部 CTk 子控件上的悬停）。"""
    try:
        if not widget.winfo_ismapped():
            return False
        rx, ry = widget.winfo_rootx(), widget.winfo_rooty()
        rw, rh = max(widget.winfo_width(), 1), max(widget.winfo_height(), 1)
        top = widget.winfo_toplevel()
        px, py = top.winfo_pointerx(), top.winfo_pointery()
        return rx <= px < rx + rw and ry <= py < ry + rh
    except tk.TclError:
        return False


def _prune_scroll_regions(regions: list[tuple[Any, tk.Canvas]]) -> None:
    alive: list[tuple[Any, tk.Canvas]] = []
    for holder, canvas in regions:
        try:
            if holder.winfo_exists() and canvas.winfo_exists():
                alive.append((holder, canvas))
        except tk.TclError:
            continue
    regions[:] = alive


def unregister_scroll_region(holder: Any, canvas: tk.Canvas) -> None:
    try:
        top = holder.winfo_toplevel()
    except tk.TclError:
        return
    regions: list = getattr(top, "_wa_scroll_regions", None)
    if not regions:
        return
    pair = (holder, canvas)
    try:
        regions.remove(pair)
    except ValueError:
        pass
    _last_wheel_at.pop(id(canvas), None)


def _ensure_toplevel_wheel_router(toplevel: tk.Misc) -> None:
    if getattr(toplevel, "_wa_scroll_router_installed", False):
        return
    toplevel._wa_scroll_router_installed = True  # type: ignore[attr-defined]
    regions: list[tuple[Any, tk.Canvas]] = []
    toplevel._wa_scroll_regions = regions  # type: ignore[attr-defined]

    def _on_wheel(event: Any) -> Optional[str]:
        if _pointer_over_scrolling_text(toplevel):
            return None
        _prune_scroll_regions(regions)
        for holder, canvas in reversed(regions):
            try:
                if not holder.winfo_ismapped():
                    continue
            except tk.TclError:
                continue
            if _pointer_inside(holder):
                scroll_wheel(canvas, event)
                return "break"
        return None

    toplevel.bind_all("<MouseWheel>", _on_wheel, add="+")


def register_scroll_region(holder: Any, canvas: tk.Canvas) -> None:
    """
    在窗口级捕获滚轮：悬停在 CTk 按钮/复选框上时也能滚动（子控件往往不转发 MouseWheel）。
    holder 为 mount_page_scroll 中的可滚动外框。
    """
    try:
        top = holder.winfo_toplevel()
    except tk.TclError:
        return
    _ensure_toplevel_wheel_router(top)
    regions: list = getattr(top, "_wa_scroll_regions", [])
    pair = (holder, canvas)
    if pair not in regions:
        regions.append(pair)

    if getattr(holder, "_wa_scroll_destroy_bound", False):
        return
    holder._wa_scroll_destroy_bound = True  # type: ignore[attr-defined]

    def _on_destroy(_event: Any = None) -> None:
        unregister_scroll_region(holder, canvas)

    try:
        holder.bind("<Destroy>", _on_destroy, add="+")
    except tk.TclError:
        pass


_SCROLL_BOUND_ATTR = "_wa_scroll_wheel_bound"


def bind_scroll_tree_once(widget: Any, handler: Callable[[Any], None]) -> None:
    """同一块根控件只绑定一次滚轮，避免 refresh 时 add=\"+\" 累积。"""
    if getattr(widget, _SCROLL_BOUND_ATTR, False):
        return
    setattr(widget, _SCROLL_BOUND_ATTR, True)
    bind_scroll_tree(widget, handler)


def bind_scroll_tree(widget: Any, handler: Callable[[Any], None]) -> None:
    """为页面内 Tk/CTk 控件绑定滚轮（辅助；主逻辑见 register_scroll_region）。"""
    try:
        widget.bind("<MouseWheel>", handler, add="+")
    except tk.TclError:
        pass
    for attr in ("_canvas", "_text_label", "_label", "_entry", "_scrollbar"):
        inner = getattr(widget, attr, None)
        if inner is not None:
            try:
                inner.bind("<MouseWheel>", handler, add="+")
            except tk.TclError:
                pass
    for child in widget.winfo_children():
        bind_scroll_tree(child, handler)


def mount_page_scroll(
    page: ctk.CTkFrame,
    *,
    footer: Optional[ctk.CTkFrame] = None,
    bg: str = "#1a1a1a",
    on_ready: Optional[Callable[[], None]] = None,
) -> tuple[ctk.CTkFrame, tk.Canvas, Callable[[], None]]:
    """
    在 page 内挂载可滚动区域。
    返回 (inner, canvas, finish_scroll_bind)；构建完子控件后调用 finish_scroll_bind()。
    """
    if footer is not None:
        footer.pack(side="bottom", fill="x", padx=0, pady=(10, 0))

    holder = ctk.CTkFrame(page, fg_color="transparent")
    holder.pack(fill="both", expand=True)

    scrollbar = ctk.CTkScrollbar(holder, orientation="vertical")
    canvas = tk.Canvas(holder, highlightthickness=0, bd=0, bg=bg)
    scrollbar.configure(command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)

    tk_shell = tk.Frame(canvas, bg=bg)
    inner = ctk.CTkFrame(tk_shell, fg_color="transparent")
    inner.pack(fill="x")

    inner_win = canvas.create_window((0, 0), window=tk_shell, anchor="nw")

    def update_scrollregion(_event: Any = None) -> None:
        canvas.update_idletasks()
        bb = canvas.bbox("all")
        if bb:
            canvas.configure(scrollregion=bb)

    def on_canvas_configure(event: Any) -> None:
        canvas.itemconfigure(inner_win, width=max(int(event.width), 120))

    tk_shell.bind("<Configure>", lambda _e: update_scrollregion())
    canvas.bind("<Configure>", on_canvas_configure)

    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    wheel_handler = lambda e: scroll_wheel(canvas, e, throttle=False)

    def finish_scroll_bind() -> None:
        if getattr(holder, _SCROLL_BOUND_ATTR, False):
            update_scrollregion()
            if on_ready:
                on_ready()
            else:
                page.after_idle(update_scrollregion)
                page.after(250, update_scrollregion)
            return
        setattr(holder, _SCROLL_BOUND_ATTR, True)
        register_scroll_region(holder, canvas)
        for w in (page, holder, canvas, tk_shell, inner, scrollbar):
            try:
                w.bind("<MouseWheel>", wheel_handler, add="+")
            except tk.TclError:
                pass
        bind_scroll_tree(page, wheel_handler)
        update_scrollregion()
        if on_ready:
            on_ready()
        else:
            page.after_idle(update_scrollregion)
            page.after(250, update_scrollregion)

    return inner, canvas, finish_scroll_bind


ADDRESS_LIST_HEIGHT = 400


def mount_bounded_list_scroll(
    parent: ctk.CTkFrame,
    *,
    height: int = ADDRESS_LIST_HEIGHT,
    bg: str = "#1a1a1a",
) -> tuple[ctk.CTkFrame, tk.Canvas, Callable[[], None], ctk.CTkFrame]:
    """固定高度的列表滚动区（通讯录群列表等），避免整页 Canvas 过长。"""
    shell = ctk.CTkFrame(parent, fg_color="transparent", height=height)
    shell.pack(fill="both", expand=True)
    shell.pack_propagate(False)
    inner, canvas, finish = mount_page_scroll(shell, bg=bg)
    return inner, canvas, finish, shell
