# 开发架构指南

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
│   ├── indicators.py           # 已收盘 K 线的指标计算
│   ├── quality.py              # 来源、新鲜度、完整性的质量评分
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
│   ├── market.py               # 行情汇总、快照适配、覆盖范围检查
│   ├── transport.py            # HTTP、代理、重试和并发控制
│   └── providers/
│       ├── binance.py          # Binance REST / WebSocket 与原始数据解析
│       └── external.py         # 新闻、链上、爆仓数据接入与证据封装
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
- `data` 对接外部接口，将结果组织为 `domain` 中的模型，并通过 `storage` 记录证据。`market.py` 负责快照适配；补充数据的证据封装仍在 `providers/external.py` 中。
- `storage` 依赖 `domain.models` 和 `domain.quality`，负责保存、索引及检索。
- `domain` 不导入 Agent、外部数据客户端、应用编排或具体存储类。`DecisionTree.evaluate()` 接收 `Mapping[str, Evidence]`，只读访问证据；规则配置文件的读取仍由 `DecisionTree` 构造函数完成。
- `config` 不依赖业务包。默认规则随 Python 包发布，其路径相对于 `settings.py` 定位，支持通过 `DECISION_TREE_PATH` 覆盖。

这是当前规模下的职责分包，尚未引入通用市场接口、数据库仓储接口或依赖注入框架。交易对、快照和结果契约仍含 Binance 约束。

## 分析链路

1. `interfaces/cli.py` 或 Python 调用进入 `application/service.py:analyze()`。
2. `data/market.py` 获取基础行情，调用领域指标计算并记录证据。
3. `domain/decision.py` 检查必需数据和方向条件；必需数据不合格时直接返回观望。
4. 模型模式下，`agent/harness.py` 组织多轮分析，通过工具补充证据并校验 Assessment。
5. 应用层检查方向与评分，刷新必要行情并重新运行规则，避免发布过期信号。
6. 领域规则计算价格区间，Prediction 校验结果，应用层写入运行归档。

规则模式跳过模型调用，演示模式使用合成行情。`watch` 继续通过容量为 1 的队列合并收盘事件，串行调用同一分析用例。

## 后续功能放在哪里

| 扩展内容 | 代码位置与集成方式 | 对应测试 |
| --- | --- | --- |
| 新增行情或补充数据来源 | 在 `data/providers/` 增加提供方；在 `data/market.py` 或提供方中适配为现有契约；需要 AI 主动查询时再注册工具 | `tests/data/` |
| 新增技术指标或规则 | 在 `domain/indicators.py`、`domain/decision.py` 实现，按需调整默认规则 JSON | `tests/domain/` |
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
