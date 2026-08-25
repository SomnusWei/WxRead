# WxReadAssistant v2.0 全新重构方案

> 全新版本，从零开始设计。用户继续使用备份的主程序（v1.x），v2.0 并行开发。

---

## 一、设计原则

| 原则 | 说明 |
|------|------|
| **Skill 优先，本地兜底** | 书架/章节池/进度/统计从 Skill 获取并本地持久化，Skill 不可用时本地兜底 |
| **CDP 仅用于登录** | Chrome DevTools Protocol 只负责扫码登录抓取 Cookie，不参与阅读流程 |
| **阅读进度本地优先** | 本地实时更新进度，定期用 Skill 覆盖校正 |
| **进度驱动选书选章** | 按进度排序选书，单章 4~5 次后换章，100% 换书 |

---

## 二、整体架构

```
┌──────────────────────────────────────────────────────────────┐
│                    WxReadAssistant v2.0                      │
├──────────────────────────────────────────────────────────────┤
│                         UI 层                                 │
│  ┌──────────────────┬──────────────────────────────────────┐ │
│  │   settings_page   │          main_page（主界面）          │ │
│  │  设置页           │  ┌─────────────┬──────────────────┐ │ │
│  │  - 时长范围       │  │  上左：状态  │  上右：操作+统计  │ │ │
│  │  - 间隔范围       │  │  + 控制按钮  │  - 阅读统计卡     │ │ │
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

---

## 二·五、UI 丝滑体验设计

> 目标：所有交互响应 < 100ms，弹窗/切换流畅无卡顿

### 1. 异步化原则

| 操作 | 执行方式 | 说明 |
|------|---------|------|
| Skill API 调用 | QThread 后台线程 | UI 不阻塞，加载动画 |
| CDP 登录连接 | QThread 后台线程 | 弹窗即显示，连接状态实时更新 |
| 阅读上报 | scheduler 线程 | 独立于 UI 线程 |
| 书架/章节池拉取 | QThread + 信号 | 拉取完成信号通知 UI 刷新 |
| Cookie 验证 | QThread | 按钮转圈，验证后回填状态 |

### 2. 弹窗与对话框动画

| 组件 | 动画效果 | 实现 |
|------|---------|------|
| CDP 登录对话框 | 淡入淡出（200ms） | `QGraphicsOpacityEffect` + `QPropertyAnimation` |
| 书架查看对话框 | 滑入（从右向左，250ms） | `QPropertyAnimation` 操作 `pos` |
| 确认/提示框 | 淡入（150ms） | `QPropertyAnimation` 操作 `windowOpacity` |
| 状态切换 | 颜色渐变（300ms） | QSS `:hover` / 自定义动画 |

### 3. 加载状态反馈

| 场景 | 反馈组件 | 效果 |
|------|---------|------|
| Skill 调用中 | 按钮文字变「⏳ 处理中...」+ 禁用 | 防重复点击 |
| 书架拉取中 | 覆盖层 + 旋转加载图标 | `QMovie` GIF 或 `QSpinnerWait` |
| CDP 连接中 | 弹窗内进度条 + 实时日志 | 日志逐行追加 |
| 阅读统计刷新 | 数字翻牌动画 | `QPropertyAnimation` 缓动 `OutCubic` |

### 4. 实时数据流（无抖动）

| 数据 | 更新方式 | 防抖策略 |
|------|---------|---------|
| 当前书名/进度/章节 | Qt 信号直连 UI Label.setText | 无需防抖（低频） |
| 工作日志 | 信号批量追加（最多 100 行缓冲） | 超过 500 行自动裁剪 |
| 阅读统计卡 | 信号 + 缓动动画 | 300ms 防抖，避免频繁刷新 |
| 成功/失败计数 | 信号直连 | 无需防抖 |

### 5. 性能保障

- **主线程只做 UI 渲染**，所有 I/O 和 API 调用在后台线程
- **日志刷新节流**：日志追加采用 100ms 批量合并，避免每行都触发重绘
- **书架列表虚拟化**：书架查看对话框使用 `QListView` + `QAbstractListModel`，支持 365+ 本书籍流畅滚动
- **进度条/计数器缓动**：数字变化使用 `QVariantAnimation` 缓动（300ms `OutCubic`），避免跳变

### 6. 防误操作

| 场景 | 处理 |
|------|------|
| 按钮快速连点 | 操作中禁用按钮 + 超时自动恢复（30s） |
| 未登录点开始 | 提示「请先扫码登录」+ 高亮登录按钮 |
| 未配置 Key 点获取 | 提示「请先在设置中配置 Skill API Key」+ 跳转设置页 |
| 关闭时阅读中 | 托盘最小化（可配置），不直接退出 |

---

## 三、设置页面（SettingsPage）

### 3.1 布局

```
┌─────────────────────────────────────────────┐
│  ⚙️ 设置                                     │
├─────────────────────────────────────────────┤
│                                             │
│  📖 阅读参数                                │
│  ┌─────────────┬─────────────┐              │
│  │ 每日最少时长 │ 每日最多时长 │  （小时）    │
│  │    [1.5]    │    [3.0]    │              │
│  └─────────────┴─────────────┘              │
│  ┌─────────────┬─────────────┐              │
│  │ 单页停留下限 │ 单页停留上限 │  （秒）      │
│  │    [30]     │    [45]     │              │
│  └─────────────┴─────────────┘              │
│                                             │
│  🔑 Skill API Key                           │
│  ┌─────────────────────────┬─────────┐      │
│  │ wrk-_________          │ ✅ 验证  │      │
│  └─────────────────────────┴─────────┘      │
│  状态：未验证 / ✅有效 / ❌无效              │
│                                             │
│  📢 WxPusher 推送                           │
│  SPT: ┌─────────────────────────────────┐    │
│  │ SPT_xxxxxxxxxxxx                 │    │
│  └─────────────────────────────────┘    │
│                                             │
│  通知规则（多选）：                         │
│  ☑ 每日首次开始（包含今日目标）             │
│  ☑ Cookie 失效通知                          │
│  ☑ 任务完成发送                             │
│  ☐ 登录成功通知                              │
│                                             │
│  💻 系统设置                                │
│  ☐ 开机自启动                               │
│  ☑ 点击关闭时最小化到托盘                    │
│                                             │
│  🧹 数据维护                                │
│  ┌─────────────────────────┬─────────┐      │
│  │ 清除 Cookie 并重启程序   │  🧹 清除 │      │
│  └─────────────────────────┴─────────┘      │
│  说明：清除后需重新扫码登录                  │
│                                             │
│  ┌───────────┐                              │
│  │ 💾 保存设置 │                              │
│  └───────────┘                              │
└─────────────────────────────────────────────┘
```

**清除 Cookie 流程**：
1. 点击「🧹 清除」→ 弹出确认对话框「确定要清除 Cookie 并重启吗？」
2. 用户确认 → 清空 `config.cookies` 中所有字段
3. 停止 scheduler 线程（如果在运行）
4. 弹出提示「Cookie 已清除，3 秒后自动重启」
5. 调用 `QProcess::startDetached` 重新启动程序 + 当前进程退出

### 3.2 配置项

```python
DEFAULT_CONFIG = {
    "reading": {
        "min_hours": 1.5,              # 每日最少阅读时长（小时）
        "max_hours": 3.0,              # 每日最多阅读时长（小时）
        "min_interval_sec": 30,        # 单页停留下限（秒）
        "max_interval_sec": 45,        # 单页停留上限（秒）
        "chapter_read_min": 4,         # 单章最少阅读次数
        "chapter_read_max": 5,         # 单章最多阅读次数
    },
    "skill": {
        "api_key": "",                  # wrk- 开头的 API Key
        "version": "1.0.5",
        "summary_cache_ttl": 180,      # 统计缓存秒数
        "refresh_interval_min": 30,    # Skill 数据刷新间隔（分钟）
    },
    "push": {
        "wxpusher_spt": "",
        "notify_daily_start": True,    # 每日首次开始
        "notify_cookie_fail": True,    # Cookie 失效
        "notify_daily_done": True,      # 任务完成
        "notify_login_success": False,  # 登录成功
    },
    "app": {
        "auto_start": False,
        "minimize_to_tray": True,
    },
    "daily_plan": {
        "date": "",
        "target_minutes": 0,
    },
}
```

### 3.3 Skill API Key 验证

点击「✅ 验证」按钮后：
1. 调用 `POST /api/agent/gateway` + `api_name=/shelf/sync`
2. HTTP 200 且返回 `books` 数组 → 显示「✅ 有效」
3. HTTP 401/403 → 显示「❌ 鉴权失败」
4. 超时/网络错误 → 显示「❌ 连接失败」

---

## 四、主界面（MainPage）

### 4.1 布局

```
┌──────────────────────────────────────────────────────────────┐
│  WxReadAssistant v2.0                          [─] [□] [×]  │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌────────────────────────┬─────────────────────────────────┐│
│  │   上左：状态+控制       │   上右：统计+操作               ││
│  │                        │                                 ││
│  │  日期：2026-08-24       │  📊 阅读统计                    ││
│  │  状态：阅读中           │  今日：1h23m  本周：5h12m      ││
│  │  登录态：✅ 有效        │  本月：12h45m 总计：156h       ││
│  │                        │                                 ││
│  │  今日目标：90min       │  🔘 操作                        ││
│  │  已完成：35min (39%)   │  ┌──────────────────────────┐  ││
│  │  成功：12  失败：1     │  │ 🔐 扫码登录（CDP弹窗）     │  ││
│  │  下次请求：14:32:15    │  │ 📚 查看书架               │  ││
│  │  ──────────────────   │  │ 🔄 获取书架/章节池/进度    │  ││
│  │  📖 当前阅读书籍        │  │ ⚙️ 设置                   │  ││
│  │  书名：祈祷落幕时       │  └──────────────────────────┘  ││
│  │  书籍进度：35%          │                                 ││
│  │  当前章节：第3章(uid=35)│  最后刷新：14:25:30            ││
│  │  本章已读：2/5 次      │                                 ││
│  │  ┌──────┬──────┬─────┐ │                                 ││
│  │  │▶ 开始│⏸ 暂停│⏹停止│ │                                 ││
│  │  └──────┴──────┴─────┘ │                                 ││
│  └────────────────────────┴─────────────────────────────────┘│
│                                                              │
├──────────────────────────────────────────────────────────────┤
│  📋 工作日志                              [清空] [暂停滚动] │
├──────────────────────────────────────────────────────────────┤
│  14:25:30 📚 书架加载完成：365 本                            │
│  14:25:31 📖 章节池构建：祈祷落幕时 31 章                   │
│  14:25:32 📊 阅读统计刷新：今日 35min                       │
│  14:30:15 ✅ 阅读成功：祈祷落幕时 第3章                     │
│  14:30:45 ⏱️ 下次请求：14:31:15（间隔 30s）                │
│  14:31:16 ✅ 阅读成功：祈祷落幕时 第3章                     │
│  ...                                                         │
└──────────────────────────────────────────────────────────────┘
```

### 4.2 上左：状态+控制

#### 基础状态

| 显示项 | 数据来源 | 刷新频率 |
|--------|---------|---------|
| 日期 | 本地 | 每分钟 |
| 状态 | scheduler | 实时 |
| 登录态 | check_session | 每次循环 |
| 今日目标 | config.daily_plan | 启动时 |
| 已完成 | Skill 今日阅读时长 | 每 30 分钟刷新 |
| 成功/失败 | scheduler 计数 | 每次请求后 |
| 下次请求时间 | scheduler | 实时 |

#### 当前阅读书籍（实时显示）

| 显示项 | 数据来源 | 刷新时机 | 说明 |
|--------|---------|---------|------|
| 📖 当前阅读书籍 | scheduler.current_book | 换书时 | 当前正在读的书名 |
| 书名 | local_db.shelf | 换书时 | 如"祈祷落幕时" |
| 书籍进度 | local_db 书籍 progress | **每次阅读成功后实时更新** | 如"35%"，本地累加 |
| 当前章节 | scheduler.current_chapter | 换章时 | 如"第3章 (uid=35)" |
| 本章已读次数 | scheduler.chapter_read_count | 每次阅读成功后 | 如"2/5 次" |

**实时进度更新规则**：
- 每次阅读上报成功后，立即更新 UI 显示的书籍进度
- 本地进度 = Skill 拉取的基准进度 + 本次会话累加
- 章节切换时进度跳变（按章节在全书中的比例计算）
- Skill 定期刷新（每 30 分钟）时用服务端数据覆盖校正

| 控制按钮 | 功能 |
|---------|------|
| ▶ 开始阅读 | 启动 scheduler 线程 |
| ⏸ 暂停 | 设置 pause_event，等待当前请求完成后暂停 |
| ⏹ 停止 | 设置 stop_event，终止调度线程 |

### 4.3 上右：阅读统计

数据来源：本地持久化的 Skill 统计数据

| 统计项 | 说明 |
|--------|------|
| 今日 | 今日阅读时长（从 Skill /readdata/detail 获取） |
| 本周 | 本周累计 |
| 本月 | 本月累计 |
| 总计 | 总累计 |

**兜底**：Skill 未获取到数据时显示 `-`。

### 4.4 上右：操作按钮

| 按钮 | 功能 | 弹窗/操作 |
|------|------|----------|
| 🔐 扫码登录 | CDP 弹窗扫码 | 弹出 CDP 登录对话框 |
| 📚 查看书架 | 显示本地书架 | 弹出书架列表（书名/进度/章节池） |
| 🔄 获取书架/章节池/进度 | Skill 全量刷新 | 调用 Skill 更新本地数据 |
| ⚙️ 设置 | 切换到设置页 | 切换 Tab |

### 4.5 下：工作日志

- 实时滚动显示运行日志
- 支持暂停滚动、清空
- 日志同时写入 `%APPDATA%\WxReadAssistant\logs\app.log`

---

## 五、数据模型与本地持久化

### 5.1 local_db.json — 书架+进度+统计

```json
{
  "last_sync_time": "2026-08-24T14:25:30",
  "shelf": {
    "books": [
      {
        "bookId": "842609",
        "title": "祈祷落幕时",
        "author": "东野圭吾",
        "cover": "https://...",
        "progress": 35,
        "current_chapter_uid": 35,
        "current_chapter_title": "第3章",
        "record_reading_time": 0,
        "last_read_time": 1787321486
      }
    ],
    "total_count": 365
  },
  "reading_stats": {
    "today_seconds": 2100,
    "weekly_seconds": 18720,
    "monthly_seconds": 45900,
    "total_seconds": 561600,
    "updated_at": "2026-08-24T14:25:30"
  }
}
```

### 5.2 chapter_cache.json — 章节池

```json
{
  "842609": {
    "title": "祈祷落幕时",
    "chapters": [
      {"chapterUid": 1, "title": "封面", "level": 1, "wordCount": 1},
      {"chapterUid": 33, "title": "1", "level": 1, "wordCount": 10919},
      {"chapterUid": 34, "title": "2", "level": 1, "wordCount": 4455}
    ],
    "updated_at": "2026-08-24T14:25:30"
  }
}
```

### 5.3 config.json — 用户配置+Cookie

```json
{
  "cookies": {"wr_skey": "...", "wr_vid": "...", "wr_rt": "..."},
  "reading": {"min_hours": 1.5, "max_hours": 3.0, ...},
  "skill": {"api_key": "wrk-...", "version": "1.0.5"},
  "push": {"wxpusher_spt": "SPT-...", ...},
  "daily_plan": {"date": "2026-08-24", "target_minutes": 90}
}
```

---

## 六、工作流

### 6.1 启动流程

```
程序启动
  │
  ├─ 1. 加载 config.json
  ├─ 2. 检查 Cookie 是否存在（wr_skey 长度 ≥ 8）
  │     ├─ 有效 → 显示"登录态：✅ 有效"
  │     └─ 无效 → 显示"登录态：❌ 失效"（需扫码登录）
  ├─ 3. 加载 local_db.json（书架+进度+统计）
  ├─ 4. 显示阅读统计（来自本地，兜底显示 "-"）
  └─ 5. 等待用户点击"▶ 开始阅读"
```

### 6.2 CDP 扫码登录流程

```
用户点击"🔐 扫码登录"
  │
  ├─ 1. 弹出 CDP 登录对话框
  ├─ 2. 提示用户启动 Chrome CDP 模式
  │     chrome.exe --remote-debugging-port=9223 --user-data-dir=...
  ├─ 3. 程序连接 CDP WebSocket
  ├─ 4. 用户在 Chrome 中扫码登录微信读书
  ├─ 5. 点击"已登录" → 调用 Network.getCookies 获取 wr_* Cookie
  ├─ 6. 持久化 Cookie 到 config.json
  ├─ 7. 自动触发"获取书架/章节池/进度"
  │     ├─ Skill /shelf/sync → 书架存入 local_db.json
  │     ├─ Skill /book/chapterinfo → 章节池存入 chapter_cache.json
  │     └─ Skill /book/getprogress → 阅读进度存入 local_db.json
  ├─ 8. 拉取 Skill /readdata/detail → 统计存入 local_db.json
  └─ 9. 关闭对话框，主界面显示"登录态：✅ 有效"
```

### 6.3 获取书架/章节池/进度流程

```
用户点击"🔄 获取书架/章节池/进度"（或自动触发）
  │
  ├─ 1. Skill /shelf/sync → 获取完整书架
  │     └─ 存入 local_db.json shelf.books
  ├─ 2. 对书架中 progress < 100% 的书：
  │     ├─ Skill /book/chapterinfo → 获取章节池
  │     │   └─ 存入 chapter_cache.json
  │     └─ Skill /book/getprogress → 获取阅读进度
  │         └─ 更新 local_db.json shelf.books[].progress
  ├─ 3. Skill /readdata/detail → 获取阅读统计
  │     └─ 存入 local_db.json reading_stats
  └─ 4. 更新 last_sync_time
```

### 6.4 阅读主循环

```
用户点击"▶ 开始阅读"
  │
  ├─ 1. 检查前置条件
  │     ├─ Cookie 有效？ → 否：提示扫码登录
  │     ├─ 书架有数据？ → 否：提示获取书架
  │     └─ 今日计划 → 随机生成目标时长
  ├─ 2. 选取阅读书籍（进度驱动选书）
  │     ├─ 过滤 progress < 100% 的书
  │     ├─ 按 progress 降序排序（优先读进度高的）
  │     └─ 选第一本作为当前书
  ├─ 3. 选取起始章节
  │     └─ 从 current_chapter_uid 开始
  ├─ 4. 启动随机延迟 30~90s
  └─ 5. 进入主循环 ↓
```

### 6.5 主循环逻辑

```
while not stop_event:
    ┌─ 暂停检查
    │   if pause_event: sleep(0.2); continue
    │
    ├─ ① 健康检查（每 12 分钟）
    │   if wr_skey 失效:
    │       暂停阅读 + WxPusher 通知 + 等待重新登录
    │
    ├─ ② 完成度检查
    │   today_seconds = local_db.reading_stats.today_seconds
    │   if today_seconds >= target_minutes * 60:
    │       标记完成 + WxPusher 通知 + 停止
    │
    ├─ ③ 检查书架是否全部 100%
    │   if all books progress == 100:
    │       停止 + 提示"书架已全部读完"
    │
    ├─ ④ 定期刷新 Skill 数据（每 30 分钟）
    │   Skill /readdata/detail → 更新今日阅读时长
    │   Skill /book/getprogress → 更新当前书进度
    │
    ├─ ⑤ 执行单次阅读上报 read_once()
    │   构造 payload → POST /web/book/read
    │   成功 → success_count++
    │         → 本地累加进度（按章节比例微调）
    │         → **实时更新 UI：书籍进度 + 本章已读次数**
    │   失败 → fail_count++，失败冷却
    │
    ├─ ⑥ 章节切换检查
    │   if 当前章节已读 4~5 次:
    │       切换到下一个章节
    │       本地更新 current_chapter_uid
    │       **实时更新 UI：当前章节 + 本章已读次数归零**
    │       if 当前书进度 >= 100%:
    │           换书：选下一本 progress < 100% 的书
    │           **实时更新 UI：当前书 + 书籍进度**
    │
    └─ ⑦ 计算下次间隔
        base = random(30, 45)
        jitter = random(0.85, 1.15)
        interval = base * jitter
        + 长休息(60~180s, 每20次)
        + 失败冷却(30~60s)
        sleep(interval)
```

### 6.6 进度管理策略

| 事件 | 本地进度更新 | UI 实时刷新 | Skill 覆盖 |
|------|-------------|-------------|-----------|
| 单次阅读成功 | progress 按章节比例微调（+1/章节数×100%） | ✅ 书籍进度实时更新 | - |
| 章节切换 | current_chapter_uid 更新，本章已读归零 | ✅ 当前章节+已读次数更新 | - |
| 章节读完 | progress 跳变（按章节在全书中的位置计算） | ✅ 书籍进度更新 | - |
| 换书 | 选下一本，重置章节和已读次数 | ✅ 当前书+进度+章节全部更新 | - |
| 定期刷新（30 分钟） | - | ✅ 用 Skill 数据刷新显示 | Skill /book/getprogress 覆盖 |
| Skill 拉取进度 100% | 本地 progress = 100 | ✅ 显示 100% | ✅ |

**UI 实时刷新机制**：
- scheduler 通过 Qt 信号 `book_progress_updated(book_title, progress, chapter_title, chapter_read_count)` 通知 UI
- 每次阅读成功后立即发射信号，UI 即时更新
- 章节切换/换书时发射 `current_book_changed` / `current_chapter_changed` 信号

### 6.7 暂停/停止条件

| 条件 | 触发动作 |
|------|---------|
| Cookie 失效 | 暂停 + WxPusher Cookie 失效通知 + 等待重新登录 |
| 已完成 >= 今日目标 | 停止 + WxPusher 任务完成通知 |
| 书架全部 100% | 停止 + 提示"书架已读完" |
| 用户点击暂停 | 等待当前请求完成后暂停 |
| 用户点击停止 | 等待当前请求完成后停止 |

---

## 七、兜底规则

### 7.1 Skill 阅读统计获取失败

| 场景 | 处理 |
|------|------|
| Skill API Key 未配置 | 统计显示 `-`，完成度用本地估算 |
| Skill 调用失败/超时 | 统计显示上次缓存值，日志告警 |
| Skill 限流 (499) | 立即返回，下次定时刷新重试 |

### 7.2 本地无书架数据

| 场景 | 处理 |
|------|------|
| local_db.json 不存在或书架为空 | 提示用户"请先获取书架" |
| 书架有书但章节池为空 | 随机选一本书，从第一章开始 |

### 7.3 本地有书架但全部 100%

| 场景 | 处理 |
|------|------|
| 所有书 progress = 100% | 停止阅读 + 提示"书架已全部读完" |

### 7.4 Cookie 失效

| 场景 | 处理 |
|------|------|
| wr_skey 缺失或长度 < 8 | 暂停 + 提示扫码登录 |
| 阅读请求返回 302/登录页 | 暂停 + WxPusher Cookie 失效通知 |

---

## 八、文件结构

```
wxread_v2/
├── main.py                      # 应用入口
├── requirements.txt
├── WxReadAssistant.spec          # 打包配置
├── app/
│   ├── __init__.py
│   ├── core/
│   │   ├── config.py            # 配置管理（APPDATA 持久化）
│   │   ├── weread_api.py        # 微信读书 API（签名/上报/续期）
│   │   ├── skill_api.py         # Skill 1.0.5 API（书架/章节/进度/统计）
│   │   ├── scheduler.py         # 阅读调度（循环/间隔/完成检测/进度管理）
│   │   ├── local_db.py          # 本地数据管理（书架/进度/统计持久化）
│   │   └── notifier.py          # WxPusher 推送
│   ├── ui/
│   │   ├── __init__.py
│   │   ├── main_window.py       # 主窗口（上下布局+托盘）
│   │   ├── main_page.py         # 主界面（状态+统计+操作+日志）
│   │   ├── settings_page.py     # 设置页
│   │   ├── cdp_login_dialog.py  # CDP 登录对话框
│   │   ├── shelf_dialog.py      # 书架查看对话框
│   │   └── styles.py            # 全局 QSS
│   └── utils/
│       ├── logger.py            # 日志
│       └── autostart.py         # 开机自启
└── docs/
    ├── Skill-API完整能力手册.md
    ├── 工作流流程与核心代码.md
    ├── 防风控策略.md
    └── CDP获取Cookie方法.md
```

---

## 九、模块职责

### 9.1 skill_api.py（新增，独立模块）

```python
class SkillAPI:
    """Skill 1.0.5 API 调用器。"""

    def __init__(self, config: ConfigStore):
        self._cfg = config

    def _call(self, api_name: str, params: dict) -> dict | None:
        """通用调用方法。"""

    def fetch_shelf(self) -> list[dict]:
        """获取书架 /shelf/sync。"""

    def fetch_chapters(self, book_id: str) -> list[dict]:
        """获取章节池 /book/chapterinfo。"""

    def fetch_progress(self, book_id: str) -> dict | None:
        """获取阅读进度 /book/getprogress。"""

    def fetch_reading_stats(self) -> dict:
        """获取阅读统计 /readdata/detail。"""

    def verify_api_key(self) -> bool:
        """验证 API Key 是否有效。"""
```

### 9.2 local_db.py（新增，本地数据管理）

```python
class LocalDB:
    """本地数据管理（书架+进度+统计持久化）。"""

    def __init__(self, data_dir: str):
        self._path = os.path.join(data_dir, "local_db.json")
        self._data = self._load()

    def get_shelf(self) -> list[dict]:
        """获取本地书架。"""

    def update_shelf(self, books: list[dict]):
        """更新书架（Skill 拉取后写入）。"""

    def get_book_progress(self, book_id: str) -> int:
        """获取单本书进度。"""

    def update_book_progress(self, book_id: str, progress: int, chapter_uid: int):
        """更新单本书进度（本地或 Skill 覆盖）。"""

    def get_reading_stats(self) -> dict:
        """获取阅读统计。"""

    def update_reading_stats(self, stats: dict):
        """更新阅读统计。"""

    def get_unread_books(self) -> list[dict]:
        """获取进度 < 100% 的书，按进度降序。"""

    def select_next_book(self) -> dict | None:
        """选下一本要读的书（进度最高的未读完的书）。"""
```

### 9.3 scheduler.py（重构，进度驱动）

```python
class Scheduler(QObject):
    """阅读调度器（进度驱动，实时通知 UI）。"""

    # Qt 信号：实时通知 UI 更新
    book_progress_updated = Signal(str, int, str, int)  # (书名, 进度%, 章节标题, 本章已读次数)
    current_book_changed = Signal(str, int)              # (书名, 进度%)
    current_chapter_changed = Signal(str, int)           # (章节标题, uid)
    reading_stats_updated = Signal(dict)                 # (统计字典)

    def __init__(self, api, skill_api, local_db, config, notifier):
        ...

    def run(self):
        """主循环：健康检查→完成检查→选书选章→阅读上报→间隔等待。"""

    def _select_book_and_chapter(self) -> tuple[str, int]:
        """选书选章：进度<100%的书，从最后章节开始。"""

    def _check_chapter_switch(self, plan) -> bool:
        """检查是否需要换章（4~5 次后）。"""

    def _check_book_switch(self, plan) -> bool:
        """检查是否需要换书（进度100%）。"""

    def _check_pause_conditions(self, plan) -> bool:
        """检查暂停条件：Cookie失效/目标完成/书架全100%。"""

    def _refresh_skill_data(self, plan):
        """定期刷新 Skill 数据（统计+进度）。"""

    def _emit_book_progress(self, book_title: str, progress: int,
                            chapter_title: str, chapter_read_count: int):
        """发射进度信号，实时更新 UI 显示当前书名、进度、章节、已读次数。"""
```

---

## 十、实现步骤

| 阶段 | 内容 | 优先级 |
|------|------|--------|
| Phase 1 | 基础框架：main.py + config.py + logger.py | P0 |
| Phase 2 | UI 框架：main_window.py + main_page.py + settings_page.py | P0 |
| Phase 3 | Skill API：skill_api.py（书架/章节/进度/统计） | P0 |
| Phase 4 | 本地数据：local_db.py（持久化+查询） | P0 |
| Phase 5 | CDP 登录：cdp_login_dialog.py（Cookie 抓取） | P0 |
| Phase 6 | 阅读调度：scheduler.py（进度驱动循环） | P0 |
| Phase 7 | 阅读上报：weread_api.py（签名+上报） | P0 |
| Phase 8 | WxPusher：notifier.py（多规则推送） | P1 |
| Phase 9 | 书架查看：shelf_dialog.py | P1 |
| Phase 10 | 系统集成：托盘+自启+打包 | P1 |

---

## 十一、与 v1.x 的关键差异

| 维度 | v1.x | v2.0 |
|------|------|------|
| 选书策略 | 随机换书（20~40 次） | **进度驱动**（优先读进度高的未读完书） |
| 选章策略 | 随机选章 | **顺序选章**（从最后章节开始，4~5 次后换章） |
| 进度管理 | Skill 基线 + 本地累加 | **本地实时 + Skill 定期覆盖** |
| 完成度判断 | Skill 基线 + session 累加 | **Skill 今日阅读时长 >= 目标** |
| 数据来源 | Cookie 抓取为主 | **Skill 为主，Cookie 仅用于登录** |
| UI 布局 | 左右分栏 | **上下布局（状态区+日志区）** |
| 暂停条件 | 仅 Cookie 失效 | **Cookie 失效 + 目标完成 + 书架全 100%** |
