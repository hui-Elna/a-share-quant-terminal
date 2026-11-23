# 04 · 采集与数据质量

对应代码：`backend/app/ingest/`、`backend/app/quality/`、`backend/app/catch_up.py`。

目标：把外部公开数据变成 **可校验、可重跑、可断点续传、幂等** 的本地事实表。本层是唯一允许访问外部数据源的地方。

---

## 1. 总体结构

```
pipeline.run_task(task)
   └─► ingest/service.py（唯一对外入口）
          ├─► ProviderChain（akshare → baostock）
          ├─► 专项通道（腾讯 / 新浪 / 东财 / 同花顺 / 中债 / 中证 / 巨潮）
          ├─► quality.py（落库前体检 → meta_data_quality）
          ├─► calendar.py（真实交易日历）
          ├─► bulk_backfill.py（历史批量回补 + 断点续传）
          └─► upsert_rows（INSERT ... ON CONFLICT DO UPDATE）
```

核心保证（`service.py` 文件头 L20-L36）：

1. **幂等 upsert**：重跑不产生重复行；写日线前确保分区存在；
2. **DB 分布式锁**：`pg_try_advisory_lock(hashtext(task))`，拿不到返回 `skipped=lock_busy`；
3. **限频**：快照 ≤1 次/分钟，历史 ≤2 请求/秒；
4. **非交易日短路**：节假日不跑空、不写脏数据；
5. **失败不清空已有数据**：只写 `error` / `failed`，绝不 DELETE；
6. **断点续传**：已完成代码写入 `ingest_job.checkpoint`；
7. **单位统一**：金额元、成交量股、比率百分数；
8. **任务可观测**：每步写 `ingest_job`（total/done/failed/rows/elapsed）。

事务约定：`db=None` 自建会话并提交；传入 `db` 时只执行语句，事务归调用方。

---

## 2. Provider 协议与降级链

文件：[`ingest/base.py`](../../backend/app/ingest/base.py)

### 2.1 能力协议

`Provider`（L410-L506）把能力拆成 9 个方法，便于**按能力降级**：

| 方案编号 | 方法 |
|---|---|
| A1 股票池 | `fetch_stock_universe` |
| A2 全市场快照 | `fetch_daily_snapshot` |
| A3 历史日线 | `fetch_stock_history` |
| A4 板块 | `fetch_sector_list` / `fetch_sector_members` |
| A5 资金与两融 | `fetch_moneyflow` / `fetch_margin` / `fetch_bond_yields` / `fetch_margin_buy_*` |
| A6 指数 | `fetch_index_daily` / `fetch_style_indices` / `fetch_broad_indices` |
| A7 交易日历 | `fetch_trade_calendar` |

`BaseProvider`（L509）所有方法默认抛 `NotImplementedError`，兜底源只需覆盖自己能提供的方法。

### 2.2 `ProviderChain` 语义

[`base.py` L574-L810](../../backend/app/ingest/base.py#L574-L810)：

1. `available()=False` → `unavailable`，跳过；
2. `NotImplementedError` → `not_implemented`，跳过（不算失败）；
3. 其他异常 → 指数退避重试，仍失败则 `failed`，继续下一个源；
4. 返回 `rows>0` → 立即返回（部分失败也算可用，交给质量校验）；
5. 返回 `rows==0` 且无失败 → `empty`，继续兜底；全部为空返回空结果（不抛异常）；
6. 全部失败 → 抛 `ProviderError`，`detail.attempts` 带完整降级记录。

`chain.last_attempts` 供任务监控展示“这次数据来自哪个源”。

### 2.3 限频与重试

- `RateLimiter`（L153-L220）：`SNAPSHOT_LIMITER`（60s/次）、`HISTORY_LIMITER`（2 请求/秒）；
- `retry_async` / `async_retry`（L247）：指数退避，默认 3 次、基数 1.5s；
- `FetchResult`（L75-L136）：统一返回体，含 `rows/written/failed/elapsed_ms/detail/quality_status`。

---

## 3. 数据源文件职责

| 文件 | 体量 | 职责 | 主要写入表 |
|---|---:|---|---|
| `service.py` | 136 KB | **采集编排唯一入口**：股票池/快照/历史/板块/资金流/两融/宏观/指数 | 几乎所有事实表 |
| `akshare_provider.py` | 66 KB | AKShare 主源：日历、快照、历史、股票池、板块、资金流、两融、中债、指数 | 由 service 落库 |
| `akshare_normalize.py` | 41 KB | AKShare 原始返回的列名兼容、单位换算与标准化（V9 从 provider 拆出） | 由 provider 调用 |
| `baostock_provider.py` | 14 KB | 兜底源：历史日线/日历/指数（其余能力不实现） | 由 service 落库 |
| `sina_provider.py` | 9 KB | 新浪概念板块成分兜底 | `dim_sector_member` |
| `tencent_history.py` | 12 KB | 腾讯 `fqkline` 批量历史 + 新浪 K 线/qfq 因子兜底；WAF 熔断 | `fact_stock_daily` |
| `sina_moneyflow.py` | 4 KB | 新浪个股资金流（约最近 300 日） | `fact_moneyflow` |
| `ths_sector.py` | 44 KB | 同花顺板块资金流/板块日线兜底 | `fact_sector_daily` |
| `ths_board.py` | 15 KB | 同花顺板块清单与成分 | `dim_sector_member` |
| `em_board.py` | 8 KB | 东财板块清单/成分直连 | `dim_sector_member` |
| `em_valuation.py` | 4 KB | 东财 datacenter 历史 PE/PB/PS/市值 | `fact_stock_daily` 估值列 |
| `em_dividends.py` | 4 KB | 巨潮分红明细 | `dim_stock_dividend` |
| `em_tls_patch.py` | 3 KB | 东财 TLS/cffi 兼容补丁 | — |
| `calendar.py` | 13 KB | 交易日历同步与查询（含 `latest/prev/next`） | `trade_calendar` |
| `bulk_backfill.py` | 30 KB | 腾讯日线/新浪资金流/东财估值批量回补 + 断点续传 | 多表 |
| `valuation.py` | 63 KB | 财报/分红抓取与同步编排 + point-in-time 估值入口（V9 门面） | `dim_stock_financial` / `dim_stock_dividend` / `fact_stock_daily` |
| `valuation_math.py` | 45 KB | 财报/分红标准化、EPS/营收 TTM、PE/PB/PS/股息率数学（V9 从 valuation 拆出） | 由 valuation 调用 |
| `valuation_store.py` | 28 KB | 日期/DB/任务/质量写入辅助（V9 从 valuation 拆出） | `dim_stock_financial` / `dim_stock_dividend` / `meta_data_quality` |
| `quality.py` | 24 KB | 落库前质量校验 + 写 `meta_data_quality` | `meta_data_quality` |

---

## 4. `service.py` 主要任务

| 任务名 | 函数 | 行号 | 说明 |
|---|---|---:|---|
| `universe` | `sync_stock_universe` | L1041 | 全市场标的（含退市/ST/行业/股本/所有制） |
| `snapshot` | `collect_daily_snapshot` | L1202 | 当日全市场快照，质量校验后 upsert |
| `backfill` | `backfill_stock_history` | L1328 | Provider 链历史回补，断点续传 |
| `sector_snapshot` | `collect_sector_snapshot` | `sector_snapshot.py` | 板块即时快照（仅当日、只写目标池、不覆盖 OHLCV） |
| `sector` | `collect_sector_daily` | L1620 | 板块日线（目标池；缺失用快照补） |
| `sector_members` | `collect_sector_members` | L1758 | 目标池成分 SCD2；每周；熔断/预算/覆盖率保护 |
| `moneyflow` | `collect_moneyflow` | L2232 | 全市场个股资金流；每周；东财/同花顺 + 新浪补缺 |
| `margin` | `collect_margin` | L2406 | 两融余额 + 融资买入额 + 北向 |
| `macro_rates` | `collect_macro_rates` | L2522 | 中债 1Y/10Y |
| `style_indices` | `collect_style_indices` | L2634 | 创业板50 + 红利低波50 |
| `broad_indices` | `collect_broad_indices` | L2756 | 6 大宽基 |
| `index` | `collect_indices` | L2842 | A 股指数 + 可选美股 |

辅助函数：

- `build_provider_chain` / `get_provider_chain`（L139-L170）：按配置构建 `akshare → baostock`；
- `_advisory_lock`（L344）：任务级 DB 锁；
- `_JobTracker`（L381）：`ingest_job` 写入封装；
- `_load_resume_checkpoint`（L577）：断点恢复；
- `_ensure_partition_for`（L704）：写入前建分区；
- `_filter_pool_frame`（L753）：股票池过滤（主板/创业板/科创板）。

### 4.1 快照与日线

- 快照要求 15:30 之后（`_after_close_ready` L207）；未到点返回 `skipped=before_close_ready_time`；
- 快照质量校验失败会阻断本次写入（`QualityBlockedError`），但保留旧数据；
- 历史日线字段包含 `adj_factor`（后复权收盘/不复权收盘），由 Provider 归一化。

### 4.2 板块 SCD2

`collect_sector_members` 的关键保护：

- 单板块失败连续 5 次或总预算 600s 触发熔断，跳过并保留既有区间；
- 当日板块清单覆盖已有成分板块 <98% 时**不关闭区间**（防止名单不完整导致误删）；
- 覆盖 90%–98% 只告警，<90% 标记质量不合格；
- `is_partial=true` 的名单不参与后续聚合。

### 4.3 资金流

- 优先东财；东财不可用时同花顺 `fetch_net_inflow_map_sync`，来源标记 `ths`；
- 新浪 `sina_moneyflow` 用于历史回补（约 300 日）；
- 资金流缺失不阻断评分，只影响情绪因子/展示。

### 4.4 两融

- 沪深分开抓取，含融资买入额；
- T+1 披露：补跑时排在评分/日报之后，避免未来函数；
- 北向自 2024-08 起暂停披露，缺失不阻断。

---

## 5. 批量回补（`bulk_backfill.py`）

| 函数 | 行号 | 说明 |
|---|---:|---|
| `backfill_prices` | L376 | 腾讯并发日线回补（全市场、可断点） |
| `backfill_moneyflow` | L541 | 新浪资金流历史 |
| `backfill_valuation` | L711 | 东财估值历史（单只一次请求） |
| `last_completed_trade_date` | L171 | 最近完整交易日 |
| `_load_state` / `_save_state` | L146 / L157 | 文件级断点状态（`backend/var/`） |
| `_retry_failed` | L95 | 失败代码重试 |

腾讯通道：

- 单次约 1023 个交易日；更早用 `backfill` 分段；
- 连续失败触发 15 分钟熔断，自动改走新浪 K 线；
- WAF/IP 级限制属于预期，不需要手工切换。

---

## 5.1 数据源健康度（`provider_health.py`）

- `ProviderChain.call` 已产出 `attempts`（source/status/elapsed_ms/error）；`service._RecordingChain` 在每个 `fetch_*` 后把 attempts 写入 `meta_provider_health`；
- 每 `(trade_date, provider, task)` 每天最多 100 行；错误过滤 URL/token/Webhook；
- `GET /api/v1/meta/provider-health?days=7` 按 provider × task 聚合尝试数、成功率、降级次数与最近错误；
- 前端在任务监控页「数据源健康」区块与看板“数据源正常/降级中”徽标展示。

---

## 6. 估值链路（`valuation.py` + `valuation_math.py` + `valuation_store.py`）

三种口径并存：

1. **`valuation_fast`（东财）**：`em_valuation.fetch_valuation` 直接取历史 PE_TTM/PB/PS/市值；
2. **`valuation`（本地推算兜底）**：
   - `valuation.py` 的 `sync_financials` → `dim_stock_financial`；
   - `valuation.py` 的 `sync_dividends` → `dim_stock_dividend`；
   - `valuation_math.py` 的 `build_valuation_frame`：`EPS_TTM = 本期累计 + 上年年报 − 上年同期`；`PE = close / EPS_TTM`；`PB = close / BPS`；`股息率 = 近 12 月 dps / close`；
   - 用 `announced_date = 报告期 + 90 天` 做 point-in-time，`merge_asof` 只取可知的最新报告期；
3. **`valuation.py` 的 `collect_latest_valuation`**：只更新最新交易日的估值列。

关键原则：

- 每日估值计算**零联网**（财报/分红缓存已进库）；
- 缺失估值留空，不外推；
- 只 UPDATE 估值列，不碰价格/成交量。

---

## 7. 数据质量

### 7.1 校验函数

文件：[`ingest/quality.py`](../../backend/app/ingest/quality.py)

| 函数 | 行号 | 校验对象 |
|---|---:|---|
| `check_snapshot` | L206 | 全市场快照：有效行数、覆盖率、关键列缺失、涨跌停异常、价格跳变 |
| `check_daily_frame` | L376 | 单只/区间日线：必需列、OHLC 关系、成交量/额、重复行 |
| `check_universe` | L528 | 股票池：数量下限、代码合法性、重复 |
| `record_quality` | L598 | 写 `meta_data_quality` |

`QualityReport`（L63）区分 `error` / `warning`：error 阻断写入，warning 记入 `anomalies`。

### 7.2 跨表不变量

文件：[`quality/invariants.py`](../../backend/app/quality/invariants.py)，在日更末尾 `log_invariants(logger)`：

- 日线行数/覆盖率；
- 指标/评分覆盖；
- 板块成分 SCD2 重叠/缺口；
- 策略 run 与 signal 一致性；
- 回测/纸面净值单调性等。

不变量只记录日志/指标，不阻断流水线。

### 7.3 新鲜度

`meta_data_quality` 是唯一来源；`services/freshness.py` 提供：

- `write_freshness`（L51）：success/attempt/failure 三种写入语义；
- `classify`（L126）：根据最后成功时间与过期阈值判断 ok/stale；
- `get_freshness_items`（L156）：批量读取；
- `summarize`（L167）：`/health` 的 `data_ready`。

前端 `FreshnessBadge` 直接消费该接口。

---

## 8. 交易日历

文件：[`ingest/calendar.py`](../../backend/app/ingest/calendar.py)

| 函数 | 行号 | 说明 |
|---|---:|---|
| `sync_trade_calendar` | L104 | 同步区间（akshare → baostock） |
| `get_trade_dates` | L221 | 区间开市日 |
| `is_trade_day` | L284 | 单日判断 |
| `latest_trade_date` | L297 | 最近开市日 |
| `prev_trade_date` / `next_trade_date` | L312 / L317 | 前后开市日 |
| `ensure_calendar_range` | L332 | 缺失区间自动同步 |

休市日也写入 `is_open=false`，用于区分“休市”和“没数据”。

---

## 9. 启动补缺（`catch_up.py`）

问题：宿主机休眠/进程未运行会错过 15:36 的 APScheduler 任务，启动后不会自动补跑。

策略（[`catch_up.py` L1-L60](../../backend/app/catch_up.py#L1-L60)）：

1. 对比“已入库最新交易日”和“最近完整交易日”；
2. 历史缺口日：先用 `history_fast / valuation_fast / moneyflow_history` 覆盖整个缺口区间，再逐日重算：
   `index → broad_indices → style_indices → sector → sector_agg → valuation → features → scores → macro_rates → market_factors → sector_scores → temperature → regime → report → algos → margin`；
3. 缺口日就是今天且已收盘：直接走完整 `run_daily_pipeline`；
4. 全程持有 `gfna:daily_pipeline` advisory lock，拿不到锁直接跳过；
5. 所有写入幂等，补跑可重复执行。

---

## 10. 网络现实与降级

README 记录的实测结论（2026-11）：

| 现象 | 系统行为 |
|---|---|
| 东财 `push2*` 被重置 | 日线走腾讯/新浪；板块走同花顺；估值走 datacenter |
| 腾讯批量 WAF | 连续失败冷却 15 分钟，自动改新浪 |
| 中证/两融 T+1 | 页面按共同最新交易日计算，不补造日期 |
| 北向暂停披露 | 缺失不阻断评分 |
| 新闻源不稳定 | 采集失败只记日志，不影响主链 |

合规边界：固定 UA、限频、不实现反爬绕过/验证码/代理池；数据仅个人研究。

---

## 11. 扩展一个数据源

1. 继承 `BaseProvider`，实现需要的 `fetch_*` 方法，返回 `FetchResult`；
2. 在 `service._PROVIDER_REGISTRY` 注册（或专项函数直接调用）；
3. 归一化到统一单位与列名（金额元、成交量股、比率百分数）；
4. 在 `quality.py` 增加对应校验（如新数据集）；
5. 在 `pipeline.TASK_DATASET` 登记 dataset，保证新鲜度可观测；
6. 补 `tests/test_ingest.py` 或专项测试（离线 fixture，不联网）。

---

## 12. V10 数据采集瘦身（2026-11）

- **目标池**：`ingest/sector_pool.py` 解析同花顺行业 + 主题白名单，按 ETF 映射、20 日均额、规模、成分数过滤；`actionable_codes` 是每日采集的唯一范围。
- **`sector_snapshot`**：同花顺即时资金流两次调用，写入 `fact_sector_daily` 快照字段；专用 upsert，不覆盖 OHLCV；仅当日执行。
- **`sector`**：`collect_sector_daily` 只抓目标池，支持 `codes` 定向回补与 90s 软截止。
- **频率**：`sector_members` 改为每周；全市场 `moneyflow` 改为每周；每日资金流由板块快照提供。
- **估值**：`collect_latest_valuation` 在同日价格/财报/分红未变时返回 `skipped=already_collected`；`force=True` 可强制重算。
- **估值单日快速路径**：批量取数 + 向量化计算 + 一次批量 UPDATE；实测 5216 只约 2.76s，与慢路径逐字段零差异。
- 配置见 `core/config.py` 的 V10 段；验收见 `docs/GFNA_V10_数据采集瘦身任务书.md`。
