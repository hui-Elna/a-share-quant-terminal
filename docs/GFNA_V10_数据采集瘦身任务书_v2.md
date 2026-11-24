# GFNA V10 数据采集瘦身任务书

<!-- 版本固定项｜模型、提示词模板、文档结构任一变化，触发 §4.3 全量回归复核 -->
> 文档版本：v2（回归防护升级）
> 生成日期：2026-11-23
> 执行模型：DeepSeek-V4.1-Flash
> 提示词模板：AI任务书回归防护规范.md
> 上一版快照：docs/GFNA_V10_数据采集瘦身任务书_v1.md
> 范围：仅数据采集链路（ingest）。
> 状态：已实施（2026-11-23；证据见 `docs/evidence/V10-ingest-baseline.md`）。
> 目标：日更 P95 < 5 分钟；单任务 < 2 分钟；估值重算 < 30 秒；板块池 = 同花顺行业约 90 个 + 主题白名单 10–20 个。
> 维护规则：冻结区不得改写；变更说明写 §3；新增约束/事实/验收项以追加方式写入 §1/§2/§4；每新增一条约束，同步新增一条 Fxx 与一条 Axx。

---

<!-- SECTION:FROZEN 冻结区｜已定稿，本次及后续修改中不得改写、删除、合并或重新表述 -->
## 1. 已定稿约束

### 1.1 范围与目标

- **F01 范围**：本任务书只覆盖数据采集链路（ingest），不扩展到其他模块。
- **F02 目标**：日更 P95 < 5 分钟；单任务 < 2 分钟；估值重算 < 30 秒。
- **F03 板块池**：同花顺行业约 90 个 + 主题白名单 10–20 个；目标池约 90–110 个。
- **F04 可交易性**：板块进入每日采集前，须满足 ETF 映射、ETF 20 日均额 ≥ 1 亿、规模 ≥ 10 亿、板块有效成分数 ≥ 5。

### 1.2 数据口径

- **F05 全市场个股快照**：每日保留，不能用板块池替代。
- **F06 板块历史**：只抓目标池；目标池之外的板块不进入每日采集。
- **F07 数据频率**：板块成分每周；全市场个股资金流每周；每日资金流由板块快照提供。
- **F08 估值去重**：同一交易日已成功且输入未变时跳过；价格、财报、分红任一输入更新后必须重算；支持 `force=True` 强制重算。
- **F09 估值口径**：PE/PB/PS/股息率口径不得改变；优化前后固定日期必须逐字段一致。
- **F10 快照写入**：使用专用 upsert，禁止用 NULL 覆盖已有 OHLCV、成交额、换手率。
- **F11 数据保留**：不新增数据库表；不删除历史数据；概念板块历史保留。
- **F12 编排**：所有任务走 `pipeline.run_task`；保留 advisory lock；单任务失败不阻断后续。
- **F13 缺失值**：一律 `NULL`，不补 0；快照无 OHLCV 就写 `NULL`。
- **F14 限频与超时**：沿用现有 limiter；单板块 60s；板块历史 90s 软截止；板块快照 120s。
- **F15 事务**：采集层沿用 `_db_scope`；外部传入 `db` 时只 flush，不越权 commit。
- **F16 分类口径**：同花顺行业 + 主题白名单；不迁移其他分类体系。
- **F17 每日任务**：calendar、universe、snapshot、index、ETF 同步每日执行。
- **F18 每周任务**：sector_members、全市场 moneyflow 每周执行。
- **F19 估值任务**：每日执行；新鲜时跳过；单次目标 <30s；整体写入 `ingest_job`。
- **F20 验收范围**：回归命令只使用 `pytest tests` 与 `pytest tests/integration`，不引入其他模块验证。
- **F21 模块体积**：新增模块 ≤ 72KB；新逻辑放独立模块，不继续膨胀 `service.py`。

### 1.3 交付与版本

- **F22 交付格式**：Markdown 任务书；版本化快照；冻结条款从 F01 起连续编号；验收清单从 A01 起连续编号。
- **F23 变更边界**：后续修改只允许在 §3 写变更说明，并在 §1 / §2 / §4 追加新条目；不得改写、删除、合并或重新表述既有条款、事实行与验收项。
- **F24 触发复核**：模型、提示词模板、文档结构任一变化，必须按 §4.3 做全量回归复核。
- **F25 幂等**：所有 upsert 以 `(sector_code, trade_date)` 或 `(code, trade_date)` 为键。
- **F26 失败隔离**：单板块/单代码失败不阻断整任务；状态记 `partial`。
- **F27 日志**：记录任务名、目标池数量、成功/失败/排除数量、覆盖率、耗时。
- **F28 回滚**：每个频率开关可独立恢复旧行为。

---

<!-- SECTION:FACTS 硬事实表｜最易被顺手优化掉，单独抽表核对 -->
## 2. 硬事实表

### 2.1 技术栈与包结构

| 项 | 值 | 备注 |
|---|---|---|
| 语言 | Python 3.11 | — |
| Web | FastAPI | — |
| ORM | SQLAlchemy 2.0 async + asyncpg | — |
| 数据库 | PostgreSQL 16 | `fact_stock_daily` 按年分区 |
| 调度 | APScheduler | 单进程 + advisory lock |
| 采集入口 | `backend/app/ingest/` | — |
| 编排 | `backend/app/pipeline.py` | `run_task` / `DEFAULT_DAILY_STEPS` |
| 定时任务 | `backend/app/worker.py` | 每日/每周 |
| 启动补跑 | `backend/app/catch_up.py` | 启动检查缺口 |
| 板块清单/历史 | `backend/app/ingest/ths_board.py` | `load_board_list` / `fetch_daily_rows` |
| 板块即时流 | `backend/app/ingest/ths_sector.py` | `fetch_sector_fundflow` / `build_sector_daily_rows` |
| 板块池（新增） | `backend/app/ingest/sector_pool.py` | `resolve_sector_pool` |
| 板块快照（新增） | `backend/app/ingest/sector_snapshot.py` | `collect_sector_snapshot` |
| 板块日线/成分/资金流 | `backend/app/ingest/service.py` | `collect_sector_daily` / `collect_sector_members` / `collect_moneyflow` |
| 估值入口 | `backend/app/ingest/valuation.py` | `collect_latest_valuation` / `compute_valuation` |
| 估值读取 | `backend/app/ingest/valuation_store.py` | `_load_financials_for_codes` / `_load_dividends_for_codes` / `_load_price_rows` |
| 估值数学 | `backend/app/ingest/valuation_math.py` | `build_valuation_frame` / `dv_ttm_series` / `ps_ttm_values` |
| 质量校验 | `backend/app/ingest/quality.py` | — |
| 新鲜度 | `backend/app/services/freshness.py` | `meta_data_quality` |
| 任务记录 | `backend/app/db/models/meta.py` | `meta_data_quality` / `ingest_job` |
| 板块模型 | `backend/app/db/models/fact.py` | `FactSectorDaily` / `FactStockDaily` |
| 成分模型 | `backend/app/db/models/dim.py` | `DimSectorMember` / `DimStockFinancial` / `DimStockDividend` |
| ETF 模型 | `backend/app/db/models/compass.py` | `DimEtfSectorMap` / `FactEtfDaily` |
| ETF 同步 | `backend/app/compass/etf_sync.py` | 行情与映射 |

### 2.2 表名与字段口径

| 项 | 值 |
|---|---|
| 个股快照表 | `fact_stock_daily` |
| 板块日线表 | `fact_sector_daily` |
| 板块成分表 | `dim_sector_member` |
| ETF 映射表 | `dim_etf_sector_map` |
| ETF 行情表 | `fact_etf_daily` |
| 财报缓存表 | `dim_stock_financial` |
| 分红缓存表 | `dim_stock_dividend` |
| 质量表 | `meta_data_quality` |
| 任务表 | `ingest_job` |
| 估值字段 | `pe_ttm` / `pb` / `ps_ttm` / `dv_ttm` |
| 板块快照字段 | `pct_chg` / `up_count` / `down_count` / `leading_stock` / `leading_stock_pct` / `main_net_inflow` |
| 板块池 dataset | `sector_pool` |
| 板块快照 dataset | `sector_snapshot` |
| 估值 dataset | `valuation` |
| 估值去重输入 | `fact_stock_daily.updated_at` / `dim_stock_financial.updated_at` / `dim_stock_dividend.updated_at` |
| 估值批量常量 | `COMPUTE_BATCH_CODES=200` |
| 板块池对象 | `trade_date / industry_codes / theme_codes / actionable_codes / excluded / version` |
| 行业板块来源 | `stock_board_industry_name_ths()` / `stock_fund_flow_industry("即时")` |

### 2.3 数量与实测耗时

| 口径 | 数量/耗时 |
|---|---:|
| 同花顺行业 | 约 90 |
| 同花顺概念 | 约 387 |
| 当前板块日线（行业+概念） | 441–459 |
| 当前板块成分 | 479 |
| 目标池 | 约 90–110 |
| 全市场个股快照 | 5221 只 |
| 估值覆盖 | 5213 只 |
| `daily_snapshot` | 33.9s |
| `sector_daily` | 288–376s；失败 2.9h / 14.7h |
| `sector_members` | 561.7s |
| `moneyflow` | 1337.9s |
| `sync_universe` | 49–69s |
| 指数/宽基/风格/两融/国债 | 1–35s |
| `valuation` 整体 | 约 183s |
| `compute_valuation` | 181.5s |
| `sync_financials` | 0.28s（5220/5225 跳过） |
| `sync_dividends` | 0.12s（5225 跳过） |
| `sync_revenue_bulk` | 0.12s（`revenue_fresh`） |
| 当前日更合计 | 约 43 分钟 |
| 瘦身后日更合计 | 约 4.7 分钟 |
| 估值优化前 | 181.5s |
| 估值优化后目标 | <30s |
| 日更目标 | P95 <5 分钟 |
| 单任务目标 | <2 分钟 |

#### 2.3.1 当前日更分环节

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

环节占比：拉取约 36 分钟（84%）、清洗约 2 分钟（5%）、处理/本地计算约 3.8 分钟（9%）、存储约 0.8 分钟（2%）。

#### 2.3.2 瘦身后日更分环节

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

环节占比：拉取约 3.4 分钟（73%）、清洗约 0.5 分钟（10%）、处理/本地计算约 0.5 分钟（10%）、存储约 0.3 分钟（7%）。

#### 2.3.3 每周任务

| 任务 | 频率 | 预估耗时 |
|---|---|---:|
| `sector_members` 全量 | 每周 | 110–130s |
| `moneyflow` 全市场逐只 | 每周 | 约 22 分钟 |
| 目标池板块历史缺口回补 | 每周/按需 | 约 75s |
| **合计** | — | **约 25 分钟/周** |

#### 2.3.4 单任务

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

重复执行事实：11-23 的 16:56 与 17:11 各出现一轮 `sync_financials/sync_dividends`，说明 `catch_up` 或重启触发了第二次估值链路。

### 2.4 配置项与默认值

```env
# 板块池
SECTOR_UNIVERSE_MODE=industry_plus_theme
SECTOR_THEME_WHITELIST=
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
VALUATION_TARGET_SEC=30

# ETF 可交易性
ETF_MIN_AMOUNT=100000000
ETF_MIN_AUM=1000000000
```

### 2.5 交付格式与命名

| 项 | 值 |
|---|---|
| 当前任务书 | `docs/GFNA_V10_数据采集瘦身任务书.md` |
| v1 快照 | `docs/GFNA_V10_数据采集瘦身任务书_v1.md` |
| 版本命名 | `docs/GFNA_V10_数据采集瘦身任务书_vN.md` |
| 当前版本 | v2 |
| 冻结条款编号 | F01–F28 |
| 验收清单编号 | A01–A20 |
| 变更位置 | 变更说明 §3；新增条目追加到 §1/§2/§4 |
| 复核位置 | §4.3 |

---

<!-- SECTION:CHANGE 变更区｜本次变更说明写在此处；既有冻结条款/事实/验收项不得改写，只允许追加 -->
## 3. 本次变更

### 3.1 变更说明

本次变更 = **将任务书升级为回归防护结构**，不改变既有采集口径、阈值、任务频率、代码路径。具体改动：

1. 新增文档头部固定项（版本、日期、模型、模板、快照）。
2. 新增 §1 冻结区 F01–F28。
3. 新增 §2 硬事实表（技术栈、包结构、表名字段、数量、耗时、配置、命名）。
4. 原第 1 节耗时数据迁入 §2.3，数值不变。
5. 原第 2 节采集范围迁入 F03–F06 与 §2.3，数值不变。
6. 原第 3 节任务清单与频率保留在 §3.3，频率与目标耗时不变。
7. 原第 4 节 S0–S8 保留在 §3.4，步骤不变。
8. 原第 5 节实现约束迁入 F12–F15、F21，内容不变。
9. 原第 6 节配置项迁入 §2.4，名称与默认值不变。
10. 原第 7 节交付标准迁入 §3.8 与 §4.1，判定条件不变。
11. 原第 8 节校验规则迁入 §4.1–§4.2，命令不变。
12. 原第 9 节风险表保留在 §3.7，新增估值相关风险后共 11 项。
13. 原第 10 节验收清单改写为 A01–A20，每条可判通过/不通过。
14. 原第 11 节附录迁入 §2.1 / §2.3，数值不变。

### 3.2 当前与目标耗时

当前日更约 **43 分钟**：其中 `valuation` 约 183s（`compute_valuation` 181.5s，网络阶段约 0.5s），`sector_daily` 约 300s，`sector_members` 约 562s，`moneyflow` 约 1338s。

瘦身后日更约 **4.7 分钟**：`sector_daily` 降至约 75s，`sector_members` 与全市场 `moneyflow` 移出每日，`valuation` 降至约 17s。

如果只加“新鲜则跳过”而不优化 `compute_valuation`，单次估值仍需 181.5s，日更会回到约 7.3 分钟，**无法满足 5 分钟目标**。因此估值优化是必选项。

### 3.3 任务清单与频率

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

### 3.4 执行步骤 S0–S8

#### S0：基线测量与开关

1. 在 `backend/app/core/config.py` 增加 §2.4 配置项。
2. 在 `run_daily_pipeline` 的 summary 中增加每步耗时（`elapsed_ms`）。
3. 查询 `ingest_job` 最近 30 条，记录基线到 `docs/evidence/V10-ingest-baseline.md`。
4. 建立开关：`SECTOR_SNAPSHOT_ENABLED`、`SECTOR_HISTORY_DAILY`、`SECTOR_MEMBERS_WEEKLY`、`MONEYFLOW_FULL_WEEKLY`、`VALUATION_SKIP_IF_FRESH`、`CATCH_UP_SKIP_FRESH`，默认按 §2.4 取值；支持一键回滚。

#### S1：板块池解析

1. 新增 `backend/app/ingest/sector_pool.py`，实现 `resolve_sector_pool`。
2. 板块清单复用 `backend/app/ingest/ths_board.py` 的 `load_board_list()`。
3. 成分数从 `dim_sector_member` 按当日 SCD2 条件统计。
4. ETF 映射从 `dim_etf_sector_map` 取 `valid_from <= trade_date AND (valid_to IS NULL OR valid_to > trade_date)`。
5. ETF 流动性从 `fact_etf_daily` 取最近 20 个交易日成交额均值；规模缺失保留 `NULL`，不补 0。
6. 排除原因写入日志与 `meta_data_quality.detail`。

#### S2：板块快照（`sector_snapshot`）

1. 新增 `backend/app/ingest/sector_snapshot.py`，实现 `collect_sector_snapshot(trade_date, db)`。
2. 复用 `backend/app/ingest/ths_sector.py` 的 `fetch_sector_fundflow()`（行业 + 概念两次调用）。
3. 只保留目标池代码；用 `build_sector_daily_rows` 生成行。
4. 写入 `fact_sector_daily` 时使用**专用 upsert**：
   - 已存在行：只更新 `pct_chg / up_count / down_count / leading_stock / leading_stock_pct / main_net_inflow / src / updated_at`；
   - 不存在行：插入，OHLCV/成交额/换手率置 `NULL`；
   - **禁止用 NULL 覆盖已有 OHLCV/成交额/换手率**。
5. 质量校验：目标池覆盖率 ≥ 95%；重复代码 = 0；行业板块行数 ≥ 80。
6. 写入 `meta_data_quality`（dataset=`sector_snapshot`），并记录 `ingest_job`。

#### S3：板块历史瘦身（`sector`）

1. 修改 `backend/app/ingest/service.py` 的 `collect_sector_daily`。
2. 调用 `fetch_daily_rows` 前先解析 `SectorPool`，只保留目标池板块。
3. 单板块失败不阻断：记录 `failed_boards` 与失败列表，任务状态 `partial`。
4. 单板块超时沿用 `SECTOR_BOARD_TIMEOUT_SEC`（默认 60s）；整任务设置 90s 软截止，超时后停止调度新板块并标记 `partial`。
5. `codes` 参数非空时允许人工定向回补，绕过池过滤。
6. 质量校验：目标池覆盖率 ≥ 95%；重复行 = 0；缺失保持 `NULL`。

#### S4：板块成分改为每周

1. 修改 `backend/app/worker.py`：`sector_members` 改为每周执行（建议周日），保留手动触发。
2. 修改 `collect_sector_members`：只处理目标池。
3. SCD2 语义不变：`effective_from / effective_to` 按原逻辑维护；`is_partial` 仍如实标记。
4. 质量校验：目标池覆盖率 ≥ 90%；`partial` 板块单独计数；成分数 < 5 标记 `insufficient`。

#### S5：资金流分层

1. 每日不再执行全市场逐只 `moneyflow`；板块级资金流由 `sector_snapshot` 提供。
2. 每周执行一次全市场 `moneyflow`，生成 `fact_moneyflow`，供下游按需使用。
3. 每日调用 `moneyflow` 时返回 `skipped`，原因 `weekly_only`，不写 0 行、不标记成功。
4. 修改 `meta_data_quality` 的新鲜度预期：`moneyflow` 按周检查，周内不判 stale。
5. 质量校验：每周全量覆盖率 ≥ 95%。

#### S6：编排与监控

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

#### S7：估值任务治理（`valuation`）

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

#### S8：回归与切换

1. 同一交易日分别用旧/新配置跑一次，比较：
   - 目标池行数与覆盖率；
   - `fact_sector_daily` 字段完整性；
   - `dim_sector_member` 成分数；
   - `fact_moneyflow` 行数（新旧日更差异）；
   - `fact_stock_daily` 的 `pe_ttm / pb / ps_ttm / dv_ttm`（优化前后零差异）。
2. 验证下游 full 口径信号不变（固定日期 + 固定 `data_version`）。
3. 验收通过后，默认配置切换为新频率；保留开关可回滚。
4. 不删除任何历史数据；概念板块历史保留。

### 3.5 配置项

以 §2.4 为唯一事实源；不在本区重复维护配置值，避免两处漂移。

### 3.6 实现约束

以 §1 冻结区 F12–F15、F21 为准。执行补充：

1. 新逻辑放独立模块，不继续膨胀 `service.py`。
2. 每次任务执行记录：任务名、目标池数量、成功/失败/排除数量、覆盖率、耗时。
3. 回滚开关必须能独立恢复旧行为，不得依赖删数据或重建表。

### 3.7 风险与处置

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

### 3.8 交付标准与交付物

交付标准：

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

交付物：

1. 代码：`sector_pool.py`、`sector_snapshot.py`，以及 `service.py`、`pipeline.py`、`worker.py`、`catch_up.py`、`valuation.py` 的必要修改。
2. 配置：§2.4 全部配置项进入 `backend/app/core/config.py`。
3. 证据：`docs/evidence/V10-ingest-baseline.md`，包含改前/改后耗时、覆盖率、重复执行次数。
4. 记录：`ingest_job` 与 `meta_data_quality` 可查询 `valuation`、`sector_snapshot`、`sector_pool` 状态。
5. 测试：单元测试、集成测试、回归对比记录。
6. 文档：`README.md`、`docs/wiki/04-ingest.md`、`docs/wiki/08-operations.md` 同步。

---

<!-- SECTION:ACCEPTANCE 验收清单｜每条必须可判通过/不通过 -->
## 4. 回归验收清单

### 4.1 验收清单 A01–A20

| 编号 | 判定条件 | 原文位置 |
|---|---|---|
| A01 | 文档头部包含版本、日期、模型、提示词模板、上一版快照五项 | 文档头部 |
| A02 | 冻结条款编号从 F01 起连续、无缺号；F01–F28 原值保留 | §1 |
| A03 | 硬事实表至少包含技术栈、包结构、表名字段、数量耗时、配置默认值、交付命名六类 | §2.1–§2.5 |
| A04 | 板块池仍为同花顺行业约 90 + 主题白名单 10–20 | F03、§2.3 |
| A05 | 目标池仍为约 90–110 | F03、§2.3 |
| A06 | 全市场个股快照仍为每日执行 | F05 |
| A07 | 板块历史仍只抓目标池 | F06、§3.4 S3 |
| A08 | 板块成分与全市场资金流仍为每周执行 | F07、§3.3 |
| A09 | 估值跳过条件包含价格、财报、分红三表 `updated_at` | F08、§3.4 S7 |
| A10 | 估值单次目标仍为 <30 秒 | §2.3、§2.4 |
| A11 | 日更 P95 <5 分钟、单任务 <2 分钟 | §2.3 |
| A12 | 快照 upsert 仍禁止用 NULL 覆盖 OHLCV、成交额、换手率 | F10、§3.4 S2 |
| A13 | 仍不新增数据库表、不删除历史数据 | F11 |
| A14 | 仍通过 `pipeline.run_task` 执行并保留 advisory lock | F12 |
| A15 | 缺失值仍一律 NULL，不补 0 | F13 |
| A16 | §2.4 中既有配置项名称与默认值未变；新增配置以追加方式出现 | §2.4 |
| A17 | 执行步骤 S0–S8 全部存在，顺序未变 | §3.4 |
| A18 | 风险表至少包含 11 项，前 11 项名称与顺序未变 | §3.7 |
| A19 | 验收命令仍为 `pytest tests` 与 `pytest tests/integration` | §4.2 |
| A20 | 版本记录包含 v1 快照；当前文件版本号与版本记录最后一行一致 | 文档头部、§2.5 |

> 维护：新增约束时追加 Fxx 与对应 Axx；不得改写既有编号内容。

### 4.2 验收命令

```powershell
cd backend
..\.venv\Scripts\python.exe -m pytest tests -q

$env:GFNA_TEST_DB=1; $env:GFNA_TEST_DATABASE_NAME="gfna_test"
$env:SCHEDULER_ENABLED=0; $env:CATCH_UP_ENABLED=0
..\.venv\Scripts\python.exe -m pytest tests/integration -q
```

测试覆盖要求：

- 单元：
  - 板块池：行业全量、白名单生效、无 ETF 排除、ETF 流动性不足排除、成分不足排除、`version` 稳定。
  - 快照合并：已有 OHLCV 不被 NULL 覆盖；新行 OHLCV 为 NULL；快照字段正确更新。
  - 板块历史：非目标池不抓取、不写入；`codes` 定向回补可绕过。
  - 资金流：每日返回 `skipped/weekly_only`；每周执行全量。
  - 估值新鲜度：fresh 跳过、价格/财报/分红更新后重算、`force=True` 强制重算。
  - 估值输出：固定日期优化前后 `pe_ttm / pb / ps_ttm / dv_ttm` 零差异。
  - 时间预算：`sector_daily` 软截止后不再调度新板块，状态 `partial`。
- 集成：
  - 交易日/非交易日/收盘前：非交易日与收盘前正确跳过。
  - 单板块超时/失败：任务 `partial`，其他板块正常写入。
  - 覆盖率不足：写 `quality_failed`，不静默成功。
  - 重复执行同一天：无重复行，结果一致。
  - 目标池为空：任务 `skipped`，原因明确。
  - 同一天连续跑两次 `valuation`：第二次 `skipped=already_collected`，无写入。
  - 估值后重跑 `snapshot`：`valuation` 因 `updated_at` 变化允许重算一次。
- 回归：
  - 固定交易日 + 固定 `data_version`，下游 full 口径信号逐条对比，零差异。
  - 固定日期 `valuation` 四字段零差异。
  - `meta_data_quality` 中 `sector_snapshot / sector_daily / sector_members / moneyflow / valuation` 状态与实际频率一致。
  - 现有 `stg_run / backtest_run` 历史可查。

### 4.3 独立回归复核指令（必须另开一轮）

```text
以下是当前完整文档，以及一份约束清单。
你的任务只是核对，不是修改。

逐条判断文档是否仍满足每条约束，输出格式：
  [编号] 通过 / 不通过 ｜ 对应原文位置（章节号或原句摘录）

规则：
- 不通过时，指出被破坏的具体条款和缺失内容，不要猜测原因。
- 不要修改文档、不要补充建议、不要评价文档质量、不要重写任何段落。
- 清单之外的改动也请单列一行「未列入清单的变动」。
- 最后只给一行汇总：通过 X / 共 Y 条。

【完整文档】
（粘贴当前版本全文）

【约束清单】
（粘贴 §4.1 验收清单 + §2 硬事实表）
```

全量重跑触发条件：模型版本、提示词模板文件名、文档结构任一变化。触发后按上述格式全量核对，并单列“与上一基线相比的输出结构差异”。

### 4.4 版本记录

| 版本 | 日期 | 变更 | 快照 |
|---|---|---|---|
| v1 | 2026-11-23 | 数据采集瘦身任务书初版（含估值治理） | `docs/GFNA_V10_数据采集瘦身任务书_v1.md` |
| v2 | 2026-11-23 | 按回归防护规范升级结构；采集口径、阈值、频率、代码路径不变 | 当前文件 |

后续规则：

1. 每次改动前先复制当前文件为 `_vN.md` 快照。
2. 只允许在 §3 写变更说明；§1 / §2 / §4 只允许追加新条目，既有内容不得改写、删除、合并或重新表述。
3. 文本 diff 检查冻结区与事实表；异常则打回重出。
4. 另开一轮按 §4.3 复核；全部通过后才归档为 `vN+1`。
5. 保留被拒稿版本，便于回溯漂移。

---

> 本任务书只覆盖数据采集链路。
