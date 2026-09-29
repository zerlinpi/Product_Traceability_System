/**
 * Legacy per-unit records (历史逐台记录) — read-only for treadmills, still
 * editable for non-treadmill products — plus genealogy lookups and the
 * admin dashboard.
 */
import type { AnyRecord } from '../types'
import api from '../index'

/** PUT /api/records/status/bulk accepts at most this many record ids per call. */
export const RECORD_STATUS_BULK_LIMIT = 200

/** Quality transitions accepted by the record status routes (VOID is read-only). */
export type RecordQualityStatus = 'ASSEMBLED' | 'PASSED' | 'HOLD'

/** Query parameters accepted by GET /api/records. */
export interface RecordFilters {
  productModelId?: number | string
  generationBatchId?: number | string
  /** ASSEMBLED | PASSED | HOLD | VOID */
  status?: string
  search?: string
  /** YYYY-MM-DD */
  dateFrom?: string
  /** YYYY-MM-DD */
  dateTo?: string
}

export default {
  /** GET /api/dashboard (ADMIN) */
  dashboard: () => api.get<AnyRecord>('/api/dashboard'),

  /** GET /api/records (ADMIN, WAREHOUSE) */
  records: (filters: RecordFilters = {}) => api.get<AnyRecord[]>('/api/records', { params: { ...filters } }),
  /** PUT /api/records/:id/status (ADMIN) */
  updateRecordStatus: (id: number, data: { status: string, reason?: string }) => api.put<AnyRecord>(`/api/records/${id}/status`, data),
  /** PUT /api/records/status/bulk (ADMIN) — up to 200 records in one transaction. */
  updateRecordStatusBulk: (data: { recordIds: number[], status: string, reason?: string }) => api.put<AnyRecord>('/api/records/status/bulk', data),
  /** PUT /api/records/:id — correct part codes / remarks; the record returns to 待检. */
  updateRecord: (id: number, data: { partCodes: string[], remarks?: string }) => api.put<AnyRecord>(`/api/records/${id}`, data),
  /** DELETE /api/records/:id — reason required; the code set can be re-entered afterwards. */
  deleteRecord: (id: number, reason: string) => api.delete<AnyRecord>(`/api/records/${id}`, { data: { reason } }),
  recordsExportUrl: '/api/records/export.xlsx',

  /** GET /api/genealogy?code= — main code or part code. */
  genealogy: (code: string) => api.get<AnyRecord>('/api/genealogy', { params: { code } }),
  machineQrUrl: (machineId: number) => `/api/machines/${machineId}/qr`,
  partLabelQrUrl: (labelId: number) => `/api/part-labels/${labelId}/qr`,
}
