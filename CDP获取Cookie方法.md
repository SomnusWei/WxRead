# CDP 获取 Cookie 方法

> 通过 Chrome DevTools Protocol (CDP) 连接本地 Chrome 浏览器，调用 `Network.getCookies` 获取微信读书 `wr_*` 系列 Cookie

---

## 一、方案概述

```
┌─────────────────────────────────────────────────────────────┐
│                    CDP 获取 Cookie 流程                      │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. 启动 Chrome（--remote-debugging-port=9223）             │
│     │                                                       │
│  2. 程序连接 CDP（HTTP /json 获取 targets）                  │
│     │                                                       │
│  3. 选择 browser/page target → 建立 WebSocket 连接          │
│     │                                                       │
│  4. 用户在 Chrome 中扫码登录微信读书                         │
│     │                                                       │
│  5. 调用 Network.getCookies → 获取所有域 Cookie             │
│     │                                                       │
│  6. 筛选 wr_* Cookie（wr_skey, wr_vid, wr_rt 等）           │
│     │                                                       │
│  7. 持久化到 config.json                                     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 二、Chrome CDP 模式启动

### 自动启动（程序内）

文件位置：[app/ui/cdp_login_dialog.py](file:///e:/item/wxread/app/ui/cdp_login_dialog.py#L53-L68)

```python
CDP_PORT = 9223  # 用 9223 避免与 test_extension 的 9222 冲突

# 设置 QtWebEngine Chromium 启动参数
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
    f"--remote-debugging-port={CDP_PORT} "
    "--disable-features=TranslateUI"
)
```

### 手动启动（Chrome 浏览器）

```powershell
# Chrome CDP 模式启动
chrome.exe --remote-debugging-port=9223 --user-data-dir=C:\Temp\ChromeCDP

# 访问微信读书
# 在 Chrome 中打开 https://weread.qq.com 并扫码登录
```

---

## 三、CDP 连接流程

### 步骤 1：等待端口就绪

文件位置：[cdp_test_standalone.py](file:///e:/item/wxread/cdp_test_standalone.py#L221-L228)

```python
def connect(self):
    """连接到 CDP。"""
    print(f"[CDP] 尝试连接 CDP 端口 {self._port}...")

    # 等待端口就绪（最多 30 秒）
    if not self._wait_for_port(timeout=30.0):
        print(f"[CDP] ❌ CDP 端口 {self._port} 未就绪")
        return False
```

### 步骤 2：获取 targets

```python
# 通过 HTTP /json 获取所有 targets
targets = self._get_targets()
# 返回示例：
# [
#   {"type": "page", "title": "微信读书", "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/page/..."},
#   {"type": "browser", "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/browser/..."}
# ]
```

### 步骤 3：建立 WebSocket 连接

文件位置：[cdp_test_standalone.py](file:///e:/item/wxread/cdp_test_standalone.py#L262-L289)

```python
# 选择 target（优先 browser，备选 page）
browser = None
for t in targets:
    if t.get("type") == "browser":
        browser = t
        break
if not browser:
    for t in targets:
        if t.get("type") == "page":
            browser = t
            break

# 建立 WebSocket 连接
ws_url = browser.get("webSocketDebuggerUrl", "")
self._ws = websocket.create_connection(
    ws_url,
    timeout=10,
    enable_multithread=True,
)
self._connected = True

# 验证连接
resp = self._send_and_receive("Runtime.evaluate", {
    "expression": "document.title",
    "returnByValue": True,
}, timeout=5)
title = resp.get("result", {}).get("result", {}).get("value", "?")
print(f"[CDP] ✅ 连接成功：页面标题={title}")
```

---

## 四、获取 Cookie

### 核心方法

文件位置：[app/ui/cdp_login_dialog.py](file:///e:/item/wxread/app/ui/cdp_login_dialog.py#L446-L469)

```python
def _fetch_cookies_once(self):
    """获取当前所有 Cookie（Network.getCookies + Storage.getCookies 合并）。"""
    cookies = []

    # 1. Network.getCookies（获取所有域 Cookie，含 HttpOnly）
    try:
        resp = self._send_and_receive("Network.getCookies", {})
        cookies = resp.get("result", {}).get("cookies", [])
        log.debug("[CDPWorker] Network.getCookies 返回 %d 条", len(cookies))
    except Exception as exc:
        log.warning("[CDPWorker] Network.getCookies 失败：%s", exc)
        cookies = []

    # 2. Storage.getCookies（更现代的接口，合并去重）
    try:
        resp2 = self._send_and_receive("Storage.getCookies", {}, timeout=5.0)
        storage_cookies = resp2.get("result", {}).get("cookies", [])
        if storage_cookies:
            for c in storage_cookies:
                if c not in cookies:
                    cookies.append(c)
    except Exception:
        log.debug("[CDPWorker] Storage.getCookies 不可用（正常）")

    return cookies
```

### CDP 命令发送与接收

文件位置：[app/ui/cdp_login_dialog.py](file:///e:/item/wxread/app/ui/cdp_login_dialog.py#L372-L397)

```python
def _send_and_receive(self, method: str, params: dict | None = None,
                      timeout: float = 10.0) -> dict:
    """发送 CDP 命令并等待匹配响应。"""
    if not self._ws or not self._connected_cdp:
        raise RuntimeError("CDP 未连接")

    self._msg_id += 1
    cmd = {"id": self._msg_id, "method": method}
    if params:
        cmd["params"] = params

    self._ws.send(json.dumps(cmd))

    # 读取响应（跳过事件通知）
    end_time = time.time() + timeout
    while time.time() < end_time:
        raw = self._ws.recv()
        resp = json.loads(raw)
        if resp.get("id") == self._msg_id:
            return resp
        # 其他 id 或事件通知，跳过

    raise TimeoutError(f"CDP 命令 {method} 超时")
```

---

## 五、Cookie 筛选与持久化

### 筛选 wr_* Cookie

文件位置：[app/ui/cdp_login_dialog.py](file:///e:/item/wxread/app/ui/cdp_login_dialog.py#L714-L728)

```python
def _on_cookies_received(self, cookies):
    """接收 Cookie 后的解析/展示逻辑。"""
    # 筛选 wr_* Cookie
    wr_cookies = [c for c in cookies if c.get("name", "").startswith("wr_")]

    # 检查 wr_skey 长度（≥8 即有效）
    wr_skey = self._find_cookie(cookies, "wr_skey")
    if wr_skey and len(wr_skey.get("value", "")) >= 8:
        log.info("✅ wr_skey 有效，长度=%d", len(wr_skey["value"]))
    else:
        log.warning("⚠️ wr_skey 缺失或长度不足")

    # 更新 UI
    for c in wr_cookies:
        self.log_msg.emit(f"  🍪 {c['name']} = {c['value'][:20]}...")
```

### 持久化到 config.json

文件位置：[app/ui/cdp_login_dialog.py](file:///e:/item/wxread/app/ui/cdp_login_dialog.py#L771-L779)

```python
def _persist_cookies(self, cookies):
    """持久化 Cookie 到 config.json。"""
    # 转换为 {name: value} 格式
    cookie_dict = {c["name"]: c["value"] for c in cookies if c.get("name")}

    # 写入 config
    self._config.set("cookies", cookie_dict)
    self._config.set("cookies_raw", cookies)

    log.info("💾 Cookie 已持久化：%d 条", len(cookie_dict))
```

---

## 六、Cookie 字段说明

| Cookie 字段 | 含义 | 必需 |
|------------|------|------|
| `wr_vid` | 用户唯一标识 | ✅ |
| `wr_skey` | 会话密钥（有效期，需刷新） | ✅ |
| `wr_rt` | 刷新令牌 | ✅ |
| `wr_name` | 昵称（URL 编码） | 可选 |
| `wr_avatar` | 头像 URL | 可选 |

### 有效性检测

```python
# 简化校验：只要 wr_skey 存在且长度 ≥ 8 即认为登录态有效
def check_session(self) -> bool:
    wr_skey = self._cfg.get("cookies.wr_skey") or ""
    return len(wr_skey) >= 8
```

**说明**：
- 微信读书 Web 端仅依赖 `wr_*` 系列 Cookie
- 不需要 RK/ptcz（Web 端不下发这两个 Cookie）
- 简化为 wr_skey 长度检查

---

## 七、CDP 端口选择

| 端口 | 用途 | 说明 |
|------|------|------|
| 9222 | 旧扩展测试 | 已弃用 |
| **9223** | **当前 CDP 登录** | **主程序使用** |

### 端口就绪检测

```python
def _wait_for_port(self, timeout=30.0) -> bool:
    """等待 CDP 端口就绪。"""
    import socket
    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(("127.0.0.1", self._port))
            sock.close()
            if result == 0:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False
```

---

## 八、完整连接日志示例

```
[CDP] 尝试连接 CDP 端口 9223...
[CDP] 端口已就绪（第 1 次尝试）
[CDP] 获取到 1 个 targets
[CDP]   - type=page title=微信读书
[CDP] 连接 WebSocket: ws://127.0.0.1:9223/devtools/page/8B9A29B42C80D8919409599C762DC16B...
[CDP] ✅ 连接成功：页面标题=微信读书
[CDPWorker] 发送 Network.getCookies...
[CDPWorker] Network.getCookies 返回 11 条
  🍪 wr_skey = yE8PYBu2RiKQwjFP...
  🍪 wr_vid = 492091
  🍪 wr_rt = ...
💾 Cookie 已持久化：11 条
✅ wr_skey 有效，长度=24
```

---

## 九、独立测试程序

文件位置：[cdp_test_standalone.py](file:///e:/item/wxread/cdp_test_standalone.py)

### CDPManager 类

```python
class CDPManager:
    """CDP 连接管理器。"""

    def __init__(self, port=9223):
        self._port = port
        self._ws = None
        self._connected = False

    def connect(self) -> bool:
        """连接到 CDP。"""

    def get_cookies(self) -> list[dict]:
        """获取所有 Cookie。"""

    def evaluate_js(self, expression: str) -> dict:
        """在浏览器上下文执行 JavaScript。"""

    def close(self):
        """关闭连接。"""
```

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
3. 在 Chrome 中扫码登录微信读书
4. 点击 **「🍪 获取 Cookie」**
5. 验证 wr_skey 存在且长度 ≥ 8

---

## 十、常见问题

### Q1: CDP 端口连接失败？

```powershell
# 检查 Chrome 是否以 CDP 模式启动
netstat -ano | findstr 9223

# 如果没有，手动启动
chrome.exe --remote-debugging-port=9223 --user-data-dir=C:\Temp\ChromeCDP
```

### Q2: Network.getCookies 返回空？

- 确保在 Chrome 中已经访问过 `weread.qq.com`
- 确保已经扫码登录（页面显示用户头像）
- 尝试访问 `https://weread.qq.com/web/shelf` 后再获取

### Q3: wr_skey 缺失？

- 检查是否已扫码登录
- 检查 Cookie 域是否为 `weread.qq.com`
- 清除 Chrome 缓存后重新登录
