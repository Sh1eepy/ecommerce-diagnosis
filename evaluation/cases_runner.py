"""离线评估：对每个黄金用例跑一次 Agent，比对工具序列与关键词，输出 evaluation_results.json。

用法：
  python evaluation/cases_runner.py

说明：
- 这是"用户反馈不准"的回归防线：修复 prompt/tool 后重跑，确认无回归。
- 用 MockLLM 时可全离线验证流程；用真实 LLM 时评估真实决策质量。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.agent import Agent

CASES = Path(__file__).parent / "evaluation_cases.json"
RESULTS = Path(__file__).parent / "evaluation_results.json"


def main() -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    results = []
    for c in cases:
        try:
            r = Agent().run(
                int(c["item_id"]),
                date.fromisoformat(c["start_date"]),
                date.fromisoformat(c["end_date"]),
                anomaly=c.get("anomaly", ""),
            )
        except Exception as e:  # noqa: BLE001
            results.append({"case_id": c["case_id"], "pass": False, "error": str(e)})
            continue

        used = set(r.get("tools_used", []))
        expected = set(c.get("expected_tools", []))
        report_text = json.dumps(r.get("report", {}), ensure_ascii=False)
        missing_tools = sorted(expected - used)
        missing_keywords = [k for k in c.get("expected_keywords", []) if k not in report_text]
        ok = not missing_tools and not missing_keywords and r.get("status") == "ok"

        results.append({
            "case_id": c["case_id"],
            "pass": ok,
            "tools_used": sorted(used),
            "missing_tools": missing_tools,
            "missing_keywords": missing_keywords,
            "stop_reason": r.get("stop_reason"),
            "steps": r.get("steps"),
        })

    out = {
        "run_at": str(date.today()),
        "total": len(cases),
        "passed": sum(1 for x in results if x["pass"]),
        "results": results,
    }
    RESULTS.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
