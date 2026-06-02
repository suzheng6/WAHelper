# 超群小帮手（WAHelper）

WhatsApp + Telegram 整合桌面助手：多账号登录、通讯录、消息监听、定时文档任务、任务管理。

## 目录

| 目录 | 说明 |
|------|------|
| `wa_multi_listener/` | 主程序入口、WA 监听、整合壳 UI、打包脚本 |
| `tg_multi_listener/` | Telegram（Telethon）监听与定时任务 |

## 开发运行

```powershell
cd wa_multi_listener
python -m venv ..\.venv
..\.venv\Scripts\pip install -r requirements.txt
..\.venv\Scripts\python main.py
```

首次运行请在各平台目录下按 `config.example.json` 配置 `config.json`（勿将含密钥的配置提交到 Git）。

## 打包

仅在需要发布时执行（需用户确认）：

```powershell
cd wa_multi_listener
python scripts\package_windows.py
```

输出：`wa_multi_listener\dist\WAHelper\`

## 说明

- 会话、定时任务数据、代理与 API 密钥均保存在本地 `config.json` / `data/`，已加入 `.gitignore`。
- 仓库不包含 `dist/` 与 `userdata_preserve/`。
