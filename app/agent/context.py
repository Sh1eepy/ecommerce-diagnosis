"""Agent 上下文管理：短 Context 构建与裁剪。

原则：数据库负责记忆，Context 负责当前思考。
- 从证据账本重建工作 Context，保留最新修正，不重复发送历史摘要
- 请求前估算预算；旧 truncate_context 仅保留兼容，Agent 不再静默丢弃中间消息
"""
from __future__ import annotations

import json
from pathlib import Path

CONTEXT_MAX_CHARS = 16000


def load_system_prompt(tools_desc: list[dict]) -> str:
    p = Path(__file__).parent / "prompts" / "system.md"
    template = p.read_text(encoding="utf-8")
    # Schema 与枚举保持完整，只去掉展示缩进，避免挤占真正的工具证据。
    return template.replace("{TOOLS}", json.dumps(tools_desc, ensure_ascii=False, separators=(",", ":")))


def build_initial_messages(tools_desc: list[dict], item_id: int, start: str, end: str, anomaly: str = "") -> list[dict]:
    user = f"请诊断商品 {item_id} 在 {start}~{end} 的经营异常。"
    if anomaly:
        user += f"\n\n【异常事件】\n{anomaly}"
    return [
        {"role": "system", "content": load_system_prompt(tools_desc)},
        {"role": "user", "content": user},
    ]


def append_tool_result(
    messages: list[dict], tool_name: str, result: dict,
    *, call_id: str | None = None, evidence: dict | None = None,
) -> None:
    if result.get("ok"):
        content = f"[工具 {tool_name} 返回]"
        if call_id and evidence:
            content += (
                f"\n[结构化证据 {call_id}]\n"
                + json.dumps(evidence.get("data"), ensure_ascii=False, default=str, separators=(",", ":"))
                + "\n报告 evidence_ref.path 必须严格从以上 JSON 根节点逐级选择。"
            )
        else:
            content += "\n" + result.get("text", "")
    else:
        content = f"[工具 {tool_name} 执行失败]\n{result.get('text', '')}"
    messages.append({"role": "user", "content": content})


def append_investigation_state(messages: list[dict], state: dict) -> None:
    messages.append({
        "role": "user",
        "content": "[当前调查状态]\n" + json.dumps(state, ensure_ascii=False),
    })


def compact_context(messages: list[dict], investigation) -> list[dict]:
    """从完整存档重建工作上下文；不复制历史摘要、不改写证据数值或路径。"""
    messages = [m for m in messages if not m.get("content", "").startswith("[预算收尾]")]
    kept = messages[:2]
    for call_id, entry in investigation.evidence.items():
        data = entry.get("data")
        omitted = []
        if entry.get("tool") == "metric" and isinstance(data, dict):
            data = dict(data)
            # 短窗口逐日记录仍有趋势信息，不能一律用窗口合计替代。
            # 长序列整项省略，不截取尾部后重排数组索引（会破坏证据路径）。
            if isinstance(data.get("series"), list) and len(data["series"]) > 14:
                del data["series"]
                omitted.append("series（日序列未送入本轮，不可据此描述逐日走势）")
            summary = data.get("summary")
            if "coverage" in data and isinstance(summary, dict) and isinstance(summary.get("coverage"), dict):
                del data["coverage"]
                omitted.append("coverage（请引用 summary.coverage.current）")
        append_tool_result(kept, entry.get("tool", ""), {"ok": True},
                           call_id=call_id, evidence={"data": data})
        if omitted:
            kept[-1]["content"] += "\n省略：" + "；".join(omitted)
    state = investigation.snapshot()
    state["successful_evidence"] = [
        {"call_id": e["call_id"], "tool": e["tool"], "args": e.get("args", {})} for e in investigation.evidence.values()
    ]
    append_investigation_state(kept, state)
    # 只保留最新一次失败报告/非法响应和对应修正意见，旧决策已经归入调查状态。
    if len(messages) > 2 and messages[-1]["content"].startswith(("final 未通过", "上一响应")):
        if len(messages) > 3 and messages[-2].get("role") == "assistant":
            kept.append(messages[-2])
        kept.append(messages[-1])
    # 不静默丢掉最新修正指令；超限由请求前预算检查安全停止。
    return kept


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
    # 从尾部倒序选择，确保真正保留最新证据，再恢复消息原顺序。
    recent = []
    for m in reversed(tail):
        m_len = len(m["content"])
        if total + m_len > max_chars:
            continue
        recent.append(m)
        total += m_len
    kept.extend(reversed(recent))
    return kept
