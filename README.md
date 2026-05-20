# report-eval

研报数据核验与评测工具 —— 自动校验研报中的金融数据准确性，并进行 100 分制质量评测。

## 功能

- **数据核验**：自动爬取同花顺标准数据，通过 LLM 对比研报中的指标数值
- **公式复算**：验证目标价、潜在收益、风险收益比等计算指标的内部一致性
- **质量评测**：四维度 100 分制评分体系（事实数据 40 + 结果数据 30 + 分析过程 20 + 合规性 10）
- **合规检查**：自动检测违规表述、强指令、营销话术等合规风险

## 快速开始

### 安装

```bash
git clone https://github.com/vyuv-ye/report-eval.git
cd report-eval
pip install -r requirements.txt
```

### 配置

复制配置模板并填入 LLM API Key：

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml`：

```yaml
llm:
  api_key: "your-api-key"
  base_url: "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
  model: "ep-20250122140342-xfg2r"

proxy:
  enabled: true
  host: "your-proxy:port"
  username: "user"
  password: "pass"
```

也可通过环境变量配置：

```bash
export LLM_API_KEY="your-api-key"
export LLM_BASE_URL="https://ark.cn-beijing.volces.com/api/v3/chat/completions"
export LLM_MODEL="ep-20250122140342-xfg2r"
export PROXY_ENABLED=true
export PROXY_HOST="your-proxy:port"
export PROXY_USERNAME="user"
export PROXY_PASSWORD="pass"
```

### 使用

```bash
# 基本用法：检测本地 examples 目录中的研报 HTML 文件
python -m report_eval examples/600519.SH.html

# 仅数据对比（跳过 LLM 评测，速度更快）
python -m report_eval examples/600519.SH.html --data-only

# 全量对比（含公式复算 + LLM 计算验证）
python -m report_eval examples/600519.SH.html --all-check

# 指定输出目录
python -m report_eval examples/600519.SH.html -o ./my_output

# 记录日志到文件
python -m report_eval examples/600519.SH.html --log-file eval.log
```

HTML 文件命名格式为 `{股票代码}.html`（如 `600519.SH.html`），程序会自动从文件名提取股票代码，从 HTML 内容中提取股票名称。

## 输出格式

结果输出为 JSON 文件，包含：

```json
{
  "ts_code": "600519.SH",
  "asset_name": "贵州茅台",
  "check_time": "2026-05-20 15:30:00",
  "summary": {
    "total": 12,
    "correct": 10,
    "error": 2,
    "data_no_error": false
  },
  "check_results": [...],
  "eval": {
    "total_score": 85.5,
    "grade": "B",
    "has_red_flag": false,
    "dim1_fact": {...},
    "dim2_result": {...},
    "dim3_analysis": {...},
    "dim4_compliance": {...},
    "fix_suggestions": [...]
  }
}
```

## 评测维度

| 维度 | 满分 | 内容 |
|------|------|------|
| 事实数据 | 40 | 准确率(18) + 时间完整性(9) + 口径一致性(8) + 来源可识别(5) |
| 结果数据 | 30 | 参考来源清晰(10) + 公式可复算(5) + 公式完整性(5) + 建议一致性(10) |
| 分析过程 | 20 | 框架覆盖(5) + 因果链(6) + 多空平衡(3) + 内部一致性(6) |
| 合规性   | 10 | 风险揭示(3) + 强指令(2) + 收益承诺(2) + 营销话术(2) + 免责声明(1) |

等级算法：

```
adjusted_score = total_score - critical_count × 15
A: adjusted ≥ 90
B: adjusted ≥ 80
C: adjusted ≥ 70
D: adjusted ≥ 60
F: adjusted < 60
```

## 项目结构

```
report-eval/
├── report_eval/
│   ├── __init__.py          # 包入口
│   ├── __main__.py          # python -m report_eval 入口
│   ├── checker.py           # 主检测流程
│   ├── config.py            # 配置加载
│   ├── llm_client.py        # LLM 客户端
│   ├── http_utils.py        # HTTP 工具
│   ├── ths_fetcher.py       # 同花顺数据爬取
│   ├── report_parser.py     # 研报解析 + LLM 校验
│   ├── data_comparator.py   # 数值对比 + 公式复算
│   ├── evaluator.py         # 评测引擎
│   └── rubric.py            # 评分标准配置
├── examples/                # 示例研报 HTML
├── output/                  # 默认输出目录
├── config.example.yaml      # 配置模板
├── requirements.txt
└── README.md
```

## 依赖说明

- **requests**：HTTP 请求
- **loguru**：日志
- **retry**：请求重试
- **PyYAML**：配置文件解析
- **PyExecJS**：MACD/K 线计算（可选，不安装则跳过技术指标）
- **urllib3**：底层 HTTP

## LLM 兼容性

本项目默认使用字节跳动火山引擎（豆包）API，兼容 OpenAI Chat Completions 格式。你可以配置任何兼容该接口的 LLM 服务：

- 火山引擎（豆包）
- OpenAI
- Azure OpenAI
- 其他兼容 `/v1/chat/completions` 的服务

只需修改 `config.yaml` 中的 `base_url` 和 `model` 即可。

## License

MIT
