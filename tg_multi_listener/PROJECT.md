# Telegram 多账号监听与提醒工具（桌面 UI）

## 一、项目概述

**项目名称**：Telegram 多账号监听与提醒工具（带桌面 UI）

**定位**：个人使用的本地自动化辅助（非群发、非营销），用于群管理、客户跟进提醒等场景。

**技术栈**：

| 分层 | 技术 |
|------|------|
| 运行环境 | Python 3.10+，本地长期运行 |
| Telegram | Telethon（登录、收发、监听） |
| 界面 | CustomTkinter / Tkinter（暗色极简主窗 + 顶置提醒弹窗） |
| 数据 | JSON 配置、本地 session、旋转文件日志 |

---

## 二、核心功能

1. **多账号登录**：支持多个 Telegram 账号并行连接（独立 session）。
2. **多群组监听**：可同时关注多个群/超级群组。
3. **每群绑定单一用户**：`watch_rules` 映射 `群 ID → 用户数字 ID`，仅在该用户发言时触发。
4. **消息触发提醒**：命中规则后弹出顶置窗口并记录日志。
5. **顶置弹窗**：`Always on top` + 抢占焦点 + 系统提示音（强提醒）。
6. **定时发送**：每天在设定时刻向多个群发文本（不按星期筛选），群发间带随机延迟；软件需持续运行。
7. **限流**：同一群内在时间窗内抑制重复弹窗（默认 10 秒，可配置）。
8. **日志**：文件日志 + 内存环形缓冲，供「日志中心 / 仪表盘」展示。

---

## 三、系统架构

```text
多账号（Telethon Client × N）
        ↓
NewMessage（入站）
        ↓
群 ID 过滤（watch_rules 含该 chat_id）
        ↓
发送者过滤（sender_id == 绑定用户）
        ↓
(群, 消息) 去重 & 按群限流
        ↓
主线程调度 → 顶置弹窗（GUI）
        ↓
日志 / 今日计数持久化
```

**运行流程简述**：后台 asyncio 线程维护多个 `TelegramClient`；每条入站消息经规则与限流后，通过 `after(0, …)` 投递到 Tk 主线程弹出 `AlertPopup`。定时任务在独立 asyncio 循环中按分钟轮询 `data/schedules.json`，到期后用指定账号顺序向各群 `send_message`，并在群之间插入随机等待。

---

## 四、项目结构（推荐）

```text
tg_multi_listener/
├── main.py                 # 入口
├── config.py               # 路径、配置读写、Account / AppConfig
├── listener.py             # 多账号监听、限流、去重
├── notifier.py             # 顶置提醒弹窗
├── scheduler.py            # 定时任务与随机延迟发送
├── logger_util.py          # 文件日志 + 内存日志
├── stats.py                # 仪表盘今日提醒计数
├── requirements.txt
├── config.example.json     # 配置样例（复制为 config.json）
├── PROJECT.md              # 本文档
├── scripts/
│   └── first_login.py      # 首次登录生成 session
├── ui/
│   ├── theme.py            # 色板与设计 token
│   └── app.py              # 主窗体与页面
├── sessions/               # *.session（勿提交仓库）
├── data/                   # schedules.json、stats.json
└── logs/                   # app.log
```

---

## 五、配置说明

### 1. 账号配置

| 字段 | 说明 |
|------|------|
| `api_id` / `api_hash` | 在 [my.telegram.org](https://my.telegram.org) 创建应用获取 |
| `session_name` | 对应 `sessions/<session_name>.session`，首次用 `scripts/first_login.py` 生成 |
| `enabled` | 是否参与连接与定时发送 |

### 2. 群组与 ID

- 超级群组/频道 ID 一般为负整数（如 `-100xxxxxxxxxx`）。可用监听日志、转发消息到 `@userinfobot` 或使用 Telethon 脚本解析。
- 多群：在 `watch_rules` 中逐项配置。

### 3. 监听规则（核心）

**每个群绑定一个用户**（数值型 Telegram user id）：

```python
WATCH_RULES = {
    -1001111111111: 123456789,
    -1002222222222: 987654321,
}
```

配置文件中等价为字符串键（JSON 不支持注释）：

```json
"watch_rules": {
  "-1001111111111": 123456789,
  "-1002222222222": 987654321
}
```

---

## 六、提醒机制

### 1. 弹窗

- `attributes('-topmost', True)` 置顶。
- `grab_set()` + `focus_force()` + `bell()` 提高打断级别。
- 显示：**群名称**、**发送者**、**正文**；按钮 **查看**（打开 t.me 或 Web）、**关闭**。
- 进入时 **alpha 渐显**（约 20ms 步进）。

### 2. 限流（按群）

- **原因**：同一用户短时间连发、多账号重复投递同一条消息时，避免弹窗轰炸与重复处理。
- **策略**：同一 `chat_id` 在 `rate_limit_seconds` 内仅允许一次通过限流到达弹窗（另含跨账号的 `(chat_id, message_id)` 短时去重）。

---

## 七、定时任务

- 任务存于 `data/schedules.json`，由 UI 添加/启停/删除。
- **规则**：每自然日、在 `hour:minute` 整分最多触发一次（无「星期」筛选）；应用需保持运行。
- **字段**：`hour` / `minute`（24 小时制）、`chat_ids`、`content`、`account_id`、`jitter_seconds`。
- **发送**：使用指定账号连接后 **逐群发送**；每群前增加 **\[0.5 , 0.5 + jitter\] 秒** 均匀随机延迟，降低瞬时批量行为风险。

---

## 八、UI 设计规范

### 1. 风格关键词

暗色背景、极简布局、参考 Notion / Linear 的信息密度与层次（本实现为工程向实用界面）。

### 2. 主题色

| Token | 色值 |
|--------|------|
| 背景 | `#0f1115` |
| 卡片 | `#1a1d24` |
| 边框 | `#2a2f3a` |
| 文字主色 | `#e6eaf2` |
| 文字次要 | `#9aa3b2` |
| 强调 | `#4f8cff` |

### 3. 布局

```text
侧边栏（导航，固定宽度）
   ↓
主内容区（滚动页面）
```

**侧栏**：仪表盘 · 账号管理 · 群组绑定 · 监听规则 · 定时任务 · 日志中心 · **保存并重载服务**

### 4. 页面职责

| 页面 | 内容要点 |
|------|-----------|
| 仪表盘 | 今日提醒次数、监听是否运行、账号摘要、最近日志片段 |
| 账号管理 | 添加/删除账号、勾选启停 |
| 群组绑定 | 维护 `watch_rules` |
| 监听规则 | 总开关、按群限流秒数 |
| 定时任务 | 列表 + 表单添加；每日时刻触发 |
| 日志中心 | 内存日志流 + 刷新 |

### 5. 弹窗结构

```text
群名称
用户
消息正文（可滚动）

[ 查看 ] [ 关闭 ]
```

### 6. 交互

- 按钮 **hover**：通过 CustomTkinter 的 `hover_color` 区分主/次按钮。
- 弹窗 **淡入**：`alpha` 自 0 → 1。
- **状态反馈**：关键操作写入日志行（界面不另建 Toast，避免重叠）。

---

## 九、使用方法

1. **获取 API**：登录 [my.telegram.org](https://my.telegram.org)，创建应用，记录 `api_id` / `api_hash`。
2. **安装依赖**：`pip install -r requirements.txt`
3. **配置文件**：复制 `config.example.json` 为 `config.json` 并修改。
4. **首次登录**：  
   `python scripts/first_login.py --api-id <ID> --api-hash <HASH> --session <名称>`  
   按终端提示完成验证码/二次验证。
5. **启动**：在项目根目录执行  
   `python main.py`
6. 在 UI 中完善规则后点击 **保存并重载服务**。

---

## 十、注意事项

- 避免 **高频群发**与短时间大量操作，以降低账号风控风险。
- **session 文件**等同于登录凭证，请限制文件权限，勿泄露或上传。
- 定时发送带随机延迟仅为降低异常模式风险，**不能**替代对 Telegram ToS 的合规使用。
- 本工具仅供 **个人管理用途**，请勿用于垃圾消息或骚扰。

---

## 十一、扩展方向（可选）

- 可视化导入/导出配置、加密存储敏感字段。
- 单群多用户、关键字、正则等高级规则。
- 统一「消息中心」聚合提醒历史与已读状态。
- 其他打包器（cx_Freeze 等）可自行接入 `paths.app_root()` 逻辑。

---

## 十二、分发与打包（Windows exe）

- **持久化路径**：打包后使用 `paths.app_root()`（exe 所在目录），`config.json`、`sessions`、`logs`、`data` 与用户数据与 exe **同文件夹**，解压即可用、整夹拷贝即可迁移。
- **打包步骤**：见 `docs/BUILD_打包说明.md`（`python -m PyInstaller --noconfirm build_windows.spec`）。
- **一键打 zip**：`powershell -ExecutionPolicy Bypass -File scripts/package_release.ps1`
- **给最终用户**：附带 `docs/用户使用说明.txt`（可复制为「请先看我.txt」）。

---

## 许可证与免责

本项目按「原样」提供；使用 Telegram API 时请遵守 Telegram 用户协议与当地法律法规。
