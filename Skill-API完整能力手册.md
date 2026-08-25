# Skill 1.0.5 API 完整能力手册

> 微信读书 Skill 网关统一入口：`POST https://i.weread.qq.com/api/agent/gateway`
> 认证方式：`Authorization: Bearer wrk-<API_KEY>`
> 申请地址：https://weread.qq.com/r/weread-skills

---

## 一、能力总览

| 能力 | 接口 | 用途 | 已实测 |
|------|------|------|--------|
| 阅读统计 | `/readdata/detail` | 今日/本周/本月/总累计时长 | ✅ |
| 书架同步 | `/shelf/sync` | 获取书架所有书的 bookId | ✅ 365 本 |
| 章节池 | `/book/chapterinfo` | 获取指定书籍的章节 UID 列表 | ✅ 31 章 |
| 阅读进度 | `/book/getprogress` | 获取指定书籍的阅读进度 | ✅ |
| 书籍信息 | `/book/info` | 获取书籍详情（标题/作者/简介） | ✅ |

---

## 二、配置项

文件位置：[app/core/config.py](file:///e:/item/wxread/app/core/config.py#L94-L98)

```python
"skill": {
    "api_key": "",                # WEREAD_API_KEY (wrk-xxxxxxxx)
    "version": "1.0.5",          # Skill 版本号
    "summary_cache_ttl": 180,    # 阅读统计缓存秒数（默认 3 分钟）
},
```

---

## 三、阅读统计（日/周/月/总计）

### 接口

```
POST /api/agent/gateway
Body: {"api_name": "/readdata/detail", "mode": "overall", "baseTime": 0, "skill_version": "1.0.5"}
```

### 返回字段

| 字段 | 说明 |
|------|------|
| `readTimes` | 阅读时长分桶数据 |
| `today` | 今日阅读秒数 |
| `weekly` | 本周累计秒数 |
| `monthly` | 本月累计秒数 |
| `totalReadingTime` | 总累计秒数 |

### 核心代码

文件位置：[app/core/weread_api.py](file:///e:/item/wxread/app/core/weread_api.py#L2660-L2793)

```python
def fetch_daily_reading_summary(self) -> dict:
    """获取今日/本周/本月/总累计阅读时长（Skill 网关）。"""
    api_key = self._cfg.get("skill.api_key")
    if not api_key:
        return {}
    
    # 检查缓存
    cache_ttl = int(self._cfg.get("skill.summary_cache_ttl") or 180)
    if self._skill_cache and time.time() - self._skill_cache_ts < cache_ttl:
        return self._skill_cache
    
    # 调用 Skill /readdata/detail
    data = self._fetch_skill_reading_summary()
    if data:
        self._skill_cache = data
        self._skill_cache_ts = time.time()
    return data
```

### 使用场景

- **状态页数据卡**：显示今日/本周/本月/总累计 4 项权威时长
- **完成度检测**：作为 `skill_baseline_sec` 参与完成度计算

---

## 四、书架所有书的 bookId

### 接口

```
POST /api/agent/gateway
Body: {"api_name": "/shelf/sync", "skill_version": "1.0.5"}
```

### 返回结构

```json
{
  "books": [
    {
      "bookId": "842609",
      "title": "祈祷落幕时",
      "author": "东野圭吾",
      "category": "精品小说-推理",
      "cover": "https://...",
      "finishReading": 0,
      "readUpdateTime": 1787485265
    }
  ],
  "albums": [...],
  "mp": {...},
  "bookCount": 365
}
```

### 书架总数口径

```
书架总数 = books.length + albums.length + (mp 非空 ? 1 : 0)
```

### 核心代码

文件位置：[app/core/weread_api.py](file:///e:/item/wxread/app/core/weread_api.py#L3069-L3094)

```python
def skill_fetch_shelf_sync(self, *, timeout: int = 10) -> list[dict]:
    """通过 Skill 1.0.5 /shelf/sync 获取完整书架。"""
    data = self._call_skill_api("/shelf/sync", {}, timeout=timeout)
    if not data:
        return []
    
    books = data.get("books") or data.get("data") or []
    if not isinstance(books, list):
        return []
    
    # 过滤有效书籍（必须有 bookId）
    valid = [b for b in books if isinstance(b, dict) and b.get("bookId")]
    return valid
```

### 使用场景

- CDP 登录成功后自动调用，获取书架存入 `config.shelf_books`
- 用于书籍跳跃阅读（换书时从书架随机选）

---

## 五、每本书的章节池

### 接口

```
POST /api/agent/gateway
Body: {"api_name": "/book/chapterinfo", "bookId": "842609", "skill_version": "1.0.5"}
```

### 返回结构

```json
{
  "bookId": "842609",
  "synckey": "...",
  "chapterUpdateTime": 1787321486,
  "chapters": [
    {
      "chapterUid": 1,
      "chapterIdx": 1,
      "title": "封面",
      "level": 1,
      "wordCount": 1,
      "price": 0,
      "paid": 0
    },
    {
      "chapterUid": 33,
      "chapterIdx": 33,
      "title": "1",
      "level": 1,
      "wordCount": 10919,
      "price": 0,
      "paid": 0
    }
  ]
}
```

### 字段说明

| 字段 | 说明 |
|------|------|
| `chapterUid` | 章节唯一 ID，**自动读书的核心参数** |
| `chapterIdx` | 章节序号（通常与 uid 一致） |
| `level` | 目录层级：1=一级标题, 2=二级, 3=正文章节 |
| `wordCount` | 章节字数 |
| `price` | 章节价格（0=免费） |
| `paid` | 是否已购买（1=已购买） |

### 核心代码

文件位置：[app/core/weread_api.py](file:///e:/item/wxread/app/core/weread_api.py#L3096-L3119)

```python
def skill_fetch_chapter_info(self, book_id: str, *, timeout: int = 10) -> list[dict]:
    """通过 Skill 1.0.5 /book/chapterinfo 获取章节池。"""
    data = self._call_skill_api(
        "/book/chapterinfo",
        {"bookId": book_id},
        timeout=timeout,
    )
    if not data:
        return []
    
    chapters = data.get("chapters") or data.get("chapterInfos") or []
    if not isinstance(chapters, list):
        return []
    
    return chapters
```

### 章节池持久化

章节池按 `book_id` 分桶存储于 `chapter_cache.json`：

```json
{
  "842609": {
    "fingerprint": "06e499e6...",
    "chapters": [1, 32, 33, 34, ...],
    "updated_at": 1787485265
  },
  "35821223": {
    "fingerprint": "a1b2c3d4...",
    "chapters": [1, 2, 3, ...],
    "updated_at": 1787485265
  }
}
```

### 使用场景

- CDP 登录后为书架前 N 本书构建章节池
- 换书时从新书的章节池中随机选择章节
- 优先选择 `level >= 3` 的正文章节

---

## 六、每本书的读书进度

### 接口

```
POST /api/agent/gateway
Body: {"api_name": "/book/getprogress", "bookId": "842609", "skill_version": "1.0.5"}
```

### 返回结构

```json
{
  "bookId": "842609",
  "book": {
    "chapterUid": 33,
    "chapterOffset": 17215,
    "progress": 0,
    "updateTime": 1787321486,
    "recordReadingTime": 0,
    "isStartReading": 0
  },
  "timestamp": 1787485265
}
```

### 字段说明

| 字段 | 说明 |
|------|------|
| `progress` | 阅读进度百分比（0-100）。**1=1%，不是 100%** |
| `chapterUid` | 当前阅读章节 UID |
| `chapterOffset` | 当前章节内偏移 |
| `recordReadingTime` | 累计阅读时长（秒） |
| `updateTime` | 最后阅读时间 |
| `isStartReading` | 是否已开始阅读 |
| `finishTime` | 读完时间（仅 progress=100 时存在） |

### 核心代码

文件位置：[app/core/weread_api.py](file:///e:/item/wxread/app/core/weread_api.py#L3121-L3136)

```python
def skill_get_book_progress(self, book_id: str, *, timeout: int = 10) -> dict | None:
    """通过 Skill 1.0.5 /book/getprogress 获取阅读进度。"""
    data = self._call_skill_api(
        "/book/getprogress",
        {"bookId": book_id},
        timeout=timeout,
    )
    return data
```

### 使用场景

- 启动时从书架选一本"进度最高未读完"的书作为默认当前书
- 可用于批量扫描书架进度（展示已读/在读/未读状态）

---

## 七、通用调用方法

所有 Skill API 都通过统一的 `_call_skill_api()` 方法调用：

文件位置：[app/core/weread_api.py](file:///e:/item/wxread/app/core/weread_api.py#L3008-L3067)

```python
def _call_skill_api(self, api_name: str, params: dict, *, timeout: int = 10) -> dict | None:
    """通用 Skill API 调用方法（1.0.5+ 版本）。"""
    api_key = str(self._cfg.get("skill.api_key") or "").strip()
    if not api_key:
        return None

    skill_version = str(self._cfg.get("skill.version") or "1.0.5")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "api_name": api_name,
        "skill_version": skill_version,
    }
    payload.update(params)

    r = self._session.post(SKILL_GATEWAY_URL, headers=headers, json=payload, timeout=timeout)

    # 499 = 限流，401/403 = 鉴权失败
    if r.status_code == 499:
        return None
    if r.status_code == 401 or r.status_code == 403:
        return None

    data = r.json()
    if data.get("errcode", 0) != 0:
        return None

    # 检查升级提示
    if "upgrade_info" in data:
        log.warning("⚠️ Skill 有新版可用：%s", data.get("upgrade_info"))

    return data
```

### 限流策略

- HTTP 499 = 限流，立即返回 None
- 批量调用时间隔 300ms 避免限流
- 阅读统计缓存 TTL = 180 秒

---

## 八、实测数据（2026-08-24）

| 接口 | 响应时间 | 结果 |
|------|---------|------|
| `/shelf/sync` | 512ms | 365 本电子书 + 1 本有声书 + 文章收藏 |
| `/book/chapterinfo` | 99ms | 31 个章节 |
| `/book/getprogress` | 103ms | progress=0%, chapterUid=33 |
| `/readdata/detail` | ~500ms | 今日/本周/本月/总累计 |

---

## 九、版本说明

- CDN 最新 Skill 包：`https://cdn.weread.qq.com/skills/weread-skills.zip`
- 包内 `SKILL.md` 声明版本：`1.0.4`
- API 实际接受 `skill_version=1.0.5`，无 `upgrade_info` 提示
- **没有 2.0.0 版本**（CDN 404）
