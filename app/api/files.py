"""文件导入 + 用户反馈接口。

文件导入：业务数据的"写入口"，只走服务层（写连接 + 校验），
Agent 永远无法通过该路径写数据。
"""
from __future__ import annotations

import json
from datetime import date
from io import StringIO
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.config import settings
from app.db import write_session
from app.models import DailyItemStat
from app.security import verify_api_key

router = APIRouter(tags=["files"])

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50MB

CSV_COLUMNS = {
    "item_id", "stat_date", "dimension_type", "dimension",
    "uv", "view_count", "click_count", "addtocart_count", "transaction_count", "gmv",
}


@router.post("/import/daily-stat")
def import_daily_stat(file: UploadFile = File(...), _: str = Depends(verify_api_key)) -> dict:
    if not (file.filename or "").lower().endswith((".csv", ".txt")):
        raise HTTPException(status_code=400, detail="仅支持 CSV 文件")
    raw = file.file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 50MB 上限")
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="文件为空")

    try:
        df = pd.read_csv(StringIO(_decode(raw)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"CSV 解析失败: {e}") from e

    missing = CSV_COLUMNS - set(df.columns)
    if missing:
        raise HTTPException(status_code=400, detail=f"缺少列: {sorted(missing)}")

    rows = 0
    with write_session() as s:
        for _, r in df.iterrows():
            try:
                d = date.fromisoformat(str(r["stat_date"]).strip())
                s.add(DailyItemStat(
                    item_id=int(r["item_id"]),
                    stat_date=d,
                    dimension_type=str(r.get("dimension_type", "all")),
                    dimension=str(r.get("dimension", "all")),
                    uv=int(r.get("uv", 0)),
                    view_count=int(r.get("view_count", 0)),
                    click_count=int(r.get("click_count", 0)),
                    addtocart_count=int(r.get("addtocart_count", 0)),
                    transaction_count=int(r.get("transaction_count", 0)),
                    gmv=float(r.get("gmv", 0.0)),
                ))
                rows += 1
            except (ValueError, TypeError) as e:
                raise HTTPException(status_code=400, detail=f"第 {rows + 2} 行数据非法: {e}") from e
        s.commit()
    return {"imported_rows": rows}


def _decode(raw: bytes):
    """尝试按 UTF-8/GBK 解码 CSV 内容。"""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


class FeedbackRequest(BaseModel):
    run_id: str = Field(description="诊断 run_id")
    rating: int = Field(ge=1, le=5, description="评分 1-5")
    category: str = Field(default="other", description="问题类别: data/tool/analysis/other")
    comment: str = Field(default="", description="反馈说明")


@router.post("/feedback")
def submit_feedback(payload: FeedbackRequest, _: str = Depends(verify_api_key)) -> dict:
    """用户对诊断报告的反馈：落盘 feedback/agent_feedback/{run_id}.json。"""
    base = Path(settings.LOG_DIR).parent / "feedback" / "agent_feedback"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{payload.run_id}.json"
    record = {
        "run_id": payload.run_id,
        "rating": payload.rating,
        "category": payload.category,
        "comment": payload.comment,
        "ts": json.dumps(str(date.today())),
    }
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("records", []).append(record)
    else:
        data = {"records": [record]}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "ok", "path": str(path)}
