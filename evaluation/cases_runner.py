"""离线评估：验证数值、证据、归因和建议质量，输出 evaluation_results.json。

用法：
  python evaluation/cases_runner.py [--limit 20]

说明：
- 数值一致性和证据支撑由确定性证据路径校验，不使用关键词替代。
- 归因正确性必须由用例提供 ground_truth.attribution_terms；未标注时明确记为 needs_label，
  不再把流程通过冒充诊断正确。
- 建议必须包含动作、理由、责任角色和成功指标。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.agent import Agent
from app.agent.quality import evaluate_report

CASES = Path(__file__).parent / "evaluation_cases.json"
RESULTS = Path(__file__).parent / "evaluation_results.json"


def _attribution_score(case: dict, report: dict, grounded_score: float) -> tuple[float | None, str]:
    truth = case.get("ground_truth") or {}
    terms = [str(x).lower() for x in truth.get("attribution_terms") or []]
    if not terms:
        return None, "needs_label"
    analysis = report.get("analysis") if isinstance(report.get("analysis"), dict) else {}
    primary_id = analysis.get("primary_hypothesis_id")
    primary = next((h for h in report.get("hypotheses") or [] if h.get("id") == primary_id), {})
    text = (str(primary.get("statement", "")) + " " + str(report.get("conclusion", ""))).lower()
    semantic_match = any(term in text for term in terms)
    return (1.0 if semantic_match and grounded_score == 1.0 else 0.0), ("ok" if semantic_match else "mismatch")


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

        report = r.get("report") or {}
        quality = evaluate_report(report, r.get("evidence") or {})
        scores = dict(quality["scores"])
        attribution, attribution_status = _attribution_score(
            c, report, scores["evidence_support"]
        )
        scores["attribution_correctness"] = attribution
        fully_evaluable = attribution is not None
        ok = bool(
            r.get("status") == "ok"
            and quality["passed"]
            and fully_evaluable
            and attribution == 1.0
        )

        results.append({
            "case_id": c["case_id"],
            "pass": ok,
            "scores": scores,
            "attribution_status": attribution_status,
            "quality_errors": quality["errors"],
            "tools_used": sorted(set(r.get("tools_used", []))),
            "stop_reason": r.get("stop_reason"),
            "steps": r.get("steps"),
        })

    passed = sum(1 for x in results if x["pass"])
    out = {
        "run_at": str(date.today()),
        "total": len(cases),
        "passed": passed,
        "pass_rate": round(passed / len(cases), 3) if cases else 0,
        "needs_ground_truth_labels": sum(1 for x in results if x.get("attribution_status") == "needs_label"),
        "results": results,
    }
    RESULTS.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"评估完成：{passed}/{len(cases)} 通过（{out['pass_rate']:.1%}）")
    for x in results:
        mark = "PASS" if x["pass"] else "FAIL"
        print(f"  {mark} {x['case_id']} steps={x.get('steps')} scores={x.get('scores')} attribution={x.get('attribution_status')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="只评估前 N 个用例（控制成本）")
    args = ap.parse_args()
    main(args.limit)
