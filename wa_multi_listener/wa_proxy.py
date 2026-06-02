"""每账号 SOCKS5 代理：解析 URL，并在 neonize 支持时调用 SetProxyAddress。"""
from __future__ import annotations

import ctypes
import re
from typing import Any, Optional
from urllib.parse import quote, urlparse

from logger_util import info, warning

_SET_PROXY_FN: Any = None
_BIND_TRIED = False


def normalize_proxy_url(raw: str) -> str:
    """将用户输入转为 whatsmeow 可用的 socks5://user:pass@host:port。"""
    t = (raw or "").strip()
    if not t:
        return ""
    low = t.lower()
    if not low.startswith("socks5://"):
        if "://" in t:
            raise ValueError("仅支持 socks5 代理")
        t = f"socks5://{t}"
        low = t.lower()

    # socks5://host:port:user:pass（四段式，无 @）
    m = re.match(
        r"^socks5://([^:/@]+):(\d+):([^:/@]+):(.+)$",
        t,
        re.IGNORECASE,
    )
    if m:
        host, port, user, password = m.groups()
        return f"socks5://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"

    parsed = urlparse(t)
    if parsed.scheme.lower() != "socks5":
        raise ValueError("仅支持 socks5 代理")
    if not parsed.hostname or not parsed.port:
        raise ValueError("代理须包含主机与端口，例如 socks5://ip:port 或 socks5://ip:port:user:pass")
    user = parsed.username or ""
    password = parsed.password or ""
    if user:
        return f"socks5://{quote(user, safe='')}:{quote(password, safe='')}@{parsed.hostname}:{parsed.port}"
    return f"socks5://{parsed.hostname}:{parsed.port}"


def _bind_set_proxy() -> Any:
    global _SET_PROXY_FN, _BIND_TRIED
    if _BIND_TRIED:
        return _SET_PROXY_FN
    _BIND_TRIED = True
    try:
        from neonize._binder import gocode
    except Exception:
        return None

    fn = None
    try:
        fn = gocode.SetProxyAddress
    except AttributeError:
        pass
    if fn is None:
        try:
            handle = getattr(gocode, "_handle", None)
            if handle:
                gpa = ctypes.windll.kernel32.GetProcAddress
                gpa.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                gpa.restype = ctypes.c_void_p
                addr = gpa(ctypes.c_void_p(handle), b"SetProxyAddress")
                if addr:
                    proto = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p)
                    fn = proto(("SetProxyAddress", gocode))
                    gocode.SetProxyAddress = fn
        except Exception:
            fn = None
    if fn is not None:
        try:
            fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
            fn.restype = ctypes.c_char_p
        except Exception:
            pass
    _SET_PROXY_FN = fn
    return fn


def proxy_supported() -> bool:
    return _bind_set_proxy() is not None


def apply_proxy(client: Any, account_id: str, proxy_raw: str) -> bool:
    """在 connect 之前设置代理。返回是否已成功应用。"""
    raw = (proxy_raw or "").strip()
    if not raw:
        return True
    try:
        url = normalize_proxy_url(raw)
    except ValueError as exc:
        warning(f"账号「{account_id}」代理格式无效：{exc}")
        return False

    fn = _bind_set_proxy()
    if fn is None:
        warning(
            f"账号「{account_id}」已保存代理，但当前 neonize 库未提供 SetProxyAddress；"
            "连接仍走本机网络。请使用含代理补丁的 neonize DLL 或等待官方更新。"
        )
        return False

    uid = getattr(client, "uuid", account_id.encode())
    if isinstance(uid, str):
        uid = uid.encode()
    try:
        err = fn(uid, url.encode("utf-8"))
        if err:
            msg = err.decode("utf-8", errors="replace").strip()
            if msg:
                warning(f"账号「{account_id}」设置代理失败：{msg}")
                return False
        info(f"账号「{account_id}」已启用 SOCKS5 代理")
        return True
    except Exception as exc:
        warning(f"账号「{account_id}」设置代理异常：{exc}")
        return False


def clear_proxy(client: Any, account_id: str) -> None:
    fn = _bind_set_proxy()
    if fn is None:
        return
    uid = getattr(client, "uuid", account_id.encode())
    if isinstance(uid, str):
        uid = uid.encode()
    try:
        fn(uid, b"")
    except Exception:
        pass
