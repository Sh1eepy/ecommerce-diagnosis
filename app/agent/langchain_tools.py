"""旧调用路径的兼容入口；工具已经是原生 BaseTool，不再重复包装。"""
from app.agent.tool import ToolRegistry


def invoke_tool(registry: ToolRegistry, name: str, args: dict, *, run_id: str, step: int) -> dict:
    return registry.execute(name, args, run_id=run_id, step=step)
