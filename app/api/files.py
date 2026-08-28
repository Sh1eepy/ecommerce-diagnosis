"""文件导入 + 用户反馈接口。

文件导入：业务数据的"写入口"，只走服务层（写连接 + 校验），
Agent 永远无法通过该路径写数据。
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.config import settings
from app.db import read_session, write_session
from app.models import DailyItemStat, DiagnosticReport
from app.metrics.registry import DailyStatDimension
from app.security import require_scope

router = APIRouter(tags=["files"])

CSV_COLUMNS = {
    "item_id", "stat_date", "dimension_type", "dimension",
    "uv", "view_count", "click_count", "addtocart_count", "transaction_count", "gmv",
}


class DailyStatRow(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    item_id: int = Field(gt=0, le=2**63 - 1)
    stat_date: date
    dimension_type: DailyStatDimension
    dimension: str = Field(min_length=1, max_length=32)
    uv: int = Field(ge=0, le=2**31 - 1)
    view_count: int = Field(ge=0, le=2**31 - 1)
    click_count: int = Field(ge=0, le=2**31 - 1)
    addtocart_count: int = Field(ge=0, le=2**31 - 1)
    transaction_count: int = Field(ge=0, le=2**31 - 1)
    gmv: float = Field(ge=0)

    @field_validator("stat_date", mode="before")
    @classmethod
    def iso_date(cls, value):
        if not isinstance(value, str):
            raise ValueError("stat_date 必须为 YYYY-MM-DD")
        return date.fromisoformat(value.strip())


@router.post("/import/daily-stat")
def import_daily_stat(file: UploadFile = File(...), _: str = Depends(require_scope("data:import"))) -> dict:
    if not (file.filename or "").lower().endswith((".csv", ".txt")):
        raise HTTPException(status_code=400, detail="仅支持 CSV 文件")
    raw = file.file.read(settings.MAX_UPLOAD_BYTES + 1)
    if len(raw) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件超过大小上限")
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="文件为空")

    try:
        reader = csv.DictReader(StringIO(_decode(raw), newline=""), strict=True)
        columns = reader.fieldnames or []
    except (UnicodeError, csv.Error) as e:
        raise HTTPException(status_code=400, detail="CSV 编码或格式非法") from e
    if len(columns) != len(CSV_COLUMNS) or set(columns) != CSV_COLUMNS:
        raise HTTPException(status_code=400, detail="CSV 列必须完整且不重复，不允许额外列")

    rows = 0
    with write_session() as s:
        try:
            for row in reader:
                if rows >= settings.MAX_UPLOAD_ROWS:
                    raise HTTPException(status_code=413, detail="CSV 超过行数上限")
                if None in row:
                    raise ValueError("CSV 行列数不匹配")
                validated = DailyStatRow.model_validate(row)
                s.add(DailyItemStat(**validated.model_dump()))
                rows += 1
                if rows % 1000 == 0:
                    s.flush()  # 控制 ORM 内存；整个文件仍在同一个事务内。
        except (ValidationError, ValueError, csv.Error) as e:
            raise HTTPException(status_code=400, detail=f"CSV 第 {reader.line_num} 行数据非法") from e
        if rows == 0:
            raise HTTPException(status_code=400, detail="CSV 没有数据行")
        s.commit()
    return {"imported_rows": rows}


def _decode(raw: bytes):
    """尝试按 UTF-8/GBK 解码 CSV 内容。"""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise UnicodeError("仅支持 UTF-8 或 GBK")


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$", description="已有诊断 run_id")
    rating: int = Field(ge=1, le=5, strict=True, description="评分 1-5")
    category: Literal["data", "tool", "analysis", "other"] = "other"
    comment: str = Field(default="", max_length=4000, description="反馈说明")


@router.post("/feedback")
def submit_feedback(payload: FeedbackRequest, _: str = Depends(require_scope("feedback:create"))) -> dict:
    """每条反馈写独立文件，文件名由服务端生成，兼容已有 records 聚合格式。"""
    with read_session() as s:
        if not s.query(DiagnosticReport.id).filter_by(run_id=payload.run_id).first():
            raise HTTPException(status_code=404, detail="诊断报告不存在")
    base = Path(settings.LOG_DIR).parent / "feedback" / "agent_feedback"
    base.mkdir(parents=True, exist_ok=True)
    feedback_id = uuid4().hex
    path = base / f"{feedback_id}.json"
    record = {
        "run_id": payload.run_id,
        "rating": payload.rating,
        "category": payload.category,
        "comment": payload.comment,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with path.open("x", encoding="utf-8") as f:
        json.dump({"records": [record]}, f, ensure_ascii=False)
    return {"status": "ok", "feedback_id": feedback_id}
