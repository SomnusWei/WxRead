# 📦 WxReadAssistant v2.2.4 — Release Note
> 打包日期：2026-08-27  ·  构建 SHA：1e18de88

## 下载地址

| 文件 | 说明 |
|------|------|
| `WxReadAssistant-v2.2.4-full.zip` | 完整安装包（**首次安装** 或 **从任意旧版本升级** 解压即用） |
| `patch-v2.2.4-from-v2.2.3.zip` | **增量补丁**（只适用于 v2.2.3 → v2.2.4，体积更小，见 README「补丁升级」） |

## 首次安装步骤

1. 解压 `WxReadAssistant-vX.Y.Z-full.zip` 到任意目录（建议 `C:\Program Files\WxReadAssistant\`）。
2. 进入子目录 `WxReadAssistant\`，双击 `WxReadAssistant.exe` 启动。
3. 首次使用：主页 → 「📷 扫码登录」→ 按提示扫码获取 Cookie。
4. 配置：进入「⚙️ 配置中心」填入 **Skill API Key**（可选，用于官方阅读统计回写）。

## 补丁升级（推荐后续版本都走这个）

1. 关闭 WxReadAssistant（含托盘：右键托盘图标 → 退出）。
2. 解压 `patch-vNEW-from-vOLD.zip` **到你的 WxReadAssistant 安装父目录**（与 `WxReadAssistant\` 同层）。
3. 双击 `apply_patch.bat`（或右键「以管理员身份运行」）。
4. 脚本会自动备份旧版 → 覆盖新文件 → 清理旧文件。
5. 重启 `WxReadAssistant.exe`，进入「⚙️ 配置中心」顶部或主界面右下版本胶囊，
   确认显示 `v2.2.4`。

## v2.2.4 CHANGELOG（与 v2.2.3 对比）

### 🎨 主界面（米白纸 Paper Studio）

- 阅读统计 4 项取消「独立卡片」，改为**一整张整板**通栏 + 4 段间距 18px + 严格等宽；
  4 段内容：标题 18px 墨黑 / 数值 36px 微信蓝 / aux 12px muted / 4px 进度条贴底留 8px。
- 根因修复：之前卡内 VBox + MinimumExpanding/stretch(1) 会在 Windows Fusion 高 DPI 下
  错误收缩 QLabel sizeHint → 标题/数值全行消失，本轮**全部改 Grid 硬预算**。
- Header：左侧「📖 微信读书助手」Logo + 右日期胶囊 / Cookie 状态点 / Cookie 胶囊。
- 版本号由「Cookie 旁」迁移到工作日志面板右下（小胶囊），窗口标题 / 托盘 ToolTip / 托盘
  消息 / 配置中心标题旁也同步展示完整版本号（含 build_id）。

### ⚙️ 配置中心

- 2×3 Grid → 行 0：📖/🔑/📢；行 1：💻 系统（1 列） + 🧹 数据（**跨 col1+col2**）；
  删除独立的「🎯 说明」组框，6 条建议**拆分到对应组框底部**作为 💡 inline tips。
- 修复 SpinBox 上下箭头在高 DPI 渲染为「黑方块」Bug：使用 QProxyStyle 直接绘制
  ▲/▼ 字符（JetBrains Mono 8pt 粗）。

### 🚢 发布流程

- 新增 `scripts/build_release.py`：一键 **版本号写入** → **PyInstaller onedir** →
  **full.zip 归档** → **增量 patch 打包** → **release_note 生成**。
- 所有 EXE 会把 **构建 SHA + 日期** 写入 `app/ui/icon_store.py::APP_BUILD_ID`，
  主界面 / 日志 / 配置中心都可见，避免用户拿到同名旧二进制。

### ✅ 校验清单

- [ ] 首次安装：exe 启动无报错；扫码登录后状态点变绿。
- [ ] 补丁升级：应用后窗口标题 / 版本胶囊显示新版次。
- [ ] 配置中心 → 6 个 GroupBox 底部均有 1 条 inline 说明；SpinBox 箭头非黑方块。
- [ ] 点击「停止 / 开始」按钮，日志追加 OK，阅读统计 4 段行高无裁字。

> 完整 sha1 清单见 `WxReadAssistant-v2.2.4`-full.sha1.txt
