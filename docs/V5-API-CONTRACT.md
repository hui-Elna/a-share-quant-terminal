# GFNA V5 · 接口契约（API Contract）

> 本文件是**前后端并行的唯一事实源**。任何一端要改字段，先改这里。
> 版本：5.3（V9.1 仓位罗盘与三项修复） ｜ 基础前缀：`/api/v1` ｜ 编码：UTF-8 ｜ 时间：ISO-8601（含时区）

**变更记录**

| 版本 | 日期 | 变更 |
|---|---|---|
| 5.0 | 2026-10-21 | V5 基线：行情 / 个股 / 选股 / 回测 / 新闻 |
| 5.1 | 2026-11-19 | 2026-11 因子优化、信号 schema v4 |
| 5.2 | 2026-11-21 | 仓位罗盘 V9：决策卡 / 情景卡 / T+1 / 复盘 / 推送 |
| 5.3 | 2026-11-22 | 纸面组合回填、成分股广度、ETF 映射；补充 V6–V9 路由索引与错误码 |

---

## 0. 通用约定

### 0.1 成功响应
直接返回业务对象，**不包 `data` 壳**（与 V2/V3 保持一致，减少前端改动）。

**列表分页（C1 追加式）**：既有 `limit` 语义与默认值不变，新增可选 `offset`（默认 `0`）；
被截断的列表可追加 `total / has_more / page / page_size`：

- `total`：过滤后的全量命中数（分页前）；
- `page`：从 1 开始，`page_size` 等于本次 `limit`；
- `has_more`：按**实际返回条数**计算（`offset + 返回数 < total`），不把 `limit` 当承诺条数；
- `count` 若存在，一律表示**本次返回条数**，不是全量。

C1 只追加字段，不改变任何成功响应的包裹形状与默认值；前端应忽略未知的追加字段。

### 0.2 错误响应（统一）
```json
{
  "ok": false,
  "error": { "code": "DATA_NOT_READY", "message": "2026-10-20 的行情尚未采集",
             "detail": { "next_action": "POST /api/v1/admin/ingest/{task}", "task": "snapshot" },
             "suggestion": "先运行日行情采集，数据就绪后重试" },
  "request_id": "..."
}
```
- `suggestion`（可选，C2）：面向用户的一句话处置建议，仅在需要时出现。前端展示优先级为
  `error.suggestion` → `error.detail.next_action` → 统一兜底文案「请稍后重试；若持续失败，请携带错误码与请求 ID 反馈。」；
  三处都没有时也不得把错误体当成功解析。
- 成功响应**不**追加 `request_id`；`request_id` 只出现在错误体顶层，用于排查。
- `detail` 为任意 JSON；`DataUnavailableError` 默认把 `next_action` 放在 `detail` 内，C2 起同名文本也会作为 `suggestion` 返回（重复时前端只显示一次）。

错误码：`DATA_NOT_READY | DATA_QUALITY | BAD_REQUEST | UNKNOWN_ALGO | INVALID_PARAM |
NOT_FOUND | PROVIDER_UNAVAILABLE | INTERNAL`

已定义但**当前不返回**的错误码（前端不要编写专属分支，按通用错误处理）：
`DATA_STALE`、`NO_TRADE_DATE`、`LLM_UNAVAILABLE`、`DB_UNAVAILABLE`、`BUSY`。

HTTP 语义：参数错 422，资源不存在 404，数据未就绪/质量问题 409，外部源不可用 503。

### 0.3 字段命名口径（V5 统一）
- 价格：`open / high / low / close / pre_close`（不再用 `close_price`）
- 涨跌：`pct_chg`（百分数，如 `-1.614` 表示 -1.614%）
- 金额：`amount` 单位为**元**；`volume` 单位为**股**
- 比率：`turnover_rate`、`dv_ttm`、`amplitude` 均为**百分数**
- 评分：`*_score` 统一 **0–10** 分制；`composite_score` 同为 0–10
- 标签：`is_*` 一律布尔

### 0.4 数据新鲜度对象（`Freshness`）
所有"展示数据"的接口都应能附带它：
```json
{ "dataset": "stock_daily", "display_name": "个股日线", "status": "ok",
  "is_critical": true,
  "last_success_at": "2026-10-21T15:42:11+08:00",
  "last_attempt_at": "2026-10-21T15:42:11+08:00",
  "last_trade_date": "2026-10-21",
  "rows": 5382, "coverage": 0.987, "anomalies": 0, "error": null }
```
`status ∈ ok | stale | quality_failed | error | unknown`；`coverage` 为 0–1 小数。
`is_critical=true` 的数据集缺失时，`/health.data_ready` 为 false。

已知 dataset 取值：`stock_universe`、`stock_daily`、`stock_features`、`stock_scores`、
`sector_daily`、`sector_members`、`moneyflow`、**`valuation`**、`index_daily`、`margin`、
`trade_calendar`、`news`、`algo_run`、`daily_report`。

> 形状说明（**两种并存，均已生效**）：
> - `/api/v1/meta/freshness` 返回 `{items: Freshness[], network_enabled, snapshot_ready_after}`（包裹对象）；
> - 内嵌为**数组**：`market/dashboard`、`market/temperature`、`market/sector/{code}`、`stocks/{code}`；
> - 内嵌为**单个对象或 `null`**：`algo/ladder`、`algo/consensus`。
>
> 前端必须两种形态都兼容（`FreshnessBadge` 与本仓的归一化逻辑已覆盖），不得假设所有 `freshness` 都是数组。

---

## 1. 健康与元信息

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | `{status, service, version, database, latest_trade_date, data_ready, detail}` |
| GET | `/api/v1/meta/freshness` | `{items: Freshness[], network_enabled, snapshot_ready_after}` |
| GET | `/api/v1/meta/trade-calendar?start=&end=` | `{start, end, days:[{cal_date,is_open}]}` |
| GET | `/api/v1/meta/jobs?limit=20&offset=0&task=&status=` | `{items: Job[], total, has_more, page, page_size}`（C1 追加分页元信息；默认排除 `archived`，显式 `status=archived` 仍可审计） |
| GET | `/api/v1/meta/partitions` | `{table, partitions:[{partition,bound}]}` |
| POST | `/api/v1/admin/ingest/{task}` | 手动触发采集，需 `X-Admin-Key`；`task ∈ calendar|universe|snapshot|backfill|sector|sector_members|sector_fallback|moneyflow|valuation|index|margin|features|scores|sector_scores|report|algos|all`；重任务（backfill/all/features/scores/valuation）自动后台执行（`{status:"accepted"}`） |
| POST | `/api/v1/admin/seed-demo` | 仅 `ENV=demo` 时可用（演示数据从生产接口移出） |

`jobs` 单项：
```json
{ "job_id": "…", "task": "daily_snapshot", "dataset": "stock_daily",
  "trade_date": "2026-10-21", "status": "success", "total": 5382, "done": 5382,
  "failed": 0, "rows_written": 5382, "started_at": "…", "finished_at": "…",
  "elapsed_ms": 41234, "error": null }
```

> 启动清理：服务启动时会把开始时间超过 2 小时仍处于 `running/queued` 的 `ingest_job`、
> `backtest_run`（以及兼容的 `stg_run.running`）标记为 `failed`，`error` 为
> “进程重启遗留：服务启动时任务仍处于 running/queued 状态，已标记 failed”，
> 同时写 `finished_at`/`elapsed_ms`。清理失败只记日志、不阻断启动。
>
> `status` 取值：`running/success/partial/failed/skipped/archived`。其中 `skipped` 的
> `error` 是**跳过原因标记**（如 `already_collected`、`lock_busy`、`non_trade_day`），
> 不代表业务失败；前端不得按红色错误渲染。

## 2. 新闻（保留，字段与 V2 完全兼容）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/articles?page=&page_size=&source_id=` | `PaginatedResponse` |
| GET | `/api/v1/articles/{id}` | `Article` |
| GET | `/api/v1/search?q=&page=&page_size=` | `PaginatedResponse` |
| GET | `/api/v1/sources` | `Source[]`（新增，前端不再写死来源） |
| GET | `/api/v1/stats` | `{total_articles,total_sources,articles_today,processed_count}` |
| GET | `/api/v1/stats/daily-trend?days=30` | `{days,data:[{date,count}]}` |
| GET | `/api/v1/stats/source-distribution` | `{data:[{name,count,percentage}],total}` |
| GET | `/api/v1/stats/keywords?limit=20` | `{keywords:[{word,count}]}` |
| GET | `/api/v1/stats/processing-status` | `{total,processed,unprocessed,failed,percentage}` |

`Article` 字段：`id,title,url,content,author,publish_time,source_id,source_name,summary,url_hash,crawl_time,is_processed,sentiment_score`。

## 3. 行情（替代旧 `/analysis/*`）

### 3.1 GET `/api/v1/market/dashboard?trade_date=`
```json
{
  "trade_date": "2026-10-21",
  "indices": [ { "index_code": "sh000001", "index_name": "上证指数",
                 "open": 3853.6, "high": 3861.0, "low": 3808.6, "close": 3814.2,
                 "pre_close": 3876.8, "pct_chg": -1.614, "volume": 50820390500,
                 "amount": null,
                 "ret_1d": -1.614, "ret_5d": -0.895, "ret_20d": -5.479,
                 "ret_60d": -3.059, "ret_ytd": -6.836,
                 "rel_300_20d": 3.480, "rel_300_ytd": 4.533 } ],
  "us_indices": [ { "index_code": "^DJI", "index_name": "道琼斯工业", "close": 42100.5,
                    "pct_chg": 0.31, "trade_date": "2026-10-20",
                    "ret_1d": 0.31, "ret_5d": 1.2, "ret_20d": -0.8,
                    "ret_60d": 4.1, "ret_ytd": 12.3,
                    "rel_300_20d": null, "rel_300_ytd": null } ],
  "sectors": [ { "sector_code": "BK1036", "sector_name": "半导体", "sector_type": "industry",
                 "close": 1234.5, "pct_chg": 3.2, "amount": 5.1e10, "turnover_rate": 2.4,
                 "up_count": 45, "down_count": 12, "leading_stock": "中芯国际",
                 "leading_stock_pct": 5.8,
                 "technical_score": 7.8, "sentiment_score": 6.4, "news_score": 5.5,
                 "composite_score": 6.9,
                 "strength_leaders": [ {"code":"688981","name":"中芯国际","change_pct":5.8,
                                        "rank_text":"市值第1 + 日均成交第1"} ],
                 "sentiment_leaders": [ {"code":"688256","name":"寒武纪","change_pct":8.1,
                                         "rank_text":"涨幅第1 × 换手率第2"} ] } ],
  "heatmap_data": [ { "sector_code": "BK1036", "sector_name": "半导体", "pct_chg": 3.2 } ],
  "heatmap_total_matching": 86,
  "top_sectors": [ /* 同 sectors 结构，按 pct_chg 降序前 10 */ ],
  "bottom_sectors": [ /* 升序前 10 */ ],
  "top_sectors_total_matching": 86,
  "bottom_sectors_total_matching": 86,
  "margin_data": { "trade_date": "2026-10-21", "margin_balance": 16356.16,
                   "short_balance": 174.23, "margin_change": 120.5, "northward_net": null },
  "report": { "content": "## 今日大盘综述…",
              "structured": { "conclusions": [ { "text": "…", "evidence": "…" } ],
                              "risks": [ "…" ], "data_gaps": [ "…" ] },
              "is_fallback": false, "model": "deepseek-chat", "created_at": "…" },
  "freshness": [ /* Freshness[] */ ],
  "stats": { "scored_sectors": 30, "total_sectors": 86, "signal_count": 0 }
}
```
- `sectors[]` / `top_sectors[]` / `bottom_sectors[]` 与 3.5 的 `items` 同结构（含 T4 的 `market_cap / member_count / is_partial / partial_member_count` 等字段）。
- 三个截断数组的 C1 追加字段：`heatmap_total_matching` / `top_sectors_total_matching` /
  `bottom_sectors_total_matching` 等于当日 `sectors` 全量数；数组本身仍是截断后的 50 / 10 / 10 条，
  前端据此显示「已显示 N / 共 M」。
- 无数据时返回 200 + `sectors: []` + `freshness` 里 `status=unknown`，**不要 500**。
- `report` 为 `null` 表示尚未生成。
- `report.structured` 为日报的结构化结论 `{conclusions, risks, data_gaps}`；历史日报行可能为 `null`，
  前端只做展示、不据此重算正文。`content` 为 Markdown 文本，渲染时必须转义、不得直出 HTML。
- `indices[]` / `us_indices[]` 的 `ret_1d/5d/20d/60d/ytd` 为**百分数**（-5.48 表示 -5.48%），按交易日窗口计算；历史长度不足该窗口时为 `null`，**不返回 0**。
- `ret_ytd` 基准是「当年首个交易日的前一交易日」收盘；若该基准不在库内（次新指数）则为 `null`。
- `rel_300_20d / rel_300_ytd` 是相对沪深300 的**超额百分点**（指数收益 − 沪深300 同周期收益），仅 A 股指数返回数值；`us_indices` 跨市场无意义，一律 `null`。

### 3.2 GET `/api/v1/market/sector/{sector_code}?trade_date=`
```json
{ "sector_code": "BK1036", "sector_name": "半导体", "sector_type": "industry",
  "is_partial": false, "partial_member_count": null,
  "trade_date": "2026-10-21",
  "market_data": { "close": 1234.5, "pct_chg": 3.2, "volume": 0, "amount": 5.1e10,
                   "turnover_rate": 2.4, "up_count": 45, "down_count": 12,
                   "leading_stock": "中芯国际", "leading_stock_pct": 5.8 },
  "scores": { "technical": 7.8, "sentiment": 6.4, "news": 5.5, "composite": 6.9,
              "detail": { "trend": 8.0, "momentum": 7.2, "volume": 7.9,
                          "margin": 5.0, "northward": 5.0, "moneyflow": 6.1,
                          "news": 5.5, "weights": { "…": "…" } } },
  "leaders": { "strength": [ … ], "sentiment": [ … ] },
  "recent_trend": [ { "date": "2026-10-17", "close": 1200.1, "pct_chg": 1.2 } ],
  "members": [ { "code": "688981", "name": "中芯国际", "pct_chg": 5.8 } ],
  "members_total_matching": 96, "total_matching": 96 }
```
- `members` 按当日 SCD2 名单返回，最多 200 条；`name` 取自 `dim_stock`，名称缺失时为 `null`，`pct_chg` 缺失时也为 `null`。
- `members_total_matching`（C1）为该板块当日名单的全量成员数（截断前）；`total_matching` 是同值的通用别名，
  两者都不是「本次返回条数」。前端据此显示「已显示 N / 共 M」，两者不一致时以 `members_total_matching` 为准。
- 名单不完整（THS `pages > 10`，仅开放前后各 5 页）时返回已抓取的部分，不补 0；`is_partial=true`，`partial_member_count` 为样本成员数（当前约 100 只），完整名单时分别为 `false / null`。

### 3.3 GET `/api/v1/market/indices?trade_date=&days=`
- `days=1`（默认）：`{ "trade_date": "2026-10-26", "items": [ /* 与 dashboard.indices 同结构，含多周期收益与相对300字段 */ ] }`
- `days>1`：`{ "trade_date": "2026-10-26", "series": { "sh000001": [ { "trade_date": "…", "open": …, "high": …, "low": …, "close": …, "pct_chg": …, "volume": …, "amount": … } ] } }`（序列模式不附带多周期收益字段）

### 3.4 GET `/api/v1/market/temperature?trade_date=&days=`
市场温度计（V6 T3）。无 `trade_date` 取库内最新交易日；`days` 取值 1–250，默认 1。

`days=1`：
```json
{ "trade_date": "2026-10-26", "temp_score": 41.3, "confidence": "ok",
  "up_count": 2082, "down_count": 2991, "flat_count": 144, "total_count": 5217,
  "limit_up_count": 65, "limit_down_count": 34, "median_pct": -0.495,
  "above_ma20_ratio": 0.2149, "amount": 1611431825081.23,
  "amount_vs_ma20": 0.963, "index_vol_annual": 15.112,
  "components": { "available_count": 6, "total_count": 6,
                  "items": [ { "key": "up_ratio", "label": "上涨占比", "unit": "%",
                               "raw": 0.3991, "score": 34.9, "weight": 0.3,
                               "direction": 1, "available": true, "note": "…" } ] },
  "src": "engine", "updated_at": "…", "freshness": [ /* Freshness[] */ ] }
```

`days>1`：`{ "trade_date": "2026-10-26", "count": 60, "series": [ /* 每日 trade_date/temp_score/confidence/计数/比率，升序 */ ] }`

- `temp_score` 为 0–100（1 位小数）；子项缺失时存 `null` 且 `confidence` 降级，**不以 0 代替**。
- `confidence`：`ok` = 6/6 子项可用；`partial` = 4–5 项可用；`insufficient` = 关键项（上涨占比 / 站上 MA20）缺失或可用项 < 4，此时 `temp_score` 为 `null`。
- `total_count` 为有效交易家数（剔除停牌）；`above_ma20_ratio` 是 0–1 小数；`amount_vs_ma20` 是倍数；`index_vol_annual` 是百分数。
- 指定日没有温度记录时返回 `409 DATA_NOT_READY`（`detail.trade_date` 指明日期）；行情或特征尚未就绪时应先运行回填/管线，而不是当成 0 分。

### 3.5 GET `/api/v1/market/sectors?trade_date=&sector_type=`
板块列表（V6 T4 起作为 treemap 数据源正式文档化）。`sector_type` 取 `industry`（默认）/ `concept`；无 `trade_date` 取库内最新交易日。
```json
{ "trade_date": "2026-10-26", "count": 90,
  "items": [ { "sector_code": "THS_消费电子", "sector_name": "消费电子",
               "sector_type": "industry", "close": 10977.422, "pct_chg": -1.421,
               "amount": 43380581000.0, "turnover_rate": null,
               "up_count": 17, "down_count": 69,
               "leading_stock": "达瑞电子", "leading_stock_pct": 20.0,
               "market_cap": 2637834474066.67, "float_cap": 2399042204473.82,
               "main_net_inflow": -690026326.24, "rs_20d": -3.907,
               "member_count": 96, "is_partial": false, "partial_member_count": null,
               "technical_score": 4.04,
               "sentiment_score": 4.96, "news_score": 5.0, "composite_score": 4.6,
               "strength_leaders": [ … ], "sentiment_leaders": [ … ] } ] }
```
- `items` 与 3.1 `sectors` 同结构；T4 **增列** `market_cap / float_cap / main_net_inflow / rs_20d / member_count`，原字段一个不动。
- `market_cap` / `float_cap`：按当日 SCD2 名单聚合的成分股合计（元）；成分数 < 3、名单不完整或个股市值缺失时为 `null`，不补 0。
- `main_net_inflow`：成分股 `fact_moneyflow.main_net_t0` 合计（元）；与市值同样受成分数 < 3 与名单完整性限制。
- `rs_20d`：板块 20 交易日收益 − 沪深300 同窗口收益（百分点）；任一侧不足 21 个交易日为 `null`。
- `member_count`：当日 SCD2 **完整**名单成分数；`0` 表示该板块没有名单（概念板块常见）；`null` 表示名单不完整（THS 只开放前后各 5 页），此时市值/资金流同 `null`，前端按「数据不足」显示。
- `is_partial` / `partial_member_count`：名单是否仅为 partial 样本，以及该样本的成员数（完整名单为 `false / null`）。partial 板块的 `up_count/down_count/market_cap` 不可用，前端必须显示「名单不完整 · 样本 N 只」并用「不适用」代替家数/市值，不能把样本数当成完整家数。
- `up_count` / `down_count` 在部分同花顺概念板块上恒为 `null`（源头不提供），前端显示「不适用」，**不得**按 0 渲染。

### 3.6 GET `/api/v1/market/sector-scores/sparkline?codes=&days=30&end=`
批量板块评分序列（P3-2 评分榜 sparkline）。`codes` 为逗号分隔板块代码（去重保序，最多 100 个）；
`days` 为最近 N 个**评分日**，取值 5–120，默认 30；`end` 为截止交易日，默认库内最新评分日。
```json
{ "end": "2026-11-06", "days": 30,
  "dates": ["2026-10-21", "2026-10-22", "…", "2026-11-06"],
  "items": [ { "sector_code": "THS_光伏设备",
               "scores": [6.21, 6.96, 7.08, null, "…"] } ] }
```
- `dates` 是 `sector_score` 中实际存在的评分日（升序、最多 `days` 个），不是自然日日历；当前库内仅 13 个评分日，因此 30 日请求返回 13 个点。
- `scores` 与 `dates` **等长**；某板块某评分日无记录时填 `null`，**不补 0、不前值填充**，前端渲染为 `—` 或断线。
- 未知板块代码不报错，返回全 `null` 序列；库内无任何评分时返回 `{ "end": null, "days": 30, "dates": [], "items": [] }`。
- 参数校验失败（`codes` 为空、含空白/超长代码、超过 100 个）返回 `422 INVALID_PARAM`，`detail.sector_code` 指明违规代码。
- 前端 `ScoreRanking` 仅对 TOP 10 板块请求 30 日序列并复用 `Sparkline`（0/1 个有效点显示 `—`，不画假线）；`sm` 以下隐藏走势列。

## 4. 个股（E4）

### 4.1 GET `/api/v1/stocks/{code}`
```json
{ "code": "688981", "name": "中芯国际", "exchange": "SH", "board": "科创板",
  "industry_l1": "电子", "industry_l2": "半导体", "ownership": "地方国企",
  "is_st": false, "list_date": "2020-07-16", "status": 1,
  "latest": { "trade_date": "2026-10-21", "open": 88.1, "high": 92.0, "low": 87.5,
              "close": 91.2, "pre_close": 88.0, "pct_chg": 3.636, "volume": 12345678,
              "amount": 1.1e9, "turnover_rate": 3.1, "market_cap": 7.2e11,
              "pe_ttm": 55.3, "pb": 4.1, "ps_ttm": 9.2, "dv_ttm": 0.35,
              "is_limit_up": false, "is_limit_down": false, "is_suspended": false },
  "features": { "ma5": 90.1, "ma20": 88.7, "rsi14": 61.2, "macd_dif": 0.8,
                "macd_dea": 0.6, "macd_hist": 0.2, "atr14": 3.1,
                "momentum_20": 8.4, "volatility_20": 32.1, "volume_ratio": 1.3,
                "ma_bullish": true, "macd_golden_cross": false },
  "scores": { "value": 3.1, "quality": 6.2, "momentum": 7.4, "volatility": 5.1,
              "liquidity": 8.8, "sentiment": 5.0, "composite": 5.9 },
  "sectors": [ { "sector_code": "BK1036", "sector_name": "半导体" } ],
  "sectors_total_matching": 1,
  "freshness": [ /* Freshness[] */ ] }
```
- `sectors` 为当日 SCD2 有效归属，最多 20 条；`sectors_total_matching`（C1）为截断前的全量归属数，
  前端据此显示「已显示 N / 共 M」，不得把 20 当成全部板块。
- `latest` / `features` / `scores` 在对应日数据缺失时逐字段为 `null`，不补 0；`freshness` 为 `Freshness[]`（与 0.4 一致）。

### 4.2 GET `/api/v1/stocks/{code}/kline?limit=250&start=&end=`
```json
{ "code": "688981", "adjust_basis": "raw", "count": 250,
  "items": [ { "trade_date": "2026-10-21", "open": 88.1, "high": 92.0,
    "low": 87.5, "close": 91.2, "volume": 12345678, "amount": 1.1e9,
    "pct_chg": 3.636, "ma5": 90.1, "ma20": 88.7, "ma60": 85.2,
    "boll_mid": 88.7, "boll_upper": 95.2, "boll_lower": 82.2 } ] }
```
- `adjust_basis` 固定为 `"raw"`：OHLC 为未复权原始价；MA/BOLL 也由同一组未复权收盘价计算（`features.py` 不读 `adj_factor`）。前端画 K 线与回测成交标记均按此口径，不做二次复权换算。
- `boll_mid/upper/lower` 来自 `fact_feature_daily`；特征缺失的日期返回 null（不是 0）。

### 4.3 GET `/api/v1/stocks/search?q=&limit=20&offset=0`
`{ "items": [ { "code": "688981", "name": "中芯国际", "industry_l1": "电子" } ],
   "total": 1, "has_more": false, "page": 1, "page_size": 20 }`
- `limit` 1–50，默认 20；`offset` 默认 0（C1）。`total` 为匹配全量数，`items` 只含本页。

### 4.4 GET `/api/v1/stocks/sparkline/batch?codes=&days=30&end=`
共识榜 / 评分榜的批量走势端点（P2-5）。一次请求替代逐行 `useKline`。
```json
{ "end": "2026-11-07", "days": 30, "adjust_basis": "raw",
  "items": [ { "code": "601668", "closes": [4.31, 4.35, 4.42, …] },
             { "code": "600938", "closes": [ … ] } ] }
```
- `codes`：逗号分隔，最多 200 个，允许省略前导零（`1` → `000001`）；重复代码自动去重，返回顺序与首次出现顺序一致。空值、非纯数字、超过 6 位或超过 200 个都会返回 `422 INVALID_PARAM`。
- `days`：5–120，默认 30，按**交易日**取最近 N 条，不是自然日。
- `end`：截止交易日，默认库内最新行情日；查询窗口为 `end − (days×2+10)` 自然日，超出窗口的缺口不向前补齐。
- `closes`：**未复权原始收盘价**，与 4.2 单只 K 线的 `close` 逐值一致（同一 `fact_stock_daily.close`）；升序，最多 `days` 个。
- 无数据的代码返回空数组 `[]`，不补 0、不插值；前端显示 `—`。
- `adjust_basis` 固定 `"raw"`，与 4.2 口径一致。

## 5. 选股（E3，兼容旧路径）

### 5.1 GET `/api/v1/algo/list`

> V8 M1（2026-11-15）起当前正式启用策略只有 **a3 / a4**；A1/A2/B1–B5 的实现已在 V9 清理中删除（历史 run 仍可查询）。
> `include_observation=true` 时额外返回 c1/c2/c3/c6 观察候选（`is_active=false`、`is_observation=true`），
> 默认不返回。观察候选可查看/执行/回测，但不进入共识、天梯与正式统计。

```json
[ { "algo_code": "a4", "algo_name": "高股息质量 × 低估值", "algo_type": "value",
    "short_desc": "…", "full_desc": "…",
    "params_schema": [ { "key": "pe_min", "label": "PE_TTM 下限", "type": "number",
                         "default": 3.0, "min": 0.0, "max": 1000 },
                       { "key": "pe_max", "label": "PE_TTM 上限", "type": "number",
                         "default": 15.0, "min": 0.01, "max": 1000 },
                       { "key": "pb_max", "label": "PB 上限", "type": "number",
                         "default": 2.5, "min": 0.1, "max": 50 } ],
    "is_active": true, "is_observation": false, "sort_order": 2 } ]
```

### 5.2 GET `/api/v1/algo/{algo_code}?trade_date=&limit=20&offset=0&force_refresh=false&<策略参数>`
```json
{ "algo": "a4", "algo_name": "高股息质量 × 低估值", "trade_date": "2026-11-13",
  "run": { "run_id": "9f2c…", "created_at": "2026-11-13T16:02:11+08:00",
           "elapsed_ms": 812, "universe_count": 5216, "signal_count": 8,
           "data_version": "2026-11-13:b5382", "signal_schema": 4,
           "params": { "pe_min": 3.0, "pe_max": 15.0, "div_min": 4.5, "dv_max": 15.0,
                       "pb_max": 2.5, "min_score": 6.5, "limit": 20 } },
  "from_cache": true, "count": 8,
  "total": 8, "has_more": false, "page": 1, "page_size": 20,
  "data": [ { "code": "601668", "name": "中国建筑", "rank": 1,
              "score": 6.03, "composite_score": 7.4,
              "close": 5.2, "pct_chg": 0.4,
              "pe_ttm": 5.93, "pb": 0.22, "ps_ttm": 0.1, "dv_ttm": 6.03,
              "amount_ma20": 800000000.0, "ma120": 4.8,
              "metrics": { "pe_ttm": 5.93, "dv_ttm": 6.03, "pb": 0.22,
                           "composite_score": 7.4 } } ] }
```
- `limit` 1–1000，默认 20，同时是策略参数「返回数量上限」：一个 run 只保存该上限内的信号，并参与缓存键。
- `offset`（C1）默认 0，只在**该 run 已保存的信号**内截取，不改变缓存键与默认值；`count` 为本次返回条数，
  `total` 为该 run 保存的信号总数，`has_more/page/page_size` 见 0.1。前端只应把 `data` 当本页数据。
- 由于一个 run 最多保存 `limit` 条信号，`total ≤ limit`；`offset > 0` 只截取该 run 已保存信号的尾部，
  `offset ≥ total` 返回空 `data`（`count=0`）。需要看到更多信号时应提高 `limit` 重新执行（会生成新 run），
  而不是反复翻页。
- **`score` 是该策略的排序指标原始值**（A3=综合分、A4=股息率、C1/C2/C3/C6=各自排序指标），
  **`composite_score` 才是 0–10 的综合因子得分**（可能为 null）。前端"得分"列请用后者。
- **参数必须真生效**：不同 `pe_max` 必须产生不同 `run_id` 与不同结果。
- `data[].metrics` 为策略依据的完整指标，前端表格列由 `metrics` 动态生成。
- `run` 额外字段：`status`（running/success/failed/superseded；superseded 表示同参数旧 run 已被新 run 取代，仍可审计但不参与统计）、失败时 `error`、回测 run 带 `is_backtest`、快照版本 `signal_schema`（见下）。

**run 级市场门槛 `market_gate`（A4/C3/C6）**

C6 每次响应顶层带 `market_gate`（新 run 与缓存 run 均按 `trade_date` 现算，不落库）：
```json
{ "index_code": "000300", "available": true, "close": 4103.54, "ma20": 4275.67,
  "pct_chg": -0.8, "passed": false, "rule": "沪深300 收盘 ≥ MA20 × 0.995",
  "threshold": 4254.2916 }
```
- `passed` 为该策略对应的门槛（c6=`c6_ok`）；`rule` / `threshold` 供 UI 展示。
- 指数数据缺失时 `available=false`、`passed=true`（按放行处理），`rule` 说明原因。

C3 使用沪深300 双均线闸门（2026-11 新增）：
```json
{ "index_code": "000300", "available": true, "close": 4103.54, "ma20": 4275.67,
  "ma60": 4200.0, "pct_chg": -0.8, "passed": false,
  "rule": "沪深300 收盘 ≥ MA20 × 0.995 且 ≥ MA60", "threshold": 4254.2916 }
```
- `passed = close ≥ MA20 × 0.995 且 close ≥ MA60`（任一均线缺失时按放行处理）。
- 空仓是策略设计的一部分：闸门未通过时不产生信号，前端应提示“市场环境要求未满足”。

a4 额外返回市场阶段字段；仅防御型阶段放行：
```json
{ "index_code": "000300", "available": true, "close": 4103.54, "ma20": 4275.67,
  "pct_chg": -0.8, "passed": false,
  "rule": "仅在防御型市场阶段启用（震荡筑底/向下调整/避险下跌）",
  "regime_code": "trend_up", "regime_label": "趋势上攻",
  "regime_trade_date": "2026-11-13", "regime_since": "2026-10-01", "regime_days": 30,
  "regime_available": true,
  "threshold": ["bottoming", "pull_down", "risk_off"] }
```
- a4 的 `passed = regime_code ∈ {bottoming, pull_down, risk_off}`；阶段数据缺失时 `regime_available=false`、`passed=true`（放行）。
- `available` 仍表示沪深300 指数是否可用，与 `regime_available` 相互独立。
- 0 信号时前端据此显示“防御型门槛未满足…”或“市场环境要求未满足…”，而非笼统的“没有选股信号”。

**逐条依据 `metrics.triggered`（A4/C3/C6 市场门槛）**

A4/C3/C6 的市场门槛会写入 `metrics.triggered`（`scope="market"`），与选股共用同一份判定：
```json
[ { "key": "a4_regime", "label": "仅在防御型市场阶段启用（震荡筑底/向下调整/避险下跌）",
    "scope": "market", "required": true, "passed": false, "actual": false,
    "threshold": ["bottoming", "pull_down", "risk_off"] } ]
```
```json
[ { "key": "c3_market", "label": "沪深300 收盘 ≥ MA20 × 0.995 且 ≥ MA60",
    "scope": "market", "required": true, "passed": false, "actual": false,
    "detail": "沪深300 close=4103.54 ma20=4275.67 ma60=4200.0" } ]
```
```json
[ { "key": "c6_market", "label": "沪深300 收盘 ≥ MA20 × 0.995", "scope": "market",
    "required": true, "passed": false, "actual": false,
    "detail": "沪深300 close=4103.54 ma20=4275.67" } ]
```
- `scope`：`stock`（个股条件）或 `market`（市场环境门槛，前端单独分组）。
- `op` / `threshold` 仅比较类条件返回；市场门槛的 `actual` 为整体布尔。
- `detail` 为人类可读字段快照；所有值均为 JSON 原生类型（numpy / Decimal 已在后端转换）。
- **快照版本 `run.signal_schema`**（P2-7）：`1` = 重构前的历史 run，`metrics` 不含
  `triggered` / `score_breakdown`；`2` = 结构化依据（triggered/score_breakdown）；
  `3` = V8 M1 a4 防御门槛；`4` = 2026-11 因子优化（A4/C1/C2/C3 逻辑与参数变更，结构同 3）。
  缓存只复用当前版本，`<4` 的历史 run 不再命中缓存。
  前端对 `signal_schema < 2` 且存在信号的 run 显示「旧版本快照」，对 `schema >= 2` 但单条信号
  缺字段显示“当前版本未记录该信号依据”，不得混为一谈、渲染成空列表或伪造全通过。
- 历史 run **只读标注，不批量重算**：重跑会生成新 run 并把同参数旧成功 run 置为
  `superseded`，改变历史与统计口径；确需最新依据时由用户显式执行或强制刷新。
- A3 与 C1/C2 不产生结构化 `triggered`；A4 带 `a4_regime`，C3 带 `c3_market`，C6 带 `c6_market`。

**A3 评分拆解 `metrics.score_breakdown`（仅 A3，V6 T7）**

直接读取 `fact_stock_score_daily.detail`，不新增计算：
```json
{ "composite": 8.5,
  "weight_sum": 0.9,
  "parts": [ { "key": "value", "name": "价值", "score": 5.0, "z": 0.5,
               "weight": 0.2, "contribution": 1.1111,
               "raw": { "pe_ttm": null, "pb": null, "ps_ttm": null } } ],
  "formula": "综合分 = 10 × Σ(分位 × 权重) / Σ(当日可用权重)；贡献 = 分项得分 × 权重 / Σ(当日可用权重)",
  "sentiment_source": "neutral" }
```
- `z` 为全市场分位（0–1），`score = z × 10`；`weight` 来自评分明细 `weights`，与
  `backend/app/engine/stock_score.py` 的 `FACTOR_WEIGHTS` 顺序一致，前端不得自造权重。
- `weight_sum` 为当日有分位的因子权重之和；`contribution = score × weight / weight_sum`
  （0–10 分制），逐项贡献之和与 `composite` 一致（±0.01 四舍五入）。
- 分位缺失的因子 `z = null`、`contribution = null`，`score` 仍按中性 0.5 显示 5.0 分，
  但不计入 `weight_sum`（与 `composite_factor` 的逐行归一口径一致）。
- CSV 导出（5.3）不包含 `triggered` / `score_breakdown` 这类结构化字段。

### 5.3 其他
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/algo/{algo_code}/runs?limit=10&offset=0` | `{items: RunSummary[], total, has_more, page, page_size}`（**默认排除回测 run**，需要时加 `include_backtest=true`） |
| GET | `/api/v1/algo/runs/{run_id}` | 单次 run 明细：`{algo, algo_name, trade_date, run:{…}, data:[…]}` |
| GET | `/api/v1/algo/{algo_code}/export.csv?trade_date=&limit=&…` | `text/csv`（UTF-8 BOM；列=排名/代码/名称/综合得分(0-10)/排序分/metrics 动态列） |
| POST | `/api/v1/algo/{algo_code}/refresh` | 忽略缓存重跑（默认参数） |

- `runs` 的 `limit` 1–50，默认 10；`offset` 默认 0（C1）。`total` 为过滤后的历史 run 总数，前端据此显示「已显示 N / 共 M」。
- `RunSummary`：`{run_id, algo, algo_name, trade_date, created_at, elapsed_ms, universe_count, signal_count, data_version, signal_schema, params, status, error, is_backtest}`。
  代码键是 **`algo`**（不是 `algo_code`）；`params` 为该 run 的参数快照，用于复现与导出。
- `export.csv` 的 `limit` 默认 100（1–1000），与策略参数一起参与 run 指纹；**导出前端的展示范围须与该 run 一致**：
  页面展示 `limit=20` 的 run 时，导出也必须传 `limit=20`，否则会命中另一个 run、导出不同结果。
  导出列中 `triggered / score_breakdown` 不参与平铺（见 5.2 末）。

### 5.4 GET `/api/v1/algo/ladder?window=60&horizon=20&trade_date=`

策略天梯：信号级 5/10/20 日前瞻收益（V6 T5）。`window ∈ {20,60,120}`，`horizon ∈ {5,10,20}`，
超出返回 `INVALID_PARAM`；`trade_date` 省略时取库内最新统计日。

口径：**T 日收盘出信号 → T+1 交易日开盘买入**，买入价 `open × adj_factor`；H 日收益 =
自 T+1 起第 H 个交易日 `close × adj_factor / 买入价 − 1`；超额对 `sh000300` 同窗口
（指数不复权，起点 T+1 收盘、终点第 H 日收盘）。**未成熟窗口不计入统计**；
T+1 停牌/一字板（`fillable=false`）排除出胜率与均值。

```json
{ "trade_date": "2026-10-26", "window": 60, "horizon": 20,
  "benchmark": "sh000300", "window_start": "2026-07-27",
  "items": [{
    "algo_code": "b5", "algo_name": "布林下轨 × KDJ 双确认", "rank": 1,
    "sample_signals": 5, "sample_stocks": 5,
    "avg_ret": -0.49, "median_ret": -0.83, "win_rate": 0.2, "avg_excess": 5.35,
    "best": 6.08, "worst": -4.06,
    "immature": 9, "unfillable": 0, "no_data": 0, "total_signals": 14,
    "low_sample": true,
    "spark": [{ "trade_date": "2026-08-17", "win_rate": 0.2, "samples": 5 }],
    "horizons": {
      "5":  { "...": "同顶层统计字段，5 日口径" },
      "10": { "...": "同顶层统计字段，10 日口径" },
      "20": { "...": "同顶层统计字段，20 日口径" }
    }
  }],
  "total": 7, "has_more": false, "page": 1, "page_size": 7,
  "immature_total": 100, "unfillable_total": 0,
  "freshness": { "dataset": "signal_perf", "status": "ok", "...": "契约 0.4" },
  "note": "信号级收益……与回测 FIFO 回合胜率不是同一口径" }
```

- `avg_ret` / `avg_excess` / `best` / `worst` 单位是**百分数**（`3.41` = +3.41%）。
- `win_rate` 为 0–1 小数，口径是 `ret_H > 0` 的信号占比，**不是回测 FIFO 回合胜率**，UI 必须标注。
- `sample_signals` = 已成熟且可成交且收益非空的样本数；`immature / unfillable / no_data`
  与有效样本互斥，`total_signals` 为区间内全部信号。
- 有效样本 < 30 时 `low_sample=true`，排序仅作参考。
- 顶层统计字段等于 `horizons[String(horizon)]`，一次请求即可渲染 5/10/20 三列。
- C1 追加 `total / has_more / page / page_size`：天梯一次返回全部启用策略（当前 7 条），不按 `limit` 截断，
  因此 `total = page_size = items.length`、`has_more=false`；前端仍可复用统一的列表元信息渲染。
- `spark` 为最近 20 个出信号日的胜率（`win_rate` 0–1，`samples` 当日有效样本数）。

### 5.5 GET `/api/v1/algo/consensus?trade_date=&min_count=2&limit=100&offset=0`

多策略共识榜（V6 T6）：同一只票被多个策略同时命中时，按命中策略数排序。

- `min_count` 为**最少命中策略数**，只接受 `2–7`，非法返回 422（统一错误体不适用 FastAPI 参数校验）；
- `limit` 1–500，默认 100；`offset` 默认 0（C1）；`trade_date` 省略时取 `fact_stock_daily.max(trade_date)`（最新行情日）；
- 统计口径：当日 `stg_run.status='success'` 且 `is_backtest IS FALSE` 的**全部 run**；同一策略的多次参数 run
  合并统计，`hit_count = count(DISTINCT algo_code)`。同一策略多次 run 命中同一票只算一次，取**名次最优**
  那次 run 的 `rank/score/metrics`。只统计当前启用策略（历史 a1/a2 run 不参与）。
- **不返回跨策略平均分**：各策略 `score` 量纲不同，混算会误导；`score` 是该策略排序指标原始值。
- `metrics` 只保留每个命中策略的**前 3 个键值**（按键名排序），避免响应膨胀；
- 所有缺失值保持 `null` / `{}`，不补 0。`pct_chg` 为百分数（`1.637` = +1.637%），`composite_score` 为 0–10；
- `total_algos_evaluated` = 当日有成功非回测 run 的策略数（含 0 信号的策略），`hit_count` 不会超过它；
- 失败 run（`status='failed'`）不参与任何计数；`freshness` 对应数据集 `algo_run`。

```json
{ "trade_date": "2026-10-26", "min_count": 2, "total_algos_evaluated": 7,
  "total": 1, "has_more": false, "page": 1, "page_size": 100,
  "items": [{
    "code": "601838", "name": "成都银行",
    "industry_l1": "银行", "board": "主板", "ownership": "央企",
    "hit_count": 2, "best_rank": 3,
    "pct_chg": 1.637, "composite_score": 7.1,
    "algos": [
      { "algo_code": "a4", "algo_name": "高股息质量 × 低估值", "rank": 3, "score": 7.5684,
        "metrics": { "close": 18.63, "composite_score": 7.1, "dv_ttm": 7.5684 } },
      { "algo_code": "a3", "algo_name": "央企 × 综合评分TOP", "rank": 45, "score": 7.1,
        "metrics": { "close": 18.63, "composite_score": 7.1, "dv_ttm": 7.5684 } }
    ]
  }],
  "freshness": { "dataset": "algo_run", "status": "ok", "...": "契约 0.4" },
  "note": "各策略评分量纲不同，榜单按命中策略数排序，不做跨策略分数平均。" }
```

- 排序：`hit_count DESC` → `best_rank ASC` → `composite_score DESC NULLS LAST` → `code ASC`。
- C1 分页：`total` 为通过 `min_count` 的**全量标的数**（分页前），`items` 只含本页；
  `has_more / page / page_size` 见 0.1。`total_algos_evaluated` 是策略数口径，不受 `offset / limit` 影响。
- `best_rank` 为各命中策略名次的最小值（`null` 名次不参与）。
- 单条 SQL 完成；同一策略当日多次 run 的去重与跨策略计数均不产生 N×策略查询。

## 6. 回测（D4/D5）

### 6.1 POST `/api/v1/backtest/{algo_code}` — 创建异步回测任务（P2-4）

Query：`start`（默认近 1 年）、`end`（默认库内最新行情交易日 = `/health.latest_trade_date`，
且不晚于今天；显式传入则原样使用）、
`rebalance_days=20`、`max_positions=20`、`benchmark=sh000300`、`commission=0.0001`、
`stamp_tax=0.0005`、`slippage=0.001`、以及策略参数（同 5.2）。

同步阶段只做参数校验与数据起点裁剪，**立即返回任务信封**，计算在后台执行：
```json
{ "bt_id": "b7d1…", "algo": "a3", "status": "queued",
  "start_date": "2025-10-21", "end_date": "2026-10-21",
  "requested_start": "2025-10-21", "effective_start": "2025-10-21", "start_trimmed": false,
  "poll": "/api/v1/backtest/b7d1…" }
```

- 省略 `end` 时，响应与落库的 `end_date` 是**解析后的最新行情交易日**，不是自然日
  `today`；周末、节假日或当日行情未落库不会把非交易日写进回测区间。
- 参数错误（未知策略、`start >= end`、区间超过 6 年、数据起点晚于 `end`）仍在 POST
  阶段返回 4xx，**不会入队**。
- 入队即落 `backtest_run.status=queued` 并提交；后台任务随后改为 `running`，
  成功写 `metrics/equity_curve/trades`，失败写 `status=failed` + `error`。
- 客户端必须轮询 `poll` 指向的 6.2；回测时长（可达数十秒）不再受 HTTP 30 秒超时约束。
- 后台任务运行在 API 进程内：进程重启时，超过 `TASK_RECOVERY_STALE_SEC`（默认 7200 秒）
  的 `running/queued` 行由启动清理标为 failed；Docker Compose 将该值设为 `0`，
  容器启动即清理全部遗留活跃行。（2026-11 起）
- 调仓日（`trading_days[::rebalance_days]`）的数据缺口**不再让整次回测失败**：
  缺行情/指标/评分的调仓日会被跳过并记录，仅当所有调仓日都缺数据时才返回 failed。
- 后台执行遇到瞬时数据库错误（连接被回收 / 网络抖动 / 连接池等待超时）会自动
  指数退避重试（最多 2 次），复用同一 `bt_id`；业务性错误不重试。
- **契约变更**：旧版 POST 同步返回的完整结果（metrics/曲线/成交/6.4 派生视图）
  现在只在 6.2 的 `success` 响应中出现。

### 6.2 GET `/api/v1/backtest/{bt_id}` — 状态与结果

`GET /api/v1/backtest/{bt_id}/export.csv` — 净值曲线 CSV（含月度收益与回撤段，见 6.4）。

详情响应始终包含：
```json
{ "bt_id": "b7d1…", "algo": "a3", "start_date": "2025-10-21", "end_date": "2026-10-21",
  "params": { "rebalance_days": 20, "max_positions": 20, "benchmark_code": "sh000300",
              "commission_rate": 0.0001, "stamp_tax_rate": 0.0005, "slippage_rate": 0.001,
              "strategy": { "pe_max": 50 } },
  "status": "success", "error": null,
  "metrics": { "total_return": 0.183, "annual_return": 0.181, "max_drawdown": -0.121,
               "sharpe": 1.42, "sortino": 1.9, "win_rate": 0.57, "profit_loss_ratio": 1.8,
               "turnover": 3.4, "excess_return": 0.052, "benchmark_return": 0.131,
               "trade_count": 42, "calmar": 1.5, "volatility": 0.21, "adjusted": true },
  "equity_curve": [ { "date": "2025-10-21", "nav": 1.0, "benchmark": 1.0 } ],
  "trades": [ { "date": "2025-10-23", "code": "600519", "name": "贵州茅台", "side": "buy",
                "price": 1512.3, "shares": 100, "amount": 151230.0, "fee": 42.8,
                "reason": "rebalance" } ],
  "extras_available": true,
  "monthly_returns": [ { "ym": "2025-11", "ret": 0.28, "benchmark": -2.72, "excess": 3.0 } ],
  "drawdown_curve": [ { "date": "2025-10-21", "nav": 1.0, "peak": 1.0, "drawdown": 0.0 } ],
  "rolling_excess_20d": [ { "date": "2025-10-21", "value": null } ],
  "industry_exposure": [ { "date": "2025-10-22", "total_value": 998000.0, "cash": 120000.0,
                           "cash_weight": 12.02, "stock_count": 20, "other_count": 1,
                           "industries": [ { "name": "银行", "weight": 20.05,
                                             "value": 200100.0, "count": 2 } ] } ],
  "created_at": "…" }
```

- `status ∈ queued | running | success | failed`，轮询间隔建议 1–2 秒。
- `queued/running`：`metrics/equity_curve/trades` 为 `null`，`extras_available=false`，
  四个派生数组为空；`error=null`。
- `failed`：`error` 为失败原因，其余结果字段为 `null`；失败记录不自动重试，可重新 POST。
- `success`：字段与旧版同步响应一致，6.4 五项派生视图全部可用。
- `success.metrics` 可能附带数据缺口标记（2026-11 起）：
  `skipped_signal_days`（被跳过的调仓日数量）、`skipped_signal_dates`（日期数组，最多 50 个）。
  缺省表示无缺口。跳过日期不参与调仓，但净值曲线仍按交易日连续计算；仅当所有调仓日
  都缺数据时 `status=failed`，`error` 会给出缺口日期。
- 回测不存在返回 404 `NOT_FOUND`；`export.csv` 在未完成时只输出表头与空 block。

### 6.3 GET `/api/v1/backtest/{bt_id}/trades?code=`
```json
{ "bt_id": "b7d1…", "algo_code": "b1", "code": "600519", "total": 42, "count": 3,
  "items": [ { "date": "2025-10-23", "code": "600519", "name": "贵州茅台", "side": "buy",
               "price": 1512.3, "shares": 100, "amount": 151230.0, "fee": 42.8,
               "reason": "rebalance" } ] }
```
- 直接读 `backtest_run.trades`，**不重新计算回测**；`code` 可省略（省略返回全部），过滤按 6 位补零后精确匹配。
- `total` 为回测**全部**成交记录数（过滤前），`count` 为本次返回条数（过滤后）；省略 `code` 时两者相等。
- `shares=0` 的记录是被拒单（`reason ∈ limit_up/limit_down/suspended/no_data`），前端需与真实成交区分。
- 回测不存在返回 404 `NOT_FOUND`。

### 6.4 回测增强视图（T9）

GET 成功的详情响应在 6.2 字段之外追加以下五项，全部由已落库的 `equity_curve` / `trades` 派生，**不重跑回测**：

- `extras_available`：`equity_curve` 与 `trades` 均非空才为 true；旧记录返回 false，四个数组均为空。
- `monthly_returns`：`[{ym, ret, benchmark, excess}]`，单位 %。
  - 月度收益以「上月末净值」为基准串联（首月以月内首个净值点为基准），`∏(1+ret/100)-1` 与 `metrics.total_return` 一致（<0.1pp）；
  - `benchmark` 历史不足时为 null，此时 `excess` 也为 null。
- `drawdown_curve`：`[{date, nav, peak, drawdown}]`，`drawdown = (nav/peak-1)×100`，最小值等于 `metrics.max_drawdown×100`。
- `rolling_excess_20d`：`[{date, value}]`，20 个交易区间滚动超额（%），前 20 个点为 null；基准缺失时对应点为 null。
- `industry_exposure`：`[{date, total_value, cash, cash_weight, stock_count, other_count, industries:[{name, weight, value, count}]}]`，权重单位 %。
  - 按成交重建调仓日持仓，估值用当日收盘价；当日无行情退化为最近收盘价，再无则用最近成交价；
  - 行业取 `dim_stock.industry_l1`，缺失归入「其他」并由 `other_count` 计数；
  - 末尾固定附「现金」桶（`count=0`），`industries` 权重之和恒为 100（±0.1pp）。

`GET /api/v1/backtest/{bt_id}/export.csv` 依次输出（空行 + `#` 注释分隔）：

1. 净值：`date,nav,benchmark`
2. 月度收益（%）：`month,strategy,benchmark,excess`
3. 回撤（%）：`date,nav,peak,drawdown`
4. 成交（既有）：`date,code,name,side,price,shares,amount,fee,reason`

CSV 与界面同源（同一引擎、同一单位）；月度与回撤为百分数、保留 2 位小数，净值沿用落库精度。

`export.csv` 接受与 6.3 相同的 `code` 过滤（1–6 位数字，按 6 位补零精确匹配，C1）：
传 `code` 时成交 block 只导出该股票，并在表头后追加一行 `# filtered by code=000001` 说明范围；
净值/月度/回撤 block 始终覆盖整个回测，不随 `code` 收窄。前端「导出当前展示范围」必须把当前筛选代码一并传入。

### 6.5 参数敏感性矩阵（T10）

POST `/api/v1/backtest/{algo_code}/grid`，body：

```json
{ "start_date": "2025-11-01", "end_date": "2026-10-26",
  "rebalance_days": [20, 40], "max_positions": [10, 20],
  "strategy": { "limit": 20, "min_score": 6.0 } }
```

- 两轴各 1–10 个取值，去重升序；笛卡尔积上限 **24 格**，超出返回 422 `INVALID_PARAM`
  （`detail: {cells, max_cells: 24}`）。
- `start_date < end_date`，区间不超过 6 年；起始日早于该策略数据起点时自动裁剪，
  响应中的 `requested_start` / `effective_start` / `start_trimmed` 如实标注。
- 每个格子是一次独立回测（`backtest_run`，`status: queued → running → success|failed`），
  各自独立事务，单格失败不影响其余格；后台并发上限 2。
- **复用**：`(algo, start, end)` 相同且参数子集一致、状态为 success 的既有回测直接复用，
  不重新计算。复用只在 `backtest_grid_cell` 新增关联，不改写回测行；同一 `bt_id`
  可同时属于多个矩阵，旧 `grid_id` 始终可查。
- 立即返回（`elapsed_ms` 恒为 0），计算在后台进行：

```json
{ "grid_id": "…", "algo": "a3", "start_date": "2025-11-01", "end_date": "2026-10-26",
  "requested_start": "2025-11-01", "effective_start": "2025-11-01", "start_trimmed": false,
  "axes": { "rebalance_days": [20, 40], "max_positions": [10, 20] },
  "cells": [ { "bt_id": "…", "params": { "rebalance_days": 20, "max_positions": 10 },
               "rebalance_days": 20, "max_positions": 10, "status": "queued", "reused": false } ],
  "total": 4, "reused": 0, "queued": 4, "elapsed_ms": 0 }
```

GET `/api/v1/backtest/grid/{grid_id}`：

```json
{ "grid_id": "…", "algo": "a3", "start_date": "2025-11-01", "end_date": "2026-10-26",
  "axes": { "rebalance_days": [20, 40], "max_positions": [10, 20] },
  "cells": [ { "bt_id": "…", "params": { "rebalance_days": 20, "max_positions": 10 },
               "rebalance_days": 20, "max_positions": 10,
               "status": "success", "reused": false,
               "metrics": { "total_return": 0.1309, "excess_return": 0.249,
                            "max_drawdown": -0.1037, "sharpe": 0.87,
                            "win_rate": 0.369, "trade_count": 242 },
               "elapsed_ms": 9282, "error": null, "aligned": true } ],
  "progress": { "done": 4, "total": 4, "failed": 0 },
  "total_elapsed_ms": 122990, "created_at": "…" }
```

- `metrics` 为 6.1 指标的子集，单位仍为小数比率；未完成的格 `metrics=null`，失败格附 `error`。
- `aligned`：该格的起止日与交易日历来源与矩阵多数格一致才为 true；false 的格不参与热力图对比，
  但 `bt_id` 仍可直接查 6.2。
- 每个 `bt_id` 可独立 `GET /api/v1/backtest/{bt_id}`；未知 `grid_id` 返回 404 `NOT_FOUND`。
- 重发完全相同的矩阵会全部命中复用：`reused=total`、`queued=0`、`elapsed_ms=0`，
  且不新增 `backtest_run` 行。

POST `/api/v1/backtest/grid/{grid_id}/retry`：

- 只重置该矩阵中 `status=failed` 的格为 `queued`，清空旧 `metrics/equity_curve/trades/error/elapsed_ms`；
  `success` 与运行中的格不动，按每格保存的 `strategy` 与起止日快照在后台重跑（并发上限 2）。
- 无失败格时返回 `retried=0`，不提交、不启动后台任务；未知 `grid_id` 返回 404 `NOT_FOUND`。

```json
{ "grid_id": "…", "algo": "a3", "retried": 2, "queued": 2, "elapsed_ms": 0 }
```

### 6.6 纸面组合跟踪（T11）

**口径**：一策略一组合，默认按「检查点 + 60 个交易日重叠窗口」续算并覆盖窗口内 `paper_nav`
（幂等 upsert）；每周第一个同步日、以及增量续算失败回退时，对整个区间做一次全量校准。
检查点（现金/份额/成本/基准基数与参数快照）存于 `paper_portfolio.params.incremental`，
策略或回测参数变化时自动改走全量。佣金/印花税/滑点/整手、T+1 开盘成交、
停牌/涨跌停判定与回测完全一致；`start_date` = 策略首个成功**实盘** run 的交易日（不含回测 run），
因此只代表同口径跟踪，不代表可交易收益。响应统一返回 `caliber` 说明。

`start_date` 取自 `stg_run`，首个实盘 run 之前的日子不会补算；策略尚无实盘 run 时返回
409 `DATA_NOT_READY`。

GET `/api/v1/paper/portfolios`：

```json
{ "items": [ {
  "portfolio_id": "…", "algo_code": "b3", "name": "均线回踩",
  "status": "active", "start_date": "2026-09-10", "tracking_days": 27,
  "latest_trade_date": "2026-10-26", "rebalance_days": 20, "max_positions": 20,
  "initial_capital": 1000000.0,
  "nav": 0.993352, "benchmark_nav": 0.902197,
  "total_return": -0.006648, "benchmark_return": -0.097803, "excess_return": 0.091155,
  "max_drawdown": -0.007,
  "vs_backtest": { "bt_id": "…", "total_return": -0.006648, "excess_return": 0.091155,
                   "max_drawdown": -0.007, "total_return_diff": 0.0,
                   "excess_return_diff": 0.0, "max_drawdown_diff": 0.0, "as_of": "2026-10-26" },
  "freshness": { "latest_trade_date": "2026-10-26", "market_trade_date": "2026-10-26",
                 "lag_days": 0, "stale": false, "updated_at": "…", "ready": true }
} ], "total": 7 }
```

- `status`：`pending`（启用但尚未创建组合，此时 `portfolio_id=null`）/ `waiting`（建仓日尚无下一交易日）/
  `active` / `failed`。列表包含全部 `PAPER_ENABLED_ALGOS`（默认全部策略），未初始化的按 `pending` 占位。
- 收益/回撤/差值均为小数比率；`tracking_days` 为 `paper_nav` 实际行数。
- `freshness`：`latest_trade_date` 为最新净值日，`market_trade_date` 为行情最新交易日，
  `stale = latest_trade_date < market_trade_date`，`lag_days` 为自然日差；`ready=false` 表示尚无净值。
- `vs_backtest` 取**同区间且同参数**（调仓/持仓/费率/初始资金/排除规则/策略参数完全一致）
  最新一次成功人工回测（排除 `params.source="paper"`），差值为「纸面 − 回测」；
  无同参数人工回测时为 null（参数不同不能归因于执行差异，故不参与比较）。

GET `/api/v1/paper/portfolio/{algo_code}?days=250`：

```json
{ "portfolio_id": "…", "algo_code": "b3", "name": "均线回踩", "status": "active",
  "start_date": "2026-09-10", "tracking_days": 27,
  "params": { "rebalance_days": 20, "max_positions": 20, "initial_capital": 1000000.0,
              "strategy": { "…": "建仓时固化的策略参数快照" } },
  "caliber": "后复权估值；佣金/印花税/滑点/整手与回测引擎完全一致；…",
  "series": [ { "date": "2026-09-10", "nav": 1.0, "benchmark": 1.0, "drawdown": 0.0 } ],
  "holdings": [ { "code": "603221", "name": "爱丽家居", "shares": 1200,
                  "weight": 0.02, "cost": 17.4412, "price": 17.55, "pnl_pct": 0.0062 } ],
  "cash_ratio": 0.9584,
  "pending_orders": [ { "side": "sell", "code": "600000", "name": "浦发银行",
                        "reason": "T+1 开盘", "fillable": false,
                        "block_reason": "一字跌停" } ],
  "vs_backtest": { "…": "同列表结构；无同参数回测时为 null" },
  "summary": { "nav": 0.993352, "benchmark_nav": 0.902197, "total_return": -0.006648,
               "benchmark_return": -0.097803, "excess_return": 0.091155,
               "max_drawdown": -0.007 },
  "freshness": { "…": "同列表结构" } }
```

- `days` 为返回的最近净值点数（1–2000，默认 250），按日期升序；`tracking_days` 为全量行数。
- `holdings` 为最新一天收盘持仓，`weight` 为占总资产比例（0–1），`cost` 含手续费，`pnl_pct` 为浮盈比例。
- `pending_orders` 为**最新一天收盘后**的下一交易日计划，取回测快照（下一交易日确为调仓成交日时）
  或由最新信号派生；`fillable=true` 可成交、`false` 不可成交并给 `block_reason`（停牌/一字涨停/一字跌停/无开盘价）、
  `null` 表示 T+1 行情尚未产生待校验。已知卖不出的持仓仍占名额，不会再生成对应买入计划。
- 未创建的组合返回 404 `NOT_FOUND`；未知策略 404 `UNKNOWN_ALGO`；策略不在 `PAPER_ENABLED_ALGOS`
  返回 422 `INVALID_PARAM`。

POST `/api/v1/paper/portfolio/{algo_code}/sync` → `202` 语义（HTTP 200）立即返回：

```json
{ "status": "accepted", "algo": "b3", "mode": "background",
  "hint": "轮询 GET /api/v1/paper/portfolio/{algo_code} 查看结果" }
```

- 后台执行同步（默认检查点续算，周首个同步日或增量失败时全量校准），单组合 advisory lock，
  重复触发时后到者跳过；完成后覆盖窗口内 `paper_nav`，并清理结束日之后的多余行。
- 定时任务：`CronTrigger(hour=17, minute=30)`，位于 `algos`(16:30) 与 `signal_perf`(17:10) 之后；
  非交易日（`trade_calendar.is_open=false`）只记录 skip 日志、不写任何行；单组合失败标记 `failed` 且不影响其余。
- 同一交易日重复执行是幂等 upsert：`(portfolio_id, trade_date)` 主键，行数不变、无重复。

## 7. 前端路由

| 路由 | 页面 |
|---|---|
| `/` | 新闻首页（保留） |
| `/article/:id` | 新闻详情（保留） |
| `/dashboard` | 盘后分析（原 `/dashboard-v3`，旧新闻统计页删除） |
| `/compass` | 仓位罗盘（V9：今日决策 / 情景卡 / 复盘验证 / 规则与边界） |
| `/algo-center` | 选股中心（参数/run 元信息/CSV、纸面跟踪入口） |
| `/ladder` | 重定向到 `/algo-center#ladder`（策略天梯锚点） |
| `/paper` | 纸面组合跟踪（V6 T11） |
| `/stocks/:code` | 个股详情（新增） |
| `/jobs` | 任务监控（新增） |
| `/about` | 关于 |
| `*` | 404 |

## 8. 兼容性说明

- 旧 `/api/v1/analysis/*`（dashboard / run-analysis / sector / seed-demo）**已移除**，
  迁移到 `/api/v1/market/*` 与 `/api/v1/admin/*`。
- 旧 `/api/v1/stocks` 不存在，新增。
- 新闻相关接口**完全兼容**，前端新闻页无需改动字段。

## 9. 仓位罗盘（V9 追加，向后兼容）

前缀：`/api/v1/compass`。成功响应不包 `data` 壳；错误沿用 0.2；所有展示接口附带 `freshness`。
`freshness` 新增数据集：`etf_daily`、`etf_sector_map`（均为非全局关键，卡片服务自行判定）。

### 9.1 权限

- 内部 beta 默认 `professional`；请求头 `X-Compass-Tier: free` 返回免费视图。
- 免费视图：候选截断为 1 个、移除 `etf_code/etf_name`、情景卡仅保留 SC-01/SC-03，
  并返回 `locked_scenario_ids`。
- 生产环境必须替换为服务端订阅权益校验，禁止信任请求头。

### 9.2 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/compass/today?trade_date=` | 当日决策卡 + 仪表盘 + 候选池 + T+1 校验条；无卡返回 409 `DATA_NOT_READY` |
| GET | `/api/v1/compass/cards?limit=&offset=` | 历史卡片摘要；免费版最多 7 条 |
| GET | `/api/v1/compass/cards/{card_id}` | 卡片详情；不存在 404 |
| GET | `/api/v1/compass/scenarios?trade_date=` | 六张情景卡与当前状态、`action_priority` |
| POST | `/api/v1/compass/checks/{check_id}/action` | body `{"action":"DELAY|DROP|EXECUTED"}`，需 `Idempotency-Key`；过期 400 `CHECK_EXPIRED`，非 WARN 状态 400 `CHECK_NOT_ACTIONABLE` |
| GET | `/api/v1/compass/review?days=` | 回撤、错误次数、空仓避损、失效记录；样本 <60 日返回 `sample_insufficient=true` |
| GET | `/api/v1/compass/rules` | 规则、承诺、非目标、判据、规则版本；公开、不依赖行情 |
| GET | `/api/v1/compass/subscription` | 当前 tier 与 beta 标记 |
| POST | `/api/v1/compass/admin/generate?trade_date=` | 生成决策卡（`X-Admin-Key`） |
| POST | `/api/v1/compass/admin/check/{card_id}` | 执行 T+1 校验（`X-Admin-Key`） |
| POST | `/api/v1/compass/admin/push/{card_id}` | 企业微信推送（`X-Admin-Key`，幂等） |
| POST | `/api/v1/compass/admin/backfill-paper` | 聚合 active 纸面组合净值回填 `fact_paper_portfolio` |
| POST | `/api/v1/compass/admin/etf-sync?trade_date=&mode=auto\|seed\|live&limit=` | 同步 ETF 日行情并重建板块映射 |
| GET | `/api/v1/compass/admin/etf-coverage?trade_date=` | 映射覆盖率、候选池覆盖、缺失板块与数据源标注 |
| GET | `/api/v1/compass/admin/cards/{card_id}` | 管理端读取卡片详情（含未脱敏字段） |
| POST | `/api/v1/compass/admin/failure` | 追加失效记录（只追加） |
| POST | `/api/v1/compass/admin/etf-map` | 新增 ETF 主映射（主映射冲突返回 422） |

### 9.3 `GET /api/v1/compass/today` 核心字段

```json
{
  "card_id": "…", "trade_date": "2026-11-20",
  "rule_version": "compass-1.0.0", "signal_version": "sector-v5.1",
  "regime": { "state": "repair_up", "tier": "range", "label": "震荡" },
  "temperature": { "score": 56, "band": "平稳" },
  "budget": { "market_cap": 40, "exec_cap": 40, "per_sector_cap": 15 },
  "candidates": [ /* 含 composite/technical/sentiment/news/情绪分位/ETF/reasons/checks；
                     V9.1 追加 breadth_below_ma20 / breadth_below_count / breadth_valid_members */ ],
  "rejected": [ /* reason_code + reason_text，含 TOP_N_CUTOFF */ ],
  "empty_reasons": [ /* code + text，按优先级 */ ],
  "derivation": [ /* 5 步推导链，服务端生成 */ ],
  "check": { "status": "PASS|WARN|DELAYED|DROPPED|SUSPENDED|IDLE|PENDING", "title": "…", "text": "…" },
  "scenarios": [ /* 六张卡，含 condition/action/invalid/basis */ ],
  "idle_days": 0,
  "freshness": [ /* Freshness[] */ ]
}
```

### 9.4 口径

- 状态读取现有五态状态机，不重算；5→3 聚合：`trend_up`→进攻 80%，`repair_up`→震荡 40%，
  `bottoming/pull_down/risk_off`→防守 10%。
- 输出为空时 `exec_cap=0`，主视觉为“空仓”，不回退到市场层 10%。
- 单板块上限：`floor(min(20%, exec_cap×40%)/5%)×5%`。
- 候选输出最多 3 个，不补位；`rejected` 同时包含门槛失败与 `TOP_N_CUTOFF`。
- T+1 偏离判定用 `abs(gap) > 2.00%`，恰好 2.00% 不触发；10:00 未处理转 `EXPIRED`。

### 9.5 数据表

`dim_etf_sector_map`、`fact_etf_daily`、`fact_sector_sentiment_percentile`、
`fact_decision_card`、`fact_decision_check`、`fact_scenario_trigger`、
`fact_signal_failure`、`fact_paper_portfolio`、`compass_push_log`。
迁移：`alembic/versions/20261121_0018_compass.py`（幂等）。

### 9.6 纸面组合回填与 ETF 数据依赖（V9.1 追加）

**纸面组合回填**：`POST /api/v1/compass/admin/backfill-paper` 读取 `paper_portfolio` 中
`status=active` 且净值记录数 ≥60 的组合，按共同起始日归一为 100，等权聚合后
upsert `fact_paper_portfolio`。`review` 追加：

```json
{
  "source": "paper_portfolio",
  "violations_available": false,
  "label_rule": "纸面组合综合",
  "label_unconstrained": "组合基准",
  "method_note": "回填自 active 纸面组合净值，等权归一；违规次数待罗盘情景卡历史积累后启用"
}
```

**成分股广度**：`breadth_below_ma20` 口径为当日 SCD2 有效成分中
`close < MA20` 的比例；仅统计有有效收盘价、MA20>0 且未停牌的成分；
有效成分少于 5 只时返回 `null`（前端显示“样本不足”），不用 0 代替。
接入 SC-05：候选级 `breadth_below_ma20 > 0.60` 触发退出。

**ETF 映射依赖链**：

1. ETF 池：优先实时现货接口；失败回退 `SEED_ETF_UNIVERSE`（种子池）；
2. 历史行情：Sina `fund_etf_hist_sina`（OHLC/成交额）；
3. 规模：种子池或实时池提供；缺失按 0，G5 会拦截；
4. 溢价：当前数据源不提供，按 0 处理，覆盖率报告 `premium_source=unavailable_assumed_zero`；
5. 映射：按板块名/别名匹配，SCD2 写入 `dim_etf_sector_map`，同日重复同步幂等。

覆盖率报告字段：`total / mapped / coverage`（全行业）、
`pool_total / pool_mapped / pool_coverage`（当日综合分前 8 候选池，产品门槛看这一项）、
`etf_daily_covered`（有当日行情的已映射 ETF 数）、`missing`。当前真实库：
全行业 77/90（85.6%），候选池 8/8（100%），已映射 ETF 当日行情 31 只。

**响应示例（真实库值随日期变化）**

```json
// POST /api/v1/compass/admin/backfill-paper
{ "status": "ok", "rows": 581, "portfolios": 2,
  "start": "2024-06-28", "end": "2026-11-19" }

// POST /api/v1/compass/admin/etf-sync?mode=seed
{ "status": "ok", "trade_date": "2026-11-19", "mode": "seed",
  "sectors": 90, "mapped": 75, "coverage": 0.8333,
  "etf_fetched": 39, "etf_rows": 3120,
  "premium_source": "unavailable_assumed_zero",
  "aum_source": "seed_or_live_pool", "history_source": "sina" }

// GET /api/v1/compass/admin/etf-coverage
{ "status": "ok", "trade_date": "2026-11-19",
  "total": 90, "mapped": 77, "coverage": 0.8556,
  "pool_total": 8, "pool_mapped": 8, "pool_coverage": 1.0,
  "etf_daily_covered": 31,
  "missing": [ /* 13 个细分行业 */ ],
  "mappings": [ /* 77 条当前有效映射 */ ] }

// POST /api/v1/compass/checks/{check_id}/action
{ "check_id": "…", "final_status": "DELAYED",
  "action_at": "2026-11-23T09:41:12+08:00",
  "recheck_at": "2026-11-23T10:00:00+08:00",
  "idempotent_replay": false }

// GET /api/v1/compass/review
{ "sample_insufficient": false, "source": "paper_portfolio",
  "violations_available": false, "label_rule": "纸面组合综合",
  "label_unconstrained": "组合基准",
  "stats": { "max_drawdown_rule": -7.67, "max_drawdown_unconstrained": -21.41,
             "empty_days": 1, "empty_avoidance": 0.0, "missed_upside": 0.03 },
  "chart": { "dates": [], "drawdown_rule": [], "drawdown_unconstrained": [] },
  "failures": [] }
```

> 响应示例中的真实库数值仅用于说明字段结构，不构成收益承诺；实际值随交易日变化。
> `etf-sync` 的 `mapped` 是本次匹配数（当前 75），覆盖率报告包含仍有效的旧映射（当前 77），两者可能不同。

### 9.7 仓位罗盘错误码补充

| 场景 | HTTP | code | `detail.reason` |
|---|---|---|---|
| 决策卡尚未生成 | 409 | `DATA_NOT_READY` | `CARD_NOT_GENERATED` |
| 关键数据陈旧/质量不合格 | 409 | `DATA_QUALITY` | `STALE_DATASET` / `DATA_INCOMPLETE` |
| 卡片/校验记录不存在 | 404 | `NOT_FOUND` | `CARD_NOT_FOUND` / `CHECK_NOT_FOUND` |
| 日期或动作参数非法 | 422 | `INVALID_PARAM` | `INVALID_DATE` / `INVALID_ACTION` |
| T+1 操作已过 10:00 | 400 | `BAD_REQUEST` | `CHECK_EXPIRED` |
| 非 WARN 状态执行操作 | 400 | `BAD_REQUEST` | `CHECK_NOT_ACTIONABLE` |
| 管理密钥缺失/错误 | 403 | `BAD_REQUEST` | `ADMIN_AUTH` |
| ETF 主映射冲突 | 422 | `INVALID_PARAM` | `MULTIPLE_PRIMARY_ETF` |

---

## 10. V6–V9 追加接口索引

以下接口已在运行，字段细节以各模块实现与 `/docs` OpenAPI 为准；本表保证“代码有、契约有”。

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/api/v1/market/overview?trade_date=` | 市场状态 + 配置建议 + 一周温度计（V7） | 公开 |
| GET | `/api/v1/market/index-compare?trade_date=&window_days=` | 创业板50 / 红利低波50 风格轮动（V7） | 公开 |
| GET | `/api/v1/market/bull-bear` | 6 宽基牛熊位置 × 估值（V7） | 公开 |
| GET | `/api/v1/stocks/{code}/benchmark?benchmark=sh000300` | 个股与基准归一化序列（V7） | 公开 |
| GET | `/api/v1/algo/fields` | 条件树字段、算子与 AND/OR 逻辑 | 公开 |
| GET | `/api/v1/algo/health` | 本地选股引擎健康状态（不再依赖外部 CLI） | 公开 |
| POST | `/api/v1/algo/preview` | 条件树只读预览；不写 `stg_run`、不进入共识 | 公开（限频） |
| GET | `/api/v1/backtest/{algo_code}/oos?oos_id=` | 样本外 walk-forward 结果；缺省取最新一组 | 公开 |
| POST | `/api/v1/backtest/{algo_code}/oos` | 创建样本外任务（后台逐折执行） | 公开 |
| GET | `/api/v1/meta/provider-health?days=` | 数据源 × 任务成功率、降级次数与最近错误 | 公开 |
| POST | `/api/v1/admin/catch-up` | 手动检查并补跑最近缺失交易日 | `X-Admin-Key` |
| POST | `/api/v1/admin/daily-chain` | 手动推进「数据 → 仓位罗盘 → 推送」串行链路；`run_data=true` 时先跑采集 | `X-Admin-Key` |
| POST | `/api/v1/admin/backup` | 立即执行数据库备份，返回文件路径、大小、校验和与清理结果 | `X-Admin-Key` |
| POST | `/api/v1/crawl/auto` | 启动/配置自动新闻采集 | 公开 |
| GET | `/api/v1/crawl/auto/status` | 自动采集状态 | 公开 |
| POST | `/api/v1/crawl/trigger` | 手动触发新闻爬虫 | 公开 |
| POST | `/api/v1/crawl/process-summaries` | 处理待摘要新闻 | 公开 |

> 一致性校验：`cd backend && ..\.venv\Scripts\python.exe scripts\check_doc_routes.py`
> 会对比实际路由与本契约表格，缺失即非零退出。
