"""WhatsApp 客户端公共配置与连接辅助。"""
from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING, Optional, Tuple

from config import Account

if TYPE_CHECKING:
    from listener import ListenerController
from logger_util import info, warning
from neonize.aioze.client import ClientFactory, NewAClient
from neonize.aioze.events import EVENT_TO_INT, MessageEv
from neonize.aioze.events import ConnectedEv, ConnectFailureEv, LoggedOutEv
from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps
from wa_proxy import apply_proxy, clear_proxy

_CONNECT_TIMEOUT_DIRECT = 30.0
_CONNECT_TIMEOUT_PROXY = 30.0

_CONNECT_FAIL_ZH = {
    1: "连接失败（一般错误）",
    2: "已登出，请重新扫码",
    3: "账号被临时限制，请稍后再试",
    4: "主设备已离线，请先在手机上打开 WhatsApp",
    5: "未知登出，请清除会话后重新扫码",
    6: "客户端版本过旧，请更新本软件或 neonize",
    7: "设备标识异常",
    8: "WhatsApp 服务器错误，请稍后重试",
    9: "实验功能限制",
    10: "WhatsApp 服务不可用，请稍后重试",
}


def make_device_props() -> DeviceProps:
    """模拟官方 WhatsApp Web（Chrome），降低「无法关联设备」概率。"""
    return DeviceProps(os="Windows", platformType=DeviceProps.CHROME)


def connect_failure_message(reason: int, raw: str = "") -> str:
    base = _CONNECT_FAIL_ZH.get(int(reason), f"连接失败（代码 {reason}）")
    raw = (raw or "").strip()
    if raw:
        return f"{base}：{raw}"
    return base


def pair_error_message(err: str) -> str:
    t = (err or "").strip()
    if not t:
        return "配对失败（手机显示无法关联设备）"
    low = t.lower()
    if "link" in low or "关联" in t or "pair" in low:
        return (
            f"无法关联设备：{t}\n\n"
            "建议：① 手机 WhatsApp 升级到最新版 ② 点「清除会话并重新扫码」"
            " ③ 在手机「已连接的设备」里删掉旧的本程序设备 ④ 关闭 VPN 后重试"
        )
    return f"配对失败：{t}"


def new_client(acc: Account, listener: Optional["ListenerController"] = None) -> NewAClient:
    factory = ClientFactory(acc.db_path())
    if listener is not None:
        listener.ensure_message_subscription(factory)
        listener.ensure_group_info_subscription(factory)
    client = factory.new_client(uuid=acc.id, props=make_device_props())
    if listener is not None:
        listener.wrap_client_execute(client)
    if (acc.proxy or "").strip():
        apply_proxy(client, acc.id, acc.proxy)
    else:
        clear_proxy(client, acc.id)
    return client


def connect_timeout_seconds(acc: Account) -> float:
    if (acc.proxy or "").strip():
        return _CONNECT_TIMEOUT_PROXY
    return _CONNECT_TIMEOUT_DIRECT


async def establish_connection(client: NewAClient, acc: Account) -> Tuple[bool, str]:
    """启动连接并等待 ConnectedEv；勿 await connect_task（Neonize 长期运行，超时会被取消）。"""
    connected_ev = asyncio.Event()
    failed_ev = asyncio.Event()
    fail_msg: list[str] = [""]

    @client.event(ConnectedEv)
    async def _on_connected(_c, _ev: ConnectedEv) -> None:
        connected_ev.set()

    @client.event(ConnectFailureEv)
    async def _on_failure(_c, ev: ConnectFailureEv) -> None:
        fail_msg[0] = connect_failure_message(ev.Reason, ev.Message or "")
        failed_ev.set()

    @client.event(LoggedOutEv)
    async def _on_logged_out(_c, ev: LoggedOutEv) -> None:
        fail_msg[0] = connect_failure_message(ev.Reason, "已登出，请重新扫码")
        failed_ev.set()

    try:
        from neonize.aioze.events import EVENT_TO_INT, MessageEv

        msg_code = EVENT_TO_INT[MessageEv]
        codes = sorted(client.event.list_func.keys())
        if msg_code in client.event.list_func:
            info(f"账号「{acc.id}」连接前已登记 MessageEv（事件码 {msg_code}）")
        else:
            warning(
                f"账号「{acc.id}」连接前未登记 MessageEv，当前事件码={codes}；"
                "将无法接收群消息监听"
            )
    except Exception:
        pass

    t0 = time.monotonic()
    await client.connect()
    timeout = connect_timeout_seconds(acc)
    proxy_note = "（经 SOCKS5 代理）" if (acc.proxy or "").strip() else ""
    info(f"账号「{acc.id}」已向 WhatsApp 发起连接{proxy_note}，最长等待 {int(timeout)} 秒")
    deadline = time.monotonic() + timeout
    next_log_at = 15.0
    while time.monotonic() < deadline:
        if failed_ev.is_set():
            return False, fail_msg[0] or "连接失败"
        if connected_ev.is_set() or client.connected:
            elapsed = time.monotonic() - t0
            info(f"账号「{acc.id}」连接完成，耗时 {elapsed:.1f} 秒")
            return True, ""
        elapsed = time.monotonic() - t0
        if elapsed >= next_log_at:
            info(
                f"账号「{acc.id}」仍在等待 WhatsApp 响应… 已 {int(elapsed)} 秒"
                "（含代理握手 / WebSocket / 密钥同步，网络慢时会更久）"
            )
            next_log_at += 30.0
        await asyncio.sleep(0.25)
    hint = "连接超时"
    if (acc.proxy or "").strip():
        hint += "（已配置代理，请确认代理可用且格式正确）"
    else:
        hint += "（可尝试配置 SOCKS5 代理或重新扫码）"
    return False, hint


def clear_session_files(acc: Account) -> None:
    path = acc.db_path()
    for p in (path, path + "-shm", path + "-wal"):
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError as exc:
                warning(f"删除会话文件失败 {p}：{exc}")
    info(f"已清除账号「{acc.id}」的本地会话，请重新扫码")


async def stop_all_clients() -> None:
    try:
        await ClientFactory.stop()
    except Exception as exc:
        warning(f"停止 WhatsApp 连接时：{exc}")
    await asyncio.sleep(0.5)


def stop_all_clients_sync() -> None:
    try:
        asyncio.run(stop_all_clients())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(stop_all_clients())
        finally:
            loop.close()
