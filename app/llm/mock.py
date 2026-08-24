"""MockLLM：离线开发/测试用，按脚本化决策序列回复。

示例：
    plan = [
        {"type": "tool_call", "tool": "metric", "args": {...}},
        {"type": "tool_call", "tool": "funnel", "args": {...}},
        {"type": "final", "report": {"conclusion": "..."}},
    ]
"""
from __future__ import annotations

import json
import ast
import re

from app.llm.base import LLMClient, LLMResponse


class MockLLM(LLMClient):
    def __init__(self, plan: list[dict] | None = None, fallback: dict | None = None):
        self.plan = list(plan or [])
        self.scripted = plan is not None
        self.idx = 0
        self.model = "mock"
        self.fallback = fallback or {
            "type": "final",
            "report": {
                "facts": [{"point": "Mock 兜底", "evidence": "无"}],
                "analysis": {"key_finding": "无", "impact": "无"},
                "conclusion": "Mock 兜底结论",
                "suggestions": [],
            },
        }

    def chat(self, messages, *, json_mode=True, timeout=None):
        if self.idx < len(self.plan):
            step = self.plan[self.idx]
            self.idx += 1
        elif self.scripted:
            step = self.fallback
        else:
            step = self._adaptive_step(messages)
        return LLMResponse(
            content=json.dumps(step, ensure_ascii=False),
            tokens_in=10,
            tokens_out=10,
            model=self.model,
        )

    @staticmethod
    def _adaptive_step(messages: list[dict]) -> dict:
        """无 API Key 时也走完整证据协议，而不是直接输出伪报告。"""
        joined = "\n".join(str(m.get("content", "")) for m in messages)
        task = re.search(r"商品\s+(\d+)\s+在\s+(\d{4}-\d{2}-\d{2})~(\d{4}-\d{2}-\d{2})", joined)
        item_id, start, end = (int(task.group(1)), task.group(2), task.group(3)) if task else (1, "2015-06-01", "2015-06-14")
        if '"call_id": "metric#1"' not in joined:
            return {
                "type": "tool_call",
                "reasoning": "先验证异常指标及其环比，建立可回查基线",
                "hypothesis": {"id": "H1", "statement": "异常来自商品核心经营指标恶化", "confidence": 0.45, "status": "active"},
                "expected_evidence": "当前窗口指标明显弱于上一窗口",
                "tool": "metric",
                "args": {"item_id": item_id, "start_date": start, "end_date": end},
            }
        if '"call_id": "funnel#2"' not in joined:
            return {
                "type": "tool_call",
                "reasoning": "定位指标恶化对应的漏斗环节",
                "hypothesis": {"id": "H1", "statement": "异常来自商品核心经营指标恶化", "confidence": 0.55, "status": "active", "evidence_refs": ["metric#1"]},
                "expected_evidence": "浏览、加购或成交环节出现明显断点",
                "tool": "funnel",
                "args": {"item_id": item_id, "start_date": start, "end_date": end},
            }

        match = re.search(r"窗口汇总:\s*(\{[^\n]+\})", joined)
        summary = {}
        if match:
            try:
                summary = ast.literal_eval(match.group(1))
            except (SyntaxError, ValueError):
                summary = {}
        metric = next((m for m in ("cvr", "gmv", "uv", "addcart_rate", "click_rate") if m in summary), "uv")
        value = summary.get(metric, 0)
        return {
            "type": "final",
            "hypothesis_updates": [{
                "id": "H1", "statement": "异常来自商品核心经营指标恶化",
                "confidence": 0.7, "status": "supported", "evidence_refs": ["metric#1", "funnel#2"],
            }],
            "report": {
                "facts": [{
                    "point": f"当前窗口 {metric}={value}", "metric": metric, "value": value,
                    "unit": "raw", "evidence_ref": {"call_id": "metric#1", "path": f"summary.current.{metric}"},
                }],
                "hypotheses": [{
                    "id": "H1", "statement": "异常来自商品核心经营指标恶化", "status": "supported",
                    "confidence": 0.7, "evidence_refs": ["metric#1", "funnel#2"],
                }],
                "analysis": {
                    "primary_hypothesis_id": "H1", "key_finding": "核心指标异常并已检查漏斗",
                    "impact": "可能影响成交表现", "limitations": ["Mock 模式不做复杂语义归因"],
                },
                "conclusion": "已确认核心指标异常；具体因果需结合业务信息复核。",
                "suggestions": [{
                    "action": "复核异常窗口的商品配置与漏斗日志", "rationale": "验证指标异常对应的业务变化",
                    "owner": "商品运营", "priority": "P1", "success_metric": f"{metric} 恢复至异常前基线",
                }],
            },
        }
