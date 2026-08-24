"""Chrome + CDP 方案独立测试程序

目标：验证在浏览器上下文内通过 CDP Runtime.evaluate 执行 fetch 
      能否成功调用微信读书的书架和章节池 API。

原理：
  1. QtWebEngine 加载 weread.qq.com，用户扫码登录
  2. 通过 CDP WebSocket 连接浏览器
  3. 在浏览器上下文内执行 fetch（自动携带 Cookie 和风控 headers）
  4. 验证 /web/shelf/booklist 和 /web/book/chapterInfos 是否返回 200

使用方法：
  python cdp_test_standalone.py
  或打包后运行 EXE

打包命令：
  pyinstaller --onefile --name CDPStandaloneTest cdp_test_standalone.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

import requests
import websocket

# ============================================================
# 0. 全局异常捕获
# ============================================================
_CRASH_LOG = Path("cdp_standalone_crash.log")

def _install_excepthook():
    def _hook(exc_type, exc_value, exc_tb):
        try:
            with open(_CRASH_LOG, "a", encoding="utf-8") as f:
                f.write("=" * 60 + "\n")
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
                f.flush()
        except Exception:
            pass
    sys.excepthook = _hook

_install_excepthook()

# ============================================================
# 1. Chromium flags：启用 CDP
# ============================================================
CDP_PORT = 9223
WEREAD_URL = "https://weread.qq.com/"

_chromium_flags = f"--remote-debugging-port={CDP_PORT} --disable-features=ExtensionsBrowserActivity,DesktopCaptureNotifications"
_existing_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
    f"{_existing_flags} {_chromium_flags}".strip()
    if _existing_flags
    else _chromium_flags
)

# ============================================================
# 1.5 辅助工具函数
# ============================================================
import hashlib

def create_id(book_id: str) -> str:
    """将 bookId 转换为 webBookId（参考 WeReadX 实现）。"""
    # 第一步：对 bookId 求 MD5
    md5_hash = hashlib.md5(book_id.encode()).hexdigest()
    
    # 第二步：取前3位
    str_sub = md5_hash[:3]
    
    # 第三步：根据 bookId 类型添加前缀
    if book_id.isdigit():
        # 纯数字 bookId，前缀为 '3'
        c = []
        for i in range(0, len(book_id), 9):
            chunk = book_id[i:i+9]
            c.append(format(int(chunk), 'x'))
        fa = ['3', c]
    else:
        # 非纯数字 bookId，前缀为 '4'
        d = ''
        for ch in book_id:
            d += format(ord(ch), 'x')
        fa = ['4', [d]]
    
    str_sub += fa[0]
    str_sub += '2' + md5_hash[-2:]
    
    # 第四步：添加长度前缀
    for m in fa[1]:
        n = format(len(m), 'x')
        if len(n) == 1:
            n = '0' + n
        str_sub += n + m
    
    # 第五步：如果长度不足20位，补充
    if len(str_sub) < 20:
        str_sub += md5_hash[:20 - len(str_sub)]
    
    # 第六步：添加最后3位 MD5
    str_sub += hashlib.md5(str_sub.encode()).hexdigest()[:3]
    
    return str_sub


def extract_books_from_initial_state(manager) -> list:
    """从 window.__INITIAL_STATE__ 解析书籍信息。"""
    print("\n📦 尝试从 __INITIAL_STATE__ 解析书籍...")
    
    code = """
(() => {
    if (!window.__INITIAL_STATE__) {
        return JSON.stringify({success: false, error: '__INITIAL_STATE__ not found'});
    }
    
    const state = window.__INITIAL_STATE__;
    const result = {
        hasRawBooks: !!state.rawBooks,
        hasRawIndexes: !!state.rawIndexes,
        hasBooks: !!state.shelf?.books,
        rawBooksCount: state.rawBooks ? Object.keys(state.rawBooks).length : 0,
        rawIndexesCount: state.rawIndexes ? Object.keys(state.rawIndexes).length : 0,
        sampleRawBooks: [],
        sampleRawIndexes: [],
        allBooks: []
    };
    
    // 从 rawBooks 提取
    if (state.rawBooks) {
        const ids = Object.keys(state.rawBooks).slice(0, 5);
        for (const id of ids) {
            const book = state.rawBooks[id];
            result.sampleRawBooks.push({
                id: id,
                title: book.title || book.bookTitle || '',
                bookId: book.bookId || id,
                author: book.author || '',
                cover: book.cover || book.coverUrl || '',
                finishReading: book.finishReading || 0,
                readUpdateTime: book.readUpdateTime || 0
            });
        }
    }
    
    // 从 rawIndexes 提取（webBookId 列表）
    if (state.rawIndexes) {
        const ids = Object.keys(state.rawIndexes).slice(0, 10);
        for (const id of ids) {
            result.sampleRawIndexes.push({
                id: id,
                value: state.rawIndexes[id]
            });
        }
    }
    
    // 从 shelf.books 提取
    if (state.shelf && state.shelf.books) {
        for (const book of state.shelf.books.slice(0, 10)) {
            result.allBooks.push({
                bookId: book.bookId || book.id || '',
                webBookId: book.webBookId || '',
                title: book.title || '',
                author: book.author || '',
                finishReading: book.finishReading || 0
            });
        }
    }
    
    return JSON.stringify({success: true, data: result});
})()
"""
    
    result = manager.evaluate_js(code, timeout=10)
    if result and result.get('success'):
        data = result.get('data', {})
        print(f"  ✅ __INITIAL_STATE__ 解析成功！")
        print(f"    rawBooks 数量: {data.get('rawBooksCount', 0)}")
        print(f"    rawIndexes 数量: {data.get('rawIndexesCount', 0)}")
        
        if data.get('sampleRawBooks'):
            print(f"    示例书籍:")
            for book in data['sampleRawBooks'][:3]:
                print(f"      - {book.get('title', '?')} (bookId={book.get('bookId', '?')})")
        
        if data.get('allBooks'):
            print(f"    shelf.books 数量: {len(data['allBooks'])}")
            for book in data['allBooks'][:3]:
                print(f"      - {book.get('title', '?')} (bookId={book.get('bookId', '?')})")
        
        return data
    else:
        print(f"  ❌ __INITIAL_STATE__ 解析失败: {result}")
        return None


# ============================================================
# 2. CDP 管理器（简化版）
# ============================================================
class CDPManager:
    """通过 CDP WebSocket 与浏览器通信。"""

    def __init__(self, port: int = CDP_PORT):
        self._port = port
        self._ws: websocket.WebSocket | None = None
        self._connected = False
        self._msg_id = 0
        self._stop_event = threading.Event()
        self._lock = threading.Lock()  # 线程锁保护 CDP 操作
        self._response_queue: dict[int, dict] = {}  # 消息 ID 到响应的映射
        self._condition = threading.Condition(self._lock)  # 条件变量
        self._listener_thread: threading.Thread | None = None
        print(f"[CDP] 初始化：port={port}")

    def connect(self) -> bool:
        """连接到 CDP。"""
        print(f"[CDP] 尝试连接 CDP 端口 {self._port}...")

        # 等待端口就绪
        if not self._wait_for_port(timeout=30.0):
            print(f"[CDP] ❌ CDP 端口 {self._port} 未就绪")
            return False

        # 获取 targets
        try:
            targets = self._get_targets()
        except Exception as exc:
            print(f"[CDP] ❌ 获取 targets 失败：{exc}")
            return False

        if not targets:
            print("[CDP] ❌ 未找到任何 targets")
            return False

        print(f"[CDP] 获取到 {len(targets)} 个 targets")
        for t in targets:
            print(f"[CDP]   - type={t.get('type', '?')} title={t.get('title', '')[:50]}")

        # 找 browser target
        browser = None
        for t in targets:
            if t.get("type") == "browser":
                browser = t
                break
        if not browser:
            # 备选：page target
            for t in targets:
                if t.get("type") == "page":
                    browser = t
                    break

        if not browser:
            print("[CDP] ❌ 未找到可用的 target")
            return False

        ws_url = browser.get("webSocketDebuggerUrl", "")
        print(f"[CDP] 连接 WebSocket: {ws_url[:100]}...")

        try:
            self._ws = websocket.create_connection(
                ws_url,
                timeout=10,
                enable_multithread=True,
            )
            # 先标记为已连接
            self._connected = True
            
            # 启动监听线程
            self._listener_thread = threading.Thread(target=self._listen, daemon=True)
            self._listener_thread.start()
            
            # 验证连接（page target 不支持 Browser.getVersion，用 Runtime.evaluate）
            try:
                resp = self._send_and_receive("Runtime.evaluate", {
                    "expression": "document.title",
                    "returnByValue": True,
                }, timeout=5)
                title = resp.get("result", {}).get("result", {}).get("value", "?")
                print(f"[CDP] ✅ 连接成功：页面标题={title}")
            except Exception:
                print(f"[CDP] ✅ WebSocket 连接成功")
            
            return True
        except Exception as exc:
            print(f"[CDP] ❌ WebSocket 连接失败：{exc}")
            self._connected = False
            return False

    def _listen(self):
        """监听 WebSocket 消息的后台线程。"""
        while self._connected and not self._stop_event.is_set():
            try:
                self._ws.settimeout(1.0)
                raw = self._ws.recv()
                msg = json.loads(raw)
                
                # 检查是否是响应消息（有 id 字段）
                if "id" in msg:
                    with self._condition:
                        self._response_queue[msg["id"]] = msg
                        self._condition.notify_all()
                # 否则是事件通知，忽略
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as exc:
                if self._connected:
                    print(f"[CDP] ⚠️ 监听线程异常：{exc}")
                break

    def disconnect(self):
        """断开 CDP 连接。"""
        self._stop_event.set()
        with self._lock:
            self._connected = False
            self._condition.notify_all()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = None
        print("[CDP] 已断开连接")

    def _wait_for_port(self, timeout: float = 15.0) -> bool:
        """等待 CDP 端口就绪。"""
        deadline = time.time() + timeout
        attempts = 0
        while time.time() < deadline and not self._stop_event.is_set():
            attempts += 1
            try:
                with socket.create_connection(("127.0.0.1", self._port), timeout=0.5):
                    print(f"[CDP] 端口已就绪（第 {attempts} 次尝试）")
                    return True
            except (ConnectionRefusedError, OSError, socket.timeout):
                pass
            time.sleep(0.3)
        return False

    def _get_targets(self) -> list[dict]:
        """获取所有 targets。"""
        url = f"http://127.0.0.1:{self._port}/json"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        return resp.json()

    def _send_and_receive(self, method: str, params: dict | None = None,
                          timeout: float = 10.0) -> dict:
        """发送 CDP 命令并等待匹配响应（使用消息队列机制）。"""
        if not self._ws or not self._connected:
            raise RuntimeError("CDP 未连接")
        
        with self._condition:
            self._msg_id += 1
            msg_id = self._msg_id
            cmd = {"id": msg_id, "method": method}
            if params:
                cmd["params"] = params
            
            # 发送命令
            try:
                self._ws.send(json.dumps(cmd))
            except Exception as exc:
                raise RuntimeError(f"发送 CDP 命令失败：{exc}")
            
            # 等待响应
            deadline = time.time() + timeout
            while msg_id not in self._response_queue:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"CDP {method} 超时")
                self._condition.wait(timeout=remaining)
            
            # 获取响应
            resp = self._response_queue.pop(msg_id)
            return resp

    def get_cookies(self) -> list[dict]:
        """获取所有 Cookie。"""
        print("[CDP] 发送 Network.getCookies...")
        try:
            resp = self._send_and_receive("Network.getCookies", {})
            cookies = resp.get("result", {}).get("cookies", [])
            print(f"[CDP] Network.getCookies 返回 {len(cookies)} 条")
        except Exception as exc:
            print(f"[CDP] Network.getCookies 失败：{exc}")
            cookies = []

        # 尝试 Storage.getCookies
        try:
            resp2 = self._send_and_receive("Storage.getCookies", {}, timeout=5)
            storage_cookies = resp2.get("result", {}).get("cookies", [])
            if storage_cookies:
                print(f"[CDP] Storage.getCookies 返回 {len(storage_cookies)} 条（合并）")
                for c in storage_cookies:
                    if c not in cookies:
                        cookies.append(c)
        except Exception:
            pass

        return cookies

    def print_cookies_summary(self, cookies: list[dict]):
        """打印 Cookie 摘要。"""
        print(f"\n{'='*60}")
        print(f"🍪 Cookie 摘要（共 {len(cookies)} 条）")
        print(f"{'='*60}")

        # 统计域分布
        domain_count: dict[str, int] = {}
        key_cookies: dict[str, dict] = {}

        for c in cookies:
            name = str(c.get("name", ""))
            value = str(c.get("value", ""))
            domain = str(c.get("domain", ""))
            http_only = bool(c.get("httpOnly", False))

            domain_count[domain] = domain_count.get(domain, 0) + 1

            # 关键 Cookie
            if name.startswith("wr_") or name in ("RK", "ptcz"):
                key_cookies[name] = {
                    "length": len(value),
                    "domain": domain,
                    "httpOnly": http_only,
                    "head": value[:4] + "***" if len(value) > 4 else value,
                }

        print("\n📊 域分布:")
        for domain, cnt in sorted(domain_count.items(), key=lambda x: -x[1]):
            print(f"  {domain:30s} {cnt:3d} 条")

        print("\n🔑 关键 Cookie:")
        for name, info in sorted(key_cookies.items()):
            print(f"  {name:20s} len={info['length']:3d}  domain={info['domain']:25s}  httpOnly={info['httpOnly']}  值={info['head']}")

        # 检查登录态
        wr_skey = key_cookies.get("wr_skey", {})
        if wr_skey and wr_skey.get("length", 0) >= 8:
            print(f"\n✅ 登录态有效（wr_skey 长度={wr_skey['length']}）")
        else:
            print(f"\n❌ 登录态无效或未登录（wr_skey={'存在' if wr_skey else '不存在'}）")

    def navigate(self, url: str, timeout: float = 15.0) -> bool:
        """导航到指定 URL。"""
        print(f"[CDP] 导航到 {url}...")
        try:
            # 先 Page.enable
            try:
                self._send_and_receive("Page.enable", {}, timeout=3)
            except Exception:
                pass

            resp = self._send_and_receive("Page.navigate", {"url": url}, timeout=timeout)
            print(f"[CDP] Page.navigate 返回：{json.dumps(resp.get('result', {}), ensure_ascii=False)[:200]}")
            return True
        except Exception as exc:
            print(f"[CDP] ❌ 导航失败：{exc}")
            return False

    def evaluate_js(self, expression: str, *, timeout: float = 15.0) -> dict | None:
        """在浏览器上下文内执行 JS 代码。"""
        try:
            params = {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "timeout": int(timeout * 1000),
            }
            resp = self._send_and_receive("Runtime.evaluate", params, timeout=timeout + 5)
            result = resp.get("result", {}).get("result", {})

            if result.get("type") == "string":
                val = result.get("value", "")
                if val:
                    try:
                        return json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        return {"_raw": val}
                return None
            elif result.get("type") == "object":
                return result.get("value") or result
            elif result.get("type") == "undefined":
                return None
            return result
        except Exception as exc:
            print(f"[CDP] ⚠️ Runtime.evaluate 失败：{exc}")
            return None


# ============================================================
# 3. 浏览器内 fetch 测试
# ============================================================
def test_fetch_bookshelf(manager: CDPManager):
    """测试书架 API（在浏览器上下文内执行 fetch）。"""
    print(f"\n{'='*60}")
    print("📚 测试 1：书架 API")
    print(f"{'='*60}")

    # 先导航到书架页（确保有足够的 Cookie 和上下文）
    print("\n[步骤 1] 导航到书架页面...")
    manager.navigate("https://weread.qq.com/web/shelf")
    time.sleep(3)

    # 方案 A：尝试多种 API 格式
    print("\n[步骤 2] 尝试不同的书架 API 格式...")
    
    # 方案 A1: GET /web/shelf/booklist
    print("\n  [A1] GET /web/shelf/booklist")
    fetch_get = """
(async () => {
    try {
        const resp = await fetch('/web/shelf/booklist', {
            method: 'GET',
            credentials: 'include',
            headers: {
                'Accept': 'application/json, text/plain, */*',
                'x-requested-with': 'XMLHttpRequest'
            }
        });
        const text = await resp.text();
        try {
            const data = JSON.parse(text);
            return JSON.stringify({ok: resp.ok, status: resp.status, contentType: resp.headers.get('content-type'), data: data});
        } catch {
            return JSON.stringify({ok: resp.ok, status: resp.status, contentType: resp.headers.get('content-type'), rawText: text.substring(0, 300)});
        }
    } catch(e) {
        return JSON.stringify({error: e.message});
    }
})()
"""
    result1 = manager.evaluate_js(fetch_get, timeout=15)
    print(f"  结果: status={result1.get('status') if result1 else 'N/A'}, ok={result1.get('ok') if result1 else 'N/A'}")
    
    if result1 and result1.get("ok"):
        print("  ✅ GET /web/shelf/booklist 成功！")
        data = result1.get("data", {})
        books = data.get("books") or data.get("recentBooks") or data.get("shelfBookIds") or data.get("data", [])
        if isinstance(books, list):
            print(f"  📖 书架包含 {len(books)} 本书籍")
            for book in books[:5]:
                book_id = book.get("bookId") or book.get("book_id") or "?"
                title = book.get("title") or book.get("bookTitle") or "?"
                print(f"    - {title} (bookId={book_id})")
        return True

    # 方案 A2: POST /web/shelf/booklist
    print("\n  [A2] POST /web/shelf/booklist")
    fetch_post = """
(async () => {
    try {
        const resp = await fetch('/web/shelf/booklist', {
            method: 'POST',
            credentials: 'include',
            headers: {
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                'x-requested-with': 'XMLHttpRequest'
            },
            body: JSON.stringify({})
        });
        const text = await resp.text();
        try {
            const data = JSON.parse(text);
            return JSON.stringify({ok: resp.ok, status: resp.status, contentType: resp.headers.get('content-type'), data: data});
        } catch {
            return JSON.stringify({ok: resp.ok, status: resp.status, contentType: resp.headers.get('content-type'), rawText: text.substring(0, 300)});
        }
    } catch(e) {
        return JSON.stringify({error: e.message});
    }
})()
"""
    result2 = manager.evaluate_js(fetch_post, timeout=15)
    print(f"  结果: status={result2.get('status') if result2 else 'N/A'}, ok={result2.get('ok') if result2 else 'N/A'}")
    
    if result2 and result2.get("ok"):
        print("  ✅ POST /web/shelf/booklist 成功！")
        data = result2.get("data", {})
        books = data.get("books") or data.get("recentBooks") or data.get("shelfBookIds") or data.get("data", [])
        if isinstance(books, list):
            print(f"  📖 书架包含 {len(books)} 本书籍")
        return True

    # 方案 A3: GET /web/shelf/recent
    print("\n  [A3] GET /web/shelf/recent")
    fetch_recent = """
(async () => {
    try {
        const resp = await fetch('/web/shelf/recent', {
            method: 'GET',
            credentials: 'include',
            headers: {
                'Accept': 'application/json, text/plain, */*',
                'x-requested-with': 'XMLHttpRequest'
            }
        });
        const text = await resp.text();
        try {
            const data = JSON.parse(text);
            return JSON.stringify({ok: resp.ok, status: resp.status, contentType: resp.headers.get('content-type'), data: data});
        } catch {
            return JSON.stringify({ok: resp.ok, status: resp.status, contentType: resp.headers.get('content-type'), rawText: text.substring(0, 300)});
        }
    } catch(e) {
        return JSON.stringify({error: e.message});
    }
})()
"""
    result3 = manager.evaluate_js(fetch_recent, timeout=15)
    print(f"  结果: status={result3.get('status') if result3 else 'N/A'}, ok={result3.get('ok') if result3 else 'N/A'}")
    
    if result3 and result3.get("ok"):
        print("  ✅ GET /web/shelf/recent 成功！")
        return True

    # 所有方案都失败
    print("\n  ⚠️ 所有书架 API 格式都返回失败")
    print(f"  A1 响应: {result1.get('rawText', '')[:100] if result1 else 'N/A'}")
    print(f"  A2 响应: {result2.get('rawText', '')[:100] if result2 else 'N/A'}")
    print(f"  A3 响应: {result3.get('rawText', '')[:100] if result3 else 'N/A'}")
    return False


def test_fetch_chapter_infos(manager: CDPManager, book_id: str = "ce032b305a9bc1ce0b0dd2a"):
    """测试章节池 API。"""
    print(f"\n{'='*60}")
    print(f"📖 测试 2：章节池 API (bookId={book_id})")
    print(f"{'='*60}")

    # 先导航到书籍阅读页
    print(f"\n[步骤 1] 导航到书籍阅读页...")
    manager.navigate(f"https://weread.qq.com/web/reader/{book_id}")
    time.sleep(3)

    # 尝试多种请求格式
    print(f"\n[步骤 2] 尝试不同的章节池 API 格式...")
    
    # 方案 B1: POST /web/book/chapterInfos (原格式)
    print(f"\n  [B1] POST /web/book/chapterInfos (bookId)")
    fetch_b1 = f"""
(async () => {{
    try {{
        const resp = await fetch('/web/book/chapterInfos', {{
            method: 'POST',
            credentials: 'include',
            headers: {{
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                'x-requested-with': 'XMLHttpRequest'
            }},
            body: JSON.stringify({{
                bookId: '{book_id}'
            }})
        }});
        const text = await resp.text();
        try {{
            const data = JSON.parse(text);
            return JSON.stringify({{ok: resp.ok, status: resp.status, data: data}});
        }} catch {{
            return JSON.stringify({{ok: resp.ok, status: resp.status, rawText: text.substring(0, 300)}});
        }}
    }} catch(e) {{
        return JSON.stringify({{error: e.message}});
    }}
}})()
"""
    result_b1 = manager.evaluate_js(fetch_b1, timeout=15)
    print(f"  结果: status={result_b1.get('status') if result_b1 else 'N/A'}, ok={result_b1.get('ok') if result_b1 else 'N/A'}")
    if result_b1 and result_b1.get("ok"):
        data = result_b1.get("data", {})
        print(f"  响应: errCode={data.get('errCode')}, errMsg={data.get('errMsg')}")
        if data.get("errCode") == 0:
            chapters = data.get("chapters") or data.get("chapterInfos") or []
            print(f"  ✅ 章节池 API 成功！章节数: {len(chapters)}")
            for ch in chapters[:5] if isinstance(chapters, list) else []:
                print(f"    - chapterUid={ch.get('chapterUid', '?')}, title={ch.get('title', '?')}")
            return True

    # 方案 B2: POST /web/book/chapterInfos (book_id 下划线格式)
    print(f"\n  [B2] POST /web/book/chapterInfos (book_id)")
    fetch_b2 = f"""
(async () => {{
    try {{
        const resp = await fetch('/web/book/chapterInfos', {{
            method: 'POST',
            credentials: 'include',
            headers: {{
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                'x-requested-with': 'XMLHttpRequest'
            }},
            body: JSON.stringify({{
                book_id: '{book_id}'
            }})
        }});
        const text = await resp.text();
        try {{
            const data = JSON.parse(text);
            return JSON.stringify({{ok: resp.ok, status: resp.status, data: data}});
        }} catch {{
            return JSON.stringify({{ok: resp.ok, status: resp.status, rawText: text.substring(0, 300)}});
        }}
    }} catch(e) {{
        return JSON.stringify({{error: e.message}});
    }}
}})()
"""
    result_b2 = manager.evaluate_js(fetch_b2, timeout=15)
    print(f"  结果: status={result_b2.get('status') if result_b2 else 'N/A'}, ok={result_b2.get('ok') if result_b2 else 'N/A'}")
    if result_b2 and result_b2.get("ok"):
        data = result_b2.get("data", {})
        print(f"  响应: errCode={data.get('errCode')}, errMsg={data.get('errMsg')}")
        if data.get("errCode") == 0:
            chapters = data.get("chapters") or data.get("chapterInfos") or []
            print(f"  ✅ 章节池 API 成功！章节数: {len(chapters)}")
            return True

    # 方案 B3: POST /web/book/chapterInfos (带 chapters 数组参数)
    print(f"\n  [B3] POST /web/book/chapterInfos (带 chapters 参数)")
    fetch_b3 = f"""
(async () => {{
    try {{
        const resp = await fetch('/web/book/chapterInfos', {{
            method: 'POST',
            credentials: 'include',
            headers: {{
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                'x-requested-with': 'XMLHttpRequest'
            }},
            body: JSON.stringify({{
                bookId: '{book_id}',
                chapters: []
            }})
        }});
        const text = await resp.text();
        try {{
            const data = JSON.parse(text);
            return JSON.stringify({{ok: resp.ok, status: resp.status, data: data}});
        }} catch {{
            return JSON.stringify({{ok: resp.ok, status: resp.status, rawText: text.substring(0, 300)}});
        }}
    }} catch(e) {{
        return JSON.stringify({{error: e.message}});
    }}
}})()
"""
    result_b3 = manager.evaluate_js(fetch_b3, timeout=15)
    print(f"  结果: status={result_b3.get('status') if result_b3 else 'N/A'}, ok={result_b3.get('ok') if result_b3 else 'N/A'}")
    if result_b3 and result_b3.get("ok"):
        data = result_b3.get("data", {})
        print(f"  响应: errCode={data.get('errCode')}, errMsg={data.get('errMsg')}")
        if data.get("errCode") == 0:
            chapters = data.get("chapters") or data.get("chapterInfos") or []
            print(f"  ✅ 章节池 API 成功！章节数: {len(chapters)}")
            return True

    # 方案 B4: GET /web/book/chapterInfos
    print(f"\n  [B4] GET /web/book/chapterInfos")
    fetch_b4 = f"""
(async () => {{
    try {{
        const resp = await fetch('/web/book/chapterInfos?bookId={book_id}', {{
            method: 'GET',
            credentials: 'include',
            headers: {{
                'Accept': 'application/json, text/plain, */*',
                'x-requested-with': 'XMLHttpRequest'
            }}
        }});
        const text = await resp.text();
        try {{
            const data = JSON.parse(text);
            return JSON.stringify({{ok: resp.ok, status: resp.status, data: data}});
        }} catch {{
            return JSON.stringify({{ok: resp.ok, status: resp.status, rawText: text.substring(0, 300)}});
        }}
    }} catch(e) {{
        return JSON.stringify({{error: e.message}});
    }}
}})()
"""
    result_b4 = manager.evaluate_js(fetch_b4, timeout=15)
    print(f"  结果: status={result_b4.get('status') if result_b4 else 'N/A'}, ok={result_b4.get('ok') if result_b4 else 'N/A'}")
    if result_b4 and result_b4.get("ok"):
        data = result_b4.get("data", {})
        print(f"  响应: errCode={data.get('errCode')}, errMsg={data.get('errMsg')}")
        if data.get("errCode") == 0:
            chapters = data.get("chapters") or data.get("chapterInfos") or []
            print(f"  ✅ 章节池 API 成功！章节数: {len(chapters)}")
            return True

    # 所有方案都失败
    print("\n  ⚠️ 所有章节池 API 格式都返回失败")
    for name, result in [("B1", result_b1), ("B2", result_b2), ("B3", result_b3), ("B4", result_b4)]:
        if result:
            data = result.get("data", {})
            err_code = data.get("errCode", "N/A")
            err_msg = data.get("errMsg", result.get("rawText", "")[:100])
            print(f"  {name}: HTTP={result.get('status')}, errCode={err_code}, errMsg={err_msg}")
    return False


def test_dom_bookshelf_extraction(manager: CDPManager):
    """从 DOM 提取书架书籍信息（优先使用 DOM URL 解析，补充 __INITIAL_STATE__）。"""
    print(f"\n{'='*60}")
    print("🎯 提取书架信息")
    print(f"{'='*60}")

    # 确保在书架页
    manager.navigate("https://weread.qq.com/web/shelf")
    time.sleep(2)
    
    books_data = []
    
    # 方案 1：从 DOM 链接提取（最可靠，直接获取 webBookId）
    print("\n[方案 1] 从 DOM 链接提取 webBookId...")
    dom_code = """
(() => {
    const links = document.querySelectorAll('a[href*="/web/reader/"]');
    const books = [];
    const seen = new Set();
    
    for (let i = 0; i < links.length && books.length < 30; i++) {
        const href = links[i].getAttribute('href') || '';
        // 从 URL 提取 webBookId：/web/reader/{webBookId}
        const match = href.match(/\\/reader\\/([a-zA-Z0-9]+)/);
        if (match && !seen.has(match[1])) {
            const webBookId = match[1];
            seen.add(webBookId);
            const title = links[i].textContent.trim().substring(0, 50);
            books.push({
                title: title,
                webBookId: webBookId,
                url: href
            });
        }
    }
    return JSON.stringify({count: books.length, books: books});
})()
"""
    dom_result = manager.evaluate_js(dom_code, timeout=10)
    if dom_result and dom_result.get('count', 0) > 0:
        for book in dom_result.get("books", []):
            books_data.append({
                'title': book.get('title', ''),
                'bookId': '',  # DOM 提取没有 bookId
                'webBookId': book.get('webBookId', ''),
                'author': '',
                'source': 'DOM'
            })
        print(f"  ✅ DOM 提取到 {len(books_data)} 本书籍")
    
    # 方案 2：从 __INITIAL_STATE__ 补充 bookId
    print("\n[方案 2] 从 __INITIAL_STATE__ 补充 bookId...")
    init_data = extract_books_from_initial_state(manager)
    if init_data:
        # 构建 webBookId -> bookId 的映射
        web_to_book = {}
        if init_data.get('sampleRawBooks'):
            for book in init_data['sampleRawBooks']:
                book_id = book.get('bookId', '')
                title = book.get('title', '')
                if book_id and title:
                    web_to_book[title] = book_id
        
        # 更新已有书籍的 bookId
        for book in books_data:
            if book['title'] in web_to_book:
                book['bookId'] = web_to_book[book['title']]
                book['source'] = 'DOM+INIT'
        
        # 如果 DOM 提取的书籍没有 bookId，尝试用 create_id 生成
        for book in books_data:
            if not book['bookId'] and book['webBookId']:
                # 无法从 webBookId 反推 bookId
                book['bookId'] = ''
    
    # 方案 3：如果 DOM 提取失败，直接从 __INITIAL_STATE__ 获取
    if not books_data and init_data:
        print("\n[方案 3] 直接从 __INITIAL_STATE__ 获取...")
        if init_data.get('sampleRawBooks'):
            for book in init_data['sampleRawBooks']:
                book_id = book.get('bookId', '')
                # 尝试用 create_id 生成 webBookId（可能不准确）
                webBookId = create_id(book_id) if book_id and book_id.isdigit() else ''
                books_data.append({
                    'title': book.get('title', ''),
                    'bookId': book_id,
                    'webBookId': webBookId,
                    'author': book.get('author', ''),
                    'source': 'rawBooks'
                })
        print(f"  ✅ __INITIAL_STATE__ 提取到 {len(books_data)} 本书籍")
    
    # 输出结果
    print(f"\n📚 共提取到 {len(books_data)} 本书籍：")
    for book in books_data[:10]:
        print(f"  - {book['title']} (webBookId={book['webBookId'][:20]}..., bookId={book.get('bookId', '?')})")
    
    # 存储到全局 state
    global state
    if books_data:
        state['books'] = books_data
        print(f"\n✅ 书架提取完成！")
        return len(books_data)
    else:
        print("❌ 书架提取失败")
        return 0


def test_dom_chapter_extraction(manager: CDPManager, book_id: str = "ce032b305a9bc1ce0b0dd2a"):
    """提取章节信息（优先使用 __INITIAL_STATE__）。"""
    print(f"\n{'='*60}")
    print(f"📖 提取章节信息 (webBookId={book_id})")
    print(f"{'='*60}")

    chapters_data = []
    
    # 方案 1：尝试从阅读页 __INITIAL_STATE__ 解析
    print(f"\n[方案 1] 导航到阅读页并从 __INITIAL_STATE__ 解析...")
    manager.navigate(f"https://weread.qq.com/web/reader/{book_id}")
    time.sleep(3)
    
    # 尝试从 __INITIAL_STATE__ 获取章节
    init_chapters_code = """
(() => {
    if (!window.__INITIAL_STATE__) {
        return JSON.stringify({success: false, error: '__INITIAL_STATE__ not found'});
    }
    
    const state = window.__INITIAL_STATE__;
    const result = {
        hasChapters: !!(state.chapters || state.chapterInfos || state.readingChapter),
        chapters: [],
        chapterCount: 0
    };
    
    // 尝试从不同路径获取章节
    if (state.chapters && Array.isArray(state.chapters)) {
        result.chapters = state.chapters.map((ch, idx) => ({
            chapterUid: ch.chapterUid || ch.uid || idx,
            title: ch.title || ch.chapterTitle || '',
            level: ch.level || 0,
            wordCount: ch.wordCount || 0
        }));
        result.chapterCount = result.chapters.length;
    } else if (state.chapterInfos && Array.isArray(state.chapterInfos)) {
        result.chapters = state.chapterInfos.map(ch => ({
            chapterUid: ch.chapterUid || ch.uid,
            title: ch.title || '',
            level: ch.level || 0
        }));
        result.chapterCount = result.chapters.length;
    }
    
    // 检查其他可能的路径
    if (result.chapterCount === 0) {
        // 检查 window 对象中的其他可能位置
        const possiblePaths = ['chapterList', 'catalog', 'bookCatalog', 'chapterTree'];
        for (const path of possiblePaths) {
            if (state[path]) {
                result[path] = true;
                if (Array.isArray(state[path])) {
                    result.chapterCount = state[path].length;
                }
            }
        }
    }
    
    return JSON.stringify({
        success: true, 
        data: result,
        stateKeys: Object.keys(state).slice(0, 30)
    });
})()
"""
    
    init_result = manager.evaluate_js(init_chapters_code, timeout=10)
    if init_result and init_result.get('success'):
        data = init_result.get('data', {})
        print(f"  __INITIAL_STATE__ 中有章节数据: {data.get('chapterCount', 0)} 章")
        print(f"  state keys: {init_result.get('stateKeys', [])[:10]}")
        
        if data.get('chapters'):
            chapters_data = data['chapters']
            print(f"  ✅ 从 __INITIAL_STATE__ 获取到 {len(chapters_data)} 个章节")
            for ch in chapters_data[:5]:
                print(f"    - chapterUid={ch.get('chapterUid', '?')}, title={ch.get('title', '?')[:30]}, level={ch.get('level', '?')}")
    
    # 方案 2：从页面内联脚本提取章节配置
    if not chapters_data:
        print(f"\n[方案 2] 从内联脚本提取章节配置...")
        inline_code = """
(() => {
    const scripts = document.querySelectorAll('script:not([src])');
    const results = [];
    
    // 查找包含 chapterUid 的脚本
    for (const script of scripts) {
        const text = script.textContent || '';
        if (!text || text.length < 100) continue;
        
        // 查找 chapterUid 模式
        const uidPattern = /chapterUid['\"]?\\s*[:=]\\s*['\"]?(\\d+)/g;
        let match;
        const foundUids = new Set();
        
        while ((match = uidPattern.exec(text)) !== null) {
            foundUids.add(match[1]);
        }
        
        if (foundUids.size > 0) {
            results.push({
                scriptLen: text.length,
                uidCount: foundUids.size,
                sampleUids: Array.from(foundUids).slice(0, 5),
                preview: text.substring(0, 200)
            });
        }
        
        if (results.length >= 3) break;
    }
    
    return JSON.stringify({
        found: results.length > 0,
        scriptsWithChapters: results,
        totalScripts: scripts.length
    });
})()
"""
        inline_result = manager.evaluate_js(inline_code, timeout=10)
        if inline_result and inline_result.get('found'):
            print(f"  找到 {len(inline_result.get('scriptsWithChapters', []))} 个包含章节的脚本")
            for script in inline_result.get('scriptsWithChapters', [])[:2]:
                print(f"    脚本长度: {script['scriptLen']}, UID 数量: {script['uidCount']}")
                print(f"    示例 UIDs: {script['sampleUids']}")
    
    # 方案 3：DOM 提取目录
    if not chapters_data:
        print(f"\n[方案 3] DOM 提取目录...")
        # 尝试点击目录按钮
        expand_code = """
(async () => {
    try {
        const catalogBtn = document.querySelector('.readerCatalog, .readerCatalogBtn, [class*="catalog"]');
        if (catalogBtn) {
            catalogBtn.click();
            await new Promise(r => setTimeout(r, 1000));
        }
        
        // 查找目录面板
        const panel = document.querySelector('.catalog-panel, .catalogPanel') || 
                      document.querySelector('[class*="catalog-panel"], [class*="CatalogPanel"]') ||
                      document;
        
        const links = panel.querySelectorAll('a[href*="/reader/"]');
        const chapters = [];
        const seen = new Set();
        
        for (const link of links) {
            const href = link.getAttribute('href') || '';
            const match = href.match(/reader\\/[^/]+\\/(\\d+)/);
            if (match && !seen.has(match[1])) {
                seen.add(match[1]);
                chapters.push({
                    chapterUid: match[1],
                    title: link.textContent.trim().substring(0, 50)
                });
            }
        }
        
        return JSON.stringify({success: true, count: chapters.length, chapters: chapters});
    } catch(e) {
        return JSON.stringify({success: false, error: e.message});
    }
})()
"""
        dom_result = manager.evaluate_js(expand_code, timeout=15)
        if dom_result and dom_result.get('count', 0) > 0:
            chapters_data = dom_result.get('chapters', [])
            print(f"  ✅ DOM 提取到 {len(chapters_data)} 个章节")
            for ch in chapters_data[:5]:
                print(f"    - chapterUid={ch.get('chapterUid', '?')}, title={ch.get('title', '?')[:30]}")
    
    # 存储到全局 state
    global state
    if chapters_data:
        state['chapters'][book_id] = {
            'bookId': book_id,
            'chapters': chapters_data,
            'count': len(chapters_data),
            'source': 'mixed'
        }
        print(f"\n✅ 章节提取完成！共 {len(chapters_data)} 个章节")
        return len(chapters_data)
    else:
        print("❌ 章节提取失败")
        return 0


def analyze_page_deep(manager: CDPManager, target_url: str = ""):
    """页面深度分析 - 提取页面上的所有信息。"""
    print(f"\n{'='*60}")
    print("🔍 页面深度分析")
    print(f"{'='*60}")

    # 如果指定了 URL，先导航
    if target_url:
        print(f"\n[步骤 0] 导航到 {target_url}...")
        manager.navigate(target_url)
        time.sleep(3)

    # 获取当前页面 URL
    current_url = manager.evaluate_js("location.href")
    print(f"\n📄 当前页面: {current_url}")

    # 1. 页面基本信息
    print(f"\n{'='*60}")
    print("📋 1. 页面基本信息")
    print(f"{'='*60}")
    
    basic_info_code = """
(() => {
    return JSON.stringify({
        url: location.href,
        title: document.title,
        domain: location.domain,
        pathname: location.pathname,
        search: location.search,
        hash: location.hash,
        bodyTextLength: document.body ? document.body.innerText.length : 0,
        bodyHTML_Length: document.body ? document.body.innerHTML.length : 0,
        scriptsCount: document.querySelectorAll('script').length,
        stylesheetsCount: document.querySelectorAll('link[rel="stylesheet"]').length,
        imagesCount: document.querySelectorAll('img').length,
        formsCount: document.querySelectorAll('form').length,
        iframesCount: document.querySelectorAll('iframe').length,
    });
})()
"""
    basic_info = manager.evaluate_js(basic_info_code)
    if basic_info:
        # 处理可能的 _raw 格式
        if "_raw" in basic_info:
            import json as _json
            try:
                basic_info = _json.loads(basic_info["_raw"])
            except Exception:
                basic_info = None
        
        if basic_info and isinstance(basic_info, dict):
            for key, value in basic_info.items():
                print(f"  {key}: {value}")

    # 2. 所有链接
    print(f"\n{'='*60}")
    print("🔗 2. 页面所有链接")
    print(f"{'='*60}")
    
    links_code = """
(() => {
    const links = document.querySelectorAll('a[href]');
    const result = [];
    for (const link of links) {
        const href = link.getAttribute('href') || '';
        const text = link.textContent.trim().substring(0, 50);
        // 只收集有意义的链接（排除 javascript:, # 等）
        if (href && !href.startsWith('javascript:') && !href.startsWith('#')) {
            result.push({
                href: href,
                text: text,
                hasReader: href.includes('/web/reader/'),
                hasShelf: href.includes('/web/shelf'),
                isExternal: !href.startsWith('/') && !href.startsWith(location.origin)
            });
        }
        if (result.length >= 50) break;  // 最多 50 条
    }
    return JSON.stringify({totalLinks: links.length, validLinks: result.length, links: result});
})()
"""
    links_data = manager.evaluate_js(links_code)
    if links_data:
        # 处理可能的 _raw 格式
        if "_raw" in links_data:
            import json as _json
            try:
                links_data = _json.loads(links_data["_raw"])
            except Exception:
                links_data = None
        
        if links_data and isinstance(links_data, dict):
            print(f"  总链接数: {links_data.get('totalLinks', '?')}")
            print(f"  有效链接数: {links_data.get('validLinks', '?')}")
            print(f"\n  前 20 条链接:")
            for i, link in enumerate(links_data.get('links', [])[:20]):
                if isinstance(link, dict):
                    marker = "📖" if link.get('hasReader') else ("📚" if link.get('hasShelf') else "  ")
                    print(f"    {marker} [{i+1}] {link.get('text', '')[:40]} -> {link.get('href', '')[:80]}")

    # 3. 所有带 data 属性的元素
    print(f"\n{'='*60}")
    print("🏷️ 3. 带 data-* 属性的元素")
    print(f"{'='*60}")
    
    data_attrs_code = """
(() => {
    const elements = document.querySelectorAll('[data-chapter-uid], [data-chapterUid], [data-book-id], [data-bookId], [data-uid], [data-id], [data-vid]');
    const result = [];
    for (const el of elements) {
        const tagName = el.tagName.toLowerCase();
        const className = (el.className || '').toString().substring(0, 50);
        const dataAttrs = {};
        for (const attr of el.attributes) {
            if (attr.name.startsWith('data-')) {
                dataAttrs[attr.name] = attr.value.substring(0, 50);
            }
        }
        if (Object.keys(dataAttrs).length > 0) {
            result.push({
                tag: tagName,
                class: className,
                data: dataAttrs,
                text: el.textContent.trim().substring(0, 50)
            });
        }
        if (result.length >= 30) break;
    }
    return JSON.stringify({totalMatches: elements.length, elements: result});
})()
"""
    data_attrs = manager.evaluate_js(data_attrs_code)
    if data_attrs:
        # 处理可能的 _raw 格式
        if "_raw" in data_attrs:
            import json as _json
            try:
                data_attrs = _json.loads(data_attrs["_raw"])
            except Exception:
                data_attrs = None
        
        if data_attrs and isinstance(data_attrs, dict):
            print(f"  匹配元素数: {data_attrs.get('totalMatches', '?')}")
            for i, el in enumerate(data_attrs.get('elements', [])[:30]):
                if isinstance(el, dict):
                    print(f"    [{i+1}] <{el.get('tag', '?')} class='{el.get('class', '')}'>")
                    for attr_name, attr_val in el.get('data', {}).items():
                        print(f"        {attr_name}='{attr_val}'")
                    print(f"        text: {el.get('text', '')[:40]}")

    # 4. window 对象中的全局变量
    print(f"\n{'='*60}")
    print("🌐 4. window 对象中的全局变量")
    print(f"{'='*60}")
    
    window_vars_code = """
(() => {
    const interestingVars = {};
    const keys = Object.keys(window).filter(k => {
        // 过滤掉浏览器内置对象，只看微信读书相关的
        return k.startsWith('r') || k.startsWith('b') || k.startsWith('c') || 
               k.includes('book') || k.includes('chapter') || k.includes('reader') ||
               k.includes('shelf') || k.includes('catalog') || k.includes('weread') ||
               k.includes('__') || k.startsWith('app') || k.startsWith('config');
    });
    
    for (const key of keys.slice(0, 30)) {
        try {
            const val = window[key];
            if (val !== null && val !== undefined) {
                const type = typeof val;
                let preview = '';
                if (type === 'string') {
                    preview = val.substring(0, 100);
                } else if (type === 'object') {
                    preview = JSON.stringify(val).substring(0, 100);
                } else {
                    preview = String(val).substring(0, 100);
                }
                interestingVars[key] = {type: type, preview: preview};
            }
        } catch(e) {
            // 忽略访问受限的属性
        }
    }
    return JSON.stringify(interestingVars);
})()
"""
    window_vars = manager.evaluate_js(window_vars_code)
    if window_vars:
        # 处理可能的 _raw 格式
        if "_raw" in window_vars:
            import json as _json
            try:
                window_vars = _json.loads(window_vars["_raw"])
            except Exception:
                window_vars = None
        
        if window_vars and isinstance(window_vars, dict):
            for key, info in window_vars.items():
                if isinstance(info, dict) and 'type' in info:
                    print(f"  {key}: ({info['type']}) {info.get('preview', '')[:80]}")

    # 5. 页面上的所有 URL（从 script 和 img src 收集）
    print(f"\n{'='*60}")
    print("🔌 5. 页面引用的 API 端点和资源")
    print(f"{'='*60}")
    
    api_endpoints_code = """
(() => {
    const urls = new Set();
    
    // 从 script 标签收集
    document.querySelectorAll('script[src]').forEach(s => {
        urls.add(s.getAttribute('src'));
    });
    
    // 从 link 标签收集
    document.querySelectorAll('link[href]').forEach(l => {
        const href = l.getAttribute('href') || '';
        if (href.includes('api') || href.includes('web') || href.includes('book') || href.includes('chapter')) {
            urls.add(href);
        }
    });
    
    // 从 data 属性中收集 API URL
    document.querySelectorAll('[data-api], [data-url], [data-endpoint]').forEach(el => {
        ['data-api', 'data-url', 'data-endpoint'].forEach(attr => {
            const val = el.getAttribute(attr);
            if (val) urls.add(val);
        });
    });
    
    // 从 inline script 中提取 URL
    const inlineScripts = document.querySelectorAll('script:not([src])');
    const urlPattern = /['"](/web\/[a-zA-Z0-9_/-]+)['"]/g;
    for (const script of inlineScripts) {
        const text = script.textContent || '';
        let match;
        while ((match = urlPattern.exec(text)) !== null) {
            urls.add(match[1]);
        }
    }
    
    return JSON.stringify({
        scriptCount: document.querySelectorAll('script[src]').length,
        inlineScriptCount: inlineScripts.length,
        apiEndpoints: Array.from(urls).filter(u => u.includes('/web/') || u.includes('api')).slice(0, 30),
        allUrls: Array.from(urls).slice(0, 20)
    });
})()
"""
    endpoints = manager.evaluate_js(api_endpoints_code)
    if endpoints:
        # 处理可能的 _raw 格式
        if "_raw" in endpoints:
            print(f"  ⚠️ 返回的是原始字符串，尝试重新解析...")
            import json as _json
            try:
                endpoints = _json.loads(endpoints["_raw"])
            except Exception:
                print(f"  原始内容: {endpoints['_raw'][:200]}")
                endpoints = None
        
        if endpoints and "scriptCount" in endpoints:
            print(f"  外部脚本数: {endpoints['scriptCount']}")
            print(f"  内联脚本数: {endpoints['inlineScriptCount']}")
            print(f"\n  📡 发现的 API 端点:")
            for url in endpoints.get('apiEndpoints', []):
                print(f"    - {url}")
            print(f"\n  📎 所有引用的 URL:")
            for url in endpoints.get('allUrls', []):
                print(f"    - {url}")

    # 6. 内联脚本内容（查找配置和 API）
    print(f"\n{'='*60}")
    print("📜 6. 内联脚本内容分析")
    print(f"{'='*60}")
    
    inline_script_code = """
(() => {
    const scripts = document.querySelectorAll('script:not([src])');
    const results = [];
    
    for (let i = 0; i < scripts.length; i++) {
        const text = scripts[i].textContent || '';
        if (text.length < 50) continue;  // 跳过太短的
        
        // 查找配置对象
        const configPatterns = [
            /window\\.(appConfig|readerConfig|bookConfig)\\s*=\\s*\\{[^}]{50,}\\}/,
            /const\\s+(config|CONFIG|options)\\s*=\\s*\\{[^}]{50,}\\}/,
            /\"(bookId|book_id|chapterUid|chapter_id)\"\\s*:\\s*\"[^\"]+\"/g,
            /\"(bookId|book_id|chapterUid|chapter_id)\"\\s*:\\s*\\d+/g,
        ];
        
        let matches = [];
        for (const pattern of configPatterns) {
            let match;
            while ((match = pattern.exec(text)) !== null) {
                matches.push(match[0].substring(0, 100));
                if (matches.length >= 10) break;
            }
            if (matches.length >= 10) break;
        }
        
        if (matches.length > 0) {
            results.push({
                scriptIndex: i,
                scriptLength: text.length,
                matches: matches,
                preview: text.substring(0, 200)
            });
        }
        
        if (results.length >= 5) break;
    }
    
    return JSON.stringify({foundScripts: results.length, scripts: results});
})()
"""
    script_analysis = manager.evaluate_js(inline_script_code)
    if script_analysis:
        # 处理可能的 _raw 格式
        if "_raw" in script_analysis:
            import json as _json
            try:
                script_analysis = _json.loads(script_analysis["_raw"])
            except Exception:
                script_analysis = None
        
        if script_analysis and isinstance(script_analysis, dict):
            if script_analysis.get('foundScripts', 0) > 0:
                print(f"  发现 {script_analysis['foundScripts']} 个包含配置的脚本")
                for i, script in enumerate(script_analysis.get('scripts', [])):
                    if isinstance(script, dict):
                        print(f"\n  脚本 #{script.get('scriptIndex', '?')} (长度: {script.get('scriptLength', '?')})")
                        print(f"    匹配的配置:")
                        for match in script.get('matches', [])[:5]:
                            print(f"      {match}")
                        print(f"    预览: {script.get('preview', '')[:100]}...")
            else:
                print("  未在脚本中找到明显的配置对象")

    # 7. 页面 DOM 结构概览
    print(f"\n{'='*60}")
    print("🏗️ 7. 页面 DOM 结构概览")
    print(f"{'='*60}")
    
    dom_structure_code = """
(() => {
    function getStructure(element, depth = 0) {
        if (depth > 4) return null;  // 限制深度
        const info = {
            tag: element.tagName.toLowerCase(),
            class: (element.className || '').toString().substring(0, 30),
            id: element.id || '',
            children: []
        };
        
        // 只显示有意义的子元素（限制数量）
        const children = element.children;
        for (let i = 0; i < Math.min(children.length, 8); i++) {
            const child = getStructure(children[i], depth + 1);
            if (child) info.children.push(child);
        }
        
        return info;
    }
    
    // 获取 body 下的主要结构
    const body = document.body;
    const structure = [];
    for (const child of body.children) {
        const info = {
            tag: child.tagName.toLowerCase(),
            class: (child.className || '').toString().substring(0, 40),
            id: child.id || '',
            textPreview: child.textContent.trim().substring(0, 50),
            childCount: child.children.length
        };
        structure.push(info);
    }
    
    return JSON.stringify({bodyChildren: structure.length, structure: structure.slice(0, 15)});
})()
"""
    dom_structure = manager.evaluate_js(dom_structure_code)
    if dom_structure:
        # 处理可能的 _raw 格式
        if "_raw" in dom_structure:
            import json as _json
            try:
                dom_structure = _json.loads(dom_structure["_raw"])
            except Exception:
                dom_structure = None
        
        if dom_structure and isinstance(dom_structure, dict):
            print(f"  Body 直接子元素数: {dom_structure.get('bodyChildren', '?')}")
            print(f"\n  主要结构:")
            for i, elem in enumerate(dom_structure.get('structure', [])[:10]):
                if isinstance(elem, dict):
                    print(f"    [{i+1}] <{elem.get('tag', '?')}> id='{elem.get('id', '')}' class='{elem.get('class', '')}'")
                    print(f"        子元素数: {elem.get('childCount', '?')}, 文本: {elem.get('textPreview', '')[:40]}")

    # 保存分析结果
    print(f"\n{'='*60}")
    print("💾 保存分析结果")
    print(f"{'='*60}")
    
    analysis_result = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "url": current_url,
        "basic_info": basic_info,
        "links": links_data,
        "data_attributes": data_attrs,
        "window_variables": window_vars,
        "api_endpoints": endpoints,
        "dom_structure": dom_structure,
    }
    
    try:
        Path("cdp_page_analysis.json").write_text(
            json.dumps(analysis_result, ensure_ascii=False, indent=2, default=str), 
            encoding="utf-8"
        )
        print(f"  ✅ 分析结果已保存: cdp_page_analysis.json")
    except Exception as e:
        print(f"  ❌ 保存失败: {e}")

    print(f"\n{'='*60}")
    print("✅ 页面深度分析完成！")
    print(f"{'='*60}")
    
    return analysis_result


def test_save_all_data(manager: CDPManager, cookies: list[dict], books: list, chapters: dict):
    """保存所有测试数据到文件。"""
    print(f"\n{'='*60}")
    print("💾 保存测试数据")
    print(f"{'='*60}")

    try:
        # 1. 保存 Cookie
        cookie_data = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(cookies),
            "cookies": [
                {k: c.get(k) for k in ["name", "domain", "path", "value", "httpOnly", "secure"] if k in c}
                for c in cookies
            ],
        }
        Path("cdp_cookies.json").write_text(json.dumps(cookie_data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ Cookie 已保存: cdp_cookies.json ({len(cookies)} 条)")

        # 2. 保存书籍信息
        Path("cdp_books.json").write_text(json.dumps(books, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ 书籍信息已保存: cdp_books.json ({len(books)} 本)")

        # 3. 保存章节池
        Path("cdp_chapters.json").write_text(json.dumps(chapters, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ 章节池已保存: cdp_chapters.json")

        return True
    except Exception as exc:
        print(f"❌ 保存失败：{exc}")
        return False


# ============================================================
# 4. Qt 应用（简单 UI）
# ============================================================
def run_app():
    """运行 Qt 应用。"""
    from PySide6.QtCore import Qt, QTimer, QUrl, Signal, QObject
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QPushButton, QLabel, QTextBrowser, QMessageBox
    )
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEngineProfile

    app = QApplication(sys.argv)
    app.setApplicationName("CDPStandaloneTest")

    # 创建 persistent profile
    profile = QWebEngineProfile("cdp-standalone-test")
    print(f"[Qt] QWebEngineProfile: storageName={profile.storageName()}")

    # CDP 管理器
    cdp_mgr = CDPManager(CDP_PORT)

    # 状态变量
    state = {
        "login_completed": False,
        "cookies": [],
        "books": [],
        "chapters": {},
        "test_results": {},
    }

    # 创建主窗口
    window = QMainWindow()
    window.setWindowTitle("🔬 Chrome + CDP 方案测试程序")
    window.resize(1200, 800)

    central = QWidget()
    window.setCentralWidget(central)
    layout = QVBoxLayout(central)

    # Skill 配置区
    skill_layout = QHBoxLayout()
    skill_layout.addWidget(QLabel("🔑 Skill API Key:"))
    api_key_input = QLineEdit()
    api_key_input.setPlaceholderText("wrk-xxxxxxxxxxxxxxxx")
    api_key_input.setFixedWidth(280)
    skill_layout.addWidget(api_key_input)
    
    btn_skill_shelf = QPushButton("📚 Skill 书架")
    btn_skill_shelf.setStyleSheet("background-color: #009688; color: white; padding: 6px 12px; font-weight: bold;")
    
    btn_skill_chapters = QPushButton("📖 Skill 章节池")
    btn_skill_chapters.setStyleSheet("background-color: #00BCD4; color: white; padding: 6px 12px; font-weight: bold;")
    
    skill_layout.addWidget(btn_skill_shelf)
    skill_layout.addWidget(btn_skill_chapters)
    skill_layout.addStretch()
    layout.addLayout(skill_layout)
    
    # 顶部按钮区
    btn_layout = QHBoxLayout()

    btn_connect = QPushButton("🔌 连接 CDP")
    btn_connect.setStyleSheet("background-color: #2196F3; color: white; padding: 8px 16px; font-weight: bold;")
    
    btn_get_cookies = QPushButton("🍪 获取 Cookie")
    btn_get_cookies.setStyleSheet("background-color: #4CAF50; color: white; padding: 8px 16px; font-weight: bold;")

    btn_test_shelf = QPushButton("📚 测试书架 API")
    btn_test_shelf.setStyleSheet("background-color: #FF9800; color: white; padding: 8px 16px; font-weight: bold;")

    btn_test_chapters = QPushButton("📖 测试章节池 API")
    btn_test_chapters.setStyleSheet("background-color: #9C27B0; color: white; padding: 8px 16px; font-weight: bold;")

    btn_test_dom_shelf = QPushButton("🎯 DOM 提取书架")
    btn_test_dom_shelf.setStyleSheet("background-color: #607D8B; color: white; padding: 8px 16px; font-weight: bold;")

    btn_test_dom_chapters = QPushButton("🎯 DOM 提取章节")
    btn_test_dom_chapters.setStyleSheet("background-color: #00897B; color: white; padding: 8px 16px; font-weight: bold;")

    btn_analyze_page = QPushButton("🔍 页面深度分析")
    btn_analyze_page.setStyleSheet("background-color: #673AB7; color: white; padding: 8px 16px; font-weight: bold;")

    btn_save = QPushButton("💾 保存数据")
    btn_save.setStyleSheet("background-color: #795548; color: white; padding: 8px 16px; font-weight: bold;")

    btn_reset = QPushButton("🔄 恢复按钮")
    btn_reset.setStyleSheet("background-color: #607D8B; color: white; padding: 8px 16px; font-weight: bold;")

    btn_run_all = QPushButton("🚀 一键测试全部")
    btn_run_all.setStyleSheet("background-color: #F44336; color: white; padding: 8px 16px; font-weight: bold;")

    btn_layout.addWidget(btn_connect)
    btn_layout.addWidget(btn_get_cookies)
    btn_layout.addWidget(btn_test_shelf)
    btn_layout.addWidget(btn_test_chapters)
    btn_layout.addWidget(btn_test_dom_shelf)
    btn_layout.addWidget(btn_test_dom_chapters)
    btn_layout.addWidget(btn_analyze_page)
    btn_layout.addWidget(btn_save)
    btn_layout.addWidget(btn_reset)
    btn_layout.addStretch()
    btn_layout.addWidget(btn_run_all)
    layout.addLayout(btn_layout)

    # 状态标签
    status_label = QLabel("就绪：请先点击「连接 CDP」开始测试")
    status_label.setStyleSheet("padding: 8px; background: #E3F2FD; font-weight: bold;")
    layout.addWidget(status_label)

    # 浏览器
    view = QWebEngineView(profile, window)
    view.load(QUrl(WEREAD_URL))
    view.setMinimumHeight(400)
    layout.addWidget(view, 1)

    # 日志面板
    log_text = QTextBrowser()
    log_text.setMaximumHeight(200)
    layout.addWidget(log_text)

    # 日志输出重定向
    class LogRedirector:
        def __init__(self, text_browser):
            self.text_browser = text_browser
            self.buffer = ""

        def write(self, text):
            self.buffer += text
            if "\n" in text:
                lines = self.buffer.split("\n")
                self.buffer = lines[-1]
                for line in lines[:-1]:
                    if line.strip():
                        self.text_browser.append(line)
                        # 限制日志行数
                        doc = self.text_browser.document()
                        if doc.blockCount() > 200:
                            cursor = self.text_browser.textCursor()
                            cursor.movePosition(cursor.Start)
                            cursor.select(cursor.BlockUnderCursor)
                            cursor.removeSelectedText()
                            cursor.deleteChar()

        def flush(self):
            if self.buffer.strip():
                self.text_browser.append(self.buffer)
                self.buffer = ""

    log_redirector = LogRedirector(log_text)
    sys.stdout = log_redirector

    print("[UI] 日志重定向已设置")

    # 通用超时保护辅助函数
    def start_with_timeout(btn, original_text, worker_fn, timeout_sec=30):
        """启动工作线程并添加超时保护。"""
        # 启动工作线程
        threading.Thread(target=worker_fn, daemon=True).start()
        
        # 超时保护
        def timeout_protect():
            import time
            time.sleep(timeout_sec)
            if btn.text().startswith("⏳"):
                print(f"[WARN] 操作超时（{timeout_sec}秒），自动恢复按钮: {original_text}")
                QTimer.singleShot(0, lambda: btn.setText(original_text))
                QTimer.singleShot(0, lambda: btn.setEnabled(True))
                QTimer.singleShot(0, lambda: status_label.setText("⚠️ 操作超时，已恢复按钮"))
        
        threading.Thread(target=timeout_protect, daemon=True).start()

    # 按钮事件
    def on_connect():
        print("\n[DEBUG] 点击了「连接 CDP」按钮")
        btn_connect.setEnabled(False)
        btn_connect.setText("⏳ 连接中...")
        status_label.setText("🔌 正在连接 CDP...")
        
        def do_connect():
            try:
                success = cdp_mgr.connect()
                QTimer.singleShot(0, lambda: on_connect_done(success))
            except Exception as e:
                print(f"[ERROR] 连接 CDP 失败: {e}")
                import traceback
                traceback.print_exc()
                QTimer.singleShot(0, lambda: on_connect_done(False))

        import threading
        threading.Thread(target=do_connect, daemon=True).start()

    def on_connect_done(success):
        print(f"[DEBUG] on_connect_done: success={success}")
        if success:
            btn_get_cookies.setEnabled(True)
            btn_connect.setText("✅ 已连接")
            status_label.setText("✅ CDP 连接成功！请在上方浏览器扫码登录")
            print("\n✅ CDP 连接成功！请扫码登录微信读书")
        else:
            btn_connect.setEnabled(True)
            btn_connect.setText("🔌 连接重试")
            status_label.setText("❌ CDP 连接失败，请检查浏览器是否已启动")

    def on_get_cookies():
        print("\n[DEBUG] 点击了「获取 Cookie」按钮")
        if not cdp_mgr._connected:
            print("⚠️ 请先连接 CDP！")
            status_label.setText("⚠️ 请先点击「连接 CDP」")
            return
        btn_get_cookies.setEnabled(False)
        btn_get_cookies.setText("⏳ 获取中...")
        status_label.setText("🍪 正在获取 Cookie...")
        
        def do_get():
            try:
                cookies = cdp_mgr.get_cookies()
                QTimer.singleShot(0, lambda: on_cookies_done(cookies))
            except Exception as e:
                print(f"[ERROR] 获取 Cookie 失败: {e}")
                import traceback
                traceback.print_exc()
                QTimer.singleShot(0, lambda: on_cookies_done([]))

        # 启动工作线程
        worker = threading.Thread(target=do_get, daemon=True)
        worker.start()
        
        # 超时保护：30秒后自动恢复按钮
        def timeout_protect():
            import time
            time.sleep(30)
            if btn_get_cookies.text() == "⏳ 获取中...":
                print("[WARN] 获取 Cookie 超时（30秒），自动恢复按钮")
                QTimer.singleShot(0, lambda: on_cookies_done([]))
        
        threading.Thread(target=timeout_protect, daemon=True).start()

    def on_cookies_done(cookies):
        state["cookies"] = cookies
        btn_get_cookies.setEnabled(True)
        btn_get_cookies.setText("🍪 获取 Cookie")
        
        if cookies:
            cdp_mgr.print_cookies_summary(cookies)
            
            # 检查登录态
            wr_skey = next((c for c in cookies if c.get("name") == "wr_skey"), {})
            if wr_skey and len(str(wr_skey.get("value", ""))) >= 8:
                state["login_completed"] = True
                status_label.setText("✅ 登录态有效！可以测试书架和章节池 API")
                print("\n✅ 登录态有效！")
            else:
                status_label.setText("⚠️ Cookie 已获取，但登录态无效（请先扫码登录）")
                print("\n⚠️ Cookie 已获取，但登录态无效")
        else:
            status_label.setText("❌ 未获取到 Cookie")
            print("\n❌ 未获取到 Cookie")

    def on_test_shelf():
        print("\n[DEBUG] 点击了「测试书架 API」按钮")
        if not cdp_mgr._connected:
            print("⚠️ 请先连接 CDP！")
            status_label.setText("⚠️ 请先点击「连接 CDP」")
            return
        btn_test_shelf.setEnabled(False)
        btn_test_shelf.setText("⏳ 测试中...")
        status_label.setText("📚 正在测试书架 API...")
        
        def do_test():
            try:
                result = test_fetch_bookshelf(cdp_mgr)
                QTimer.singleShot(0, lambda: on_shelf_done(result))
            except Exception as e:
                print(f"[ERROR] 测试书架 API 失败: {e}")
                import traceback
                traceback.print_exc()
                QTimer.singleShot(0, lambda: on_shelf_done(False))

        start_with_timeout(btn_test_shelf, "📚 测试书架 API", do_test)

    def on_shelf_done(result):
        print(f"[DEBUG] on_shelf_done: result={result}")
        btn_test_shelf.setEnabled(True)
        btn_test_shelf.setText("📚 测试书架 API")
        state["test_results"]["bookshelf"] = result
        btn_save.setEnabled(True)
        status_label.setText(f"📚 书架 API 测试：{'✅ 成功' if result else '❌ 失败'}")

    def on_test_chapters():
        print("\n[DEBUG] 点击了「测试章节池 API」按钮")
        if not cdp_mgr._connected:
            print("⚠️ 请先连接 CDP！")
            status_label.setText("⚠️ 请先点击「连接 CDP」")
            return
        if not state["books"]:
            print("\n⚠️ 请先测试书架 API 或 DOM 提取获取 bookId")
            status_label.setText("⚠️ 请先获取 bookId")
            return
            
        book_id = state["books"][0].get("webBookId", "") if state["books"] else "ce032b305a9bc1ce0b0dd2a"
        btn_test_chapters.setEnabled(False)
        btn_test_chapters.setText("⏳ 测试中...")
        status_label.setText(f"📖 正在测试章节池 API (bookId={book_id})...")
        
        def do_test():
            try:
                result = test_fetch_chapter_infos(cdp_mgr, book_id)
                QTimer.singleShot(0, lambda: on_chapters_done(result))
            except Exception as e:
                print(f"[ERROR] 测试章节池 API 失败: {e}")
                import traceback
                traceback.print_exc()
                QTimer.singleShot(0, lambda: on_chapters_done(False))

        start_with_timeout(btn_test_chapters, "📖 测试章节池 API", do_test)

    def on_chapters_done(result):
        print(f"[DEBUG] on_chapters_done: result={result}")
        btn_test_chapters.setEnabled(True)
        btn_test_chapters.setText("📖 测试章节池 API")
        state["test_results"]["chapters"] = result
        status_label.setText(f"📖 章节池 API 测试：{'✅ 成功' if result else '❌ 失败'}")

    def on_test_dom_shelf():
        print("\n[DEBUG] 点击了「DOM 提取书架」按钮")
        if not cdp_mgr._connected:
            print("⚠️ 请先连接 CDP！")
            status_label.setText("⚠️ 请先点击「连接 CDP」")
            return
        btn_test_dom_shelf.setEnabled(False)
        btn_test_dom_shelf.setText("⏳ 提取中...")
        status_label.setText("🎯 正在从 DOM 提取书架信息...")
        
        def do_test():
            try:
                count = test_dom_bookshelf_extraction(cdp_mgr)
                QTimer.singleShot(0, lambda: on_dom_shelf_done(count))
            except Exception as e:
                print(f"[ERROR] DOM 提取书架失败: {e}")
                import traceback
                traceback.print_exc()
                QTimer.singleShot(0, lambda: on_dom_shelf_done(0))

        start_with_timeout(btn_test_dom_shelf, "🎯 DOM 提取书架", do_test)

    def on_dom_shelf_done(count):
        print(f"[DEBUG] on_dom_shelf_done: count={count}")
        btn_test_dom_shelf.setEnabled(True)
        btn_test_dom_shelf.setText("🎯 DOM 提取书架")
        if count > 0:
            status_label.setText(f"✅ 书架提取成功：获取到 {count} 本书籍")
            print(f"\n📚 提取到 {count} 本书籍")
            # 书籍列表已在 test_dom_bookshelf_extraction 中存储到 state["books"]
            if state.get("books"):
                print(f"  已存储到 state['books']，共 {len(state['books'])} 本")
                for book in state['books'][:5]:
                    print(f"    - {book.get('title', '?')} (bookId={book.get('bookId', '?')}, webBookId={book.get('webBookId', '?')[:20]}...)")
        else:
            status_label.setText("❌ 书架提取失败")

    def on_test_dom_chapters():
        print("\n[DEBUG] 点击了「DOM 提取章节」按钮")
        if not cdp_mgr._connected:
            print("⚠️ 请先连接 CDP！")
            status_label.setText("⚠️ 请先点击「连接 CDP」")
            return
        if not state["books"]:
            print("\n⚠️ 请先提取书架获取书籍")
            status_label.setText("⚠️ 请先提取书架获取书籍")
            return
            
        # 优先使用 webBookId，如果没有则从 bookId 生成
        first_book = state["books"][0]
        webBookId = first_book.get("webBookId", "")
        bookId = first_book.get("bookId", "")
        
        if not webBookId and bookId and bookId.isdigit():
            webBookId = create_id(bookId)
            print(f"  从 bookId={bookId} 生成 webBookId={webBookId}")
        
        if not webBookId:
            webBookId = "ce032b305a9bc1ce0b0dd2a"  # 默认书籍
            print(f"  使用默认 webBookId={webBookId}")
        
        btn_test_dom_chapters.setEnabled(False)
        btn_test_dom_chapters.setText("⏳ 提取中...")
        status_label.setText(f"📖 正在提取章节信息 (webBookId={webBookId})...")
        
        def do_test():
            try:
                count = test_dom_chapter_extraction(cdp_mgr, webBookId)
                QTimer.singleShot(0, lambda: on_dom_chapters_done(count))
            except Exception as e:
                print(f"[ERROR] 章节提取失败: {e}")
                import traceback
                traceback.print_exc()
                QTimer.singleShot(0, lambda: on_dom_chapters_done(0))

        start_with_timeout(btn_test_dom_chapters, "🎯 DOM 提取章节", do_test)

    def on_dom_chapters_done(count):
        print(f"[DEBUG] on_dom_chapters_done: count={count}")
        btn_test_dom_chapters.setEnabled(True)
        btn_test_dom_chapters.setText("🎯 DOM 提取章节")
        if count > 0:
            status_label.setText(f"✅ 章节提取成功：获取到 {count} 个章节")
            print(f"\n📖 提取到 {count} 个章节")
            # 章节数据已在 test_dom_chapter_extraction 中存储到 state["chapters"]
            if state.get("chapters"):
                for book_id, chapter_data in state["chapters"].items():
                    print(f"  书籍 {book_id}: {chapter_data.get('count', 0)} 个章节")
                    chapters = chapter_data.get('chapters', [])
                    for ch in chapters[:3]:
                        print(f"    - chapterUid={ch.get('chapterUid', '?')}, title={ch.get('title', '?')[:30]}")
        else:
            status_label.setText("❌ 章节提取失败")

    def on_save():
        print("\n[DEBUG] 点击了「保存数据」按钮")
        try:
            success = test_save_all_data(
                cdp_mgr,
                state["cookies"],
                state["books"],
                state["chapters"]
            )
            status_label.setText("💾 数据保存成功！" if success else "❌ 数据保存失败")
        except Exception as e:
            print(f"[ERROR] 保存数据失败: {e}")
            import traceback
            traceback.print_exc()
            status_label.setText(f"❌ 保存失败: {e}")

    def on_analyze_page():
        print("\n[DEBUG] 点击了「页面深度分析」按钮")
        if not cdp_mgr._connected:
            print("⚠️ 请先连接 CDP！")
            status_label.setText("⚠️ 请先点击「连接 CDP」")
            return
        btn_analyze_page.setEnabled(False)
        btn_analyze_page.setText("⏳ 分析中...")
        status_label.setText("🔍 正在进行页面深度分析...")
        
        # 获取当前页面 URL 或使用书籍阅读页
        target_url = ""
        if state["books"]:
            book_id = state["books"][0].get("webBookId", "")
            if book_id:
                target_url = f"https://weread.qq.com/web/reader/{book_id}"
        
        def do_analyze():
            try:
                result = analyze_page_deep(cdp_mgr, target_url)
                QTimer.singleShot(0, lambda: on_analyze_done(result))
            except Exception as e:
                print(f"[ERROR] 页面分析失败: {e}")
                import traceback
                traceback.print_exc()
                QTimer.singleShot(0, lambda: on_analyze_done(None))

        start_with_timeout(btn_analyze_page, "🔍 页面深度分析", do_analyze, timeout_sec=60)

    def on_analyze_done(result):
        print(f"[DEBUG] on_analyze_done: result={'成功' if result else '失败'}")
        btn_analyze_page.setEnabled(True)
        btn_analyze_page.setText("🔍 页面深度分析")
        if result:
            status_label.setText("✅ 页面分析完成，详情请查看日志和 cdp_page_analysis.json")
            print("\n✅ 页面分析完成！详细结果已保存到 cdp_page_analysis.json")
        else:
            status_label.setText("❌ 页面分析失败")

    def on_run_all():
        """一键执行所有测试。"""
        btn_run_all.setEnabled(False)
        status_label.setText("🚀 正在执行完整测试流程...")
        print("\n" + "="*60)
        print("🚀 开始完整测试流程")
        print("="*60)
        
        def run_all():
            results = {}
            
            # Step 1: 连接
            print("\n--- Step 1: 连接 CDP ---")
            connect_ok = cdp_mgr.connect()
            results["connect"] = connect_ok
            
            if not connect_ok:
                QTimer.singleShot(0, lambda: on_all_done(results))
                return
            
            # Step 2: 获取 Cookie
            print("\n--- Step 2: 获取 Cookie ---")
            cookies = cdp_mgr.get_cookies()
            state["cookies"] = cookies
            cdp_mgr.print_cookies_summary(cookies)
            results["cookies"] = len(cookies)
            
            # Step 3: 等待登录
            wr_skey = next((c for c in cookies if c.get("name") == "wr_skey"), {})
            if wr_skey and len(str(wr_skey.get("value", ""))) >= 8:
                state["login_completed"] = True
                print("\n✅ 登录态有效！")
            else:
                print("\n⚠️ 请先扫码登录微信读书...")
                # 等待用户登录
                for i in range(30):
                    time.sleep(2)
                    cookies = cdp_mgr.get_cookies()
                    wr_skey = next((c for c in cookies if c.get("name") == "wr_skey"), {})
                    if wr_skey and len(str(wr_skey.get("value", ""))) >= 8:
                        state["login_completed"] = True
                        state["cookies"] = cookies
                        print(f"\n✅ 登录成功！（等待 {i*2} 秒）")
                        break
                    print(f"  等待扫码中... ({i*2+2}s)")
            
            # Step 4: 测试书架 API
            if state["login_completed"]:
                print("\n--- Step 4: 测试书架 API ---")
                shelf_ok = test_fetch_bookshelf(cdp_mgr)
                results["bookshelf"] = "✅ 成功" if shelf_ok else "❌ 失败"
                
                # Step 5: 测试章节池 API
                print("\n--- Step 5: 测试章节池 API ---")
                chapter_ok = test_fetch_chapter_infos(cdp_mgr)
                results["chapters"] = "✅ 成功" if chapter_ok else "❌ 失败"
                
                # Step 6: DOM 提取书架（兜底）
                if not shelf_ok:
                    print("\n--- Step 6: DOM 提取书架（兜底）---")
                    dom_count = test_dom_bookshelf_extraction(cdp_mgr)
                    results["dom_shelf"] = f"✅ {dom_count} 本书" if dom_count > 0 else "❌ 失败"
                    
                    # 获取实际书籍列表
                    if dom_count > 0:
                        dom_books = cdp_mgr.evaluate_js("""
(() => {
    const links = document.querySelectorAll('a[href*="/web/reader/"]');
    const books = [];
    for (let i = 0; i < links.length && books.length < 10; i++) {
        const href = links[i].getAttribute('href') || '';
        const parts = href.split('/');
        const webBookId = parts[parts.length - 1];
        if (webBookId && /^[a-zA-Z0-9]+$/.test(webBookId)) {
            const title = links[i].textContent.trim().substring(0, 30);
            books.push({title: title, webBookId: webBookId, url: href});
        }
    }
    return JSON.stringify(books);
})()
""")
                        if dom_books:
                            state["books"] = dom_books
                
                # Step 7: DOM 提取章节（兜底）
                if not chapter_ok and state["books"]:
                    print("\n--- Step 7: DOM 提取章节（兜底）---")
                    book_id = state["books"][0].get("webBookId", "ce032b305a9bc1ce0b0dd2a")
                    dom_chapter_count = test_dom_chapter_extraction(cdp_mgr, book_id)
                    results["dom_chapters"] = f"✅ {dom_chapter_count} 章节" if dom_chapter_count > 0 else "❌ 失败"
                    
                    if dom_chapter_count > 0:
                        state["chapters"]["dom_extracted"] = {
                            "book_id": book_id,
                            "chapter_count": dom_chapter_count,
                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                        }
                
                # Step 8: 保存数据
                print("\n--- Step 8: 保存数据 ---")
                test_save_all_data(cdp_mgr, state["cookies"], state["books"], state["chapters"])
                results["save"] = "✅ 完成"
            
            QTimer.singleShot(0, lambda: on_all_done(results))

        threading.Thread(target=run_all, daemon=True).start()

    def on_all_done(results):
        btn_run_all.setEnabled(True)
        print("\n" + "="*60)
        print("🎉 完整测试流程完成！")
        print("="*60)
        for key, value in results.items():
            print(f"  {key}: {value}")
        
        # 总结
        success_count = sum(1 for v in results.values() if "✅" in str(v))
        total_count = len(results)
        summary = f"测试完成：{success_count}/{total_count} 项成功"
        status_label.setText(summary)
        print(f"\n📊 {summary}")
        btn_save.setEnabled(True)

    def on_reset_buttons():
        """强制恢复所有按钮到可用状态。"""
        print("\n[DEBUG] 点击了「恢复按钮」")
        buttons = [
            (btn_get_cookies, "🍪 获取 Cookie"),
            (btn_test_shelf, "📚 测试书架 API"),
            (btn_test_chapters, "📖 测试章节池 API"),
            (btn_test_dom_shelf, "🎯 DOM 提取书架"),
            (btn_test_dom_chapters, "🎯 DOM 提取章节"),
            (btn_analyze_page, "🔍 页面深度分析"),
            (btn_save, "💾 保存数据"),
            (btn_skill_shelf, "📚 Skill 书架"),
            (btn_skill_chapters, "📖 Skill 章节池"),
        ]
        for btn, text in buttons:
            btn.setEnabled(True)
            btn.setText(text)
        status_label.setText("✅ 所有按钮已恢复")
        print("✅ 所有按钮已恢复")

    # ============================================================
    # Skill 1.0.5 测试函数
    # ============================================================

    SKILL_GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"

    def call_skill_api(api_name: str, params: dict, timeout: int = 15):
        """调用微信读书 Skill API。"""
        api_key = api_key_input.text().strip()
        if not api_key:
            print("\n⚠️ 请先输入 Skill API Key！")
            status_label.setText("⚠️ 请先输入 Skill API Key")
            return None
        
        if not api_key.startswith("wrk-"):
            print("\n⚠️ API Key 格式错误，应以 wrk- 开头")
            status_label.setText("⚠️ API Key 格式错误")
            return None
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "api_name": api_name,
            "skill_version": "1.0.5",
        }
        payload.update(params)
        
        try:
            print(f"\n🔄 Skill {api_name} 请求: {json.dumps(params, ensure_ascii=False)[:200]}")
            import requests
            r = requests.post(SKILL_GATEWAY_URL, headers=headers, json=payload, timeout=timeout, verify=False)
            print(f"  HTTP {r.status_code} time={r.elapsed.total_seconds():.2f}s")
            
            if r.status_code == 499:
                print("  ⚠️ 触发限流 (499)")
                return None
            if r.status_code == 401 or r.status_code == 403:
                print(f"  ❌ 鉴权失败 (HTTP {r.status_code})")
                return None
            
            data = r.json()
            if isinstance(data, dict) and data.get("errcode", 0) != 0:
                print(f"  ❌ 接口错误: errcode={data.get('errcode')} msg={data.get('errmsg')}")
                return None
            
            if isinstance(data, dict) and "upgrade_info" in data:
                print(f"  ⚠️ Skill 有新版: {data.get('upgrade_info')}")
            
            return data
        except Exception as e:
            print(f"  ❌ 请求异常: {e}")
            return None

    def on_skill_shelf():
        """测试 Skill /shelf/sync 接口。"""
        print("\n[DEBUG] 点击了「Skill 书架」按钮")
        btn_skill_shelf.setEnabled(False)
        btn_skill_shelf.setText("⏳ 请求中...")
        status_label.setText("📚 正在通过 Skill 获取书架...")
        
        def do_test():
            try:
                data = call_skill_api("/shelf/sync", {})
                if data:
                    books = data.get("books") or data.get("data") or []
                    if isinstance(books, list):
                        valid_books = [b for b in books if isinstance(b, dict) and b.get("bookId")]
                        print(f"\n📚 Skill /shelf/sync 返回 {len(valid_books)} 本书籍:")
                        for book in valid_books[:10]:
                            print(f"  - {book.get('title', '?')} (bookId={book.get('bookId', '?')})")
                            state["books"].append({
                                "title": book.get("title", ""),
                                "bookId": book.get("bookId", ""),
                                "webBookId": "",
                                "author": book.get("author", ""),
                                "source": "Skill"
                            })
                        if len(valid_books) > 10:
                            print(f"  ... 还有 {len(valid_books) - 10} 本")
                    QTimer.singleShot(0, lambda: on_skill_shelf_done(True, len(valid_books)))
                else:
                    QTimer.singleShot(0, lambda: on_skill_shelf_done(False, 0))
            except Exception as e:
                print(f"[ERROR] Skill 书架获取失败: {e}")
                import traceback
                traceback.print_exc()
                QTimer.singleShot(0, lambda: on_skill_shelf_done(False, 0))

        def on_skill_shelf_done(success, count):
            btn_skill_shelf.setEnabled(True)
            btn_skill_shelf.setText("📚 Skill 书架")
            if success:
                status_label.setText(f"✅ Skill 书架获取成功：{count} 本书籍")
                print(f"\n✅ Skill 书架获取成功！共 {count} 本书籍")
            else:
                status_label.setText("❌ Skill 书架获取失败")

        start_with_timeout(btn_skill_shelf, "📚 Skill 书架", do_test)

    def on_skill_chapters():
        """测试 Skill /book/chapterinfo 接口。"""
        print("\n[DEBUG] 点击了「Skill 章节池」按钮")
        api_key = api_key_input.text().strip()
        if not api_key:
            print("⚠️ 请先输入 Skill API Key")
            status_label.setText("⚠️ 请先输入 Skill API Key")
            return
        
        if not state["books"]:
            print("⚠️ 请先获取书架")
            status_label.setText("⚠️ 请先获取书架")
            return
        
        # 选择一本书（优先选择有 bookId 的）
        book = None
        for b in state["books"]:
            if b.get("bookId"):
                book = b
                break
        
        if not book:
            print("⚠️ 没有找到有效的 bookId")
            status_label.setText("⚠️ 没有找到有效的 bookId")
            return
        
        book_id = book["bookId"]
        print(f"  选择书籍: {book.get('title')} (bookId={book_id})")
        
        btn_skill_chapters.setEnabled(False)
        btn_skill_chapters.setText("⏳ 请求中...")
        status_label.setText(f"📖 正在获取章节池 (bookId={book_id})...")
        
        def do_test():
            try:
                data = call_skill_api("/book/chapterinfo", {"bookId": book_id})
                if data:
                    chapters = data.get("chapters") or data.get("chapterInfos") or []
                    if isinstance(chapters, list):
                        print(f"\n📖 Skill /book/chapterinfo 返回 {len(chapters)} 个章节:")
                        for ch in chapters[:10]:
                            print(f"  - chapterUid={ch.get('chapterUid', '?')}, title={ch.get('title', '?')[:30]}, level={ch.get('level', '?')}")
                        state["chapters"][book_id] = {
                            "bookId": book_id,
                            "chapters": chapters,
                            "count": len(chapters),
                            "source": "Skill"
                        }
                        if len(chapters) > 10:
                            print(f"  ... 还有 {len(chapters) - 10} 个章节")
                    QTimer.singleShot(0, lambda: on_skill_chapters_done(True, len(chapters)))
                else:
                    QTimer.singleShot(0, lambda: on_skill_chapters_done(False, 0))
            except Exception as e:
                print(f"[ERROR] Skill 章节池获取失败: {e}")
                import traceback
                traceback.print_exc()
                QTimer.singleShot(0, lambda: on_skill_chapters_done(False, 0))

        def on_skill_chapters_done(success, count):
            btn_skill_chapters.setEnabled(True)
            btn_skill_chapters.setText("📖 Skill 章节池")
            if success:
                status_label.setText(f"✅ Skill 章节池获取成功：{count} 个章节")
                print(f"\n✅ Skill 章节池获取成功！共 {count} 个章节")
            else:
                status_label.setText("❌ Skill 章节池获取失败")

        start_with_timeout(btn_skill_chapters, "📖 Skill 章节池", do_test)

    # ============================================================
    # 连接按钮事件
    # ============================================================
    btn_connect.clicked.connect(on_connect)
    btn_get_cookies.clicked.connect(on_get_cookies)
    btn_test_shelf.clicked.connect(on_test_shelf)
    btn_test_chapters.clicked.connect(on_test_chapters)
    btn_test_dom_shelf.clicked.connect(on_test_dom_shelf)
    btn_test_dom_chapters.clicked.connect(on_test_dom_chapters)
    btn_analyze_page.clicked.connect(on_analyze_page)
    btn_save.clicked.connect(on_save)
    btn_reset.clicked.connect(on_reset_buttons)
    btn_run_all.clicked.connect(on_run_all)
    btn_skill_shelf.clicked.connect(on_skill_shelf)
    btn_skill_chapters.clicked.connect(on_skill_chapters)

    # 显示窗口
    window.show()
    print("\n🚀 测试程序已启动！请点击「🔌 连接 CDP」开始")

    # 启动事件循环
    sys.exit(app.exec())


# ============================================================
# 5. 主入口
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("🔬 Chrome + CDP 方案独立测试程序")
    print("=" * 60)
    print(f"CDP 端口: {CDP_PORT}")
    print(f"测试目标: 验证浏览器内 fetch 调用书架/章节池 API")
    print(f"")
    print("📋 测试流程:")
    print("  1. 连接 CDP → 获取 Cookie（验证 wr_skey 有效性）")
    print("  2. 测试书架 API → /web/shelf/booklist")
    print("  3. 测试章节池 API → /web/book/chapterInfos")
    print("  4. DOM 提取兜底 → 从书架页面提取书籍信息")
    print("  5. 保存所有测试数据")
    print("=" * 60)
    print("")

    run_app()
