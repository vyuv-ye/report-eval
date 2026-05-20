"""
研报解析与 LLM 校验模块。
1. 从研报 JSON 提取纯文本与结构化字段
2. 调用 LLM 提取研报中所有可量化指标并与标准数据对比
3. 调用 LLM 提取投资判断相关字段
"""
import json
import re
from typing import Dict, List, Optional

from loguru import logger

from report_eval.llm_client import get_llm_client

# ── LLM Prompts ──

DATA_CHECK_SYSTEM_PROMPT = """你是一个金融数据校验助手。用户会给你一段研报文本和一份标准数据（来自同花顺的真实行情与财务数据）。

你的任务：
1. 从研报文本中找出所有可量化的财务/行情数据指标及其数值。
2. 将研报中的每个指标与标准数据进行对比，判断是否正确。

重要时间说明：
- 标准数据中的财务指标带有报告期标注，如"营业总收入[2026-03-31]"表示该数据属于2026Q1财报期。
- 研报是在特定日期生成的，请根据研报文本上下文判断它引用的是哪一期数据，与对应报告期的标准数据对比。
- 行情数据（当前、开盘、昨收、最高、最低等）是研报生成当天的实时数据，直接对比即可。
- 不要因为"今天"与"研报生成日"不同而误判数据错误。

报告期匹配规则（极其重要）：
- 必须严格按报告期匹配，研报中引用的指标属于哪个报告期，就只能与标准数据中相同报告期的数据对比。
- 如果研报提到"去年同期""同比"等字样，说明引用的是去年同一季度的数据。
- 如果标准数据中没有该报告期的数据，则**必须判定为"unknown"**，绝对不能拿其他报告期的数据来对比判错。

判断规则（允许一定误差）：
- 百分比类指标（如毛利率、涨跌幅等）：绝对差 ≤ 0.5 即为正确
- 其他数值类指标：相对误差 ≤ 1%（即 |研报值-标准值|/|标准值| ≤ 0.01）即为正确
- 如果研报中的指标在标准数据中找不到对应项、对应项为空、或报告期不匹配，**判定为"unknown"**

输出要求：
- 只输出 JSON 数组，每个元素格式：
  {"indicator": "指标名", "report_value": 数值, "standard_value": 数值或null, "result": "correct"/"error"/"unknown"}
- report_value: 研报中的原始数值，统一为纯数字（float）。金额如果原文是万/亿则换算为原始数值，百分比去掉%号
- standard_value: 标准数据中对应指标的值，找不到时为 null
- result: correct=正确，error=错误，unknown=标准数据中无此指标或报告期不匹配
- 不要输出任何解释，只输出 JSON 数组。"""

EXTRACT_FIELDS_SYSTEM_PROMPT = """你是一个金融研报结构化字段提取助手。请从研报文本中提取以下可回溯字段。

提取字段清单：
1. current_price: 当前价/最新价
2. target_price: 目标价
3. stop_loss_price: 止损价
4. support_price: 支撑位
5. resistance_price: 压力位
6. upside_pct: 潜在收益(%)
7. downside_pct: 潜在风险(%)
8. risk_reward_ratio: 风险收益比（如 "2.5:1" 或数字）
9. rating: 投资评级/建议
10. header_rating: Header/标题处的评级
11. pe_ttm: 市盈率(TTM)
12. pe_forward: 预测市盈率
13. pb: 市净率
14. report_date: 报告生成日期
15. data_date: 行情数据日期
16. financial_period: 财报期（如"2025Q3"）
17. scenario_optimistic: 乐观情景
18. scenario_baseline: 基准情景
19. scenario_pessimistic: 悲观情景
20. main_risk_factors: 主要风险因素（列表）

输出要求：
- 只输出 JSON 对象，字段名如上。
- 找不到的字段写 null。
- 情景字段用对象：{"price": 数值, "probability": 百分比数值, "trigger": "触发条件文字", "invalidation": "失效条件文字"}
- 数值统一为 float。
- 不要输出任何解释，只输出 JSON 对象。"""

COMPLIANCE_SYSTEM_PROMPT = """你是一个金融研报合规检查助手。请检查研报文本中的合规风险。

检查项：
1. 确定性收益表述：是否有"必涨""稳赚""无风险""保证"等确定性收益承诺
2. 强指令表述：是否有"必须买入""无条件加仓""梭哈"等强个人化买卖指令
3. 个人化配置：是否有"首次仓位X%"等无适当性前提的仓位建议
4. 营销话术：是否有"猎杀游戏""最后一舞"等营销化表达
5. 风险揭示：是否包含市场风险、模型风险、数据延迟风险的提示
6. 免责声明：是否声明"不构成投资建议"
7. 来源标注：引用券商目标价时是否标注来源和日期
8. 建议与风险收益比一致性：若风险收益比低于1，是否仍给出积极建议

输出要求：
- 只输出 JSON 对象，格式：
{
  "guaranteed_return_phrases": ["原文摘录1", ...],
  "strong_directive_phrases": ["原文摘录1", ...],
  "personal_position_phrases": ["原文摘录1", ...],
  "marketing_phrases": ["原文摘录1", ...],
  "has_risk_disclosure": true/false,
  "has_disclaimer": true/false,
  "missing_source_citations": ["描述1", ...],
  "rating_rrr_inconsistency": "不一致的描述" 或 null,
  "red_flags": ["一票否决项描述1", ...]
}
- 不要输出任何解释，只输出 JSON 对象。"""

ANALYSIS_QUALITY_SYSTEM_PROMPT = """你是一个金融研报分析质量评审助手。请评估研报文本的分析过程专业性与内部一致性。

【评估维度】（满分20分）
1. 分析框架完整性（5分）：是否覆盖基本面、估值、技术面、资金面、市场情绪/催化剂、风险因素
2. 因果链专业性（6分）：数据→逻辑→判断链是否完整
3. 多空观点平衡（3分）：是否同时呈现看多理由和看空风险
4. 内部一致性（6分）：评级、数值、信号、情景推演是否自洽

【输出格式】
只输出 JSON 对象：
{
  "framework_coverage": {"基本面": true/false, "估值": true/false, "技术面": true/false, "资金面": true/false, "情绪催化": true/false, "风险因素": true/false},
  "framework_score": 0到5的数值,
  "causal_chain_score": 0到6的数值,
  "causal_chain_issues": ["问题描述1", ...],
  "balance_score": 0到3的数值,
  "balance_issues": ["问题描述1", ...],
  "consistency_score": 0到6的数值,
  "consistency_issues": ["问题描述1", ...],
  "consistency_details": {
    "rating_consistency": {"score": 0到1.5, "issue": "描述或null"},
    "numeric_consistency": {"score": 0到1.5, "issue": "描述或null", "conflicting_examples": []},
    "signal_consistency": {"score": 0到1.5, "issue": "描述或null", "conflicting_signals": []},
    "scenario_completeness": {"score": 0到1.5, "issue": "描述或null", "scenario_probability_sum": 数值或null}
  },
  "scenario_probability_sum": 数值或null,
  "header_vs_final_rating_match": true/false/null,
  "total_score": 0到20的数值
}"""

INDICATOR_EXTRACT_SYSTEM_PROMPT = """你是一个金融研报指标提取专家。请从研报文本中提取所有可量化的数据指标，并按类别分组。

提取类别：
1. financial（财务指标）：营收、净利润、毛利率、净利率、ROE、资产负债率等
2. valuation（估值指标）：PE-TTM、PE-Forward、PB、PS、PEG、股息率、市值等
3. technical（技术指标）：MACD、DIFF、DEA、KDJ、RSI、成交量、换手率等
4. price（价格指标）：当前价、目标价、止损价、支撑位、压力位、均线等
5. calculation（计算指标）：潜在收益、潜在风险、风险收益比等

输出要求：
- 只输出 JSON 对象，格式如下：
{
  "financial": [{"indicator": "指标名", "value": 数值, "unit": "单位", "period": "财报期", "context": "原文描述"}],
  "valuation": [...],
  "technical": [...],
  "price": [...],
  "calculation": [{"indicator": "指标名", "value": 数值, "unit": "单位", "formula_or_explanation": "计算公式"}]
}
- value 统一为 float 类型
- 不要输出任何解释，只输出 JSON 对象"""

CALCULATION_VERIFY_SYSTEM_PROMPT = """你是一个金融计算验证专家。我会给你：
1. 研报文本片段
2. 爬取的基础数据（真实行情/财务数据）
3. 研报中声明的计算类指标

你的任务：
1. 从研报文本中找出每个计算指标的计算逻辑
2. 用提供的基础数据重新计算
3. 对比研报声明值与复算值，判断是否正确

判断规则：
- 百分比类指标绝对差≤0.5%，价格类相对误差≤1%
- 计算逻辑不明确或无法复算时，标记为 "unclear"

输出要求：
- 只输出 JSON 数组，每个元素格式：
{
  "indicator": "指标名",
  "report_value": 数值,
  "recalculated_value": 数值,
  "formula_used": "使用的计算公式",
  "explanation_from_report": "研报原文中的计算解释",
  "result": "correct"/"error"/"unclear"
}
- 不要输出任何解释，只输出 JSON 数组"""


# ── 指标名称统一映射 ──

INDICATOR_NAME_MAPPING = {
    "PE-TTM": "pe_ttm", "市盈率(TTM)": "pe_ttm", "PE": "pe_ttm",
    "PB": "pb", "市净率": "pb",
    "当前价": "current_price", "最新价": "current_price",
    "目标价": "target_price", "止损价": "stop_loss_price",
    "支撑位": "support_price", "压力位": "resistance_price",
    "潜在收益": "upside_pct", "潜在涨幅": "upside_pct",
    "潜在风险": "downside_pct", "潜在跌幅": "downside_pct",
    "风险收益比": "risk_reward_ratio",
    "毛利率": "gross_profit_margin", "净利率": "net_profit_margin",
    "净资产收益率": "roe", "ROE": "roe",
    "资产负债率": "debt_asset_ratio",
    "每股收益": "eps", "EPS": "eps",
    "股息率": "dividend_yield",
}


# ── 解析工具函数 ──

_NUMBER = r"-?\d+(?:\.\d+)?"


def _strip_html(html_str: str) -> str:
    text = re.sub(r'<script\b[^>]*>.*?</script>', ' ', html_str, flags=re.I | re.S)
    text = re.sub(r'<style\b[^>]*>.*?</style>', ' ', text, flags=re.I | re.S)
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _safe_float(val):
    if val is None:
        return None
    try:
        return float(str(val).replace(',', '').replace('：', ':'))
    except (TypeError, ValueError):
        return None


def _pct_to_number(val):
    if val is None:
        return None
    try:
        return abs(float(str(val).replace('%', '').replace('+', '').replace('：', ':')))
    except (TypeError, ValueError):
        return None


def _first_match(patterns: list, text: str) -> Optional[str]:
    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            return m.group(1)
    return None


def _last_match(patterns: list, text: str):
    matches = []
    for pat in patterns:
        matches.extend(re.findall(pat, text, flags=re.I))
    return matches[-1] if matches else None


def _parse_report_json_to_text(json_str: str) -> str:
    """将研报 JSON 拼接为纯文本。"""
    try:
        data = json.loads(json_str)
        parts = []
        for card in data.get('cards', []):
            title = card.get('title', '')
            html = card.get('html', '')
            if title:
                parts.append(title)
            if html:
                text = re.sub(r'<[^>]+>', ' ', html)
                text = re.sub(r'\s+', ' ', text).strip()
                parts.append(text)
        return '\n'.join(parts)
    except Exception as e:
        logger.error(f'_parse_report_json_to_text error: {e}')
        return json_str


def parse_html_to_text(html_str: str) -> str:
    """将研报 HTML 文件内容转为纯文本。"""
    return _strip_html(html_str)


def extract_meta_from_html(html_str: str, filename: str = '') -> dict:
    """从 HTML 文件中提取 meta 信息（asset_code, asset_name, report_date）。"""
    meta = {}

    # 从文件名提取 asset_code，如 600519.SH.html -> 600519.SH
    if filename:
        basename = re.sub(r'\.html?$', '', filename, flags=re.I)
        if re.match(r'^\d{6}\.[A-Z]{2}$', basename):
            meta['asset_code'] = basename

    # 从 <title> 提取 asset_name
    m = re.search(r'<title>\s*(.+?)(?:深度研报|研报|报告)?\s*</title>', html_str, re.I)
    if m:
        meta['asset_name'] = m.group(1).strip()

    # 尝试从内容中提取 report_date
    text = _strip_html(html_str[:5000])
    date_match = re.search(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})', text)
    if date_match:
        meta['report_date'] = date_match.group(1).replace('/', '-')

    return meta


def extract_fields_from_html(html_str: str, meta: dict = None) -> dict:
    """从研报 HTML 直接用正则提取结构化字段（对应 extract_fields_from_report_json）。"""
    full_text = _strip_html(html_str)
    meta = meta or {}

    fields = {
        'asset_code': meta.get('asset_code'),
        'asset_name': meta.get('asset_name'),
        'report_date': meta.get('report_date'),
    }

    current_price = _first_match([
        rf'最新价\s*/?\s*涨跌幅\s*({_NUMBER})\s*元?',
        rf'最新价[微下上]?调?至?\s*({_NUMBER})\s*元?',
        rf'股价(?:微涨至|反弹至|已突破原区间上探至)?\s*({_NUMBER})\s*元',
        rf'当前\s*({_NUMBER})',
    ], full_text)
    if current_price:
        fields['current_price'] = _safe_float(current_price)

    target_price = _last_match([
        rf'目标价\s*(?:从{_NUMBER}元(?:下调|上调)?至)?\s*({_NUMBER})\s*元',
        rf'目标价\s*<[^>]+>\s*({_NUMBER})\s*元',
    ], full_text) or _last_match([rf'目标价[^0-9]*?({_NUMBER})\s*元'], full_text)
    if target_price:
        fields['target_price'] = _safe_float(target_price)

    stop_loss = _last_match([
        rf'止损价(?:从{_NUMBER}元收紧至|上移至)?\s*({_NUMBER})\s*元',
        rf'止损\s*({_NUMBER})\s*元',
    ], full_text) or _last_match([rf'止损[价位]?[^0-9]*?({_NUMBER})\s*元'], full_text)
    if stop_loss:
        fields['stop_loss_price'] = _safe_float(stop_loss)

    upside = _first_match([rf'(?:潜在收益|潜在涨幅|上涨空间)[^0-9+-]*?\+?\s*({_NUMBER})\s*%'], full_text)
    if upside:
        fields['upside_pct'] = _pct_to_number(upside)

    downside = _first_match([rf'(?:潜在风险|潜在跌幅|回撤幅度|最大回撤)[^0-9-]*?-?\s*({_NUMBER})\s*%'], full_text)
    if downside:
        fields['downside_pct'] = _pct_to_number(downside)

    risk_reward = _first_match([rf'风险收益比[^0-9]*?({_NUMBER})\s*[：:]\s*1?'], full_text)
    if risk_reward:
        fields['risk_reward_ratio'] = _safe_float(risk_reward)

    header_ratings = re.findall(r'(强烈看涨|积极看涨|中性观望|谨慎看跌|强烈推荐|推荐|买入|减仓|观望)', full_text[:600])
    if header_ratings:
        fields['header_rating'] = header_ratings[0]

    final_ratings = re.findall(r'(强烈看涨|积极看涨|中性观望|谨慎看跌|强烈推荐|推荐|买入|减仓|观望)', full_text)
    if final_ratings:
        fields['final_rating'] = final_ratings[-1]

    m = re.search(rf'PE[-(（]?TTM[)）]?\s*({_NUMBER})', full_text)
    if m:
        fields['pe_ttm'] = _safe_float(m.group(1))

    m = re.search(rf'PB\s*({_NUMBER})\s*倍?', full_text)
    if m:
        fields['pb'] = _safe_float(m.group(1))

    m = re.search(rf'股息率\s*({_NUMBER})\s*%', full_text)
    if m:
        fields['dividend_yield'] = _safe_float(m.group(1))

    return fields


def _call_llm(system_prompt: str, user_query: str, max_tokens: int = 4096) -> Optional[str]:
    client = get_llm_client()
    return client.chat(system_prompt, user_query, max_tokens=max_tokens)


def _parse_json_from_llm(text: str, expect_array: bool = True):
    if not text:
        return [] if expect_array else {}
    pattern = r'\[.*\]' if expect_array else r'\{.*\}'
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        logger.warning(f'LLM 回答中未找到 JSON: {text[:300]}')
        return [] if expect_array else {}
    try:
        return json.loads(m.group())
    except json.JSONDecodeError as e:
        logger.warning(f'JSON 解析失败: {e}')
        return [] if expect_array else {}


# ── 正则提取 ──

def extract_fields_from_report_json(json_str: str) -> dict:
    """从研报 JSON 直接用正则提取结构化字段。"""
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        logger.warning('extract_fields_from_report_json: JSON 解析失败')
        return {}

    meta = data.get('meta', {})
    cards = data.get('cards', [])

    parts = []
    for card in cards:
        title = card.get('title', '')
        html = card.get('html', '')
        if title:
            parts.append(title)
        if html:
            parts.append(_strip_html(html))
    for chart in data.get('echarts', []):
        code = chart.get('init_code', '')
        if code:
            parts.append(code)
    full_text = '\n'.join(parts)

    fields = {
        'asset_code': meta.get('asset_code'),
        'asset_name': meta.get('asset_name'),
        'report_date': meta.get('report_date'),
    }

    header_cards = [c for c in cards if 'Header' in c.get('title', '') or c.get('card_id', '').startswith('card_header')]
    header_text = ' '.join(_strip_html(c.get('html', '')) for c in header_cards)
    final_cards = [c for c in cards if '投资判断' in c.get('title', '')]
    decision_text = ' '.join(_strip_html(c.get('html', '')) for c in final_cards) or full_text

    current_price = _first_match([
        rf'最新价\s*/?\s*涨跌幅\s*({_NUMBER})\s*元?',
        rf'最新价[微下上]?调?至?\s*({_NUMBER})\s*元?',
        rf'股价(?:微涨至|反弹至|已突破原区间上探至)?\s*({_NUMBER})\s*元',
        rf'当前\s*({_NUMBER})',
    ], full_text)
    if current_price:
        fields['current_price'] = _safe_float(current_price)

    target_price = _last_match([
        rf'目标价\s*(?:从{_NUMBER}元(?:下调|上调)?至)?\s*({_NUMBER})\s*元',
        rf'目标价\s*<[^>]+>\s*({_NUMBER})\s*元',
    ], decision_text) or _last_match([rf'目标价[^0-9]*?({_NUMBER})\s*元'], full_text)
    if target_price:
        fields['target_price'] = _safe_float(target_price)

    stop_loss = _last_match([
        rf'止损价(?:从{_NUMBER}元收紧至|上移至)?\s*({_NUMBER})\s*元',
        rf'止损\s*({_NUMBER})\s*元',
    ], decision_text) or _last_match([rf'止损[价位]?[^0-9]*?({_NUMBER})\s*元'], full_text)
    if stop_loss:
        fields['stop_loss_price'] = _safe_float(stop_loss)

    upside = _last_match(
        [rf'(?:潜在收益|上涨空间(?:仅)?)\s*[（(]?\s*([+-]?{_NUMBER})%'], decision_text
    ) or _first_match([rf'(?:潜在收益|潜在涨幅|上涨空间)[^0-9+-]*?\+?\s*({_NUMBER})\s*%'], full_text)
    if upside:
        fields['upside_pct'] = _pct_to_number(upside)

    downside = _last_match(
        [rf'(?:潜在风险|回撤幅度|最大回撤)[^-\d]{{0,20}}([+-]?{_NUMBER})%'], decision_text
    ) or _first_match([rf'(?:潜在风险|潜在跌幅|回撤幅度|最大回撤)[^0-9-]*?-?\s*({_NUMBER})\s*%'], full_text)
    if downside:
        fields['downside_pct'] = _pct_to_number(downside)

    risk_reward = _last_match(
        [rf'风险收益比(?:显著改善至|小幅改善至|骤降至|降至|不佳)?\s*({_NUMBER})\s*[：:]\s*1?'], decision_text
    ) or _first_match([rf'风险收益比[^0-9]*?({_NUMBER})\s*[：:]\s*1?'], full_text)
    if risk_reward:
        fields['risk_reward_ratio'] = _safe_float(risk_reward)

    header_ratings = re.findall(r'(强烈看涨|积极看涨|中性观望|谨慎看跌|强烈推荐|推荐|买入|减仓|观望)', header_text or full_text[:600])
    if header_ratings:
        fields['header_rating'] = header_ratings[0]

    final_ratings = re.findall(r'(强烈看涨|积极看涨|中性观望|谨慎看跌|强烈推荐|推荐|买入|减仓|观望)', decision_text)
    if final_ratings:
        fields['final_rating'] = final_ratings[0]

    m = re.search(rf'PE[-(（]?TTM[)）]?\s*({_NUMBER})', full_text)
    if m:
        fields['pe_ttm'] = _safe_float(m.group(1))

    m = re.search(rf'PB\s*({_NUMBER})\s*倍?', full_text)
    if m:
        fields['pb'] = _safe_float(m.group(1))

    m = re.search(rf'股息率\s*({_NUMBER})\s*%', full_text)
    if m:
        fields['dividend_yield'] = _safe_float(m.group(1))

    return fields


def check_report_by_llm(
    report_text: str,
    standard_data: Dict[str, float],
    report_date: str = None,
) -> List[dict]:
    """调用 LLM 提取研报中所有指标并与标准数据对比。"""
    if not report_text:
        return []

    max_text_len = 12000
    truncated = report_text[:max_text_len] if len(report_text) > max_text_len else report_text
    standard_str = json.dumps(standard_data, ensure_ascii=False, indent=2)

    date_hint = ""
    if report_date:
        date_hint = f"\n注意：该研报生成日期为 {report_date}，标准数据也是该日期采集的。\n"

    user_query = f"""以下是研报文本：
---
{truncated}
---

以下是标准数据（来自同花顺，带有报告期时间标注）：
---
{standard_str}
---
{date_hint}
请从研报文本中提取所有可量化的数据指标，与标准数据对比后判断正确性，按要求输出 JSON 数组。"""

    answer = _call_llm(DATA_CHECK_SYSTEM_PROMPT, user_query)
    raw_list = _parse_json_from_llm(answer, expect_array=True)

    results = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        indicator = item.get('indicator')
        if not indicator:
            continue
        report_val = item.get('report_value')
        standard_val = item.get('standard_value')
        result = item.get('result', 'unknown')
        try:
            report_val = float(report_val) if report_val is not None else None
        except (TypeError, ValueError):
            report_val = None
        try:
            standard_val = float(standard_val) if standard_val is not None else None
        except (TypeError, ValueError):
            standard_val = None
        if result not in ('correct', 'error', 'unknown'):
            result = 'unknown'
        results.append({
            'indicator': indicator,
            'report_value': report_val,
            'standard_value': standard_val,
            'result': result,
        })

    logger.info(
        f'check_report_by_llm: {len(results)} 个指标, '
        f'correct={sum(1 for r in results if r["result"]=="correct")}, '
        f'error={sum(1 for r in results if r["result"]=="error")}, '
        f'unknown={sum(1 for r in results if r["result"]=="unknown")}'
    )
    return results


def extract_structured_fields(report_text: str) -> dict:
    """从研报文本中提取结构化字段。"""
    if not report_text:
        return {}
    truncated = report_text[:12000]
    user_query = f"以下是研报文本：\n---\n{truncated}\n---\n\n请按要求提取所有可回溯字段，输出 JSON 对象。"
    answer = _call_llm(EXTRACT_FIELDS_SYSTEM_PROMPT, user_query)
    fields = _parse_json_from_llm(answer, expect_array=False)

    for num_key in ['current_price', 'target_price', 'stop_loss_price',
                    'support_price', 'resistance_price', 'upside_pct',
                    'downside_pct', 'risk_reward_ratio', 'pe_ttm', 'pe_forward', 'pb']:
        if num_key in fields and fields[num_key] is not None:
            try:
                val = fields[num_key]
                if isinstance(val, str):
                    val = re.sub(r'[:%：]', '', val)
                fields[num_key] = float(val)
            except (TypeError, ValueError):
                pass
    return fields


def check_compliance(report_text: str) -> dict:
    """合规性检查（LLM）。"""
    if not report_text:
        return {}
    truncated = report_text[:15000]
    user_query = f"以下是研报文本：\n---\n{truncated}\n---\n\n请按要求检查合规风险，输出 JSON 对象。"
    answer = _call_llm(COMPLIANCE_SYSTEM_PROMPT, user_query, max_tokens=4096)
    return _parse_json_from_llm(answer, expect_array=False)


def check_analysis_quality(report_text: str) -> dict:
    """分析过程评估（LLM）。"""
    if not report_text:
        return {}
    truncated = report_text[:15000]
    user_query = f"以下是研报文本：\n---\n{truncated}\n---\n\n请按要求评估分析过程的专业性与一致性，输出 JSON 对象。"
    answer = _call_llm(ANALYSIS_QUALITY_SYSTEM_PROMPT, user_query, max_tokens=4096)
    return _parse_json_from_llm(answer, expect_array=False)


def extract_indicators_by_category(report_text: str) -> Dict[str, List[dict]]:
    """LLM 分类提取研报中的各类指标。"""
    if not report_text:
        return {"financial": [], "valuation": [], "technical": [], "price": [], "calculation": []}

    truncated = report_text[:15000]
    user_query = f"以下是研报文本：\n---\n{truncated}\n---\n\n请按要求提取所有指标并分类输出 JSON 对象。"
    answer = _call_llm(INDICATOR_EXTRACT_SYSTEM_PROMPT, user_query, max_tokens=4096)
    result = _parse_json_from_llm(answer, expect_array=False)

    for category in ["financial", "valuation", "technical", "price", "calculation"]:
        if category not in result:
            result[category] = []

    logger.info(
        f'extract_indicators_by_category: '
        f'financial={len(result.get("financial", []))}, '
        f'valuation={len(result.get("valuation", []))}, '
        f'technical={len(result.get("technical", []))}, '
        f'price={len(result.get("price", []))}, '
        f'calculation={len(result.get("calculation", []))}'
    )
    return result


def merge_extraction_results(regex_fields: dict, llm_indicators: Dict[str, List[dict]]) -> Dict[str, float]:
    """合并正则提取与 LLM 提取结果。正则优先。"""
    merged = {}
    for k, v in regex_fields.items():
        if v is not None and isinstance(v, (int, float)):
            merged[k] = float(v)

    for category, items in llm_indicators.items():
        for item in items:
            indicator = item.get("indicator", "")
            value = item.get("value")
            if value is None:
                continue
            normalized_name = INDICATOR_NAME_MAPPING.get(indicator, indicator)
            if normalized_name not in merged:
                try:
                    merged[normalized_name] = float(value)
                except (TypeError, ValueError):
                    pass

    return merged


def verify_calculations_by_llm(
    report_text: str,
    base_data: Dict[str, float],
    calculation_indicators: List[dict],
) -> List[dict]:
    """LLM 计算验证：用基础数据复算计算类指标。"""
    if not calculation_indicators or not base_data:
        return []

    truncated = report_text[:10000]
    base_data_str = json.dumps(base_data, ensure_ascii=False, indent=2)
    calc_str = json.dumps(calculation_indicators, ensure_ascii=False, indent=2)

    user_query = f"""以下是研报文本片段：
---
{truncated}
---

以下是爬取的基础数据：
---
{base_data_str}
---

以下是研报中声明的计算类指标：
---
{calc_str}
---

请根据研报中的计算逻辑，用基础数据复算，并判断是否正确。"""

    answer = _call_llm(CALCULATION_VERIFY_SYSTEM_PROMPT, user_query, max_tokens=4096)
    raw_list = _parse_json_from_llm(answer, expect_array=True)

    results = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        indicator = item.get("indicator")
        if not indicator:
            continue

        report_val = item.get("report_value")
        recalc_val = item.get("recalculated_value")
        result = item.get("result", "unclear")

        try:
            report_val = float(report_val) if report_val is not None else None
        except (TypeError, ValueError):
            report_val = None
        try:
            recalc_val = float(recalc_val) if recalc_val is not None else None
        except (TypeError, ValueError):
            recalc_val = None

        diff = None
        diff_rate = None
        if report_val is not None and recalc_val is not None:
            diff = round(abs(report_val - recalc_val), 6)
            if recalc_val != 0:
                diff_rate = round(diff / abs(recalc_val), 6)

        results.append({
            "indicator": indicator,
            "report_value": report_val,
            "recalculated_value": recalc_val,
            "formula_used": item.get("formula_used"),
            "result": result if result in ("correct", "error", "unclear") else "unclear",
            "diff": diff,
            "diff_rate": diff_rate,
        })

    logger.info(
        f'verify_calculations_by_llm: {len(results)} 个计算指标, '
        f'correct={sum(1 for r in results if r["result"]=="correct")}, '
        f'error={sum(1 for r in results if r["result"]=="error")}, '
        f'unclear={sum(1 for r in results if r["result"]=="unclear")}'
    )
    return results
