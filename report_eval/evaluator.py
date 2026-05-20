"""
研报评测引擎 —— 100 分制 4 维度评测。

权重分配：
  - 维度一：事实数据的时效性与准确率  40 分
  - 维度二：结果数据的可回溯性        30 分
  - 维度三：分析过程的专业性与一致性  20 分
  - 维度四：合规性                    10 分
"""
import math
import re
import traceback
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict

from loguru import logger

from report_eval.rubric import (
    RED_TERMS,
    compute_grade,
    AGGRESSIVE_RATINGS,
    AGGRESSIVE_ADVICE_TERMS,
    CONSERVATIVE_ADVICE_TERMS,
    RISK_REWARD_THRESHOLDS,
    REQUIRED_ANALYSIS_META,
)

_CAUSAL_TERMS = ["因为", "由于", "导致", "支撑", "验证", "触发", "核心逻辑", "关键风险", "因此", "显示"]
_BULL_TERMS = ["多方", "看涨", "机会", "乐观"]
_BEAR_TERMS = ["空方", "风险", "悲观", "观望", "减仓"]
_NUMBER = r"-?\d+(?:\.\d+)?"


# ── 数据模型 ──

@dataclass
class IssueItem:
    severity: str
    message: str
    evidence: str = ""
    suggestion: str = ""

    def to_str(self) -> str:
        return f"[{self.severity}] {self.message}"


@dataclass
class DimensionScore:
    name: str
    max_score: float
    score: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)
    structured_issues: List[IssueItem] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for i in self.structured_issues if i.severity == "CRITICAL")

    def add_issue(self, severity: str, message: str, evidence: str = "", suggestion: str = ""):
        item = IssueItem(severity=severity, message=message, evidence=evidence, suggestion=suggestion)
        self.structured_issues.append(item)
        self.issues.append(item.to_str())


@dataclass
class EvalResult:
    total_score: float = 0.0
    grade: str = ""
    has_red_flag: bool = False
    red_flags: List[str] = field(default_factory=list)

    dim1_fact: Optional[DimensionScore] = None
    dim2_result: Optional[DimensionScore] = None
    dim3_analysis: Optional[DimensionScore] = None
    dim4_compliance: Optional[DimensionScore] = None

    traceable_fields: Dict[str, Any] = field(default_factory=dict)
    fix_suggestions: List[str] = field(default_factory=list)

    @property
    def total_critical_count(self) -> int:
        count = 0
        for dim in [self.dim1_fact, self.dim2_result, self.dim3_analysis, self.dim4_compliance]:
            if dim:
                count += dim.critical_count
        return count

    def to_dict(self) -> dict:
        d = {
            "total_score": round(self.total_score, 2),
            "grade": self.grade,
            "has_red_flag": self.has_red_flag,
            "red_flags": self.red_flags,
            "traceable_fields": self.traceable_fields,
            "fix_suggestions": self.fix_suggestions,
        }
        for dim_name in ["dim1_fact", "dim2_result", "dim3_analysis", "dim4_compliance"]:
            dim = getattr(self, dim_name)
            if dim:
                d[dim_name] = asdict(dim)
        return d


# ── 规则引擎 ──

def rule_check_compliance(text: str) -> dict:
    """纯规则合规检查，基于 RED_TERMS 字典。"""
    guaranteed = sorted({t for t in RED_TERMS.get("guaranteed_return", []) if t in text})
    strong_directives = sorted({t for t in RED_TERMS.get("strong_instruction", []) if t in text})
    personal_positions = sorted({t for t in RED_TERMS.get("personalized_allocation", []) if t in text})
    marketing = sorted({t for t in RED_TERMS.get("marketing_language", []) if t in text})

    risk_terms = ["风险", "止损", "回撤", "不确定", "免责声明"]
    has_risk_disclosure = any(t in text for t in risk_terms)
    has_disclaimer = "不构成投资建议" in text

    red_flags = []
    if guaranteed:
        red_flags.append(f"确定性收益表述: {', '.join(guaranteed[:3])}")
    if strong_directives:
        red_flags.append(f"强指令操作表述: {', '.join(strong_directives[:3])}")
    if len(marketing) >= 3:
        red_flags.append(f"大量营销话术({len(marketing)}处)可能误导用户")

    return {
        "guaranteed_return_phrases": guaranteed,
        "strong_directive_phrases": strong_directives,
        "personal_position_phrases": personal_positions,
        "marketing_phrases": marketing,
        "has_risk_disclosure": has_risk_disclosure,
        "has_disclaimer": has_disclaimer,
        "missing_source_citations": [],
        "rating_rrr_inconsistency": None,
        "red_flags": red_flags,
        "_source": "rule",
    }


def rule_check_analysis_quality(text: str) -> dict:
    """纯规则分析质量检查。"""
    causal_hits = sum(1 for t in _CAUSAL_TERMS if t in text)
    causal_score = min(6, causal_hits / 7 * 6)
    causal_chain_issues = [] if causal_hits >= 4 else ["因果链关键词偏少，分析深度不足"]

    has_bull = any(t in text for t in _BULL_TERMS)
    has_bear = any(t in text for t in _BEAR_TERMS)
    balance_score = 3.0 if (has_bull and has_bear) else 1.5
    balance_issues = [] if (has_bull and has_bear) else ["多空观点呈现不平衡"]

    consistency_issues = []
    consistency_details = {}

    # 4a 评级前后一致性
    rating_score = 1.5
    rating_issue = None
    aggressive_in_header = any(t in text[:300] for t in AGGRESSIVE_RATINGS + AGGRESSIVE_ADVICE_TERMS)
    conservative_in_body = any(t in text for t in CONSERVATIVE_ADVICE_TERMS)
    if aggressive_in_header and conservative_in_body and not any(t in text[:300] for t in CONSERVATIVE_ADVICE_TERMS):
        rating_score = 0.75
        rating_issue = "标题偏积极但正文含保守建议"
        consistency_issues.append(rating_issue)
    consistency_details["rating_consistency"] = {"score": rating_score, "issue": rating_issue}

    # 4b 数值重复一致性
    numeric_score = 1.5
    numeric_issue = None
    conflicting_examples = []
    _num_pat = re.compile(r"-?\d+(?:\.\d+)?")
    _check_fields = [
        ("主力净流入", 0.1), ("主力净流出", 0.1),
        ("当前价", 0.5), ("最新价", 0.5),
        ("目标价", 0.5), ("止损价", 0.5),
    ]
    for label, tol in _check_fields:
        positions = [m.start() for m in re.finditer(re.escape(label), text)]
        if len(positions) >= 2:
            vals = []
            for pos in positions[:4]:
                snippet = text[pos: pos + 60]
                nums = [float(n) for n in _num_pat.findall(snippet) if float(n) > 0]
                if nums:
                    vals.append(nums[0])
            unique_vals = set(round(v, 1) for v in vals)
            if len(unique_vals) >= 2:
                conflicting_examples.append(f"{label}: {sorted(unique_vals)}")
    if conflicting_examples:
        deduct = min(1.5, 0.75 * len(conflicting_examples))
        numeric_score = max(0.0, 1.5 - deduct)
        numeric_issue = f"发现 {len(conflicting_examples)} 处数值矛盾"
        consistency_issues.append(numeric_issue)
    consistency_details["numeric_consistency"] = {
        "score": numeric_score, "issue": numeric_issue, "conflicting_examples": conflicting_examples,
    }

    # 4c 多维信号一致性
    signal_score = 1.5
    signal_issue = None
    conflicting_signals = []
    kdj_overbought = bool(re.search(r"KDJ[^\d]{0,10}([89]\d|1[0-9]\d)", text))
    macd_dead = "MACD死叉" in text or "死叉" in text
    main_outflow = "主力净流出" in text and "主力净流入" not in text
    aggressive_conclusion = any(t in text[-500:] for t in AGGRESSIVE_ADVICE_TERMS + AGGRESSIVE_RATINGS)
    if kdj_overbought and aggressive_conclusion:
        conflicting_signals.append("KDJ超买区间，但结论偏积极")
    if macd_dead and aggressive_conclusion:
        conflicting_signals.append("MACD死叉，但结论偏积极")
    if main_outflow and aggressive_conclusion:
        conflicting_signals.append("主力净流出，但结论偏积极")
    if len(conflicting_signals) >= 2:
        signal_score = 0.0
        signal_issue = f"多维信号与结论方向相反: {'; '.join(conflicting_signals)}"
        consistency_issues.append(signal_issue)
    elif len(conflicting_signals) == 1:
        signal_score = 1.0
        signal_issue = f"信号轻微矛盾: {conflicting_signals[0]}"
    consistency_details["signal_consistency"] = {
        "score": signal_score, "issue": signal_issue, "conflicting_signals": conflicting_signals,
    }

    # 4d 情景推演完整性
    scenario_score = 1.5
    scenario_issue_parts = []
    probs = [float(x) for x in re.findall(rf"(?:乐观|基准|悲观)(?:剧本|情景|场景)[^%]{{0,80}}({_NUMBER})%", text)]
    prob_sum = round(sum(probs), 1) if probs else None
    has_optimistic = any(w in text for w in ["乐观剧本", "乐观情景", "乐观场景", "乐观预期"])
    has_pessimistic = any(w in text for w in ["悲观剧本", "悲观情景", "悲观场景", "悲观预期"])
    if not (has_optimistic and has_pessimistic):
        scenario_score -= 0.5
        scenario_issue_parts.append("缺少完整的多情景推演")
    if prob_sum is not None and not math.isclose(prob_sum, 100.0, abs_tol=5.0):
        scenario_score -= 0.5
        scenario_issue_parts.append(f"情景概率之和={prob_sum}%")
    has_trigger = any(w in text for w in ["触发条件", "当…时", "若…则", "一旦"])
    has_stop = any(w in text for w in ["失效条件", "止损条件", "若跌破", "止损价"])
    if not has_trigger:
        scenario_score -= 0.25
        scenario_issue_parts.append("情景缺少触发条件")
    if not has_stop:
        scenario_score -= 0.25
        scenario_issue_parts.append("情景缺少失效条件")
    scenario_score = max(0.0, scenario_score)
    scenario_issue = "；".join(scenario_issue_parts) if scenario_issue_parts else None
    if scenario_issue:
        consistency_issues.append(scenario_issue)
    consistency_details["scenario_completeness"] = {
        "score": scenario_score, "issue": scenario_issue, "scenario_probability_sum": prob_sum,
    }

    consistency_score = round(rating_score + numeric_score + signal_score + scenario_score, 2)

    matched = [m for m in REQUIRED_ANALYSIS_META if m in text]
    framework_score = 5.0 * len(matched) / len(REQUIRED_ANALYSIS_META)

    return {
        "framework_score": round(framework_score, 2),
        "framework_coverage": {m: (m in text) for m in REQUIRED_ANALYSIS_META},
        "causal_chain_score": round(causal_score, 2),
        "causal_chain_issues": causal_chain_issues,
        "balance_score": round(balance_score, 2),
        "balance_issues": balance_issues,
        "consistency_score": consistency_score,
        "consistency_issues": consistency_issues,
        "consistency_details": consistency_details,
        "scenario_probability_sum": prob_sum,
        "header_vs_final_rating_match": None,
        "_source": "rule",
    }


# ── 主评测器 ──

class ReportEvaluator:
    """研报评测器，接收各步骤的中间结果产出最终评分。"""

    def evaluate(
        self,
        check_results: List[dict],
        structured_fields: dict,
        analysis_quality: dict,
        compliance: dict,
        standard_full_data: Optional[dict] = None,
    ) -> EvalResult:
        result = EvalResult()

        try:
            result.dim1_fact = self._eval_fact_accuracy(check_results, standard_full_data)
        except Exception as e:
            logger.error(f"维度一评分失败: {e}\n{traceback.format_exc()}")
            result.dim1_fact = DimensionScore(name="事实数据", max_score=40, score=0)

        try:
            result.dim2_result = self._eval_result_traceability(structured_fields, check_results)
        except Exception as e:
            logger.error(f"维度二评分失败: {e}\n{traceback.format_exc()}")
            result.dim2_result = DimensionScore(name="结果数据", max_score=30, score=0)

        try:
            result.dim3_analysis = self._eval_analysis_quality(analysis_quality)
        except Exception as e:
            logger.error(f"维度三评分失败: {e}\n{traceback.format_exc()}")
            result.dim3_analysis = DimensionScore(name="分析过程", max_score=20, score=0)

        try:
            result.dim4_compliance = self._eval_compliance(compliance)
        except Exception as e:
            logger.error(f"维度四评分失败: {e}\n{traceback.format_exc()}")
            result.dim4_compliance = DimensionScore(name="合规性", max_score=10, score=0)

        result.total_score = (
            result.dim1_fact.score + result.dim2_result.score
            + result.dim3_analysis.score + result.dim4_compliance.score
        )

        all_red_flags = list(result.dim4_compliance.details.get("red_flags", []))
        result.red_flags = all_red_flags
        result.has_red_flag = len(all_red_flags) > 0

        result.grade = compute_grade(result.total_score, result.total_critical_count)
        result.traceable_fields = structured_fields or {}
        result.fix_suggestions = self._generate_suggestions(result)

        return result

    def _eval_fact_accuracy(self, check_results: List[dict], standard_full: Optional[dict]) -> DimensionScore:
        dim = DimensionScore(name="事实数据", max_score=40)
        if not check_results:
            dim.add_issue("HIGH", "无指标数据可评测")
            return dim

        total = len(check_results)
        correct = sum(1 for r in check_results if r.get("result") == "correct")
        error = sum(1 for r in check_results if r.get("result") == "error")
        unknown = sum(1 for r in check_results if r.get("result") == "unknown")
        known = correct + error
        accuracy = correct / known if known > 0 else 0

        accuracy_score = round(accuracy * 18, 2)

        time_score = 9.0
        if standard_full:
            if not standard_full.get("real_line_trend"):
                time_score -= 3
            if not standard_full.get("factor"):
                time_score -= 2

        consistency_score = 8.0
        error_indicators = [r["indicator"] for r in check_results if r.get("result") == "error"]
        if error_indicators:
            deduction = min(8.0, len(error_indicators) * 1.0)
            consistency_score -= deduction
            dim.add_issue(
                "HIGH" if len(error_indicators) >= 3 else "MEDIUM",
                f"指标数据口径不一致: {', '.join(error_indicators[:5])}",
            )

        source_score = 5.0
        unknown_ratio = unknown / total if total > 0 else 0
        if unknown_ratio > 0.5:
            source_score -= 3
        elif unknown_ratio > 0.3:
            source_score -= 1.5

        dim.score = max(0, accuracy_score + time_score + consistency_score + source_score)
        dim.details = {
            "total_indicators": total, "correct": correct, "error": error,
            "unknown": unknown, "accuracy": round(accuracy, 4),
            "accuracy_score": accuracy_score, "time_score": time_score,
            "consistency_score": consistency_score, "source_score": source_score,
            "error_indicators": error_indicators,
        }
        return dim

    def _eval_result_traceability(self, fields: dict, check_results: List[dict]) -> DimensionScore:
        dim = DimensionScore(name="结果数据", max_score=30)
        if not fields:
            dim.add_issue("HIGH", "未能提取结构化字段")
            return dim

        source_score = 10.0
        key_fields = ["target_price", "stop_loss_price", "current_price", "support_price", "resistance_price"]
        missing = [f for f in key_fields if not fields.get(f)]
        if missing:
            source_score -= min(10, len(missing) * 2)

        calc_score = 5.0
        curr = fields.get("current_price")
        target = fields.get("target_price")
        reported_upside = fields.get("upside_pct")

        if curr and target and isinstance(curr, (int, float)) and isinstance(target, (int, float)):
            expected_upside = (target - curr) / curr * 100 if curr != 0 else None
            if reported_upside is not None and expected_upside is not None:
                if abs(reported_upside - expected_upside) > 1.0:
                    calc_score -= 2

        formula_fields = ["current_price", "target_price", "stop_loss_price",
                          "upside_pct", "downside_pct", "risk_reward_ratio"]
        formula_present = sum(1 for f in formula_fields if fields.get(f) is not None)
        formula_score = 5.0 * formula_present / len(formula_fields)

        advice_score = 10.0
        rrr = fields.get("risk_reward_ratio")
        rating = fields.get("rating") or fields.get("final_rating") or ""

        if rrr is not None and isinstance(rrr, (int, float)):
            is_aggressive = any(r in str(rating) for r in AGGRESSIVE_RATINGS)
            if rrr < RISK_REWARD_THRESHOLDS["poor"] and is_aggressive:
                advice_score -= 8
                dim.add_issue("CRITICAL", f"风险收益比仅 {rrr:.2f}:1 但评级积极")
            elif rrr < RISK_REWARD_THRESHOLDS["acceptable"] and is_aggressive:
                advice_score -= 4

        dim.score = max(0, source_score + calc_score + formula_score + advice_score)
        dim.details = {
            "source_score": round(source_score, 2), "calc_score": round(calc_score, 2),
            "formula_score": round(formula_score, 2), "advice_score": round(advice_score, 2),
            "risk_reward_ratio": rrr, "rating": rating, "missing_fields": missing,
        }
        return dim

    def _eval_analysis_quality(self, aq: dict) -> DimensionScore:
        dim = DimensionScore(name="分析过程", max_score=20)
        if not aq:
            dim.add_issue("HIGH", "分析质量评估未返回结果")
            return dim

        framework_score = min(5, max(0, aq.get("framework_score", 0)))
        causal_score = min(6, max(0, aq.get("causal_chain_score", 0)))
        balance_score = min(3, max(0, aq.get("balance_score", 0)))
        consistency_score = min(6, max(0, aq.get("consistency_score", 0)))

        dim.score = framework_score + causal_score + balance_score + consistency_score

        for k in ["causal_chain_issues", "balance_issues", "consistency_issues"]:
            for msg in aq.get(k, []):
                dim.add_issue("MEDIUM", msg)

        prob_sum = aq.get("scenario_probability_sum")
        if prob_sum is not None and abs(prob_sum - 100) > 5:
            dim.score = max(0, dim.score - 0.5)

        dim.details = {
            "framework_score": framework_score, "causal_chain_score": causal_score,
            "balance_score": balance_score, "consistency_score": consistency_score,
            "framework_coverage": aq.get("framework_coverage", {}),
            "scenario_probability_sum": prob_sum,
            "analysis_source": aq.get("_source", "llm"),
        }
        return dim

    def _eval_compliance(self, comp: dict) -> DimensionScore:
        dim = DimensionScore(name="合规性", max_score=10)
        if not comp:
            dim.add_issue("HIGH", "合规检查未返回结果")
            return dim

        score = 10.0
        red_flags = list(comp.get("red_flags", []))

        if not comp.get("has_risk_disclosure"):
            score -= 3
            dim.add_issue("HIGH", "缺少风险揭示")

        strong_directives = comp.get("strong_directive_phrases", [])
        if strong_directives:
            score -= 2
            dim.add_issue("HIGH", f"强指令式表述: {', '.join(strong_directives[:3])}")

        guaranteed = comp.get("guaranteed_return_phrases", [])
        if guaranteed:
            score -= 2
            dim.add_issue("CRITICAL", f"确定性收益表述: {', '.join(guaranteed[:3])}")
            if not any("收益" in rf for rf in red_flags):
                red_flags.append(f"确定性收益表述: {', '.join(guaranteed[:2])}")

        marketing = comp.get("marketing_phrases", [])
        if marketing:
            score -= min(2, 0.5 * len(marketing))
            if len(marketing) >= 3 and not any("营销" in rf for rf in red_flags):
                red_flags.append(f"大量营销话术({len(marketing)}处)")

        if not comp.get("has_disclaimer"):
            score -= 0.5

        dim.score = max(0, score)
        dim.details = {
            "guaranteed_return_phrases": guaranteed,
            "strong_directive_phrases": strong_directives,
            "marketing_phrases": marketing,
            "has_risk_disclosure": comp.get("has_risk_disclosure"),
            "has_disclaimer": comp.get("has_disclaimer"),
            "red_flags": red_flags,
            "compliance_source": comp.get("_source", "llm"),
        }
        return dim

    def _generate_suggestions(self, result: EvalResult) -> List[str]:
        suggestions = []
        if result.red_flags:
            suggestions.append("⚠ 存在违规项，必须优先修复红线问题")
        if result.dim1_fact:
            errs = result.dim1_fact.details.get("error_indicators", [])
            if errs:
                suggestions.append(f"请核实以下指标数据: {', '.join(errs[:5])}")
            if result.dim1_fact.details.get("accuracy", 1) < 0.8:
                suggestions.append("事实准确率低于80%，建议全面复核数据来源")
        if result.dim2_result:
            missing = result.dim2_result.details.get("missing_fields", [])
            if missing:
                suggestions.append(f"建议补充关键字段: {', '.join(missing)}")
        if result.dim3_analysis and result.dim3_analysis.score < 12:
            suggestions.append("分析框架不够完整，建议补充缺失的分析维度")
        if result.dim4_compliance:
            if not result.dim4_compliance.details.get("has_risk_disclosure"):
                suggestions.append("请添加风险揭示段落")
            if not result.dim4_compliance.details.get("has_disclaimer"):
                suggestions.append("请添加免责声明")
        return suggestions
