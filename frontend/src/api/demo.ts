/**
 * 只读演示模式（V9 P0-1 / P1-6）
 * ================================
 * `VITE_DEMO_MODE=1` 时，GET 请求改读 `public/demo/*.json`；
 * POST/PUT/PATCH/DELETE 一律拒绝，避免演示站产生任何写操作。
 *
 * 这里只做路径映射，不 import ApiError，避免与 client.ts 形成循环依赖。
 */

/** 是否启用演示模式（构建时注入） */
export const DEMO_MODE = import.meta.env.VITE_DEMO_MODE === '1'

/** 精确路径 → 快照文件 */
const EXACT: Record<string, string> = {
  '/health': 'health.json',
  '/market/dashboard': 'dashboard.json',
  '/market/overview': 'overview.json',
  '/market/indices': 'market_indices.json',
  '/market/sectors': 'market_sectors.json',
  '/market/bull-bear': 'market_bull_bear.json',
  '/market/sector-scores/sparkline': 'sector_sparkline.json',
  '/compass/today': 'compass_today.json',
  '/compass/scenarios': 'compass_scenarios.json',
  '/compass/review': 'compass_review.json',
  '/compass/rules': 'compass_rules.json',
  '/algo/list': 'algo_list.json',
  '/algo/fields': 'algo_fields.json',
  '/algo/ladder': 'algo_ladder.json',
  '/algo/consensus': 'algo_consensus.json',
  '/algo/a3': 'algo_result_a3.json',
  '/algo/a3/runs': 'algo_runs_a3.json',
  '/algo/a4': 'algo_result_a4.json',
  '/algo/a4/runs': 'algo_runs_a4.json',
  '/stats': 'news_stats.json',
  '/sources': 'news_sources.json',
  '/articles': 'news_articles.json',
  '/meta/freshness': 'freshness.json',
  '/meta/provider-health': 'provider_health.json',
  '/meta/trade-calendar': 'trade_calendar.json',
  '/stocks/sparkline/batch': 'stock_sparkline.json',
  '/stocks/search': 'stock_search.json',
  '/crawl/auto/status': 'crawl_status.json',
}

/** 把请求路径映射到演示快照文件；未命中返回 null。 */
export function demoPath(path: string): string | null {
  const clean = path.split('?')[0].replace(/\/+$/, '') || '/'
  if (EXACT[clean]) return EXACT[clean]

  const stockKline = /^\/stocks\/(\d{6})\/kline$/.exec(clean)
  if (stockKline) return `stock_${stockKline[1]}_kline.json`

  const stock = /^\/stocks\/(\d{6})$/.exec(clean)
  if (stock) return `stock_${stock[1]}.json`

  return null
}
