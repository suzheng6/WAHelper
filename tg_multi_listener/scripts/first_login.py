"""命令行备用登录（与图形界面共用 config.json 里的共用 API）。

用法：
  cd tg_multi_listener
  python scripts/first_login.py --session 账号session名

会从项目根目录 config.json 读取 api_id / api_hash；
若未配置，仍可用参数： --api-id / --api-hash。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from compat_config import SESSIONS_DIR, ensure_dirs, load_config  # noqa: E402
from telethon import TelegramClient  # noqa: E402


async def run(api_id: int, api_hash: str, session_name: str) -> None:
    ensure_dirs()
    path = os.path.join(SESSIONS_DIR, f"{session_name}.session")
    client = TelegramClient(path, api_id, api_hash)
    await client.start()
    me = await client.get_me()
    print(f"登录成功：{me.id} @{getattr(me, 'username', '') or '-'}")
    await client.disconnect()


def main() -> None:
    p = argparse.ArgumentParser(description="Telegram session 首次登录（CLI）")
    p.add_argument("--session", default=os.environ.get("SESSION_NAME", "default"))
    p.add_argument("--api-id", type=int, default=0)
    p.add_argument("--api-hash", default="")
    args = p.parse_args()

    api_id = int(args.api_id or 0)
    api_hash = (args.api_hash or "").strip()

    if not api_id or not api_hash:
        cfg = load_config()
        api_id = int(cfg.api_id or 0)
        api_hash = str(cfg.api_hash or "").strip()

    if not api_id or not api_hash:
        print("请先在 config.json 中填写 api_id / api_hash，或使用 --api-id / --api-hash")
        sys.exit(1)

    asyncio.run(run(api_id, api_hash, args.session.strip()))


if __name__ == "__main__":
    main()
