# 📖 WxReadAssistant

> 基于 PySide6 的微信读书自动阅读桌面助手（Windows 平台）
> CDP 扫码登录 + Skill API 拉取书架/章节池 + 本地数据库驱动进度阅读
>
> 🎯 **最新版本 v2.2.6**：[前往 GitHub Releases 下载](https://github.com/SomnusWei/WxRead/releases/latest)

## 功能特性

### 🎯 核心能力

| 功能 | 说明 |
|------|------|
| **CDP 扫码登录** | 通过 Chrome DevTools Protocol 连接本地 Chrome 浏览器扫码登录，自动提取并持久化 Cookie（`wr_skey` 长度 ≥ 8 即视为有效） |
| **Skill API 集成** | 独立 `skill_api` 模块对接微信读书 Skill 网关，获取书架、章节池、阅读进度、阅读统计 |
| **本地数据库兜底** | `local_db.json` 持久化书架/进度/统计，`chapter_cache.json` 按 bookId 分桶存储章节池；Skill 不可用时本地继续驱动阅读 |
| **进度驱动选书选章** | 按进度排序优先选进度最高且 <100% 的书；单章阅读 4~5 次后换章；100% 换书；跳过封面/版权页（uid 1~33） |
| **混合完成度检测** | 本地实时累加阅读秒数 + 每 30 分钟 Skill 覆盖校正双保险，精准判断当日达标 |
| **阅读统计 4 项** | 通过 `/readdata/detail` 获取今日 / 本周 / 本月 / 总累计 4 项权威时长 |
| **可视化报告** | 聚合 LocalDB + Skill 数据，弹出报告页：读者画像 / 书架归一化饼图（进度+可见性双维度）/ 阅读趋势线 / 月度热力图 / 20 条阅读高光，纯 QPainter 绘制无额外依赖 |
| **风控规避** | 随机书籍/章节切换、±15% 间隔抖动、每 N 次成功插入长休息、动态 `baggage`/`sentry-trace` 请求头 |
| **WxPusher 推送** | 每日任务开始/完成、Cookie 失效、Skill 登录成功等事件推送（消息中时长以「X小时Y分钟」呈现） |
| **开机自启 + 托盘** | 注册表写入自启项，支持 `--minimized` 启动即最小化到托盘，单例防多开 |
| **异步 UI** | Skill 调用 / CDP 登录 / 书架拉取全部走 QThread，UI 不阻塞 |

### 📊 阅读统计 4 项

通过微信读书 Skill 网关获取官方统计数据：

- **今日阅读时长** — 当日累计秒数
- **本周累计时长** — 本周一至今累计
- **本月累计时长** — 本月累计
- **总累计时长** — 账号历史总阅读时长

### 🔑 授权机制

- 首次安装后获得 **12 小时免费试用**（时间戳双备份防重置）。
- 试用期结束后需输入 16 位注册码激活，激活方式：`⚙️ 配置中心 → 🔑 授权管理`。
- 注册码在本地校验，无需联网；激活后**永久使用**全部功能。
- 注册码不绑定设备，同一注册码可在多台机器激活。

> 🔐 **获取注册码请联系开发者**
> - 📱 QQ：37784552
> - 📧 Email：somnusweiwei1989@outlook.com

## 技术架构

```
┌──────────────────────────────────────────────────────────────┐
│                    WxReadAssistant                           │
├──────────────────────────────────────────────────────────────┤
│                         UI 层                                 │
│  ┌──────────────────┬──────────────────────────────────────┐ │
│  │   settings_page   │          main_page（主界面）          │ │
│  │  - 时长/间隔范围  │  ┌─────────────┬──────────────────┐ │ │
│  │  - 启动延迟       │  │  上左：状态  │  上右：操作+统计  │ │ │
│  │  - 长休息/失败冷却│  │  + 控制按钮  │  - 阅读统计卡     │ │ │
│  │  - Skill Key      │  └─────────────┤  - CDP 登录       │ │ │
│  │  - WxPusher       │  ┌─────────────┤  - 查看/获取书架  │ │ │
│  │  - 系统设置       │  │  下：工作日志 │  - 获取章节池     │ │ │
│  │  - 🔑 授权管理     │  └─────────────┴──────────────────┘ │ │
│  └──────────────────┘  └──────────────────────────────────┘ │
├──────────────────────────────────────────────────────────────┤
│                       业务逻辑层                              │
│  ┌───────────┬──────────────┬────────────┬─────────────────┐ │
│  │ scheduler  │ weread_api   │ skill_api   │  licensing     │ │
│  │ 阅读调度   │ 阅读上报     │ Skill 调用  │  注册码/试用期  │ │
│  │ 进度管理   │ 签名算法     │ 书架/章节   │  纯本地校验     │ │
│  │ 完成检测   │ 续期         │ 进度/统计   │  12h 免费试用   │ │
│  └───────────┴──────────────┴────────────┴─────────────────┘ │
├──────────────────────────────────────────────────────────────┤
│                       数据持久化层                            │
│  ┌──────────────┬───────────────┬──────────────────────────┐ │
│  │ config.json   │ local_db.json  │  chapter_cache.json     │ │
│  │ 用户配置      │ 书架+进度+统计 │  章节池（按 bookId 分桶）│ │
│  │               │ +授权状态（哈希，无明文）                 │ │
│  └──────────────┴───────────────┴──────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

### 核心工作流

```
启动 → 单例检测 → 加载样式/日志 → 构造主窗口 → 授权检查（过期弹激活窗）
       ↓
调度循环：
  1. 检测登录态（wr_skey 有效性） + 授权门禁（试用过期禁止开始阅读）
  2. 启动随机延迟（默认 15~25s，可配置）
  3. 健康检查 + 完成度检查
  4. 选书（进度最高且 <100%）→ 选章（跳过封面/版权页）
  5. read_once 单次阅读上报（30~45s ± 15% 抖动）
  6. 本地累加进度与今日阅读秒数，触发 UI 更新
  7. 计算下次间隔（成功/失败/长休息不同策略）
  8. 每 30 分钟用 Skill 覆盖校正进度与统计
```

### CDP 登录流程

1. 启动 Chrome（`--remote-debugging-port=9223`）
2. 程序连接 CDP WebSocket
3. 用户在 Chrome 中扫码登录微信读书
4. 通过 `Network.getCookies` 获取 `wr_*` Cookie（按名称前缀白名单过滤）
5. 登录成功后保持 WebSocket 活跃，直到书架与章节池拉取完成
6. Cookie 持久化，后续请求动态生成 `baggage`/`sentry-trace` 头绕过风控

### Skill API 端点

| 端点 | 用途 |
|------|------|
| `/shelf/sync` | 获取书架（电子书 + 专辑 + 文章收藏） |
| `/book/chapterinfo` | 获取指定书籍的章节池 |
| `/book/getprogress` | 获取阅读进度 |
| `/readdata/detail` | 获取阅读统计（日 / 周 / 月 / 总） |
| `/weread/report` | 获取微信读书报告聚合数据（书架构成 / 阅读高光等，4xx 时自动熔断退回本地） |

## 项目结构

```
WxRead/
├── app/
│   ├── core/
│   │   ├── config.py          # 全局配置（%APPDATA%/WxReadAssistant/config.json）
│   │   ├── licensing.py       # 注册码生成/校验 + 12h 试用期 + 激活持久化（纯本地）
│   │   ├── scheduler.py       # 进度驱动阅读调度器
│   │   ├── weread_api.py      # 微信读书阅读上报 + 签名算法
│   │   ├── skill_api.py       # Skill 网关调用（书架/章节/进度/统计/报告）
│   │   ├── report_aggregator.py # 报告数据聚合（LocalDB + Skill → 16 字段统一结构）
│   │   ├── local_db.py        # 本地数据库（书架/进度/章节池/授权段持久化）
│   │   └── notifier.py        # WxPusher 推送（format_duration: X小时Y分钟）
│   ├── ui/
│   │   ├── main_page.py       # 主界面（状态+操作+统计+日志单页布局）
│   │   ├── main_window.py     # 主窗口（组装各模块 + 托盘 + 启动授权检查）
│   │   ├── settings_page.py   # 设置页（时长/间隔/Skill Key/推送/系统/授权管理）
│   │   ├── shelf_dialog.py    # 书架查看对话框
│   │   ├── report_dialog.py   # 可视化报告对话框（QPainter 图表 · 纯绘制无依赖）
│   │   ├── cdp_login_dialog.py# CDP 扫码登录对话框
│   │   ├── license_dialog.py  # 注册码激活对话框（启动过期时弹出）
│   │   ├── icon_store.py      # 运行时图标（含 EXE 内嵌图标 资源源）
│   │   └── styles.py          # 全局 QSS 样式
│   └── utils/
│       ├── logger.py          # 日志（APPDATA 14 天 + 项目根 3 天）
│       └── autostart.py       # 开机自启（注册表 HKCU\...\Run）
├── installer/                 # Inno Setup 安装包脚本（wxread_assistant.iss + 中文语言包）
├── assets/app_icon.ico        # EXE/安装器内嵌图标（scripts/make_icon.py 渲染生成）
├── main.py                    # 入口（单例 + --minimized）
├── WxReadAssistant.spec       # PyInstaller 打包配置（已指定 icon）
├── requirements.txt
├── scripts/
│   ├── build_release.py       # 一键构建：full.zip + setup.exe + 增量补丁 + release_note
│   └── make_icon.py           # 从 icon_store 渲染合成多尺寸 app_icon.ico
└── release/                   # 发布区（full 目录 / full.zip / setup.exe / patch / sha1 / release_note）
```

## 快速开始

### 环境要求

- Windows 10/11（x64）
- 本地 Chrome 浏览器（CDP 扫码登录需要）

### 🚢 下载安装（正式用户）

所有正式二进制见 **[GitHub Releases](https://github.com/SomnusWei/WxRead/releases)**。

推荐方式（新手）：**下载 `WxReadAssistant-vX.Y.Z-setup.exe`**，双击安装向导完成安装（自动创建开始菜单/桌面快捷方式，自带卸载器）。

免安装备用：解压 `WxReadAssistant-vX.Y.Z-full.zip`，进入 `WxReadAssistant\` 目录双击 EXE 启动。

> 大二进制不进入 git 历史（`.gitignore` 已过滤），请通过 Releases 下载。

### 首次使用

1. 主页 → 「📷 扫码登录」按提示使用 Chrome CDP 扫码获取 Cookie。
2. 主页 → 「获取数据」拉取书架与章节池。
3. （可选）进入「⚙️ 配置中心」填入 **Skill API Key** / **WxPusher Token**。
4. （可选）试用期结束后，「⚙️ 配置中心 → 🔑 授权管理」输入注册码激活。
5. 点「开始阅读」。

> 🔐 获取注册码：QQ 37784552 或 Email somnusweiwei1989@outlook.com

### 从旧版本升级

#### 方式 A：重新运行最新 setup.exe（推荐）

安装器使用与之前相同的 AppId，会**原地升级**到安装目录，不触碰 `%APPDATA%` 下的用户数据（Cookie / 配置 / 进度 / 日志）。

#### 方式 B：增量补丁（`patch-vX.Y.Z-from-vA.B.C.zip`）

1. 退出 WxReadAssistant（含托盘图标 → 右键 → 退出）。
2. 下载对应版本的补丁 zip。
3. 解压到安装目录的**父级目录**（保证解压后 `apply_patch.bat` 与 `WxReadAssistant\` 同一层）。
4. 双击 `apply_patch.bat`。
5. `%APPDATA%\WxReadAssistant\` 下的用户数据完全不受影响。

### 源码运行 / 开发者构建

```bash
pip install -r requirements.txt
python main.py                       # 直接运行
python main.py --minimized           # 启动即最小化到托盘

# 打包 EXE + setup.exe + 增量补丁
python scripts\build_release.py --version 2.2.6 --previous 2.2.5
```

> 依赖：PySide6（含 QtWebEngine）、requests、urllib3、websocket-client、Pillow、pyinstaller。

## 配置说明

配置文件位于 `%APPDATA%\WxReadAssistant\config.json`，可在「设置」页可视化编辑，主要项：

| 配置组 | 项 | 默认值 |
|--------|----|--------|
| 阅读参数 | 每日目标时长范围 | 8~10 小时 |
| 阅读参数 | 单次阅读间隔 | 30~45 秒（±15% 抖动） |
| 阅读参数 | 启动延迟范围 | 15~25 秒 |
| 阅读参数 | 长休息 | 每 20 次休息 60~180 秒 |
| 阅读参数 | 失败冷却范围 | 30~60 秒 |
| 阅读参数 | Skill 数据刷新间隔 | 30 分钟 |
| Skill | API Key | （在设置页填入） |
| 推送 | WxPusher 开关 / Token | 可选 |
| 系统 | 开机自启 / 最小化到托盘 | 可选 |
| 🔑 授权 | 注册码 | 联系开发者获取（QQ 37784552 / Email somnusweiwei1989@outlook.com） |

> 各设置的 inline 提示位于「⚙️ 配置中心」对应组框底部，建议首次使用先浏览。

## 关键设计

- **进度驱动选书**：优先选进度最高且 <100% 的书，100% 换书，避免单一章节重复触发风控。
- **本地兜底**：Skill 不可用时由 `local_db.json` 继续驱动阅读循环，保证连续运行。
- **混合完成度**：本地实时累加秒数（每 30~45 秒 +45s）+ 每 30 分钟 Skill 覆盖，双保险判定当日达标。
- **风控规避**：随机书籍/章节切换、间隔抖动、长休息、动态 `baggage`/`sentry-trace` 请求头匹配浏览器指纹。
- **Cookie 安全**：CDP 抓取按名称前缀白名单过滤，`wr_skey` 全量存储不截断，PySide6 6.11+ SameSite 枚举序列化时转 `.value`。
- **授权安全（纯本地）**：注册码算法使用 HMAC-SHA256 截断 40-bit，本地恒时比对；首次启动时间戳在 `local_db.json` 与注册表双备份取最早值防删除重置；激活只存代码哈希，不存明文。
- **日志双写**：APPDATA（14 天保留，便于长期追溯）+ 项目根（3 天保留，便于即时排查）。

## CHANGELOG

### v2.2.6 — 2026-08-28

#### 🔑 本地授权系统（新功能）
- 新增 `app/core/licensing.py`：
  - 16 位注册码（格式 `XXXX-XXXX-XXXX-XXXX`，Crockford Base32 + HMAC-SHA256 截断），**全程零网络校验**。
  - 12 小时免费试用期：首次启动时间戳双备份（`local_db.json` + HKCU 注册表），取最早值防删除重置。
  - 激活持久化：仅存注册码哈希，不存原文；`LocalDB.clear_all()` 保留授权段（清 Cookie 不掉激活）。
  - 易混字符自动归一（O→0 / I、L→1）。
- 新增 `app/ui/license_dialog.py`：启动 0.8s 后若试用过期，弹出激活对话框（支持「稍后再说」；激活成功自动关闭）。
- 「⚙️ 配置中心」新增「🔑 授权管理」卡片：实时显示剩余试用期 / 已激活 / 已过期状态 + 注册码输入与激活。
- 门禁：过期后点「开始阅读」弹激活提示，跳转配置中心完成激活后再开始阅读。
- 注册码不绑定设备；**获取注册码请联系开发者**：QQ 37784552 / Email somnusweiwei1989@outlook.com。

#### 📦 发布形态升级（Windows 安装包）
- 新增 Inno Setup 6 安装包管线：
  - `installer/wxread_assistant.iss` 安装脚本（简体中文向导，`ArchitecturesAllowed=x64compatible`，卸载器不动用户数据）。
  - `scripts/build_release.py` 新增第 7 步自动调用 ISCC，未安装 Inno Setup 时优雅跳过；新增安装器图标。
  - 新增 `installer/Languages/ChineseSimplified.isl`（kira-96 翻译，Inno Setup 6.5.0+ 兼容）。
- 产物新增 `WxReadAssistant-vX.Y.Z-setup.exe`（≈169 MB），推荐新手直接下载双击安装。
- 同时保留 `full.zip`（免安装）与 `patch.zip`（增量升级，≈6 MB 小体积）。

#### 🎨 应用图标对齐（EXE/安装器/任务栏 同源）
- 新增 `scripts/make_icon.py`：基于运行时 `icon_store.make_app_icon()` 离屏渲染 16~256 共 7 个尺寸，Pillow 合成多尺寸 `assets/app_icon.ico`。
- `WxReadAssistant.spec` 绑定 `icon='assets/app_icon.ico'`；`SetupIconFile` 指向同一 ico。
- 顺手修复 icon_store 既有 bug：封面白字斜体 W 之前因 `setPen(NoPen)` 而不可见，现已改为白色笔，主视觉真正呈现。

#### 📨 WxPusher 消息时长格式优化
- `app/core/notifier.py` 新增 `format_duration()`：推送消息中的时长改为人类可读形式。
  - 例：`8小时12分钟`（原 `492 分钟`），480 分钟→`8小时`，12 分钟→`12分钟`，0→`0分钟`。

---

### v2.2.5 — 2026-08-28
- **v2.2.4 基础上新增**：原生应用图标对齐（EXE/安装器/快捷方式）+ 封面白字 W 主视觉修复。

### v2.2.4 — 2026-08-27
- 可视化报告页面 + stop/start 重入修复 + Skill 统计刷新 KeyError 修复 + 扫码登录主窗口闪烁修复。

### v2.2.3 — 2026-08-26
- 修复「重置今日目标」按钮功能错误。

### v2.2.2 — 2026-08-26
- 配置中心新增「重置今日目标」按钮。

### v2.2.1 — 2026-08-25
- 修复跨天后 `today_seconds` 未重置导致的「误判完成」bug。

### v2.2.0 — 2026-08-25
- 主界面（米白纸 Paper Studio）+ 配置中心大改版 + 版本可见性胶囊 + 发布管线 + 增量补丁机制。

### v2.1.0 — 2026-07
- 历史基线：米白纸 Paper Studio + 三列 3:4:4 布局 + 配色令牌体系。

## 版本 & 校验

- **版本号源**：`app/ui/icon_store.py::APP_VERSION`（打包脚本会自动改写）
- **Build ID**：`app/ui/icon_store.py::APP_BUILD_ID` = `<git short sha> · <YYYYMMDD>`
- **显示位置**：窗口标题 / 托盘 ToolTip / 主界面日志区右下胶囊 / 「⚙️ 配置中心」标题旁小胶囊 / 启动日志 banner。
- **一致性校验**：Releases 页每个二进制附带对应 sha1 清单。

## 🔐 获取注册码

注册码仅由开发者生成与分发（纯本地校验）。需要购买或咨询请联系：

- 📱 **QQ：37784552**
- 📧 **Email：somnusweiwei1989@outlook.com**

## License

仅供学习交流使用，请遵守微信读书用户协议。使用风险自负。
