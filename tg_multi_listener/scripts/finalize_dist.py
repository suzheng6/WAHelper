"""PyInstaller 完成后：把 dist/ChaoQunHelper/* 展平到 dist 根目录（覆盖），再生成 dist/超群小帮手.exe。

onedir 要求 exe 与 _internal 同级；全部只在 dist 根目录，不保留 ChaoQunHelper 子文件夹。
"""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD_SUB = DIST / "ChaoQunHelper"
SRC_ONEFILE = DIST / "ChaoQunHelper.exe"
DST_CN = DIST / "超群小帮手.exe"


def flatten_onedir() -> bool:
    """若存在 dist/ChaoQunHelper/，将其内容移到 dist 根目录并删除子文件夹。"""
    exe_nested = BUILD_SUB / "ChaoQunHelper.exe"
    if not (BUILD_SUB.is_dir() and exe_nested.is_file()):
        return False
    for item in list(BUILD_SUB.iterdir()):
        dest = DIST / item.name
        try:
            if dest.exists() or dest.is_symlink():
                if dest.is_dir():
                    shutil.rmtree(dest, ignore_errors=False)
                else:
                    dest.unlink()
        except OSError as exc:
            raise SystemExit(f"无法清空目标以覆盖：{dest} — {exc}") from exc
        shutil.move(str(item), str(dest))
    try:
        BUILD_SUB.rmdir()
    except OSError:
        pass
    return True


def main() -> None:
    flatten_onedir()

    src = DIST / "ChaoQunHelper.exe" if (DIST / "ChaoQunHelper.exe").is_file() else SRC_ONEFILE
    if not src.is_file():
        raise SystemExit(f"未找到 {DIST / 'ChaoQunHelper.exe'} 或 {SRC_ONEFILE}，请先执行 PyInstaller。")

    try:
        if DST_CN.is_file():
            try:
                DST_CN.unlink()
            except OSError:
                pass
        shutil.copy2(src, DST_CN)
    except OSError as exc:
        print(f"警告：无法写入 {DST_CN}（通常 exe 正在运行）：{exc}")
        print(f"可直接运行：{src}")
        raise SystemExit(0)

    if src.name == "ChaoQunHelper.exe" and src.resolve() != DST_CN.resolve():
        try:
            src.unlink()
        except OSError:
            pass

    print(f"已生成（dist 根目录）: {DST_CN}")


if __name__ == "__main__":
    main()
