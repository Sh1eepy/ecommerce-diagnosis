"""Agent 包：默认工具白名单注册表。"""
from __future__ import annotations

from app.agent.tool import ToolRegistry
from app.agent.tools.dimension import DimensionTool
from app.agent.tools.funnel import FunnelTool
from app.agent.tools.metric import MetricTool
from app.agent.tools.peer import PeerTool


def default_registry() -> ToolRegistry:
    """Agent 只能使用白名单内的只读 Tool。"""
    return ToolRegistry([MetricTool(), FunnelTool(), DimensionTool(), PeerTool()])


__all__ = ["ToolRegistry", "default_registry"]

