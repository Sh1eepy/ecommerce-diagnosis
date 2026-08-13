"""离线评估：对每个黄金用例跑一次 Agent，比对工具序列与关键词，输出 evaluation_results.json。

用法：
  python evaluation/cases_runner.py [--limit 20]

说明：
- 这是"用户反馈不准"的回归防线：修复 prompt/tool 后重跑，确认无回归。
- 真实 LLM 模式下评估真实决策质量；未配置 Key 时自动用 MockLLM 验证流程。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.agent import Agent

CASES = Path(__file__).parent / "evaluation_cases.json"
RESULTS = Path(__file__).parent / "evaluation_results.json"


def main(limit: int | None = None) -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    if limit:
        cases = cases[:int(limit)]
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
        # 关键词：命中任一变体即算覆盖该指标主题（LLM 可能用 cvr/CVR/转化率 等）
        kw_variants = c.get("expected_keywords", [])
        missing_keywords = [] if any(k in report_text for k in kw_variants) else list(kw_variants)
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

    passed = sum(1 for x in results if x["pass"])
    out = {
        "run_at": str(date.today()),
        "total": len(cases),
        "passed": passed,
        "pass_rate": round(passed / len(cases), 3) if cases else 0,
        "results": results,
    }
    RESULTS.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"评估完成：{passed}/{len(cases)} 通过（{out['pass_rate']:.1%}）")
    for x in results:
        mark = "PASS" if x["pass"] else "FAIL"
        miss = ",".join(x.get("missing_tools", [])) or ("kw:" + ",".join(x.get("missing_keywords", [])) if x.get("missing_keywords") else "ok")
        print(f"  {mark} {x['case_id']} steps={x.get('steps')} {miss}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="只评估前 N 个用例（控制成本）")
    args = ap.parse_args()
    main(args.limit)

