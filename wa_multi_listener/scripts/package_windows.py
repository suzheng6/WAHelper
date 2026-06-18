"""Windows 打包：构建 dist/WAHelper；分发 zip 不含登录与配置。"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
PRESERVE = ROOT / "userdata_preserve"
COLLECT_NAME = os.environ.get("WA_COLLECT_NAME", "WAHelper")
OUT = DIST / COLLECT_NAME
VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"
PYINSTALLER = ROOT.parent / ".venv" / "Scripts" / "pyinstaller.exe"
APP_ZIP_BASENAME = "超群小帮手"
SHELL_PY = ROOT / "wa_ui" / "shell.py"

PLATFORM_DIRS = ("whatsapp", "telegram")
LEGACY_FLAT_USER = ("config.json", "sessions", "data", "logs")
USERDATA_SUBDIRS = frozenset({"sessions", "data", "logs", "userdata_preserve"})
USERDATA_FILES = frozenset({"config.json"})


def read_app_version() -> str:
    text = SHELL_PY.read_text(encoding="utf-8")
    m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', text)
    if not m:
        raise RuntimeError(f"未在 {SHELL_PY} 中找到 APP_VERSION")
    return m.group(1).strip()


def distribution_zip_name(*, include_userdata: bool = False) -> str:
    ver = read_app_version()
    if include_userdata:
        return f"{APP_ZIP_BASENAME}_{ver}_本机完整.zip"
    return f"{APP_ZIP_BASENAME}_{ver}.zip"


def _copy_file_newer(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not src.is_file():
        return
    if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
        return
    shutil.copy2(src, dst)


def _merge_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_file():
        _copy_file_newer(src, dst)
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        s, d = item, dst / item.name
        if item.is_dir():
            _merge_tree(s, d)
        else:
            _copy_file_newer(s, d)


def _backup_schedule2() -> None:
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for p in (
        PRESERVE / "whatsapp" / "data" / "schedule2.json",
        OUT / "whatsapp" / "data" / "schedule2.json",
        OUT / "data" / "schedule2.json",
    ):
        if p.is_file():
            shutil.copy2(p, p.with_name(f"schedule2.json.bak_{stamp}"))
            print(f"已备份定时任务：{p.with_name(f'schedule2.json.bak_{stamp}')}")


def _pull_from_dist_root(src_root: Path) -> None:
    if not src_root.is_dir():
        return
    for sub in PLATFORM_DIRS:
        _merge_tree(src_root / sub, PRESERVE / sub)
    wa_dest = PRESERVE / "whatsapp"
    for name in LEGACY_FLAT_USER:
        _merge_tree(src_root / name, wa_dest / name)
    print(f"已收拢用户数据：{src_root} → {PRESERVE}")


def pull_userdata_into_preserve() -> None:
    PRESERVE.mkdir(parents=True, exist_ok=True)
    _backup_schedule2()
    for name in ("WAHelper", "WAHelper_build"):
        _pull_from_dist_root(DIST / name)


def restore_userdata_to_out() -> None:
    if not PRESERVE.is_dir():
        return
    OUT.mkdir(parents=True, exist_ok=True)
    for sub in PLATFORM_DIRS:
        src = PRESERVE / sub
        if src.exists():
            _merge_tree(src, OUT / sub)
            print(f"已恢复用户数据：{sub} → {OUT}")
    legacy_data = PRESERVE / "data"
    if legacy_data.is_dir() and not (PRESERVE / "whatsapp" / "data").exists():
        _merge_tree(legacy_data, OUT / "whatsapp" / "data")


def _is_userdata_in_release(rel: Path) -> bool:
    """判断是否为用户登录/配置/业务数据（不应打进对外分发 zip）。"""
    parts = rel.parts
    if not parts:
        return False
    name = rel.name
    if name in USERDATA_FILES:
        return True
    if name.startswith("schedule2.json.bak"):
        return True
    if parts[0] in USERDATA_SUBDIRS:
        return True
    if len(parts) >= 2 and parts[0] in PLATFORM_DIRS:
        if parts[1] in USERDATA_SUBDIRS:
            return True
        if len(parts) >= 2 and parts[1] == "config.json":
            return True
    if len(parts) >= 3 and parts[0] in PLATFORM_DIRS and parts[2] in USERDATA_SUBDIRS:
        return True
    if name in ("WAHelper.exe", "WhatsApp监听助手.exe"):
        return True
    low = name.lower()
    if "sessions" in parts and (low.endswith(".db") or low.endswith(".session")):
        return True
    return False


def make_zip(*, include_userdata: bool, zip_name: str) -> Path:
    zip_path = DIST / zip_name
    if zip_path.is_file():
        zip_path.unlink()
    skipped = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in OUT.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(OUT)
            if not include_userdata and _is_userdata_in_release(rel):
                skipped += 1
                continue
            zf.write(f, arcname=str(rel))
    mb = zip_path.stat().st_size / (1024 * 1024)
    label = "含本机登录/配置" if include_userdata else "不含登录/配置（可对外分发）"
    print(f"已生成压缩包：{zip_path}（约 {mb:.1f} MB，{label}，跳过 {skipped} 个用户文件）")
    return zip_path


def run_pyinstaller() -> int:
    env = os.environ.copy()
    env["WA_COLLECT_NAME"] = COLLECT_NAME
    cmd = [
        str(PYINSTALLER),
        str(ROOT / "build_windows.spec"),
        "--noconfirm",
        "--clean",
    ]
    print("正在运行 PyInstaller…")
    return subprocess.run(cmd, cwd=str(ROOT), env=env).returncode


def run_finalize() -> int:
    env = os.environ.copy()
    env["WA_COLLECT_NAME"] = COLLECT_NAME
    return subprocess.run(
        [str(VENV_PY), str(ROOT / "scripts" / "finalize_dist.py")],
        cwd=str(ROOT),
        env=env,
    ).returncode


def main() -> int:
    if not PYINSTALLER.is_file():
        print(f"未找到 pyinstaller：{PYINSTALLER}")
        return 1

    pull_userdata_into_preserve()

    if run_pyinstaller() != 0:
        return 1

    if run_finalize() != 0:
        return 1

    restore_userdata_to_out()

    build_dir = ROOT / "build"
    if build_dir.is_dir():
        shutil.rmtree(build_dir, ignore_errors=True)

    # 对外分发：不含 sessions / config.json / data / logs
    dist_zip = distribution_zip_name(include_userdata=False)
    make_zip(include_userdata=False, zip_name=dist_zip)
    # 可选：本机完整备份 zip（含登录），默认不生成以节省时间；需要时设 WA_ZIP_WITH_USERDATA=1
    if os.environ.get("WA_ZIP_WITH_USERDATA", "").strip() in ("1", "true", "yes"):
        make_zip(include_userdata=True, zip_name=distribution_zip_name(include_userdata=True))

    print(f"\n完成。")
    print(f"  本机运行目录（含你的登录数据）：{OUT}")
    print(f"  对外分发包（无登录/配置）：{DIST / dist_zip}")
    print(f"  用户数据备份目录：{PRESERVE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
