# 开发架构指南

首次了解项目可以先阅读 [模块分层与目录说明](module-map.md)，按“模块 → 子职责 → 实际文件”查看全貌。

Alpha-Pulse 使用按职责分包的模块化单体架构。CLI 和 Python 调用共享同一条分析流程；规则决定方向资格和价格区间，Agent 按需补充证据，应用层负责最终复核与归档。

## 目录职责

```text
src/
├── __init__.py                 # 包信息与版本
├── __main__.py                 # python src/__main__.py
├── interfaces/
│   └── cli.py                  # 命令、watch 调度、输出与连接诊断
├── application/
│   ├── service.py              # analyze()：装配组件、分析、复核、归档
│   └── demo.py                 # 离线合成行情
├── domain/
│   ├── models.py               # Candle / Evidence / Snapshot / Assessment / Prediction
│   └── decision.py             # 规则配置模型、方向评估、价格区间
├── agent/
│   ├── harness.py              # 有轮次和工具预算的模型执行循环
│   ├── context.py              # 快照、证据索引、历史压缩
│   ├── contracts.py            # 模型输出 Schema、JSON 编码、AgentError
│   ├── prompts.py              # 共享系统提示词
│   ├── tools.py                # 工具定义、参数校验与调用分发
│   └── providers/
│       ├── claude.py           # Claude SDK 请求与错误映射
│       └── deepseek.py         # DeepSeek Anthropic 兼容接口适配
├── data/
│   ├── ingestion/
│   │   ├── transport.py        # HTTP / WebSocket、代理、超时、重试、并发限制
│   │   └── providers/          # Binance、GDELT、Coin Metrics、CoinGlass 请求适配
│   ├── cleaning/              # K 线、盘口、成交、衍生品及外部数据校验与标准化
│   ├── processing/            # 指标、订单流、流动性、爆仓统计
│   ├── quality/               # 时效性、完整性与来源质量评分
│   ├── assembly/              # 行情与外部证据摘要、Evidence、Snapshot 组装
│   ├── pipeline.py            # MarketData / ExternalData：并发采集、处理、降级与保存
│   └── errors.py              # 数据层共用的脱敏异常
├── storage/
│   └── evidence.py             # 内存索引、JSON 文件存储、原始证据分页
└── config/
    ├── __init__.py             # 对外导出 Settings
    ├── settings.py             # 环境配置校验和默认资源定位
    └── default_decision_tree.json
```

业务包直接位于 `src/` 下，包含各自的 `__init__.py`。跨包导入使用 `application.service`、`domain.models` 等绝对路径。wheel 显式打包这七个业务包，安装后 CLI 通过 `interfaces.cli:main` 启动。

## 依赖边界

- `interfaces` 调用 `application` 执行分析，也可以调用 `data` 完成独立行情查询与订阅。
- `application` 是组件装配和用例编排的位置，可以组合 Agent、数据、领域规则、存储和配置。
- `agent/harness.py` 通过传入模型的 `complete()` 方法调用 LLM，不导入具体模型提供方。模型由应用层选择，调用结束后由应用层关闭。
- `agent/providers` 负责模型协议差异，依赖共享提示词和输出契约，不依赖执行循环或应用编排。DeepSeek 目前保留对 Claude 适配器的继承，复用兼容协议的流式请求与错误处理；此次未修改 SDK 行为。
- `agent/tools.py` 将 `data` 和 `storage` 的能力暴露给模型，校验参数并限制调用预算。
- `data/pipeline.py` 编排采集、清洗、加工和组装，通过 `storage` 保存证据。`MarketData` 提供基础行情与快照，`ExternalData` 提供新闻、爆仓和链上补充证据。
- `data/ingestion` 管理外部请求、分页和协议。Binance K 线分页调用 `cleaning/candles.py` 做规范化、去重和排序，因为分页游标依赖 K 线时间；盘口和成交请求返回原始响应，由流水线清洗、加工。WebSocket 传输负责重连，提供方负责事件过滤和跨重连去重。
- `data/cleaning`、`processing`、`quality`、`assembly` 不发起网络请求，也不依赖具体存储类。清洗保留来源字段和单位，加工计算特征，质量模块检查覆盖及时间，组装层生成摘要和共享模型。
- `storage` 负责内存索引、JSON 保存和分页检索；`add()` 委托 `data/assembly/evidence.py` 创建证据，`ledger()` 委托 `data/quality/scoring.py` 重新评分。组装层不反向依赖存储，避免循环导入。
- `domain` 不导入 Agent、外部数据客户端、应用编排或具体存储类。`DecisionTree.evaluate()` 接收 `Mapping[str, Evidence]`，只读访问证据；规则配置文件的读取仍由 `DecisionTree` 构造函数完成。
- `config` 不依赖业务包。默认规则随 Python 包发布，其路径相对于 `settings.py` 定位，支持通过 `DECISION_TREE_PATH` 覆盖。

这是当前规模下的职责分包，尚未引入通用市场接口、数据库仓储接口或依赖注入框架。交易对、快照和结果契约仍含 Binance 约束。

## 分析链路

1. `interfaces/cli.py` 或 Python 调用进入 `application/service.py:analyze()`。
2. `data/pipeline.py` 通过 `ingestion` 获取基础行情，调用 `cleaning`、`processing`、`quality` 和 `assembly`，记录证据并返回统一快照。
3. `domain/decision.py` 检查必需数据和方向条件；必需数据不合格时直接返回观望。
4. 模型模式下，`agent/harness.py` 组织多轮分析，通过工具补充证据并校验 Assessment。
5. 应用层检查方向与评分，刷新必要行情并重新运行规则，避免发布过期信号。
6. 领域规则计算价格区间，Prediction 校验结果，应用层写入运行归档。

规则模式跳过模型调用，演示模式使用合成行情。`watch` 继续通过容量为 1 的队列合并收盘事件，串行调用同一分析用例。

## 后续功能放在哪里

| 扩展内容 | 代码位置与集成方式 | 对应测试 |
| --- | --- | --- |
| 新增行情或补充数据来源 | 在 `data/ingestion/providers/` 增加请求适配，按职责补充清洗、加工和组装，在 `data/pipeline.py` 接入；需要 AI 主动查询时再注册工具 | `tests/data/` |
| 新增技术指标 | 在 `data/processing/` 实现，通过组装层加入摘要或快照 | `tests/data/` |
| 新增交易规则 | 在 `domain/decision.py` 实现，按需调整默认规则 JSON | `tests/domain/` |
| 新增模型提供方 | 在 `agent/providers/` 实现 `complete(messages, tools, *, final)` 与 `close()`，并在配置和应用装配处接入；当前 Harness 仍要求兼容的消息块与响应属性 | `tests/agent/`、`tests/application/` |
| 新增 Agent 工具 | 在 `agent/tools.py` 增加参数模型、定义和分发；业务数据处理放在数据层 | `tests/agent/` |
| 新增 HTTP API | 在 `interfaces/` 增加入口，复用 `application/service.py:analyze()` | `tests/interfaces/` |
| 新增分析用例或批量任务 | 在 `application/` 增加编排，复用领域与数据组件 | `tests/application/` |
| 更换证据存储 | 在 `storage/` 实现读写能力，在应用层装配；领域规则继续只接收证据映射 | 存储测试与 `tests/application/` |

## 导入与入口

源码采用 `src/` 下直接分包的结构。安装后使用以下导入路径；仓库内测试、脚本和 README 示例使用相同路径。

```python
from application.service import analyze
from config import Settings
from domain.models import Prediction
```

命令入口为 `alpha-pulse`，模块入口为 `python -m interfaces.cli`。在项目根目录也可直接运行 `python src/__main__.py`。

直接调用规则时，将 `tree.evaluate(snapshot, store)` 改为 `tree.evaluate(snapshot, store.records)`。默认规则资源移到 `src/config/default_decision_tree.json`；依赖旧文件路径的外部脚本需要同步更新，使用 `Settings.tree_file()` 的调用不受影响。

### 数据层迁移

数据层按实际功能拆分，并扩展了 Binance 采集窗口、实时状态和特征契约。波动指标继续集中在 `processing/indicators.py`；`processing/derivatives.py` 负责 OI、账户结构、资金费率和爆仓统计。`Snapshot` 新增 `features`、`feature_quality_status`、`feature_metadata`、`data_quality`、交易规则及校准时差等字段；`Evidence` 新增时差元数据。CLI 命令与 Prediction 顶层结构保持兼容，具体字段和计算口径见 [数据契约](data-contract.md)。

旧内部模块已移除，直接使用 Python 内部接口的脚本需更新导入：

| 原入口 | 当前入口 |
| --- | --- |
| `data.market.MarketData` | `data.pipeline.MarketData` |
| `data.providers.external.ExternalData` | `data.pipeline.ExternalData` |
| `data.providers.binance.Binance` | `data.ingestion.providers.binance.Binance` |
| `data.transport.JsonHTTP` / `DataError` | `data.ingestion.transport.JsonHTTP` / `data.errors.DataError` |
| `domain.indicators` | `data.processing.indicators` |
| `domain.quality.assess_quality` | `data.quality.scoring.assess_quality` |
| `Candle.from_rest()` / `Candle.from_ws()` | `data.cleaning.candles.from_rest()` / `from_ws()` |

`Binance.depth()` 返回原始盘口；`Binance.trades()` 返回含 `trades`、窗口边界、连续性和分页截断标记的对象。需要加工后的盘口或订单流时，使用 `MarketData.order_book()` / `order_flow()`。其摘要保留原有规则使用的字段，并增加新特征和覆盖信息；`EvidenceStore.add()` 增加可选时差参数。历史 `INDICATOR_WARMUP_CANDLES=250` 配置需改为至少 `251`。

`ingestion/streaming.py` 管理双路订阅、周期刷新和恢复；`ingestion/state.py` 管理订单簿序号及滚动成交状态；`ingestion/clock.py` 估计服务器时差。`catalog.py` 定义衍生品接口、时间窗口、刷新频率和优先级。`quality/features.py` 判断特征依赖的证据是否有效，`assembly/features.py` 负责计算结果、单位与证据 ID 的绑定。

## 开发与验证

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
uv run alpha-pulse demo --scenario long
uv run python -m interfaces.cli schema
```

`pyproject.toml` 已配置 pytest 的 `src` 搜索路径，直接通过已有虚拟环境运行 `python -m pytest` 也可定位源码。项目移动或 CLI 入口更新后，应重新执行 `uv sync --dev`，更新可编辑安装与命令脚本。

测试按 `agent/`、`application/`、`data/`、`domain/`、`interfaces/` 分组；共享配置在 `tests/conftest.py`，模拟响应构造在 `tests/helpers.py`。离线测试使用模拟网络或合成行情，不代表外部服务实时可用。
