# GFNA Code Wiki

> 面向开发者的代码地图：**项目怎么分层、数据怎么流动、关键逻辑在哪里、如何运行与扩展**。
>
> 生成基线：2026-11-16 工作区代码（README 描述为 GFNA V7，仓库内已包含 V8 增量）。
> 文档通过静态阅读源码 + 实际导出 FastAPI 路由生成；命令与字段均以代码为准。

---

## 1. 这个项目是什么

GFNA 是一套 **A 股行情采集 + 本地量化选股系统**：

```
外部数据源（AKShare/Baostock/腾讯/新浪/东财/同花顺/中债/中证）
        │
        ▼
L1 采集 ingest ──► L2 PostgreSQL 单一事实源
                        │
                        ▼
              L3 计算 engine（指标/因子/评分/市场状态/板块/信号统计）
                        │
                        ▼
              L4 策略 strategy（条件树/选股/回测/参数矩阵/纸面组合）
                        │
                        ▼
              L5 服务 api + React 前端（看板/选股中心/个股/任务监控）
```

核心设计原则：

- **本地库是唯一事实源**：所有分析接口只读 PostgreSQL，请求路径不访问外部数据源；
- **可复现**：每次选股落 `stg_run`（run_id / 参数快照 / 数据版本），改参数必然产生新 run；
- **诚实缺数据**：缺失值保留 `NULL`，不补 0；数据新鲜度由 `meta_data_quality` 统一表达；
- **防未来函数**：估值按公告日 point-in-time，回测 T 日信号 T+1 开盘成交，策略包静态禁止负向 `shift`；
- **单机自用边界**：API/前端/数据库只绑定 `127.0.0.1`，无账号体系。

---

## 2. 技术栈与关键事实

| 维度 | 事实 |
|---|---|
| 后端 | Python 3.11、FastAPI 0.110、SQLAlchemy 2.0（async + asyncpg）、APScheduler |
| 前端 | React 18、TypeScript 5.5、Vite 5、TanStack Query 5、Tailwind 4、Recharts、lightweight-charts |
| 数据库 | PostgreSQL 16；`fact_stock_daily` 按年 RANGE 分区 |
| 任务编排 | `backend/app/pipeline.py` 的 `run_task` / `run_daily_pipeline` |
| 定时任务 | 15:36 盘后串行链路（数据 → 罗盘 → 推送）/ 每 30 分钟巡检 / 17:10 信号统计 / 17:30 纸面 / 02:00 补齐 / 定时新闻 |
| 当前正式策略 | **A3、A4、C1、C2、C3、C6**（2026-11-28 起六个全部启用）；A1/A2/B1–B5/C4/C5 实现已移除，仅保留历史 run |
| 后端入口 | `backend/app/main.py` → `app:app` |
| CLI 入口 | `backend/scripts/pipeline_cli.py` |
| 端口 | 后端 8000、前端 5173、宿主机 PostgreSQL 5433（容器内 5432） |

代码规模（不含 `.venv` / `node_modules`）：

| 区域 | 文件数 | 约行数 |
|---|---:|---:|
| `backend/app` | 106 | 34.9k |
| `backend/scripts` | 30 | 5.4k |
| `backend/tests` | 66 | 11.1k |
| `frontend/src` | 112 | 20.9k |

---

## 3. 文档索引

| 文档 | 内容 | 适合谁 |
|---|---|---|
| [01-架构总览](./01-architecture.md) | 分层、部署拓扑、请求链路、目录结构、设计约束 | 所有人，先读 |
| [02-后端应用层](./02-backend-api.md) | 启动生命周期、配置、错误模型、完整 API 清单、鉴权 | 后端 / 前端联调 |
| [03-数据模型](./03-data-model.md) | 全部表、分区、视图、SCD2、新鲜度、迁移 | 后端 / 数据分析 |
| [04-采集与数据质量](./04-ingest.md) | Provider 链、采集任务、限频重试、质量校验、断点续传、补缺 | 数据工程 |
| [05-计算引擎](./05-engine.md) | 指标、因子、评分、市场状态、温度、板块、信号统计 | 量化 / 算法 |
| [06-策略与回测](./06-strategy-backtest.md) | 策略定义、条件树、缓存复现、回测撮合、参数矩阵、纸面组合 | 量化 / 策略 |
| [策略因子清单](../STRATEGY-FACTORS.md) | A3/A4/C1/C2/C3/C6 参数、条件因子、排序与数据门槛 | 量化 / 策略 |
| [07-前端应用](./07-frontend.md) | 路由、页面、Hooks、组件、设计系统、状态持久化 | 前端 |
| [08-运行与部署](./08-operations.md) | 环境变量、引导、CLI、一键启动、Docker、定时任务、CI、排障 | 运维 / 新成员 |
| [09-依赖关系与扩展](./09-dependencies.md) | 模块依赖图、关键调用链、扩展点、技术债、术语表 | 架构 / 贡献者 |

推荐阅读路径：

- **快速上手**：`README.md`（仓库根，操作手册）→ [08-运行与部署](./08-operations.md) → [01-架构总览](./01-architecture.md)
- **改后端接口**：[02-后端应用层](./02-backend-api.md) → [03-数据模型](./03-data-model.md) → [09-依赖关系与扩展](./09-dependencies.md)
- **改策略/因子**：[05-计算引擎](./05-engine.md) → [06-策略与回测](./06-strategy-backtest.md)
- **改前端页面**：[07-前端应用](./07-frontend.md) → [02-后端应用层](./02-backend-api.md)

---

## 4. 仓库目录速查

```
backend/
├── app/
│   ├── main.py            FastAPI 入口（lifespan / 中间件 / 异常处理）
│   ├── pipeline.py        任务编排（run_task / run_daily_pipeline / advisory lock）
│   ├── worker.py          APScheduler 定时任务
│   ├── catch_up.py        启动时补最近缺失交易日
│   ├── api/v1/            路由：market/algo/backtest/stocks/paper/...
│   ├── core/              配置、常量、错误码、日志、状态枚举、所有制规则
│   ├── db/                Base/Session/ORM models/分区与视图/种子
│   ├── ingest/            Provider 链、各数据源、质量校验、批量回补
│   ├── engine/            纯计算与落库（指标/因子/市场/板块/信号/纸面）
│   ├── strategy/          条件树、策略、runner、回测、参数矩阵
│   ├── services/          新鲜度、LLM、新闻、日报、任务恢复
│   ├── notify/            企业微信推送
│   ├── quality/           数据不变量巡检
│   └── fixtures/          离线演示数据
├── alembic/               14 个迁移
├── scripts/               bootstrap_db / pipeline_cli / backfill_* / 运维脚本
└── tests/                 单元 + integration
frontend/src/
├── api/                   HTTP 封装（按后端模块拆分）
├── hooks/                 React Query 查询/轮询
├── pages/                 9 个路由页面
├── components/            图表、表格、回测、任务、命令面板
├── lib/                   纯函数与本地状态（treemap/session）
└── types/                 与后端契约对应的 TS 类型
docs/                      V5 方案、API 契约、V6/V7/V8 任务书、evidence
docker-compose.yml         PostgreSQL + FastAPI + Nginx
```

---

## 5. 需要先知道的约定

| 约定 | 说明 |
|---|---|
| 时区 | 全系统 `Asia/Shanghai`；所有数据库连接显式设置 session timezone |
| 单位 | `volume`=股、`amount`=元、市值=元、比率=百分数（`%`） |
| 评分 | 个股/板块为 **0–10**；市场温度为 **0–100**；六维因子原始为 **0–1 分位** |
| 缺失值 | 一律 `NULL`，不补 0；缺数据时情绪/技术分用中性 5.0 并标注原因 |
| 事务 | API 依赖 `get_db` 成功即 commit；引擎层 `db=None` 自建事务、传入 `db` 只 flush |
| 锁 | 采集/日更使用 `pg_try_advisory_lock`；多实例部署时同一任务只跑一个 |
| 错误体 | `{"ok": false, "error": {"code", "message", "detail", "suggestion"}, "request_id"}` |
| 股票池 | 只保留沪深主板 / 创业板 / 科创板；北交所不采集 |

---

## 6. 相关文档（仓库已有）

- 仓库根 `README.md`：操作手册、数据源现实、策略口径、常见问题。
- `docs/architecture.md`：V5 时期的架构说明（部分内容已被 V6/V7/V8 扩展，阅读时以本 Wiki 为准）。
- `docs/V5-API-CONTRACT.md`：接口契约细节。
- `docs/GFNA_V6_产品升级任务书.md`、`docs/GFNA_V7_*.md`、`docs/GFNA_V8_策略汰换与盘后推送任务书.md`：版本需求与验收口径。
- `docs/evidence/`：回测基线与校准证据。
