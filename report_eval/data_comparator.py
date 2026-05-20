"""
数值对比模块：公式复算、risk_reward 比对、数据一致性检查。
"""
import re
from typing import Dict, List, Optional

PERCENT_INDICATORS = {
    "毛利率", "净利率", "资产负债率", "净资产收益率", "总资产收益率", "总资产净利率",
    "营业总收入同比增长", "归属净利润同比增长", "归母净利润同比增长", "扣非净利润同比增长",
    "涨跌幅", "换手率",
}

DIFF_RATE_THRESHOLD = 0.01
PERCENT_ABS_THRESHOLD = 0.5

CONSISTENCY_CHECK_LABELS = [
    "最新价", "目标价", "止损价", "风险收益比",
    "5日均线", "MACD", "潜在收益", "潜在风险",
]


def _is_percent_indicator(name: str) -> bool:
    for kw in PERCENT_INDICATORS:
        if kw in name:
            return True
    if name.endswith("(%)") or name.endswith("（%）"):
        return True
    return False


def check_data_consistency(full_text: str) -> List[dict]:
    """检查同一字段在报告中是否出现多个不同的数值。"""
    inconsistencies = []
    number_pattern = r"-?\d+(?:\.\d+)?"

    for label in CONSISTENCY_CHECK_LABELS:
        values = []
        for m in re.finditer(re.escape(label), full_text):
            snippet = full_text[m.start(): m.start() + 80]
            for num in re.findall(number_pattern, snippet):
                try:
                    values.append(float(num))
                except ValueError:
                    pass

        if len(values) > 1:
            rounded = {round(v, 2) for v in values if v > 0}
            if len(rounded) > 2:
                inconsistencies.append({
                    "label": label,
                    "values": values,
                    "rounded_values": sorted(rounded),
                })

    return inconsistencies


def compare_one(indicator_name: str, report_val: float, standard_val: float) -> dict:
    diff = abs(report_val - standard_val)
    if standard_val != 0:
        diff_rate = diff / abs(standard_val)
    else:
        diff_rate = 0.0 if diff == 0 else 1.0

    if _is_percent_indicator(indicator_name):
        is_match = 1 if diff <= PERCENT_ABS_THRESHOLD else 0
    else:
        is_match = 1 if diff_rate <= DIFF_RATE_THRESHOLD else 0

    return {
        "diff": round(diff, 6),
        "diff_rate": round(diff_rate, 6),
        "is_match": is_match,
    }


def verify_calculated_indicators(fields: dict) -> List[dict]:
    """
    验证研报中计算指标的内部一致性：
    - upside_pct  = (target_price - current_price) / current_price * 100
    - downside_pct = (current_price - stop_loss_price) / current_price * 100
    - risk_reward_ratio = upside_pct / downside_pct
    """
    results: List[dict] = []
    current = fields.get('current_price')
    target = fields.get('target_price')
    stop = fields.get('stop_loss_price')
    reported_upside = fields.get('upside_pct')
    reported_downside = fields.get('downside_pct')
    reported_rr = fields.get('risk_reward_ratio')

    if current and target and isinstance(current, (int, float)) and isinstance(target, (int, float)) and current != 0:
        computed_upside = round((target - current) / current * 100, 4)
        if reported_upside is not None:
            diff = abs(computed_upside - reported_upside)
            results.append({
                "indicator_name": "潜在收益(%)(公式复算)",
                "report_value": reported_upside,
                "standard_value": computed_upside,
                "is_match": 1 if diff <= 0.15 else 0,
                "data_source": "formula",
            })

    if current and stop and isinstance(current, (int, float)) and isinstance(stop, (int, float)) and current != 0:
        computed_downside = round((current - stop) / current * 100, 4)
        if reported_downside is not None:
            diff = abs(computed_downside - reported_downside)
            results.append({
                "indicator_name": "潜在风险(%)(公式复算)",
                "report_value": reported_downside,
                "standard_value": computed_downside,
                "is_match": 1 if diff <= 0.15 else 0,
                "data_source": "formula",
            })

    if reported_upside and reported_downside and reported_downside != 0:
        computed_rr = round(reported_upside / reported_downside, 4)
        if reported_rr is not None:
            diff = abs(computed_rr - reported_rr)
            results.append({
                "indicator_name": "风险收益比(公式复算)",
                "report_value": reported_rr,
                "standard_value": computed_rr,
                "is_match": 1 if diff <= 0.05 else 0,
                "data_source": "formula",
            })

    return results


def compare_with_risk_reward(
    fields: dict,
    potential_return,
    potential_risk,
    risk_reward_json: Optional[Dict],
) -> List[dict]:
    """将研报中的计算指标与外部 risk_reward 数据对比。"""
    results: List[dict] = []
    if not risk_reward_json:
        return results

    def _cmp(name, report_val, db_val, threshold=0.01, use_abs_diff=False):
        if report_val is None or db_val is None:
            return
        report_val = float(report_val)
        db_val = float(db_val)
        if use_abs_diff:
            ok = abs(report_val - db_val) <= threshold
        else:
            ok = (abs(report_val - db_val) / abs(db_val) <= threshold) if db_val != 0 else (report_val == 0)
        results.append({
            "indicator_name": name,
            "report_value": round(report_val, 6),
            "standard_value": round(db_val, 6),
            "is_match": 1 if ok else 0,
            "data_source": "risk_reward",
        })

    _cmp("当前价(risk_reward)", fields.get('current_price'), risk_reward_json.get('current_price'))
    _cmp("目标价(risk_reward)", fields.get('target_price'), risk_reward_json.get('target_price'))
    _cmp("止损价(risk_reward)", fields.get('stop_loss_price'), risk_reward_json.get('stop_price'))

    if potential_return is not None and fields.get('upside_pct') is not None:
        _cmp("POTENTIAL_RETURN", fields['upside_pct'] / 100, float(potential_return), threshold=0.002, use_abs_diff=True)

    if potential_risk is not None and fields.get('downside_pct') is not None:
        _cmp("POTENTIAL_RISK", fields['downside_pct'] / 100, float(potential_risk), threshold=0.002, use_abs_diff=True)

    _cmp("风险收益比(risk_reward)", fields.get('risk_reward_ratio'),
         risk_reward_json.get('risk_reward_ratio'), threshold=0.05, use_abs_diff=True)

    return results
