"""容器内扫码登录：Playwright headless chromium 按需启动（扫码窗口 ≤ ~95s）。

对外唯一入口：
    run_login_session(emit, *, timeout=95, headless=True, proxy=None) -> dict | None

成功返回 {"cookies": {name: value}, "cookies_raw": [{name,value,domain,path,...}]}；
超时/失败返回 None。

emit(event_dict) 回调事件：
    {"event":"starting"}
    {"event":"status", "msg": "..."}
    {"event":"qr", "image_png": <bytes>}     # 二维码截图（PNG）
    {"event":"login_ok"}
    {"event":"error", "msg": "..."}
"""
from __future__ import annotations

import time
from typing import Callable

HOME_URL = "https://weread.qq.com/"

# 固定桌面 UA（与 config.headers 的 Windows UA 保持同一浏览器家族）
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 最小反自动化指纹（chromium 仅登录时短暂启动；阅读上报走 requests 不经浏览器）
_STEALTH_JS = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    "window.chrome = {runtime: {}};"
    "Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});"
    "Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});"
)

Emit = Callable[[dict], None]


def _pick_qr_bytes(page, timeout_sec: float = 12.0) -> bytes | None:
    """定位登录二维码并截图，找不到返回 None。

    实测（2026-09）：点「登录」后二维码为
      - 主 frame：img.wr_login_modal_qr_img（171px，data: URL）
      - 微信开放平台 iframe（open.weixin.qq.com）：img.js_qrcode_img（172px）
    首页还存在大量书封/广告位等方形素材，因此：
      ① 显式选择器优先，且必须已解码（naturalWidth>0）；
      ② 启发式扫描仅在白名单 frame 内进行，且 class/id 必须含 qr；
      ③ 绝不按「形状像二维码」猜测未知 frame 内容。
    """
    explicit_selectors = [
        ".wr_login_modal_qr_img",
        "img.js_qrcode_img",
        "canvas.qrcode",
    ]
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        # ① 显式选择器：主 frame 优先，其次微信开放平台 frame
        for frame in page.frames:
            url = frame.url or ""
            if not (url.startswith("https://weread.qq.com")
                    or url.startswith("https://open.weixin.qq.com")):
                continue
            for sel in explicit_selectors:
                try:
                    handle = frame.query_selector(sel)
                    if handle is None:
                        continue
                    box = handle.bounding_box()
                    if not box or box["width"] < 100 or box["height"] < 100:
                        continue
                    ready = handle.evaluate(
                        "el => el.tagName === 'CANVAS' || el.naturalWidth > 0"
                    )
                    if not ready:
                        continue
                    shot = handle.screenshot(type="png")
                    if shot:
                        return shot
                except Exception:
                    continue
        # ② 白名单 frame 内按 class/id 含 qr 启发式兜底
        for frame in page.frames:
            url = frame.url or ""
            if not (url.startswith("https://weread.qq.com")
                    or url.startswith("https://open.weixin.qq.com")):
                continue
            try:
                metas = frame.evaluate(
                    """
                    () => {
                      const out = [];
                      ['img', 'canvas'].forEach(tag => {
                        document.querySelectorAll(tag).forEach((el, i) => {
                          const r = el.getBoundingClientRect();
                          if (!r || r.width < 100 || r.height < 100) return;
                          const ratio = r.width / Math.max(1, r.height);
                          if (ratio < 0.7 || ratio > 1.4) return;
                          const sig = ((el.className || '') + ' ' + (el.id || '')
                                       + ' ' + (el.src || '')).toLowerCase();
                          if (!sig.includes('qr')) return;
                          out.push({tag: el.tagName, idx: i});
                        });
                      });
                      return out;
                    }
                    """
                )
            except Exception:
                continue
            for meta in metas or []:
                try:
                    tag = meta["tag"].lower()
                    handle = frame.query_selector_all(tag)[meta["idx"]]
                    if handle is None:
                        continue
                    shot = handle.screenshot(type="png")
                    if shot:
                        return shot
                except Exception:
                    continue
        page.wait_for_timeout(500)
    return None


def _cookies_to_session(context) -> dict | None:
    """从浏览器上下文提取 cookies，wr_skey 长度 ≥8 才视为登录成功。"""
    cookies = context.cookies()
    kept = [
        c for c in cookies
        if str(c.get("domain", "")).endswith("qq.com")
    ]
    cookies_dict = {str(c["name"]): str(c["value"]) for c in kept if c.get("name")}
    skey = cookies_dict.get("wr_skey", "")
    vid = cookies_dict.get("wr_vid", "")
    if len(skey) >= 8 and vid:
        return {"cookies": cookies_dict, "cookies_raw": kept}
    return None


def run_login_session(
    emit: Emit,
    *,
    timeout: int = 95,
    headless: bool = True,
    proxy: str | None = None,
) -> dict | None:
    """启动 chromium → 打开 weread.qq.com → 推二维码 → 轮询登录 → 关闭浏览器。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        emit({"event": "error", "msg": f"Playwright 未安装：{exc}"})
        return None

    emit({"event": "starting"})
    launch_kwargs = {
        "headless": headless,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(**launch_kwargs)
            try:
                context = browser.new_context(
                    user_agent=_UA, locale="zh-CN", no_viewport=False,
                    viewport={"width": 430, "height": 760},
                )
                context.add_init_script(_STEALTH_JS)
                page = context.new_page()
                emit({"event": "status", "msg": "正在打开微信读书登录页…"})
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)

                # 2026-09 实测：首页不再直接展示二维码，需先点「登录」弹出登录模态框，
                # 二维码出现在 img.wr_login_modal_qr_img（主 frame，data: URL）
                # 及 open.weixin.qq.com iframe 的 img.js_qrcode_img（171~172px 正方形）
                try:
                    page.get_by_text("登录", exact=True).first.click(timeout=5000)
                    emit({"event": "status", "msg": "已打开登录弹窗，正在获取二维码…"})
                    page.wait_for_timeout(2500)
                except Exception:
                    # 个别版本首页可能直接展示二维码，点击失败不致命，交给定位循环兜底
                    pass

                shot = _pick_qr_bytes(page)
                if shot is None:
                    # 兜底：整页截图（页面结构变化时也能让用户看到登录页）
                    try:
                        shot = page.screenshot(type="png", full_page=False)
                    except Exception:
                        shot = None
                if shot:
                    emit({"event": "qr", "image_png": shot})
                    emit({"event": "status", "msg": "请使用微信扫一扫（手机端可长按图片存相册）"})
                else:
                    emit({"event": "error", "msg": "登录页二维码定位失败，请改用 Cookie 粘贴登录"})
                    return None

                deadline = time.time() + timeout
                while time.time() < deadline:
                    session = _cookies_to_session(context)
                    if session is not None:
                        # 多等 2 秒让全部 cookie 落齐
                        page.wait_for_timeout(2000)
                        session = _cookies_to_session(context) or session
                        emit({"event": "status", "msg": "扫码成功，正在保存登录态…"})
                        emit({"event": "login_ok"})
                        return session
                    page.wait_for_timeout(1000)
                emit({"event": "error", "msg": "扫码超时（约 95 秒），请重新获取二维码"})
                return None
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as exc:  # noqa: BLE001
        emit({"event": "error", "msg": f"浏览器启动失败：{exc}"})
        return None
