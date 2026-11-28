# 03 · 数据模型

对应代码：`backend/app/db/`。ORM 类全部在 [`models/__init__.py`](../../backend/app/db/models/__init__.py) 显式导入，`Base.metadata` 才能包含全部表。

> 单位硬约束：`volume`=股、`amount`=元、市值=元、比率=百分数（`%`）、覆盖率=0–1 小数。
> 时区：所有连接显式设置 `Asia/Shanghai`，时间戳列使用 `TIMESTAMPTZ`。

---

## 1. 表清单总览

| 分组 | 表 | 主键 | 作用 |
|---|---|---|---|
| 维度 | `dim_stock` | `code` | 标的：板块/行业/所有制/ST/退市/股本 |
| 维度 | `dim_sector_member` | `(sector_code, stock_code, effective_from)` | 板块成分 SCD2 |
| 维度 | `dim_stock_financial` | `(code, report_date)` | 财报指标（EPS/BPS/ROE/EPS_TTM） |
| 维度 | `dim_stock_dividend` | `(code, ex_date)` | 现金分红（每股 dps） |
| 事实 | `fact_stock_daily` | `(code, trade_date)` | 个股日线（按年分区） |
| 事实 | `fact_sector_daily` | `(sector_code, trade_date)` | 板块日线 + 聚合指标 |
| 事实 | `fact_feature_daily` | `(code, trade_date)` | 预计算技术指标 |
| 事实 | `fact_stock_score_daily` | `(code, trade_date)` | 六维因子 0–10 分 |
| 事实 | `fact_moneyflow` | `(code, trade_date)` | 主力资金流 T0–T4 + 5 日 |
| 事实 | `market_indices` | `(index_code, trade_date)` | 指数日线（A 股/美股/风格/宽基） |
| 事实 | `margin_trading` | `trade_date` | 两融 + 北向 |
| 事实 | `sector_score` | `(trade_date, sector_code)` | 板块三面评分 + 龙头 |
| 事实 | `market_temperature_daily` | `trade_date` | 温度分 + 7 子项 |
| 事实 | `macro_rates` | `trade_date` | 1Y/10Y 国债收益率 |
| 事实 | `fact_market_daily` | `trade_date` | 市场级原始因子 |
| 事实 | `market_regime_daily` | `trade_date` | 五态 + 配置 + 因子依据 |
| 事实 | `daily_report` | `trade_date` | AI 复盘日报 |
| 策略 | `stg_run` | `run_id` | 每次策略运行元信息 |
| 策略 | `stg_signal` | `(run_id, code)` | 信号明细 |
| 策略 | `stg_signal_perf` | `(algo_code, signal_date, code)` | 信号 5/10/20 日前瞻收益 |
| 策略 | `backtest_run` | `bt_id` | 回测结果（JSONB 曲线/成交） |
| 策略 | `backtest_grid` | `grid_id` | 参数矩阵主表 |
| 策略 | `backtest_grid_cell` | `(grid_id, bt_id)` | 矩阵格 → 回测行 |
| 策略 | `paper_portfolio` | `portfolio_id` | 纸面组合 |
| 策略 | `paper_nav` | `(portfolio_id, trade_date)` | 纸面组合每日净值/持仓 |
| 治理 | `trade_calendar` | `cal_date` | 真实交易日历（含休市行） |
| 治理 | `meta_data_quality` | `dataset` | 数据集新鲜度（前端唯一来源） |
| 治理 | `ingest_job` | `job_id` | 采集任务运行记录/检查点 |
| 治理 | `algo_meta` | `algo_code` | 策略元信息台账（`SPECS` 才是事实源） |
| 新闻 | `news_sources` | `id` | 新闻来源 |
| 新闻 | `news_articles` | `id` | 新闻文章（`url_hash` 唯一） |
| 新闻 | `crawl_logs` | `id` | 爬虫运行日志 |
| 推送 | `push_log` | `id` | 推送幂等日志 |

共 33 张业务表 + 2 个视图。

---

## 2. 维度表

### 2.1 `dim_stock` — 标的维度

文件：[`models/dim.py` L19-L56](../../backend/app/db/models/dim.py#L19-L56)

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | `String(6)` PK | 6 位代码 |
| `exchange` | `String(4)` | `SH/SZ/BJ` |
| `board` | `String(8)` | 主板/创业板/科创板/北交所 |
| `name` | `String(64)` | 名称 |
| `industry_l1` / `industry_l2` | `String(64)` | 行业（申万一级/二级口径） |
| `ownership` | `String(16)` | 央企/地方国企/民企/外资/其他/未知（A3 输入） |
| `list_date` / `delist_date` | `Date` | **退市标的必须保留**，避免幸存者偏差 |
| `is_st` | `Boolean` | ST 判定 |
| `status` | `SmallInteger` | 1=正常 2=停牌 9=退市 |
| `total_share` / `float_share` | `Numeric(20,4)` | 总股本/流通股本（股） |

索引：`industry_l1`、`ownership`、`(status,is_st)`。

### 2.2 `dim_sector_member` — 板块成分 SCD2

[L59-L87](../../backend/app/db/models/dim.py#L59-L87)

一行 = 某成分股在某板块的一段有效区间：

- `effective_from <= d AND (effective_to IS NULL OR effective_to > d)` 才是“当日有效成分”；
- `is_partial=true` 表示名单只覆盖同花顺前后各 5 页，**不参与**市值/家数/资金流聚合；
- 回测和板块聚合必须带 SCD2 时间条件，不能直接读“当前名单”。

### 2.3 `dim_stock_financial` — 财报关键指标

[L90-L124](../../backend/app/db/models/dim.py#L90-L124)

- `report_date` 报告期；`announced_date = 报告期 + 90 天`（市场可知日）；
- `eps` / `bps` / `roe` / `payout_ratio` / `eps_ttm` / `revenue`；
- 估值计算用 `announced_date <= 交易日` 的最新报告期，避免未来函数；
- 财报缓存进库后，每日估值计算零联网。

### 2.4 `dim_stock_dividend` — 现金分红

[L127-L154](../../backend/app/db/models/dim.py#L127-L154)

- `ex_date` 为市场可知日（除权日）；
- `dps` = 每股现金分红（元，含税）；巨潮原始“每 10 股派 X 元”入库时除以 10；
- 股息率 = 近 12 个月 `dps` 之和（`ex_date <= d`）/ 收盘价。

---

## 3. 事实表

### 3.1 `fact_stock_daily` — 个股日线（分区表）

[`models/fact.py` L31-L84](../../backend/app/db/models/fact.py#L31-L84)

- PK `(code, trade_date)`；`postgresql_partition_by = RANGE (trade_date)`；
- 价格字段 `open/high/low/close/pre_close`（未复权原始价）；
- `pct_chg` / `amplitude` / `volume`(股) / `amount`(元) / `turnover_rate`(%)；
- **`adj_factor` 回测正确性关键**：后复权收益 = `close × adj_factor`；
- `market_cap` / `float_cap` / `pe_ttm` / `pb` / `ps_ttm` / `dv_ttm`；
- `is_limit_up` / `is_limit_down` / `is_suspended` 是回测“不可成交”的判定依据；
- `src` 记录来源（akshare/tencent/sina/...）。

分区由 [`db/views.py` L34-L73](../../backend/app/db/views.py#L34-L73) 维护：

- 命名 `fact_stock_daily_y2026`，覆盖 `[2026-01-01, 2027-01-01)`；
- 预建 `HISTORY_START.year` 到明年 + `default`；
- 写入前调用 `ensure_partition_for(conn, d)`，避免跨年写入落到 default 后无法迁移。

### 3.2 `fact_sector_daily` — 板块日线

[L87-L138](../../backend/app/db/models/fact.py#L87-L138)

- 行情字段同个股；
- 成分聚合字段：`market_cap` / `float_cap` / `main_net_inflow` / `rs_20d` / `member_count`；
- `up_count` / `down_count` / `leading_stock` / `leading_stock_pct` 由成分重算（同花顺日线不提供）。

### 3.3 `fact_feature_daily` — 预计算技术指标

[L141-L210](../../backend/app/db/models/fact.py#L141-L210)

按列分组：

| 组 | 字段 |
|---|---|
| 均线 | `ma5/10/20/60/120`、`ema5/20/60` |
| 动量 | `rsi14`、`rsi6`、`macd_dif/dea/hist`、`macd_golden_cross`、`roc10`、`momentum_5/20/60` |
| 波动 | `boll_mid/upper/lower`、`boll_width`、`boll_width_pct60`、`atr14`、`volatility_20`、`amp60` |
| 量能 | `volume_ratio`、`amount_ma20`、`vol_ma5` |
| 形态 | `ma_bullish`、`ema20_up`、`ema_golden_cross`、`ema_cross_3d`、`kdj_k/d/j`、`kdj_j_min2`、`kdj_golden_cross` |
| 区间 | `high20_close`、`range20_pct` |

> 只放指标，不放评分；评分依赖横截面，在 `fact_stock_score_daily`。

### 3.4 `fact_stock_score_daily` — 六维因子评分

[L213-L236](../../backend/app/db/models/fact.py#L213-L236)

- `value_score` / `quality_score` / `momentum_score` / `volatility_score` / `liquidity_score` / `sentiment_score` / `composite_score`，均为 **0–10**；
- `detail` JSONB 保存原始因子值、分位、权重，便于归因。

### 3.5 `fact_moneyflow` — 个股资金流

[L239-L262](../../backend/app/db/models/fact.py#L239-L262)

- `main_net_t0`–`t4`、`main_net_5d`（元）；
- `north_hold_ratio`（%），北向自 2024-08 起可能长期为空，属预期内。

### 3.6 `market_indices` — 指数日线

[L265-L290](../../backend/app/db/models/fact.py#L265-L290)

- `index_code`（如 `sh000300`、`sz399673`、`H30269`、`^DJI`）；
- `index_name`、OHLC、`pct_chg`、`volume`、`amount`、`src`。

### 3.7 `margin_trading` — 两融 + 北向

[L293-L310](../../backend/app/db/models/fact.py#L293-L310)

- `margin_balance` / `short_balance` / `margin_change`（亿元）；
- `northward_net`（亿元，可能 NULL）；
- `margin_buy_amount`（沪深融资买入额合计，元）。

### 3.8 `sector_score` — 板块三面评分

[L313-L337](../../backend/app/db/models/fact.py#L313-L337)

- `technical_score` / `sentiment_score` / `news_score` / `composite_score`（0–10）；
- `strength_leaders` / `sentiment_leaders`（JSONB 数组）；
- `detail` 保存子项得分、数据源、缺失原因。

### 3.9 `market_temperature_daily` — 市场温度

[L340-L382](../../backend/app/db/models/fact.py#L340-L382)

- 广度：`up_count/down_count/flat_count/total_count/limit_up_count/limit_down_count/median_pct`；
- 量能：`amount`、`amount_vs_ma20`；
- 趋势：`above_ma20_ratio`、`index_vol_annual`；
- `temp_score`（0–100）、`confidence`（ok/partial/insufficient）、`components` JSONB。

### 3.10 `macro_rates` — 国债收益率

[L385-L398](../../backend/app/db/models/fact.py#L385-L398)

- `cn_1y` / `cn_10y` / `term_spread`（%），用于 ERP、股债收益差。

### 3.11 `fact_market_daily` — 市场原始因子

[L401-L478](../../backend/app/db/models/fact.py#L401-L478)

只存原始值，标准化分位在 `market_regime.py` 按 750 日滚动窗口计算：

| 组 | 字段 |
|---|---|
| 指数趋势 | `close/high/low`、`ret_1d`、`ret_20`、`ema20/60/120`、`ema_slope60`、`ma_alignment`、`adx14`、`di_plus/minus`、`efficiency_ratio`、`choppiness`、`macd_hist`、`rsi14`、`price_vs_ma120` |
| 量能 | `amount`、`amount_ma20`、`amount_vs_ma20`、`market_turnover`、`turnover_ma5`、`volume_price_match`、`industry_heat_concentration`、`capital_efficiency` |
| 广度 | `up/down/flat/total_count`、`limit_up/down_cnt`、`adv_dec_ratio`、`above_ma30_ratio`、`new_high_60_ratio`、`broken_net_ratio` |
| 估值/避险 | `dividend_yield`、`earnings_yield`、`cn_1y/10y`、`erp`、`bond_equity_gap`、`term_spread`、`defensive_rel_momentum`、`margin_buy_amount/ratio` |
| 波动 | `rv20`、`rv60`、`natr14`、`boll_bandwidth`、`max_drawdown_3m` |

`data_quality`：`full` / `partial` / `accumulating`。

### 3.12 `market_regime_daily` — 市场状态

[L481-L516](../../backend/app/db/models/fact.py#L481-L516)

- `regime_code/label/since/days`；
- `confidence_score/level`、`optimism_score/level`；
- 五组得分 `group_t/m/b/v/q/r`；
- `factors`（每个因子的 raw/pct/x/group）、`allocation`（配置建议）、`evidence`（Top5 依据）；
- `completeness`、`data_quality`、`stale_fields`。

### 3.13 `daily_report` — AI 日报

[L519-L534](../../backend/app/db/models/fact.py#L519-L534)

- `content` Markdown、`structured` JSONB（结论/依据/风险）；
- `model`、`is_fallback`（LLM 不可用时为 true）。

---

## 4. 策略与回测表

### 4.1 `stg_run` / `stg_signal`

文件：[`models/strategy.py` L24-L73](../../backend/app/db/models/strategy.py#L24-L73)

`stg_run`：

| 字段 | 说明 |
|---|---|
| `run_id` | UUID |
| `algo_code` / `algo_name` | 策略 |
| `trade_date` | 信号日 |
| `params` JSONB | 归一化后的参数快照 |
| `universe_count` / `signal_count` | 候选池/信号数 |
| `data_version` | 当日四表行数 + 最近写入时间指纹 |
| `signal_schema` | 5（2026-11-28 C1/C3 风控；4=2026-11 因子优化；3=V8 M1） |
| `is_backtest` | 回测产生的 run |
| `status` | running/success/failed/superseded |
| `elapsed_ms` / `error` | |

`stg_signal`：`(run_id, code)` PK，`rank` / `score` / `metrics` JSONB（该策略全部指标依据）。

### 4.2 `stg_signal_perf` — 信号前瞻收益

[L76-L121](../../backend/app/db/models/strategy.py#L76-L121)

- 一行 = 策略 T 日信号，T+1 开盘买入后 5/10/20 日收益；
- `entry_price = T+1 open × adj_factor`；
- `ret_5/10/20`、`excess_5/10/20`、`mature_*`、`fillable`；
- **只允许写入非回测 run**；统计时排除 `fillable=false`。

### 4.3 `backtest_run`

[L124-L145](../../backend/app/db/models/strategy.py#L124-L145)

- `params`（策略 + 回测参数快照）、`metrics`、`equity_curve`（`[{date,nav,benchmark}]`）、`trades`；
- `status`：queued/running/success/failed。

### 4.4 `backtest_grid` / `backtest_grid_cell`

[L148-L191](../../backend/app/db/models/strategy.py#L148-L191)

- 矩阵身份与回测行解耦；
- `axes = {"rebalance_days": [...], "max_positions": [...]}`；
- 关联表记录每格参数与 `reused`，同一回测可被多个矩阵复用。

### 4.5 `paper_portfolio` / `paper_nav`

[L194-L243](../../backend/app/db/models/strategy.py#L194-L243)

- 一策略一组合；`params` 保存策略/回测参数与复用的 `bt_id`；
- `paper_nav` 每日一行：`nav`、`benchmark`、`drawdown`、`holdings`、`cash_ratio`、`pending_orders`、`vs_backtest`。

---

## 5. 治理与状态表

### 5.1 `trade_calendar`

[`models/meta.py` L23-L32](../../backend/app/db/models/meta.py#L23-L32)

`cal_date` PK、`is_open`、`source`。休市日也有行，用于区分“休市”和“没数据”。

### 5.2 `meta_data_quality`

[L35-L53](../../backend/app/db/models/meta.py#L35-L53)

**前端新鲜度徽标的唯一数据源**：

- `last_success_at` / `last_attempt_at` / `last_trade_date`；
- `rows` / `coverage` / `anomalies`；
- `status`：ok / stale / quality_failed / error / unknown；
- `error` / `detail`。

写入语义（`services/freshness.py:write_freshness`）：

| mode | 行为 |
|---|---|
| `success` | 更新成功时间、行数、覆盖率、详情 |
| `attempt` | 质量不合格尝试：更新体检结果但**保留 last_success_at** |
| `failure` | 采集异常：只更新尝试时间/状态/错误，保留成功信息 |

### 5.3 `ingest_job`

[L56-L84](../../backend/app/db/models/meta.py#L56-L84)

`job_id`、`task`、`dataset`、`trade_date`、`params`、`status`、`total/done/failed/rows_written`、`elapsed_ms`、`error`、`checkpoint`（断点续传）。

### 5.4 `algo_meta`

[L87-L100](../../backend/app/db/models/meta.py#L87-L100)

种子表；实际策略清单以来 `strategy/strategies.py` 的 `SPECS` / `CANDIDATE_SPECS` 为准。

### 5.5 `push_log`

[`models/push.py`](../../backend/app/db/models/push.py)

- 唯一约束 `(run_id, channel)`，保证同一 run 只推送一次；
- `status`、`attempts`、`error`、`sent_at`。

---

## 6. 新闻表

[`models/news.py`](../../backend/app/db/models/news.py)

- `news_sources`：名称唯一、base_url、语言、抓取间隔；
- `news_articles`：`url_hash` 唯一去重；`sentiment_score` 为 LLM 标注 -1..1；`is_processed`；
- `crawl_logs`：每次爬虫的 found/new/status/error。

---

## 7. 视图

[`db/views.py` L76-L109](../../backend/app/db/views.py#L76-L109)

| 视图 | 内容 |
|---|---|
| `v_stock_latest` | `dim_stock` + 最新交易日 `fact_stock_daily` |
| `v_stock_features` | 日线 + 技术指标 + 六维评分（`LEFT JOIN`） |

视图用 `CREATE OR REPLACE VIEW` 幂等创建；`drop_all_objects` 会先删视图再删表。

---

## 8. 迁移（Alembic）

目录：`backend/alembic/versions/`，共 19 个版本：

| 版本 | 内容 |
|---|---|
| `20261021_0001_v5_baseline.py` | V5 基线 |
| `20261029_0002_market_temperature.py` | 市场温度 |
| `20261029_0003_sector_metrics.py` | 板块聚合指标 |
| `20261029_0004_sector_member_partial.py` | 成分 `is_partial` |
| `20261030_0005_signal_perf.py` | 信号前瞻 |
| `20261031_0006_legacy_state.py` | 历史状态表占位（revision 保留，建表已移除） |
| `20261101_0007_backtest_grid.py` | 参数矩阵 |
| `20261101_0008_paper_portfolio.py` | 纸面组合 |
| `20261108_0009_backtest_grid_table.py` | 矩阵表重构 |
| `20261108_0010_stg_run_signal_schema.py` | 信号 schema |
| `20261119_0017_signal_schema_v4.py` | 2026-11 因子优化：默认值升到 4 |
| `20261112_0011_market_regime.py` | 市场状态 |
| `20261112_0012_market_factor_ohlc.py` | 市场因子 OHLC |
| `20261115_0013_signal_score_precision.py` | 分数精度 |
| `20261115_0014_push_log.py` | 推送日志 |
| `20261123_0019_drop_legacy_state.py` | 删除历史状态表 `app_watchlist_state` |

约定：

- 全新环境：`bootstrap_db.py` 一键建表 + 分区 + 视图 + 种子；
- 已有环境：`alembic upgrade head` 增量迁移；
- 测试库必须带 `_test` 后缀（`alembic/env.py` 强制校验）。

---

## 9. 种子数据

[`db/seed.py`](../../backend/app/db/seed.py)：`ALGO_SEED` 写入 `algo_meta`，并为 `dim_stock.ownership` 做按需回填。

`fixtures/demo_data.py`：确定性演示数据（股票池/日线/指数/板块/资金流/新闻），用于无网验收；只建议在空库或 `ENV=demo` 下使用。
