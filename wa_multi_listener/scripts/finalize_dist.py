"""构建后：中文 exe 名、补丁 neonize DLL、说明文件。"""
from __future__ import annotations

import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
OUT = os.path.join(DIST, os.environ.get("WA_COLLECT_NAME", "WAHelper"))
SRC = os.path.join(OUT, "WAHelper.exe")
DST_CN = os.path.join(OUT, "超群小帮手.exe")
DLL_NAME = "neonize-windows-amd64.dll"
LEGACY_EXE_NAMES = ("WhatsApp监听助手.exe",)


def main() -> int:
    if os.path.isfile(SRC):
        if os.path.isfile(DST_CN):
            os.remove(DST_CN)
        os.replace(SRC, DST_CN)
        print(f"已生成：{DST_CN}")
    elif os.path.isfile(DST_CN):
        print(f"已存在：{DST_CN}")
    else:
        print(f"未找到构建产物：{SRC}")
        return 1

    for legacy in LEGACY_EXE_NAMES:
        legacy_path = os.path.join(OUT, legacy)
        if os.path.isfile(legacy_path):
            os.remove(legacy_path)
            print(f"已移除旧版程序：{legacy_path}")

    patched = os.path.join(ROOT, DLL_NAME)
    if os.path.isfile(patched):
        shutil.copy2(patched, os.path.join(OUT, DLL_NAME))
        print(f"已复制代理补丁 DLL 到：{OUT}")

    for name in ("整合版说明.txt", "定时任务导入说明与示例.txt", "请先看我.txt"):
        for src_dir in (os.path.join(ROOT, "docs"), ROOT):
            src = os.path.join(src_dir, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(OUT, name))
                print(f"已复制：{name}")
                break

    # 分发目录附带示例配置（非用户真实 config.json）
    for sub, names in (
        ("whatsapp", ("config.example.json",)),
        ("telegram", ("config.example.tg.json", "config.example.json")),
        ("", ("config.example.json",)),
    ):
        dest_dir = os.path.join(OUT, sub) if sub else OUT
        os.makedirs(dest_dir, exist_ok=True)
        for name in names:
            src = os.path.join(ROOT, name)
            if not os.path.isfile(src):
                continue
            dest = os.path.join(dest_dir, "config.example.json")
            if not os.path.isfile(dest):
                shutil.copy2(src, dest)
                print(f"已复制示例配置：{dest}")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
