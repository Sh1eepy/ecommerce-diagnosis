"""Workflow 护栏：跟踪成功证据，不规定固定工具序列。"""
from __future__ import annotations

class Workflow:
    def __init__(self):
        self.used: set[str] = set()
        self.failed: set[str] = set()

    def observe(self, tool_name: str, result: dict) -> None:
        if result.get("ok"):
            self.used.add(tool_name)
        else:
            self.failed.add(tool_name)

    def used_tools(self) -> list[str]:
        return sorted(self.used)

    def can_finalize(self) -> bool:
        return bool(self.used)

    def progress_text(self) -> str:
        return (
            f"成功工具: {', '.join(self.used_tools()) or '无'}；"
            f"失败工具: {', '.join(sorted(self.failed)) or '无'}"
        )

    def to_dict(self) -> dict:
        return {"used": sorted(self.used), "failed": sorted(self.failed)}

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Workflow":
        workflow = cls()
        raw = raw if isinstance(raw, dict) else {}
        workflow.used = {str(x) for x in raw.get("used") or []}
        workflow.failed = {str(x) for x in raw.get("failed") or []}
        return workflow
