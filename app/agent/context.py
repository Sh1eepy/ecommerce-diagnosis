"""Agent 上下文管理：短 Context 构建与裁剪。

原则：数据库负责记忆，Context 负责当前思考。
- 每次诊断一个短 Context（system + 初始任务 + 若干轮工具结果）
- 超长时裁剪中间轮次，保留 system、初始任务和最近证据
"""
from __future__ import annotations

import json
from pathlib import Path

CONTEXT_MAX_CHARS = 16000


def load_system_prompt(tools_desc: list[dict]) -> str:
    p = Path(__file__).parent / "prompts" / "system.md"
    template = p.read_text(encoding="utf-8")
    return template.replace("{TOOLS}", json.dumps(tools_desc, ensure_ascii=False, indent=2))


def build_initial_messages(tools_desc: list[dict], item_id: int, start: str, end: str, anomaly: str = "") -> list[dict]:
    user = f"请诊断商品 {item_id} 在 {start}~{end} 的经营异常。"
    if anomaly:
        user += f"\n\n【异常事件】\n{anomaly}"
    return [
        {"role": "system", "content": load_system_prompt(tools_desc)},
        {"role": "user", "content": user},
    ]


def append_tool_result(messages: list[dict], tool_name: str, result: dict) -> None:
    if result.get("ok"):
        content = f"[工具 {tool_name} 返回]\n{result.get('text', '')}"
    else:
        content = f"[工具 {tool_name} 执行失败]\n{result.get('text', '')}"
    messages.append({"role": "user", "content": content})


def truncate_context(messages: list[dict], max_chars: int = CONTEXT_MAX_CHARS) -> list[dict]:
    """裁剪：保留 system、第一条 user、最近一轮消息；超长时丢弃中间轮。"""
    if sum(len(m["content"]) for m in messages) <= max_chars:
        return messages
    system = messages[0]
    first_user = messages[1] if len(messages) > 1 else None
    tail = messages[2:]
    kept = [system]
    if first_user:
        kept.append(first_user)
    total = sum(len(m["content"]) for m in kept)
    for m in tail:
        m_len = len(m["content"])
        if total + m_len > max_chars:
            break
        kept.append(m)
        total += m_len
    return kept
