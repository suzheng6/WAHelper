"""从旧版 config.json 恢复通讯录「上次任务→」文件名。

适用场景：误删全部任务后添加新任务，导致其它群的 last_schedule_source_name 被清空。

默认只恢复当前为空的条目；加 --force 会用备份覆盖全部条目。

示例（整合版 WAHelper，TG 配置在 telegram 子目录）:
  python restore_last_schedule_from_backup.py ^
    --current "D:\\WAHelper\\telegram\\config.json" ^
    --backup "D:\\旧文件夹\\telegram\\config.json"

仅预览、不写文件:
  python restore_last_schedule_from_backup.py --current ... --backup ... --dry-run
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _load_last_schedule_map(path: Path) -> Dict[str, str]:
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    out: Dict[str, str] = {}
    for row in raw.get("address_book", []):
        if not isinstance(row, dict):
            continue
        eid = str(row.get("id", "")).strip()
        if not eid:
            continue
        out[eid] = str(row.get("last_schedule_source_name", "") or "").strip()
    return out


def _remark_map(path: Path) -> Dict[str, str]:
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    out: Dict[str, str] = {}
    for row in raw.get("address_book", []):
        if not isinstance(row, dict):
            continue
        eid = str(row.get("id", "")).strip()
        if eid:
            out[eid] = str(row.get("remark", "") or eid).strip() or eid
    return out


def merge_last_schedule(
    current_path: Path,
    backup_path: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> Tuple[List[Tuple[str, str, str]], int]:
    """返回 (恢复列表[(id, remark, name)], 跳过数)。"""
    if not current_path.is_file():
        raise FileNotFoundError(f"当前配置不存在: {current_path}")
    if not backup_path.is_file():
        raise FileNotFoundError(f"备份配置不存在: {backup_path}")

    backup_map = _load_last_schedule_map(backup_path)
    remarks = _remark_map(current_path)

    with current_path.open("r", encoding="utf-8") as f:
        cfg: Dict[str, Any] = json.load(f)

    restored: List[Tuple[str, str, str]] = []
    skipped = 0
    for row in cfg.get("address_book", []):
        if not isinstance(row, dict):
            continue
        eid = str(row.get("id", "")).strip()
        if not eid:
            continue
        cur = str(row.get("last_schedule_source_name", "") or "").strip()
        bak = backup_map.get(eid, "")
        if not bak:
            continue
        if cur and not force:
            skipped += 1
            continue
        if cur == bak:
            continue
        row["last_schedule_source_name"] = bak
        restored.append((eid, remarks.get(eid, eid), bak))

    if restored and not dry_run:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        pre = current_path.with_name(current_path.name + f".pre_restore_{ts}")
        shutil.copy2(current_path, pre)
        with current_path.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"已备份当前配置到: {pre}")

    return restored, skipped


def main() -> None:
    p = argparse.ArgumentParser(description="从备份 config.json 恢复「上次任务→」")
    p.add_argument("--current", required=True, help="当前正在使用的 config.json 路径")
    p.add_argument("--backup", required=True, help="旧版/备份 config.json 路径")
    p.add_argument("--force", action="store_true", help="覆盖当前非空条目（默认只填空项）")
    p.add_argument("--dry-run", action="store_true", help="只显示将恢复的内容，不写文件")
    args = p.parse_args()

    restored, skipped = merge_last_schedule(
        Path(args.current),
        Path(args.backup),
        force=args.force,
        dry_run=args.dry_run,
    )

    if not restored:
        print("未恢复任何条目。")
        if skipped:
            print(f"（{skipped} 个条目当前已有值且未加 --force，已跳过）")
        print("请确认 --backup 指向误操作之前的 config.json，或尝试 Windows「以前的版本」找旧文件。")
        return

    print(f"{'[预览] ' if args.dry_run else ''}将恢复 {len(restored)} 个群的上次任务标记：")
    for _eid, remark, name in restored:
        print(f"  · {remark} → {name}")
    if skipped:
        print(f"另有 {skipped} 个条目当前非空，未覆盖（可用 --force）。")
    if args.dry_run:
        print("未写入文件。确认无误后去掉 --dry-run 再执行。")
    else:
        print("已写入当前 config.json。请重启 WAHelper 后打开 TG 通讯录查看。")


if __name__ == "__main__":
    main()
