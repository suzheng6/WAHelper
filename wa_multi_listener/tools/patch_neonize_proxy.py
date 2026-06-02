"""为 neonize 编译带 SetProxyAddress 的 goneonize DLL（需本机已安装 Go）。"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _goneonize_version() -> str:
    import importlib.util

    spec = importlib.util.find_spec("neonize")
    if not spec or not spec.origin:
        return "0.3.18.post0"
    dl = os.path.join(os.path.dirname(spec.origin), "download.py")
    with open(dl, "r", encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"__GONEONIZE_VERSION__\s*=\s*['\"]([^'\"]+)['\"]", text)
    return m.group(1) if m else "0.3.18.post0"


def _patch_main_go(src: str) -> str:
    if "SetProxyAddress" in src:
        return src
    pending = """
var pendingProxy = make(map[string]string)

//export SetProxyAddress
func SetProxyAddress(id *C.char, addr *C.char) *C.char {
	uuid := C.GoString(id)
	proxyAddr := C.GoString(addr)
	client, exists := clients[uuid]
	if !exists {
		if proxyAddr == "" {
			delete(pendingProxy, uuid)
		} else {
			pendingProxy[uuid] = proxyAddr
		}
		return C.CString("")
	}
	if proxyAddr == "" {
		client.SetProxy(nil)
		return C.CString("")
	}
	err := client.SetProxyAddress(proxyAddr)
	if err != nil {
		return C.CString(err.Error())
	}
	return C.CString("")
}

"""
    marker = "func Neonize("
    if marker not in src:
        raise RuntimeError("未找到 Neonize 函数，goneonize 版本可能已变更")
    m = re.search(r"(?m)^import\s*\(", src)
    if m:
        depth = 0
        i = m.start()
        while i < len(src):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        src = src[:i] + "\n" + pending + src[i:]
    else:
        pkg = re.search(r"(?m)^package main\s*\n", src)
        if not pkg:
            raise RuntimeError("无法定位 package main")
        end = pkg.end()
        src = src[:end] + pending + src[end:]
    needle = "\tclients[uuid] = client"
    if needle in src:
        insert = """
	if proxyAddr, ok := pendingProxy[uuid]; ok {
		if err := client.SetProxyAddress(proxyAddr); err != nil {
			fmt.Printf("WARNING: proxy %s: %v\\n", proxyAddr, err)
		}
		delete(pendingProxy, uuid)
	}
"""
        src = src.replace(needle, needle + insert, 1)
    return src


def main() -> int:
    ver = _goneonize_version()
    print(f"goneonize 版本: {ver}")
    go = shutil.which("go")
    if not go:
        print("未找到 go 命令，请先安装 Go: https://go.dev/dl/")
        return 1

    spec = importlib.util.find_spec("neonize")
    if not spec or not spec.origin:
        print("未找到 neonize 包")
        return 1
    pkg = os.path.dirname(spec.origin)
    dll_name = "neonize-windows-amd64.dll"
    if sys.platform != "win32":
        print("当前脚本仅演示 Windows amd64 构建，请按平台调整 -buildmode 输出名")
        return 1

    tmp = tempfile.mkdtemp(prefix="goneonize-patch-")
    try:
        repo = os.path.join(tmp, "neonize")
        tag = ver if ver.startswith("v") else ver
        env_git = os.environ.copy()
        env_git.pop("HTTP_PROXY", None)
        env_git.pop("HTTPS_PROXY", None)
        env_git.pop("ALL_PROXY", None)
        subprocess.check_call(
            [
                "git",
                "-c",
                "http.proxy=",
                "-c",
                "https.proxy=",
                "clone",
                "--depth",
                "1",
                "--branch",
                tag,
                "https://github.com/krypton-byte/neonize.git",
                repo,
            ],
            env=env_git,
            stdout=subprocess.DEVNULL,
        )
        go_dir = os.path.join(repo, "goneonize")
        if not os.path.isdir(go_dir):
            raise RuntimeError("neonize 仓库中未找到 goneonize 目录")
        main_go = os.path.join(go_dir, "main.go")
        with open(main_go, "r", encoding="utf-8") as f:
            content = f.read()
        content = _patch_main_go(content)
        with open(main_go, "w", encoding="utf-8") as f:
            f.write(content)
        ver_go = os.path.join(go_dir, "version.go")
        if os.path.isfile(ver_go):
            with open(ver_go, "r", encoding="utf-8") as f:
                vsrc = f.read()
            vsrc = re.sub(
                r'version := "[^"]*"',
                f'version := "{ver}"',
                vsrc,
                count=1,
            )
            with open(ver_go, "w", encoding="utf-8") as f:
                f.write(vsrc)
        out = os.path.join(tmp, dll_name)
        env = os.environ.copy()
        env["CGO_ENABLED"] = "1"
        subprocess.check_call(
            [go, "build", "-buildmode=c-shared", "-o", out, "."],
            cwd=go_dir,
            env=env,
        )
        dst_pkg = os.path.join(pkg, dll_name)
        dst_app = os.path.join(ROOT, dll_name)
        shutil.copy2(out, dst_app)
        print(f"已复制补丁 DLL 到程序目录:\n  {dst_app}")
        try:
            shutil.copy2(out, dst_pkg)
            print(f"已覆盖 site-packages:\n  {dst_pkg}")
        except OSError as exc:
            print(f"未能覆盖 site-packages（请先关闭 WhatsApp 助手）: {exc}")
            print("下次启动时会从程序目录自动安装补丁。")
        print("请重启 WhatsApp 助手。")
    except subprocess.CalledProcessError as exc:
        print(f"构建失败: {exc}")
        return 1
    finally:
        built = os.path.join(tmp, dll_name)
        if os.path.isfile(built):
            keep = os.path.join(ROOT, "_build_" + dll_name)
            try:
                shutil.copy2(built, keep)
            except OSError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
