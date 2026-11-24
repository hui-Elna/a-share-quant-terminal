# 05 · 计算引擎

对应代码：`backend/app/engine/`（24 个文件）。

设计原则：

- **纯函数优先**：指标/因子/评分函数只依赖 pandas/numpy，可脱离数据库单测；
- **不联网**（唯一例外：板块资金流本地缺失时的降级，受 `INGEST_NETWORK_ENABLED` 控制）；
- **缺失值诚实**：`NULL` 不补 0；中性分 5.0 必须带原因；
- **窗口只用 T 日及以前**：市场状态/温度/信号统计均有明确防前视约束；
- **事务委托**：`db=None` 自建短事务，传入 `db` 只 flush（见 `_db.py`）。

---

## 1. 数据加工总链路

```
fact_stock_daily
  │
  ├─ indicators.compute_features ──► fact_feature_daily
  │
  ├─ factors（横截面分位）──────────► fact_stock_score_daily（0–10）
  │
  ├─ breadth / market_factors ─────► fact_market_daily
  │        ├─ market_temperature ──► market_temperature_daily
  │        └─ market_regime ───────► market_regime_daily
  │
  ├─ sector_agg / sector_analysis ─► fact_sector_daily / sector_score
  │
  ├─ signal_perf ──────────────────► stg_signal_perf（仅供事后统计）
  │
  └─ paper（复用回测引擎）──────────► paper_nav
```

---

## 2. 技术指标（`indicators.py`）

文件：[`engine/indicators.py`](../../backend/app/engine/indicators.py)

### 2.1 公式口径（写死在代码里）

| 指标 | 口径 | 函数 |
|---|---|---|
| MA | 简单移动平均，窗口等权 | `ma` L34 |
| EMA | `adjust=False, min_periods=1` | `ema` L39 |
| RSI | **Wilder 平滑**（RMA），period=14 | `rsi` L70 |
| MACD | DIF=EMA12−EMA26；DEA=EMA9(DIF)；HIST=DIF−DEA | `macd` L90 |
| BOLL | 20 日中轨 ±2 倍**总体标准差**（ddof=0） | `boll` L103 |
| ATR | Wilder 平滑真实波幅，period=14 | `atr` L126 |
| 动量 | 区间收益率 ×100（%） | `momentum` L132 |
| 波动率 | 日收益标准差 ×√252 ×100（年化 %） | `volatility` L137 |
| 量比 | 近 5 日均量 / 近 20 日均量 | `volume_ratio` L147 |
| KDJ | RSV(9) → K/D 以 50 为种子 1/3 平滑，J=3K−2D | `kdj` L175 |
| 带宽 | `(上轨−下轨)/中轨` | `boll_bandwidth` L194 |
| 金叉 | 前一日 DIF≤DEA 且当日 DIF>DEA | `golden_cross` L223 |
| 多头排列 | MA5>MA10>MA20 | `ma_bullish` L239 |

`_wilder_smooth`（L44）的标准实现：第 period 个值取前 period 个观测的简单平均作为种子，之后递推；直接用 `ewm(alpha=1/period)` 会有系统性偏差。

### 2.2 批量特征

- `compute_features(df)` L265：输入单只股票日线，输出全部特征列；
- `FEATURE_COLUMNS`：与 `fact_feature_daily` 列对应；
- `last_features(features)` L352：取最后一行转 dict（个股详情/板块技术分复用）。

---

## 3. 特征落库（`features.py`）

文件：[`engine/features.py`](../../backend/app/engine/features.py)

| 函数 | 行号 | 说明 |
|---|---:|---|
| `compute_frames` | L68 | 纯函数：按 code 分组整块计算，不逐行 |
| `compute_features_for_range` | L248 | 区间增量写库（ON CONFLICT） |
| `compute_features_for_date` | L310 | 单日增量（pipeline 入口） |

关键常量：

- `INDICATOR_WARMUP_DAYS = 240`：区间起点往前多读的自然日，避免 MA60/momentum_60 为 NaN；
- `DEFAULT_CODE_BATCH = 800`、`MAX_FRAME_ROWS_PER_BATCH = 200_000`：内存保护；
- `WRITE_CHUNK_ROWS = 800`、`_MAX_BIND_PARAMS = 30_000`：asyncpg 绑定参数上限保护。

写入严格限制在 `[start,end]`，预热数据只读不写。

---

## 4. 因子（`factors.py`）与横截面（`cross_section.py`）

### 4.1 六维因子

文件：[`engine/factors.py`](../../backend/app/engine/factors.py)

输出与输入同索引的 **0–1 分位**，NaN 保留：

| 因子 | 函数 | 逻辑 |
|---|---|---|
| 价值 | `value_factor` L90 | 低 PE/PB/PS 更好；≤0 视为最差 |
| 质量 | `quality_factor` L120 | 高 ROE、低负债；当前用 1/PB + 股息率代理（无完整财报数据） |
| 动量 | `momentum_factor` L143 | 20/60 日涨幅越大越好 |
| 波动 | `volatility_factor` L162 | 年化波动越低越好 |
| 流动性 | `liquidity_factor` L177 | 20 日均成交额越大越好 |
| 情绪 | `sentiment_factor` L193 | 新闻情感；无个股级新闻时中性 0.5 |

`_combine`（L65）当某分量整列为空时**跳过并重新归一化权重**，避免“没数据 = 最差”；`composite_factor` L216 输出综合分位。

### 4.2 横截面标准化

文件：[`engine/cross_section.py`](../../backend/app/engine/cross_section.py)

| 函数 | 行号 | 说明 |
|---|---:|---|
| `winsorize` | L29 | 分位缩尾，削极端值 |
| `percentile_rank` | L62 | 0–1 分位，NaN 保留 |
| `industry_neutral_zscore` | L76 | 行业内 z-score（ddof=0）；行业样本 <8 退化为全市场；组内 std=0 返回 0 |

> 当前个股评分默认**不做行业中性化**；行业内中性化是策略可选步骤（见文件头说明）。

---

## 5. 个股评分（`stock_score.py`）

文件：[`engine/stock_score.py`](../../backend/app/engine/stock_score.py)

流程：

1. `compute_factor_percentiles`（L80）：取某交易日横截面，六因子各算 0–1 分位；
2. `score_cross_section`（L118）：分位 ×10 → 0–10，按权重合成；
3. 写 `fact_stock_score_daily`，`detail` 保存原始值 + 分位 + 权重。

权重（`FACTOR_WEIGHTS` L51）：

| 因子 | 权重 |
|---|---:|
| 价值 value | 0.20 |
| 质量 quality | 0.15 |
| 动量 momentum | 0.25 |
| 波动 volatility | 0.15 |
| 流动性 liquidity | 0.15 |
| 情绪 sentiment | 0.10 |

口径：0–10、`round(2)`；横截面剔除停牌股；缺数据因子给中性 0.5 分位（5.0 分）。

入口：`compute_stock_scores`（L270，单日）、`compute_stock_scores_range`（L303，区间，自建事务分批提交）。

---

## 6. 市场因子、温度、状态

### 6.1 市场原始因子（`market_factors.py`）

文件：[`engine/market_factors.py`](../../backend/app/engine/market_factors.py)

把“指数 + 全 A 聚合 + 宏观利率 + 两融”压成每日一行，写 `fact_market_daily`：

- 指数趋势：EMA/ADX/效率系数/ChoPPiness/RSI/MACD/价格相对 MA120；
- 量能：成交额/量能比/换手率/量价匹配/行业热度集中度；
- 广度：涨跌家数/涨停跌停/站上 MA30 比例/60 日新高；
- 估值与避险：股息率/盈利收益率/ERP/股债差/期限利差/防御篮子超额/融资买入占比；
- 波动：RV20/RV60/NATR14/布林带宽/近 60 日最大回撤。

入口：`compute_market_factors_range`（L508）、`compute_and_save_market_factors`（L566）。

常量：`STOCK_LOOKBACK_DAYS=120`、`INDEX_LOOKBACK_DAYS=420`、`MIN_TOTAL_COUNT=2500`、`FULL_COVERAGE_COUNT=3500`。

### 6.2 温度计（`market_temperature.py`）

文件：[`engine/market_temperature.py`](../../backend/app/engine/market_temperature.py)

```
temp = 100 × (
  0.20×换手率分位 + 0.20×成交额分位 + 0.15×涨停家数分位
+ 0.15×融资买入占比分位 + 0.10×60日新高占比分位
+ 0.10×RSI归一 + 0.10×(1−20日波动分位)
)
```

| 函数 | 行号 | 说明 |
|---|---:|---|
| `temperature_band` | L60 | 五档：冰点/清冷/平稳/温热/沸腾 |
| `compute_temperature_frame` | L116 | 7 子项滚动分位与合成 |
| `temperature_rows` | L241 | 行转换 |
| `compute_temperature` | L253 | 读取 `fact_market_daily` 计算 |
| `save_temperature` | L273 | upsert |
| `compute_and_save_temperature` | L284 | pipeline 入口 |
| `weekly_summary` | L307 | 一周均值/极值/斜率/情绪结论 |

缺子项组内重新归一，不补 0；换手率/成交额缺失 → `insufficient`，总分为 null；至少 5 个可用子项。

### 6.3 市场状态机（`market_regime.py`）

文件：[`engine/market_regime.py`](../../backend/app/engine/market_regime.py)（50 KB，最复杂的引擎文件）

输入 `fact_market_daily`，输出：

1. 标准化因子与五组得分 `T/M/B/V_q/Q/R`；
2. 五态判定；
3. 把握度 `confidence` 与乐观度 `optimism`（独立字段）；
4. 防御/进攻/均衡三档配置（合计 100%、5% 整数倍、日变化 ≤15pct）；
5. Top5 因子依据。

标准化：**750 日滚动窗口**、1%/99% 缩尾、百分位映射到 [-1,1]；不足 250 日不输出状态（`data_quality="accumulating"`）。

五态（`REGIME_CODES`）：

| code | 标签 | 含义 |
|---|---|---|
| `trend_up` | 趋势上攻 | 趋势/动量/广度共振向上 |
| `repair_up` | 震荡（向上修复） | 弱修复 |
| `bottoming` | 震荡筑底 | 超跌+波动高+广度低 |
| `pull_down` | 震荡（向下调整） | 趋势/动量转弱 |
| `risk_off` | 避险下跌 | 风险偏好收缩 |

状态机迟滞：`REGIME_CONFIRM_DAYS=3`、`REGIME_MIN_RESIDENCE_DAYS=5`、`REGIME_EXIT_RELAX=0.8`。

| 函数 | 行号 | 说明 |
|---|---:|---|
| `FactorSpec` | L58 | 因子定义（列/类型/方向） |
| `rolling_percentile` | L117 | 滚动分位 |
| `standardize` | L157 | 缩尾 + 百分位映射 |
| `group_scores` | L211 | 五组得分 |
| `classify_state` | L326 | 五态判定 |
| `maintains_state` | L389 | 驻留/退出判断 |
| `state_scores` | L453 | 各状态得分 |
| `confidence_optimism` | L552 | 把握度/乐观度 |
| `build_allocation` | L649 | 三档配置建议 |
| `load_sector_strength` | L754 | 板块强度（配置建议用） |
| `pick_sectors` | L777 | 具体板块选择 |
| `build_evidence` | L906 | Top5 因子依据 |
| `simulate_history` | L1001 | 历史状态回放 |
| `compute_regime_range` | L1186 | 区间计算 |
| `compute_and_save_regime` | L1258 | pipeline 入口 |

---

## 7. 板块分析

### 7.1 技术面（`technical_score.py`）

[`engine/technical_score.py`](../../backend/app/engine/technical_score.py)

- 趋势 40%：`Σ(close−MA_n)/MA_n×100` → clip；
- 动量 30%：RSI14 区间 + MACD 金叉加成；
- 量能 30%：量比映射；
- 历史 <20 日返回中性 5.0，`reason=insufficient_history`。

函数：`score_sector_technical_detail` L108、`score_sector_technical` L187。

### 7.2 情绪面（`sentiment.py`）

[`engine/sentiment.py`](../../backend/app/engine/sentiment.py)

| 子项 | 函数 | 口径 |
|---|---|---|
| 融资 | `calc_margin_sentiment` L70 | 余额变化方向 + 融券占比 + 3 日趋势 |
| 北向 | `calc_northward_sentiment` L133 | ±50 亿 → 2–8 分；无数据中性 |
| 新闻 | `calc_news_sentiment` L162 | `5 + mean(sentiment)×5`；样本<3 向 5 收缩 50% |
| 资金流 | `calc_moneyflow_sentiment` L199 | 主力净流入 ±10 亿 → ±3 分 |

`combine_sentiment` L223：缺子项按权重比例分给可用项（解决“北向恒空导致 25% 权重永久失效”）。

板块情绪权重：融资 0.35 / 北向 0.30 / 资金流 0.35（新闻独立成第三面）。

### 7.3 板块评分（`sector_analysis.py`）

[`engine/sector_analysis.py`](../../backend/app/engine/sector_analysis.py)

```
composite = technical×0.40 + sentiment×0.35 + news×0.25
```

- 全量板块都评分并落库，`top_n` 只影响 LLM 消息面取材；
- 任一子项无数据 → 5.0，`detail.reasons` 说明；
- 历史不足 20 日 → 技术分 5.0，不抛异常；
- 板块资金流优先从 `fact_moneyflow` 按成分聚合，缺失时才联网（受开关控制），且必须用**中文名**匹配。

函数：`score_all_sectors` L561、`save_sector_scores` L730、`compute_and_save_sector_scores` L795。

### 7.4 龙头识别（`leaders.py`）

[`engine/leaders.py`](../../backend/app/engine/leaders.py)

- 实力龙头：先按市值 Top10，再按成交额取前 N；
- 情绪龙头：小盘 + `pct_chg × turnover_rate`，全板块下跌时退化为“最抗跌”并标 `fallback=True`；
- 单位：输入元，输出同时给元和亿元。

函数：`identify_strength_leaders` L108、`identify_sentiment_leaders` L169、`identify_leaders` L269、`sector_members_with_quotes` L288。

### 7.5 板块聚合（`sector_agg.py`）

[`engine/sector_agg.py`](../../backend/app/engine/sector_agg.py)

按**当日 SCD2 名单**把成分股市值/资金流/相对强度聚合回板块：

- `market_cap/float_cap` 合计；
- `main_net_inflow` 合计；
- `rs_20d`：板块 20 日收益 − 沪深300；
- `member_count` / `up_count` / `down_count` / 领涨股；
- 成分数 <3 时市值/资金流置 NULL；`is_partial` 名单不参与聚合；
- 只 UPDATE 当日已有板块行，不凭空插入。

函数：`build_sector_metrics` L144、`compute_sector_metrics` L173、`save_sector_metrics` L189、`compute_and_save_sector_metrics` L264。

---

## 8. 市场广度与指数

### 8.1 `breadth.py`

[`engine/breadth.py`](../../backend/app/engine/breadth.py)

`load_breadth` L41：日报与温度计共用口径；只统计 `close` 非空、`pct_chg` 可解析的个股；涨跌停直接取 `fact_stock_daily` 标志；`median_pct` 为涨跌幅中位数。

### 8.2 `index_perf.py`

[`engine/index_perf.py`](../../backend/app/engine/index_perf.py)

`window_returns` L58：1/5/20/60 日与 YTD 收益；按交易日计数；历史不足返回 `None`（绝不返回 0）；美股按 `as_of` 之前最后一根。

### 8.3 风格轮动（`index_compare.py`）

[`engine/index_compare.py`](../../backend/app/engine/index_compare.py)

```
R = 创业板50 收盘 / 红利低波50 收盘
```

- 阈值优先滚动 20/80 分位（只用截至当日历史）；
- 历史 <250 日退回固定阈值 0.18/0.30；
- 三态：偏成长 / 均衡 / 偏红利；
- 前端不自行推算 R。

`build_index_compare` L135。

### 8.4 牛熊估值（`broad_valuation.py`）

[`engine/broad_valuation.py`](../../backend/app/engine/broad_valuation.py)

固定 6 个宽基指数，四面板：

1. 2005 以来归一化走势（3 年/10 年/全周期）；
2. 回撤 × 盈利收益率四象限（PE 为文件快照）；
3. 1/3/6/12 月动量热力；
4. 21 年回撤史 + ERP 当前值/分位（快照）。

硬约束：不得出现上证50、中证1000；PE 是当前 TTM 绝对值，不说成历史分位；不输出买卖建议。

`build_broad_valuation` L128。

---

## 9. 信号统计与共识

### 9.1 信号前瞻（`signal_perf.py`）

[`engine/signal_perf.py`](../../backend/app/engine/signal_perf.py)

口径：

- T 日收盘信号 → T+1 开盘买入；
- 买入价 = `T+1 open × adj_factor`；
- H 日收益 = 自 T+1 起第 H 个交易日 `close × adj_factor / 买入价 − 1`；
- 超额 = 个股 H 日收益 − 沪深300 同窗口收益；
- 窗口成熟以**本地行情最后一天**为界，未走完 `mature=false`；
- 停牌/一字涨停 `fillable=false`，统计时整体排除。

**仅供事后统计，禁止被策略层 import 为特征**；文件不得出现 `.shift(-`。

函数：`compute_forward_result` L115、`build_ladder` L285、`recompute_signal_perf` L452、`ladder_snapshot` L595。
常量：`HORIZONS=(5,10,20)`、`LADDER_WINDOWS=(20,60,120)`。

### 9.2 共识（`consensus.py`）

[`engine/consensus.py`](../../backend/app/engine/consensus.py)

- 纯读查询，不跑策略、不写库；
- 取目标日全部成功且非回测 run；
- 同一策略多次参数 run 合并，`hit_count=count(DISTINCT algo_code)`；
- 同一策略多次命中只取名次最优（同分取更晚 run）；
- 只统计当前 `SPECS` 启用策略；
- 不返回跨策略平均分（量纲不同）。

`build_consensus` L100、`consensus_signals` L210。

---

## 10. 回测派生视图

[`engine/backtest_view.py`](../../backend/app/engine/backtest_view.py)：`build_backtest_extras` L362

全部从已落库的 `equity_curve` / `trades` 派生，不重新回测：

- `monthly_returns`：月链式，逐月复利等于总收益；
- `drawdown_curve`：`nav / 运行峰值 − 1`；
- `rolling_excess_20d`：20 日滚动超额；
- `industry_exposure`：按调仓日重建持仓，按申万一级汇总，现金单列。

百分比字段为百分数（4.2 表示 4.2%）。

---

## 11. 纸面组合

[`engine/paper.py`](../../backend/app/engine/paper.py)

- 一策略一组合，每天从 `start_date → 最新行情日` 整段重算并覆盖 `paper_nav`；
- 与回测**同一引擎、同一成交规则**；差异只在于起点是策略首个实盘 run 交易日；
- 增量续算：水位 + 60 日重叠窗口，每周全量校准；
- 每日净值行保存收盘持仓与下一交易日 `pending_orders`。

函数：`enabled_algos` L46、`is_enabled` L54、`sync_paper_portfolio` L288、`_sync_paper_portfolio` L301、`sync_all_paper_portfolios` L549、`sync_paper_background` L574、`list_paper_portfolios` L637、`get_paper_detail` L703。

---

## 12. 日报输入（`report_input.py`）

[`engine/report_input.py`](../../backend/app/engine/report_input.py)：`build_daily_report_input` L301

只负责从本地库汇总结构化事实，不调用 LLM、不写 `daily_report`：

```json
{
  "trade_date": "2026-11-14",
  "indices": [...],
  "breadth": {"up": 0, "down": 0, "flat": 0, "limit_up": 0, "limit_down": 0, "median_pct": 0},
  "top_sectors": [...], "bottom_sectors": [...],
  "sector_scores_coverage": {"scored": 0, "total": 0},
  "margin": {...},
  "news_sentiment": {"mean": 0, "count": 0, "top_titles": [], "top_items": []},
  "leaders_by_sector": {...},
  "data_gaps": ["..."]
}
```

`data_gaps` 让 LLM 不能编造不存在的数据。

---

## 13. 数据库事务工具（`_db.py`）

[`engine/_db.py`](../../backend/app/engine/_db.py)

- `session_ctx(db=None)` L31：外部传入复用，否则 `session_scope()` 自建短事务；
- `commit_or_flush`：自建会话 commit，外部会话 flush；
- `to_jsonable`：Decimal/date/NaN → JSON 可序列化。

引擎层**不写 `meta_data_quality`**，新鲜度由 pipeline 统一负责，避免互相覆盖。

---

## 14. 引擎函数索引

| 文件 | 关键函数 | 行号 |
|---|---|---|
| `indicators.py` | `compute_features` / `last_features` | 265 / 352 |
| `features.py` | `compute_features_for_range` / `..._for_date` | 248 / 310 |
| `factors.py` | 六因子 / `composite_factor` | 90–216 |
| `cross_section.py` | `industry_neutral_zscore` | 76 |
| `stock_score.py` | `score_cross_section` / `compute_stock_scores` | 118 / 270 |
| `market_factors.py` | `compute_and_save_market_factors` | 566 |
| `market_temperature.py` | `compute_and_save_temperature` / `weekly_summary` | 284 / 307 |
| `market_regime.py` | `classify_state` / `compute_and_save_regime` | 326 / 1258 |
| `technical_score.py` | `score_sector_technical_detail` | 108 |
| `sector_analysis.py` | `score_all_sectors` / `save_sector_scores` | 561 / 730 |
| `sector_agg.py` | `compute_and_save_sector_metrics` | 264 |
| `leaders.py` | `identify_leaders` | 269 |
| `breadth.py` | `load_breadth` | 41 |
| `signal_perf.py` | `recompute_signal_perf` / `ladder_snapshot` | 452 / 595 |
| `consensus.py` | `build_consensus` | 100 |
| `index_compare.py` | `build_index_compare` | 135 |
| `broad_valuation.py` | `build_broad_valuation` | 128 |
| `backtest_view.py` | `build_backtest_extras` | 362 |
| `paper.py` | `sync_paper_portfolio` / `get_paper_detail` | 288 / 703 |
| `report_input.py` | `build_daily_report_input` | 301 |
