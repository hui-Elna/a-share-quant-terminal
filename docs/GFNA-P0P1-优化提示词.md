# GFNA V8 → V9 优化提示词文档（P0 / P1 六项）

> **用途**：将本文档整体投喂给编码 Agent，按第 7 节批次顺序逐项执行。每项均为「要求 → 实际实行方案 → 验收标准 → 风险与局限」四段式，可直接作为任务指令。
> **目标仓库**：`https://github.com/hui-Elna/a-share-quant-terminal`，默认分支 `main`。
> **依据**：2026-11-17 对仓库 README、完整目录树、CI 配置、V8 回测基线与关键源码的实测结果。文中所有文件路径均来自实测目录树，未做路径臆造。
> **性质**：这是**程序优化执行提示词**，不是需求摘要。执行者必须产出可运行代码、测试与文档，不得只输出方案描述。

---

## 0. 全局设定（执行前必读）

### 0.1 角色与目标

你是该仓库的资深全栈维护者。目标是在**不破坏现有回测口径与已启用策略结论**的前提下，补齐 P0（展示可信度、验证严谨性、数据合规可见性）与 P1（交互配置、推送解释、部署轻量化）六项能力。

### 0.2 硬性约束（违反即视为任务失败）

1. **不得修改回测成交规则与费率**：T+1 开盘成交、佣金万1（双边，最低 5 元）、印花税万5（卖出）、滑点千1、后复权口径，一律保持 `backend/app/strategy/backtest.py` 现状。
2. **不得改动 `SPECS` / `CANDIDATE_SPECS` 的启用状态**：A3/A4 启用、C1–C6 观察、B1–B5 停用，除非对应策略通过新增的样本外验证且经人工确认。
3. **不得破坏现有测试**：基线为文档自述的 634 passed / 75 skipped。任何改动后全量测试不得出现新增失败。
4. **复用优先**：过滤条件、交易日历、市场门槛、指标计算一律调用现有模块，禁止另写一套并行实现。
5. **安全**：不得提交 `.env`、`WECOM_WEBHOOK_URL`、`DEEPSEEK_API_KEY`、`ADMIN_API_KEY`；日志与响应中不得输出完整 URL 或密钥。
6. **可回滚**：每项任务一个独立提交；数据库变更必须有 Alembic 迁移且支持 `downgrade`。
7. **文档同步**：每项任务完成后同步更新 `README.md`、`docs/wiki/` 对应篇目、`docs/CHANGELOG-V5.md`。
8. **口径诚实**：任何展示层不得把样本内结果标为"稳健"、不得把条件复述包装成"投资建议"、不得在归因缺失时编造理由。

### 0.3 技术基线（实测）

| 层 | 现状 |
|---|---|
| 后端 | FastAPI 0.110 + SQLAlchemy async 2.0 + PostgreSQL 16 + Alembic（最新迁移 `20261115_0014_push_log.py`） |
| 前端 | React 18 + Vite + React Query；页面在 `frontend/src/pages/`，公共组件在 `frontend/src/components/` |
| 测试 | `backend/tests/`（单元 + integration）、`backend/scripts/visual_acceptance.py`（Playwright 4 视口 × 2 主题 × 5 页面） |
| CI | `.github/workflows/ci.yml`：后端单测、PG16 集成测试、前端构建 + 纯函数测试、浏览器视觉验收 |
| 策略 | `backend/app/strategy/strategies.py` 定义 SPECS；条件 DSL 在 `backend/app/strategy/condition.py` |
| 推送 | `backend/app/notify/push.py`，`scripts/push_cli.py` 支持 `--dry-run` / `--date` |
| 数据源 | `backend/app/ingest/` 下 akshare / baostock / 腾讯 / 新浪 / 东财 / 同花顺多 Provider |
| 文档 | `docs/wiki/` 9 篇、`docs/V5-API-CONTRACT.md`、`docs/evidence/V8-backtest-baseline.md` |

### 0.4 统一验收命令（每项任务结束都跑一遍）

```bash
cd backend && PYTHONPATH=. pytest tests -q
GFNA_TEST_DB=1 SCHEDULER_ENABLED=0 CATCH_UP_ENABLED=0 PYTHONPATH=. pytest tests/integration -q
cd frontend && npm run build && npm run test:treemap && npm run test:sparkline && npm run test:palette
cd backend && python -m alembic upgrade head && python -m alembic downgrade -1 && python -m alembic upgrade head
```

---

## 1. P0-1 在线只读 Demo（含演示数据快照）

### 1.1 背景与缺口依据
实测 `homepage: null`、`has_pages: false`、仓库仅 4 张静态截图（`docs/assets/*.png`）。用户无法在评审现场点开验证，"可展示性"因此停在合格线。

### 1.2 要求
1. 提供**无需后端、无需数据库**即可打开的只读演示站，覆盖 5 个核心页面：盘后分析、选股中心、新闻情报、个股详情、关于页。
2. 演示数据必须来自项目自带生成器 `backend/scripts/pipeline_cli.py demo`，不得手工编造行情。
3. 前端明确标注"演示数据 / 只读"徽标，避免被误认为实时结果。
4. 发布方式优先 GitHub Pages；若因权限无法开启，则交付本地 `npm run preview` 一键命令 + 3 分钟录屏脚本。
5. 演示快照中不得包含真实持仓或任何密钥。

### 1.3 实际实行方案
1. 新增 `backend/scripts/export_demo_snapshot.py`：
   - 复用 `pipeline_cli.py demo` 及 `features` / `scores` / `sector_scores` / `algos` 四个下游任务生成确定性演示库；
   - 调用现有 API 逻辑（非 HTTP，直接调 service/engine 函数）导出 JSON 到 `frontend/public/demo/`；
   - 输出文件按接口拆分：`dashboard.json`、`overview.json`、`algo_list.json`、`algo_ladder.json`、`market_bull_bear.json`、`stock_<code>.json`；
   - 每个文件写入 `generated_at` 与 `is_demo: true` 字段。
2. 改造 `frontend/src/api/` 的请求封装：新增 `VITE_DEMO_MODE` 环境变量；为 1 时所有 GET 请求改为读取 `/demo/*.json`，POST 请求直接返回本地拒绝提示。React Query 配置保持原样，仅替换 fetch 层。
3. 在 `frontend/src/components/` 新增 `DemoBanner.tsx`，在 `App.tsx` 顶部按 `VITE_DEMO_MODE` 渲染只读提示条。
4. 新增 `package.json` 脚本 `build:demo`（`VITE_DEMO_MODE=1 vite build`，输出 `dist-demo/`）。
5. 新增 `.github/workflows/demo-pages.yml`：`push` 到 `main` 时执行 `export_demo_snapshot.py` → `npm run build:demo` → `actions/upload-pages-artifact` → `actions/deploy-pages`。
6. 在 `README.md` 顶部徽标区加入 Demo 链接，并在 `docs/wiki/08-operations.md` 增加"演示站发布与更新"章节。

### 1.4 验收标准
- 未安装 PostgreSQL / Docker 的机器，仅凭 `npm install && npm run preview` 可打开 5 个页面且无控制台报错。
- 演示数据与 `pipeline_cli.py demo` 生成的库逐字段一致（写比对脚本断言）。
- Pages 工作流在 CI 中构建成功；若 Pages 未开启，工作流给出明确失败信息而非静默跳过。
- `npm run build`（正常模式）不受 `VITE_DEMO_MODE` 影响。

### 1.5 风险与局限
- GitHub Pages 是否已开启无法从仓库元数据确认，需人工在仓库设置中启用；未启用时只能交付本地预览。
- 快照是静态的，不随交易日更新；需接受"演示站数据停留在构建日"。
- 只读模式无法演示采集、回测写入等核心链路，必须在页面上说明。

---

## 2. P0-2 样本外 / 滚动窗口验证（与样本内并列展示）

### 2.1 背景与缺口依据
`docs/evidence/V8-backtest-baseline.md` 第 7.2 节明确写"若要把 `pe_max` 改为 12，必须先做样本外验证（例如 2022–2023 或滚动窗口）"，但仓库内未见任何 OOS 产物。当前唯一达标策略 a3 的结论仅成立于 2024-01-02 → 2026-11-13 单一窗口内。

### 2.2 要求
1. 实现 walk-forward 验证：训练窗 / 测试窗严格分离，测试窗起点必须晚于训练窗终点，禁止重叠。
2. **必须复用** `backend/app/strategy/backtest.py` 的成交与费率逻辑，不得新建一套回测引擎。
3. 默认参数：训练 24 个月、测试 6 个月、步进 6 个月；结果至少覆盖 4 个 fold。
4. 数据不足时允许降级为 expanding window，但必须在结果中标注降级类型，不得静默处理。
5. 无论结果好坏，全部 fold 都必须展示；禁止只挑选表现好的 fold。
6. 在样本外结果上不得再次调参后直接报告——若调参，必须重新切分窗口并标注"二次切分"。

### 2.3 实际实行方案
1. 数据准备：先用 `python scripts/pipeline_cli.py history_fast --start 2021-01-01` 与 `backfill --start 2021-01-01 --end 2023-12-31` 补足窗口；若数据源失败，在结果中标注实际可用起始日并按降级规则处理。
2. 新增 `backend/app/engine/oos_validation.py`：
   - `build_folds(start, end, train_months, test_months, step_months)` 返回 fold 列表，内含 `train_start/end`、`test_start/end`、`is_expanding`；
   - `run_oos(algo_code, folds, params)` 对每个 fold 调用现有回测入口，收集年化、Sharpe、最大回撤、超额、成交笔数；
   - 断言 `test_start > train_end`，否则抛 `AppError`。
3. 新增 Alembic 迁移 `20261117_0015_oos_run.py`，建表 `stg_oos_run`：`id`、`algo_code`、`fold_index`、`train_start`、`train_end`、`test_start`、`test_end`、`window_type`、`metrics JSONB`、`param_fingerprint`、`signal_schema`、`created_at`。
4. 新增 CLI `backend/scripts/oos_cli.py`：`python scripts/oos_cli.py --algo a3 --train-months 24 --test-months 6 --step-months 6 --start 2021-01-01 --end 2026-11-13`，终端输出 fold 表 + 汇总，并落库。
5. 扩展 `backend/app/api/v1/backtest.py`：新增 `GET /api/v1/backtest/{code}/oos` 读取 folds，`POST /api/v1/backtest/{code}/oos` 复用现有异步任务框架触发；错误走统一 `AppError` 响应体。
6. 改造 `frontend/src/pages/AlgoCenter.tsx`：增加"样本外验证"标签页，展示 fold 表格（训练/测试区间、四项指标、样本内外对照）、净值曲线（复用现有图表组件）、`is_expanding` 与"样本外"徽标。
7. 扩展 `backend/tests/test_oos_validation.py` 与 `backend/tests/test_api_v5.py`：覆盖窗口无重叠、降级标注、异常入参、API 契约。

### 2.4 验收标准
- CLI 对 a3 输出 ≥4 个 fold，且每个 fold 的 `test_start > train_end`。
- 样本外与样本内指标在同一页面并列，且页面含"样本外不保证未来收益"提示。
- 用同参数重跑一次，13 项指标逐位相等（与 V8 基线复现方式一致）。
- 全量测试无新增失败；迁移 upgrade / downgrade / upgrade 三步通过。

### 2.5 风险与局限
- **最大风险**：样本外结果大概率显著差于样本内。必须在实施前书面约定"无论结果如何全部展示"，否则这项优化会退化成新的选择性报告。
- 2021 年起日线依赖第三方回补，可能失败；失败时必须标注实际窗口，不得缩短训练窗冒充达标。
- OOS 计算量约为单次回测的 fold 数倍，需在 API 侧限制并发并复用信号缓存。

---

## 3. P0-3 数据源健康度与合规说明面板

### 3.1 背景与缺口依据
README 自列大量 `push2*` 重置、腾讯 501 拦截、新浪 DNS 失败场景，且注明"数据仅个人研究，不对外分发或商用"。当前用户只能从 `/health` 和新鲜度徽标被动看到结果，看不到**哪个源在生效、降级了几次**。

### 3.2 要求
1. 采集链路每次 Provider 尝试都要有结构化埋点：源名、任务名、结果（`ok` / `fail` / `skip`）、耗时、脱敏错误码。
2. 提供按数据源 × 任务维度的成功率与降级次数查询接口。
3. 前端展示当前生效源与降级状态，异常时给出可执行的下一步（重跑任务 / 等待冷却）。
4. 合规说明必须落到页面与独立文档，明确用途限制与第三方数据源清单。
5. 埋点不得输出完整 URL、token 或 Webhook 地址。

### 3.3 实际实行方案
1. 在 `backend/app/ingest/service.py` 与 `backend/app/ingest/base.py` 的 Provider 调用入口包裹统一记录函数 `record_provider_attempt()`，写入新表 `meta_provider_health`（迁移 `20261117_0016_provider_health.py`）：`id`、`provider`、`task`、`result`、`duration_ms`、`error_code`、`trade_date`、`created_at`，按日聚合。
2. 扩展 `backend/app/api/v1/meta.py`：新增 `GET /api/v1/meta/provider-health?days=7`，返回按源 × 任务的成功率、降级次数、最近失败原因（脱敏）；无数据时返回结构化空态而非 500。
3. 在 `frontend/src/pages/JobsPage.tsx` 增加"数据源健康"标签页，展示成功率表格 + 降级时间线；在 `frontend/src/pages/DashboardPage.tsx` 的新鲜度徽标旁增加"降级中"状态徽标。
4. 在 `frontend/src/pages/AboutPage.tsx` 增加"数据与合规"区块；新增 `docs/DATA_USAGE_NOTICE.md`，列明数据源清单、用途限制、第三方条款提示。
5. 扩展 `backend/tests/test_ingest.py`、`test_api_v5.py`：模拟某源失败，断言埋点落库为 `fail` 且降级链继续。

### 3.4 验收标准
- 人为让一个 Provider 失败后，接口能在 7 日窗口内查到该次失败与后续降级成功记录。
- 响应与日志中搜索不到任何完整 Webhook / API Key 字符串。
- 前端 build 与视觉验收通过，新增标签页在 4 视口下无溢出。

### 3.5 风险与局限
- 第三方数据源的商用许可状态**无法确认**；合规文档只做风险提示，不构成法律意见。
- 埋点本身有性能开销，需限制单日记录上限（如每任务每源最多 100 条）。

---

## 4. P1-4 条件树可视化规则编辑器（改条件即时预览命中数）

### 4.1 背景与缺口依据
选股当前以 JSON 参数 + API 驱动（`backend/app/strategy/condition.py` 提供 DSL），非开发者无法自行配置；`AlgoCenter.tsx` 已有参数面板但仅覆盖预设策略。

### 4.2 要求
1. 支持 AND / OR 嵌套条件树，字段、算子、阈值均可配置。
2. "预览命中数"必须复用正式选股的过滤与评分链路，口径与正式 run 一致。
3. 预览接口不得写 `stg_run`，不得污染共识榜与每日任务。
4. 预览必须限流与限时，失败时返回统一错误体并给出下一步提示。
5. 前端提供条件树 ↔ JSON 双向转换，便于复制与复现。

### 4.3 实际实行方案
1. 扩展 `backend/app/api/v1/algo.py`：
   - `GET /api/v1/algo/fields` 返回可用字段、算子、取值范围（数据源为 `condition.py` 的字段定义，避免硬编码两份）；
   - `POST /api/v1/algo/preview` 接收条件树 JSON，调用 `apply_common_filters` 与 `_evaluate_strategy` 的 mask 计算路径，返回 `hit_count`、前 20 个代码、各条件通过数、`param_fingerprint`；
   - 超时上限 20 秒，频率上限 10 次/分钟/实例，超限返回 `AppError`。
2. 新增 `frontend/src/components/ConditionTreeEditor.tsx`：
   - 字段与算子下拉来自 `GET /algo/fields`；
   - 支持嵌套分组增删、AND/OR 切换；
   - "预览命中数"按钮调用 preview，侧栏展示结果；
   - JSON 视图可编辑并与图形视图同步。
3. 在 `frontend/src/pages/AlgoCenter.tsx` 增加"自定义规则"入口，嵌入编辑器。
4. 扩展 `backend/tests/test_condition_dsl.py` 与 `test_api_v5.py`：覆盖条件树解析、预览口径一致性（与同名正式策略 mask 对比）、限流与超时。

### 4.4 验收标准
- 同一条件树在 preview 与正式 run 中的 `hit_count` 与股票集合完全一致。
- preview 不产生 `stg_run` 记录（测试断言表行数不变）。
- 编辑器在 4 视口下可用，JSON 与图形视图互转无信息丢失。

### 4.5 风险与局限
- 全市场横截面计算昂贵，复杂条件可能触发 20 秒超时；界面需明确提示"条件过复杂时请缩小范围"。
- 字段清单与后端 DSL 必须同源，否则会出现"前端可选、后端不认"的错配。

---

## 5. P1-5 推送消息增加一句话归因

### 5.1 背景与缺口依据
README 给出的推送示例只有策略名、代码、评分，用户无法判断"为什么选中"。

### 5.2 要求
1. 每个入选股票附一句 ≤50 字归因，内容只能来自 `stg_signal.details` 中的实际条件值。
2. `details` 缺失或字段不足时输出"归因缺失"，**禁止编造**。
3. 归因措辞必须是条件复述，不得出现"建议买入""值得买"等投资建议表述。
4. 保持 4 KB 截断逻辑与幂等去重不变。

### 5.3 实际实行方案
1. 扩展 `backend/app/notify/push.py` 的 `_build_card`：
   - 新增 `_one_line_reason(signal)` 从 `details` 提取 2–3 个关键条件（如 `PE 12.3 < 15`、`股息率 4.8% ≥ 4%`），拼接为短句；无 `details` 返回"归因缺失"；
   - 增加免责尾注："条件复述，非投资建议"。
2. 消息体积控制：归因默认只对前 3 只生成；超过 4 KB 时按现有截断逻辑裁剪，保留尾注。
3. 扩展 `backend/tests/test_push.py`：覆盖归因生成、`details` 缺失、投资建议词过滤、截断后尾注保留。
4. 用 `python scripts/push_cli.py --dry-run --date 2026-11-13` 人工核对最终消息体。

### 5.4 验收标准
- dry-run 输出中每个信号都有一句归因或"归因缺失"标记，无空值。
- 全文搜索不到"建议买入"等违规表述。
- 同一 `run_id` 二次推送仍被 `push_log` 唯一约束拦截，不重复发送。

### 5.5 风险与局限
- `details` 字段可能很大或结构不统一，需做容错解析；解析失败一律降级为"归因缺失"。

---

## 6. P1-6 轻量运行模式（无需 PostgreSQL / Docker 的体验路径）

### 6.1 背景与缺口依据
README 的快速开始需要 Python 3.11 + PostgreSQL 16 + Node 24 + Docker + 可选 Playwright，评审现场部署成本高。

### 6.2 技术选型对比（执行者必须先确认方案）

| 方案 | 做法 | 成本 | 风险 | 结论 |
|---|---|---|---|---|
| A. 多方言 ORM 适配 | 让 SQLAlchemy 同时支持 SQLite 与 PG | 高 | RANGE 分区、SCD2、`pg_try_advisory_lock` 均为 PG 特性，改造后回测结果可能不一致 | **本次不实施** |
| B. 只读快照模式 | 复用 P0-1 的 `export_demo_snapshot.py` + 前端 demo mode | 低 | 只读，不能演示写入链路 | **推荐，本次实施** |
| C. `docker-compose.lite.yml` | 仅 backend + SQLite，砍掉 PG 与 scheduler | 中 | 仍需 Docker，且同 A 的方言风险 | 列为 Phase 2，本次不实施 |

### 6.3 要求（针对方案 B）
1. 提供一条明确路径：无需数据库与 Docker，3 步内打开可交互界面。
2. 与 P0-1 共用同一份快照生成逻辑，不得出现第二套数据导出代码。
3. 明确标注"只读演示"与"完整功能仍需 PostgreSQL"，不得宣称轻量版功能完整。

### 6.4 实际实行方案
1. 直接复用第 1 节的 `backend/scripts/export_demo_snapshot.py` 与 `VITE_DEMO_MODE` 前端改造，不新增后端运行模式。
2. 新增 `docs/wiki/09-dependencies.md` 章节"3 分钟体验路径"，写明：
   ```bash
   npm install
   npm run build:demo
   npm run preview
   ```
3. 在 `README.md` 快速开始之前插入"只想看看效果"小节，链接到该章节。
4. 在 `start.sh` 与 `start-local.ps1` 增加 `demo` 参数分支，调用上述命令（Windows 侧需处理 Node 脚本环境变量写法差异）。

### 6.5 验收标准
- 一台仅安装 Node 的机器按 3 步命令可打开界面。
- 仓库中不存在第二份演示数据导出脚本（用 `grep -r "export_demo"` 验证调用点唯一）。
- README 中轻量模式与完整模式的边界描述清晰。

### 6.6 风险与局限
- 只读模式无法体验采集、选股写入、回测创建等核心链路；这是方案 B 的固有取舍，必须显式告知。
- `start-local.ps1` 修改后需在 Windows 实机验证，CI 为 Linux 环境无法覆盖。

---

## 7. 执行批次、依赖与回滚

| 批次 | 任务 | 依赖 | 说明 |
|---|---|---|---|
| A | P0-1 + P1-6 | 无 | 共用快照导出与 demo mode，**必须合并实施**，避免重复代码 |
| B | P0-2 | 无（可并行，工作量最大） | OOS 迁移编号 0015，需与 C 的 0016 顺序协调 |
| C | P0-3 | 无 | 迁移编号 0016 |
| D | P1-4 | 无 | 依赖 `condition.py` 字段定义，不依赖其他新任务 |
| E | P1-5 | 无 | 推送改动放最后，避免多次改动同一文件 |

**回滚策略**：每批次一个独立提交；迁移按 `0015` → `0016` 顺序追加，不修改历史迁移文件。任一验收失败即 `git revert` 该批次提交并 `alembic downgrade` 到对应版本。

---

## 8. 全局验收清单（全部完成后执行）

- [ ] 后端单元 + 集成测试全绿，无新增失败
- [ ] 前端 build 与三组纯函数测试通过
- [ ] Playwright 视觉验收覆盖新增页面（演示站、样本外验证、数据源健康、规则编辑器）
- [ ] `alembic upgrade head → downgrade -1 → upgrade head` 三步通过
- [ ] CI 四组任务全部成功，新增 `demo-pages.yml` 成功或给出明确权限失败信息
- [ ] `README.md`、`docs/wiki/*`、`docs/CHANGELOG-V5.md` 已同步
- [ ] 全仓库搜索无 `.env` 密钥、Webhook、API Key 泄露
- [ ] 样本外结果无论好坏均已展示，且标注样本内 / 样本外口径

---

## 9. 明确不做的事（防止过度扩张）

1. 不新增选股策略、不调整 A3/A4 参数。
2. 不引入新的回测引擎或费率模型。
3. 不改造多租户权限（项目保持无账号体系的单机自用现状）。
4. 不实现 SQLite 版主库（方案 A 明确排除）。
5. 不把样本内回测收益表述为预期收益或投资建议。
6. 不修改 B1–B5 停用状态与 A1/A2 下线记录。
