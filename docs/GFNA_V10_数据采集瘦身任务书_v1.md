# GFNA V10 数据采集瘦身任务书

> 日期：2026-11-23
> 状态：待执行
> 范围：**仅数据采集链路（ingest）**。
> 目标：日更 P95 < 5 分钟；单任务 < 2 分钟；估值重算 < 30 秒；板块池 = 同花顺行业约 90 个 + 主题白名单 10–20 个。

---

## 0. 一句话

保留每日全市场个股快照；新增板块快照；板块历史只抓目标池；板块成分与全市场个股资金流改为每周；估值改为“新鲜则跳过 + 单次向量化/SQL 重算”；把日更采集从约 43 分钟压到 5 分钟内。

---

## 1. 数据采集耗时

### 1.1 口径

- “单次日更”= 一个交易日的采集链路自身耗时，不含采集后的下游计算任务。
- 任务总耗时来自本机 `ingest_job` 实测；拉取/清洗/处理/存储为按代码路径拆分估算，误差约 ±20%。
- “拉取”= 网络请求与分页；“清洗”= 字段映射、单位换算、过滤；“处理”= 质量校验、去重、聚合准备、本地计算；“存储”= upsert 与 commit。
- “估值”= 财报/分红缓存的新鲜度检查 + 本地 PE/PB/PS/股息率重算；网络阶段通常已缓存跳过，耗时主要在本地计算。

### 1.2 当前日更耗时

| 任务 | 拉取 | 清洗 | 处理/计算 | 存储 | 合计 |
|---|---:|---:|---:|---:|---:|
| `daily_snapshot`（全市场 5221 只） | 26s | 4s | 2s | 2s | **34s** |
| `sector_daily`（约 450 个板块） | 270s | 15s | 8s | 7s | **300s** |
| `sector_members`（479 个板块） | 520s | 25s | 8s | 9s | **562s** |
| `moneyflow`（全市场逐只） | 1240s | 60s | 20s | 18s | **1338s** |
| `sync_universe` | 48s | 6s | 2s | 4s | **60s** |
| 指数/宽基/风格/两融/国债 | 25s | 5s | 2s | 3s | **35s** |
| ETF 同步与映射（估算） | 35s | 4s | 2s | 4s | **45s** |
| `valuation`（本地重算） | 0.5s | 0.2s | 181.5s | 1s | **约 183s** |
| **合计** | **2164.5s** | **119.2s** | **225.5s** | **48s** | **约 43 分钟** |

时间分配：

| 环节 | 耗时 | 占比 |
|---|---:|---:|
| 数据拉取 | 约 36 分钟 | 84% |
| 清洗/归一 | 约 2 分钟 | 5% |
| 处理/本地计算 | 约 3.8 分钟 | 9% |
| 存储 | 约 0.8 分钟 | 2% |

其中 `valuation` 的 183s 里，网络阶段仅约 0.5s（`sync_financials` 0.28s、`sync_dividends` 0.12s、`sync_revenue` 0.12s），**`compute_valuation` 占 181.5s**。如果 `catch_up` 或重启触发第二次整链路，估值会再跑约 3 分钟。

最坏情况：`sector_daily` 出现过 2.9 小时 / 14.7 小时的失败记录；`moneyflow` 单次约 22 分钟。当前日更不具备稳定的 5 分钟上限。

### 1.3 瘦身后日更耗时

| 任务 | 拉取 | 清洗 | 处理/计算 | 存储 | 合计 |
|---|---:|---:|---:|---:|---:|
| `daily_snapshot` | 26s | 4s | 2s | 2s | **34s** |
| `sector_snapshot`（新增，2 次调用） | 12s | 2s | 1s | 1s | **16s** |
| `sector_daily`（目标池约 90–110 个） | 60s | 7s | 4s | 4s | **75s** |
| `sync_universe`（保留每日） | 48s | 6s | 2s | 4s | **60s** |
| 指数/宽基/风格/两融/国债 | 25s | 5s | 2s | 3s | **35s** |
| ETF 同步与映射（估算） | 35s | 4s | 2s | 4s | **45s** |
| `valuation`（新鲜跳过/向量化） | 0.5s | 0.2s | 15s | 1s | **约 17s** |
| `moneyflow`（每日跳过） | 0 | 0 | 0 | 0 | **0** |
| `sector_members`（每日跳过） | 0 | 0 | 0 | 0 | **0** |
| **合计** | **206.5s** | **28.2s** | **28s** | **19s** | **约 4.7 分钟** |

时间分配：

| 环节 | 耗时 | 占比 |
|---|---:|---:|
| 数据拉取 | 约 3.4 分钟 | 73% |
| 清洗/归一 | 约 0.5 分钟 | 10% |
| 处理/本地计算 | 约 0.5 分钟 | 10% |
| 存储 | 约 0.3 分钟 | 7% |

加上重试与调度开销，**单次日更预计 4.7–5 分钟**；正常情况下不超过 5 分钟。

如果只加“新鲜则跳过”而不优化 `compute_valuation`，单次估值仍需 181.5s，日更会回到约 7.3 分钟，**无法满足 5 分钟目标**。因此估值优化是必选项，不能只做去重。

### 1.4 每周任务耗时

| 任务 | 频率 | 预估耗时 |
|---|---|---:|
| `sector_members` 全量 | 每周 | 110–130s |
| `moneyflow` 全市场逐只 | 每周 | 约 22 分钟 |
| 目标池板块历史缺口回补 | 每周/按需 | 约 75s |
| **合计** | — | **约 25 分钟/周** |

### 1.5 单任务预估

| 任务 | 预估耗时 | 说明 |
|---|---:|---|
| `daily_snapshot` | 34s | 1 次全市场快照 |
| `sector_snapshot` | 16s | 同花顺行业+概念 2 次调用 |
| `sector_daily` | 75s | 目标池约 90–110 个板块，逐板块历史 |
| `sector_members` | 120s | 每周全量 |
| `moneyflow` | 1338s | 每周全市场逐只 |
| `sync_universe` | 60s | 每日 |
| 指数/宽基/风格/两融/国债 | 35s | 每日合计 |
| ETF 同步与映射 | 45s | 每日，估算 |
| `valuation`（当前） | 183s | 每日；`compute_valuation` 181.5s |
| `valuation`（优化后） | 17s | 每日；新鲜时 0s 跳过 |

---

## 2. 采集范围

### 2.1 板块池

| 组成 | 数量 | 来源 |
|---|---:|---|
| 同花顺行业板块 | 约 90 | `stock_board_industry_name_ths()` / `stock_fund_flow_industry("即时")` |
| 主题白名单 | 10–20 | 同花顺概念板块，按配置显式指定 |
| 目标池合计 | 约 90–110 | 行业 + 白名单 |

规则：

1. 行业板块默认全部进入目标池。
2. 主题板块只保留 `SECTOR_THEME_WHITELIST` 中显式配置的代码。
3. 目标池之外的板块不进入每日采集；已采集的历史数据保留，不删除。
4. 人工回补可通过 `codes` 参数临时绕过池过滤，仅用于定向修复。

### 2.2 可交易性过滤

板块进入每日采集前，必须满足：

| 条件 | 阈值 |
|---|---|
| ETF 映射（`dim_etf_sector_map` 当日有效） | 至少 1 只 |
| ETF 20 日均成交额 | ≥ `ETF_MIN_AMOUNT`（默认 1 亿） |
| ETF 规模 | ≥ `ETF_MIN_AUM`（默认 10 亿） |
| 板块有效成分数 | ≥ `SECTOR_MIN_MEMBERS`（默认 5） |

不满足的板块标记原因（`no_etf / etf_illiquid / etf_small / insufficient_members`），不进入目标池；不伪造映射、不补 0。

### 2.3 板块池解析

新增 `backend/app/ingest/sector_pool.py`：

```python
@dataclass(frozen=True)
class SectorPool:
    trade_date: date
    industry_codes: tuple[str, ...]
    theme_codes: tuple[str, ...]
    actionable_codes: tuple[str, ...]
    excluded: dict[str, str]       # code -> reason
    version: str                   # 配置 + 数据条件的稳定哈希
```

- `resolve_sector_pool(db, trade_date) -> SectorPool`
- 输入：板块清单、`dim_sector_member` 成分数、`dim_etf_sector_map`、`fact_etf_daily`。
- 输出按 `trade_date` 缓存；同一天多次调用不重复计算。
- 解析结果写入 `meta_data_quality.detail`（dataset=`sector_pool`），包含数量、排除原因分布、`version`。
- 本任务**不新增数据库表**；如确需新增，先停下并单独提交任务书。

---

## 3. 任务清单与频率

| 任务 | 数据源 | 频率 | 目标耗时 | 输出 |
|---|---|---|---:|---|
| `calendar` | akshare/baostock | 每日 | < 10s | `trade_calendar` |
| `universe` | akshare | 每日 | < 70s | `dim_stock` |
| `snapshot` | 东财/新浪 | 每日 | < 40s | `fact_stock_daily` |
| `sector_snapshot`（新增） | 同花顺即时 | 每日 | < 20s | `fact_sector_daily`（无 OHLCV） |
| `sector`（历史） | 同花顺历史 | 每日 | < 90s | `fact_sector_daily`（目标池） |
| `sector_members` | 同花顺 | 每周 | < 130s | `dim_sector_member`（目标池） |
| `moneyflow` | 东财/同花顺/新浪 | 每周 | < 25min | `fact_moneyflow` |
| 指数/宽基/风格 | 新浪/中证 | 每日 | < 40s | `market_indices` |
| 两融/国债 | 东财/中债 | 每日 | < 10s | `margin_trading` / `macro_rates` |
| ETF 同步与映射 | 新浪/东财 | 每日 | < 60s | `fact_etf_daily` / `dim_etf_sector_map` |
| 财报/分红 | 东财批量 | 每日 | < 1s | `dim_stock_financial` / `dim_stock_dividend` |
| `valuation`（本地重算） | 财报/分红缓存 + 当日价格 | 每日；新鲜则跳过 | < 30s | `fact_stock_daily` 的 `pe_ttm/pb/ps_ttm/dv_ttm` |

约束：

- `sector_snapshot` 只允许当日执行；历史日期不走该任务。
- `sector_members` 与 `moneyflow` 的每周任务不放入 `DEFAULT_DAILY_STEPS`。
- 所有任务继续通过 `pipeline.run_task` 执行，保留 advisory lock。
- 单任务失败不阻断后续任务；失败写 `meta_data_quality` 与 `ingest_job`。

---

## 4. 执行步骤

### S0：基线测量与开关

1. 在 `backend/app/core/config.py` 增加第 6 节配置项。
2. 在 `run_daily_pipeline` 的 summary 中增加每步耗时（`elapsed_ms`）。
3. 查询 `ingest_job` 最近 30 条，记录基线到 `docs/evidence/V10-ingest-baseline.md`。
4. 建立开关：`SECTOR_SNAPSHOT_ENABLED`、`SECTOR_HISTORY_DAILY`、`SECTOR_MEMBERS_WEEKLY`、`MONEYFLOW_FULL_WEEKLY`、`VALUATION_SKIP_IF_FRESH`、`CATCH_UP_SKIP_FRESH`，默认按本任务书取值；支持一键回滚。

### S1：板块池解析

1. 新增 `backend/app/ingest/sector_pool.py`，实现 `resolve_sector_pool`。
2. 板块清单复用 `backend/app/ingest/ths_board.py` 的 `load_board_list()`。
3. 成分数从 `dim_sector_member` 按当日 SCD2 条件统计。
4. ETF 映射从 `dim_etf_sector_map` 取 `valid_from <= trade_date AND (valid_to IS NULL OR valid_to > trade_date)`。
5. ETF 流动性从 `fact_etf_daily` 取最近 20 个交易日成交额均值；规模缺失保留 `NULL`，不补 0。
6. 排除原因写入日志与 `meta_data_quality.detail`。

### S2：板块快照（`sector_snapshot`）

1. 新增 `backend/app/ingest/sector_snapshot.py`，实现 `collect_sector_snapshot(trade_date, db)`。
2. 复用 `backend/app/ingest/ths_sector.py` 的 `fetch_sector_fundflow()`（行业 + 概念两次调用）。
3. 只保留目标池代码；用 `build_sector_daily_rows` 生成行。
4. 写入 `fact_sector_daily` 时使用**专用 upsert**：
   - 已存在行：只更新 `pct_chg / up_count / down_count / leading_stock / leading_stock_pct / main_net_inflow / src / updated_at`；
   - 不存在行：插入，OHLCV/成交额/换手率置 `NULL`；
   - **禁止用 NULL 覆盖已有 OHLCV/成交额/换手率**。
5. 质量校验：目标池覆盖率 ≥ 95%；重复代码 = 0；行业板块行数 ≥ 80。
6. 写入 `meta_data_quality`（dataset=`sector_snapshot`），并记录 `ingest_job`。

### S3：板块历史瘦身（`sector`）

1. 修改 `backend/app/ingest/service.py` 的 `collect_sector_daily`。
2. 调用 `fetch_daily_rows` 前先解析 `SectorPool`，只保留目标池板块。
3. 单板块失败不阻断：记录 `failed_boards` 与失败列表，任务状态 `partial`。
4. 单板块超时沿用 `SECTOR_BOARD_TIMEOUT_SEC`（默认 60s）；整任务设置 90s 软截止，超时后停止调度新板块并标记 `partial`。
5. `codes` 参数非空时允许人工定向回补，绕过池过滤。
6. 质量校验：目标池覆盖率 ≥ 95%；重复行 = 0；缺失保持 `NULL`。

### S4：板块成分改为每周

1. 修改 `backend/app/worker.py`：`sector_members` 改为每周执行（建议周日），保留手动触发。
2. 修改 `collect_sector_members`：只处理目标池。
3. SCD2 语义不变：`effective_from / effective_to` 按原逻辑维护；`is_partial` 仍如实标记。
4. 质量校验：目标池覆盖率 ≥ 90%；`partial` 板块单独计数；成分数 < 5 标记 `insufficient`。

### S5：资金流分层

1. 每日不再执行全市场逐只 `moneyflow`；板块级资金流由 `sector_snapshot` 提供。
2. 每周执行一次全市场 `moneyflow`，生成 `fact_moneyflow`，供下游按需使用。
3. 每日调用 `moneyflow` 时返回 `skipped`，原因 `weekly_only`，不写 0 行、不标记成功。
4. 修改 `meta_data_quality` 的新鲜度预期：`moneyflow` 按周检查，周内不判 stale。
5. 质量校验：每周全量覆盖率 ≥ 95%。

### S6：编排与监控

1. `pipeline.DEFAULT_DAILY_STEPS` 的采集段调整为：
   ```text
   calendar, universe, index, broad_indices, style_indices,
   sector_snapshot, sector, snapshot, moneyflow(weekly skip),
   sector_members(weekly skip), margin, ...
   ```
   其余步骤保持不变。
2. `pipeline.TASK_DATASET` 增加 `sector_snapshot -> sector_snapshot`。
3. `worker.py` 增加每周任务；每日任务中移除 `sector_members` 与全市场 `moneyflow`。
4. `meta_data_quality` 增加 `sector_snapshot`、`sector_pool` 数据集。
5. 任务监控页显示：任务名、频率、上次成功、覆盖率、耗时、排除原因统计。
6. 所有任务保留 advisory lock；重复执行幂等。

### S7：估值任务治理（`valuation`）

现状：

- `sync_financials` 0.28s（5220/5225 跳过）、`sync_dividends` 0.12s（5225 跳过）、`sync_revenue` 0.12s（`revenue_fresh`），网络阶段合计约 0.5s；
- `compute_valuation` 181.5s（5213 只），是主要耗时；
- `DEFAULT_DAILY_STEPS` 每天都包含 `valuation`；`catch_up` 在“今天已收盘但某个数据集不完整”时会重跑完整流水线，导致同一天估值跑两次（实测 11-23 的 16:56 与 17:11 各一轮）；
- `sync_financials` 的 4 只失败代码（`000002/810011/810013/810014`）会让任务长期 `partial`，与耗时无关，单独列入数据质量修复。

P0 去重（必须先做）：

1. 配置 `VALUATION_SKIP_IF_FRESH=1`。
2. `collect_latest_valuation` 开头检查 `meta_data_quality`：
   - `dataset='valuation'`；
   - `last_trade_date == target`；
   - `status == 'ok'`；
   - `last_success_at >= max(fact_stock_daily.updated_at, dim_stock_financial.updated_at, dim_stock_dividend.updated_at)`；
   - 价格、财报、分红任一输入更新后都必须重算，不能只看价格表；
   - 若相关 `updated_at` 为空，退化为 `last_trade_date == target` 且 `status == 'ok'`，避免 NULL 比较导致永远重算；
   - 满足且未传 `force=True` 时，直接返回 `rows=0, detail={"skipped":"already_collected"}`，不执行 sync/compute。
3. `collect_latest_valuation` 增加 `force: bool = False`；手动强制重算时传 `force=True`。
4. `run_daily_pipeline` / `catch_up` 增加 `skip_if_fresh` 行为：只补缺失数据集，不因某个数据集失败而重跑全部；至少对 `valuation` 生效。
5. 保留 `CATCH_UP_LOCK`；不得绕过 advisory lock。

P1 优化单次计算（满足 <30s）：

1. 先加分段计时：`sync_financials / sync_dividends / sync_revenue / compute_valuation`，写入 `meta_data_quality.detail` 与日志。
2. 批量向量化：
   - `build_valuation_frame` / `dv_ttm_series` / `ps_ttm_values` 支持多 code 时，整批（`COMPUTE_BATCH_CODES`）调用一次，不逐只调用；
   - 预建 `code -> financials/dividends` 索引，移除循环内 `financials[financials["code"] == code]`；
   - 单日只取每只股票 `announced_date <= target` 的最新报告期与近 365 天分红。
3. 更彻底：用单条 SQL `UPDATE fact_stock_daily ... FROM` CTE 完成 PE/PB/PS/股息率计算；目标 <10s。
4. 无论向量化还是 SQL，**PE/PB/PS/股息率口径不得改变**；固定日期输出必须与优化前逐字段一致。

P2 可观测性：

1. 为 `valuation` 整体写一条 `ingest_job`；现在只有 `sync_financials` / `sync_dividends` 子任务有记录。
2. 任务监控页显示 `valuation` 的状态、耗时、覆盖率、是否 `skipped`。
3. 日志打印四段耗时，便于定位。

验收：

- 同一天第二次调用返回 `skipped=already_collected`，0 网络、0 写入；
- 价格/财报/分红任一输入在估值后更新时，`last_success_at < max(updated_at)`，允许重算一次；
- `force=True` 可强制重算；
- 单次估值 <30s；
- 固定日期 PE/PB/PS/股息率与优化前零差异；
- 下游 full 口径信号零差异。

### S8：回归与切换

1. 同一交易日分别用旧/新配置跑一次，比较：
   - 目标池行数与覆盖率；
   - `fact_sector_daily` 字段完整性；
   - `dim_sector_member` 成分数；
   - `fact_moneyflow` 行数（新旧日更差异）；
   - `fact_stock_daily` 的 `pe_ttm / pb / ps_ttm / dv_ttm`（优化前后零差异）。
2. 验证下游 full 口径信号不变（固定日期 + 固定 `data_version`）。
3. 验收通过后，默认配置切换为新频率；保留开关可回滚。
4. 不删除任何历史数据；概念板块历史保留。

---

## 5. 实现约束

| 约束 | 要求 |
|---|---|
| 模块体积 | 新增模块 ≤ 72KB；`service.py` 已较大，新逻辑放独立模块 |
| 事务 | 采集层沿用 `_db_scope`；外部传入 `db` 时只 flush，不越权 commit |
| 幂等 | 所有 upsert 以 `(sector_code, trade_date)` 或 `(code, trade_date)` 为键 |
| 锁 | 每个任务保留 `pg_try_advisory_lock` |
| 缺失值 | 一律 `NULL`，不补 0；快照无 OHLCV 就写 NULL |
| 超时 | 单板块 60s；单任务软截止 90s（板块历史）/ 120s（板块快照） |
| 限频 | 沿用 `HISTORY_LIMITER` 与 `SNAPSHOT_LIMITER`，不得擅自提高并发 |
| 失败隔离 | 单板块/单代码失败不阻断整任务；状态记 `partial` |
| 日志 | 记录任务名、目标池数量、成功/失败/排除数量、覆盖率、耗时 |
| 估值新鲜度 | 以 `meta_data_quality.valuation.last_trade_date + last_success_at` 与 `fact_stock_daily / dim_stock_financial / dim_stock_dividend` 的最大 `updated_at` 比较；任一输入更新后必须重算 |
| 估值计算 | 批量向量化或 SQL；目标 <30s；不得改变 PE/PB/PS/股息率口径 |
| 回滚 | 每个频率开关可独立恢复旧行为 |

---

## 6. 配置项

```env
# 板块池
SECTOR_UNIVERSE_MODE=industry_plus_theme   # industry | industry_plus_theme
SECTOR_THEME_WHITELIST=                    # 逗号分隔的同花顺概念代码
SECTOR_MIN_MEMBERS=5
SECTOR_HISTORY_DAILY=1
SECTOR_BOARD_TIMEOUT_SEC=60
SECTOR_HISTORY_SOFT_DEADLINE_SEC=90

# 快照
SECTOR_SNAPSHOT_ENABLED=1
SECTOR_SNAPSHOT_TIMEOUT_SEC=120

# 频率
SECTOR_MEMBERS_WEEKLY=1
MONEYFLOW_FULL_WEEKLY=1

# 估值
VALUATION_SKIP_IF_FRESH=1
CATCH_UP_SKIP_FRESH=1
VALUATION_BATCH_CODES=200
VALUATION_TARGET_SEC=30                  # 仅监控告警，不做硬超时

# ETF 可交易性
ETF_MIN_AMOUNT=100000000
ETF_MIN_AUM=1000000000
```

所有配置进入 `backend/app/core/config.py`，不散落在业务代码。

---

## 7. 交付标准

- [ ] `sector_pool.py` 可稳定解析目标池，输出数量、排除原因、`version`。
- [ ] `sector_snapshot` 每日执行，目标池覆盖率 ≥ 95%，不覆盖已有 OHLCV。
- [ ] `sector_daily` 只采集目标池，覆盖率 ≥ 95%，单板块失败不阻断。
- [ ] `sector_members` 每周执行，目标池覆盖率 ≥ 90%，SCD2 语义不变。
- [ ] `moneyflow` 每日跳过、每周全量，覆盖率 ≥ 95%。
- [ ] `valuation` 同一天重复调用返回 `skipped`，0 网络、0 写入。
- [ ] `valuation` 单次重算 <30s，输出与优化前逐字段一致。
- [ ] `valuation` 整体写入 `ingest_job`，四段耗时可见。
- [ ] 日更总耗时 P95 < 5 分钟，单任务 < 2 分钟。
- [ ] `meta_data_quality` 与 `ingest_job` 记录完整，新鲜度状态不误报。
- [ ] 所有任务幂等、可重跑、可回滚。
- [ ] 不新增数据库表；不删除历史数据。
- [ ] 下游 full 口径信号零差异。
- [ ] 文档同步：`README.md`、`docs/wiki/04-ingest.md`、`docs/wiki/08-operations.md`。

---

## 8. 校验规则

### 8.1 单元测试

- 板块池：行业全量、白名单生效、无 ETF 排除、ETF 流动性不足排除、成分不足排除、`version` 稳定。
- 快照合并：已有 OHLCV 不被 NULL 覆盖；新行 OHLCV 为 NULL；快照字段正确更新。
- 板块历史：非目标池不抓取、不写入；`codes` 定向回补可绕过。
- 资金流：每日返回 `skipped/weekly_only`；每周执行全量。
- 时间预算：`sector_daily` 软截止后不再调度新板块，状态 `partial`。
- 估值新鲜度：fresh 跳过、价格更新后重算、`force=True` 强制重算。
- 估值输出：固定日期优化前后 `pe_ttm / pb / ps_ttm / dv_ttm` 零差异。

### 8.2 集成测试

- 交易日/非交易日/收盘前：非交易日与收盘前正确跳过。
- 单板块超时/失败：任务 `partial`，其他板块正常写入。
- 覆盖率不足：写 `quality_failed`，不静默成功。
- 重复执行同一天：无重复行，结果一致。
- 目标池为空：任务 `skipped`，原因明确。
- 同一天连续跑两次 `valuation`：第二次 `skipped=already_collected`，无写入。
- 估值后重跑 `snapshot`：`valuation` 因 `updated_at` 变化允许重算一次。

### 8.3 回归

- 固定交易日 + 固定 `data_version`，下游 full 口径信号逐条对比，零差异。
- 固定日期 `valuation` 四字段零差异。
- `meta_data_quality` 中 `sector_snapshot` / `sector_daily` / `sector_members` / `moneyflow` / `valuation` 状态与实际频率一致。
- 现有 `stg_run` / `backtest_run` 历史可查。

### 8.4 验收命令

```powershell
cd backend
..\.venv\Scripts\python.exe -m pytest tests -q

$env:GFNA_TEST_DB=1; $env:GFNA_TEST_DATABASE_NAME="gfna_test"
$env:SCHEDULER_ENABLED=0; $env:CATCH_UP_ENABLED=0
..\.venv\Scripts\python.exe -m pytest tests/integration -q
```

---

## 9. 风险与处置

| 风险 | 处置 |
|---|---|
| 同花顺即时快照不能回溯历史 | 快照只用于当日；历史走逐板块历史接口 |
| 快照 NULL 覆盖历史 OHLCV | 专用 upsert，只更新快照字段；单测锁定 |
| 板块清单变更/代码失效 | 缓存清单；未知代码跳过并告警；保留旧代码映射 |
| 单板块超时拖垮整任务 | 60s 单板块超时 + 90s 任务软截止 + `partial` |
| 每周资金流/成分导致新鲜度误报 | 调整 `meta_data_quality` 预期周期；周内不判 stale |
| 目标池为空 | 任务 `skipped`，原因 `empty_pool`，不写 0 行 |
| 降低采集频率导致数据缺口 | 保留手动触发；每周任务可随时补跑；缺口按历史口径回补 |
| 并发提高触发限频/风控 | 沿用现有 limiter，不擅自提高并发 |
| 估值新鲜度判断误跳过 | 必须比较 `last_success_at` 与价格/财报/分红三表的 `max(updated_at)`；任一输入更新后重算 |
| 估值优化改变口径 | 固定日期四字段逐字段对比；保留纯函数回退 |
| `catch_up` 重跑整链路 | 增加 `skip_if_fresh`；只补缺失数据集，不因单数据集失败重跑全部 |

---

## 10. 验收清单

- [ ] 日更 P95 < 5 分钟；单任务 < 2 分钟。
- [ ] `sector_daily` 目标池覆盖率 ≥ 95%，无小时级任务。
- [ ] `sector_snapshot` 每日成功，覆盖率 ≥ 95%。
- [ ] `sector_members` 每周成功，覆盖率 ≥ 90%。
- [ ] `moneyflow` 每周成功，覆盖率 ≥ 95%。
- [ ] `valuation` 同日重复调用 `skipped`，单次 <30s。
- [ ] `valuation` 四字段与优化前一致，且整体写入 `ingest_job`。
- [ ] 目标池数量、排除原因、`version` 可在日志/质量表中审计。
- [ ] 快照不覆盖历史 OHLCV。
- [ ] 下游 full 口径信号零差异。
- [ ] 所有开关可回滚。
- [ ] 文档同步完成。

---

## 11. 附录

### 11.1 实测总量

| 任务 | 实测耗时 |
|---|---:|
| `daily_snapshot` | 33.9s |
| `sector_daily` | 288–376s；失败 2.9h / 14.7h |
| `sector_members` | 561.7s |
| `moneyflow` | 1337.9s |
| `sync_universe` | 49–69s |
| `index_daily` / `broad_indices` / `style_indices` | 1–26s |
| `margin` / `macro_rates` | 1–2s |
| `valuation`（本地） | 约 183s；`compute_valuation` 181.5s，网络阶段约 0.5s |

`valuation` 的分段实测：

| 阶段 | 耗时 |
|---|---:|
| `sync_financials` | 0.28s（5220/5225 跳过） |
| `sync_dividends` | 0.12s（5225 跳过） |
| `sync_revenue_bulk` | 0.12s（`revenue_fresh`） |
| `compute_valuation` | **181.5s（5213 只）** |

11-23 的 16:56 与 17:11 各出现一轮 `sync_financials/sync_dividends`，说明 `catch_up` 或重启触发了第二次估值链路。

### 11.2 数量对照

| 口径 | 数量 |
|---|---:|
| 同花顺行业 | 约 90 |
| 同花顺概念 | 约 387 |
| 当前板块日线 | 441–459 |
| 当前板块成分 | 479 |
| 目标池 | 约 90–110 |

### 11.3 相关代码

| 文件 | 作用 |
|---|---|
| `backend/app/pipeline.py` | 任务编排、`DEFAULT_DAILY_STEPS`、`TASK_DATASET` |
| `backend/app/worker.py` | 每日/每周定时任务 |
| `backend/app/ingest/service.py` | `collect_sector_daily`、`collect_sector_members`、`collect_moneyflow`、`sync_stock_universe` |
| `backend/app/ingest/ths_board.py` | 板块清单与历史日线 |
| `backend/app/ingest/ths_sector.py` | 板块即时资金流与快照行构造 |
| `backend/app/ingest/quality.py` | 质量校验 |
| `backend/app/db/models/fact.py` | `FactSectorDaily` |
| `backend/app/db/models/dim.py` | `DimSectorMember` |
| `backend/app/db/models/compass.py` | `DimEtfSectorMap`、`FactEtfDaily` |
| `backend/app/compass/etf_sync.py` | ETF 行情与映射同步 |
| `backend/app/services/freshness.py` | `meta_data_quality` |
| `backend/app/ingest/valuation.py` | `collect_latest_valuation`、`compute_valuation` |
| `backend/app/ingest/valuation_store.py` | 财报/分红/价格读取 |
| `backend/app/ingest/valuation_math.py` | `build_valuation_frame`、`dv_ttm_series`、`ps_ttm_values` |
| `backend/app/catch_up.py` | 启动补跑与 `skip_if_fresh` |
| `backend/app/db/models/meta.py` | `meta_data_quality` / `ingest_job` |

---

> 本任务书只覆盖数据采集链路。
