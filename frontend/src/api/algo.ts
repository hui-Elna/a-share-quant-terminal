/**
 * 选股 API（契约第 5 节）
 */
import { downloadCsv, extractListPage, request } from './client'
import type { ListPage } from '../types/common'
import type {
  AlgoFieldSpec,
  AlgoMeta,
  AlgoParams,
  AlgoResult,
  AlgoRunDetail,
  AlgoRunSummary,
  ConditionNode,
  ConditionPreviewResponse,
  ConsensusResponse,
  LadderResponse,
} from '../types/algo'

/** 算法清单（5.1；含观察候选，便于选股中心展示） */
export function fetchAlgoList(): Promise<AlgoMeta[]> {
  return request<AlgoMeta[]>('/algo/list?include_observation=true')
}

/**
 * 执行算法（5.2）
 * @param params 策略参数（来自 params_schema），会平铺进 query string
 * @param options.tradeDate 指定交易日
 * @param options.forceRefresh 忽略缓存强制重算
 */
export function fetchAlgoResult(
  algoCode: string,
  params: AlgoParams = {},
  options: { tradeDate?: string; forceRefresh?: boolean } = {},
): Promise<AlgoResult> {
  return request<AlgoResult>(`/algo/${encodeURIComponent(algoCode)}`, {
    query: {
      ...params,
      trade_date: options.tradeDate || undefined,
      // 只有显式强制刷新才传，避免 false 也进 query 影响缓存键
      force_refresh: options.forceRefresh ? true : undefined,
    },
  })
}

/** 忽略缓存重跑（5.3 POST /algo/{algo_code}/refresh） */
export function refreshAlgoResult(
  algoCode: string,
  params: AlgoParams = {},
  tradeDate?: string,
): Promise<AlgoResult> {
  return request<AlgoResult>(`/algo/${encodeURIComponent(algoCode)}/refresh`, {
    method: 'POST',
    query: { ...params, trade_date: tradeDate || undefined },
  })
}

/**
 * 历史 run 列表（5.3，不含明细，C1 追加分页元信息）。
 * 保留包裹对象里的 total/has_more/page/page_size；裸数组时仅返回 items。
 */
export async function fetchAlgoRuns(
  algoCode: string,
  limit = 10,
  offset = 0,
): Promise<ListPage<AlgoRunSummary>> {
  const payload = await request<unknown>(`/algo/${encodeURIComponent(algoCode)}/runs`, {
    query: { limit, offset },
  })
  return extractListPage<AlgoRunSummary>(payload)
}

/**
 * 单次 run 明细（5.3）。
 * 契约未定义响应结构，后端按与 5.2 相同的结构返回（已在交付说明中标注）。
 */
export function fetchAlgoRunDetail(runId: string): Promise<AlgoRunDetail> {
  return request<AlgoRunDetail>(`/algo/runs/${encodeURIComponent(runId)}`)
}

/**
 * 导出选股结果 CSV（5.3；后端直接返回 text/csv，文件名如 b1_2026-10-21.csv）。
 * @param limit 导出范围须与页面展示的 run 一致：传入该 run 保存信号的 limit
 */
export function downloadAlgoCsv(
  algoCode: string,
  params: AlgoParams = {},
  tradeDate?: string,
  limit?: number | null,
): Promise<void> {
  return downloadCsv(
    `/algo/${encodeURIComponent(algoCode)}/export.csv`,
    {
      ...params,
      limit: typeof limit === 'number' ? limit : undefined,
      trade_date: tradeDate || undefined,
    },
    `${algoCode}_export.csv`,
  )
}

/** 条件树字段清单（V9 P1-4） */
export function fetchAlgoFields(): Promise<{
  fields: AlgoFieldSpec[]
  operators: string[]
  logic: string[]
}> {
  return request('/algo/fields')
}

/** 条件树只读预览（不写 stg_run） */
export function previewConditionTree(payload: {
  tree: ConditionNode
  trade_date?: string
  limit?: number
}): Promise<ConditionPreviewResponse> {
  return request('/algo/preview', {
    method: 'POST',
    body: payload,
    timeoutMs: 25_000,
    retry: 0,
  })
}

/**
 * 策略天梯（5.4）
 * @param window 回看交易日数：20/60/120
 * @param horizon 收益口径：5/10/20
 */
export function fetchLadder(
  window: number,
  horizon: number,
  tradeDate?: string,
): Promise<LadderResponse> {
  return request<LadderResponse>('/algo/ladder', {
    query: { window, horizon, trade_date: tradeDate || undefined },
  })
}

/**
 * 多策略共识榜（5.5）
 * @param minCount 最少命中策略数（后端 2–7）
 */
export function fetchConsensus(
  minCount: number,
  tradeDate?: string,
  limit = 100,
  offset = 0,
): Promise<ConsensusResponse> {
  return request<ConsensusResponse>('/algo/consensus', {
    query: { min_count: minCount, limit, offset, trade_date: tradeDate || undefined },
  })
}
