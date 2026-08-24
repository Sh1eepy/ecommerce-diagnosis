"""结构化报告的确定性质量检查，供在线 Agent 与离线评估复用。"""
from __future__ import annotations

import math
import re
from typing import Any


def resolve_path(data: Any, path: str) -> Any:
    """解析 data.summary.current.cvr 或 stages.1.count 形式的证据路径。"""
    cur = data
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise KeyError(path)
    return cur


def _numeric_equal(claimed: Any, actual: Any) -> bool:
    try:
        a, b = float(claimed), float(actual)
    except (TypeError, ValueError):
        return False
    return math.isclose(a, b, rel_tol=0.015, abs_tol=0.01)


def evaluate_report(report: dict, evidence: dict[str, dict]) -> dict:
    """返回四个质量维度及可供模型修正的错误列表。"""
    errors: list[str] = []
    facts = report.get("facts") if isinstance(report.get("facts"), list) else []
    hypotheses = report.get("hypotheses") if isinstance(report.get("hypotheses"), list) else []
    suggestions = report.get("suggestions") if isinstance(report.get("suggestions"), list) else []

    valid_fact_refs = 0
    numeric_total = numeric_valid = 0
    nonzero_metrics: set[str] = set()
    for i, fact in enumerate(facts):
        if not isinstance(fact, dict):
            errors.append(f"facts[{i}] 不是对象")
            continue
        ref = fact.get("evidence_ref") or {}
        call_id, path = ref.get("call_id"), ref.get("path")
        if call_id not in evidence:
            errors.append(f"facts[{i}] 引用了不存在或失败的证据 {call_id!r}")
            continue
        try:
            actual = resolve_path(evidence[call_id].get("data"), path)
        except (KeyError, TypeError):
            errors.append(f"facts[{i}] 的证据路径不存在: {call_id}:{path}")
            continue
        valid_fact_refs += 1
        if "value" in fact:
            claimed = fact["value"]
            numeric_pair = (
                isinstance(claimed, (int, float)) and not isinstance(claimed, bool)
                and isinstance(actual, (int, float, str))
            )
            if numeric_pair:
                numeric_total += 1
                if _numeric_equal(claimed, actual):
                    numeric_valid += 1
                    try:
                        if float(actual) != 0 and fact.get("metric"):
                            nonzero_metrics.add(str(fact["metric"]).lower())
                    except (TypeError, ValueError):
                        pass
                else:
                    errors.append(f"facts[{i}] 数值 {claimed} 与证据 {actual} 不一致")
            elif claimed != actual:
                errors.append(f"facts[{i}] 值 {claimed!r} 与证据 {actual!r} 不一致")

    hypothesis_ids: set[str] = set()
    supported_ids: set[str] = set()
    hypothesis_refs_valid = True
    for i, h in enumerate(hypotheses):
        if not isinstance(h, dict) or not h.get("id") or not h.get("statement"):
            errors.append(f"hypotheses[{i}] 缺少 id/statement")
            hypothesis_refs_valid = False
            continue
        hid = str(h["id"])
        hypothesis_ids.add(hid)
        refs = h.get("evidence_refs") if isinstance(h.get("evidence_refs"), list) else []
        if not refs or any(ref not in evidence for ref in refs):
            errors.append(f"假设 {hid} 没有有效 evidence_refs")
            hypothesis_refs_valid = False
        if h.get("status") == "supported":
            supported_ids.add(hid)
        try:
            confidence = float(h.get("confidence"))
            if not 0 <= confidence <= 1:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"假设 {hid} 的 confidence 必须在 0~1")
            hypothesis_refs_valid = False

    analysis = report.get("analysis") if isinstance(report.get("analysis"), dict) else {}
    primary = analysis.get("primary_hypothesis_id")
    attribution_ok = bool(primary and primary in supported_ids)
    if not attribution_ok:
        errors.append("analysis.primary_hypothesis_id 必须指向一个 supported 假设")

    # 防止结构化事实正确、自然语言总结却自相矛盾（如事实 UV=13，结论称 UV 全部归零）。
    narrative = " ".join([
        str(analysis.get("key_finding") or ""),
        str(analysis.get("impact") or ""),
        str(report.get("conclusion") or ""),
    ]).lower()
    metric_terms = {
        "uv": ["uv", "访客"],
        "gmv": ["gmv"],
        "addtocart_count": ["加购"],
        "transaction_count": ["成交"],
        "cvr": ["cvr", "转化率"],
    }
    for metric in sorted(nonzero_metrics):
        for term in metric_terms.get(metric, [metric]):
            if re.search(re.escape(term) + r".{0,24}(?:全部|均|已)?(?:归零|为\s*0)", narrative):
                errors.append(f"叙述与事实矛盾：{metric} 的证据值非零，却声称归零")
                break

    actionable = 0
    for s in suggestions:
        if isinstance(s, dict) and all(
            str(s.get(k) or "").strip()
            for k in ("action", "rationale", "owner", "priority", "success_metric")
        ):
            actionable += 1
    actionability_ok = bool(suggestions) and actionable == len(suggestions)
    if not actionability_ok:
        errors.append("每条建议必须包含 action/rationale/owner/priority/success_metric")

    evidence_support_ok = bool(facts) and valid_fact_refs == len(facts) and bool(hypotheses) and hypothesis_refs_valid
    numeric_consistency_ok = numeric_total > 0 and numeric_valid == numeric_total
    if numeric_total == 0:
        errors.append("至少一条事实必须包含可回查的数值 value")

    scores = {
        "numeric_consistency": round(numeric_valid / numeric_total, 3) if numeric_total else 0.0,
        "evidence_support": 1.0 if evidence_support_ok else 0.0,
        "attribution_grounding": 1.0 if attribution_ok else 0.0,
        "recommendation_actionability": round(actionable / len(suggestions), 3) if suggestions else 0.0,
    }
    narrative_consistency_ok = not any(e.startswith("叙述与事实矛盾") for e in errors)
    scores["narrative_consistency"] = 1.0 if narrative_consistency_ok else 0.0
    return {
        "passed": numeric_consistency_ok and evidence_support_ok and attribution_ok and actionability_ok and narrative_consistency_ok,
        "scores": scores,
        "errors": errors,
    }
