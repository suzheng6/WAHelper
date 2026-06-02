"""从用户所选目录导入 TG 的 config.json 与 sessions（仅登录与配置，不含定时任务数据）。"""
from __future__ import annotations

import json
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple

from paths import app_root, resource_path
from platform_paths import combo_root, tg_data_root
from logger_util import info, warning

_CONFIG_NAME = "config.json"
_SESSIONS_DIRNAME = "sessions"


def _read_json(path: str) -> Optional[Any]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _merge_address_book(existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_ref: Dict[str, Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []
    for row in existing + incoming:
        if not isinstance(row, dict):
            continue
        ref = str(row.get("chat_ref", "") or "").strip()
        key = ref.lower() if ref else str(row.get("id", ""))
        if not key:
            continue
        if key in by_ref:
            by_ref[key].update(row)
            continue
        by_ref[key] = dict(row)
        out.append(by_ref[key])
    return out


def _merge_accounts(existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for row in existing + incoming:
        if not isinstance(row, dict):
            continue
        aid = str(row.get("id", "")).strip()
        if not aid:
            continue
        prev = by_id.get(aid)
        if prev is None:
            by_id[aid] = dict(row)
        else:
            prev.update(row)
    return list(by_id.values())


def resolve_legacy_tg_dir(user_path: str) -> Optional[str]:
    """
    在用户选择的文件夹内定位 TG 数据根目录（含 config.json）。
    支持：直接选旧助手目录、选 dist、选含 telegram/ 子目录的整合包目录等。
    """
    if not user_path or not os.path.isdir(user_path):
        return None
    p = os.path.abspath(user_path)

    def _ok(root: str) -> bool:
        return os.path.isfile(os.path.join(root, _CONFIG_NAME))

    if _ok(p):
        return p

    for sub in ("telegram", "tg_multi_listener", "dist", "ChaoQunHelper", "WAHelper"):
        cand = os.path.join(p, sub)
        if _ok(cand):
            return cand

    try:
        for name in os.listdir(p):
            sub = os.path.join(p, name)
            if os.path.isdir(sub) and _ok(sub):
                return sub
    except OSError:
        pass
    return None


def ensure_tg_data_ready() -> str:
    """新用户：创建 telegram/ 目录，并在无 config.json 时写入示例配置（可正常添加账号登录）。"""
    root = tg_data_root()
    sessions = os.path.join(root, _SESSIONS_DIRNAME)
    os.makedirs(sessions, exist_ok=True)
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    os.makedirs(os.path.join(root, "data"), exist_ok=True)

    cfg_path = os.path.join(root, _CONFIG_NAME)
    if os.path.isfile(cfg_path):
        return root

    wa_pkg = os.path.dirname(os.path.abspath(__file__))
    ai_root = os.path.dirname(wa_pkg)
    candidates = [
        os.path.join(root, "config.example.json"),
        os.path.join(wa_pkg, "config.example.tg.json"),
        os.path.join(wa_pkg, "config.example.json"),
        resource_path("config.example.tg.json"),
        resource_path("config.example.json"),
        os.path.join(ai_root, "tg_multi_listener", "dist", "config.example.json"),
        os.path.join(ai_root, "tg_multi_listener", "config.example.json"),
    ]
    for src in candidates:
        if os.path.isfile(src):
            try:
                shutil.copyfile(src, cfg_path)
                info(f"已创建 Telegram 默认配置：{cfg_path}")
                return root
            except OSError:
                continue

    _write_json(
        cfg_path,
        {
            "api_id": 0,
            "api_hash": "",
            "accounts": [],
            "address_book": [],
            "watch_rules": {},
            "rate_limit_seconds": 10,
            "listening_enabled": True,
        },
    )
    info(f"已创建 Telegram 空白配置：{cfg_path}")
    return root


def _copy_session_files(src_sessions: str, dest_sessions: str) -> Tuple[int, int]:
    """复制 .session 及 journal。返回 (新增数, 更新数)。"""
    added = updated = 0
    if not os.path.isdir(src_sessions):
        return added, updated
    os.makedirs(dest_sessions, exist_ok=True)
    for name in os.listdir(src_sessions):
        if not name.endswith(".session"):
            continue
        s = os.path.join(src_sessions, name)
        if not os.path.isfile(s):
            continue
        d = os.path.join(dest_sessions, name)
        try:
            if not os.path.isfile(d):
                shutil.copy2(s, d)
                added += 1
            elif os.path.getmtime(s) > os.path.getmtime(d):
                shutil.copy2(s, d)
                updated += 1
            journal = s + "-journal"
            if os.path.isfile(journal):
                shutil.copy2(journal, d + "-journal")
        except OSError as exc:
            warning(f"复制会话失败 {name}：{exc}")
    return added, updated


def import_from_legacy_dir(user_selected_path: str) -> Tuple[bool, str]:
    """
    从用户选择的文件夹导入 TG 的 config.json 与 sessions/*.session。
    不导入 data/、logs/、定时任务等其它文件。
    """
    src_root = resolve_legacy_tg_dir(user_selected_path)
    if not src_root:
        return (
            False,
            "未在所选文件夹中找到 config.json。\n"
            "请直接选择旧版 TG 助手目录，或包含 config.json 与 sessions 子文件夹的目录。",
        )

    src_cfg_path = os.path.join(src_root, _CONFIG_NAME)
    src_sessions = os.path.join(src_root, _SESSIONS_DIRNAME)
    src_cfg = _read_json(src_cfg_path)
    if not isinstance(src_cfg, dict):
        return False, "config.json 无法解析"

    ensure_tg_data_ready()
    dest_root = tg_data_root()
    dest_cfg_path = os.path.join(dest_root, _CONFIG_NAME)
    dest_sessions = os.path.join(dest_root, _SESSIONS_DIRNAME)
    os.makedirs(dest_sessions, exist_ok=True)

    dest_cfg = _read_json(dest_cfg_path)
    if not isinstance(dest_cfg, dict):
        dest_cfg = {}

    if src_cfg.get("api_id"):
        dest_cfg["api_id"] = src_cfg.get("api_id")
    if src_cfg.get("api_hash"):
        dest_cfg["api_hash"] = src_cfg.get("api_hash")

    dest_cfg["accounts"] = _merge_accounts(
        dest_cfg.get("accounts") if isinstance(dest_cfg.get("accounts"), list) else [],
        src_cfg.get("accounts") if isinstance(src_cfg.get("accounts"), list) else [],
    )
    dest_cfg["address_book"] = _merge_address_book(
        dest_cfg.get("address_book") if isinstance(dest_cfg.get("address_book"), list) else [],
        src_cfg.get("address_book") if isinstance(src_cfg.get("address_book"), list) else [],
    )
    if "rate_limit_seconds" in src_cfg:
        dest_cfg["rate_limit_seconds"] = src_cfg["rate_limit_seconds"]
    if "listening_enabled" in src_cfg:
        dest_cfg["listening_enabled"] = src_cfg["listening_enabled"]

    _write_json(dest_cfg_path, dest_cfg)
    added, updated = _copy_session_files(src_sessions, dest_sessions)

    n_acc = len(dest_cfg.get("accounts") or [])
    n_ab = len(dest_cfg.get("address_book") or [])
    rel = os.path.relpath(src_root, user_selected_path)
    from_note = f"（识别目录：{rel}）" if rel != "." else ""
    info(
        f"TG 已导入 config + sessions：来源 {src_root}，账号 {n_acc}，通讯录 {n_ab}，"
        f"会话 +{added} 更新 {updated}"
    )
    return (
        True,
        f"已从所选文件夹导入登录与配置{from_note}\n"
        f"· 账号：{n_acc} 个\n"
        f"· 通讯录：{n_ab} 条\n"
        f"· 会话文件：新增 {added}，更新 {updated}\n"
        f"保存位置：telegram/\n\n"
        f"请切换到 Telegram 标签；若已打开请关闭程序后重开，或在导入后自动刷新界面。",
    )


def default_legacy_tg_candidates() -> List[str]:
    """文件夹选择对话框的初始路径候选。"""
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    home = os.path.expanduser("~")
    out: List[str] = []
    for p in (
        os.path.join(parent, "tg_multi_listener", "dist"),
        os.path.join(parent, "tg_multi_listener"),
        combo_root(),
        os.path.join(home, "Desktop"),
        parent,
    ):
        if p and os.path.isdir(p) and p not in out:
            out.append(p)
    return out
