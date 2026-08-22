# 微信读书助手（Windows 桌面版）

基于 PySide6 自研的 Win 端桌面工具，针对 [findmover/wxread](https://github.com/findmover/wxread) 的微信读书接口做了稳定的 Windows 集成版。

## ✨ 核心功能

1. **扫码一键登录**：内嵌浏览器打开 weread.qq.com，用户完成扫码登录后点击「我已登录完成」，程序自动抽取 Cookies / Headers 并持久化保存。
2. **随机时长 & 随机间隔**：
   - 每日阅读时长可设范围（如 8～10 小时），每天启动时在该区间随机一个分钟级精确时长，例如 9小时47分钟。
   - 每一次翻页上报的间隔可设范围（默认 25–45 秒），叠加 ±15% 抖动 + 每 20 次插入 60–180 秒"休息"，最大程度模拟真实阅读节奏。
3. **登录态自动维护**：每次阅读前健康检查 + `wr_skey` 自动续期；若完全失效则立即通过 WxPusher 提醒你重新扫码。
4. **WxPusher 推送**：支持每日任务开始/结束、Cookie 失效三类通知；支持"发送测试消息"校验通道。
5. **开机自启动 & 托盘最小化**：
   - 设置中勾选"开机自启动"会写入 `HKCU\...\Run` 注册表（免管理员）。
   - 点击关闭按钮时默认最小化到右下角托盘，后台继续运行；托盘菜单支持显示/隐藏/退出。
   - 支持"启动后直接进托盘"，便于开后台。
6. **清新简约 UI**：浅色 + 蓝色主色，圆角卡片，三标签页（状态 / 扫码登录 / 设置）。

## 🚀 运行 / 打包

### 1) 开发运行

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

### 2) 打包为 EXE

双击 `build.bat`，或手动执行：

```powershell
pip install pyinstaller
pyinstaller --noconfirm --clean --name WxReadAssistant --windowed --onedir ^
  --collect-all PySide6 ^
  --collect-submodules PySide6.QtWebEngineCore --collect-submodules PySide6.QtWebEngineWidgets ^
  main.py
```

输出位于 `dist\WxReadAssistant\WxReadAssistant.exe`。

## 📁 项目结构

```
wxread/
├── main.py                # 应用入口
├── build.bat              # PyInstaller 一键打包
├── requirements.txt
└── app/
    ├── core/
    │   ├── config.py        # JSON 持久化配置 (APPDATA/WxReadAssistant)
    │   ├── weread_api.py    # 微信读书接口：sg/s 签名 / Cookie 续期 / /read 上报
    │   ├── scheduler.py     # 后台调度：随机时长 + 随机节奏
    │   └── notifier.py      # WxPusher 推送（异步、重试、去重节流）
    ├── ui/
    │   ├── main_window.py   # 主窗口 + 系统托盘 + closeEvent
    │   ├── status_page.py   # 状态 + 实时日志
    │   ├── login_page.py    # QWebEngineView 扫码登录
    │   ├── settings_page.py # 设置页
    │   └── styles.py        # 全局 QSS（清新简约风）
    └── utils/
        ├── autostart.py     # 开机自启（注册表 Run）
        └── logger.py        # 文件 + 控制台 + UI 信号日志
```

## ⚙️ 关键技术点

- **接口签名**：与 findmover/wxread 同源的自定义哈希 `cal_hash`、`encode_data`，以及基于 `KEY` 盐值的 `sg = SHA256(ts + rn + KEY)` 双重签名。
- **Cookie 续期**：调用 `/web/login/renewal` 自动刷新 `wr_skey`，失败即发 WxPusher 通知（4 小时内去重，防止轰炸）。
- **风控规避**：13 本默认书 + 17 章池 + 多项字段随机（`ci`/`co`/`pr`/`rn`/`ts+offset`），请求间隔 ±15% 抖动 + 每 20 次长停顿。
- **数据持久化**：配置存于 `%APPDATA%/WxReadAssistant/config.json`，日志存于同目录 `logs/app.log`，按天滚动保留 14 天。

## 📝 使用流程

1. 启动程序 → 切换到「🔐 扫码登录」标签。
2. 内嵌浏览器打开 weread.qq.com → 手机微信扫码登录 → 确认能正常打开一本书。
3. 点击【✅ 我已登录完成】，程序自动抽取 Cookie 并立刻做一次健康检查。
4. 切换到「⚙️ 设置」：设置每日阅读时长范围（如 8–10 小时）、翻页间隔范围；填入 WxPusher SPT 并点"发送测试消息"验证；勾选开机自启等。
5. 切换到「🟢 状态」→ 点【▶ 开始阅读】。完成后可关闭窗口，程序会最小化到托盘后台继续。

> 若某天 Cookie 彻底失效，会通过 WxPusher 立即推送提醒你回来重新扫码。
