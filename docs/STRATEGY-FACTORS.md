# 在用策略因子清单（A3 / A4 / C1–C3 / C6）

> 口径版本：2026-11-28（C1/C2/C3/C6 并入正式启用集，C4/C5 已永久移除）
> 事实源：`backend/app/strategy/specs.py`（`SPECS` / `CANDIDATE_SPECS` / `_SORT_SPEC` / `_METRIC_FIELDS`）、
> `backend/app/strategy/conditions.py`（求值条件）、`backend/app/core/config.py`（`STRATEGY_DEFAULTS`）。
> 本文档只描述**当前代码中存在**的策略；A1、A2、B1–B5、C4、C5 的实现已删除，仅保留历史 `stg_run` 记录。
> 优化依据与回测证据见 [FACTOR-OPTIMIZATION-2026-11.md](./FACTOR-OPTIMIZATION-2026-11.md)。

---

## 1. 策略总览

| 标识 | 名称 | 类型 | 状态 | 排序指标（`score`） | 数据门槛 |
|---|---|---|---|---|---|
| `a3` | 央企 × 综合评分TOP | score | 正式启用 | `composite_score` 降序 | 个股综合评分 |
| `a4` | 高股息质量 × 低估值 | value | 正式启用 | `dv_ttm` 降序 | PE/PB/股息率/评分/MA120 |
| `c1` | 海龟突破 | breakout | 正式启用（建议 40 日调仓） | `float_cap` 降序 | 前 20 日最高价 + 中期动量/低波 |
| `c2` | 高窄旗形 | breakout | 正式启用 | `range40_ratio` 降序 | 40 日窗口高低点 |
| `c3` | RPS 相对强度突破 | breakout | 正式启用 | `rps` 降序 | 120 日收益率横截面 + 双均线闸门 |
| `c6` | 布林挤压突破 | breakout | 正式启用 | `boll_width_pct60` 升序 | 布林带宽分位/量能 |

- 正式启用策略进入 `/api/v1/algo/list` 默认清单、每日选股、信号前瞻统计、策略天梯、共识榜、纸面组合与回测。
- C1/C2/C3/C6 已于 2026-11-28 由观察候选转入正式启用集；`CANDIDATE_SPECS` 当前为空，`include_observation=true` 仅为旧客户端兼容。
- 所有策略的 `limit` 默认 20（1–1000），同时参与 run 缓存键。

---

## 2. 全策略统一前置过滤

来源：`strategy/data.py:apply_common_filters`。

| 因子 / 字段 | 规则 | 说明 |
|---|---|---|
| `is_st` | 必须为假 | 剔除 ST / *ST |
| `status` | `== 1` | 剔除停牌、退市整理 |
| `delist_date` | 为空或 `> trade_date` | 剔除已退市 |
| `is_suspended` | 必须为假 | 当日停牌剔除 |
| `close` | `> 0` | 当日无有效行情剔除 |
| `list_date` | 上市交易日 ≥ 60 | 次新股过滤（无日历时按 84 个自然日近似） |

窗口策略（C1–C3）在取历史 K 线时额外剔除停牌/零成交量交易日，并要求满足各自最小窗口长度。

---

## 3. A3 · 央企 × 综合评分TOP

### 3.1 参数

| 参数 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `limit` | 20 | 1–1000 | 返回数量上限 |
| `min_score` | 6.0 | 0–10 | 最低综合评分；兼容旧 0–100 口径传值（60 → 6.0） |

### 3.2 条件因子

| 因子 | 中文名 | 数据源 | 说明 |
|---|---|---|---|
| `ownership` | 所有制 | `dim_stock.ownership` | 必须等于 `央企`（央企名录 + 名称规则，未知不会误判） |
| `composite_score` | 综合评分 | `fact_stock_score_daily` | 0–10 分；`>= min_score` 才入选 |

### 3.3 排序与输出

- 排序：`composite_score` 降序，同值按 `code` 升序。
- 输出 `metrics`：`composite_score`、`value_score`、`quality_score`、`momentum_score`、
  `volatility_score`、`liquidity_score`、`sentiment_score`、`ownership`、`industry_l1`、
  `close`、`pct_chg`、`pe_ttm`、`pb`。
- 额外输出 `metrics.score_breakdown`（分项权重、分位、贡献，来自评分表 `detail`）。

### 3.4 数据门槛

- `composite_score` 不能全空；
- 股票池必须存在 `ownership == '央企'` 的标的，否则给出数据不可用错误。

---

## 4. A4 · 高股息质量 × 低估值

### 4.1 参数

| 参数 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `limit` | 20 | 1–1000 | 返回数量上限 |
| `pe_min` | 3.0 | 0–1000 | PE_TTM 严格下限（剔除低 PE 陷阱） |
| `pe_max` | 15.0 | 0.01–1000 | PE_TTM 严格上限 |
| `div_min` | 4.5 | 0–100 | 股息率下限（%） |
| `dv_max` | 15.0 | 0.1–100 | 股息率上限（%） |
| `pb_max` | 2.5 | 0.1–50 | PB 上限 |
| `min_score` | 6.5 | 0–10 | 最低综合评分 |
| `min_amount` | 5.0e7（5000 万元） | 0–1e12 | 20 日均成交额下限（元） |
| `trend_dev` | 0.9 | 0.5–1.5 | 趋势容忍度：`close >= MA120 × trend_dev` |

### 4.2 条件因子

| 因子 | 中文名 | 数据源 | 说明 |
|---|---|---|---|
| `pe_ttm` | 滚动市盈率 | `fact_stock_daily` | 必须落在 `(pe_min, pe_max)`；`pe_min` 默认 3，剔除周期股/一次性收益造成的低 PE 假象 |
| `dv_ttm` | 滚动股息率（%） | `fact_stock_daily` | 必须落在 `[div_min, dv_max]` |
| `pb` | 市净率 | `fact_stock_daily` | 必须 `> 0` 且 `<= pb_max` |
| `composite_score` | 综合评分 | `fact_stock_score_daily` | `>= min_score`（默认 6.5，剔除高股息低质量） |
| `amount_ma20` | 20 日均成交额 | `fact_feature_daily` | `>= min_amount` |
| `ma120` | 120 日均线 | `fact_feature_daily` | 必须 `> 0` |
| `close` | 收盘价 | `fact_stock_daily` | `>= ma120 × trend_dev` |
| `market_regime` | 市场阶段 | `market_regime_daily` | 仅 `bottoming` / `pull_down` / `risk_off` 放行；阶段缺失按放行 |

### 4.3 排序与输出

- 排序：`dv_ttm` 降序，同值按 `code` 升序。
- 输出 `metrics`：`pe_ttm`、`dv_ttm`、`pb`、`ps_ttm`、`composite_score`、`amount_ma20`、
  `ma120`、`close`、`pct_chg`、`industry_l1`。
- `metrics.triggered` 带一条 `a4_regime`（`scope="market"`）。

### 4.4 数据门槛

- `dv_ttm`、`pe_ttm`、`pb`、`composite_score` 不能全空；
- `amount_ma20`、`ma120` 覆盖率均需 ≥ 50%。

---

## 5. C1 · 海龟突破

### 5.1 参数

| 参数 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `limit` | 20 | 1–1000 | 返回数量上限 |
| `lookback` | 20 | 5–60 | 突破窗口（交易日） |
| `min_amount` | 1.0e8（1 亿元） | 0–1e12 | 当日成交额下限（元） |
| `mom60_min` | 0.0 | -1.0–1.0 | 60 日动量下限；默认 0 表示只在中期上涨趋势中做突破 |
| `vol_cap` | 0.05 | 0.005–0.5 | 20 日收益波动率（日标准差）上限；默认 5% |

### 5.2 条件因子

| 因子 | 中文名 | 数据源 | 说明 |
|---|---|---|---|
| `high20_prev` | 前 N 日最高价 | 日线窗口计算 | 不含当日；`adj_close > high20_prev` 即突破 |
| `adj_close` | 复权收盘价 | 日线 × `adj_factor` | 突破、收阳、真涨均用它 |
| `adj_open` | 复权开盘价 | 日线 × `adj_factor` | `adj_close > adj_open`（收阳） |
| `prev_adj_close` | 前一日复权收盘 | 日线 × `adj_factor` | `adj_close > prev_adj_close`（真涨） |
| `amount` | 当日成交额 | `fact_stock_daily` | `>= min_amount` |
| `mom60` | 60 日动量 | 复权收盘计算 | `adj_close[-1] / adj_close[-61] - 1 >= mom60_min` |
| `vol20` | 20 日波动率 | 复权收盘日收益标准差 | `<= vol_cap`；剔除高波动题材股 |
| `is_limit_up` | 当日涨停 | `fact_stock_daily` | 必须为假（涨停日 T+1 开盘难以成交） |

### 5.3 排序与输出

- 排序：`float_cap`（流通市值）降序。
- **执行建议：40 个交易日调仓**。回测中 20 日调仓全窗口 -17.6%，40 日调仓 +12.1%；
  默认 20 日调仓只用于横向对比，不推荐实盘跟踪。
- 输出 `metrics`：`high20_prev`、`adj_close`、`adj_open`、`prev_adj_close`、`mom60`、`vol20`、
  `amount`、`float_cap`、`close`、`pct_chg`、`industry_l1`。

### 5.4 数据门槛

- `high20_prev` 全空时报“历史窗口不足”；需至少 `max(lookback + 1, 61)` 根有效日线
  （61 根用于 60 日动量，缺一不可）。

---

## 6. C2 · 高窄旗形

### 6.1 参数

| 参数 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `limit` | 20 | 1–1000 | 返回数量上限 |
| `lookback` | 40 | 20–80 | 动量窗口（交易日） |
| `mom_min` | 1.6 | 1.0–5.0 | 40 日区间比下限 |
| `tight_max` | 1.12 | 1.0–2.0 | 10 日收敛上限 |
| `high_level` | 0.8 | 0.5–1.0 | 高位保持比例 |
| `shrink_max` | 0.5 | 0.1–1.0 | 缩量比例上限 |
| `min_amount` | 1.0e8（1 亿元） | 0–1e12 | 20 日均额下限（元） |

### 6.2 条件因子

| 因子 | 中文名 | 数据源 | 说明 |
|---|---|---|---|
| `range40_ratio` | 40 日区间比 | `high40 / low40` | 必须 `> mom_min`（强动量） |
| `high40` / `low40` | 40 日最高 / 最低 | 日线窗口计算 | 区间比分子与分母 |
| `high10` / `low10` | 10 日最高 / 最低 | 日线窗口计算 | `high10 / low10 < tight_max`（收敛） |
| `high_level` 逻辑 | 高位保持 | `low10 >= high_level × high40` | 收敛不能跌回区间底部 |
| `vol_ma20_prev` | 前 20 日均量 | 日线窗口计算 | 缩量基准 |
| `volume` | 当日成交量 | `fact_stock_daily` | `< shrink_max × vol_ma20_prev` |
| `amount_ma20` | 20 日均成交额 | `fact_feature_daily` | `>= min_amount` |

### 6.3 排序与输出

- 排序：`range40_ratio` 降序。
- 输出 `metrics`：`range40_ratio`、`high40`、`low40`、`high10`、`low10`、
  `vol_ma20_prev`、`volume`、`close`、`pct_chg`、`amount_ma20`、`industry_l1`。

### 6.4 数据门槛

- `high40` 全空时报“历史窗口不足”；需至少 `max(lookback, 21)` 根有效日线。

---

## 7. C3 · RPS 相对强度突破

### 7.1 参数

| 参数 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `limit` | 20 | 1–1000 | 返回数量上限 |
| `lookback` | 120 | 60–250 | RPS / 新高窗口（交易日） |
| `rps_threshold` | 90.0 | 50–100 | RPS 分位下限 |
| `near_high_ratio` | 0.85 | 0.5–1.0 | 接近新高比例 |
| `min_amount` | 1.0e8（1 亿元） | 0–1e12 | 20 日均额下限（元） |
| `vol_cap` | 0.05 | 0.005–0.5 | 20 日收益波动率（日标准差）上限 |
| `roc20_max` | 0.08 | 0–1 | 20 日涨幅上限（剔除已加速的追高标的） |

### 7.2 条件因子

| 因子 | 中文名 | 数据源 | 说明 |
|---|---|---|---|
| `roc120` | 120 日收益率 | 复权收盘计算 | `end / start - 1` |
| `rps` | 相对强度百分位 | 当日横截面排名 | `>= rps_threshold`（默认前 10%） |
| `high120` | 120 日最高价 | 日线窗口计算 | `adj_close >= near_high_ratio × high120` |
| `adj_close` | 复权收盘价 | 日线 × `adj_factor` | 与新高比较 |
| `roc20` | 20 日涨幅 | 复权收盘计算 | `<= roc20_max`（默认 8%，避免追在加速段） |
| `vol20` | 20 日波动率 | 复权收盘日收益标准差 | `<= vol_cap`（默认 5%） |
| `amount_ma20` | 20 日均成交额 | `fact_feature_daily` | `>= min_amount` |
| 市场门槛 | 沪深300 双均线 | `market_indices` | `close >= MA20 × 0.995` 且 `close >= MA60`；指数缺失按放行 |

### 7.3 排序与输出

- 排序：`rps` 降序。
- 输出 `metrics`：`rps`、`roc120`、`roc20`、`vol20`、`high120`、`adj_close`、`close`、`pct_chg`、
  `amount_ma20`、`industry_l1`。
- `metrics.triggered` 带一条 `c3_market`（`scope="market"`）。

### 7.4 数据门槛

- `roc120` 全空时报“历史窗口不足”；需至少 `max(lookback + 1, 21)` 根有效日线。

---

## 8. C6 · 布林挤压突破

> 2026-11 优化未改动参数；跨四相位均值为正（+11.2%），2026-11-28 起并入正式启用集。

### 8.1 参数

| 参数 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `limit` | 20 | 1–1000 | 返回数量上限 |
| `squeeze_pct` | 20.0 | 1–100 | 带宽 60 日分位上限（%） |
| `vol_mult` | 1.5 | 0.1–10.0 | 突破量能倍数 |
| `range_max` | 10.0 | 0–100 | 20 日区间宽度上限（%） |
| `min_amount` | 2.0e8（2 亿元） | 0–1e12 | 20 日均额下限（元） |

### 8.2 条件因子

| 因子 | 中文名 | 数据源 | 说明 |
|---|---|---|---|
| `boll_width_pct60` | 布林带宽 60 日分位 | `fact_feature_daily` | 用**前一日**带宽的 60 日分位，`<= squeeze_pct` |
| `boll_upper` | 布林上轨 | `fact_feature_daily` | `close > boll_upper` |
| `high20_close` | 前 20 日最高收盘 | `fact_feature_daily` | 不含当日；`close > high20_close` |
| `vol_ma5` | 5 日均量 | `fact_feature_daily` | 量能基准 |
| `volume` | 当日成交量 | `fact_stock_daily` | `>= vol_mult × vol_ma5` |
| `range20_pct` | 20 日区间宽度（%） | `fact_feature_daily` | 不含当日；`< range_max` |
| `amount_ma20` | 20 日均成交额 | `fact_feature_daily` | `>= min_amount` |
| 市场门槛 | 沪深300 趋势 | `market_indices` | `close >= MA20 × 0.995`；指数缺失按放行 |

### 8.3 排序与输出

- 排序：`boll_width_pct60` 升序（越挤压越靠前）。
- 输出 `metrics`：`boll_width_pct60`、`boll_upper`、`high20_close`、`range20_pct`、
  `volume`、`vol_ma5`、`amount_ma20`、`close`、`pct_chg`、`industry_l1`。
- `metrics.triggered` 带一条 `c6_market`（`scope="market"`）。

### 8.4 数据门槛

- `boll_width_pct60`、`vol_ma5`、`amount_ma20` 任一全空时报“指标尚未生成”。

---

## 9. 因子 × 策略引用矩阵

| 因子 | A3 | A4 | C1 | C2 | C3 | C6 |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| `composite_score` | ✓ | ✓ | | | | |
| `ownership` | ✓ | | | | | |
| `pe_ttm` / `pb` / `ps_ttm` | | ✓ | | | | |
| `dv_ttm` | | ✓ | | | | |
| `amount_ma20` | | ✓ | | ✓ | ✓ | ✓ |
| `ma120` | | ✓ | | | | |
| `close` / `pct_chg` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `high20_prev` / `float_cap` | | | ✓ | | | |
| `mom60` / `vol20` | | | ✓ | | | |
| `range40_ratio` / `high40` / `low40` | | | | ✓ | | |
| `high10` / `low10` / `vol_ma20_prev` | | | | ✓ | | |
| `rps` / `roc120` / `high120` | | | | | ✓ | |
| `roc20` / `vol20` | | | | | ✓ | |
| `adj_close` / `adj_open` | | | ✓ | | ✓ | |
| `boll_width_pct60` / `boll_upper` | | | | | | ✓ |
| `high20_close` / `range20_pct` / `vol_ma5` | | | | | | ✓ |

> `close` 还作为所有策略的通用展示字段与统一过滤条件（`close > 0`）。

---

## 10. 维护约定

1. 新增/修改策略时，先改 `specs.py` 的 `StrategySpec.params_schema` 与 `defaults`，再同步
   `config.py:STRATEGY_DEFAULTS`，最后更新本清单。
2. 新策略默认进入 `CANDIDATE_SPECS`；通过样本外验证并人工确认后才可移入 `SPECS`。
3. 已移除策略（A1/A2/B1–B5/C4/C5）不得重新引用其参数键或排序字段；历史 run 只读。
4. 排序字段必须在 `_SORT_SPEC` 中登记，输出指标必须在 `_METRIC_FIELDS` 中登记，
   否则前端列与回测缓存会出现口径不一致。
5. 本清单仅列策略层因子；指标计算口径见 `docs/wiki/05-engine.md` 与 `backend/app/engine/indicators.py`。
