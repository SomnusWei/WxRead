# 📖 WxReadAssistant

> 基于 PySide6 的微信读书自动阅读桌面助手（Windows 平台）
> 通过 CDP (Chrome DevTools Protocol) 扫码登录，配合 Skill 1.0.5 API 获取书架和章节池

## 功能特性

### 🎯 核心能力

| 功能 | 说明 |
|------|------|
| **CDP 扫码登录** | 通过 Chrome DevTools Protocol 连接本地 Chrome 浏览器扫码登录，自动提取 Cookie |
| **Skill 1.0.5 API** | 对接微信读书 Skill 网关，获取书架、章节池、阅读统计等 |
| **书架书籍跳跃阅读** | 在书架中随机切换书籍，避免单一章节重复 |
| **章节池分桶存储** | 按 bookId 分桶存储章节 UID，避免跨书章节错配 |
| **每日目标持久化** | 当日首次启动随机取值（如 8-10h），写入 `config.json`，次日自动重取 |
| **Skill 统计** | 对接微信读书 Skill 1.0.5 网关，获取今日/本周/本月/总累计 4 项权威时长 |
| **混合完成度检测** | Skill 基线（每 5 分钟/10 次成功刷新）+ 本地累加双保险，精准判断达标 |
| **Cookie 有效性检测** | 简化为 wr_skey 长度检查（≥8 位即有效），不再强制校验 RK/ptcz |
| **风控规避** | 随机书籍/章节切换、±15% 间隔抖动、每 20 次插入长休息（60-180s） |
| **详细调试日志** | CDP 连接、Cookie 获取、书架拉取、章节池构建等关键节点全链路日志 |

### 🔧 CDP + Skill 技术栈

```
┌─────────────────────────────────────────────────────────────┐
│                    WxReadAssistant                          │
├─────────────────────────────────────────────────────────────┤
│  CDP 登录流程                                               │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ 1. 启动 Chrome (--remote-debugging-port=9223)         │  │
│  │ 2. 程序连接 CDP WebSocket                             │  │
│  │ 3. 用户在 Chrome 中扫码登录微信读书                   │  │
│  │ 4. 通过 Network.getCookies 获取 wr_* Cookie          │  │
│  │ 5. 自动调用 Skill API 获取书架和章节池                │  │
│  └───────────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│  Skill 1.0.5 API                                           │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ /shelf/sync → 获取书架（电子书+专辑+文章收藏）        │  │
│  │ /book/chapterinfo → 获取指定书籍的章节池              │  │
│  │ /book/getprogress → 获取阅读进度                      │  │
│  │ /readdata/detail → 获取阅读统计（日/周/月/年/总）     │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 📊 阅读统计 4 项

通过微信读书 Skill 1.0.5 网关获取官方统计数据：

- **今日阅读时长** — 当日累计秒数
- **本周累计时长** — 本周一至今累计
- **本月累计时长** — 本月累计
- **总累计时长** — 账号历史总阅读时长

### 🔔 推送通知

- **WxPusher** 推送：每日任务开始/完成、Cookie 失效提醒
- **Skill 登录成功推送**：CDP 登录成功后推送包含书架统计信息
- **测试消息**：设置页一键验证推送通道

### 🖥️ 桌面集成

- **系统托盘**：关闭窗口自动最小化到托盘后台运行
- **开机自启**：注册表免管理员写入
- **单实例**：QLocalSocket 确保全局唯一，二次启动自动激活
- **CDP 登录对话框**：独立弹窗进行扫码登录，不依赖内嵌浏览器

---

## 快速开始

### 环境要求

- **操作系统**：Windows 10/11（64 位）
- **Python**：≥ 3.10
- **Chrome 浏览器**：最新版（需支持 CDP）
- **依赖管理**：pip

### 安装 & 运行

```powershell
# 1. 克隆仓库
git clone https://github.com/your-repo/wxread.git
cd wxread

# 2. 创建虚拟环境
python -m venv .venv
.venv\Scripts\Activate.ps1

# 3. 安装依赖
pip install -r requirements.txt

# 4. 启动
python main.py
```

### 打包 EXE

```powershell
pip install pyinstaller

# 主程序
pyinstaller --noconfirm WxReadAssistant.spec

# CDP 测试程序
pyinstaller --noconfirm CDPStandaloneTest.spec
```

打包产物：
- 主程序：`dist/WxReadAssistant/WxReadAssistant.exe`
- 测试程序：`dist/CDPStandaloneTest.exe`

---

## 使用流程

### 第一步：配置 Skill API Key

1. 访问 [weread.qq.com/r/weread-skills](https://weread.qq.com/r/weread-skills) 申请 `wrk-` 开头的 API Key
2. 启动程序，进入 **「⚙️ 设置」** 标签
3. 在 Skill API Key 输入框中填入 API Key

### 第二步：CDP 扫码登录

1. 启动程序，点击 **「🔐 CDP 登录」** 按钮
2. 程序会自动打开 Chrome 浏览器（若未启动 CDP 模式会提示手动启动）
3. **手动启动 CDP 模式**（如自动启动失败）：
   ```powershell
   # Chrome CDP 模式启动
   chrome.exe --remote-debugging-port=9223 --user-data-dir=C:\Temp\ChromeCDP
   ```
4. 在 Chrome 中访问 `https://weread.qq.com` 并扫码登录
5. 登录成功后，点击 **「✅ 已登录完成」**，程序自动：
   - 通过 CDP 获取 `wr_*` Cookie
   - 调用 Skill `/shelf/sync` 获取书架
   - 调用 Skill `/book/chapterinfo` 获取章节池
   - 持久化存储所有数据
   - 拉取 Skill 阅读统计基线

### 第三步：配置阅读参数

切换到 **「⚙️ 设置」** 标签：

| 配置项 | 说明 |
|--------|------|
| 每日时长范围 | 如 8-10 小时，每日启动时在区间内随机一个精确时长 |
| 阅读间隔范围 | 默认 25-45 秒，每次请求间随机 |
| 换书频率 | 每 20-40 次阅读随机切换书籍 |
| Skill API Key | 从 [weread.qq.com/r/weread-skills](https://weread.qq.com/r/weread-skills) 申请 |
| WxPusher SPT | 从 [wxpusher.zjiecode.com](https://wxpusher.zjiecode.com) 申请 |
| 开机自启 | 勾选后写入注册表 |

### 第四步：开始阅读

切换到 **「🟢 状态」** 标签，点击 **「▶ 开始阅读」**。

程序会自动：
1. 检测 Cookie 有效性（wr_skey 长度 ≥ 8 即有效）
2. 从 Skill 网关获取今日基线时长
3. 在目标区间内随机生成今日目标
4. 从书架中随机选择书籍和章节
5. 启动阅读循环，按随机间隔上报
6. 完成目标后通过 WxPusher 推送完成通知

---

## 架构设计

```
┌─────────────────────────────────────────────────────────────┐
│                    WxReadAssistant                          │
├─────────────────────────────────────────────────────────────┤
│                          app/core                            │
│  ┌──────────┬──────────┬───────────┬──────────────────────┐│
│  │ config.py │weread_api│scheduler  │  notifier.py         ││
│  │ JSON 配置 │ 微信读书  │ 阅读调度  │ WxPusher 推送        ││
│  │ 持久化    │ API 封装  │ 随机节奏  │ 异步+重试            ││
│  └──────────┴──────────┴───────────┴──────────────────────┘│
├─────────────────────────────────────────────────────────────┤
│                          app/ui                              │
│  ┌──────────┬──────────┬───────────┬──────────────────────┐│
│  │main_window│status_page│cdp_login_dialog│settings_page    ││
│  │ 主窗口+  │ 状态+    │ CDP 登录   │ 设置+               ││
│  │ 托盘     │ 统计卡   │ 对话框     │ Skill 配置          ││
│  └──────────┴──────────┴───────────┴──────────────────────┘│
├─────────────────────────────────────────────────────────────┤
│                          CDP + Skill                         │
│  ┌───────────────────────┬──────────────────────────────────┐│
│  │  CDP Protocol         │  Skill 1.0.5 API                 ││
│  │  Network.getCookies  │  /shelf/sync                     ││
│  │  WebSocket 通信       │  /book/chapterinfo               ││
│  │                       │  /book/getprogress               ││
│  └───────────────────────┴──────────────────────────────────┘│
├─────────────────────────────────────────────────────────────┤
│                          app/utils                           │
│  ┌───────────────────┬──────────────────────────────────────┐│
│  │    logger.py      │     autostart.py                      ││
│  │  日志（文件+控制台） │  开机自启（注册表 Run）               ││
│  └───────────────────┴──────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

### CDP 数据流程

```
Chrome 浏览器 (weread.qq.com)
    │
    ├── CDP 连接
    │   └── WebSocket: ws://127.0.0.1:9223/devtools/page/{id}
    │
    ├── Cookie 获取
    │   └── Network.getCookies → 获取 wr_* 系列 Cookie
    │       → wr_skey, wr_vid, wr_rt 等
    │
    └── Skill API 调用
        ├── POST /api/agent/gateway (Bearer wrk-*)
        │   ├── /shelf/sync → 获取书架（365+ 本书籍）
        │   ├── /book/chapterinfo → 获取章节池（chapterUid 列表）
        │   └── /readdata/detail → 获取阅读统计
        │
        └── 数据持久化
            → config.json（Cookie、配置）
            → chapter_cache.json（章节池，按 bookId 分桶）
            → wxread-skill-cache.json（Skill 统计缓存）
```

### 完成度计算

```
# Skill 混合方案（主路径）
completed_sec = skill_baseline_sec + session_accum_sec

# 本地估算（降级：Skill 不可用时）
fallback_sec = started_minutes * 60

# 最终完成度
completed_minutes = max(completed_sec, fallback_sec) // 60

当 completed_minutes >= target_minutes → 停止阅读 + WxPusher 推送完成通知
```

**刷新周期**：
- 启动时立即拉取 Skill 网关基线（今日已读权威秒数）
- 每 5 分钟 或 每 10 次成功阅读 → 刷新 Skill 基线，`session_accum_sec` 归零

### 书籍跳跃阅读

```python
# 换书逻辑
每 20-40 次阅读 → 随机切换书籍
同一章节连续使用超过 5 次 → 强制切换

# 换章逻辑
从 chapter_pool[bookId] 中随机选择章节
优先选择 level >= 3 的正文章节
```

### Cookie 有效性检测

```
check_session()
  ├─ wr_skey 存在且长度 ≥ 8 → ✅ 有效
  └─ wr_skey 缺失或长度不足 → ❌ 失效 → 需重新 CDP 登录
```

**简化说明**：
- 微信读书 Web 端仅依赖 `wr_*` 系列 Cookie
- 不再强制校验 RK/ptcz（Web 端不下发这两个 Cookie）
- 简化为 wr_skey 长度检查

---

## 🔍 调试与日志

运行时日志位于 `%APPDATA%\WxReadAssistant\logs\app.log`，关键节点全链路打印：

### CDP 连接日志

```
[CDP] 尝试连接 CDP 端口 9223...
[CDP] 端口已就绪（第 1 次尝试）
[CDP] 获取到 1 个 targets
[CDP]   - type=page title=微信读书
[CDP] 连接 WebSocket: ws://127.0.0.1:9223/devtools/page/...
[CDP] ✅ 连接成功：页面标题=微信读书
```

### Skill API 日志

```
🔄 Skill /shelf/sync 请求: {}
🔄 Skill /shelf/sync 响应：HTTP=200 time=510ms
📚 Skill /shelf/sync 返回 365 本有效书籍

🔄 Skill /book/chapterinfo 请求: {'bookId': '842609'}
🔄 Skill /book/chapterinfo 响应：HTTP=200 time=140ms
📖 Skill /book/chapterinfo 返回 31 个章节
```

### 书架与章节池日志

```
📚 共提取到 365 本书籍：
  - 祈祷落幕时 (bookId=842609)
  - 金蚕往事全集（1—14）(bookId=35821223)
  ...

📖 书籍「祈祷落幕时」章节池：31 个章节
  - chapterUid=1, level=1, title=封面
  - chapterUid=33, level=1, title=1
  ...
```

---

## 目录结构

```
wxread/
├── main.py                     # 应用入口（单例+托盘）
├── requirements.txt            # Python 依赖
├── WxReadAssistant.spec        # PyInstaller 打包配置（主程序）
├── CDPStandaloneTest.spec      # PyInstaller 打包配置（测试程序）
├── cdp_test_standalone.py      # CDP 独立测试程序
├── README.md                   # 本文件
├── weread_skill_latest/        # Skill 1.0.5 文档
│   └── weread-skills/
│       ├── SKILL.md
│       ├── search.md
│       ├── book.md
│       ├── shelf.md
│       ├── readdata.md
│       ├── notes.md
│       ├── review.md
│       └── discover.md
├── app/
│   ├── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py           # 配置管理（APPDATA 持久化）
│   │   ├── weread_api.py       # 微信读书 API（Skill 1.0.5 + Cookie）
│   │   ├── scheduler.py        # 阅读调度（随机时长/间隔/完成检测）
│   │   └── notifier.py         # WxPusher 推送
│   ├── ui/
│   │   ├── __init__.py
│   │   ├── main_window.py      # 主窗口（状态页+设置页）
│   │   ├── status_page.py      # 状态页（进度+统计卡+日志）
│   │   ├── cdp_login_dialog.py # CDP 登录对话框（独立弹窗）
│   │   ├── settings_page.py    # 设置页（时长/推送/Skill/自启）
│   │   └── styles.py           # 全局 QSS
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── logger.py           # 日志（文件+UI 信号）
│   │   └── autostart.py        # 开机自启
│   └── resources/
│       └── app.ico             # 应用图标
└── dist/
    ├── WxReadAssistant/        # 主程序打包产物
    └── CDPStandaloneTest.exe   # 测试程序打包产物
```

---

## 配置说明

### 配置文件路径

所有运行时数据存储于 `%APPDATA%\WxReadAssistant\`：

| 文件 | 说明 |
|------|------|
| `config.json` | 用户配置（Cookie/Skill/推送/阅读参数等） |
| `chapter_cache.json` | 章节池缓存（按 bookId 分桶） |
| `wxread-skill-cache.json` | Skill 阅读统计缓存（TTL 180s） |
| `logs/app.log` | 运行日志（按天滚动，保留 14 天） |

### 关键配置项

```json
{
  "reading": {
    "min_hours": 8,
    "max_hours": 10,
    "min_interval_sec": 25,
    "max_interval_sec": 45,
    "switch_book_every_min": 20,
    "switch_book_every_max": 40
  },
  "skill": {
    "api_key": "wrk-xxxxxxxxxxxxxxxx",
    "version": "1.0.5",
    "summary_cache_ttl": 180
  },
  "push": {
    "wxpusher_spt": "SPT_xxxxxxxxxxxx",
    "notify_cookie_fail": true,
    "notify_daily_done": true,
    "notify_login_success": true
  },
  "app": {
    "auto_start": false,
    "start_minimized": false
  }
}
```

---

## 测试程序

`cdp_test_standalone.py` 是独立的 CDP 测试程序，用于调试和验证 CDP 连接、Cookie 获取、Skill API 调用等功能。

### 功能按钮

| 按钮 | 功能 |
|------|------|
| 🔌 连接 CDP | 连接本地 Chrome CDP 端口 |
| 🍪 获取 Cookie | 通过 CDP Network.getCookies 获取 Cookie |
| 📚 Skill 书架 | 调用 Skill `/shelf/sync` 获取书架 |
| 📖 Skill 章节池 | 调用 Skill `/book/chapterinfo` 获取章节池 |
| 🎯 DOM 提取书架 | 从页面 DOM 解析书籍链接 |
| 🎯 DOM 提取章节 | 从页面 DOM 解析章节信息 |
| 🔍 页面深度分析 | 分析页面结构、脚本、API 端点等 |
| 💾 保存数据 | 保存获取的所有数据到本地文件 |
| 🔄 恢复按钮 | 强制恢复所有按钮到可用状态 |

### 使用方法

```powershell
# 运行测试程序
python cdp_test_standalone.py

# 或运行打包后的 EXE
dist/CDPStandaloneTest.exe
```

测试流程：
1. 启动 Chrome CDP 模式
2. 点击 **「🔌 连接 CDP」**
3. 在 Skill API Key 输入框填入 `wrk-` 开头的 Key
4. 点击 **「📚 Skill 书架」** 获取书架
5. 点击 **「📖 Skill 章节池」** 获取章节池

---

## 风险说明

> ⚠️ **本项目仅供学习交流，自动化行为可能触发风控**

1. **频率风险**：过于频繁的请求可能导致账号被限制
2. **Cookie 过期**：`wr_skey` 有效期有限，需定期重新 CDP 登录
3. **IP 风险**：长时间固定 IP 可能触发风控，建议配合代理使用
4. **Skill Key**：`wrk-*` 开头的 API Key 需妥善保管，切勿提交到代码仓库
5. **Chrome 版本**：建议使用最新版 Chrome，确保 CDP 协议兼容性

---

## 技术栈

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | ≥ 3.10 | 主语言 |
| PySide6 | ≥ 6.7 | Qt 官方 Python 绑定 |
| Chrome | 最新版 | CDP 浏览器载体 |
| websocket-client | ≥ 1.6 | CDP WebSocket 通信 |
| requests | ≥ 2.32 | HTTP 请求（Skill API） |
| PyInstaller | ≥ 6.0 | EXE 打包 |

---

## 参考项目

- [findmover/wxread](https://github.com/findmover/wxread) — 微信读书刷时长脚本
- [微信读书 Skill](https://weread.qq.com/r/weread-skills) — 官方 Skill API（当前版本 1.0.5）
- [WeReadX](https://github.com/aprpure/wereadx) — 微信读书 Web 接口封装参考

## License

本项目仅供学习交流使用。
