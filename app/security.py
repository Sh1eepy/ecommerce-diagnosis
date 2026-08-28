"""API 安全：X-API-Key 认证 + 内存滑动窗口限流。

说明：限流为进程内实现（单实例够用）；多实例上线时建议换成 Redis。
"""
from __future__ import annotations

import secrets
import time
from threading import Lock

from fastapi import Depends, Header, HTTPException
from starlette.responses import JSONResponse

from app.config import ApiScope, settings

_requests: dict[str, list[float]] = {}
_rate_lock = Lock()
_ALL_SCOPES = frozenset({"report:read", "diagnosis:create", "data:import", "feedback:create"})


def _key_scopes(key: str) -> frozenset[str] | None:
    # 显式权限优先，避免同一个 Key 同时在旧配置里时被意外提升为全权限。
    for configured, scopes in settings.API_KEY_SCOPES.items():
        if secrets.compare_digest(key.encode(), configured.encode()):
            return frozenset(scopes)
    if settings.APP_ENV != "production":
        for configured in settings.api_key_list:
            if secrets.compare_digest(key.encode(), configured.encode()):
                return _ALL_SCOPES
    return None


def verify_api_key(x_api_key: str | None = Header(default=None)) -> str:
    if not x_api_key or _key_scopes(x_api_key) is None:
        raise HTTPException(status_code=401, detail="无效的 API Key")
    _check_rate_limit(x_api_key)
    return x_api_key


def require_scope(scope: ApiScope):
    """认证后再授权；权限来自服务端配置，不接受请求体或模型自报权限。"""
    def dependency(key: str = Depends(verify_api_key)) -> str:
        if scope not in (_key_scopes(key) or ()):
            raise HTTPException(status_code=403, detail="当前 API Key 无此操作权限")
        return key
    return dependency


def _check_rate_limit(key: str) -> None:
    now = time.monotonic()
    with _rate_lock:
        recent = [t for t in _requests.get(key, []) if now - t < 60]
        if len(recent) >= settings.API_RATE_LIMIT_PER_MINUTE:
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
        recent.append(now)
        _requests[key] = recent


class RequestBodyLimitMiddleware:
    """在 multipart/JSON 解析前限制实际读取字节，不依赖可伪造的 Content-Length。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = (settings.MAX_UPLOAD_BYTES + 64 * 1024
                 if scope["path"] == "/api/v1/import/daily-stat" else 64 * 1024)
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length")
        if length is not None:
            try:
                size = int(length)
                if size < 0:
                    raise ValueError
            except ValueError:
                return await JSONResponse({"detail": "Content-Length 非法"}, 400)(scope, receive, send)
            if size > limit:
                return await JSONResponse({"detail": "请求体超过大小上限"}, 413)(scope, receive, send)
        consumed = 0

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    raise HTTPException(status_code=413, detail="请求体超过大小上限")
            return message

        await self.app(scope, limited_receive, send)
