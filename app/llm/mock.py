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
import re

from app.llm.base import LLMClient, LLMResponse


class MockLLM(LLMClient):
    minimum_output_tokens = 1

    def output_token_reserve(self, limit):
        return min(10, limit)

    def estimate_input_tokens(self, messages):
        return 10  # 与下方固定 Mock usage 一致；真实 Provider 使用保守估计。

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

    def chat(self, messages, *, json_mode=True, timeout=None, max_tokens=None):
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

        summary = {}
        # 与报告校验使用同一结构化原值，不再从给人阅读的中文摘要猜字段。
        marker = "[结构化证据 metric#1]\n"
        for message in reversed(messages):
            content = str(message.get("content", ""))
            if marker not in content:
                continue
            try:
                data, _ = json.JSONDecoder().raw_decode(content.split(marker, 1)[1])
                summary = data["summary"]["current"]
            except (KeyError, TypeError, ValueError):
                continue
            if isinstance(summary, dict) and summary:
                break
        if not isinstance(summary, dict) or not summary:
            # 缺证据就留给质量门槛判 incomplete，不能编造一个“观测为零”。
            return {"type": "final", "report": {"conclusion": "指标结构化证据缺失，不能生成已核对事实"}}
        metric = next((m for m in ("cvr", "gmv", "uv", "addcart_rate", "click_rate") if m in summary), "uv")
        value = summary.get(metric, 0)
        return {
            "type": "final",
            "hypothesis_updates": [{
                "id": "H1", "statement": "异常来自商品核心经营指标恶化",
                "confidence": 0.5, "status": "uncertain", "evidence_refs": ["metric#1", "funnel#2"],
            }],
            "report": {
                "facts": [{
                    "section": "change", "point": f"当前窗口 {metric}={value}", "metric": metric, "value": value,
                    "unit": "raw", "evidence_ref": {"call_id": "metric#1", "path": f"summary.current.{metric}"},
                }],
                "hypotheses": [{
                    "id": "H1", "statement": "可能存在商品核心经营指标变化，原因待验证", "status": "uncertain",
                    "confidence": 0.5, "evidence_refs": ["metric#1", "funnel#2"],
                }],
                "analysis": {
                    "attribution_status": "uncertain", "primary_hypothesis_id": None,
                    "key_finding": "已取得核心指标与漏斗观察数据，尚不能确认原因",
                    "impact": "可能影响成交表现", "limitations": ["Mock 模式不做复杂语义归因"],
                },
                "conclusion": "已取得指标观察值；是否异常及具体因果需结合基线与业务信息复核。",
                "suggestions": [{
                    "action": "复核异常窗口的商品配置与漏斗日志", "rationale": "验证指标异常对应的业务变化",
                    "owner": "商品运营", "priority": "P1", "success_metric": "取得配置变更时间与漏斗日志的核查结果",
                }],
            },
        }
