# 📖 WxReadAssistant

> 基于 PySide6 的微信读书自动阅读桌面助手（Windows 平台）
> CDP 扫码登录 + Skill API 拉取书架/章节池 + 本地数据库驱动进度阅读

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
| **WxPusher 推送** | 每日任务开始/完成、Cookie 失效、Skill 登录成功等事件推送 |
| **开机自启 + 托盘** | 注册表写入自启项，支持 `--minimized` 启动即最小化到托盘，单例防多开 |
| **异步 UI** | Skill 调用 / CDP 登录 / 书架拉取全部走 QThread，UI 不阻塞 |

### 📊 阅读统计 4 项

通过微信读书 Skill 网关获取官方统计数据：

- **今日阅读时长** — 当日累计秒数
- **本周累计时长** — 本周一至今累计
- **本月累计时长** — 本月累计
- **总累计时长** — 账号历史总阅读时长

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
│  └──────────────────┘  └─────────────┴──────────────────┘ │ │
├──────────────────────────────────────────────────────────────┤
│                       业务逻辑层                              │
│  ┌───────────┬──────────────┬────────────┬─────────────────┐ │
│  │ scheduler  │ weread_api   │ skill_api   │  cdp_login      │ │
│  │ 阅读调度   │ 阅读上报     │ Skill 调用  │  CDP 扫码登录   │ │
│  │ 进度管理   │ 签名算法     │ 书架/章节   │  Cookie 抓取    │ │
│  │ 完成检测   │ 续期         │ 进度/统计   │  Cookie 持久化  │ │
│  └───────────┴──────────────┴────────────┴─────────────────┘ │
├──────────────────────────────────────────────────────────────┤
│                       数据持久化层                            │
│  ┌──────────────┬───────────────┬──────────────────────────┐ │
│  │ config.json   │ local_db.json  │  chapter_cache.json     │ │
│  │ 用户配置      │ 书架+进度+统计 │  章节池（按 bookId 分桶）│ │
│  └──────────────┴───────────────┴──────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

### 核心工作流

```
启动 → 单例检测 → 加载样式/日志 → 构造主窗口
       ↓
调度循环：
  1. 检测登录态（wr_skey 有效性）
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
│   │   ├── scheduler.py       # 进度驱动阅读调度器
│   │   ├── weread_api.py      # 微信读书阅读上报 + 签名算法
│   │   ├── skill_api.py       # Skill 网关调用（书架/章节/进度/统计/报告）
│   │   ├── report_aggregator.py # 报告数据聚合（LocalDB + Skill → 16 字段统一结构）
│   │   ├── local_db.py        # 本地数据库（书架/进度/章节池持久化）
│   │   └── notifier.py        # WxPusher 推送
│   ├── ui/
│   │   ├── main_page.py       # 主界面（状态+操作+统计+日志单页布局）
│   │   ├── main_window.py     # 主窗口（组装各模块 + 托盘）
│   │   ├── settings_page.py   # 设置页（时长/间隔/Skill Key/推送/系统）
│   │   ├── shelf_dialog.py    # 书架查看对话框
│   │   ├── report_dialog.py   # 可视化报告对话框（QPainter 图表 · 纯绘制无依赖）
│   │   ├── cdp_login_dialog.py# CDP 扫码登录对话框
│   │   └── styles.py          # 全局 QSS 样式
│   └── utils/
│       ├── logger.py          # 日志（APPDATA 14 天 + 项目根 3 天）
│       └── autostart.py       # 开机自启（注册表 HKCU\...\Run）
├── main.py                    # 入口（单例 + --minimized）
├── WxReadAssistant.spec       # PyInstaller 打包配置
├── requirements.txt
├── scripts/
│   └── build_release.py       # 一键构建：full.zip + 增量补丁 + release_note
├── _weread_skills_104/        # Skill 1.0.4 协议文档
├── weread_skill_latest/       # Skill 最新协议文档
├── dist/                      # 打包 onedir 产物（WxReadAssistant/，.gitignore 忽略）
└── release/                   # 发布区（full.zip / patch / sha1 清单 / release_note）
```

## 快速开始

### 环境要求

- Windows 10/11
- Python 3.10+
- 本地 Chrome 浏览器（CDP 扫码登录需要）

### 安装依赖

```bash
pip install -r requirements.txt
```

> 依赖：PySide6（含 QtWebEngine）、requests、urllib3、websocket-client、pyinstaller

### 源码运行

```bash
python main.py
# 或启动即最小化到托盘（配合开机自启）
python main.py --minimized
```

### 🚢 发布 & 安装（正式用户）

所有正式二进制都放 `release/` 目录，**大二进制（onedir 目录 / full.zip / patch.zip / sha1）不进入 git 历史**（已加入 `.gitignore`），仅 `release_note-vX.Y.Z.md` 版本说明会随源码一起 commit。

> **📦 当前 v2.2.4**：完整包 `WxReadAssistant-v2.2.4-full.zip`（255 MB）+ 增量补丁 `patch-v2.2.4-from-v2.2.3.zip`（6 MB）已产出。新用户下载 full.zip 解压即用；v2.2.3 用户可下载 patch.zip 双击 `apply_patch.bat` 升级。

#### 产物清单（scripts/build_release.py 自动生成）

```
release/
├── WxReadAssistant-vX.Y.Z-full/               # onedir 完整目录（可直接运行 / 分发）
├── WxReadAssistant-vX.Y.Z-full.zip            # 完整安装包（新手）
├── WxReadAssistant-vX.Y.Z-full.sha1.txt       # 完整包 sha1 清单（校验一致性）
├── patch-vX.Y.Z-from-vA.B.C.zip               # 增量补丁（旧用户）
│   ├── WxReadAssistant/                       #   替换目录（与旧版 onedir 合并）
│   ├── apply_patch.bat                        #   双击应用（自动备份+覆盖+清理）
│   ├── patch_meta.txt                         #   删除清单（D 行 = 需要删除的旧文件）
│   └── README-补丁使用说明.md
└── release_note-vX.Y.Z.md                     #   版本说明 + CHANGELOG + 校验清单
```

#### 1. 开发者：构建新发布版

```bash
# 首次/单版次构建：产出 full.zip + release_note
python scripts\build_release.py --version 2.2.0

# 构建下一版，并基于 2.2.0 生成 增量补丁 zip
python scripts\build_release.py --version 2.2.1 --previous 2.2.0
```

脚本会依次执行：**语法检查 → 版本号 & build_id 写入 icon_store.py → smoke 启动 → PyInstaller onedir → full.zip 归档 + sha1 清单 → （对比 --previous 生成 patch）→ release_note**。

#### 1.5 开发者：如何生成下一版的增量补丁（build_release.py 实操步骤）

> **核心原则**：每次发布完一个版本，都要在本机 `release/` 下**保留上一版的 onedir 展开目录**作为差分基线；
> 缺了它只能发 full.zip，无法生成小体积 patch.zip。

**① 发布 v2.2.0 时先拿到基线目录（这一步你已完成）**
```powershell
cd e:\item\wxread
python scripts\build_release.py --version 2.2.0
# 完成后 release/ 会出现：
#   release/WxReadAssistant-v2.2.0-full/     ← 这个目录千万别删！它就是 v2.2.1 的差分基线
#   release/WxReadAssistant-v2.2.0-full.zip
#   release/WxReadAssistant-v2.2.0-full.sha1.txt
#   release/release_note-v2.2.0.md
```

**② 做完新功能/修复 bug，准备发 v2.2.1（带增量补丁）**
```powershell
cd e:\item\wxread
# --version  写新版本号
# --previous 写上一版的版本号（会去找 release/WxReadAssistant-v2.2.0-full/ 做差分）
python scripts\build_release.py --version 2.2.1 --previous 2.2.0
```

**③ 脚本对补丁具体会做什么**
1. 用 PyInstaller 正常构建 v2.2.1 的 onedir 目录；
2. 遍历对比 `release/WxReadAssistant-v2.2.0-full/` 与 新 onedir：
   - **A（新增）/ M（修改）** → 直接复制进 `patch-v2.2.1-from-v2.2.0.zip` 的 `WxReadAssistant/` 子目录，用户覆盖即可；
   - **D（删除）** → 把相对路径写入 `patch_meta.txt`（每行以 `D ` 开头），由 `apply_patch.bat` 读后删除；
3. 把 `apply_patch.bat` + `README-补丁使用说明.md` 一同塞入 patch zip。

最终 `release/` 新增 5 个产物：
```
release/
├── WxReadAssistant-v2.2.1-full/           ← 新版 onedir（又成为 v2.2.2 的基线）
├── WxReadAssistant-v2.2.1-full.zip        ← 完整包
├── WxReadAssistant-v2.2.1-full.sha1.txt
├── patch-v2.2.1-from-v2.2.0.zip           ← 增量补丁（用户双击 apply_patch.bat 升级）
└── release_note-v2.2.1.md
```

**④ 常见出错与解决**

| 报错 / 提示 | 原因 | 解决 |
|-------------|------|------|
| `⚠️ 未找到上一版目录 release\WxReadAssistant-v2.2.0-full，跳过增量补丁` | `--previous` 指定的基线目录不在 release/ | 把上一版的 onedir 展开目录（注意是目录不是 zip）解压/复制到 `release/WxReadAssistant-vOLD-full/` 再重跑；或直接把 `dist/WxReadAssistant/` 手动改名后挪过去 |
| `patch_meta.txt` 里删除条目过多 | 上一版基线目录内容可能不是**原版 onedir**，混入了用户配置/日志等杂项 | 重新解压一份原版 `WxReadAssistant-vOLD-full.zip` 的内容作为基线，不要用已运行过、产生了 `local_db.json` 等文件的目录 |
| 用户双击 `apply_patch.bat` 提示「进程仍在运行」 | WxReadAssistant.exe（含托盘）未退出 | 右键托盘图标 → 退出；或任务管理器杀掉 `WxReadAssistant.exe` 再打补丁 |

**⑤ apply_patch.bat 的保证（给你/用户双重安心）**
- ✅ 进程占位检查：EXE 仍在运行 → 直接报错退出，绝不覆盖半开文件；
- ✅ 自动备份：旧版目录复制到 `_backup_vOLD_时间戳/`，打坏了手动改回目录名就能回滚；
- ✅ 不碰用户数据：**完全不读写** `%APPDATA%\WxReadAssistant\`（Cookie、`config.json`、`local_db.json`、`chapter_cache.json`、日志）；
- ✅ 删除清单：只删 `patch_meta.txt` 中 `D ` 开头列出的旧文件，不扫盘不乱删。

#### 2. 终端用户：首次安装（完整包）

1. 下载 `WxReadAssistant-vX.Y.Z-full.zip`，解压到例如 `C:\Program Files\WxReadAssistant\`。
2. 进入 `WxReadAssistant\` 子目录，双击 `WxReadAssistant.exe` 启动。
3. 主页 → 「📷 扫码登录」按提示使用 Chrome CDP 扫码获取 Cookie。
4. （可选）进入「⚙️ 配置中心」填入 **Skill API Key** / **WxPusher Token**。

#### 3. 终端用户：补丁升级（推荐后续更新都走它）

> 补丁包内部自带 `apply_patch.bat` + 中文说明，**不触碰** `%APPDATA%\WxReadAssistant\` 下的 Cookie / 配置 / 本地数据库。

1. 退出 WxReadAssistant（含托盘 → 右键 → 退出）。
2. 下载 `patch-vNEW-from-vOLD.zip`。
3. 解压到你**安装目录的父级**（保证解压后 `apply_patch.bat` 与 `WxReadAssistant\` 在同一目录）。
4. 双击 `apply_patch.bat`（或右键「以管理员身份运行」）。
5. 脚本依次：**备份旧版 → 覆盖新文件 → 删除旧文件清单 → 回滚提示**。
6. 启动主程序后，通过「窗口标题 / 主界面右下版本胶囊 / 配置中心标题旁」任意一处确认版次是否升级。

### 打包 EXE（原生 PyInstaller）

```bash
# 请优先使用 scripts/build_release.py，它会处理版本号/补丁等交付细节。
# 裸打包仅作备用：
python -m PyInstaller WxReadAssistant.spec --noconfirm
# 产物：dist/WxReadAssistant/WxReadAssistant.exe
```

## 版本 & 校验

- **版本号源**：`app/ui/icon_store.py::APP_VERSION`（打包脚本会自动改写）
- **Build ID**：`app/ui/icon_store.py::APP_BUILD_ID` = `<git short sha> · <YYYYMMDD>`
- **显示位置**：窗口标题 / 托盘 ToolTip / 主界面日志区右下胶囊 / 「⚙️ 配置中心」标题旁小胶囊 / 启动日志 banner。
- **一致性校验**：`release/WxReadAssistant-vX.Y.Z-full.sha1.txt` 列出每个 onedir 文件的 sha1。

## CHANGELOG

### v2.2.4 — 2026-08-27

#### 📊 可视化报告页面（新功能）
- 新增 `app/core/report_aggregator.py`：聚合 LocalDB + yao-weread-skill 数据，输出 16 字段统一报告结构；Skill 不可用时退回 LocalDB 确定性聚合。
- 新增 `app/ui/report_dialog.py`：纯 QPainter 绘制的可视化报告页，包含：
  - 读者画像 banner（总书数 / 已读 / 在读 / 今日时长 / 连续天数）
  - 书架归一化饼图（**进度维度**：已完成/在读/未读 + **可见性维度**：私密书/公开书，双正交维度合计=藏书总数，杜绝重复计数）
  - 阅读趋势线（近 30 日每日时长折线）
  - 月度热力图（GitHub 风格贡献图）
  - 20 条阅读高光摘要
- 主界面藏书楼操作区新增「查看报告」按钮（4×2 Grid），点击弹出报告对话框，主窗口不最小化。
- `skill_api.py` 新增 `fetch_weread_report()` 方法，499/4xx 自动熔断停止后续端点请求。

#### 🐛 Bug 修复
- **stop()→再 start() 卡在启动延迟**：`scheduler.py::run()` 入口未 `_stop_event.clear()`，导致重入时线程立即 return；已补充 `_stop_event` / `_pause_event` / 计数器 / 当前书 / skill_refresh_ts 全状态复位。
- **Skill 统计刷新 KeyError 'today_seconds'**：`local_db.py::update_reading_stats` 原地 `stats.pop()` 修改了调用方字典，导致后续访问失败；已改为先 `dict(stats)` 拷贝再操作。
- **扫码登录主窗口闪烁**：`CDPLoginDialog` 设为独立顶层窗口（parent=None + WindowType.Tool），与「查看报告」行为一致。
- **启动延时无倒计时**：改为 250ms 步长循环，每秒发射状态信号更新 UI 倒计时。
- **调度器暂停事件语义反转**：`_pause_event.set()`=运行态（非暂停），主循环条件从 `if self._pause_event.is_set()` 改为 `if not self._pause_event.is_set()`。

### v2.2.3 — 2026-08-26
- 修复「重置今日目标」按钮功能错误：原逻辑清除的是已完成时长（today_seconds），改为清除 `daily_plan` 字段（真正重置目标）。

### v2.2.2 — 2026-08-26
- 配置中心新增「重置今日目标」按钮，方便手动清除当日目标重新随机。
- `LocalDB` 新增 `reset_today_seconds()` 方法。

### v2.2.1 — 2026-08-25
- 修复跨天后 `today_seconds` 未重置导致误发「已完成」消息 + 自动停止任务。
- 新增 `today_date` 字段，`get_today_seconds()` 检测跨天时自动归零。

### v2.2.0 — 2026-08-25

#### 🎨 主界面（整板非卡 · Paper Studio）
- 阅读统计 4 项取消「4 独立白卡」，重新设计为**一张整板通栏**：
  - 外框仅 1 条 `#E5E5DF` 细边 + 圆角 12（白底 `#FFFFFF`）
  - 内部 4 段**等宽**（Grid 4 列 `stretch=1`）+ **段间距 18px**（`horizontalSpacing=18`）
  - 每段统一行预算：标题 28 / 数值 54 / aux 18 / 条 12（含底留 8），行高由 `setRowMinimumHeight` 硬写 + `setRowStretch=0` 禁用弹性，根治 Windows Fusion 高 DPI 下「数值/标题全行消失或裁下半」。
  - 字号节奏（与非卡整板视觉匹配）：标题 18px 墨黑 / 数值 36px 微信蓝 `#307CFF` / aux 12px muted / 进度 4px 无边框贴底留 8。

#### ⚙️ 配置中心
- 布局：行 0 三列（📖/🔑/📢）；行 1 两列（💻/🧹，🧹 跨 col1+col2 合并避免右下留空）。
- 删除独立「🎯 说明」组框，6 条建议拆为 inline `💡 ...` 放在对应组框底部。
- SpinBox 上下箭头 Bug（高 DPI 黑方块）**根治**：`_ArrowTextSpinBoxStyle(QProxyStyle)` 覆写 `drawComplexControl(CC_SpinBox)`，`super` 画框后用 JetBrains Mono 8pt 粗体以字符方式绘制「▲/▼」——完全绕开 Fusion `border-triangle` 渲染坏路径。

#### 🧭 版本可见性（解决「用户拿到同名旧二进制无法分辨」痛点）
- 新增 `app_version_display()` → 统一返回 `"{APP_VERSION} · {APP_BUILD_ID}"`，应用到：
  1. 窗口标题（`WxReadAssistant v...`）
  2. 托盘 ToolTip / 托盘激活消息 / 最小化消息
  3. 主界面日志面板右下 胶囊
  4. 「⚙️ 配置中心」标题 + 返回按钮之间的小胶囊
  5. 启动日志 banner（`===== WxReadAssistant vX 启动 =====`）
- `scripts/build_release.py` 每次构建前**覆盖写入** `APP_VERSION` 与 `APP_BUILD_ID`（`git sha · YYYYMMDD`）。

#### 🚢 发布与补丁分发（本次交付核心）
- 新增 `scripts/build_release.py`：一键 **full.zip** + **增量 patch.zip**（基于 `--previous` 做 A/M/D 差分：A/M 覆盖，D 写入 `patch_meta.txt` → `apply_patch.bat` 读后删除）。
- 补丁 zip 内嵌 `apply_patch.bat`：
  - 进程占位检查（若 WxReadAssistant.exe 仍运行 → 报错退出）
  - 旧版备份 → `_backup_vOLD_时间戳/`（失败可回滚）
  - robocopy 覆盖 → 删除清单中的旧文件
  - 保证**完全不动** `%APPDATA%\WxReadAssistant\`（Cookie / 配置 / 本地 DB / 日志）。
- `release/` 目录统一管理：full 目录、full.zip、sha1 清单、patch、release_note；`.gitignore` 已避免把大二进制推送进 git。
- **补丁基线约定**：每次发布后请在本机**保留** `release/WxReadAssistant-vX.Y.Z-full/` 目录；下一次构建 `--previous X.Y.Z` 就能直接差分出增量补丁，无需再重跑 600MB+ full.zip 的完整分发。
- v2.2.0 本身是该机制的「首个基线版」：仅产出 full.zip（255MB），增量补丁从 v2.2.1 开始可用。

### v2.1.0 — 2026-07（历史基线，首次引入「米白纸 Paper Studio」+ 夜读星辰开关移除）
- Header 左 Logo「📖 微信读书助手」 + 右日期胶囊 / Cookie 状态。
- 三列比例 3:4:4：阅读状态 / 当前阅读 / 藏书楼操作。
- 藏书楼操作：3×2 Grid（登录/书架｜获取数据/刷新统计｜刷新进度/配置中心），移除重复 Cookie 控件。
- 中列「当前阅读」册页米黄底：`#FBF3DE` + 米黄描边 `#E6D7B0`。
- 取消「夜读星辰」双主题：QSS 主题切换接口保留但恒应用 LIGHT（米白纸 Paper Studio）。
- QSS 配色令牌：`paper #F9F9F5 / card #FFFFFF / border #E5E5DF / ink #1F2937 / ok #2D9D3C / gold #D99B2A / danger #D14343 / buff #FBF3DE / buff-bd #E6D7B0`。

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

> 各设置的 inline 提示位于「⚙️ 配置中心」对应组框底部，建议首次使用先浏览。

## 关键设计

- **进度驱动选书**：优先选进度最高且 <100% 的书，100% 换书，避免单一章节重复触发风控。
- **本地兜底**：Skill 不可用时由 `local_db.json` 继续驱动阅读循环，保证连续运行。
- **混合完成度**：本地实时累加秒数（每 30~45 秒 +45s）+ 每 30 分钟 Skill 覆盖，双保险判定当日达标。
- **风控规避**：随机书籍/章节切换、间隔抖动、长休息、动态 `baggage`/`sentry-trace` 请求头匹配浏览器指纹。
- **Cookie 安全**：CDP 抓取按名称前缀白名单过滤，`wr_skey` 全量存储不截断，PySide6 6.11+ SameSite 枚举序列化时转 `.value`。
- **日志双写**：APPDATA（14 天保留，便于长期追溯）+ 项目根（3 天保留，便于即时排查）。

## License

仅供学习交流使用，请遵守微信读书用户协议。使用风险自负。
