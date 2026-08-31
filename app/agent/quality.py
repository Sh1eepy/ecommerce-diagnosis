"""结构化报告的确定性质量检查，供在线 Agent 与离线评估复用。"""
from __future__ import annotations

import math
import re
from typing import Any

from app.metrics.windows import has_full_observations


def evidence_limits(evidence: dict[str, dict]) -> dict[str, str]:
    """由服务端工具能力推导边界，不接受模型自报的“已验证因果”标记。

    现有工具均为观察性查询，没有因果验证或损失核算能力。新增工具时必须
    单独设计、测试其证明能力，不能仅靠 report 内一个布尔值放行。
    """
    limits = {"causal_unverified": "现有证据只能支持观察与相关性，尚不能确认或排除经营异常的具体原因；假设置信度不是经校准的概率。"}
    for entry in evidence.values():
        if not isinstance(entry, dict):
            continue
        tool = entry.get("tool")
        data = entry.get("data")
        if not isinstance(data, dict):
            continue
        if tool == "peer":
            limits["category_baseline_missing"] = "同行只有本窗口横向数据，没有同行历史基线，不能判断大盘趋势或排除大盘影响。"
        if tool == "metric":
            summary = data.get("summary")
            samples = summary.get("sample_counts", {}) if isinstance(summary, dict) else {}
            if any(isinstance(v, dict) and isinstance(v.get("transaction_count"), (int, float))
                   and v["transaction_count"] < 20 for v in samples.values()):
                limits["small_sample"] = "前后至少一个窗口的成交不足20笔，变化可能受少量成交影响；这是谨慎提示，不是统计显著性检验，不能仅凭百分比认定经营恶化。"
            windows_coverage = summary.get("coverage") if isinstance(summary, dict) else None
            if not isinstance(windows_coverage, dict) or not all(
                has_full_observations(windows_coverage.get(key)) for key in ("current", "previous")
            ):
                limits["daily_coverage_unverified"] = "当前或上一窗口缺少日记录，或旧证据未记录双窗口覆盖；可能无事件或漏数据，不能默认补零或直接解释环比。"
            if data.get("unavailable_dates"):
                limits["availability_observations_only"] = "不可用日期只是观察点，未重建整个窗口的生效状态，也未验证不可售与成交变化的因果关系。"
        # metric / peer / dimension 都可能携带 GMV 或客单价，历史 checkpoint 也适用。
        def has_amount(value):
            if isinstance(value, dict):
                return any(k in {"gmv", "avg_price"} or has_amount(v) for k, v in value.items())
            return isinstance(value, list) and any(has_amount(v) for v in value)

        if has_amount(data):
            limits["gmv_proxy_not_loss"] = "GMV 使用成交笔数乘商品最新价的近似口径；窗口差额不是实际损失，不能据此确认货币单位或损失金额。"
    return limits


def _report_texts(report: dict):
    """保留字段语境；建议 action 不能给另一句确定性断言豁免。"""
    analysis = report.get("analysis")
    analysis = analysis if isinstance(analysis, dict) else {}
    for key in ("key_finding", "impact"):
        yield f"analysis.{key}", analysis.get(key), ""
    if isinstance(analysis.get("limitations"), list):
        for i, value in enumerate(analysis["limitations"]):
            yield f"analysis.limitations[{i}]", value, ""
    yield "conclusion", report.get("conclusion"), ""
    for field, keys in (("facts", ("point",)), ("hypotheses", ("statement",)),
                        ("suggestions", ("action", "rationale", "success_metric"))):
        entries = report.get(field)
        if isinstance(entries, list):
            for i, entry in enumerate(entries):
                if isinstance(entry, dict):
                    for key in keys:
                        yield f"{field}[{i}].{key}", entry.get(key), entry.get("action", "")


def _overclaim_errors(report: dict, evidence: dict | None = None) -> list[str]:
    """有限的中英文过度断言检查；不是通用自然语言蕴含或因果证明器。"""
    patterns = {
        "significance_unverified": (r"(?:显著下降|显著下跌|显著低于|显著恶化|统计显著)",),
        "category_baseline_missing": (
            r"(?:大盘|类目|同行).{0,10}(?:正常|稳定|无异常|未下跌|未下降|整体下跌|整体下降|上涨)",
            r"排除.{0,10}(?:大盘|类目|同行)",
            r"(?:market|category|peers?)\s+(?:is\s+|are\s+)?(?:normal|stable|healthy|declining)",
            r"(?:rule[ds]? out|exclude[ds]?).{0,16}(?:market|category)",
        ),
        "causal_unverified": (
            r"(?:下架|不可售|不可用|库存|available\s*=\s*0).{0,20}(?:导致|造成|所致|根因|直接原因)",
            r"(?:原因|根因|所致|导致).{0,16}(?:下架|不可售|不可用|库存)",
            r"(?:确认|证实|确定|表明).{0,16}(?:整个窗口|全窗口|全程|窗口内).{0,8}(?:不可售|下架)",
            r"(?:unavailable|delisted|out.of.stock).{0,25}(?:caused|causes|resulted in)",
            r"(?:已(?:经)?(?:确认|证实)|事实表明).{0,12}(?:导致|造成|所致|根因|直接原因)",
        ),
        "gmv_proxy_not_loss": (
            r"(?:gmv|销售额|成交额).{0,10}(?:损失|损害)",
            r"损失.{0,6}\d.{0,8}(?:元|万|亿)",
            r"(?:gmv|revenue).{0,12}(?:loss|lost)",
        ),
        # 当前注册工具只有浏览/加购/成交聚合值，没有支付事件或用户会话链路。
        "payment_evidence_missing": (
            r"(?:支付|付款)(?:环节|阶段|链路|流程)?(?:的)?(?:转化率|成功率|失败率|次数|笔数)",
            r"(?:无|未|没有)(?:支付|付款)(?!日志|记录|数据|信息|证据)",
            r"(?:支付|付款).{0,12}(?:受阻|异常|故障|失败|无成交|没有成交)",
            r"(?:定位|集中|问题出在|问题在|卡在|瓶颈在).{0,8}(?:支付|付款)",
        ),
    }
    fixes = {
        "payment_evidence_missing": "改为浏览/加购/成交的聚合观察；没有支付日志，支付故障只能作为待核查假设，不能给出支付率或确认支付定位",
        "daily_coverage_unverified": "只列前后窗口各自已记录的数值并说明缺记录；删除增减、正常、环比及自行计算的差额，不把缺行补零",
        "causal_unverified": "事实中删除已确认因果；核查建议写成“请核查是否存在…”，未知原因保留待验证",
    }
    coverage_patterns = (
        r"(?:访客(?:数)?|流量|uv|浏览(?:量|数)?|成交(?:笔数|量)|cvr|转化率|gmv|销售额).{0,24}(?:增加|增长|上涨|上升|减少|下降|下跌|降至|升至|提高|降低|正常|稳定|未降|差额)",
        r"(?:较|相比|相较|比)(?:上期|上一窗口|上个窗口|前期).{0,16}(?:增|减|升|降)",
        r"(?:差额|环比).{0,8}\d",
    )
    limits = evidence_limits(evidence) if evidence is not None else {}
    errors = set()
    for path, value, action in _report_texts(report):
        if not isinstance(value, str):
            continue
        active_patterns = dict(patterns)
        field_limits = limits
        # 单条事实按其引用取覆盖边界，避免别的工具缺覆盖污染完整的事实证据。
        if evidence is not None and path.startswith("facts["):
            fact = report["facts"][int(path.split("[")[1].split("]")[0])]
            ref = fact.get("evidence_ref")
            call_id = ref.get("call_id") if isinstance(ref, dict) else None
            if isinstance(call_id, str) and call_id in evidence:
                field_limits = evidence_limits({call_id: evidence[call_id]})
        if "daily_coverage_unverified" in field_limits:
            active_patterns["daily_coverage_unverified"] = coverage_patterns
        # 逐分句判断，避免在别处加一句“可能”就绕过确定性结论。
        # 仅规整横向空白，保留换行这一语义边界，避免前句否定修饰后句断言。
        text = re.sub(r"(?<=[\u4e00-\u9fff])[ \t\u3000]+(?=[\u4e00-\u9fff])", "", value.lower())
        for clause in re.split(r"[，,。；;！!？?\n]|但是|然而|不过|但", text):
            for code, expressions in active_patterns.items():
                for expression in expressions:
                    for match in re.finditer(expression, clause):
                        prefix = clause[:match.start()]
                        negative = re.search(r"(?:不能|无法|尚未|不足以|未能|不应|未确认|未证实|并非|不是|不等于|不得|不可|缺少|缺乏|尚无|cannot|not|unconfirmed).{0,12}$", prefix)
                        negative = negative or re.match(r"(?:仍|尚|暂)?(?:未知|不明|未验证|无法计算|不可计算|尚不清楚)", clause[match.end():])
                        tentative = re.search(r"(?:可能|或许|疑似|待验证|待核实|是否|may|might|possibly).{0,8}$", prefix)
                        # “确认是否存在支付故障或商品下架导致…”的问句语境不能用8字截断。
                        question = re.search(r"是否[^，。；!?！？\n]{0,80}$", prefix)
                        checking_action = isinstance(action, str) and re.match(
                            r"^(?:请.{0,8}?)?(?:核查|核实|排查|检查|核对|调查|查明|确认是否|验证是否)", action)
                        pending_suggestion = path.startswith("suggestions[") and checking_action and re.match(
                            r"^(?:请.{0,8}?)?(?:排除|核查|核实|排查|检查|核对|判断是否|确认是否|验证是否)", clause.strip())
                        if code == "payment_evidence_missing" and path.endswith(".success_metric") and checking_action:
                            # 验收目标“获取支付失败记录”不是声称已经拿到记录或确认了故障。
                            pending_suggestion = pending_suggestion or re.match(
                                r"^(?:获取|取得|收集|查阅).{0,60}(?:日志|记录|证据|反馈)", clause.strip())
                            # “确认或排除”保留两种核查结果，只在核查建议的验收目标中适用。
                            # 下方仍逐句检查“已确认”等断言，不能用目标为后续结论开脱。
                            pending_suggestion = pending_suggestion or re.match(
                                r"^(?:确认|证实|验证)(?:或|或者)(?:排除|否定)(?:支付|付款)", clause.strip())
                        asserted = re.search(r"已(?:经)?(?:确认|证实|确定|排除)|事实表明|必然|肯定|就是", clause)
                        if asserted:
                            tentative = question = pending_suggestion = False
                        # “库存变化可能导致”中的限定词在匹配内部，且必须紧贴
                        # 因果谓词；别处的“可能”仍不能替确定性断言开脱。
                        if code in {"causal_unverified", "payment_evidence_missing"} and not asserted:
                            tentative = tentative or re.search(
                                r"(?:可能|或许|疑似|是否)(?:是|为|存在|出现)?(?:导致|造成|所致|根因|直接原因|受阻|异常|故障|失败)$",
                                match.group(),
                            )
                        qualified = tentative or question or pending_suggestion
                        # 缺覆盖不能靠“可能”计算环比；聚合工具不能支持猜测支付率。
                        if code in {"gmv_proxy_not_loss", "daily_coverage_unverified"}:
                            qualified = False
                        if code == "payment_evidence_missing" and re.search(r"转化率|成功率|失败率|次数|笔数", match.group()):
                            qualified = question or pending_suggestion
                        if not negative and not qualified:
                            fix = fixes.get(code, "改为可回查事实或待验证假设，不得直接确认趋势、因果或实际损失")
                            errors.add(f"证据越界[{code}] {path}：原句“{clause.strip()[:100]}”；{fix}")
    return sorted(errors)


def resolve_path(data: Any, path: str) -> Any:
    """解析 data.summary.current.cvr 或 stages.1.count 形式的证据路径。"""
    cur = data
    if not isinstance(path, str) or not path or any(not part for part in path.split(".")):
        raise KeyError(path)
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise KeyError(path)
    return cur


def _numeric_equal(claimed: Any, actual: Any) -> bool:
    try:
        a, b = float(claimed), float(actual)
    except (TypeError, ValueError):
        return False
    return math.isclose(a, b, rel_tol=0.015, abs_tol=0.01)


def _claims_metric_zero(narrative: str, term: str, metric_terms: dict[str, list[str]]) -> bool:
    """有限的局部主语检查，避免把别的指标的零值套到本指标上。"""
    labels = sorted({x for terms in metric_terms.values() for x in terms}, key=len, reverse=True)
    label_pattern = "|".join(re.escape(x) for x in labels)
    pattern = re.escape(term) + r"(?P<between>.{0,24}?)(?:归零|为\s*0(?![\d.]))"
    # 不跨分句/转折或不同报告字段寻找谓词。
    for clause in re.split(r"[，,。；;！!？?\n]|但是|不过|然而|但|而", narrative):
        for match in re.finditer(pattern, clause):
            between = match.group("between")
            if re.search(label_pattern, between):
                # 仍拦截“UV和成交均为0”这类并列主语；“UV增加成交为0”
                # 则已切换主语，不能推断 UV 为零。
                remainder = re.sub(label_pattern, "", between)
                if not re.fullmatch(r"(?:\s|和|与|及|、|均|都|全部|已|已经|数|量)*", remainder):
                    continue
            return True
    return False


def evaluate_report(report: dict, evidence: dict[str, dict]) -> dict:
    """校验事实与边界；通过表示报告合格，不表示根因已经证实。"""
    errors: list[str] = []
    facts = report.get("facts") if isinstance(report.get("facts"), list) else []
    hypotheses = report.get("hypotheses") if isinstance(report.get("hypotheses"), list) else []
    suggestions = report.get("suggestions") if isinstance(report.get("suggestions"), list) else []

    valid_fact_refs = 0
    numeric_total = numeric_valid = 0
    nonzero_metrics: set[str] = set()
    for i, fact in enumerate(facts):
        if not isinstance(fact, dict):
            errors.append(f"facts[{i}] 不是对象")
            continue
        if not isinstance(fact.get("point"), str) or not fact["point"].strip():
            errors.append(f"facts[{i}] 的 point 必须为非空字符串")
        # 旧报告没有 section 仍兼容；新报告可将环节/人群定位单独展示。
        if "section" in fact and fact["section"] not in ("change", "focus"):
            errors.append(f"facts[{i}] 的 section 必须为 change 或 focus")
        ref = fact.get("evidence_ref") or {}
        if not isinstance(ref, dict):
            errors.append(f"facts[{i}] 的 evidence_ref 必须是对象")
            continue
        call_id, path = ref.get("call_id"), ref.get("path")
        if not isinstance(call_id, str) or call_id not in evidence:
            errors.append(f"facts[{i}] 引用了不存在或失败的证据 {call_id!r}")
            continue
        try:
            actual = resolve_path(evidence[call_id].get("data"), path)
        except (KeyError, TypeError):
            errors.append(f"facts[{i}] 的证据路径不存在: {call_id}:{path}")
            continue
        valid_fact_refs += 1
        if "value" in fact:
            claimed = fact["value"]
            numeric_pair = (
                isinstance(claimed, (int, float)) and not isinstance(claimed, bool)
                and isinstance(actual, (int, float, str))
            )
            if numeric_pair:
                numeric_total += 1
                if _numeric_equal(claimed, actual):
                    numeric_valid += 1
                    try:
                        if float(actual) != 0 and fact.get("metric"):
                            nonzero_metrics.add(str(fact["metric"]).lower())
                    except (TypeError, ValueError):
                        pass
                else:
                    errors.append(f"facts[{i}] 数值 {claimed} 与证据 {actual} 不一致")
            elif claimed != actual:
                errors.append(f"facts[{i}] 值 {claimed!r} 与证据 {actual!r} 不一致")

    hypothesis_ids: set[str] = set()
    hypothesis_refs_valid = True
    for i, h in enumerate(hypotheses):
        if not isinstance(h, dict) or not h.get("id") or not h.get("statement"):
            errors.append(f"hypotheses[{i}] 缺少 id/statement")
            hypothesis_refs_valid = False
            continue
        hid = str(h["id"])
        refs = h.get("evidence_refs") if isinstance(h.get("evidence_refs"), list) else []
        if hid in hypothesis_ids:
            errors.append(f"假设 ID 重复: {hid}")
            hypothesis_refs_valid = False
        hypothesis_ids.add(hid)
        if not refs or any(not isinstance(ref, str) or ref not in evidence for ref in refs):
            errors.append(f"假设 {hid} 没有有效 evidence_refs")
            hypothesis_refs_valid = False
        if h.get("status") not in ("uncertain", "active"):
            errors.append(f"假设 {hid} 尚无因果验证，status 必须为 uncertain 或 active，不能仅凭引用就确认或排除原因")
            hypothesis_refs_valid = False
        try:
            confidence = float(h.get("confidence"))
            if not 0 <= confidence <= 1:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"假设 {hid} 的 confidence 必须在 0~1")
            hypothesis_refs_valid = False

    analysis = report.get("analysis") if isinstance(report.get("analysis"), dict) else {}
    attribution_ok = analysis.get("attribution_status") == "uncertain" and analysis.get("primary_hypothesis_id") is None
    if not attribution_ok:
        errors.append("当前工具不能证实因果：analysis.attribution_status 必须为 uncertain，primary_hypothesis_id 为 null；可在 hypotheses 保留待验证原因")
    limitations = analysis.get("limitations")
    uncertainty_ok = isinstance(limitations, list) and bool(limitations) and all(isinstance(x, str) and x.strip() for x in limitations)
    if not uncertainty_ok:
        errors.append("analysis.limitations 必须明确列出尚未确认的部分，不能是空列表或字符串")
    errors.extend(_overclaim_errors(report, evidence))

    # 防止结构化事实正确、自然语言总结却自相矛盾（如事实 UV=13，结论称 UV 全部归零）。
    narrative = "\n".join([
        str(analysis.get("key_finding") or ""),
        str(analysis.get("impact") or ""),
        str(report.get("conclusion") or ""),
    ]).lower()
    metric_terms = {
        "uv": ["uv", "访客"],
        "gmv": ["gmv"],
        "addtocart_count": ["加购"],
        "transaction_count": ["成交"],
        "cvr": ["cvr", "转化率"],
    }
    for metric in sorted(nonzero_metrics):
        for term in metric_terms.get(metric, [metric]):
            if _claims_metric_zero(narrative, term, metric_terms):
                errors.append(f"叙述与事实矛盾：{metric} 的证据值非零，却声称归零")
                break

    actionable = 0
    for s in suggestions:
        if isinstance(s, dict) and all(
            isinstance(s.get(k), str) and s[k].strip()
            for k in ("action", "rationale", "owner", "priority", "success_metric")
        ) and s["priority"] in ("P0", "P1", "P2"):
            actionable += 1
    actionability_ok = bool(suggestions) and actionable == len(suggestions)
    if not actionability_ok:
        errors.append("每条建议必须包含非空字符串 action/rationale/owner/priority/success_metric，priority 为 P0/P1/P2")

    # 可以只报告已核对的事实与未知部分，无需为了过关编造一个原因假设。
    evidence_support_ok = bool(facts) and valid_fact_refs == len(facts) and hypothesis_refs_valid
    numeric_consistency_ok = numeric_total > 0 and numeric_valid == numeric_total
    if numeric_total == 0:
        errors.append("至少一条事实必须包含可回查的数值 value")

    scores = {
        "numeric_consistency": round(numeric_valid / numeric_total, 3) if numeric_total else 0.0,
        "evidence_support": 1.0 if evidence_support_ok else 0.0,
        "attribution_grounding": 1.0 if attribution_ok else 0.0,
        "recommendation_actionability": round(actionable / len(suggestions), 3) if suggestions else 0.0,
    }
    narrative_consistency_ok = not any(e.startswith("叙述与事实矛盾") for e in errors)
    scores["narrative_consistency"] = 1.0 if narrative_consistency_ok else 0.0
    scores["uncertainty_disclosure"] = 1.0 if uncertainty_ok and not any(e.startswith("证据越界") for e in errors) else 0.0
    return {
        "passed": not errors and numeric_consistency_ok and evidence_support_ok and attribution_ok and actionability_ok and uncertainty_ok,
        "scores": scores,
        "errors": errors,
        "attribution_status": "uncertain",
        "evidence_limits": evidence_limits(evidence),
    }
