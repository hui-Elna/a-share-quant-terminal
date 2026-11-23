# V10 数据采集瘦身基线证据

> 日期：2026-11-23
> 范围：仅数据采集链路（ingest）。
> 对应任务书：`docs/GFNA_V10_数据采集瘦身任务书.md`

---

## 1. 改前基线

本机 `ingest_job` 最近记录：

| 任务 | 耗时 |
|---|---:|
| `daily_snapshot` | 33.9s |
| `sector_daily` | 288–376s；失败记录 2.9h / 14.7h |
| `sector_members` | 561.7s |
| `moneyflow` | 1337.9s |
| `sync_universe` | 49–69s |
| `valuation` 整体 | 约 183s |
| `compute_valuation` | 181.5s（5213 只） |
| `sync_financials` | 0.28s（5220/5225 跳过） |
| `sync_dividends` | 0.12s（5225 跳过） |
| `sync_revenue_bulk` | 0.12s（`revenue_fresh`） |

11-23 的 16:56 与 17:11 各出现一轮估值子任务，说明 `catch_up`/重启触发了重复链路。

---

## 2. 改后验证

### 2.1 板块目标池

命令：只读调用 `resolve_sector_pool(db, latest_trade_date)`。

结果（2026-11-23）：

| 项 | 数值 |
|---|---:|
| 同花顺行业 | 90 |
| 主题白名单 | 0（尚未配置） |
| 可交易目标池 | 53 |
| 排除 | 37 |
| `etf_illiquid` | 24 |
| `no_etf` | 13 |
| `version` | `bbdd70844a73` |

结论：板块历史采集范围从约 450 个板块收敛到 53 个可交易板块；13 个行业无 ETF、24 个 ETF 20 日均额不足 1 亿被排除。若需要扩大目标池，可调整 `ETF_MIN_AMOUNT` 或补充主题白名单。

### 2.2 估值新鲜度跳过

命令：

```powershell
$env:PYTHONPATH='c:\docker-projects\grna\backend'
..\.venv\Scripts\python.exe C:\Users\21468\AppData\Local\Temp\gfna_valuation_skip_smoke.py
```

结果：

```text
rows 0
elapsed_ms 151
detail {'task': 'collect_latest_valuation', 'skipped': 'already_collected',
        'existing': 5213, 'trade_date': '2026-11-23'}
```

`ingest_job` 整体记录：`task=valuation, status=skipped, done=5213, rows_written=0, elapsed_ms=981`。

结论：同一天第二次估值调用从约 183s 降为 0.15–1.0s，且不写入、不联网；`catch_up`/重启重复调用会被同一保护拦截。

### 2.3 估值单日快速路径

命令：对 2026-11-20 全市场 5216 只股票执行 `compute_valuation`，并与改造前慢路径写入值逐字段比较。

结果：

```text
codes 5216
rows 5216
elapsed_ms 2756
diff_count 0
```

结论：单日估值从 181.5s 降到 **2.76–3.04s**（两次实测），PE/PB/PS/股息率与慢路径零差异。

### 2.4 测试

| 命令 | 结果 |
|---|---:|
| `pytest tests -q` | 743 passed, 76 skipped |
| `GFNA_TEST_DB=1 GFNA_TEST_DATABASE_NAME=gfna_test pytest tests/integration -q` | 35 passed |
| `GFNA_TEST_DB=1 GFNA_TEST_DATABASE_NAME=gfna_api_test pytest tests/test_api_v5.py -q` | 33 passed |

新增/修改的测试覆盖：

- `tests/test_sector_pool_v10.py`：目标池、白名单、可交易性、版本稳定性。
- `tests/test_sector_snapshot_v10.py`：快照只写目标池、OHLCV 保持 NULL、update 列不碰 OHLCV。
- `tests/test_sector_daily_v10.py`：板块历史只抓目标池。
- `tests/test_valuation_freshness_v10.py`：新鲜跳过、`force=True` 强制重算。
- `tests/test_pipeline_v10.py`：资金流周更跳过、`meta_data_quality` 新鲜度判断。
- `tests/integration/test_sector_snapshot_upsert.py`：快照 upsert 保留历史 OHLCV。

---

## 3. 边界与后续

1. `sector_snapshot` 的实时网络调用未在本证据中执行；单元测试覆盖了过滤与 upsert 语义，实际网络行为应在 15:36 盘后流水线观察。
2. 目标池 53 个低于任务书预估 60–80，原因是 ETF 流动性门槛与 13 个行业无 ETF；这是数据事实，不伪造映射。
3. 本次未新增数据库表、未删除历史数据、未修改股票回测费率。
4. 估值快速路径只在 `start == end` 时启用；区间回补仍走原慢路径，避免改变历史回补语义。
