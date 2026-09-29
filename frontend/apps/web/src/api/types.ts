/**
 * Shapes returned by the Flask API.
 *
 * Only the fields the UI relies on are declared; the backend serializers in
 * `traceability/serializers.py`, `app.py` and the domain modules remain the
 * source of truth. `AnyRecord` is used where a payload is displayed generically
 * (for example the 47-column purchase-order template).
 */

export type AnyRecord = Record<string, any>

export type Role = 'ADMIN' | 'WAREHOUSE' | 'OPERATIONS'

export const ROLE_LABELS: Record<Role, string> = {
  ADMIN: '管理员',
  WAREHOUSE: '仓管',
  OPERATIONS: '运营',
}

export interface SessionUser {
  id: number | null
  username: string
  displayName: string
  role: Role
  active: boolean
  mustChangePassword: boolean
  lastLoginAt: string | null
  createdAt: string
  updatedAt: string
  productModelIds: number[]
  supplierIds: number[]
  csrfToken?: string
}

export interface Pagination {
  page: number
  pageSize: number
  total: number
  totalPages?: number
}

/** Quality status of a per-unit record or a batch registration. */
export type QualityStatus = 'ASSEMBLED' | 'PASSED' | 'HOLD'
