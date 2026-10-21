# Binance 数据与特征契约

数据实现位于 `src/data/`：`ingestion` 采集与维护实时状态，`cleaning` 校验与标准化，`processing` 计算特征，`quality` 判断可用性，`assembly` 组装快照和证据，`pipeline.py` 组织各层。接口窗口和刷新周期集中在 `catalog.py`。

## 运行方式

- `analyze`：一次 REST 快照，获取完整历史窗口并生成特征。CVD 可由连续的 REST 聚合成交窗口计算；盘口变化率、WebSocket 序号和强平窗口在没有实时状态时为 `MISSING`。
- `watch`：保持 `/market`、`/public` 两路 WebSocket 连接，订阅 1m 与分析周期 K 线、聚合成交、深度、最优报价及标记价格。分析期间继续消费行情，收盘事件触发分析；慢分析期间只保留最新待处理收盘事件。
- `stream`：保留原有单 K 线事件输出，不启动完整分析流水线。
- P2 默认关闭；`ENABLE_P2_FEATURES=true` 启用高级别趋势、资金费率规则、OI 动量、拥挤评分与强平事件统计。

缓存属于当前 Binance / LiveMarket 实例。`watch` 每 5 秒检查刷新任务，实际请求遵循各接口缓存期限和统计周期；单次分析按需刷新，不运行后台定时器。P0/P1/P2 表示采集优先级，不自动成为新的交易规则门槛。

## 采集清单

REST 路径以 `https://fapi.binance.com` 为基础；`symbol` 使用完整交易对，例如 `BTCUSDT`。

| 数据 | 接口或流 | 实现窗口与刷新 |
| --- | --- | --- |
| 交易规则 P0 | `/fapi/v1/exchangeInfo` | 保存目标合约整行及全部 filters；启动获取，默认每小时刷新；检查 TRADING、PERPETUAL、tickSize、stepSize、minQty、保证金币种。 |
| 服务器时间 P0 | `/fapi/v1/time` | 默认采样 3 次，使用最低 RTT 样本的请求中点估计偏差；每 5 分钟重校准。 |
| 分析周期 K 线 P0 | `/fapi/v1/klines` | 默认请求 253 根，保留 251 根已收盘数据；收盘增量更新缓存，缺口时 REST 重取。 |
| 最近分钟线 P0 | 同上，`1m` | 默认请求 35 根，保留最多 31 根，至少要求连续 30 根已收盘数据；每分钟更新。 |
| 高级别 K 线 P2 | 同上，`1h` / `4h` | 默认保留 120 根已收盘数据，可配 100～150；只请求高于分析周期的周期，避免重复。 |
| 深度快照 P0 | `/fapi/v1/depth` | 单次模式默认各 100 档，可配 500；实时初始化和异常重建请求 500 档保留储备，对外提供各 100 档。 |
| 聚合成交 P1 | `/fapi/v1/aggTrades` | 默认最近 5 分钟，可配 1～5；每页 1000，首请求使用时间，后续仅用 fromId；记录分页截断和 ID 缺口。 |
| 标记价格 P0 | `/fapi/v1/premiumIndex` | 每次快照强制校验最新完整状态；实时特征可以引用更新且有效的 markPrice 流证据。 |
| 已结算资金费率 P1 | `/fapi/v1/fundingRate` | 最近 30 条；每 5 分钟检查，实时下次结算时间变化时清除缓存。 |
| 资金费率规则 P2 | `/fapi/v1/fundingInfo` | 启动/每小时刷新，只保留目标合约调整记录；空数组是正常状态，不推断默认结算间隔。 |
| 当前 OI P0 | `/fapi/v1/openInterest` | 最新 1 个，默认 30 秒刷新。 |
| 短中期 OI P0 | `/futures/data/openInterestHist` | `5m × 50`，按 5 分钟刷新。 |
| 长期 OI P1 | 同上 | `1h × 26`，按小时刷新。 |
| 全体账户比 P0 | `/futures/data/globalLongShortAccountRatio` | `5m × 50`。 |
| 大户账户比 P0 | `/futures/data/topLongShortAccountRatio` | `5m × 50`，请求头 `X-MBX-APIKEY`。 |
| 大户持仓比 P0 | `/futures/data/topLongShortPositionRatio` | `5m × 50`，请求头 `X-MBX-APIKEY`。 |
| 主动买卖量比 P0 | `/futures/data/takerlongshortRatio` | `5m × 50`。 |
| 实时 K 线 P1 | `@kline_1m`、`@kline_{interval}` | 去除重复周期订阅，按 `x` 区分收盘；只有已收盘 K 线进入指标缓存。 |
| 实时成交 P1 | `@aggTrade` | 滚动 1～5 分钟，REST 启动补齐及 ID 断档恢复。 |
| 实时深度 P1 | `@depth@500ms` | 本地校验 U/u/pu，缺口、交叉或已知储备档位耗尽时失效并重建。 |
| 最优报价 P1 | `@bookTicker` | 最新买卖价量及短期历史；用于最佳报价、中价和价差。 |
| 实时标记价格 P1 | `@markPrice@1s` | 保留最新完整状态，拒绝旧事件覆盖新状态。 |
| 已观测强平 P2 | `@forceOrder` | 按已成交数量 z × 均价 ap 聚合 5m/15m/1h，重复累计更新覆盖而非相加。 |

大户比缺少 API Key 时仅降级相关特征，不将其他公开数据判为失败。密钥只发给两个需要它的接口；不需要 Secret，不调用交易接口。

## 特征字段与计算口径

所有特征位于 `Snapshot.features`，同名 `feature_quality_status` 标记可用性，`feature_metadata` 包含证据 ID、单位、分析截点和必要的计算说明。保留原有 `indicators`、`order_book`、`order_flow` 与 `derivatives`，供现有规则和内部调用使用。

| 分类 | 字段与口径 |
| --- | --- |
| 趋势 | `ema9/21/50`、`ema_alignment`（1/-1/0）、`rsi14`、`macd`、`macd_signal`、`macd_histogram`、`atr14`、`trend_strength_atr` 保留原算法；EMA 使用 SMA 初值，RSI/ATR 使用 Wilder 平滑。 |
| 波动 | `atr_pct = ATR / close × 100`，沿用现有百分数单位；`bollinger_upper/middle/lower = SMA20 ± 2σ`（总体标准差）；`bollinger_bandwidth = 4σ / middle`，兼容字段 `bollinger_width_pct` 再乘 100。 |
| 均价与活跃度 | `rolling_vwap30` 使用最近 30 根分析周期 K 线；`vwap_30m` 使用最近 30 根连续已收盘分钟线。均为 quote_volume / volume。`volume_ratio20`、`trade_count_ratio20` 用最新一根除以前 20 根均值，分母为零则缺失。 |
| 支撑阻力 | `support20/resistance20` 使用判断 K 线之前的 20 根已收盘 K 线，排除当前判断 K 线。 |
| 多周期 P2 | `htf_trend_alignment` 为按周期索引的 direction/aligned；比较 EMA 排列。可用高级别为空时缺失。 |
| 分钟主动成交 | `taker_buy_ratio_1m/30m = buy / total`；`taker_delta_1m/30m = 2 × buy - total`，单位为基础资产。`taker_buy_sell_ratio_5m = buyVol / sellVol`。 |
| 聚合成交 | `cvd_1m/5m` 为指定连续窗口的主动买入基础量减卖出基础量，非历史无限累计值；buyer-maker 表示主动卖出。`trade_intensity_1m` 为聚合事件数 / 60，非原始逐笔成交数。 |
| 量价背离 | `volume_delta_divergence` 比较最新已收盘 1m K 线 open→close 与该同一分钟聚合成交 delta；窗口不完整则缺失。 |
| 报价 | `best_bid/best_ask/mid_price/spread_bps` 优先使用新鲜 bookTicker，单次模式回退 REST 深度；`spread_bps = (ask-bid)/mid × 10000`。 |
| 可见深度 | `bid_depth_usdt/ask_depth_usdt` 各含 `0.1%`、`0.5%` 两个价格带的价格×数量；`orderbook_imbalance` 使用 0.5% 带归一化差值。保留旧字段 `bid_notional/ask_notional/imbalance`。有限档位之外未知，带内金额为已观测可见深度，不代表完整市场深度。 |
| 深度与滑点 | `depth_change_rate` 比较前后两个有效盘口在前一中价固定 ±0.5% 范围的 bid/ask 相对变化；零基数缺失。`estimated_slippage_bps` 含 buy/sell，以指定名义金额 / 当前中价求基础数量，逐档模拟，包含半点差；深度不足返回缺失。 |
| OI | `oi_current` 为合约基础数量；`oi_notional_usdt` 为最新历史 sumOpenInterestValue，不用当前 OI 乘当前价格替代。`oi_change_5m/15m/1h/4h/24h` 为相对变化小数，要求真实时间端点及中间各桶连续。 |
| OI 与价格 | `oi_price_regime` 将同一 15 分钟的价格和 OI 方向组合为 `price_up/down_oi_up/down`，不变时 `flat`；价格取历史 OI 时点之前最近一分钟收盘。P2 `oi_momentum` 为相邻两个 5m 相对变化率之差。 |
| 账户结构 | `global_long_short_ratio`、`top_account_ls_ratio`、`top_position_ls_ratio` 使用对应比值；`global_ls_change_1h` 为绝对比值差。`top_trader_divergence` 在相同时间端点比较两种大户比的 1h 变化方向，相反为 true。 |
| 拥挤 P2 | `crowding_risk_score` 将全体账户比、OI、已结算费率分别相对于各自历史基线计算绝对 z-score，截断至 3σ 后等权映射至 0～100。未校准的启发式评分，不是概率，也不直接触发默认交易规则。 |
| 资金费率 | `funding_rate_current` 是当前公布值；`funding_rate_mean_30` 是最近 30 次已结算均值；`funding_rate_trend = (最新-最早)/29`，按每次结算的斜率，非每小时斜率。费率单位均为小数。 |
| 溢价与结算 | `time_to_next_funding` 为相对于校准时钟的毫秒数，已过时的结算时间缺失；`mark_index_basis_pct = (mark-index)/index × 100`。 |
| 强平 P2 | `liquidation_buy_usdt/sell_usdt/imbalance` 默认别名指向 5m；完整字段附 `_5m/_15m/_1h`。买入方向通常对应空头平仓，卖出对应多头平仓。只有连续订阅覆盖整个窗口才有效。 |

Binance forceOrder 每秒仅推送该时间段的最新事件，无法恢复全量强平。统计始终称为“已观测”，断线后窗口覆盖重新累计；不能把未观察到事件解释为交易所没有强平。盘口属于可见限价委托，也不包含隐藏流动性。

## 时间、质量与降级

`snapshot_as_of` 是完成本轮采集后冻结的校准服务器时间。历史查询限定采集开始时的 endTime，后续组装排除截点之后的数据。它保证证据不晚于截点，不代表不同端点在同一毫秒成交，也不提供历史回放的严格可用时点隔离。

服务器偏差是 `serverTime - (local_send + local_receive)/2`。快照、K 线证据、规则时效判断和输出时间都采用这一时基；`Evidence.fetched_at` 仍保存本地采集时间，并另记 `clock_offset_ms`。最低 RTT 超过 5 秒或校准失败时，不生成有效市场快照。

- `VALID`：依赖证据齐全、合法、未过期且计算窗口满足要求。
- `STALE`：至少一个依赖超过 TTL。
- `MISSING`：缺密钥、数据不足、部分覆盖或计算分母不可用。
- `INVALID`：解析失败、非法数值、未来时间或盘口序号不连续等。

优先级为 INVALID → STALE → MISSING → VALID。非有效的业务特征写为 `null`；质量诊断字段允许保留 false、缺口列表等实际结果。`data_freshness_ms` 按源保存年龄，未知为 null；字典本身有效不表示所有源都可用，应同时读取 `data_quality.sources`。

`kline_completeness` 分别记录主周期/分钟窗口数量、已收盘数量与缺口；`symbol_trading_valid` 记录交易规则资格；`websocket_sequence_valid` 在单次模式为缺失、在断档或盘口过期时为 false。原有必需证据（主周期、分钟线、盘口、成交、标记价格、当前 OI）失败会拒绝方向分析，新增可选来源只影响依赖它的特征。Agent 初始上下文只提供 VALID 特征，不合格的旧摘要替换为质量状态；原始 Evidence 仍归档以便查验。

实时盘口首次增量必须满足 `U <= lastUpdateId <= u`，后续 `pu == 上次 u`；零数量删除档位。断线立即撤销连续性，重建期间不发布有效盘口。连接代次用于丢弃旧队列事件及断线前发出的 REST 补数结果。队列存在待消费消息时，实时成交不声明完整，分析回退 REST。盘口、报价和标记价格分别检查时效。

## 配置与迁移

完整配置见 [`.env.example`](../.env.example)。关键默认值：

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `INDICATOR_WARMUP_CANDLES` | 251 | 最低 251；旧配置中的 250 需更新。 |
| `SNAPSHOT_MINUTES` | 30 | 分钟窗口最低覆盖；增加归档窗口不改变固定的 30m 特征口径。 |
| `TRADE_WINDOW_MINUTES` / `TRADE_MAX_PAGES` | 5 / 100 | 成交窗口及每次补数页数预算；1 分钟配置下 CVD5m 缺失。 |
| `DEPTH_LIMIT` | 100 | REST 单次深度，可改 500；实时初始化自动使用 500。 |
| `SLIPPAGE_NOTIONAL_USDT` | 10000 | 模拟下单名义规模；对非 USDT 报价交易对实际单位为该报价资产。 |
| `REALTIME_STALE_MS` | 5000 | 实时状态最大允许年龄。 |
| `CLOCK_SAMPLES` / `CLOCK_REFRESH_SECONDS` | 3 / 300 | 校时采样数及刷新间隔。 |
| `EXCHANGE_INFO_REFRESH_SECONDS` | 3600 | 交易规则刷新间隔。 |
| `ENABLE_P2_FEATURES` | false | P2 特征和数据源开关。 |
| `HIGHER_TIMEFRAMES` / `HIGHER_TIMEFRAME_CANDLES` | `["1h","4h"]` / 120 | 高级别周期及已收盘样本数。 |

`Snapshot` 与 `Evidence` 为增量扩展，Prediction 顶层契约未改变。`Binance.trades()` 的返回值从裸列表改为含原始成交、窗口和连续性元数据的对象；直接调用它的脚本需要读取 `result["trades"]`。

HTTP 层保留并发限制、有限重试与 Retry-After 处理，尚未实现跨进程请求权重预算。所有金额字段中的 USDT 命名针对默认 BTCUSDT；其他报价资产应按交易规则解释，项目不做汇率转换。

## 验证与参考

离线测试覆盖接口参数、同毫秒成交分页、校时偏差、缓存、缺密钥、空 fundingInfo、窗口缺口、特征数值、盘口序号、断线并发恢复、快照来源及 Agent 上下文预算。使用 MockTransport 和合成 WebSocket 事件，不等同于真实网络或账号权限验收。

接口实现依据 Binance 官方文档：[REST 市场数据](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)、[WebSocket 连接与路由](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect)、[本地订单簿同步](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly)、[市场数据流与强平采样](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market)。
