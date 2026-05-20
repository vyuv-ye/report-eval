"""
研报数据核验 + 评测主入口。

使用方式：
    # 检测本地 HTML 研报文件
    python -m report_eval examples/600519.SH.html

    # 仅数据对比（跳过评测）
    python -m report_eval examples/600519.SH.html --data-only

    # 全量对比（含公式复算、LLM 计算验证）
    python -m report_eval examples/600519.SH.html --all-check

    # 指定输出目录
    python -m report_eval examples/600519.SH.html -o ./output
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from report_eval.ths_fetcher import fetch_ths_full, flatten_ths_full, is_trading_hours, LINE_INDICATORS
from report_eval.report_parser import (
    extract_fields_from_report_json,
    extract_fields_from_html,
    extract_meta_from_html,
    parse_html_to_text,
    check_report_by_llm,
    extract_structured_fields,
    check_analysis_quality,
    extract_indicators_by_category,
    merge_extraction_results,
    verify_calculations_by_llm,
    _parse_report_json_to_text,
    _strip_html,
)
from report_eval.data_comparator import (
    verify_calculated_indicators,
    check_data_consistency,
)
from report_eval.evaluator import ReportEvaluator, EvalResult, rule_check_compliance, rule_check_analysis_quality


def run_check(
    file_path: str,
    data_only: bool = False,
    all_check: bool = False,
    output_dir: str = None,
) -> dict:
    """
    对本地研报文件（HTML 或 JSON）执行完整的数据核验 + 评测流程。

    Args:
        file_path: 研报文件路径（支持 .html 和 .json）
        data_only: 仅数据对比，跳过 LLM 评测
        all_check: 全量对比（含公式复算、LLM 计算验证）
        output_dir: 结果输出目录

    Returns:
        包含检测和评测结果的字典
    """
    logger.info(f'读取研报: {file_path}')
    with open(file_path, 'r', encoding='utf-8') as f:
        raw_content = f.read()

    is_html = file_path.lower().endswith(('.html', '.htm'))

    if is_html:
        filename = os.path.basename(file_path)
        meta = extract_meta_from_html(raw_content, filename)
        ts_code = meta.get('asset_code', '')
        asset_name = meta.get('asset_name', '')
    else:
        data = json.loads(raw_content)
        meta = data.get('meta', {})
        ts_code = meta.get('asset_code', '')
        asset_name = meta.get('asset_name', '')

    if not ts_code:
        logger.error('无法获取 asset_code（JSON 中缺少 meta.asset_code 或 HTML 文件名不符合格式）')
        return {"error": "缺少 asset_code"}

    logger.info(f'ts_code={ts_code} asset_name={asset_name}')

    # ── Step 1: 爬取标准数据 ──
    logger.info('[Step 1] 爬取同花顺标准数据...')
    ths_full = fetch_ths_full(ts_code)
    ths_data = flatten_ths_full(ths_full)

    trading = is_trading_hours()
    if trading:
        ths_data = {k: v for k, v in ths_data.items() if k not in LINE_INDICATORS}

    merged_standard = dict(ths_data) if ths_data else {}
    logger.info(f'标准数据共 {len(merged_standard)} 个指标')

    # ── Step 2: 正则提取结构化字段 ──
    logger.info('[Step 2] 正则提取结构化字段...')
    if is_html:
        structured_fields = extract_fields_from_html(raw_content, meta)
    else:
        structured_fields = extract_fields_from_report_json(raw_content)
    logger.info(f'正则提取字段: {json.dumps(structured_fields, ensure_ascii=False)}')

    report_date = meta.get('report_date')

    # ── Step 2.5: LLM 分类提取指标 ──
    logger.info('[Step 2.5] LLM 分类提取指标...')
    if is_html:
        report_text = parse_html_to_text(raw_content)
    else:
        report_text = _parse_report_json_to_text(raw_content)
    llm_indicators = extract_indicators_by_category(report_text)

    # ── Step 2.6: 合并提取结果 ──
    merged_fields = merge_extraction_results(structured_fields, llm_indicators)
    logger.info(f'合并后字段: {len(merged_fields)} 个')

    # ── Step 2.7: 数据一致性检查 ──
    if is_html:
        full_text_for_check = report_text
    else:
        data = json.loads(raw_content)
        cards_html = ' '.join(c.get('html', '') for c in data.get('cards', []))
        full_text_for_check = _strip_html(cards_html) if cards_html else report_text
    consistency_issues = check_data_consistency(full_text_for_check)
    if consistency_issues:
        logger.warning(f'数据一致性警告: {len(consistency_issues)} 个字段存在多个数值')

    # ── Step 3: LLM 事实指标对比 ──
    logger.info('[Step 3] LLM 事实指标对比...')
    check_results = []
    if merged_standard:
        check_results = check_report_by_llm(report_text, merged_standard, report_date=report_date) or []

    # ── Step 3.5: LLM 计算验证 ──
    calc_verify_results = []
    if all_check:
        logger.info('[Step 3.5] LLM 计算验证...')
        calculation_indicators = llm_indicators.get("calculation", [])
        if calculation_indicators and merged_standard:
            calc_verify_results = verify_calculations_by_llm(
                report_text, merged_standard, calculation_indicators
            )
            logger.info(f'LLM计算验证: {len(calc_verify_results)} 条')

    # ── Step 4: 公式复算 ──
    calc_results = []
    if all_check:
        logger.info('[Step 4] 公式复算...')
        calc_results = verify_calculated_indicators(structured_fields)
        logger.info(f'公式复算: {len(calc_results)} 条')

    # ── Step 5: 合并结果 ──
    logger.info('[Step 5] 合并结果...')
    all_results = []

    def _fuzzy_get(data_dict, indicator_name):
        if not data_dict or not indicator_name:
            return None
        val = data_dict.get(indicator_name)
        if val is not None:
            return val
        for key, v in data_dict.items():
            if key in indicator_name or indicator_name in key:
                return v
        return None

    for item in check_results:
        result_str = item.get('result', 'unknown')
        if result_str == 'unknown':
            continue
        all_results.append({
            'indicator_name': item.get('indicator', ''),
            'report_value': item.get('report_value'),
            'standard_value': item.get('standard_value'),
            'is_match': 1 if result_str == 'correct' else 0,
            'data_source': 'ths',
            'ths_value': _fuzzy_get(ths_data, item.get('indicator')),
        })

    for item in calc_results:
        all_results.append({
            'indicator_name': item['indicator_name'],
            'report_value': item['report_value'],
            'standard_value': item['standard_value'],
            'is_match': item['is_match'],
            'data_source': item.get('data_source', 'formula'),
            'ths_value': None,
        })

    for item in calc_verify_results:
        result_str = item.get('result', 'unclear')
        if result_str == 'unclear':
            continue
        all_results.append({
            'indicator_name': item.get('indicator', ''),
            'report_value': item.get('report_value'),
            'standard_value': item.get('recalculated_value'),
            'is_match': 1 if result_str == 'correct' else 0,
            'data_source': 'llm_calculation_verify',
            'ths_value': None,
        })

    correct_cnt = sum(1 for r in all_results if r['is_match'] == 1)
    error_cnt = sum(1 for r in all_results if r['is_match'] == 0)

    logger.info(f'\n{"="*60}')
    logger.info(f'检测结果: ts_code={ts_code} ({asset_name})')
    logger.info(f'共 {len(all_results)} 条指标 | 正确={correct_cnt} 错误={error_cnt}')
    logger.info(f'{"="*60}')

    for r in all_results:
        status = '✓' if r['is_match'] == 1 else '✗'
        logger.info(f'  {status} {r["indicator_name"]}: 报告={r["report_value"]} vs 标准={r["standard_value"]} [{r["data_source"]}]')

    # 构建输出
    output = {
        "ts_code": ts_code,
        "asset_name": asset_name,
        "report_date": report_date,
        "check_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "summary": {
            "total": len(all_results),
            "correct": correct_cnt,
            "error": error_cnt,
            "data_no_error": error_cnt == 0,
        },
        "check_results": all_results,
        "consistency_issues": consistency_issues,
    }

    # ── Step 7: 评测（可选）──
    eval_result = None
    if not data_only:
        logger.info('[Step 7] 评测评分...')
        llm_structured = extract_structured_fields(report_text)
        final_fields = {**llm_structured, **structured_fields}

        analysis_quality = {}
        try:
            analysis_quality = check_analysis_quality(report_text) or {}
        except Exception as e:
            logger.warning(f'LLM 分析质量失败: {e}')
        if not analysis_quality:
            analysis_quality = rule_check_analysis_quality(report_text)
        else:
            rule_aq = rule_check_analysis_quality(report_text)
            for key in ('framework_score', 'framework_coverage', 'scenario_probability_sum'):
                if analysis_quality.get(key) is None:
                    analysis_quality[key] = rule_aq.get(key)

        compliance = rule_check_compliance(report_text)

        evaluator = ReportEvaluator()
        eval_result = evaluator.evaluate(
            check_results=[
                {'indicator': r['indicator_name'], 'report_value': r['report_value'],
                 'standard_value': r['standard_value'],
                 'result': 'correct' if r['is_match'] == 1 else 'error'}
                for r in all_results
            ],
            structured_fields=final_fields,
            analysis_quality=analysis_quality,
            compliance=compliance,
            ths_full_data=ths_full,
        )

        output["eval"] = eval_result.to_dict()

        logger.info(
            f'[评测完成] 总分={eval_result.total_score:.1f} 等级={eval_result.grade} '
            f'红线={eval_result.has_red_flag} '
            f'维度=[{eval_result.dim1_fact.score:.1f}/{eval_result.dim2_result.score:.1f}/'
            f'{eval_result.dim3_analysis.score:.1f}/{eval_result.dim4_compliance.score:.1f}]'
        )
    else:
        logger.info('[DATA ONLY] 跳过评测')

    # 输出到文件
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        out_name = f"{ts_code}_{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
        out_path = os.path.join(output_dir, out_name)
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        logger.info(f'结果已写入: {out_path}')

    return output


def main():
    import argparse

    parser = argparse.ArgumentParser(description='研报数据核验与评测工具')
    parser.add_argument('report_file', type=str, help='研报文件路径（支持 .html 和 .json）')
    parser.add_argument('--data-only', action='store_true', default=False,
                        help='仅数据对比，跳过 LLM 评测')
    parser.add_argument('--all-check', action='store_true', default=False,
                        help='全量对比（含公式复算、LLM 计算验证）')
    parser.add_argument('-o', '--output', type=str, default='./output',
                        help='结果输出目录（默认 ./output）')
    parser.add_argument('--log-file', type=str, default=None,
                        help='日志文件路径')

    args = parser.parse_args()

    if args.log_file:
        logger.add(args.log_file, rotation="10 MB")

    if not os.path.isfile(args.report_file):
        logger.error(f'文件不存在: {args.report_file}')
        sys.exit(1)

    run_check(
        file_path=args.report_file,
        data_only=args.data_only,
        all_check=args.all_check,
        output_dir=args.output,
    )


if __name__ == '__main__':
    main()
