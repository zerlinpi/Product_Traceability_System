/**
 * Batch traceability (走步机整批溯源): production batches, field registration,
 * read-only trace query and batch quality.
 */
import type { AnyRecord } from '../types'
import api from '../index'

/** Query parameters accepted by GET /api/batch-trace-records. */
export interface BatchRecordFilters {
  /** 批次码（模糊匹配） */
  batchCode?: string
  /** 起始时间，须与 to 同时提供 */
  from?: string
  /** 结束时间 */
  to?: string
}

export default {
  /** GET /api/production-batches */
  productionBatches: () => api.get<AnyRecord[]>('/api/production-batches'),
  /** GET /api/production-batches/:id — flat batch dict + `registration` + `reverseTrace`. */
  productionBatch: (id: number) => api.get<AnyRecord>(`/api/production-batches/${id}`),
  /** POST /api/production-batches (ADMIN, WAREHOUSE) — idempotent scope `production-batches.create`. */
  createProductionBatch: (data: { productModelId: number, prefix: string, quantity: number }) => api.post<AnyRecord>('/api/production-batches', data, { idempotent: 'production-batches.create' }),
  /** PNG QR image of a production batch (same-origin GET, usable as <img src>). */
  productionBatchQrUrl: (id: number) => `/api/production-batches/${id}/qr`,

  /** POST /api/batch-entry/scan (ADMIN, WAREHOUSE) — idempotent scope `batch-entry.scan`. */
  batchEntryScan: (data: AnyRecord) => api.post<AnyRecord>('/api/batch-entry/scan', data, { idempotent: 'batch-entry.scan' }),

  /** POST /api/batch-trace/query — read-only, no side effects. */
  batchTraceQuery: (code: string) => api.post<AnyRecord>('/api/batch-trace/query', { code }),

  /** GET /api/batch-trace-records */
  batchTraceRecords: (filters: BatchRecordFilters = {}) => api.get<AnyRecord[]>('/api/batch-trace-records', { params: { ...filters } }),
  /** POST /api/batch-trace-records/:id/pass (ADMIN) */
  passBatchRecord: (id: number) => api.post<AnyRecord>(`/api/batch-trace-records/${id}/pass`, {}),
  /** POST /api/batch-trace-records/:id/hold (ADMIN) — reason required. */
  holdBatchRecord: (id: number, reason: string) => api.post<AnyRecord>(`/api/batch-trace-records/${id}/hold`, { reason }),
}
