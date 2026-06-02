"""清理 dist 下可再生成或与主 exe 重复的产物，减小体积、避免误用多份 _internal。

保留：超群小帮手.exe（或 ChaoQunHelper.exe）、_internal、config、data、sessions。
删除：*.bak、与中文 exe 重复的 ChaoQunHelper.exe、超群小帮手_分发/、超群小帮手_分发.zip、logs 内日志文件。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"


def main() -> None:
    if not DIST.is_dir():
        print("无 dist 目录，跳过。")
        return

    removed: list[str] = []

    for p in DIST.glob("*.bak"):
        try:
            p.unlink()
            removed.append(str(p.relative_to(ROOT)))
        except OSError as e:
            print(f"跳过删除 {p}: {e}", file=sys.stderr)

    exes = sorted(DIST.glob("*.exe"))
    names = {p.name for p in exes}
    # 若存在除 ChaoQunHelper.exe 外的其它 .exe，视为中文/别名入口，可删英文重复
    other_exe = [p for p in exes if p.name != "ChaoQunHelper.exe"]
    if other_exe and "ChaoQunHelper.exe" in names:
        dup = DIST / "ChaoQunHelper.exe"
        try:
            dup.unlink()
            removed.append(str(dup.relative_to(ROOT)))
        except OSError as e:
            print(f"跳过删除 {dup}: {e}", file=sys.stderr)

    for p in list(DIST.iterdir()):
        if p.is_dir() and p.name.endswith("_分发"):
            try:
                shutil.rmtree(p)
                removed.append(str(p.relative_to(ROOT)))
            except OSError as e:
                print(f"跳过删除目录 {p}: {e}", file=sys.stderr)
        elif p.is_file() and p.suffix == ".zip" and p.name.endswith("_分发.zip"):
            try:
                p.unlink()
                removed.append(str(p.relative_to(ROOT)))
            except OSError as e:
                print(f"跳过删除 {p}: {e}", file=sys.stderr)

    log_dir = DIST / "logs"
    if log_dir.is_dir():
        for f in log_dir.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                    removed.append(str(f.relative_to(ROOT)))
                except OSError:
                    pass

    if removed:
        print("已删除：")
        for r in removed:
            print(f"  {r}")
    else:
        print("没有需要删除的项（或路径不存在）。")


if __name__ == "__main__":
    main()
