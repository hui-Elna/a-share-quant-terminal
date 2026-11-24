# 07 · 前端应用

对应代码：`frontend/src/`（112 个文件，约 20.9k 行）。

---

## 1. 技术栈与构建

| 项 | 选择 |
|---|---|
| 框架 | React 18 + TypeScript 5.5 |
| 构建 | Vite 5 + `@vitejs/plugin-react` |
| 路由 | React Router 6（`BrowserRouter`，页面全部懒加载） |
| 数据 | TanStack Query 5（查询缓存/轮询） |
| 样式 | Tailwind CSS 4（`@theme` 令牌） |
| 图表 | Recharts（趋势/柱/雷达）、lightweight-charts（K 线） |
| 动效 | framer-motion（尊重 `prefers-reduced-motion`） |
| 图标 | lucide-react |

命令：

```bash
npm run dev          # http://localhost:5173
npm run build        # tsc -b && vite build
npm run preview      # 构建产物预览（同样代理 /api）
npm run test:treemap # node --test
npm run test:sparkline
npm run test:palette
npm run test:sample
```

Vite 代理（[`vite.config.ts`](../../frontend/vite.config.ts)）：

- `/api` → `http://localhost:8000`
- `/health` → `http://localhost:8000`
- `server` 与 `preview` 都显式配置，避免预览时 `/api` 404；
- 生产构建按 `vendor-react / vendor-motion / vendor-icons / vendor-charts / vendor-kline` 分包。

---

## 2. 路由表

[`src/App.tsx`](../../frontend/src/App.tsx)

| 路径 | 页面 | 用途 |
|---|---|---|
| `/` | `HomePage` | 新闻情报：列表、搜索、自动抓取 |
| `/article/:id` | `DetailPage` | 新闻详情 + AI 摘要 |
| `/dashboard` | `DashboardPage` | 盘后分析：指数/板块热力/评分榜/两融/AI 日报 |
| `/index-compare` | `IndexComparePage` | 风格轮动 + 6 宽基牛熊估值 |
| `/algo-center` | `AlgoCenter` | 选股中心：策略/参数/结果/天梯/共识/回测/矩阵 |
| `/ladder` | 重定向 | `/algo-center#ladder` |
| `/paper` | `PaperPage` | 纸面组合净值/持仓/待执行 |
| `/stocks/:code` | `StockDetailPage` | K 线 + 指标 + 因子雷达 + 所属板块 + 信号 |
| `/jobs` | `JobsPage` | 数据新鲜度 + 任务监控 + 一键补数 |
| `/about` | `AboutPage` | 说明与免责声明 |
| `*` | `NotFoundPage` | 404 |

所有页面 `lazy()` 加载；`Layout` 常驻（Navbar + Outlet + Footer + CommandPalette + 状态中心）。

---

## 3. 分层结构

```
pages/（页面编排）
  └─ hooks/（React Query：查询、轮询、mutation）
       └─ api/（HTTP 封装 + 类型）
            └─ api/client.ts（BASE_URL / 超时 / 错误体 / 重试 / CSV）
components/（可复用 UI/图表）
lib/（纯函数 + 本地状态）
types/（后端契约 TS 类型）
```

全局配置（[`main.tsx`](../../frontend/src/main.tsx)）：

- `QueryClient` 默认 `retry: false`（重试统一交给 `client.ts`，避免 4xx 重复请求）；
- `networkMode: 'always'`（断网时也真实发请求，由 client 报错，而不是挂起）；
- `refetchOnWindowFocus: false`；
- `staleTime: 5 分钟`；
- `QueryCache/MutationCache.onError` 把异常记入 `statusFeed`（状态中心可追溯）。

---

## 4. 页面说明

### 4.1 `HomePage` — 新闻情报

- 数据：`useArticles(page, sourceId)`、`useSearch(q, page)`、`useAutoCrawl()`；
- 顶部 `StatsBar`、来源 `FilterTabs`、`ArticleCard` 列表、`PaginationBar`；
- 自动抓取：同一浏览器会话 10 分钟最多触发一次，轮询 `/crawl/auto/status`；
- 抓取完成写入 `statusFeed` 并在页面提示新增条数。

### 4.2 `DashboardPage` — 盘后分析

[`pages/DashboardPage.tsx`](../../frontend/src/pages/DashboardPage.tsx)

| 区块 | 数据 |
|---|---|
| 交易日选择 + 刷新 + 新鲜度 | `useTradeCalendar`、`useDashboard`、`FreshnessBadge` |
| 指数卡片 | `dashboard.indices` / `us_indices`；`IndexCard` + `Sparkline` |
| 融资融券 | `dashboard.margin_data`；北向为空显示“暂停披露” |
| 板块热力图 | `SectorHeatmap`（treemap，面积=市值，颜色=涨跌幅） |
| 综合评分榜 | `ScoreRanking` + `/market/sector-scores/sparkline` |
| 领涨/领跌 | `dashboard.top_sectors/bottom_sectors` |
| AI 复盘 | `AIReport` 渲染 `daily_report` Markdown |
| 市场状态 | `MarketOverview` → `/market/overview`（`RegimeCard`、`AllocationCard`、`ThermometerCard`） |

### 4.3 `IndexComparePage` — 指数对比

- 数据：`useIndexCompare` → `/market/index-compare`；
- 顶部比值刻度 `RatioScale` + 三态仓位；
- 比价走势（Recharts `ComposedChart`，含滚动分位参考线）；
- `BullBearValuation` 组件：6 宽基四面板（走势/回撤×盈利收益率/动量热力/回撤史+ERP）；
- 所有数值由后端计算，前端不自行推算 R。

### 4.4 `AlgoCenter` — 选股中心

[`pages/AlgoCenter.tsx`](../../frontend/src/pages/AlgoCenter.tsx)

流程：

```
加载 /algo/list（含观察候选）
  → 恢复 URL/会话状态（algo/date/run/参数/表格滚动）
  → 参数面板（schema 驱动，不自动请求）
  → 点击「执行算法」useMutation → /algo/{code}
  → RunMetaPanel（run_id/耗时/universe/data_version/参数快照/from_cache）
  → AlgoDataTable 动态列 + 排序/展开/分页
  → AlgoBarChart Top10
  → CSV 导出
  → 历史 run 切换 / 强制刷新
  → StrategyLadder（天梯）
  → ConsensusBoard（多策略共识）
  → BacktestPanel + SensitivityMatrix（回测/参数矩阵）
```

状态恢复：`lib/algoSession.ts` 用 `sessionStorage` 按策略保存参数/日期/run，按 `run_id` 保存表格状态；URL 支持 `?algo=&date=&run=&p.<param>=`，刷新/分享/前进后退可还原。

### 4.5 `PaperPage` — 纸面组合

- 数据：`usePaperPortfolios`、`usePaperDetail`、`usePaperSync`；
- 组合卡片（净值/超额/回撤/跟踪天数）；
- 净值 vs 基准面积图、回撤面积图；
- 当前持仓表、下一交易日待执行清单（`fillable=false` 显示停牌/涨跌停原因）；
- “与回测差值”面板；
- 同步进行中 3 秒轮询，页面隐藏即停。

### 4.6 `StockDetailPage` — 个股详情

- `useStock` / `useKline` / `useStockSignals`；
- `KlineChart`（懒加载 lightweight-charts）：蜡烛图 + MA/BOLL + 成交量 + 回测买卖标记；
- 指标卡：RSI/MACD/ATR/波动率/量比/动量；
- `AlgoRadarChart` 六维因子；
- 所属板块、最近信号、新鲜度徽标。

### 4.7 `JobsPage` / `DetailPage` / `AboutPage`

| 页面 | 要点 |
|---|---|
| `JobsPage` | 数据集新鲜度总览 + 任务表 + 30 秒自动刷新（可暂停）+ 一键补数（`triggerCatchUp`） |
| `DetailPage` | 新闻正文、AI 摘要、来源与时间 |
| `AboutPage` | 系统说明、数据边界、免责声明 |

---

## 5. API 层

| 文件 | 对应后端 | 主要函数 |
|---|---|---|
| `api/client.ts` | 通用 | `request`、`buildUrl`、`buildQueryString`、`downloadCsv`、`extractList`、`extractListPage`、`ApiError` |
| `api/market.ts` | `/market/*` | `fetchDashboard`、`fetchIndexSeries`、`fetchSectors`、`fetchSectorDetail`、`fetchMarketOverview`、`fetchIndexCompare`、`fetchBullBearValuation` |
| `api/algo.ts` | `/algo/*` | `fetchAlgoList`、`fetchAlgoResult`、`refreshAlgoResult`、`fetchAlgoRuns`、`fetchAlgoRunDetail`、`fetchLadder`、`fetchConsensus`、`downloadAlgoCsv` |
| `api/backtest.ts` | `/backtest/*` | `runBacktest`、`fetchBacktestStatus/Result`、`fetchBacktestTrades`、`downloadBacktestCsv`、`createBacktestGrid`、`fetchBacktestGrid`、`retryBacktestGrid` |
| `api/stock.ts` | `/stocks/*` | `fetchStock`、`fetchKline`、`searchStocks`、`fetchSparklineBatch`、`fetchBenchmark` |
| `api/paper.ts` | `/paper/*` | `fetchPaperPortfolios`、`fetchPaperDetail`、`syncPaperPortfolio` |
| `api/meta.ts` | `/meta/*`、`/health`、`/admin/catch-up` | `fetchFreshness`、`fetchTradeCalendar`、`fetchJobs`、`fetchHealth`、`triggerCatchUp` |
| `api/news.ts` | `/articles`、`/search`、`/stats/*`、`/crawl/*` | 新闻列表/详情/搜索/统计/抓取 |

### 5.1 `client.ts` 约定

- `BASE_URL = '/api/v1'`；`/health` 通过 `baseUrl: ''` 覆盖；
- 默认 30s 超时（AbortController）；
- 解析后端统一错误体为 `ApiError`（`code/message/detail/requestId/suggestion`）；
- 只对 5xx / 网络错误重试 1 次，4xx 不重试；
- `isRetryable = status===0 || status>=500`；
- CSV 下载统一封装，文件名由调用方指定。

---

## 6. Hooks

| Hook | 作用 | 关键策略 |
|---|---|---|
| `useDashboard` | 仪表盘 | 最新视图 10 分钟轮询；历史视图不轮询 |
| `useIndexSeries` | 指数序列 | 按交易日缓存 |
| `useSectorScoreSparkline` | 板块评分序列 | 按代码集合排序去重作为 key |
| `useTradeCalendar` | 交易日历 | 6 小时 stale |
| `useAlgoList` | 策略清单 | 5 分钟 |
| `useAlgoRun` | 执行策略 | mutation，成功后失效 run 列表 |
| `useAlgoRuns` / `useAlgoRunDetail` | 历史 run | 60s / 5min |
| `useLadder` / `useConsensus` | 天梯/共识 | 1 分钟；共识默认折叠不请求 |
| `useBacktest*` | 回测/矩阵 | 1.5s 轮询，10 分钟上限；矩阵 3s 轮询，完成后停 |
| `usePaperPortfolios/Detail` | 纸面组合 | 同步中 3s；隐藏标签页停止 |
| `useStock/useKline/useSparklineBatch` | 个股 | 5 分钟；批量 sparkline 一次请求 |
| `useArticles/useSearch/useAutoCrawl` | 新闻 | 翻页保留旧数据；自动抓取 10 分钟节流 |
| `useFreshness` / `useJobs` | 元信息 | 任务页自动刷新 |
| `useDelayedFlag` | 骨架屏延迟 | 避免闪屏 |

---

## 7. 关键组件

| 组件 | 用途 | 关键点 |
|---|---|---|
| `Layout` | 全局骨架 | Navbar + Outlet + Footer + 命令面板 + 状态中心 |
| `Navbar` | 导航 | 搜索、主题切换、状态中心 |
| `CommandPalette` | Ctrl/Cmd+K | 股票（250ms debounce）/页面/策略；自研，无 cmdk |
| `FreshnessBadge` | 数据新鲜度 | 消费 `/meta/freshness` |
| `DataTable` | 通用表格 | 排序、展开、分页、粘性表头 |
| `AlgoDataTable` | 选股结果 | 按策略 metrics 动态列 |
| `RunMetaPanel` | run 元信息 | run_id/耗时/universe/data_version/参数/来源/强制刷新 |
| `AlgoParamPanel` | 参数表单 | schema 驱动；参数变化不自动请求 |
| `StrategyLadder` | 策略天梯 | 5/10/20 日窗口、胜率/超额 |
| `ConsensusBoard` | 多策略共识 | 命中策略数、每策略 rank/score |
| `BacktestPanel` | 回测表单 | 区间/调仓/持仓/费率/滑点 |
| `BacktestResultView` | 回测结果 | 指标、净值、回撤、月度收益、行业暴露 |
| `BacktestExtras` | 派生视图 | 月度收益/回撤/滚动超额/行业暴露 |
| `SensitivityMatrix` | 参数矩阵 | 热力图、单格展开、失败重试 |
| `KlineChart` | K 线 | lightweight-charts；蜡烛/MA/BOLL/成交量/买卖标记 |
| `SectorHeatmap` | 板块热力图 | 手写 squarified treemap；前 80 折叠“其他”；抽屉详情 |
| `ScoreRanking` | 评分榜 | Top10 + sparkline |
| `ThermometerCard` / `RegimeCard` / `AllocationCard` | 市场状态 | 温度档位、五态、三档配置 |
| `BullBearValuation` | 牛熊估值 | 6 宽基四面板 |
| `IndexCard` | 指数卡 | 多周期收益、sparkline、排名 |
| `MarketOverview` | 市场概览 | 组合上述状态组件 |
| `AIReport` / `Markdown` | AI 日报 | Markdown 渲染 |
| `StateViews` | 空/错/骨架 | 统一页面状态 |
| `StatusCenter` | 状态中心 | sessionStorage 流水，30 秒去重 |
| `AmbientBackdrop` / `IntroCurtain` | 视觉氛围 | 尊重 reduced motion |

---

## 8. lib 纯函数与本地状态

| 文件 | 作用 |
|---|---|
| `lib/treemap.ts` | Squarified treemap 布局；`squarify`；过滤非正权重；面积严格按比例 |
| `lib/sparkline.ts` | 迷你走势路径生成 |
| `lib/palette.ts` | 图表/主题配色工具 |
| `lib/color.ts` | `trendHeat`、涨跌色工具 |
| `lib/format.ts` | 金额/百分比/日期/成交量/评分格式化 |
| `lib/csv.ts` | 前端 CSV 导出 |
| `lib/navigation.ts` | `NAV_ITEMS`、命令面板事件 |
| `lib/statusFeed.ts` | 会话级状态流水（sessionStorage，50 条上限，30s 去重） |
| `lib/motionPrefs.ts` | 动效偏好 |
| `lib/algoSession.ts` | 选股中心参数/日期/run/表格状态 + URL 序列化 |
| `lib/backtestSession.ts` | 回测任务/矩阵状态持久化（跨刷新恢复） |
| `lib/algoParams.ts` | 参数快照 diff、默认值处理 |
| `lib/market.ts` | 市场相关小工具 |
| `lib/utils.ts` | `cn` 等 |

### 8.1 `algoSession.ts`

- 上下文键：`gfna:algo:context:v1:<algo>`；
- 表格状态键：`gfna:algo:table:v1:<run_id>`；
- URL 参数：`algo`、`date`、`run`、`p.<参数>`；
- sessionStorage 不可用时安全降级为 no-op。

---

## 9. 类型契约

`src/types/` 与后端响应一一对应：

| 文件 | 内容 |
|---|---|
| `common.ts` | `Freshness`、`PaginatedResponse`、`ApiErrorBody`、`ListPage` |
| `market.ts` | `DashboardData`、`IndexQuote`、`SectorQuote`、`MarketOverview`、`IndexCompareResult`、`BroadValuation` |
| `algo.ts` | `AlgoMeta`、`AlgoParams`、`AlgoResult`、`AlgoRun`、`ConsensusResponse`、`LadderResponse` |
| `backtest.ts` | `BacktestOptions`、`BacktestResult`、`BacktestGrid*` |
| `stock.ts` | `StockLatest`、`StockFeatures`、`KlinePoint` |
| `paper.ts` | `PaperPortfolioCard`、`PaperDetail`、`PaperHolding`、`PaperPendingOrder` |
| `jobs.ts` | `Job`、`JobQuery` |

> 后端字段新增时，先更新 `types/`，再更新页面/组件；不要在组件里写内联 any。

---

## 10. 设计系统

[`src/index.css`](../../frontend/src/index.css) 定义「墨 · 铜 · 纸」：

| 令牌 | 浅色 | 深色 |
|---|---|---|
| 背景 | `#f4f1e9` 暖纸 | `#0f1110` |
| 卡片 | `#fcfaf4` | `#171a18` |
| 主色 | 墨 `#1b1d21` | `#f1eee4` |
| 品牌高光 | 铜 `#a3762f` | `#d3a55f` |
| 涨 | `#be2b22`（红） | `#f0655a` |
| 跌 | `#0e7350`（绿） | `#46c58d` |
| 状态色 | success/warning/alert | 对应提亮 |

规则：

- 涨跌红绿是全页唯一强饱和色；按钮/链接用墨色，不抢注意力；
- 1px 发丝线 + 低对比阴影构建层级，不用厚重卡片；
- 数字统一 `font-mono`；中文优先本机无衬线；
- 市场五态有独立色（trend/repair/bottom/pull/riskoff）；
- 温度有冷→热色带；
- 动效尊重 `prefers-reduced-motion`。

主题切换：`ThemeProvider` 给 `<html>` 加/移除 `.dark`，Tailwind 4 用 `@custom-variant dark` 支持 class 切换。

---

## 11. 错误、空态与加载

- `StateViews` 提供 `LoadingState` / `EmptyState` / `ErrorState` / 骨架屏；
- `ApiError` 的 `code / message / suggestion / requestId` 直接展示；
- `DATA_NOT_READY` 是业务未就绪，不记入状态中心；
- 骨架屏用 `useDelayedFlag` 延迟显示，避免闪烁；
- 页面隐藏时停止轮询（`refetchIntervalInBackground: false`）。

---

## 12. 扩展一个页面

1. `pages/NewPage.tsx`，默认导出；
2. `App.tsx` 增加 `lazy()` 与 `<Route>`；
3. `lib/navigation.ts` 若需要导航项，加 `NAV_ITEMS`；
4. `api/<module>.ts` 封装端点；
5. `hooks/use<Feature>.ts` 用 React Query 封装查询/轮询；
6. `types/` 补契约类型；
7. 复用 `PageHeader/SectionHeader/DataTable/StateViews/FreshnessBadge`；
8. 页面隐藏时停止轮询，错误走 `ApiError` 统一提示。
