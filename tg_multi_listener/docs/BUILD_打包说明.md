# Windows 分发包打包说明（维护者）

目标：一键生成「无需 Python」的绿色压缩包，最终用户解压后双击 exe 即用。

## 1. 目录约定（已实现）

| 场景 | 配置与数据位置 |
|------|----------------|
| 源码运行 | `tg_multi_listener/` 目录 |
| PyInstaller 单文件 exe | **与 exe 同目录**（`paths.app_root()`），便于拷贝整个文件夹分发 |

首次启动若无 `config.json`，会从内置或同目录的 `config.example.json` 复制生成。

## 2. 本地打包命令

在项目根目录 `tg_multi_listener/` 下：

```powershell
pip install -r requirements.txt
pip install -r requirements-build.txt
python -m PyInstaller --noconfirm build_windows.spec
python scripts\finalize_dist.py
```

生成物：

- `dist/ChaoQunHelper.exe`（PyInstaller 构建名，ASCII，避免少数环境下编码问题）
- `dist/超群小帮手.exe`（与界面标题一致的中文程序名，由 `scripts/finalize_dist.py` 复制生成）

## 3. 组装发给用户的压缩包（一键，推荐）

在项目根目录执行：

```powershell
.\scripts\package_release.ps1
```

会自动完成：安装依赖 → PyInstaller → `scripts/finalize_dist.py`（生成 `超群小帮手.exe`）→ `scripts/assemble_release.py`（UTF-8 处理中文路径）组装 **`dist/超群小帮手_分发/`** 并生成 **`dist/超群小帮手_分发.zip`**。分发目录内含：`超群小帮手.exe`、`config.example.json`、`请先看我.txt`、`定时任务导入说明与示例.txt`（若文件存在）。

手动组装时，将以下内容打进同一文件夹即可：

- `dist/超群小帮手.exe`
- `config.example.json`
- `docs/用户使用说明.txt`（可复制为根目录 `请先看我.txt`）
- （可选）`docs/定时任务导入说明与示例.txt`

## 4. 确认清单（交付前）

- [ ] 在无 Python 的干净 Windows 上解压测试：能启动、能登录、能保存配置。
- [ ] 确认 `sessions`、`config.json` 随文件夹移动后仍可用。
- [ ] 提醒用户：不要把含 `sessions` 的文件夹公开上传。

## 5. 更新版本时

替换新的 `超群小帮手.exe`；用户 **保留** 自己的 `config.json` 与 `sessions` 即可延续使用。
