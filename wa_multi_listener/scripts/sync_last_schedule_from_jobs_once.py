"""一次性：从任务管理 JSON 同步「上次任务文件名」到 config.json（不写入软件逻辑）。"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "WAHelper"

PLATFORMS = (
    ("whatsapp", "schedule2.json"),
    ("telegram", "schedules.json"),
)


def _jobs_path(platform: str, jobs_file: str) -> Path:
    return DIST / platform / "data" / jobs_file


def _config_path(platform: str) -> Path:
    return DIST / platform / "config.json"


def _map_jobs_to_entry_names(jobs: List[Dict[str, Any]]) -> Dict[str, str]:
    """通讯录条目 id -> source_name；无任务条目不出现（由调用方填 ''）。"""
    out: Dict[str, str] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = str(job.get("source_name") or "").strip()
        for eid in job.get("chat_entry_ids") or []:
            s = str(eid).strip()
            if s:
                out[s] = name
    return out


def _sync_platform(platform: str, jobs_file: str) -> None:
    cfg_path = _config_path(platform)
    jobs_path = _jobs_path(platform, jobs_file)
    if not cfg_path.is_file():
        print(f"[{platform}] 跳过：无 {cfg_path}")
        return
    if not jobs_path.is_file():
        print(f"[{platform}] 跳过：无 {jobs_path}")
        return

    with open(jobs_path, "r", encoding="utf-8") as f:
        jobs = json.load(f)
    if not isinstance(jobs, list):
        jobs = []

    job_map = _map_jobs_to_entry_names(jobs)

    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    book = cfg.get("address_book")
    if not isinstance(book, list):
        print(f"[{platform}] 无 address_book")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = cfg_path.with_suffix(f".json.bak_{stamp}")
    shutil.copy2(cfg_path, bak)

    filled = 0
    cleared = 0
    for ent in book:
        if not isinstance(ent, dict):
            continue
        eid = str(ent.get("id", "")).strip()
        if not eid:
            continue
        if eid in job_map:
            ent["last_schedule_source_name"] = job_map[eid]
            if job_map[eid]:
                filled += 1
            else:
                cleared += 1
        else:
            ent["last_schedule_source_name"] = ""
            cleared += 1

    tmp = cfg_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    tmp.replace(cfg_path)

    print(
        f"[{platform}] 已写入 {cfg_path.name}（备份 {bak.name}）"
        f" · 任务数 {len(jobs)} · 有条目映射 {len(job_map)}"
        f" · 通讯录 {len(book)} · 有文件名 {filled} · 记为空 {cleared}"
    )


def main() -> None:
    if not DIST.is_dir():
        raise SystemExit(f"未找到运行目录：{DIST}")
    print(f"数据目录：{DIST}\n")
    for platform, jobs_file in PLATFORMS:
        _sync_platform(platform, jobs_file)
    print("\n完成。请重启软件或打开「定时任务」页查看「上次任务→」。")


if __name__ == "__main__":
    main()
