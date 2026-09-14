"""Bearer Token 鉴权（WXREAD_WEB_TOKEN 设置时启用）。

- 普通 API/静态页：Authorization: Bearer <token>
- EventSource(SSE) 无法自定义请求头，允许 GET 带 ?access_token=<token>
  （仅在配置了 WXREAD_WEB_TOKEN 时校验；未配置时端口侧由 uvicorn 绑 127.0.0.1）
"""
from __future__ import annotations

import os
import hmac

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

PUBLIC_PATHS = {"/healthz", "/favicon.ico", "/api/version"}


def _expected_token() -> str:
    return os.environ.get("WXREAD_WEB_TOKEN", "").strip()


def _check(token: str) -> bool:
    expected = _expected_token()
    return bool(expected) and hmac.compare_digest(token, expected)


class BearerTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        expected = _expected_token()
        if not expected:
            return await call_next(request)
        path = request.url.path
        if path in PUBLIC_PATHS:
            return await call_next(request)
        # 静态页/静态资源必须可匿名打开：index.html 内的令牌输入页是浏览器登录入口，
        # 所有敏感数据接口均以 /api 为前缀，静态资源本身不含任何用户数据。
        if not path.startswith("/api/"):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        # SSE / 浏览器直接下载兜底：?access_token=
        if not token:
            token = request.query_params.get("access_token", "").strip()
        if not _check(token):
            return JSONResponse(
                status_code=401,
                content={"ok": False, "code": "UNAUTHORIZED",
                         "msg": "缺少或无效的 Bearer Token"},
            )
        return await call_next(request)
