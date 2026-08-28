"""人工审查与案例经验：数据库为主记录，Markdown 为可重建的阅读副本。

不自动替用户认定正确；可确认 LLM 提炼草稿，不从任意本地 MD 加载指令。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException
from pydantic import Field, model_validator
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import read_session, write_session
from app.models import AnomalyEvent, DiagnosticReport, ReportReview, ReviewDraft, utcnow
from app.review_drafts import FeedbackInput, feedback_json, reviewer_id, report_digest, review_target


class ReviewRequest(FeedbackInput):
    correct_lesson: str = Field(default="", max_length=600)
    incorrect_lesson: str = Field(default="", max_length=600)
    evidence_note: str = Field(default="", max_length=600)
    applicability: str = Field(default="", max_length=400)
    draft_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    draft_confirmed: bool = Field(default=False, strict=True)
    use_as_memory: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def memory_needs_lesson(self):
        if self.draft_id and not self.draft_confirmed:
            raise ValueError("请先确认或修正模型提炼草稿")
        if self.use_as_memory and not (self.correct_lesson or self.incorrect_lesson):
            raise ValueError("加入经验库前请填写可参考经验或应避免的误判")
        return self


VERDICTS = {"correct": "认为正确", "partial": "部分正确", "incorrect": "认为不正确", "uncertain": "暂不能判断"}


def review_matches(review: ReportReview | None, report: DiagnosticReport) -> bool:
    return review is not None and report_digest(report) == review.report_digest


def review_view(review: ReportReview | None, report: DiagnosticReport | None = None) -> dict:
    if review is None or (report is not None and not review_matches(review, report)):
        return {"status": "unreviewed"}
    return {"status": "reviewed", "id": review.id, "run_id": review.run_id,
            "reviewed_at": review.created_at.isoformat(), **json.loads(review.feedback_json),
            "use_as_memory": review.use_as_memory}


def _markdown(snapshot: dict, payload: ReviewRequest, run_id: str, digest: str) -> str:
    # 不让反馈中的标题/HTML伪装成系统规则；仍作为不可信人工原话保存。
    def quote(value):
        return "\n".join("> " + line.replace("<", "&lt;").replace(">", "&gt;") for line in value.splitlines()) or "> 未填写"
    return (
        f"# 人工审查案例 · 异常 {snapshot['anomaly_id']}\n\n"
        f"报告：{run_id}；内容摘要：{digest}\n\n"
        f"指标：{snapshot['metric']}；规则：{snapshot['rule_id']}；商品：{snapshot['item_id']}\n\n"
        f"窗口：{snapshot['date_start']} ~ {snapshot['date_end']}\n\n"
        f"规则观察：基线 {snapshot['baseline_value']}，当前 {snapshot['current_value']}（非因果证明）。\n\n"
        f"人工结论：{VERDICTS[payload.verdict]}；提交时允许后续参考：{'是' if payload.use_as_memory else '否'}\n\n"
        "实际启用状态以数据库为准，撤回后会产生独立停用记录；此文件保留审查时的原话。\n\n"
        "人工经验不是本次诊断的工具证据，不能据此确认原因；遇到冲突须重新核查。\n\n"
        f"## 用户反馈\n\n{quote(payload.comment)}\n\n"
        f"经验来源：{'LLM 草稿经用户确认或修订，草稿 ' + payload.draft_id if payload.draft_id else '用户填写'}\n\n"
        f"## 可参考经验\n\n{quote(payload.correct_lesson)}\n\n"
        f"## 应避免的误判\n\n{quote(payload.incorrect_lesson)}\n\n"
        f"## 适用条件与限制\n\n{quote(payload.applicability)}\n\n"
        f"## 核查依据（用户提供，未经系统独立验证）\n\n{quote(payload.evidence_note)}\n"
    )


def export_review(review: ReportReview) -> bool:
    """独立不可覆盖文件；导出失败不撤销已提交审查，重复提交相同审查可补导出。"""
    base = Path(settings.LOG_DIR).resolve().parent / "knowledge" / "reviews"
    path = base / f"review-{review.id}.md"  # 仅数据库整数参与路径
    return _write_once(path, review.memory_markdown)


def _write_once(path: Path, content: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return path.read_text(encoding="utf-8") == content
        with path.open("x", encoding="utf-8") as f:
            f.write(content)
        return True
    except (OSError, UnicodeError):
        return False


def submit_review(run_id: str, payload: ReviewRequest, principal: str) -> dict:
    # run_id 上的唯一约束抵御重复点击/并发提交；重诊生成新 run_id，需重新审查。
    encoded = json.dumps(payload.model_dump(), ensure_ascii=False, sort_keys=True)
    with write_session() as s:
        report, anomaly = review_target(s, run_id)
        if payload.draft_id:
            draft = s.get(ReviewDraft, payload.draft_id)
            if (draft is None or draft.status != "ready" or draft.run_id != run_id
                    or draft.reviewer != reviewer_id(principal) or draft.report_digest != report_digest(report)
                    or draft.input_json != feedback_json(payload)):
                raise HTTPException(409, "提炼草稿与当前用户、报告或反馈不一致，请重新核对")
        existing = s.query(ReportReview).filter_by(run_id=run_id).first()
        if existing:
            if not review_matches(existing, report):
                raise HTTPException(409, "原报告内容已经变化，旧审查不适用于当前内容；请生成新报告版本")
            if existing.feedback_json != encoded:
                raise HTTPException(409, "该版本已审查，不能覆盖原反馈；请刷新查看")
            review = existing
        else:
            snapshot = {k: getattr(anomaly, k) for k in
                        ("item_id", "category_id", "metric", "rule_id", "baseline_value", "current_value")}
            snapshot.update(anomaly_id=anomaly.id, date_start=str(anomaly.date_start), date_end=str(anomaly.date_end))
            digest = report_digest(report)
            review = ReportReview(
                run_id=run_id, anomaly_id=anomaly.id, metric=anomaly.metric, rule_id=anomaly.rule_id,
                scope="category" if anomaly.item_id == 0 else "item", report_digest=digest,
                reviewer=reviewer_id(principal),
                feedback_json=encoded, anomaly_json=json.dumps(snapshot, ensure_ascii=False),
                use_as_memory=payload.use_as_memory,
                memory_markdown=_markdown(snapshot, payload, run_id, digest),
            )
            s.add(review)
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                review = s.query(ReportReview).filter_by(run_id=run_id).first()
                if review is None or review.feedback_json != encoded:
                    raise HTTPException(409, "报告已被其他请求审查，请刷新") from None
    return {"review": review_view(review), "markdown_exported": export_review(review)}


def relevant_memories(anomaly_id: int | None) -> list[dict]:
    """只取同指标、同规则、同层级的最近三条人工授权经验；不依赖词向量服务。"""
    if anomaly_id is None:
        return []
    with read_session() as s:
        anomaly = s.get(AnomalyEvent, anomaly_id)
        if anomaly is None:
            return []
        rows = (s.query(ReportReview, DiagnosticReport)
                .join(DiagnosticReport, DiagnosticReport.run_id == ReportReview.run_id)
                .filter(ReportReview.use_as_memory.is_(True), ReportReview.metric == anomaly.metric,
                        ReportReview.rule_id == anomaly.rule_id,
                        ReportReview.scope == ("category" if anomaly.item_id == 0 else "item"))
                .order_by(ReportReview.id.desc()).limit(30).all())
        latest_reports = {}
        for rep in (s.query(DiagnosticReport).filter(DiagnosticReport.anomaly_id.in_([r.anomaly_id for r, _ in rows]))
                    .order_by(DiagnosticReport.created_at.desc(), DiagnosticReport.id.desc()).all()):
            latest_reports.setdefault(rep.anomaly_id, rep.run_id)
        selected, chars = [], 0
        for review, report in rows:
            if latest_reports.get(review.anomaly_id) != review.run_id:
                continue  # 新版本尚未审查，不继承旧版本反馈。
            if not review_matches(review, report):
                continue
            feedback = json.loads(review.feedback_json)
            snapshot = json.loads(review.anomaly_json)
            entry = {"review_id": review.id, "source_anomaly_id": review.anomaly_id,
                     "source_run_id": review.run_id, "source": "human_unverified",
                     "verdict": feedback["verdict"], "correct_lesson": feedback["correct_lesson"],
                     "incorrect_lesson": feedback["incorrect_lesson"], "evidence_note": feedback["evidence_note"],
                     "applicability": feedback.get("applicability", ""),
                     "window": [snapshot[k] for k in ("date_start", "date_end")],
                     "source_observation": {k: snapshot[k] for k in ("metric", "rule_id", "baseline_value", "current_value")}}
            size = len(json.dumps(entry, ensure_ascii=False))
            if chars + size > 2400:
                continue
            selected.append(entry)
            chars += size
            if len(selected) == 3:
                break
        return selected


def disable_memory(run_id: str) -> dict:
    with write_session() as s:
        review = s.query(ReportReview).filter_by(run_id=run_id).with_for_update().first()
        if review is None:
            raise HTTPException(404, "审查记录不存在")
        review.use_as_memory = False
        review.memory_disabled_at = review.memory_disabled_at or utcnow()
        s.commit()
    path = Path(settings.LOG_DIR).resolve().parent / "knowledge" / "reviews" / f"review-{review.id}-disabled.md"
    note = f"# 经验已停用\n\n审查 {review.id}，报告 {review.run_id}\n\n停用时间：{review.memory_disabled_at.isoformat()} UTC\n\n保留原反馈，不再用于新诊断；已经在途的诊断仍使用其启动时快照。\n"
    return {"review": review_view(review), "markdown_exported": _write_once(path, note)}
