"""告警 Webhook：诊断完成后推送通知（钉钉/企微/飞书等任意 POST 端点）。

安全：请求体带 HMAC-SHA256 签名头 X-Alert-Signature（用 ALERT_WEBHOOK_SECRET），
      接收方可用同一 secret 校验，防止伪造告警。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def _sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def send_diagnosis_alert(result: dict) -> bool:
    """诊断完成后推送 webhook；未配置 URL 则静默跳过。返回是否实际发送。"""
    url = settings.ALERT_WEBHOOK_URL.strip()
    if not url:
        return False

    report = result.get("report") or {}
    payload = {
        "event": "diagnosis_finished",
        "run_id": result.get("run_id"),
        "item_id": result.get("item_id"),
        "window": result.get("window"),
        "anomaly_id": result.get("anomaly_id"),
        "status": result.get("status"),
        "stop_reason": result.get("stop_reason"),
        "steps": result.get("steps"),
        "tool_calls": result.get("tool_calls"),
        "conclusion": report.get("conclusion", ""),
        "suggestions": report.get("suggestions", []),
        "severity": report.get("severity", ""),
    }
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if settings.ALERT_WEBHOOK_SECRET:
        headers["X-Alert-Signature"] = _sign(body, settings.ALERT_WEBHOOK_SECRET)

    try:
        resp = httpx.post(url, content=body, headers=headers, timeout=5.0)
        resp.raise_for_status()
        logger.info("告警已发送 run_id=%s status=%s", result.get("run_id"), resp.status_code)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("告警发送失败 run_id=%s: %s", result.get("run_id"), e)
        return False
