# 02 · 后端应用层（启动、配置、API）

对应代码：`backend/app/main.py`、`core/`、`api/`、`schemas/`。

---

## 1. 应用启动与生命周期

入口：[`backend/app/main.py`](../../backend/app/main.py)

```python
app = FastAPI(
    title="GFNA V5 · A 股行情与量化选股",
    version="5.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)
```

`lifespan` 做六件事（[main.py L58-L120](../../backend/app/main.py#L58-L120)）：

1. `sanitize_proxy_env()`：清洗 `NO_PROXY` 中的 `[::1]`，避免 httpx/openai 初始化报 `Invalid port`；
2. 连接数据库并记录版本；失败只记录错误，应用仍启动（接口返回 `DB_UNAVAILABLE`）；
3. `AUTO_CREATE_TABLES=1` 时执行 `Base.metadata.create_all` 兜底建表；
4. `recover_stale_tasks()`：把超时的 `ingest_job` / `stg_run` / `backtest_run` 标记为 failed；
5. `SCHEDULER_ENABLED=1` 时启动 APScheduler；
6. `SCHEDULER_ENABLED and CATCH_UP_ENABLED` 时后台执行 `run_catch_up_in_background()`。

关闭时：取消补数任务 → 停止调度器 → `dispose_engines()`。

**Windows 兼容**：必须在任何 asyncio 导入前设置 `WindowsSelectorEventLoopPolicy`（[main.py L29-L32](../../backend/app/main.py#L29-L32)），否则 Proactor 下会卡死。

### 1.1 中间件与异常处理

| 机制 | 位置 | 行为 |
|---|---|---|
| 请求 ID | `request_context` [L150-L156](../../backend/app/main.py#L150-L156) | 读取或生成 `X-Request-ID`，写入日志上下文与响应头 |
| CORS | [L137-L143](../../backend/app/main.py#L137-L143) | 允许 `CORS_ORIGINS`，默认 `*` |
| 业务异常 | `handle_app_error` [L163-L168](../../backend/app/main.py#L163-L168) | 统一转 `AppError.to_payload()` |
| 未处理异常 | `handle_unexpected` [L171-L186](../../backend/app/main.py#L171-L186) | 500 + `INTERNAL` + request_id |

### 1.2 健康检查

`GET /health`（[L206-L241](../../backend/app/main.py#L206-L241)）：

```json
{
  "status": "ok",
  "service": "GFNA",
  "version": "5.0.0",
  "database": "connected",
  "latest_trade_date": "2026-11-14",
  "data_ready": true,
  "detail": {"fresh_datasets": 12, "total_datasets": 21, "message": "..."}
}
```

`data_ready` 由 `services/freshness.summarize()` 判断关键数据集是否 ok。

---

## 2. 配置中心

文件：[`backend/app/core/config.py`](../../backend/app/core/config.py)。优先级：**环境变量 > `backend/.env` > 默认值**；根目录 `.env` 先加载，`backend/.env` 覆盖。

### 2.1 数据库与时区

| 变量 | 默认 | 说明 |
|---|---|---|
| `POSTGRES_HOST` | `localhost` | |
| `POSTGRES_PORT` | `5432` | Docker 内部为 `postgres:5432` |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DATABASE` | `postgres` / 空 / `gfna_db` | |
| `DB_POOL_SIZE` | `10` | 异步引擎连接池 |
| `DB_ECHO` | `0` | 打印 SQL |
| `HISTORY_START` | `2021-01-01` | 历史深度与分区起始年 |
| `SNAPSHOT_READY_HOUR/MINUTE` | `15:30` | 收盘后判定，早于该时刻采集层拒绝快照 |

### 2.2 数据源与限频

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATA_PRIMARY_PROVIDER` | `akshare` | 主源 |
| `DATA_FALLBACK_PROVIDER` | `baostock` | 兜底 |
| `SNAPSHOT_MIN_INTERVAL_SEC` | `60` | 全市场快照 ≤1 次/分钟 |
| `SNAPSHOT_FETCH_TIMEOUT_SEC` | `180` | 分页抓取超时 |
| `SECTOR_BOARD_TIMEOUT_SEC` | `60` | 单板块历史墙钟上限 |
| `HISTORY_REQUESTS_PER_SEC` | `2` | 历史回补限频 |
| `FETCH_TIMEOUT_SEC` / `FETCH_MAX_RETRIES` / `FETCH_BACKOFF_BASE_SEC` | `30` / `3` / `1.5` | 单次请求 |
| `HTTP_USER_AGENT` | 固定 Chrome UA + `GFNA/5.0` | 合规边界 |
| `INGEST_NETWORK_ENABLED` | `1` | 置 0 走离线 fixture |

### 2.3 质量阈值

`QUALITY_MIN_UNIVERSE=4000`、`QUALITY_MIN_SNAPSHOT_ROWS=3500`、`QUALITY_MIN_COVERAGE=0.95`、`QUALITY_MIN_VALUATION_COVERAGE=0.80`、`QUALITY_MAX_MISSING_RATIO=0.05`、`QUALITY_MAX_ABS_PCT_CHG=30`、`QUALITY_PRICE_JUMP_RATIO=0.5`。

### 2.4 评分与回测

- 板块综合权重：`WEIGHT_TECHNICAL=0.40`、`WEIGHT_SENTIMENT=0.35`、`WEIGHT_NEWS=0.25`；
- 情绪四子项：`SENT_WEIGHT_MARGIN/NORTH/NEWS/FLOW=0.25`；
- 中性分：`NEUTRAL_SCORE=5.0`；
- `BacktestConfig`：调仓 20 日、持仓 20、佣金万 1、印花税万 5、滑点 0.1%、最低佣金 5 元、初始资金 100 万。

### 2.5 策略默认参数

`STRATEGY_DEFAULTS` 是策略参数的唯一默认值来源（A3/A4/C1/C2/C3/C6 均在此登记）。注意：

- **A3 `min_score` 是 0–10 分制**，默认 6.0，不是 0–100；
- A4 默认 `pe_min=3`、`pe_max=15`、`div_min=4.5%`、`dv_max=15%`、`pb_max=2.5`、`min_score=6.5`、`min_amount=5e7`、`trend_dev=0.9`；
- C1 默认 `mom60_min=0`、`vol_cap=0.05`；C2 默认 `tight_max=1.12`、`shrink_max=0.5`、`min_amount=1e8`；
- C1 默认 `roc20_max=0.25`、`temp_max=70`，温度 ≥ 70 或 `risk_off` 不开新仓；C3 默认 `near_high_ratio=0.85`、`vol_cap=0.05`、`roc20_max=0.08`，并启用沪深300 双均线 + 连续站稳 + MA20 上行闸门；两者默认 8% 止损。

### 2.6 其他

| 变量 | 默认 | 说明 |
|---|---|---|
| `SCHEDULER_ENABLED` | `1` | CI/测试置 0 |
| `AUTO_CREATE_TABLES` | `1` | 启动兜底建表 |
| `CATCH_UP_ENABLED` / `CATCH_UP_MAX_DAYS` / `CATCH_UP_SCAN_DAYS` | `1` / `5` / `5` | 启动补缺 |
| `DAILY_CHAIN_MAX_DATA_RETRIES` | `1` | 数据阶段失败后的额外重采次数 |
| `ADMIN_API_KEY` | 空 | 空时管理接口仅本机可访问 |
| `CORS_ORIGINS` | `*` | |
| `PUSH_*` / `WECOM_WEBHOOK_URL` | 关闭 | 企业微信推送 |
| `OBSERVATION_ALGOS` / `PUSH_OBSERVATION` | 空 / `0` | 兼容旧配置；C1/C2/C3/C6 已启用，候选池为空 |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` / `LLM_ENABLED` | 空 / `deepseek-chat` / 有 key 即开 | AI 日报 |

---

## 3. 常量与业务规则

文件：[`backend/app/core/constants.py`](../../backend/app/core/constants.py)

| 主题 | 关键定义 |
|---|---|
| 股票池 | `POOL_BOARDS = (主板, 创业板, 科创板)`；北交所不采集 |
| 涨跌停 | 主板 10%、创业板/科创板 20%、北交所 30%；ST 主板 5% |
| 指数 | `INDEX_MAP`（上证/深证/创业板/科创50/沪深300/中证500/中证1000/美股） |
| 风格 | `STYLE_INDEX_MAP`：创业板50 `sz399673`、红利低波50 `H30269` |
| 宽基 | `BROAD_INDEX_MAP` 固定 6 个；含 2026-11-13 的 PE/回撤快照 |
| 市场状态 | 五态 `trend_up/repair_up/bottoming/pull_down/risk_off` + 阈值/迟滞参数 |
| 温度 | 7 子项权重与五档 `冰点/清冷/平稳/温热/沸腾` |
| 工具函数 | `exchange_of` / `board_of` / `limit_prices` / `is_pool_stock_code` / `is_st_name` |

> 不要在业务代码里散落 `code.startswith("688")`，统一走这些函数。

---

## 4. 统一错误模型

文件：[`backend/app/core/errors.py`](../../backend/app/core/errors.py)

```json
{
  "ok": false,
  "error": {
    "code": "DATA_NOT_READY",
    "message": "还没有市场状态数据",
    "detail": {"next_action": "运行每日管线 ..."},
    "suggestion": "..."
  },
  "request_id": "a1b2c3d4e5f6"
}
```

| 异常 | HTTP | code |
|---|---:|---|
| `NotFoundError` | 404 | `NOT_FOUND` |
| `DataUnavailableError` | 409 | `DATA_NOT_READY` |
| `QualityBlockedError` | 409 | `DATA_QUALITY` |
| `ProviderError` | 503 | `PROVIDER_UNAVAILABLE` |
| `UnknownAlgoError` | 404 | `UNKNOWN_ALGO` |
| `InvalidParamError` | 422 | `INVALID_PARAM` |
| 其他 | 500 | `INTERNAL` |

设计选择：**数据没来返回 409 而不是 200/空数组**，让前端能区分“接口坏了”和“数据未就绪”，并通过 `detail.next_action` 告诉用户该做什么。

---

## 5. 路由与鉴权

聚合入口：[`backend/app/api/v1/__init__.py`](../../backend/app/api/v1/__init__.py)

```python
api_router.include_router(articles.router)
api_router.include_router(stats.router)
api_router.include_router(market.router)
api_router.include_router(stocks.router)
api_router.include_router(algo.router)
api_router.include_router(backtest.router)
api_router.include_router(paper.router)
api_router.include_router(meta.router)
api_router.include_router(admin.router)
api_router.include_router(compass.router)
```

### 5.1 管理接口鉴权

[`backend/app/api/deps.py`](../../backend/app/api/deps.py)：

- 配置了 `ADMIN_API_KEY`：`X-Admin-Key` 必须严格匹配；
- 未配置：只允许 `127.0.0.1` / `::1` / `localhost` / `testclient`，防止公网裸奔。

### 5.2 完整端点清单

以下为实际导出的路由（`app.routes`），按模块分组。

#### 系统 / 新闻

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 服务信息 |
| GET | `/health` | 数据库 + 最新交易日 + 数据就绪 |
| GET | `/api/v1/articles` | 新闻分页列表 |
| GET | `/api/v1/articles/{article_id}` | 新闻详情 |
| GET | `/api/v1/search` | 新闻全文搜索 |
| GET | `/api/v1/sources` | 新闻来源列表 |
| POST | `/api/v1/crawl/trigger` | 触发 Scrapy 爬虫 |
| POST | `/api/v1/crawl/process-summaries` | 批量生成摘要 |
| POST | `/api/v1/crawl/auto` | 自动采集（后台） |
| GET | `/api/v1/crawl/auto/status` | 自动采集状态 |
| GET | `/api/v1/stats` | 新闻统计总览 |
| GET | `/api/v1/stats/daily-trend` | 每日新增趋势 |
| GET | `/api/v1/stats/source-distribution` | 来源分布 |
| GET | `/api/v1/stats/keywords` | 关键词 |
| GET | `/api/v1/stats/processing-status` | 处理状态 |

#### 行情

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/market/dashboard` | 盘后分析总览 |
| GET | `/api/v1/market/indices` | 指数快照；`days>1` 返回序列 |
| GET | `/api/v1/market/sectors` | 板块列表（日线 + 评分 + 聚合） |
| GET | `/api/v1/market/sector-scores/sparkline` | 板块评分序列 |
| GET | `/api/v1/market/temperature` | 市场温度计（可回看） |
| GET | `/api/v1/market/overview` | 市场状态 + 配置建议 + 一周温度 |
| GET | `/api/v1/market/index-compare` | 创业板50 / 红利低波50 风格轮动 |
| GET | `/api/v1/market/bull-bear` | 6 宽基牛熊位置 × 估值 |
| GET | `/api/v1/market/sector/{sector_code}` | 板块详情（三面 + 龙头 + 成分） |

#### 个股

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/stocks/search` | 代码/名称模糊搜索，按股票池过滤 |
| GET | `/api/v1/stocks/sparkline/batch` | 批量最近 N 日收盘序列 |
| GET | `/api/v1/stocks/{code}` | 快照 + 指标 + 因子 + 所属板块 |
| GET | `/api/v1/stocks/{code}/kline` | K 线（含 MA/BOLL，复权口径可选） |
| GET | `/api/v1/stocks/{code}/benchmark` | 个股 vs 基准归一化序列 |

#### 选股

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/algo/list` | 策略清单；`include_observation=true` 含候选 |
| GET | `/api/v1/algo/health` | 引擎自检 |
| GET | `/api/v1/algo/ladder` | 策略天梯（5/10/20 日前瞻收益） |
| GET | `/api/v1/algo/consensus` | 多策略共识榜 |
| GET | `/api/v1/algo/fields` | 条件树字段/算子/逻辑清单 |
| POST | `/api/v1/algo/preview` | 条件树只读预览（20s 超时、10 次/分钟） |
| GET | `/api/v1/algo/{algo_code}` | 执行/读取策略结果 |
| POST | `/api/v1/algo/{algo_code}/refresh` | 忽略缓存重跑 |
| GET | `/api/v1/algo/{algo_code}/runs` | 历史 run 列表 |
| GET | `/api/v1/algo/runs/{run_id}` | 单次 run 明细 |
| GET | `/api/v1/algo/{algo_code}/export.csv` | 结果 CSV |

#### 回测

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/backtest/{algo_code}` | 创建回测（后台执行） |
| GET | `/api/v1/backtest/{bt_id}` | 状态/结果 |
| GET | `/api/v1/backtest/{bt_id}/trades` | 成交明细（可按 code 过滤） |
| GET | `/api/v1/backtest/{bt_id}/export.csv` | 净值 CSV |
| POST | `/api/v1/backtest/{algo_code}/grid` | 参数敏感性矩阵 |
| GET | `/api/v1/backtest/grid/{grid_id}` | 矩阵状态与每格指标 |
| POST | `/api/v1/backtest/grid/{grid_id}/retry` | 重试失败格 |
| POST | `/api/v1/backtest/{algo_code}/oos` | 创建样本外 walk-forward（后台逐折执行） |
| GET | `/api/v1/backtest/{algo_code}/oos?oos_id=` | 读取样本外结果（缺省最新一组） |

#### 纸面 / 元信息 / 管理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/paper/portfolios` | 组合列表 |
| GET | `/api/v1/paper/portfolio/{algo_code}` | 净值/持仓/待执行 |
| POST | `/api/v1/paper/portfolio/{algo_code}/sync` | 手动重算（后台） |
| GET | `/api/v1/meta/freshness` | 所有数据集新鲜度 |
| GET | `/api/v1/meta/provider-health` | 数据源 × 任务成功率/降级次数（days=1–30） |
| GET | `/api/v1/meta/trade-calendar` | 交易日历 |
| GET | `/api/v1/meta/jobs` | 任务监控（`status=archived` 可审计） |
| GET | `/api/v1/meta/partitions` | `fact_stock_daily` 分区概览 |
| POST | `/api/v1/admin/ingest/{task}` | 手动触发任务（需管理员） |
| POST | `/api/v1/admin/catch-up` | 检查并补跑缺失交易日 |
| POST | `/api/v1/admin/daily-chain` | 推进盘后串行链路（数据 → 罗盘 → 推送） |
| POST | `/api/v1/admin/backup` | 立即执行数据库备份 |
| POST | `/api/v1/admin/seed-demo` | 演示数据（仅 `ENV=demo`） |

#### 仓位罗盘（V9）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/compass/today` | 当日决策卡 + 预算 + 候选 + T+1 校验 |
| GET | `/api/v1/compass/cards` | 历史卡片摘要 |
| GET | `/api/v1/compass/cards/{card_id}` | 卡片详情（按 tier 脱敏） |
| GET | `/api/v1/compass/scenarios` | 六张情景卡与触发状态 |
| POST | `/api/v1/compass/checks/{check_id}/action` | T+1 延迟/放弃/已执行（幂等） |
| GET | `/api/v1/compass/review` | 纸面组合复盘（回撤/避损/失效记录） |
| GET | `/api/v1/compass/rules` | 规则、承诺、边界（公开） |
| GET | `/api/v1/compass/subscription` | 当前 tier（beta） |
| GET | `/api/v1/compass/admin/cards/{card_id}` | 管理端卡片详情 |
| POST | `/api/v1/compass/admin/generate` | 生成决策卡（管理员） |
| POST | `/api/v1/compass/admin/check/{card_id}` | 执行 T+1 校验（管理员） |
| POST | `/api/v1/compass/admin/push/{card_id}` | 企业微信推送（管理员） |
| POST | `/api/v1/compass/admin/backfill-paper` | 纸面组合聚合回填（管理员） |
| POST | `/api/v1/compass/admin/etf-sync` | ETF 同步与板块映射（管理员） |
| GET | `/api/v1/compass/admin/etf-coverage` | 映射覆盖率与缺失板块（管理员） |
| POST | `/api/v1/compass/admin/failure` | 追加失效记录（管理员） |
| POST | `/api/v1/compass/admin/etf-map` | 新增 ETF 主映射（管理员） |

---

## 6. 分页与响应约定

- 成功响应保持**裸业务对象**，不包 `data`；错误统一走错误体；
- 列表接口尽量追加 `total / limit / offset / has_more / page / page_size`（`core/pagination.py`）；
- 截断数组同时给 `*_total_matching`（如 `heatmap_total_matching`、`sectors_total_matching`），避免前端误以为全量；
- 数据未就绪返回 409 + `next_action`；空库看板返回 200 + `empty_reason`。

---

## 7. 各 API 文件职责

| 文件 | 行数 | 核心函数 | 说明 |
|---|---:|---|---|
| `market.py` | 974 | `get_dashboard`、`get_market_overview`、`get_index_compare`、`get_bull_bear_valuation`、`get_sector_detail` | 行情/板块/市场状态；大量私有查询函数 `_query_*` |
| `algo.py` | 386 | `get_algo_list`、`get_algo_ladder`、`get_algo_consensus`、`get_algo_result`、`refresh_algo`、`export_algo_csv` | 选股中心 |
| `backtest.py` | 738 | `create_backtest`、`create_backtest_grid`、`get_backtest`、`get_backtest_trades`、`export_backtest_csv`、OOS | 回测、矩阵与样本外 |
| `stocks.py` | 377 | `search_stocks`、`get_sparkline_batch`、`get_stock_detail`、`get_kline`、`get_benchmark_series` | 个股 |
| `paper.py` | 73 | `get_paper_portfolios`、`get_paper_portfolio`、`sync_paper_portfolio_api` | 纸面组合 |
| `meta.py` | 149 | `get_freshness`、`get_trade_calendar`、`list_jobs`、`get_partitions`、`get_provider_health` | 元信息 |
| `admin.py` | 100 | `trigger_ingest`、`trigger_catch_up`、`seed_demo` | 管理 |
| `articles.py` | 382 | `list_articles`、`get_article_detail`、`search_articles`、`get_stats`、爬虫触发 | 新闻 |
| `stats.py` | 207 | `get_daily_trend`、`get_source_distribution`、`get_keywords`、`get_processing_status` | 新闻统计 |
| `compass.py` | 275 | `today`、`cards`、`card_detail`、`scenarios`、`check_action`、`review`、`rules`、`subscription`、`admin_*` | 仓位罗盘 |

---

## 8. 扩展一个 API 的步骤

1. 在 `app/api/v1/<module>.py` 新建 `router`（已有模块则追加）；
2. 若新模块，在 `api/v1/__init__.py` `include_router`；
3. 参数校验用 `InvalidParamError`，资源不存在用 `NotFoundError`，数据未就绪用 `DataUnavailableError`；
4. 只读查询直接走 `Depends(get_db)`；需要落库时明确事务所有权；
5. 返回体遵循裸对象 + 分页元信息 + `freshness`（如涉及数据卡片）；
6. 在 `docs/V5-API-CONTRACT.md` 或本 Wiki 的端点清单补一行。

> `api/` 不做计算。若需要新指标/聚合，放在 `engine/`，API 只调用并组装。
