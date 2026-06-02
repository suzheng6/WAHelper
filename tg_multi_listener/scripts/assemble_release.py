"""从 dist 根目录直接打「超群小帮手_分发.zip」（zip 内为扁平结构），不创建超群小帮手_分发文件夹。"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
ZIP_OUT = DIST / "超群小帮手_分发.zip"


def _pick_exe() -> Path:
    cn = DIST / "超群小帮手.exe"
    eng = DIST / "ChaoQunHelper.exe"
    if cn.is_file():
        return cn
    if eng.is_file():
        return eng
    raise SystemExit(f"dist 根目录未找到 超群小帮手.exe 或 ChaoQunHelper.exe（请先 finalize_dist / PyInstaller）")


def main() -> None:
    legacy_pkg = DIST / "超群小帮手_分发"
    if legacy_pkg.is_dir():
        try:
            shutil.rmtree(legacy_pkg)
            print(f"已删除旧版分发目录: {legacy_pkg}")
        except OSError as exc:
            print(f"警告：无法删除旧目录 {legacy_pkg}: {exc}")

    exe = _pick_exe()
    internal = DIST / "_internal"
    if not internal.is_dir():
        raise SystemExit(f"缺少 {internal}，请确认 onedir 已展平到 dist 根目录。")

    if ZIP_OUT.is_file():
        ZIP_OUT.unlink()

    with zipfile.ZipFile(ZIP_OUT, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        arc_exe = "超群小帮手.exe"
        if exe.name == arc_exe:
            zf.write(exe, arc_exe)
        else:
            zf.write(exe, arc_exe)

        for p in sorted(internal.rglob("*")):
            if p.is_file():
                arc = "_internal/" + p.relative_to(internal).as_posix()
                zf.write(p, arc)

        cfg = ROOT / "config.example.json"
        if cfg.is_file():
            zf.write(cfg, "config.example.json")

        readme = ROOT / "docs" / "用户使用说明.txt"
        if readme.is_file():
            zf.write(readme, "请先看我.txt")

        sched = ROOT / "docs" / "定时任务导入说明与示例.txt"
        if sched.is_file():
            zf.write(sched, sched.name)

        dist_sessions = DIST / "sessions"
        if dist_sessions.is_dir() and any(dist_sessions.iterdir()):
            for p in dist_sessions.rglob("*"):
                if p.is_file():
                    zf.write(p, "sessions/" + p.relative_to(dist_sessions).as_posix())

        dist_data = DIST / "data"
        if dist_data.is_dir() and any(dist_data.iterdir()):
            for p in dist_data.rglob("*"):
                if p.is_file():
                    zf.write(p, "data/" + p.relative_to(dist_data).as_posix())

        dist_cfg = DIST / "config.json"
        if dist_cfg.is_file():
            zf.write(dist_cfg, "config.json")

    print(f"分发压缩包（扁平）: {ZIP_OUT}")


if __name__ == "__main__":
    main()
