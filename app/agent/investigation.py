"""Agent 调查状态：显式保存假设、证据和置信度。"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any


VALID_HYPOTHESIS_STATUS = {"active", "supported", "rejected", "uncertain"}


def _json_safe(value: Any) -> Any:
    """证据会进入 API/任务 JSON；数据库 Decimal、date 等统一安全序列化。"""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _confidence(value: Any, default: float = 0.5) -> float:
    try:
        return round(min(1.0, max(0.0, float(value))), 3)
    except (TypeError, ValueError):
        return default


@dataclass
class Hypothesis:
    id: str
    statement: str
    confidence: float = 0.5
    status: str = "active"
    evidence_refs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "statement": self.statement,
            "confidence": self.confidence,
            "status": self.status,
            "evidence_refs": list(self.evidence_refs),
        }


class InvestigationState:
    """由确定性代码维护，避免调查状态只存在于模型的自由文本中。"""

    def __init__(self):
        self.hypotheses: dict[str, Hypothesis] = {}
        self.evidence: dict[str, dict] = {}
        self.failed_calls: list[dict] = []

    def update_hypothesis(self, raw: dict | None) -> str | None:
        if not isinstance(raw, dict):
            return None
        hid = str(raw.get("id") or "").strip()[:64]
        statement = str(raw.get("statement") or "").strip()[:500]
        if not hid or not statement:
            return None
        status = str(raw.get("status") or "active")
        if status not in VALID_HYPOTHESIS_STATUS:
            status = "active"
        current = self.hypotheses.get(hid)
        refs = raw.get("evidence_refs") if isinstance(raw.get("evidence_refs"), list) else []
        valid_refs = [str(ref) for ref in refs if str(ref) in self.evidence]
        if current:
            current.statement = statement
            current.confidence = _confidence(raw.get("confidence"), current.confidence)
            current.status = status
            current.evidence_refs = list(dict.fromkeys(current.evidence_refs + valid_refs))
        else:
            self.hypotheses[hid] = Hypothesis(
                id=hid,
                statement=statement,
                confidence=_confidence(raw.get("confidence")),
                status=status,
                evidence_refs=valid_refs,
            )
        return hid

    def apply_updates(self, decision: dict) -> str | None:
        for raw in decision.get("hypothesis_updates") or []:
            self.update_hypothesis(raw)
        return self.update_hypothesis(decision.get("hypothesis"))

    def observe_tool(self, step: int, tool: str, result: dict, hypothesis_id: str | None) -> str | None:
        call_id = f"{tool}#{step}"
        if not result.get("ok"):
            self.failed_calls.append({"call_id": call_id, "error": result.get("text", "")[:300]})
            return None
        self.evidence[call_id] = {
            "call_id": call_id,
            "tool": tool,
            "summary": result.get("text", "")[:1000],
            "rows": result.get("rows", 0),
            "data": _json_safe(result.get("data")),
        }
        if hypothesis_id and hypothesis_id in self.hypotheses:
            h = self.hypotheses[hypothesis_id]
            if call_id not in h.evidence_refs:
                h.evidence_refs.append(call_id)
        return call_id

    def valid_evidence_ref(self, ref: str) -> bool:
        return ref in self.evidence

    def snapshot(self) -> dict:
        return {
            "hypotheses": [h.as_dict() for h in self.hypotheses.values()],
            "successful_evidence": [
                {"call_id": e["call_id"], "tool": e["tool"], "rows": e["rows"], "summary": e["summary"]}
                for e in self.evidence.values()
            ],
            "failed_calls": list(self.failed_calls),
        }
