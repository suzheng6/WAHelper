"""图形界面驱动的 Telethon 登录（供主程序集成）。"""
from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Callable, Optional, Protocol

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    RPCError,
    SessionPasswordNeededError,
)

from .compat_config import Account, AppConfig, ensure_dirs
from .logger_util import error, info
from .ui.login_dialog import LoginUIBridge

if TYPE_CHECKING:
    import customtkinter as ctk


class _PreLogin(Protocol):
    def __call__(self) -> None: ...


def run_login_in_thread(
    root: "ctk.CTk",
    account: Account,
    cfg: AppConfig,
    on_done: Callable[[bool, str], None],
    *,
    pre_login: Optional[_PreLogin] = None,
) -> None:
    """在后台线程执行登录；回调在主线程执行（通过 root.after）。
    Telegram 应用的 api_id / api_hash 来自 cfg（所有账号共用一套）。
    pre_login：在连接 session 之前执行（例如停止统一会话线程并 join，避免 sqlite 锁死）。
    """

    def worker() -> None:
        if pre_login is not None:
            try:
                pre_login()
            except Exception as exc:
                root.after(0, lambda: on_done(False, f"无法暂停后台 Telegram 服务：{exc}"))
                return
        bridge = LoginUIBridge(root)

        async def inner() -> tuple[bool, str]:
            ensure_dirs()
            if not int(cfg.api_id) or not str(cfg.api_hash).strip():
                return False, "请先在软件里填写共用的 API ID 与 API Hash（my.telegram.org）"
            path = account.session_path()
            client = TelegramClient(path, cfg.api_id, cfg.api_hash)
            try:
                await asyncio.sleep(0.45)
                await client.connect()
                if await client.is_user_authorized():
                    me = await client.get_me()
                    name = getattr(me, "username", None) or str(me.id)
                    await client.disconnect()
                    return True, f"该 session 已登录：{name}"

                phone = bridge.ask_string(
                    "登录 · 第 1 步：手机号",
                    "请输入 Telegram 绑定手机号（含国家区号，例如 +8613812345678）。",
                    placeholder="+86",
                )
                if not phone:
                    await client.disconnect()
                    return False, "已取消"

                try:
                    await client.send_code_request(phone)
                except PhoneNumberInvalidError:
                    await client.disconnect()
                    return False, "手机号格式无效"
                except FloodWaitError as exc:
                    await client.disconnect()
                    return False, f"请求过于频繁，请稍后再试（约 {exc.seconds}s）"
                except RPCError as exc:
                    await client.disconnect()
                    return False, f"发送验证码失败：{exc}"

                code = bridge.ask_string(
                    "登录 · 第 2 步：验证码",
                    "请输入短信或 Telegram 内收到的登录验证码（数字）。",
                )
                if not code:
                    await client.disconnect()
                    return False, "已取消"

                try:
                    await client.sign_in(phone, code.strip())
                except SessionPasswordNeededError:
                    pwd = bridge.ask_string(
                        "登录 · 第 3 步：二步验证",
                        "该账号开启了二步验证，请输入您的二步验证密码（不是登录验证码）。",
                        secret=True,
                    )
                    if not pwd:
                        await client.disconnect()
                        return False, "已取消"
                    await client.sign_in(password=pwd)
                except PhoneCodeInvalidError:
                    await client.disconnect()
                    return False, "验证码错误"
                except RPCError as exc:
                    await client.disconnect()
                    return False, f"登录失败：{exc}"

                if not await client.is_user_authorized():
                    await client.disconnect()
                    return False, "登录未完成"

                me = await client.get_me()
                label = getattr(me, "username", None) or str(me.id)
                await client.disconnect()
                return True, f"登录成功：{label}"
            except Exception as exc:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                error(f"登录异常：{exc}")
                return False, str(exc)

        try:
            ok, msg = asyncio.run(inner())
        except Exception as exc:
            ok, msg = False, str(exc)

        root.after(0, lambda: on_done(ok, msg))

    threading.Thread(target=worker, name="tg-login", daemon=True).start()
