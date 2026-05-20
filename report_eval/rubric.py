"""评分标准配置：红线词库、等级算法、评测常量。"""

RED_TERMS = {
    "guaranteed_return": ["必涨", "稳赚", "无风险", "保证收益", "确定上涨", "零风险", "百分百"],
    "strong_instruction": ["必须买入", "无条件加仓", "梭哈", "满仓", "All in", "all in"],
    "personalized_allocation": ["首次仓位", "10-15%", "建议仓位", "仓位控制在"],
    "marketing_language": [
        "猎杀游戏", "最后一舞", "强力逼空", "盛宴继续", "错过就没了",
        "上车", "抄底神器", "火药桶", "王炸", "核爆",
    ],
}

AGGRESSIVE_RATINGS = ["强烈看涨", "积极看涨", "强烈推荐", "推荐", "买入"]
AGGRESSIVE_ADVICE_TERMS = ["加仓", "建仓", "买入", "逢低建仓", "顺势而为"]
CONSERVATIVE_ADVICE_TERMS = ["观望", "等待", "轻仓", "减仓", "止损", "不建议追高"]
RISK_REWARD_THRESHOLDS = {"poor": 0.5, "acceptable": 1.0, "good": 2.0}
REQUIRED_ANALYSIS_META = ["基本面", "估值分析", "技术面", "资金面", "风险分析", "投资判断"]


def compute_grade(score: float, critical_count: int) -> str:
    adjusted = max(0.0, score - critical_count * 15)
    if adjusted >= 90:
        return "A"
    if adjusted >= 80:
        return "B"
    if adjusted >= 70:
        return "C"
    if adjusted >= 60:
        return "D"
    return "F"
