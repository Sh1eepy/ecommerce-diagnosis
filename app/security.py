"""API 安全：X-API-Key 认证 + 内存滑动窗口限流。

说明：限流为进程内实现（单实例够用）；多实例上线时建议换成 Redis。
"""
from __future__ import annotations

import time

from fastapi import Header, HTTPException

from app.config import settings

_requests: dict[str, list[float]] = {}


def verify_api_key(x_api_key: str | None = Header(default=None)) -> str:
    if not x_api_key or x_api_key not in settings.api_key_list:
        raise HTTPException(status_code=401, detail="无效的 API Key")
    _check_rate_limit(x_api_key)
    return x_api_key


def _check_rate_limit(key: str) -> None:
    now = time.time()
    recent = [t for t in _requests.get(key, []) if now - t < 60]
    if len(recent) >= settings.API_RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    recent.append(now)
    _requests[key] = recent
