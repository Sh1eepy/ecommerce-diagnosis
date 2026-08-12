"""Workflow：固定大流程状态机。

大流程固定（骨架），具体分析路径由 Agent 自主决定（血肉）。
流程：
  确认异常 → 查看历史趋势(metric) → 定位异常环节(funnel)
  → 自主选择分析 → 维度拆解(dimension) → 综合证据与报告

Workflow 只做两件事：
1. 跟踪已使用工具，判断关键环节是否覆盖；
2. 在 Agent 过早输出 final 时给出"关键环节未覆盖"的提示。
"""
from __future__ import annotations

STAGES = [
    {"id": "confirm", "label": "确认异常", "required_tool": None},
    {"id": "trend", "label": "查看历史趋势", "required_tool": "metric"},
    {"id": "funnel", "label": "定位异常环节", "required_tool": "funnel"},
    {"id": "explore", "label": "自主选择分析", "required_tool": None},
    {"id": "dimension", "label": "维度拆解", "required_tool": "dimension"},
    {"id": "report", "label": "综合证据与报告", "required_tool": None},
]

# 最关键的环节：缺少它们时不允许直接出结论
CRITICAL_TOOLS = {"metric", "funnel"}


class Workflow:
    def __init__(self):
        self.used: set[str] = set()

    def observe(self, tool_name: str, result: dict) -> None:
        self.used.add(tool_name)

    def used_tools(self) -> list[str]:
        return sorted(self.used)

    def missing_tools(self) -> list[str]:
        return [st["required_tool"] for st in STAGES
                if st["required_tool"] and st["required_tool"] not in self.used]

    def missing_critical(self) -> list[str]:
        return sorted(CRITICAL_TOOLS - self.used)

    def progress_text(self) -> str:
        done = [st["id"] for st in STAGES if st["required_tool"] is None or st["required_tool"] in self.used]
        return f"已完成阶段: {', '.join(done)}；已用工具: {', '.join(self.used_tools()) or '无'}"
