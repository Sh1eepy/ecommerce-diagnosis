"""Agent 包：默认工具白名单注册表。"""
from __future__ import annotations

from app.agent.tool import ToolRegistry
from app.agent.tools.dimension import DimensionTool
from app.agent.tools.funnel import FunnelTool
from app.agent.tools.metric import MetricTool


def default_registry() -> ToolRegistry:
    """Agent 只能使用这 3 个只读 Tool（白名单）。"""
    return ToolRegistry([MetricTool(), FunnelTool(), DimensionTool()])


__all__ = ["ToolRegistry", "default_registry"]
