# 08 · 运行、部署与测试

---

## 1. 前置条件

| 项 | 要求 |
|---|---|
| Python | 3.11+ |
| Node.js | 20+（CI 使用 24） |
| PostgreSQL | 16（本地或 Docker） |
| 可选 | Playwright Chromium（新闻 Scrapy 爬虫） |
| 可选 | DeepSeek API Key（不配则日报降级为模板） |

---

## 2. 本地启动（Windows）

```powershell
cd C:\docker-projects\grna
python -m venv .venv
.\.venv\Scripts\activate
pip install -r backend\requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

copy backend\.env.example backend\.env
# 编辑 backend\.env：POSTGRES_PASSWORD、可选 DEEPSEEK_API_KEY

cd backend
python scripts\bootstrap_db.py
python scripts\pipeline_cli.py calendar
python scripts\pipeline_cli.py universe
python scripts\pipeline_cli.py history_fast --start 2023-01-01
python scripts\pipeline_cli.py backfill --start 2021-01-01 --end 2022-12-31
python scripts\pipeline_cli.py moneyflow_history
python scripts\pipeline_cli.py valuation_fast --start 2018-01-01
python scripts\pipeline_cli.py all

python -m uvicorn app.main:app --reload --port 8000
```

另开终端：

```powershell
cd frontend
npm install
npm run dev
```

访问：

- 前端 `http://localhost:5173`
- API 文档 `http://127.0.0.1:8000/docs`
- 健康检查 `http://127.0.0.1:8000/health`

> 顺序硬约束：**日线 → 估值 → 指标 → 因子 → 选股**。`all` 只跑最新交易日，不回补历史。

### 2.1 一键启停（Windows，脱离会话常驻）

```powershell
.\start-local.ps1          # 后端 8000 + 前端 5173（先 bootstrap_db）
.\start-local.ps1 -Demo    # 空库先灌演示数据（first_run.py）
.\start-local.ps1 -Stop    # 停止占用端口的进程
```

日志：`logs/backend.log`、`logs/frontend.log`。

注意：脚本用 `vite preview` 提供**构建产物**，首次需先 `npm run build`（或改用 `npm run dev`）。

### 2.2 Linux / macOS

```bash
chmod +x start.sh
./start.sh
# 可选镜像：
PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ ./start.sh
```

`start.sh` 会检查 Python、创建 `backend/.venv`、安装依赖、生成 `backend/.env`、执行 `bootstrap_db.py`，最后启动 uvicorn。

### 2.3 无网验收（确定性演示数据）

```bash
cd backend
python scripts/pipeline_cli.py demo
python scripts/pipeline_cli.py features
python scripts/pipeline_cli.py scores
python scripts/pipeline_cli.py sector_scores
python scripts/pipeline_cli.py algos
```

---

## 3. 环境变量（按需）

完整清单见 [`core/config.py`](../../backend/app/core/config.py)。常用：

| 变量 | 默认 | 说明 |
|---|---|---|
| `POSTGRES_HOST/PORT/USER/PASSWORD/DATABASE` | localhost/5432/postgres/空/gfna_db | 数据库 |
| `HISTORY_START` | 2021-01-01 | 历史深度 |
| `DATA_PRIMARY_PROVIDER` / `DATA_FALLBACK_PROVIDER` | akshare / baostock | 采集链 |
| `INGEST_NETWORK_ENABLED` | 1 | 0 = 离线 |
| `SCHEDULER_ENABLED` | 1 | 0 = 不启动定时任务 |
| `DB_POOL_SIZE` | 10 | 连接池大小（并发回测/前端轮询占满会导致 pool timeout） |
| `TASK_RECOVERY_STALE_SEC` | 7200 | 启动清理遗留 running/queued 的阈值；Docker Compose 设为 0 |
| `AUTO_CREATE_TABLES` | 1 | 启动兜底建表 |
| `CATCH_UP_ENABLED` | 1 | 启动补缺 |
| `ADMIN_API_KEY` | 空 | 空 = 管理接口仅本机 |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` | 空 / deepseek-chat | AI 日报 |
| `PUSH_ENABLED` / `WECOM_WEBHOOK_URL` | 0 / 空 | 企业微信推送 |
| `PUSH_ALGOS` / `PUSH_TOP_N` / `PUSH_MODE` | 空 / 10 / always | 推送范围 |
| `OBSERVATION_ALGOS` / `PUSH_OBSERVATION` | 空 / 0 | 观察候选 |
| `PUSH_WATCHLIST_ONLY` | 0 | 1 = 只推自选命中；0 = 全部并标 `[自选]` |
| `PAPER_ENABLED_ALGOS` | 空 = 全部 | 纸面组合 |
| `CORS_ORIGINS` | `*` | 收紧生产来源 |
| `LOG_LEVEL` / `LOG_JSON` | INFO / 0 | 日志 |

> 安全：`backend/.env` 与根 `.env` 可能包含数据库密码、DeepSeek Key、Webhook URL，**不要提交到仓库**；若已泄露请立即轮换。

---

## 4. 数据库引导

```bash
cd backend
python scripts/bootstrap_db.py                  # 建库 + 建表 + 分区 + 视图 + 种子
python scripts/bootstrap_db.py --only-schema    # 只建结构，不写种子
python scripts/bootstrap_db.py --recreate       # 危险：删全部业务表后重建
```

它做四件事：

1. 目标库不存在则 `CREATE DATABASE`；
2. `Base.metadata.create_all` 建表（含分区父表）；
3. 预建年度分区 + default，创建 `v_stock_latest` / `v_stock_features`；
4. 写 `algo_meta` 种子，按需回填 `dim_stock.ownership`。

已有库增量迁移（容器启动时会自动执行）：

```bash
cd backend
python scripts/migrate_db.py     # 有 alembic_version 时 upgrade head；无版本且结构完整时 stamp head
alembic upgrade head             # 等价的手动方式
```

> `bootstrap_db.py` 的 `create_all` 只补“缺失的表”，不会给旧表补列。
> 已有数据卷升级镜像后，必须执行增量迁移，否则会出现
> `column ... does not exist` 导致接口 500（典型：`margin_trading.margin_buy_amount`）。

---

## 5. 流水线 CLI

入口：[`backend/scripts/pipeline_cli.py`](../../backend/scripts/pipeline_cli.py)

```bash
python scripts/pipeline_cli.py calendar
python scripts/pipeline_cli.py universe
python scripts/pipeline_cli.py snapshot
python scripts/pipeline_cli.py backfill [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--codes 600519,000001]
python scripts/pipeline_cli.py history_fast --start 2023-10-01
python scripts/pipeline_cli.py moneyflow_history --start 2024-01-01
python scripts/pipeline_cli.py valuation_fast --start 2023-10-01
python scripts/pipeline_cli.py rebuild --start 2025-07-01 --end 2026-10-23
python scripts/pipeline_cli.py features
python scripts/pipeline_cli.py scores
python scripts/pipeline_cli.py sector_scores
python scripts/pipeline_cli.py sector_snapshot
python scripts/pipeline_cli.py algos
python scripts/pipeline_cli.py report
python scripts/pipeline_cli.py daily      # 等价 all
python scripts/pipeline_cli.py all
python scripts/pipeline_cli.py valuation --force   # 忽略新鲜度，强制重算
python scripts/pipeline_cli.py moneyflow --force   # 忽略周频限制，立即补采
python scripts/pipeline_cli.py demo       # 离线演示数据
python scripts/pipeline_cli.py backtest a3 --start 2025-10-21 --end 2026-10-21
```

支持任务名：`backfill / history_fast / moneyflow_history / valuation_fast / rebuild / snapshot / daily / all / features / scores / sector_scores / sector_snapshot / algos / report / calendar / universe / index / sector / sector_members / sector_fallback / moneyflow / valuation / margin / demo`。

---

## 6. 其他常用脚本

目录：`backend/scripts/`

| 脚本 | 用途 |
|---|---|
| `first_run.py` | 空库灌演示数据 + 指标/评分/选股（容器首启用） |
| `backfill_indices.py` | 指数历史 |
| `backfill_style_indices.py` | 风格指数（创业板50/红利低波50） |
| `backfill_broad_indices.py` | 6 宽基指数 |
| `backfill_macro.py` | 国债收益率 |
| `backfill_temperature.py` / `backfill_regime.py` | 温度/状态历史 |
| `backfill_market_factors.py` | 市场因子 |
| `backfill_sector_scores.py` / `backfill_sector_metrics.py` | 板块评分/聚合 |
| `backfill_short_features.py` | 短线指标 |
| `backfill_signal_perf.py` | 信号前瞻 |
| `backfill_valuation.py` | 估值 |
| `backfill_sector_ths.py` | 同花顺板块 |
| `import_ths_members.py` / `import_sina_members.py` | 成分导入 |
| `reconcile_freshness.py` | 新鲜度对账 |
| `check_invariants.py` | 不变量巡检 |
| `calibrate_regime.py` | 市场状态阈值校准 |
| `daily_quick.py` | 快速日更 |
| `push_cli.py` | 手动测试企业微信推送（`--dry-run`） |
| `visual_acceptance.py` | Playwright 视觉验收 |
| `backend_watchdog.py` / `install_backend_watchdog.py` | 后端看门狗 |
| `dedupe_strategy_runs.py` | 清理重复 run |

---

## 7. 定时任务

[`backend/app/worker.py`](../../backend/app/worker.py)

| 时间 | 任务 | 说明 |
|---|---|---|
| 15:36 | 盘后流水线 | 真实交易日判断；节假日跳过 |
| 16:30 | 策略选股 + 观察候选 + 推送 | 先 `algos`，再 `OBSERVATION_ALGOS`，最后推送 |
| 17:10 | 信号前瞻统计 | 重算最近 25 个交易日 |
| 17:30 | 纸面组合 | 非交易日跳过；单组合失败隔离 |
| 周日 11:30 | 板块成分周更 | `sector_members`，只处理目标池 |
| 周日 12:00 | 个股资金流周更 | 全市场 `moneyflow`，`force=True` |
| 02:00 | 历史补齐 | `backfill`，断点续传 |
| 每 N 分钟 | 新闻采集 | `CRAWL_INTERVAL_MINUTES`，默认 60 |

15:36 盘后流水线中的 `valuation` 会在同日价格/财报/分红未变时跳过（`skipped=already_collected`）；`catch_up` 复用同一保护，避免重复计算。

并发安全：每个任务 `pg_try_advisory_lock("gfna:<name>")`；拿不到锁直接跳过。

`misfire_grace_time`：日更 3 小时、夜间 6 小时，避免主机休眠导致任务被判过期；跨天缺口由 `catch_up` 处理。

---

## 8. Docker 部署

```bash
docker compose up -d --build
docker compose logs -f backend
```

服务：

| 服务 | 镜像 | 端口 |
|---|---|---|
| `postgres` | `postgres:16-alpine` | 宿主机 `127.0.0.1:5433` → 容器 5432 |
| `backend` | `backend/Dockerfile`（python:3.11-slim-bookworm） | `127.0.0.1:8000` |
| `frontend` | `frontend/Dockerfile`（node 构建 + nginx） | `127.0.0.1:5173` → 80 |

后端镜像包含：

- Playwright Chromium（新闻爬虫）；
- `bootstrap_db.py` 自动初始化；
- `migrate_db.py` 启动前自动应用增量迁移（已有数据卷升级不会因缺列 500）；
- `first_run.py` 空库灌演示数据（`SEED_DEMO_ON_EMPTY=1` 默认）；
- HEALTHCHECK `/health`。

启动顺序：`bootstrap_db.py` → `migrate_db.py` → `first_run.py` → `uvicorn`。

### 升级已有数据卷

```bash
docker compose up -d --build
docker compose logs -f backend      # 应看到「应用数据库增量迁移 ... ✅ 数据库迁移完成」
```

若日志出现 `column ... does not exist`：

```bash
docker compose exec backend python -m alembic upgrade head
docker compose restart backend
```

只有确认数据可丢弃时才使用 `bootstrap_db.py --recreate`（会删除全部业务表）。

### 8.1 回测在 Docker 下随机失败

典型现象：同一策略同一区间，回测有时 failed；把「调仓周期」从 20 改成 21（或改起止日）
后又能成功。

根因：调仓日为 `trading_days[::rebalance_days]`。其中任一调仓日缺行情/指标/评分
（节假日兜底日历把非交易日算进来、盘中数据未落库、历史回填缺口、显式传入未来 `end`），
旧实现会让整次回测 `failed`；改动调仓周期只是改变信号日相位，绕开缺口后看起来"恢复"。

现状（2026-11 起）：

- 缺数据的调仓日会被跳过并记录到 `metrics.skipped_signal_days / skipped_signal_dates`，
  其余调仓日照常计算；只有全部调仓日都缺数据才 failed；
- 后台回测对瞬时数据库错误自动退避重试 2 次；
- Compose 已设置 `TASK_RECOVERY_STALE_SEC=0`：容器重启后遗留 `running/queued` 立即标 failed；
- 时区：compose / Dockerfile 均为 `TZ=Asia/Shanghai`，数据库会话也固定为该时区。

排查命令：

```bash
docker compose logs backend --since 30m | findstr /i "回测 failed 重试"
docker compose exec backend date
docker exec gfna-postgres psql -U postgres -d gfna_db -c "show timezone; select now();"
# 交易日历与实际行情缺口（应尽量为空）
docker exec gfna-postgres psql -U postgres -d gfna_db -c \
  "SELECT c.cal_date FROM trade_calendar c LEFT JOIN (SELECT DISTINCT trade_date FROM fact_stock_daily) d ON d.trade_date=c.cal_date WHERE c.is_open AND d.trade_date IS NULL AND c.cal_date <= current_date ORDER BY c.cal_date DESC LIMIT 20;"
```

前端 Nginx：

- SPA 路由 `try_files ... /index.html`；
- `/api/` 反代 `backend:8000`；
- `/health` 单独反代；
- 静态资源 30 天缓存。

首次初始化：

```bash
docker compose exec backend python scripts/bootstrap_db.py
docker compose exec backend python scripts/pipeline_cli.py all
```

---

## 9. 测试

### 9.1 后端单元测试（不需要数据库）

```bash
cd backend
..\.venv\Scripts\python.exe -m pytest tests -q
# Linux/macOS
PYTHONPATH=. pytest tests -q
```

覆盖：指标已知值（Wilder RSI）、因子单调性、条件树、参数指纹、回测绩效、未来函数检测、API 契约、错误体、策略参数真生效、run 可复现、指数对比/牛熊估值、纸面组合、自选命中。

### 9.2 集成测试（需要真实库）

```powershell
$env:GFNA_TEST_DB=1
$env:SCHEDULER_ENABLED=0
$env:CATCH_UP_ENABLED=0
cd backend
..\.venv\Scripts\python.exe -m pytest tests/integration -q
```

测试库名必须以 `_test` 结尾（`alembic/env.py` 强制校验）。

先准备数据：

```bash
python scripts/pipeline_cli.py demo
for task in features scores sector_scores algos; do python scripts/pipeline_cli.py $task; done
```

### 9.3 前端

```bash
cd frontend
npm run build
npm run test:treemap
npm run test:sparkline
npm run test:palette
npm run test:sample
```

---

## 10. CI

[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) 四组：

| Job | 内容 |
|---|---|
| `backend` | `pytest tests -q`（无 DB，禁用调度） |
| `backend-integration` | PostgreSQL 16 服务 + `alembic upgrade head` + `pytest tests/integration -q` |
| `frontend` | `npm ci` → `npm run build` → treemap/sparkline/palette 测试 |
| `frontend-browser` | demo 数据 + 启动后端 + preview + `visual_acceptance.py`（4 视口 × 2 主题 × 5 页面） |

CI 统一设置 `TZ=Asia/Shanghai`、`SCHEDULER_ENABLED=0`、`AUTO_CREATE_TABLES=0`、`CATCH_UP_ENABLED=0`。

---

## 11. 排障

| 现象 | 原因 / 处理 |
|---|---|
| `pip install` 卡住 | 用阿里云镜像 |
| 导入崩、缺 asyncpg/pandas | 用仓库根 `.venv` 的解释器 |
| Windows uvicorn 卡死 | 不要移除 `WindowsSelectorEventLoopPolicy` |
| 页面无数据 | 空库：`pipeline_cli.py demo` 或按顺序回补真实数据 |
| 新闻爬虫 `Executable doesn't exist` | `python -m playwright install chromium` |
| 东财 `RemoteDisconnected` | 本网络屏蔽 `push2*`；系统已降级腾讯/新浪/同花顺 |
| 腾讯回补 501/拦截 | 内置 WAF 熔断，冷却 15 分钟后重试或等自动切新浪 |
| 板块热力图空 | 先跑 `sector`；同花顺分页不完整时标 `partial`，市值/家数如实置空 |
| 管理接口 403 | 未配置 `ADMIN_API_KEY` 且非本机；配置或本机调用 |
| 任务一直 lock_busy | 可能有僵尸连接持有 advisory lock；等待 15 分钟 `idle_in_transaction_session_timeout` 回收 |
| 回测 409 数据不足 | 先跑历史回补 + `features/scores`；响应 `next_action` 有提示 |
| 前端 `/api` 404 | dev 用 `npm run dev`；preview 需 `npm run build`；生产检查 Nginx |

---

## 12. 访问边界与安全

- 本机启动与 Docker 部署都只绑定 `127.0.0.1`；
- 单用户设计，不做多租户隔离；
- 管理写操作需 `X-Admin-Key` 或本机来源；
- 需要跨设备访问时优先 SSH 隧道 / Tailscale；确有公网需求再在反向代理层加 Basic Auth，**不要把端口改成 `0.0.0.0`**；
- 外部数据仅个人研究，不得对外分发或商用。

---

## 13. 演示站发布与更新

演示站是**只读静态快照**，不需要后端与数据库：

```bash
cd backend
python scripts/export_demo_snapshot.py      # 生成 frontend/public/demo/*.json
cd ../frontend
npm install
npm run build:demo                          # 输出 dist-demo/
npm run preview -- --outDir dist-demo       # 本地预览
```

- 导出脚本使用独立 `*_demo` 库（默认 `gfna_demo`），不会写生产库；
- 行情来自 `app.fixtures.demo_data.seed_demo_data`，下游走 `features/scores/sector_scores/market_factors/temperature/regime/algos`；
- 演示站顶部固定显示「演示数据 · 只读」横幅；POST/PUT 等写操作在 `frontend/src/api/client.ts` 直接拒绝；
- GitHub Actions `demo-pages.yml` 在 push `main` 后自动重建并部署 Pages。首次使用需在仓库 Settings → Pages 选择 **GitHub Actions**；未开启时 deploy 步骤会显式失败；
- 快照停留在构建日，不随交易日更新；`frontend/public/demo/manifest.json` 记录生成时间与文件清单。
