# Alpha Pulse Agent

Python 3.11+ 的币安 USD-M 永续合约 K 线分析 Agent。目前交付命令行、Python 调用入口和 JSON 结果，不含前端或交易执行。

## 启动

```bash
uv sync --dev
# 已有 .env 时直接编辑它，保留现有配置。首次使用可从 .env.example 复制。
uv run alpha-pulse doctor
uv run alpha-pulse analyze --symbol BTCUSDT --interval 1m
```

在 `.env` 中配置模型凭据；示例和本机配置使用 DeepSeek：

```dotenv
MODEL_PROVIDER=deepseek
DEEPSEEK_API_KEY=你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_EFFORT=high
HTTPS_PROXY=http://127.0.0.1:13659
HTTP_PROXY=http://127.0.0.1:13659
ALL_PROXY=socks5://127.0.0.1:13659
```

使用 [DeepSeek 官方 Anthropic 兼容接口](https://api-docs.deepseek.com/guides/anthropic_api/)，请求发往 DeepSeek，不需要 Claude 账户。模型可选 `deepseek-v4-pro` 或 `deepseek-v4-flash`，思考强度可选 `low/high/max`。该接口不支持 `output_config.format`，因此通过明确的 JSON Schema 提示、Pydantic 校验和修复轮次保证本地输出契约；模型输出无法通过校验时返回故障型 `no_trade`。

`analyze` 和 `watch` 默认读取 `MODEL_PROVIDER`，也可显式指定 `--engine deepseek`。原有 Claude 接入保留，配置 `ANTHROPIC_API_KEY` 后用 `--engine claude` 选择；Claude 的登录凭据和服务端 fallback 不用于 DeepSeek 请求。`doctor` 只验证 REST/WSS 和配置状态，不调用模型、不验证模型账户权限。模型、数据源密钥请留在 `.env`，不要提交。

代理会显式传入 REST 和 WebSocket 客户端。配置优先级为 `BINANCE_PROXY`（仅币安）→ `HTTPS_PROXY` → `HTTP_PROXY` → `ALL_PROXY`。环境变量优先于 `.env`，代理变量支持大小写；未配置则直连。HTTP CONNECT 和 SOCKS5 均支持，保持 TLS 证书校验。

```bash
# 不联网、不调用模型，合成行情展示 JSON 合约
uv run alpha-pulse demo --scenario long
uv run alpha-pulse demo --scenario short
uv run alpha-pulse demo --scenario ranging

# 使用真实行情，仅运行决策规则；engine=rules，显式区别于模型 Agent
uv run alpha-pulse analyze --symbol ETHUSDT --engine rules --output runs/eth.json

# K 线收盘时触发分析；标准输出是一行一个 JSON，日志写入 stderr
uv run alpha-pulse watch --symbol BTCUSDT --interval 1m

# 仅订阅实时流，收到 3 个事件后退出，不调用模型
uv run alpha-pulse stream --symbol BTCUSDT --interval 1m --count 3

# 查看分析快照，或获取指定时间区间；时间戳统一为 UTC 毫秒
uv run alpha-pulse snapshot --symbol BTCUSDT --interval 5m
uv run alpha-pulse klines --symbol BTCUSDT --interval 15m --start-ms 1788566400000 --end-ms 1788652800000
uv run alpha-pulse schema
```

`watch` 的分析串行执行，分析期间新收盘事件合并为最新一个，避免积压。断线会退避重连，下一次分析重新从 REST 加载完整近期历史。它不会补跑断线期间每根已错过 K 线的预测。`stream` 保留形成中的 K 线及 `closed` 标识，过滤倒序事件和重复收盘事件。

## 数据与工具

每次分析必取：指定周期的 250 根指标预热 K 线、至少近半小时的 1 分钟 K 线、100 档买卖盘、最近 500 条聚合成交、标记价格/资金费率、当前持仓量。还会尝试获取持仓历史、全体账户与大户账户/持仓多空比、主动买卖量比的 5 分钟序列。

EMA9/21/50、RSI14、MACD(12,26,9)、ATR14、布林带(20,2)、滚动 VWAP30、量比和近期支撑阻力由本地根据已收盘 K 线计算；它们不是币安直接返回的技术指标。资金费率、持仓量等字段来自币安公开接口。买卖盘只是可见挂单，主动成交样本覆盖时长随交易活跃度变化。

| Agent 可自主调用的工具 | 行为与限制 |
| --- | --- |
| `get_klines` | 指定币种、周期、毫秒起止区间；最多 5000 根，分页、去重、排序、缺口标记；达到上限返回 `next_start_ms`，不把截断结果当成完整区间 |
| `get_order_book` / `get_order_flow` | 刷新买卖盘和实际主动成交样本 |
| `get_derivatives` | 刷新币安衍生指标；每个子接口独立记录成功或失败 |
| `search_news` | GDELT 检索配置域名白名单内的媒体标题与原文链接；无需密钥，受公共服务可用性/索引延迟影响 |
| `get_onchain` | Coin Metrics 社区接口的每日活跃地址/交易数；默认明确映射 BTC、ETH、LTC、BCH、DOGE，也支持配置自定义 JSON 适配地址 |
| `get_liquidations` | CoinGlass V4 聚合多空爆仓 USD 金额；需要 API key 与允许对应时间粒度的套餐 |
| `assess_evidence` | 根据来源、数据实际时间与完整度重算数据质量标签 |
| `read_evidence` / `save_note` | 分页读取归档原始数据；保留简短工作笔记 |

CoinGlass 默认覆盖 `Binance,OKX,Bybit`，输出明确包含 `exchange_list`，不能称为“所有交易所全量”。没有密钥返回 `unavailable`，不能把币安单所强平推送冒充全网爆仓。新闻只验证来源域名，未读取核验文章正文；`indexed_at` 不等于发布时间。链上日频数据用于背景判断，不能当成分钟级实时资金流。

可选配置：

```dotenv
COINGLASS_API_KEY=
COINGLASS_EXCHANGES=Binance,OKX,Bybit
ONCHAIN_URL_TEMPLATE=
ONCHAIN_API_KEY=
```

自定义链上地址只由操作者配置，模型不能传入任意 URL。支持 `{symbol}`、`{base_asset}` 占位符；可返回 `{"observed_at": 1788652800000, "summary": {"metric": 123}, "data": [...]}`。缺少观测时间会降低可信度，不会用获取时间伪装数据新鲜度。未知币种不自动猜链或代币合约。

## Agent 与上下文

`market.py` 准备必要数据；`tools.py` 注册有类型校验的只读工具；`agent.py` 通过官方 Anthropic SDK 对接 DeepSeek 兼容接口或 Claude，处理流式响应，并控制调用轮数、工具次数、超时、错误返回和最终结构校验。模型可自主选择补充工具，再接受或否决决策树允许的方向。工具调用历史中的思考块按接口要求原样回传，不写入运行轨迹。

上下文分为固定任务/规则、近期行情和指标、证据索引、工作笔记、最近完整工具交换。原始数据保存到 `runs/<run_id>/evidence/`；模型默认收到摘要，需要时按证据 ID 分页回读。超过 `CONTEXT_MAX_BYTES` 时只移除完整的旧工具调用与结果对，保留索引和笔记；这是明确的 UTF-8 字节预算，不是声称精确的 token 计数。全部原始证据仍保留在运行目录。外部正文、标题和笔记均标记为不可信数据，不作为系统指令。

默认模型调用最多 6 轮、工具调用最多 16 次，总分析 180 秒。方向性结果在模型结束后重新获取必要行情；如果规则方向改变或价格偏移超过 0.75 ATR，输出 `no_trade`，避免渲染已经过期的入场区间。凭据、私有思考块不写入轨迹。

每次运行保存：`snapshot.json`、可选的 `final_snapshot.json`、`evidence/*.json`、`trace.jsonl`（模型模式）、`run.json`、`prediction.json`。

## 决策树与输出

尚未收到你的实际预设决策树或本地参考项目路径。当前提供可替换的默认规则，文件为 `src/alpha_pulse/default_decision_tree.json`；用 `DECISION_TREE_PATH` 指定另一份 JSON 即可。启动时校验字段和运算符，不执行动态代码。

默认顺序：数据完整/新鲜 → 点差 → 盘口深度 → 波动率上下限 → 趋势强度 → 资金费率 → EMA/MACD/RSI/主动成交/盘口方向共振 → 模型判断 → 新行情复核 → 价格区间校验。默认阈值是待回测的工程基线，没有承诺预测效果。数据质量失败会立即结束，模型不能越过门槛。

预测 Schema 以 `alpha-pulse schema` 为准，方向性结果形状如下（数字仅为示意）：

```json
{
  "schema_version": "1.0",
  "status": "trade",
  "direction": "long",
  "symbol": "BTCUSDT",
  "interval": "1m",
  "entry_zone": {"low": 70000, "high": 70020},
  "take_profit_zone": {"low": 70400, "high": 70500},
  "stop_loss_zone": {"low": 69840, "high": 69860},
  "valid_from": 1788652800000,
  "valid_until": 1788654600000,
  "validity_seconds": 1800
}
```

完整结果另含 `run_id`、生成/数据时间、市场状态、原因码、证据及可信度、规则路径、风险、参考价和净风险收益比。前端用每个 zone 的 `low/high` 作为纵轴，`valid_from/valid_until` 作为横轴渲染矩形。价格按交易对 tick size 对齐；多头满足止损 < 入场 < 盈利，空头相反。风险收益比按入场区间最不利价格、最远止损和最近盈利端点计算，并计入配置的双边手续费和滑点。

可直接查看完整合成示例：[偏多](examples/prediction.long.json)、[偏空](examples/prediction.short.json)、[无机会](examples/prediction.ranging.json) 和 [JSON Schema](examples/prediction.schema.json)。运行 `uv run python scripts/export_examples.py` 可重新生成。

不合适时机返回 `status=no_trade`、`direction=neutral`、全部价格区间为 `null`。明确震荡用 `market_regime=ranging`；接口故障、缺数据或模型失败用 `unknown` 及对应 `reason_code`，避免把系统故障描述成震荡。`confidence` 是未校准分析评分，不是上涨概率或胜率。合成演示结果的 `engine=demo`，不可用于真实行情。

CLI 正常预测（包括行情无机会）退出码为 0；关键数据缺失、配置/模型/网络错误为 2；用户中断为 130。库入口：

```python
from alpha_pulse.config import Settings
from alpha_pulse.service import analyze

result = await analyze(Settings(), symbol="BTCUSDT", interval="1m")
print(result.model_dump_json())
```

## 验证和设计参考

```bash
uv run pytest
uv run ruff check .
```

接口与设计参考：[币安市场数据](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data)、[币安实时流](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams)、[CoinGlass 聚合爆仓接口](https://docs.coinglass.com/reference/aggregated-liquidation-history)、[Coin Metrics API](https://docs.coinmetrics.io/api/v4/)、[GDELT DOC API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)。

上下文设计借鉴 [Anthropic 的 Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) 的按需加载、压缩和结构化笔记，以及 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的可组合工具/扩展思路。本项目使用专用市场工具与独立循环，没有嵌入 Claude Code 或 DeepSeek Harness，也不依赖它们的内部实现。
