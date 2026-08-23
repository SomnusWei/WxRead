# 📖 WxReadAssistant

> 基于 PySide6 的微信读书自动阅读桌面助手（Windows 平台）

## 功能特性

### 🎯 核心能力

| 功能 | 说明 |
|------|------|
| **扫码登录** | 内嵌浏览器扫码登录，自动提取 Cookie 与 Headers 持久化保存 |
| **智能阅读** | 每日目标随机生成（如 9h47m），阅读间隔 25-45s 随机 ±15% 抖动 |
| **章节池** | 内置 `chapter_cache.json`，按 Cookie 指纹分桶存储，Cookie 失效自动重建 |
| **Skill 统计** | 对接微信读书 Skill 1.0.4 网关，获取今日/本周/本月/总累计 4 项权威时长 |
| **混合完成度** | Skill 基线 + 本地估算双保险，准确判断今日目标完成状态 |
| **自动续期** | `wr_skey` 过期自动调用 `/web/login/renewal` 续命 |
| **风控规避** | 随机书籍/章节切换、±15% 间隔抖动、每 20 次插入长休息（60-180s） |

### 📊 阅读统计 4 项

通过微信读书 Skill 网关获取官方统计数据：

- **今日阅读时长** — 当日累计秒数
- **本周累计时长** — 本周一至今累计
- **本月累计时长** — 本月累计
- **总累计时长** — 账号历史总阅读时长

### 🔔 推送通知

- **WxPusher** 推送：每日任务开始/完成、Cookie 失效提醒
- **4 小时去重**：防止 Cookie 失效时重复轰炸
- **测试消息**：设置页一键验证推送通道

### 🖥️ 桌面集成

- **系统托盘**：关闭窗口自动最小化到托盘后台运行
- **开机自启**：注册表免管理员写入
- **单实例**：QLocalSocket 确保全局唯一，二次启动自动激活
- **启动入托盘**：配合开机自启，后台静默运行

---

## 快速开始

### 环境要求

- **操作系统**：Windows 10/11（64 位）
- **Python**：≥ 3.10
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
pyinstaller --noconfirm WxReadAssistant.spec
```

打包产物位于 `dist/WxReadAssistant/WxReadAssistant.exe`。

---

## 使用流程

### 第一步：扫码登录

1. 启动程序，切换到 **「🔐 扫码登录」** 标签
2. 内嵌浏览器自动打开 `weread.qq.com`
3. 手机微信扫码登录，在浏览器中打开任意一本书（如《三体》）
4. 点击 **「✅ 我已登录完成」**，程序自动：
   - 提取 Cookie（`wr_skey`、`RK`、`ptcz`、`pac_uid` 等）
   - 运行 JS 注入劫持 `fetch`/`XMLHttpRequest` 捕获阅读请求
   - 刷新章节池（`chapter_cache.json`）
   - 拉取 Skill 阅读统计基线

### 第二步：配置参数

切换到 **「⚙️ 设置」** 标签：

| 配置项 | 说明 |
|--------|------|
| 每日时长范围 | 如 8-10 小时，每日启动时在区间内随机一个精确时长 |
| 阅读间隔范围 | 默认 25-45 秒，每次请求间随机 |
| Skill API Key | 从 [weread.qq.com/r/weread-skills](https://weread.qq.com/r/weread-skills) 申请 |
| WxPusher SPT | 从 [wxpusher.zjiecode.com](https://wxpusher.zjiecode.com) 申请 |
| 开机自启 | 勾选后写入注册表 |
| 启动入托盘 | 配合开机自启使用 |

### 第三步：开始阅读

切换到 **「🟢 状态」** 标签，点击 **「▶ 开始阅读」**。

程序会自动：
1. 检测登录态有效性
2. 从 Skill 网关获取今日基线时长
3. 在目标区间内随机生成今日目标
4. 启动阅读循环，按随机间隔上报
5. 完成目标后通过 WxPusher 推送完成通知

---

## 架构设计

```
┌─────────────────────────────────────────────────────┐
│                    WxReadAssistant                    │
├─────────────┬───────────────────┬───────────────────┤
│   main.py   │   PySide6 / Qt     │   app/resources   │
│  应用入口    │  GUI 框架 + WebEngine │   图标资源       │
├─────────────┴───────────────────┴───────────────────┤
│                      app/core                         │
│  ┌──────────┬──────────┬───────────┬────────────────┐ │
│  │ config.py │weread_api│scheduler  │  notifier.py   │ │
│  │ JSON 配置 │ 微信读书  │ 阅读调度  │ WxPusher 推送  │ │
│  │ 持久化    │ API 封装  │ 随机节奏  │ 异步+重试      │ │
│  └──────────┴──────────┴───────────┴────────────────┘ │
├───────────────────────────────────────────────────────┤
│                      app/ui                           │
│  ┌──────────┬──────────┬───────────┬────────────────┐ │
│  │main_window│status_page│login_page │ settings_page   │ │
│  │ 主窗口+  │ 状态+    │ 扫码+     │ 设置+          │ │
│  │ 托盘     │ 统计卡   │ JS 劫持   │ Skill 配置      │ │
│  └──────────┴──────────┴───────────┴────────────────┘ │
├───────────────────────────────────────────────────────┤
│                      app/utils                        │
│  ┌───────────────────┬──────────────────────────────┐ │
│  │    logger.py      │     autostart.py              │ │
│  │  日志（文件+控制台） │  开机自启（注册表 Run）       │ │
│  └───────────────────┴──────────────────────────────┘ │
└───────────────────────────────────────────────────────┘
```

### 阅读数据流程

```
内嵌浏览器 (weread.qq.com/web/reader)
    │
    ├── JS 注入 → 劫持 fetch / XMLHttpRequest
    │   └── 捕获 POST /web/book/read 请求
    │       → 提取 b(book hex)、c(chapter hex)、ct、rt 等
    │
    ├── 章节池获取
    │   └── POST /web/book/chapterInfos (Cookie 鉴权)
    │       → 持久化到 chapter_cache.json
    │       → cookie_fingerprint 绑定生命周期
    │
    └── Skill 统计基线
        └── POST /api/agent/gateway (Bearer wrk-*)
            mode=overall / weekly / monthly
            → 3 次请求 + 700ms 间隔
            → readTimes 分桶累加
            → 提取今日/本周/本月/总累计
```

### 完成度计算

```
completed_minutes = max(
    skill_baseline_sec + session_accum_sec,  # Skill 混合方案
    started_minutes  # 本地估算（降级）
)

当 completed_minutes >= target_minutes → 停止阅读
```

### 签名算法

```
sg = SHA256(ts + rn + KEY)    # KEY = "3c5c8717f3daf09iop3423zafeqoi"
s  = cal_hash(encode_data(payload))  # FNV-1a 变体哈希
```

---

## 目录结构

```
wxread/
├── main.py                     # 应用入口（单例+托盘）
├── requirements.txt            # Python 依赖
├── WxReadAssistant.spec        # PyInstaller 打包配置
├── README.md                   # 本文件
├── app/
│   ├── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py           # 配置管理（APPDATA 持久化）
│   │   ├── weread_api.py       # 微信读书 API（签名/续期/阅读上报）
│   │   ├── scheduler.py        # 阅读调度（随机时长/间隔/完成检测）
│   │   └── notifier.py         # WxPusher 推送
│   ├── ui/
│   │   ├── __init__.py
│   │   ├── main_window.py      # 主窗口（三标签页+系统托盘）
│   │   ├── status_page.py      # 状态页（进度+统计卡+日志）
│   │   ├── login_page.py       # 登录页（内嵌浏览器+JS劫持）
│   │   ├── settings_page.py    # 设置页（时长/推送/Skill/自启）
│   │   └── styles.py           # 全局 QSS
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── logger.py           # 日志（文件+UI 信号）
│   │   └── autostart.py       # 开机自启
│   └── resources/
│       └── app.ico             # 应用图标
└── _weread_skills_104/         # Skill 1.0.4 文档
    └── weread-skills/
        ├── SKILL.md
        ├── readdata.md
        ├── shelf.md
        └── ...
```

---

## 配置说明

### 配置文件路径

所有运行时数据存储于 `%APPDATA%\WxReadAssistant\`：

| 文件 | 说明 |
|------|------|
| `config.json` | 用户配置（时长/间隔/Skill/推送等） |
| `chapter_cache.json` | 章节池缓存（Cookie 指纹绑定） |
| `logs/app.log` | 运行日志（按天滚动，保留 14 天） |
| `wxread-login-profile/` | Qt WebEngine 浏览器数据 |

### 关键配置项

```json
{
  "reading": {
    "min_hours": 8,
    "max_hours": 10,
    "min_interval_sec": 25,
    "max_interval_sec": 45
  },
  "skill": {
    "api_key": "wrk-xxxxxxxxxxxxxxxx",
    "version": "1.0.4",
    "summary_cache_ttl": 180
  },
  "push": {
    "wxpusher_spt": "SPT_xxxxxxxxxxxx",
    "notify_cookie_fail": true,
    "notify_daily_done": true
  },
  "app": {
    "auto_start": false,
    "start_minimized": false
  }
}
```

---

## 风险说明

> ⚠️ **本项目仅供学习交流，自动化行为可能触发风控**

1. **签名风险**：微信读书可能更新 `sg`/`s` 签名算法，届时需同步更新
2. **频率风险**：过于频繁的请求可能导致账号被限制
3. **Cookie 过期**：`wr_skey` 有效期有限，需配合自动续期
4. **IP 风险**：长时间固定 IP 可能触发风控，建议配合代理使用
5. **Skill Key**：`wrk-*` 开头的 API Key 需妥善保管，切勿提交到代码仓库

---

## 技术栈

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | ≥ 3.10 | 主语言 |
| PySide6 | ≥ 6.7 | Qt 官方 Python 绑定 |
| QtWebEngine | ≥ 6.7 | 内嵌浏览器（Chromium 内核） |
| requests | ≥ 2.32 | HTTP 请求 |
| PyInstaller | ≥ 6.0 | EXE 打包 |

---

## 参考项目

- [findmover/wxread](https://github.com/findmover/wxread) — 微信读书刷时长脚本（核心签名算法来源）
- [微信读书 Skill 1.0.4](https://weread.qq.com/r/weread-skills) — 官方 Skill API 文档

## License

本项目仅供学习交流使用。
