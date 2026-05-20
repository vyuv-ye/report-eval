"""
同花顺数据爬取模块。
- 财务数据（finance.html）
- 实时行情（realhead）
- 日K线 + MACD 技术指标
- 分时图数据
"""
import json
import random
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests
import urllib3
from loguru import logger
from retry import retry

from report_eval.config import get_proxy_config

try:
    import execjs
    _HAS_EXECJS = True
except ImportError:
    _HAS_EXECJS = False
    logger.warning("execjs 未安装，MACD/K线计算将不可用（pip install PyExecJS）")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── 字段重命名映射 ──
FIELD_RENAMES = {
    "净利润": "净利润(归母净利润)",
    "净利润同比增长率": "净利润同比增长率(归母净利润同比增长率)",
}

_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
    "Connection": "close",
    "Pragma": "no-cache",
    "Referer": "https://stockpage.10jqka.com.cn/",
    "Sec-Fetch-Dest": "iframe",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-site",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
}

# ── 东财字段映射 ──
DC_A_FIELDS_MAP = {
    "EPSJB": "基本每股收益(元)",
    "EPSKCJB": "扣非每股收益(元)",
    "EPSXS": "稀释每股收益(元)",
    "BPS": "每股净资产(元)",
    "MGZBGJ": "每股公积金(元)",
    "MGWFPLR": "每股未分配利润(元)",
    "MGJYXJJE": "每股经营现金流(元)",
    "TOTALOPERATEREVE": "营业总收入(元)",
    "MLR": "毛利润(元)",
    "PARENTNETPROFIT": "归属净利润(元)",
    "KCFJCXSYJLR": "扣非净利润(元)",
    "TOTALOPERATEREVETZ": "营业总收入同比增长(%)",
    "PARENTNETPROFITTZ": "归属净利润同比增长(%)",
    "KCFJCXSYJLRTZ": "扣非净利润同比增长(%)",
    "YYZSRGDHBZC": "营业总收入滚动环比增长(%)",
    "NETPROFITRPHBZC": "归属净利润滚动环比增长(%)",
    "KFJLRGDHBZC": "扣非净利润滚动环比增长(%)",
    "ROEJQ": "净资产收益率(加权)(%)",
    "ROEKCJQ": "净资产收益率(扣非/加权)(%)",
    "ZZCJLL": "总资产收益率(加权)(%)",
    "XSMLL": "毛利率(%)",
    "XSJLL": "净利率(%)",
    "YSZKYYSR": "预收账款/营业收入",
    "XSJXLYYSR": "销售净现金流/营业收入",
    "JYXJLYYSR": "经营净现金流/营业收入",
    "TAXRATE": "实际税率(%)",
    "LD": "流动比率",
    "SD": "速动比率",
    "XJLLB": "现金流量比率",
    "ZCFZL": "资产负债率(%)",
    "QYCS": "权益系数",
    "CQBL": "产权比率",
    "ZZCZZTS": "总资产周转天数(天)",
    "CHZZTS": "存货周转天数(天)",
    "YSZKZZTS": "应收账款周转天数(天)",
    "TOAZZL": "总资产周转率(次)",
    "CHZZL": "存货周转率(次)",
    "YSZKZZL": "应收账款周转率(次)",
}

DC_HK_FIELDS_MAP = {
    "BASIC_EPS": "基本每股收益(元)",
    "BPS": "每股净资产(元)",
    "PER_NETCASH_OPERATE": "每股经营现金流(元)",
    "PER_OI": "每股营业收入(元)",
    "OPERATE_INCOME": "营业总收入(元)",
    "OPERATE_INCOME_YOY": "营业总收入同比增长(%)",
    "OPERATE_INCOME_QOQ": "营业总收入滚动环比增长(%)",
    "HOLDER_PROFIT": "归母净利润(元)",
    "HOLDER_PROFIT_YOY": "归母净利润同比增长(%)",
    "HOLDER_PROFIT_QOQ": "归母净利润滚动环比增长(%)",
    "TAX_EBT": "所得税/利润总额(%)",
    "OCF_SALES": "经营现金流/营业收入(%)",
    "ROE_AVG": "平均净资产收益率(%)",
    "ROA": "总资产净利率(%)",
    "NET_PROFIT_RATIO": "净利率(%)",
    "CURRENT_RATIO": "流动比率(倍)",
    "DEBT_ASSET_RATIO": "资产负债率(%)",
    "EQUITY_MULTIPLIER": "权益乘数",
    "EQUITY_RATIO": "产权比率",
    "GROSS_PROFIT": "毛利润(元)",
    "GROSS_PROFIT_RATIO": "毛利率(%)",
    "ACCOUNTS_RECE_TDAYS": "应收账款周转率(次)",
    "INVENTORY_TDAYS": "存货周转率(次)",
}

LINE_INDICATORS = {"当前", "开盘", "昨收", "最低", "最高", "成交量", "成交额", "换手率", "涨跌额", "涨跌幅"}


def convert_chinese_number(value) -> Optional[float]:
    """将带中文单位的数字字符串转换为 float"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    value = str(value).strip()
    if value.endswith("%"):
        value = value[:-1]
    units = {
        "万亿": 1e12, "千亿": 1e11, "百亿": 1e10, "十亿": 1e9,
        "亿": 1e8, "千万": 1e7, "百万": 1e6, "十万": 1e5,
        "万": 1e4, "千": 1e3, "百": 1e2, "十": 1e1,
    }
    for unit, multiplier in sorted(units.items(), key=lambda x: -len(x[0])):
        if value.endswith(unit):
            num_part = value[:-len(unit)]
            try:
                return float(num_part) * multiplier
            except ValueError:
                return None
    try:
        return float(value)
    except ValueError:
        return None


def is_trading_hours() -> bool:
    now = datetime.now()
    h, m = now.hour, now.minute
    return (h, m) >= (9, 15) and (h, m) <= (15, 0)


def _get_proxies() -> Optional[dict]:
    return get_proxy_config().get("proxies")


@retry(tries=5, delay=2)
def _send_request(url: str):
    proxies = _get_proxies()
    response = requests.get(url, headers=_HEADERS, proxies=proxies, verify=False, timeout=30)
    response.raise_for_status()
    return response


def _build_report_dict(title, report, col_idx, simple_yoy=None) -> tuple:
    data_dict = {}
    for i, t in enumerate(title):
        key = t if isinstance(t, str) else t[0]
        data_dict[key] = report[i][col_idx]

    data_yoy = {}
    if simple_yoy is not None:
        for i, t in enumerate(title):
            key = (t if isinstance(t, str) else t[0]) + "(同比)"
            data_yoy[key] = simple_yoy[i][col_idx]

    for old_key, new_key in FIELD_RENAMES.items():
        if old_key in data_dict:
            data_dict[new_key] = convert_chinese_number(data_dict.pop(old_key))
    return data_dict, data_yoy


def _handle_income_data(data) -> tuple:
    if not isinstance(data, dict):
        data = json.loads(data)
    title = data["title"]
    report = data["report"]
    simple_yoy = data["simple_yoy"]
    income_data, income_yoy_data = _build_report_dict(title, report, 0, simple_yoy)
    return income_data, income_yoy_data


# ── MACD 计算 ──

_MACD_JS = """
function e(e) {
    var t = 12, n = 26, r = 9, i = t - 1, s = t + 1, o = n - 1, u = n + 1,
        a = r - 1, f = r + 1, l = 0, c, h, p, d, v, m, g = [];
    for (l = 0; l < e.length; l++)
        c = e[l].c,
        l == 0 || !c ? (v = m = c || 0, p = h = v - m, d = 0)
            : (v = (2 * c + i * v) / s, m = (2 * c + o * m) / u,
               h = v - m, p = (2 * h + a * p) / f, d = 2 * (h - p)),
        g.push({ MACD: d, DIFF: h, DEA: p, date: e[l].t });
    return g
}
"""

_KLINE_JS = """
function i(e, t) {
    var n = parseFloat(e.c === e.o && t ? t.c : e.o),
        r = parseFloat(e.c), i = n - r;
    return i < 0 ? i = "ab" : i > 0 ? i = "be" : i === 0 && (i = "eq"), i
}

function a(t) {
    var n = t.volumn.split(","), r = t.price.split(","),
        s = t.priceFactor, o = t.dates.split(","),
        u = t.sortYear, a = o.length, f = {};
    f.totalKlineNum = t.total, f.firstDate = t.start,
    f.issuePrice = t.issuePrice, f.isGetTotalData = !1,
    f.name = t.name, f.sortYear = t.sortYear, f.priceFactor = t.priceFactor;
    if (t.total == 0) return f.dataArray = [], f;
    var l = [], c, h, p = ["i", "o", "a", "c"], d = 0, v = "", m = 0, g = [];
    for (var y = 0; y < u.length; y++) g.push([u[y][0], m]), m += u[y][1];
    g.push(["", Infinity]);
    for (var b = 0; b < a; b++) {
        l[b] = {},
        b === 0 ? (v = g[d][0], l[b].t = v + o[b],
            f.isGetTotalData = f.firstDate == l[b].t ? !0 : !1)
            : b < g[d + 1][1] ? l[b].t = v + o[b]
            : b >= g[d + 1][1] && (++d, v = g[d][0], l[b].t = v + o[b]),
        l[b].n = parseInt(n[b]);
        for (var w = 0; w < 4; w++)
            l[b].i ? l[b][p[w]] = r[4 * b + w] / s + l[b].i
                    : l[b][p[w]] = r[4 * b + w] / s;
        l[b].s = i(l[b], l[b - 1]),
        b === 0 ? l[b].yc = l[b].o : l[b].yc = l[b - 1].c
    }
    return l
}
"""


def _calculate_macd(data: list) -> list:
    if not _HAS_EXECJS:
        return []
    try:
        return execjs.compile(_MACD_JS).call("e", data)
    except Exception as e:
        logger.warning(f"MACD 计算失败: {e}")
        return []


def _get_daily_line_data(ts_code: str, curr_price) -> List[dict]:
    if not _HAS_EXECJS:
        return []
    try:
        if ".HK" in ts_code:
            code_num = "HK" + ts_code[1:-3]
            if ts_code.startswith("8"):
                code_num = "K" + ts_code[:-3]
            url = f"https://d.10jqka.com.cn/v6/line/hk_{code_num}/01/all.js"
        else:
            code_num = ts_code[:-3]
            if ".SZ" in ts_code:
                url = f"https://d.10jqka.com.cn/v6/line/32_{code_num}/01/all.js"
            else:
                url = f"https://d.10jqka.com.cn/v6/line/hs_{code_num}/01/all.js"

        html = _send_request(url).text
        html = html[html.find("{"):html.find("}") + 1]
        data_list = execjs.compile(_KLINE_JS).call("a", json.loads(html))
        data_list.append({
            "t": time.strftime("%Y%m%d", time.localtime()),
            "c": float(curr_price) if curr_price else 0,
        })
        macd_list = _calculate_macd(data_list)
        return macd_list[::-1][:3]
    except Exception as e:
        logger.warning(f"_get_daily_line_data({ts_code}) 失败: {e}")
        return []


def _get_real_line_data(line_time_url: str) -> List[dict]:
    try:
        resp_text = _send_request(line_time_url).text
        start_index = resp_text.find("(")
        end_index = resp_text.rfind(")")
        line_data = json.loads(resp_text[start_index + 1:end_index])
        for k, v in line_data.items():
            date_str = v["dates"][0]
            formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
            real_line_data = []
            for row in v["data"].split(";"):
                h_m_time, price, amount, avg_price, volume = row.split(",")
                w_time_str = f"{formatted_date} {h_m_time[:2]}:{h_m_time[2:]}:00"
                real_line_data.append({
                    "timestr": w_time_str,
                    "price": price,
                    "amount": amount,
                    "volume": volume,
                    "avg_price": avg_price,
                })
            return real_line_data
    except Exception as e:
        logger.warning(f"_get_real_line_data 失败: {e}")
        return []
    return []


# ── 对外接口 ──

def flatten_ths_full(ths_full: dict) -> dict:
    """
    从 ths_full 完整数据中提取扁平 {指标中文名[报告期]: 数值} 字典。
    每期数据的指标名带上报告期后缀，避免多期同名字段冲突。
    """
    flat = {}
    _SKIP_KEYS = {
        "科目\\时间", "时间\\科目", "科目\\时间(同比)", "报表核心指标", "报表全部指标",
        "报表核心指标(同比)", "报表全部指标(同比)", "六、每股收益", "六、每股收益(同比)",
        "报表年结日", "原始货币", "审计意见", "盈利指标", "资本结构", "每股指标",
    }
    reports = ths_full.get("report", [])
    for report in reports:
        period = report.get("科目\\时间") or report.get("时间\\科目") or ""
        for k, v in report.items():
            if v is None or v == '' or v is False:
                continue
            if k in _SKIP_KEYS:
                continue
            val = convert_chinese_number(v)
            key = f"{k}[{period}]" if period else k
            if val is not None:
                flat[key] = val
            elif isinstance(v, str) and v.strip():
                flat[key] = v

    for k, v in ths_full.get("real_line_trend", {}).items():
        if v is None or v == '' or v is False:
            continue
        val = convert_chinese_number(v)
        if val is not None:
            flat[k] = val
        elif isinstance(v, str) and v.strip():
            flat[k] = v
    return flat


def fetch_ths_data(ts_code: str) -> dict:
    """从同花顺爬取财务 + 行情数据，返回扁平字典。"""
    full = fetch_ths_full(ts_code)
    return flatten_ths_full(full)


def fetch_ths_full(ts_code: str) -> dict:
    """
    从同花顺爬取完整数据，返回结构化字典：
    {
      "ts_code": str,
      "report": [dict, dict],       # 最近两期财报
      "real_line_trend": dict,       # 实时行情
      "factor": [dict, ...],        # MACD(最近3条)
      "real_line_data": [dict, ...], # 分时图
    }
    """
    result: Dict[str, Any] = {
        "ts_code": ts_code,
        "report": [],
        "real_line_trend": {},
        "factor": [],
        "real_line_data": [],
    }
    try:
        if ".HK" in ts_code:
            code_num = "HK" + ts_code[1:-3]
            if ts_code.startswith("8"):
                code_num = "K" + ts_code[:-3]
            url = f"https://stockpage.10jqka.com.cn/basicweb/176/{code_num}/finance.html"
            line_url = f"https://d.10jqka.com.cn/v6/realhead/hk_{code_num}/defer/last.js"
            line_time_url = f"https://d.10jqka.com.cn/v6/time/hk_{code_num}/defer/last.js"
        else:
            code_num = ts_code[:-3]
            url = f"https://basic.10jqka.com.cn/{code_num}/finance.html"
            line_url = f"https://d.10jqka.com.cn/v2/realhead/hs_{code_num}/last.js"
            line_time_url = f"https://d.10jqka.com.cn/v6/time/hs_{code_num}/defer/last.js"

        line_response = _send_request(line_url).text
        start_index = line_response.find("(")
        end_index = line_response.rfind(")")
        line_data = json.loads(line_response[start_index + 1:end_index])
        items = line_data.get("items", {})

        curr_price = items.get("10")
        line_item = {
            "当前": curr_price,
            "开盘": items.get("7"),
            "昨收": items.get("6"),
            "最低": items.get("9"),
            "最高": items.get("8"),
            "成交量": items.get("13"),
            "成交额": items.get("19"),
            "换手率": str(items.get("1968584", "")) + "%" if items.get("1968584") else None,
            "涨跌额": items.get("264648"),
            "涨跌幅": str(items.get("199112", "")) + "%" if items.get("199112") else None,
        }
        result["real_line_trend"] = {k: v for k, v in line_item.items() if v is not None}

        response = _send_request(url)
        if ".HK" in ts_code:
            match = re.search(r'id="keyindex">(.*?)</p>', response.text)
        else:
            match = re.search(r'id="main">(.*?)</p>', response.text)

        if match:
            data = json.loads(match.group(1))
            title = data["title"]
            report = data["report"]
            if len(title) == len(report):
                report_dict_0, _ = _build_report_dict(title, report, 0)
                result["report"].append(report_dict_0)
                if len(report[0]) > 1:
                    report_dict_1, _ = _build_report_dict(title, report, 1)
                    result["report"].append(report_dict_1)

                if ".HK" not in ts_code:
                    try:
                        stk_income_url = f"https://basic.10jqka.com.cn/api/stock/finance/{code_num}_benefit.json"
                        income_resp = _send_request(stk_income_url)
                        flash_data = income_resp.json().get("flashData")
                        if flash_data:
                            income_data, income_yoy_data = _handle_income_data(flash_data)
                            result["report"][0].update(income_data)
                            result["report"][0].update(income_yoy_data)
                    except Exception as e:
                        logger.warning(f"fetch income data failed for {ts_code}: {e}")

        factor_data = _get_daily_line_data(ts_code, curr_price)
        result["factor"] = factor_data

        real_line = _get_real_line_data(line_time_url)
        result["real_line_data"] = real_line

    except Exception as e:
        logger.error(f"fetch_ths_full({ts_code}) error: {e}")

    return result
